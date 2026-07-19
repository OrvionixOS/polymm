"""
Odds Coverage Filter — Prevents SpreadBot from trading markets already covered
by external odds in sports_odds_v2.

Queries sports_odds_v2 periodically and builds a lookup:
    {(norm_team1, norm_team2): {market_type: set(lines)}}

SpreadBot calls is_covered(team1, team2, market_type, line) to check
if a specific market+line is already handled by the SportsBot.
"""
import asyncio
import time
import re
from collections import defaultdict
from typing import Dict, Set, Tuple, Optional

from src.core.match_id import normalize_team
from src.data.supabase_client import SupabaseClient


def _team_pair_key(t1: str, t2: str) -> Tuple[str, str]:
    """Canonical sorted key from two normalized team names."""
    n1 = normalize_team(t1)
    n2 = normalize_team(t2)
    return tuple(sorted([n1, n2]))


class OddsCoverageFilter:
    """Filter that checks whether a market is already covered by external odds.
    
    Usage:
        filter = OddsCoverageFilter(refresh_interval=300)
        await filter.refresh()  # Initial load
        
        if filter.is_covered("Butler Bulldogs", "Creighton Bluejays", "h2h"):
            # Skip — SportsBot handles this
            pass
    """
    
    def __init__(self, refresh_interval: int = 300):
        self._refresh_interval = refresh_interval
        self._last_refresh: float = 0
        self._lookup: Dict[Tuple[str, str], Dict[str, Set[Optional[float]]]] = {}
        self._record_count = 0
    
    async def refresh(self) -> None:
        """Fetch all records from sports_odds_v2 and rebuild the lookup."""
        sb = SupabaseClient()
        all_records = []
        page_size = 1000
        offset = 0
        
        while True:
            result = (
                sb.client.table("sports_odds_v2")
                .select("team1, team2, market_type, line")
                .range(offset, offset + page_size - 1)
                .execute()
            )
            rows = result.data or []
            all_records.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size
        
        # Build lookup: (norm_t1, norm_t2) sorted → {market_type → set(lines)}
        lookup: Dict[Tuple[str, str], Dict[str, Set[Optional[float]]]] = defaultdict(
            lambda: defaultdict(set)
        )
        for r in all_records:
            key = _team_pair_key(r["team1"], r["team2"])
            mt = r["market_type"]
            line = r.get("line")
            if line is not None:
                line = float(line)
            lookup[key][mt].add(line)
        
        self._lookup = dict(lookup)
        self._record_count = len(all_records)
        self._last_refresh = time.time()
        print(f"   📊 [ODDS FILTER] Loaded {self._record_count} odds records, "
              f"{len(self._lookup)} unique matches")
    
    async def ensure_fresh(self) -> None:
        """Refresh if stale (past refresh_interval)."""
        if time.time() - self._last_refresh > self._refresh_interval:
            await self.refresh()
    
    def _find_match(self, team1: str, team2: str) -> Optional[Dict[str, Set[Optional[float]]]]:
        """Find odds coverage for a team pair.
        
        Tries exact normalized key first, then containment fallback.
        """
        key = _team_pair_key(team1, team2)
        
        # Fast path: exact
        if key in self._lookup:
            return self._lookup[key]
        
        # Slow path: containment matching (same logic as analytics/sports_coverage.py)
        n1 = normalize_team(team1)
        n2 = normalize_team(team2)
        
        def contains(a: str, b: str) -> bool:
            return len(a) >= 3 and len(b) >= 3 and (a in b or b in a)
        
        for odds_key, markets in self._lookup.items():
            o1, o2 = odds_key
            if (contains(n1, o1) and contains(n2, o2)) or \
               (contains(n1, o2) and contains(n2, o1)):
                return markets
        
        return None
    
    def is_covered(
        self,
        team1: str,
        team2: str,
        market_type: str,
        line: Optional[float] = None,
    ) -> bool:
        """Check if a specific market+line is covered by external odds.
        
        Args:
            team1: First team name (will be normalized)
            team2: Second team name (will be normalized)
            market_type: Odds market type ('h2h', 'spreads', 'totals')
            line: Specific line value (e.g., 2.5 for O/U 2.5). None for h2h.
        
        Returns:
            True if we have external odds for this exact market+line combo.
        """
        match_markets = self._find_match(team1, team2)
        if match_markets is None:
            return False
        
        if market_type not in match_markets:
            return False
        
        # For h2h, any coverage means covered (no line to check)
        if market_type == "h2h":
            return True
        
        # For spreads/totals, check if the specific line exists
        if line is not None:
            lines = match_markets[market_type]
            # Match on absolute value (spreads can be +/- in different sources)
            abs_line = abs(line)
            return any(
                l is not None and abs(float(l)) == abs_line
                for l in lines
            )
        
        # No line provided — if the market type exists at all, it's covered
        return True
    
    @property
    def match_count(self) -> int:
        return len(self._lookup)
    
    @property
    def record_count(self) -> int:
        return self._record_count


def _safe_float(s: str) -> Optional[float]:
    """Convert string to float, returning None on failure."""
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_spread_opportunity_market(
    bin_label: str,
    question: str,
) -> Tuple[str, Optional[float]]:
    """Extract odds market type and line from a SpreadBot opportunity or order team name.
    
    Handles both bin_label format (from scan) and order.team format (from hydrated orders):
    - bin_label: "O/U 145.5", "Spread: Team (-3.5)"
    - order.team: "Gil Vicente FC: O/U 3.5", "Spread: Butler (-3.5)"
    
    Returns:
        (market_type, line) where market_type is 'h2h', 'spreads', or 'totals'
        and line is the numeric line value (or None for h2h).
    """
    bl = (bin_label or "").strip()
    q_lower = (question or "").lower()
    
    # O/U markets: "O/U 145.5", "O/U 2.5", "Team: O/U 3.5"
    ou_match = re.search(r'O/U\s+(\d+\.?\d*)', bl, re.IGNORECASE)
    if ou_match:
        val = _safe_float(ou_match.group(1))
        if val is not None:
            return ("totals", val)
    
    # Spread markets: "Spread: Team (-3.5)", "Spread: Butler (+3.5)"
    spread_match = re.search(r'Spread.*?\(([-+]?\d+\.?\d*)\)', bl)
    if spread_match:
        val = _safe_float(spread_match.group(1))
        if val is not None:
            return ("spreads", abs(val))
    
    # Question-based spread detection
    if "spread" in q_lower:
        spread_q = re.search(r'[-+]?(\d+\.?\d*)', q_lower.split("spread")[-1])
        if spread_q:
            val = _safe_float(spread_q.group(1))
            if val is not None:
                return ("spreads", val)
    
    # Totals in question: "Over/Under X.X" or "O/U" in question
    if "over" in q_lower or "under" in q_lower or "o/u" in q_lower:
        totals_match = re.search(r'(\d+\.?\d*)', q_lower.split("over")[-1] if "over" in q_lower else q_lower)
        if totals_match:
            val = _safe_float(totals_match.group(1))
            if val is not None:
                return ("totals", val)
    
    # Default: winner/moneyline
    return ("h2h", None)
