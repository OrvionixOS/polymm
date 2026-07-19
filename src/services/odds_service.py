"""
Unified Odds Service - Reads fresh odds from Supabase (populated by local scrapers).

For Fly.io deployment, this reads from Supabase instead of scraping directly,
avoiding geo-blocking issues.
"""
import asyncio
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from dotenv import load_dotenv

from src.data.recorder import get_recorder
from src.core.match_id import normalize_team, normalize_game, make_match_id

load_dotenv()


@dataclass
class MatchOdds:
    """Odds data for a single match from a single source."""
    match_id: str
    team1: str
    team2: str
    odds1: float
    odds2: float
    fair_prob1: float  # No-vig probability for team1 (%)
    fair_prob2: float
    source: str        # source_b, source_l, source_n, the-odds-api
    game: str          # cs2, dota2, lol, rugby, football
    is_live: bool
    market_type: str = "h2h"  # h2h, spreads, totals
    line: float = 0  # 0 for h2h, 1.5 for spreads, 2.5 for totals
    fair_prob_draw: Optional[float] = None  # For 3-way sports (rugby, soccer)
    start_time: Optional[datetime] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AggregatedMatch:
    """Aggregated odds from multiple sources."""
    match_id: str
    team1: str
    team2: str
    game: str
    is_live: bool
    market_type: str = "h2h"  # h2h, spreads, totals
    line: float = 0  # 0 for h2h, 1.5 for spreads, 2.5 for totals
    outcome1_name: str = ""  # For totals: "Over", for spreads: team name
    outcome2_name: str = ""  # For totals: "Under", for spreads: team name
    team1_spread_point: Optional[float] = None  # Signed spread for team1 (e.g., +1.5 or -1.5)
    sources: Dict[str, MatchOdds] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def fair_prob1(self) -> float:
        """Median probability across sources (more robust to outliers).
        
        Uses median when multiple sources available, otherwise returns the single value.
        Returns 0.0 if no sources available - caller should check and skip.
        """
        if not self.sources:
            return 0.0
        probs = [m.fair_prob1 for m in self.sources.values() if m.fair_prob1 > 0]
        if not probs:
            return 0.0
        return statistics.median(probs) if len(probs) > 1 else probs[0]
    
    @property
    def fair_prob2(self) -> float:
        """Median probability across sources (more robust to outliers).
        
        Uses median when multiple sources available, otherwise returns the single value.
        Returns 0.0 if no sources available - caller should check and skip.
        """
        if not self.sources:
            return 0.0
        probs = [m.fair_prob2 for m in self.sources.values() if m.fair_prob2 > 0]
        if not probs:
            return 0.0
        return statistics.median(probs) if len(probs) > 1 else probs[0]
    
    @property
    def num_sources(self) -> int:
        return len(self.sources)
    
    def is_fresh(self, max_age_live: int = 60, max_age_prematch: int = 1800) -> bool:
        """Check if data is fresh enough to act on.

        Args:
            max_age_live: Max age in seconds for live matches (default 60s).
                Aligned with the Rust bot's `fair_feed` default 2026-06-12
                so the same staleness rule applies across both codebases.
            max_age_prematch: Max age in seconds for prematch (default 1800s = 30 min)
        """
        max_age = max_age_live if self.is_live else max_age_prematch
        age = (datetime.now(timezone.utc) - self.timestamp).total_seconds()
        return age <= max_age
    
    @property
    def fair_prob_draw(self) -> Optional[float]:
        """Median draw probability across sources (for 3-way sports).
        
        Returns None if not a 3-way match (esports).
        """
        if not self.sources:
            return None
        probs = [m.fair_prob_draw for m in self.sources.values() if m.fair_prob_draw is not None and m.fair_prob_draw > 0]
        if not probs:
            return None
        return statistics.median(probs) if len(probs) > 1 else probs[0]
    
    @property
    def is_3way(self) -> bool:
        """Check if this is a 3-way match (rugby, soccer)."""
        return self.fair_prob_draw is not None


class OddsService:
    """
    Service that reads odds from Supabase (populated by local scrapers).
    
    In the hybrid architecture:
    - Local scrapers run on your machine (Bulgaria) and push to Supabase
    - This service reads from Supabase on Fly.io
    """
    
    def __init__(self, refresh_interval: float = 15.0, fair_value_log_interval: float = 300.0, live_mode: bool = False):
        """
        Args:
            refresh_interval: Seconds between fetch cycles
            fair_value_log_interval: Seconds between fair value log snapshots (default 5 min)
            live_mode: If True, read from esports_odds_live table (for LiveBot)
        """
        self.refresh_interval = refresh_interval
        self.fair_value_log_interval = fair_value_log_interval
        self.live_mode = live_mode
        self._cache: Dict[str, AggregatedMatch] = {}  # h2h moneyline (esports + rugby)
        self._v2_cache: Dict[str, AggregatedMatch] = {}  # multi-market (the-odds-api)
        self._running = False
        self._last_update: Optional[datetime] = None
        self._last_fair_value_log: Optional[datetime] = None
        self._supabase_client = None
    
    def _get_supabase(self):
        """Lazy load Supabase client."""
        if self._supabase_client is None:
            from src.data.supabase_client import get_supabase_client
            self._supabase_client = get_supabase_client()
        return self._supabase_client
    
    async def start(self):
        """Initialize service."""
        self._running = True
        print("🌐 OddsService started (reading from Supabase)")
    
    async def stop(self):
        """Stop service."""
        self._running = False
        print("🛑 OddsService stopped")
    
    async def run(self):
        """Main loop: read from Supabase, update cache, repeat."""
        await self.start()
        
        while self._running:
            try:
                await self._fetch_from_supabase()
                
                # Also fetch multi-market data from sports_odds_v2
                if not self.live_mode:
                    try:
                        await self._fetch_from_supabase_v2()
                    except Exception as e:
                        pass  # v2 table may not exist yet
                
                self._last_update = datetime.now(timezone.utc)
                
                # Push fair probs to BotState for reactive edge evaluation
                self._push_fair_probs_to_bot_state()
                
                # Periodically log fair values for analysis
                await self._maybe_log_fair_values()
            except Exception as e:
                print(f"❌ OddsService error: {e}")
            
            await asyncio.sleep(self.refresh_interval)
        
        await self.stop()
    
    def _push_fair_probs_to_bot_state(self):
        """
        Push current fair probabilities to BotState for reactive edge evaluation.
        
        This integrates OddsService with the reactive system - when fair probs
        change, BotState will evaluate edges and emit EDGE_LOST events if needed.
        
        CRITICAL: We pass team names along with fair probs so BotState can
        correctly map them even if team order differs.
        
        Pushes from BOTH caches:
        - _cache: esports h2h moneyline (exact match_id)
        - _v2_cache: sports multi-market (spreads, totals) — requires prefix search
        """
        try:
            from src.state.bot_state import get_bot_state
            bot_state = get_bot_state()
            
            updates_pushed = 0
            
            # === 1. Push h2h moneyline from esports cache ===
            for match in self._cache.values():
                if not match.sources:
                    continue
                
                # Only push if we have valid fair probs
                if match.fair_prob1 <= 0 or match.fair_prob2 <= 0:
                    continue
                
                # Convert from percentage to 0-1 scale
                fair1 = match.fair_prob1 / 100.0
                fair2 = match.fair_prob2 / 100.0
                
                # Push match-level fair probs (skips condition_id-suffixed matches
                # via the cross-market guard in update_fair_probs_by_team)
                result = bot_state.update_fair_probs_by_team(
                    match_id=match.match_id, 
                    team1=match.team1,
                    team2=match.team2,
                    fair_prob1=fair1, 
                    fair_prob2=fair2,
                )
                if result is not None:
                    updates_pushed += 1
                
                # === 1b. Push per-order fair values for condition_id-suffixed h2h matches ===
                # update_fair_probs_by_team's cross-market guard skips condition_id-suffixed
                # matches to prevent h2h probs contaminating spread/totals entries.
                # But legitimate h2h orders with condition_ids (Yes/No draw markets,
                # binary sports h2h) need per-order fair values too.
                # We iterate BotState matches and push per-order values for h2h matches
                # that the cross-market guard skipped.
                source_t1 = normalize_team(match.team1)
                source_t2 = normalize_team(match.team2)
                
                for bs_match_id, bs_match in bot_state._matches.items():
                    # Only look at condition_id-suffixed matches (the ones the guard skips)
                    if not bs_match_id or ":" not in bs_match_id:
                        continue
                    parts = bs_match_id.rsplit(":", 1)
                    if len(parts) != 2 or len(parts[1]) < 10 or not parts[1].startswith("0x"):
                        continue
                    
                    # Must be an h2h match (no O/U or spread markers in team names)
                    bs_teams_str = " ".join(
                        (t or "").lower() for t in [bs_match.team1, bs_match.team2]
                    )
                    if "o/u" in bs_teams_str or "spread" in bs_teams_str:
                        continue  # This is a spread/totals match — skip
                    
                    # Check team name match (either order)
                    import re as _re
                    bt1 = _re.sub(r'[:\s]*\bNo\b$', '', bs_match.team1 or "").strip()
                    bt2 = _re.sub(r'[:\s]*\bNo\b$', '', bs_match.team2 or "").strip()
                    bs_t1_norm = normalize_team(bt1) if bt1 else ""
                    bs_t2_norm = normalize_team(bt2) if bt2 else ""
                    
                    # Detect "Will X win?" pattern by examining the match_id structure.
                    # For these markets, make_match_id uses team and team+"No".
                    # After normalization, one side is "teamno" and the other is the
                    # alias-expanded team name (e.g., "psveindhoven" vs "psvno").
                    # Detection: one part ends with "no" AND the other part is the
                    # alias-expanded version of the base (without "no").
                    is_win_market = False
                    win_team_norm = ""
                    mid_parts = bs_match_id.split(":vs:")
                    if len(mid_parts) == 2:
                        mid_t1 = mid_parts[0].rsplit(":", 1)[-1]
                        mid_t2 = mid_parts[1].split(":")[0]  # strip condition_id
                        for no_side, other_side in [(mid_t1, mid_t2), (mid_t2, mid_t1)]:
                            if no_side.endswith("no") and len(no_side) > 2:
                                base = no_side[:-2]  # strip "no"
                                # The other side should be the alias-expanded base
                                base_expanded = normalize_team(base)
                                if (base_expanded == other_side or
                                    base_expanded in other_side or
                                    other_side in base_expanded or
                                    base == other_side):
                                    is_win_market = True
                                    win_team_norm = normalize_team(other_side)
                                    break
                    
                    if is_win_market:
                        teams_match = (
                            source_t1 == win_team_norm or source_t2 == win_team_norm or
                            win_team_norm in source_t1 or source_t1 in win_team_norm or
                            win_team_norm in source_t2 or source_t2 in win_team_norm
                        )
                    else:
                        # Require both teams to match (either order)
                        teams_match = (
                            (source_t1 == bs_t1_norm and source_t2 == bs_t2_norm) or
                            (source_t1 == bs_t2_norm and source_t2 == bs_t1_norm)
                        )
                        # Also try containment for nicknames (e.g., "Sharks" in "San Jose Sharks")
                        if not teams_match and bs_t1_norm and bs_t2_norm:
                            teams_match = (
                                (source_t1 in bs_t1_norm or bs_t1_norm in source_t1) and
                                (source_t2 in bs_t2_norm or bs_t2_norm in source_t2)
                            ) or (
                                (source_t1 in bs_t2_norm or bs_t2_norm in source_t1) and
                                (source_t2 in bs_t1_norm or bs_t1_norm in source_t2)
                            )
                    
                    if not teams_match:
                        continue
                    
                    # Align fair probs to BotState team order
                    if source_t1 == bs_t1_norm or (source_t1 in bs_t1_norm or bs_t1_norm in source_t1):
                        aligned1, aligned2 = fair1, fair2
                    else:
                        aligned1, aligned2 = fair2, fair1
                    
                    # Handle Yes/No binary markets (3-way with draw)
                    if (bs_match.team1 or "").endswith(" No") or (bs_match.team2 or "").endswith(" No"):
                        # For "Will X win?" markets: Yes = team_prob, No = 1 - team_prob
                        yes_team = bs_match.team1 if not (bs_match.team1 or "").endswith(" No") else bs_match.team2
                        yes_clean = _re.sub(r'[:\s]*(?:O/U\s*[\d.]+|Spread\s*[-+]?[\d.]+)$', '', yes_team).strip()
                        yes_norm = normalize_team(yes_clean)
                        
                        yes_prob = None
                        if yes_norm == source_t1 or yes_norm in source_t1 or source_t1 in yes_norm:
                            yes_prob = fair1
                        elif yes_norm == source_t2 or yes_norm in source_t2 or source_t2 in yes_norm:
                            yes_prob = fair2
                        
                        if yes_prob is not None:
                            no_prob = 1.0 - yes_prob
                            if (bs_match.team1 or "").endswith(" No"):
                                aligned1, aligned2 = no_prob, yes_prob
                            else:
                                aligned1, aligned2 = yes_prob, no_prob
                    
                    # Push per-order fair values
                    for order in [bs_match.order1, bs_match.order2]:
                        if not order or not order.is_open:
                            continue
                        if bs_match.is_order_hedge(order):
                            continue
                        order_fair = None
                        if order.token_id == bs_match.token1:
                            order_fair = aligned1
                        elif order.token_id == bs_match.token2:
                            order_fair = aligned2
                        # Fallback: use order slot position
                        if order_fair is None:
                            if order is bs_match.order1:
                                order_fair = aligned1
                            elif order is bs_match.order2:
                                order_fair = aligned2
                        if order_fair is not None:
                            bot_state.update_order_fair_value(bs_match_id, order, order_fair)
            
            # === 2. Push v2 multi-market (spreads, totals) ===
            # Non-esports BotState match_ids include condition_id suffix:
            #   "football:fcbarcelona:vs:sevilla:0xabc123def456"
            # So we search by prefix and match by order team names.
            for v2_match in self._v2_cache.values():
                if not v2_match.sources:
                    continue
                if v2_match.fair_prob1 <= 0 or v2_match.fair_prob2 <= 0:
                    continue
                
                base_match_id = v2_match.match_id  # e.g., "football:fcbarcelona:vs:sevilla"
                fair1 = v2_match.fair_prob1 / 100.0
                fair2 = v2_match.fair_prob2 / 100.0
                
                # Find ALL BotState matches that correspond to this v2 match.
                # BotState match_ids can have O/U or spread info embedded in team names:
                #   "football:afcbournemouth:o/u2.5:vs:manchesterunitedfc:0xabc123"
                # while v2 base_match_id is "football:afcbournemouth:vs:manchesterunitedfc".
                # Simple startswith fails, so we extract game prefix and teams.
                v2_parts = base_match_id.split(":vs:")
                if len(v2_parts) != 2:
                    continue
                v2_game_and_team1 = v2_parts[0]  # "football:afcbournemouth"
                v2_game = v2_game_and_team1.split(":")[0]  # "football"
                v2_game_norm = normalize_game(v2_game)  # Canonical: "basketball", "hockey", etc.
                v2_team1 = ":".join(v2_game_and_team1.split(":")[1:])  # "afcbournemouth"
                v2_team2 = v2_parts[1]  # "manchesterunitedfc"
                
                for bs_match_id, bs_match in bot_state._matches.items():
                    # Quick filter: must have same normalized game prefix
                    bs_game = bs_match_id.split(":")[0] if ":" in bs_match_id else ""
                    bs_game_norm = normalize_game(bs_game) if bs_game else ""
                    if bs_game_norm != v2_game_norm:
                        continue
                    
                    # Use normalize_team() on BOTH sides for proper matching
                    # This leverages the existing alias mapping (e.g., "76ers" → "philadelphia76ers")
                    # and handles city-prefix differences ("Sharks" vs "San Jose Sharks")
                    v2_t1_norm = normalize_team(v2_match.team1)
                    v2_t2_norm = normalize_team(v2_match.team2)
                    
                    # Get team names from BotState match (these may have O/U or Spread suffixes)
                    bs_t1 = bs_match.team1 or ""
                    bs_t2 = bs_match.team2 or ""
                    
                    # Strip O/U, Spread, and "No" suffixes for matching
                    import re
                    bs_t1_clean = re.sub(r'[:\s]*(?:O/U\s*[\d.]+|Spread\s*[-+]?[\d.]+|\bNo\b)$', '', bs_t1).strip()
                    bs_t2_clean = re.sub(r'[:\s]*(?:O/U\s*[\d.]+|Spread\s*[-+]?[\d.]+|\bNo\b)$', '', bs_t2).strip()
                    
                    bs_t1_norm = normalize_team(bs_t1_clean) if bs_t1_clean else ""
                    bs_t2_norm = normalize_team(bs_t2_clean) if bs_t2_clean else ""
                    
                    # Detect "Will X win?" or "Will X end in a draw?" pattern
                    is_win_market = False
                    is_draw_market = False
                    win_team_norm = ""
                    draw_team_norms = []  # Both teams in the draw match
                    mid_parts = bs_match_id.split(":vs:")
                    if len(mid_parts) == 2:
                        mid_t1 = mid_parts[0].rsplit(":", 1)[-1]
                        mid_t2 = mid_parts[1].split(":")[0]  # strip condition_id
                        
                        # Check for "endinadraw?" pattern
                        # e.g., "republicofirelandendinadraw?" vs "willczechia"
                        if "endinadraw" in mid_t1 or "endinadraw" in mid_t2:
                            is_draw_market = True
                            # Extract team names from both sides
                            for side in [mid_t1, mid_t2]:
                                cleaned = side.replace("endinadraw?", "").replace("endinadraw", "")
                                if cleaned.startswith("will"):
                                    cleaned = cleaned[4:]  # strip "will" prefix
                                if cleaned:
                                    draw_team_norms.append(normalize_team(cleaned))
                        else:
                            # Original "Will X win?" detection
                            for no_side, other_side in [(mid_t1, mid_t2), (mid_t2, mid_t1)]:
                                if no_side.endswith("no") and len(no_side) > 2:
                                    base = no_side[:-2]
                                    base_expanded = normalize_team(base)
                                    if (base_expanded == other_side or
                                        base_expanded in other_side or
                                        other_side in base_expanded or
                                        base == other_side):
                                        is_win_market = True
                                        win_team_norm = normalize_team(other_side)
                                        break
                    
                    if is_draw_market:
                        # Draw market: match if any extracted team name from the
                        # "endinadraw?" match_id appears in the v2 teams
                        teams_match = any(
                            (tn == v2_t1_norm or tn in v2_t1_norm or v2_t1_norm in tn or
                             tn == v2_t2_norm or tn in v2_t2_norm or v2_t2_norm in tn)
                            for tn in draw_team_norms
                        ) if draw_team_norms else False
                    elif is_win_market:
                        teams_match = (
                            v2_t1_norm == win_team_norm or v2_t2_norm == win_team_norm or
                            win_team_norm in v2_t1_norm or v2_t1_norm in win_team_norm or
                            win_team_norm in v2_t2_norm or v2_t2_norm in win_team_norm
                        )
                    else:
                        # Standard two-team matching: both v2 teams must match both BS teams
                        teams_match = (
                            (v2_t1_norm == bs_t1_norm and v2_t2_norm == bs_t2_norm) or
                            (v2_t1_norm == bs_t2_norm and v2_t2_norm == bs_t1_norm)
                        )
                        # Containment fallback for nicknames (e.g., "Capitals" in "Washington Capitals")
                        if not teams_match and bs_t1_norm and bs_t2_norm:
                            teams_match = (
                                (v2_t1_norm in bs_t1_norm or bs_t1_norm in v2_t1_norm) and
                                (v2_t2_norm in bs_t2_norm or bs_t2_norm in v2_t2_norm)
                            ) or (
                                (v2_t1_norm in bs_t2_norm or bs_t2_norm in v2_t1_norm) and
                                (v2_t2_norm in bs_t1_norm or bs_t1_norm in v2_t2_norm)
                            )
                    
                    if not teams_match:
                        continue
                    
                    # Check if this BotState match corresponds to this v2 market type
                    # (e.g., don't push totals fair values to h2h orders)
                    if not self._v2_match_corresponds(v2_match, bs_match):
                        continue
                    
                    # Align fair probs to BotState team order.
                    aligned_fair1, aligned_fair2 = self._align_v2_fair_probs(
                        v2_match, bs_match, fair1, fair2
                    )
                    
                    # BINARY ADJUSTMENT for 3-way h2h Yes/No markets.
                    # If this is a 3-way sport (has draw) and BotState has a "No"
                    # team (from "Will X win?" parsing), the raw team probs don't
                    # work for binary markets. We need:
                    #   Yes side fair = team_prob (raw, from v2)
                    #   No side fair = 1 - team_prob (includes draw + opponent)
                    #
                    # CRITICAL: aligned_fair1/2 are in MATCH_ID order (alphabetical),
                    # NOT BotState team order. For Yes/No markets, BotState team1 is
                    # the subject (e.g., "N.Ireland"), which may differ from match_id
                    # team1 (e.g., "Italy"). We MUST look up the Yes team's prob
                    # directly from v2 match, not assume aligned_fair1 = Yes team.
                    if (v2_match.market_type in ("h2h", "h2h_h1")
                            and v2_match.fair_prob_draw is not None
                            and v2_match.fair_prob_draw > 0):
                        bs_t1 = bs_match.team1 or ""
                        bs_t2 = bs_match.team2 or ""
                        
                        if bs_t1.endswith(" No") or bs_t2.endswith(" No"):
                            # Find the Yes team (the one WITHOUT " No")
                            yes_team = bs_t1 if not bs_t1.endswith(" No") else bs_t2
                            yes_clean = re.sub(
                                r'[:\s]*(?:O/U\s*[\d.]+|Spread\s*[-+]?[\d.]+)$', '', yes_team
                            ).strip()
                            yes_norm = normalize_team(yes_clean)
                            
                            # Match the Yes team to v2.team1 or v2.team2
                            yes_prob = None
                            if (yes_norm == v2_t1_norm or yes_norm in v2_t1_norm
                                    or v2_t1_norm in yes_norm):
                                yes_prob = fair1  # v2.team1 is the Yes team
                            elif (yes_norm == v2_t2_norm or yes_norm in v2_t2_norm
                                    or v2_t2_norm in yes_norm):
                                yes_prob = fair2  # v2.team2 is the Yes team
                            
                            if yes_prob is not None:
                                no_prob = 1.0 - yes_prob
                                if bs_t1.endswith(" No"):
                                    aligned_fair1 = no_prob   # team1 = No side
                                    aligned_fair2 = yes_prob  # team2 = Yes side
                                else:
                                    aligned_fair1 = yes_prob  # team1 = Yes side
                                    aligned_fair2 = no_prob   # team2 = No side
                    
                    # DRAW MARKET ADJUSTMENT: "Will X end in a draw?" Yes/No.
                    # Use fair_prob_draw from v2 data as the Yes probability.
                    if (is_draw_market
                            and v2_match.market_type in ("h2h", "h2h_h1")
                            and v2_match.fair_prob_draw is not None
                            and v2_match.fair_prob_draw > 0):
                        draw_prob = v2_match.fair_prob_draw / 100.0
                        no_draw_prob = 1.0 - draw_prob
                        bs_t1 = bs_match.team1 or ""
                        bs_t2 = bs_match.team2 or ""
                        # "No" token → no_draw_prob, everything else → draw_prob
                        if bs_t1 == "No" or bs_t1.endswith(" No"):
                            aligned_fair1 = no_draw_prob
                            aligned_fair2 = draw_prob
                        elif bs_t2 == "No" or bs_t2.endswith(" No"):
                            aligned_fair1 = draw_prob
                            aligned_fair2 = no_draw_prob
                        else:
                            # Both teams are Yes-style (shouldn't happen, but safe)
                            aligned_fair1 = draw_prob
                            aligned_fair2 = no_draw_prob
                    
                    # UPDATE PER-ORDER FAIR VALUES for all market types.
                    # CRITICAL: Condition_id-suffixed matches (all non-esports) can't
                    # use match-level fair_probs in get_fair_value_for_order (safety net
                    # blocks them). Per-order fair_value is the only path that works.
                    for order in [bs_match.order1, bs_match.order2]:
                        if not order or not order.is_open:
                            continue
                        # Determine which aligned fair prob applies to this order.
                        # Use multiple strategies since token1/token2 may not be set
                        # on hydrated MatchState entries.
                        order_fair = None
                        
                        # Strategy 1: token_id match (most reliable when set)
                        if order.token_id == bs_match.token1:
                            order_fair = aligned_fair1
                        elif order.token_id == bs_match.token2:
                            order_fair = aligned_fair2
                        
                        # Strategy 2: order slot position (order1 → fair1, order2 → fair2)
                        if order_fair is None:
                            if order is bs_match.order1:
                                order_fair = aligned_fair1
                            elif order is bs_match.order2:
                                order_fair = aligned_fair2
                        
                        # PER-ORDER "No" TOKEN ADJUSTMENT for 3-way h2h.
                        # Match-level team names (from match_id) never have " No" —
                        # only order.team does (e.g., "PSV No", "NEC No").
                        # For "No" tokens: fair = 1 - team_win_prob
                        # For "Yes" tokens (named by team, e.g., "PSV"): fair = team_win_prob
                        # We need to find the CORRECT team's prob, not just aligned_fair.
                        if (order_fair is not None
                                and v2_match.market_type in ("h2h", "h2h_h1")
                                and v2_match.fair_prob_draw is not None
                                and v2_match.fair_prob_draw > 0):
                            order_team = (order.team or "").strip()
                            order_is_no = order_team.endswith(" No") or order_team == "No"
                            
                            if order_is_no or order_team:
                                # Find which v2 team this order refers to
                                if order_is_no:
                                    base_team = order_team.removesuffix(" No").strip()
                                else:
                                    base_team = order_team
                                base_norm = normalize_team(base_team)
                                
                                team_prob = None
                                if (base_norm == v2_t1_norm or base_norm in v2_t1_norm
                                        or v2_t1_norm in base_norm):
                                    team_prob = fair1  # This order's team = v2 team1
                                elif (base_norm == v2_t2_norm or base_norm in v2_t2_norm
                                        or v2_t2_norm in base_norm):
                                    team_prob = fair2  # This order's team = v2 team2
                                
                                if team_prob is not None:
                                    if order_is_no:
                                        order_fair = 1.0 - team_prob  # No = draw + opponent
                                    else:
                                        order_fair = team_prob  # Yes = team win prob
                        
                        if order_fair is not None:
                            bot_state.update_order_fair_value(bs_match_id, order, order_fair)
                    
                    # Also set match-level fair_prob for hedge calculations
                    result = bot_state.update_fair_probs(
                        match_id=bs_match_id,
                        fair_prob1=aligned_fair1,
                        fair_prob2=aligned_fair2,
                    )
                    if result is not None:
                        updates_pushed += 1
                
        except Exception as e:
            print(f"⚠️ Error pushing fair probs to BotState: {e}")
            import traceback
            traceback.print_exc()
    
    def find_v2_fair_value(
        self, team1: str, team2: str, outcome: str,
        market_type: str = "", line: float = 0.0,
    ) -> Optional[float]:
        """
        Search the v2 cache directly for a fair value by team names.
        
        FALLBACK for positions where _push_fair_probs_to_bot_state couldn't
        push (e.g., non-standard single-team match_ids).
        Uses normalize_team() for proper alias-aware matching.
        
        Returns:
            Fair value (0-1 scale) or None if not found
        """
        if not team1:
            return None
            
        t1_norm = normalize_team(team1)
        t2_norm = normalize_team(team2) if team2 else ""
        outcome_norm = normalize_team(outcome) if outcome not in ("Over", "Under", "Yes", "No") else ""
        
        for v2_match in self._v2_cache.values():
            if v2_match.fair_prob1 <= 0 or v2_match.fair_prob2 <= 0:
                continue
            
            v2_t1 = normalize_team(v2_match.team1)
            v2_t2 = normalize_team(v2_match.team2)
            
            # Check team match (either order)
            # Uses both exact normalized match AND bidirectional containment
            # for ambiguous short names like "sharks" (could be SJ Sharks or Durban Sharks)
            def _teams_match(a: str, b: str) -> bool:
                if not a or not b:
                    return False
                return a == b or a in b or b in a
            
            if t2_norm:
                if not ((_teams_match(t1_norm, v2_t1) and _teams_match(t2_norm, v2_t2)) or
                        (_teams_match(t1_norm, v2_t2) and _teams_match(t2_norm, v2_t1))):
                    continue
            else:
                # Single-team: check if t1 matches either side
                if not (_teams_match(t1_norm, v2_t1) or _teams_match(t1_norm, v2_t2)):
                    continue
            
            # Filter by market type if specified
            if market_type:
                if v2_match.market_type != market_type:
                    if not (market_type == "h2h" and v2_match.market_type in ("h2h_h1",)):
                        continue
            
            # Filter by line if specified
            v2_line = getattr(v2_match, 'line', 0) or 0
            if line > 0 and abs(v2_line - line) > 0.01:
                continue
            
            # Determine which fair prob applies to our outcome
            fair1 = v2_match.fair_prob1 / 100.0
            fair2 = v2_match.fair_prob2 / 100.0
            
            # Determine if t1 aligns with v2_t1 or v2_t2
            t1_is_v2_t1 = _teams_match(t1_norm, v2_t1)
            
            if outcome in ("Over", "Yes"):
                if v2_match.market_type in ("totals", "totals_1h"):
                    return fair1  # v2 convention: prob1 = Over
                else:
                    return fair1 if t1_is_v2_t1 else fair2
            elif outcome in ("Under", "No"):
                if v2_match.market_type in ("totals", "totals_1h"):
                    return fair2  # v2 convention: prob2 = Under
                elif outcome == "No":
                    return (1.0 - fair1) if t1_is_v2_t1 else (1.0 - fair2)
                else:
                    return fair2 if t1_is_v2_t1 else fair1
            else:
                # Team name outcome: match by normalized name
                if _teams_match(outcome_norm, v2_t1):
                    return fair1
                elif _teams_match(outcome_norm, v2_t2):
                    return fair2
                elif t1_is_v2_t1:
                    return fair1
                else:
                    return fair2
        
        return None
    
    def _v2_match_corresponds(self, v2_match: AggregatedMatch, bs_match) -> bool:
        """Check if a v2 multi-market match corresponds to a BotState MatchState.
        
        Matches by checking if the BotState match's order/position team names
        relate to this v2 market type + line.
        
        Examples:
        - v2: totals line=3.5 → BotState with orders on "Over"/"Under" or 
          team names containing "O/U 3.5"
        - v2: spreads line=1.5 → BotState with team names containing "(-1.5)"
        """
        # Get team names from BotState match orders, positions, or match teams
        bs_teams = set()
        for team_attr in [bs_match.team1, bs_match.team2]:
            if team_attr:
                bs_teams.add(team_attr.lower())
        if bs_match.order1:
            bs_teams.add(bs_match.order1.team.lower() if bs_match.order1.team else "")
        if bs_match.order2:
            bs_teams.add(bs_match.order2.team.lower() if bs_match.order2.team else "")
        
        bs_teams_str = " ".join(bs_teams)
        
        if v2_match.market_type in ("totals", "totals_1h"):
            # Totals: look for "O/U X.X" or "Over"/"Under" in team names
            line_str = f"o/u {v2_match.line:g}"
            has_ou = line_str in bs_teams_str or (
                "over" in bs_teams_str and "under" in bs_teams_str
                and f"{v2_match.line:g}" in bs_teams_str
            )
            # For 1H, also check for "1h" prefix in team names
            if v2_match.market_type == "totals_1h":
                return has_ou and "1h" in bs_teams_str
            return has_ou
        elif v2_match.market_type in ("spreads", "spreads_1h"):
            # Spreads: look for "(-X.X)" or "(+X.X)" in team names, OR "Spread X.X"
            line_str = f"({v2_match.line:g})"
            neg_line_str = f"(-{v2_match.line:g})"
            pos_line_str = f"(+{v2_match.line:g})"
            spread_keyword = f"spread {v2_match.line:g}"
            has_spread = (neg_line_str in bs_teams_str or pos_line_str in bs_teams_str 
                    or line_str in bs_teams_str or spread_keyword in bs_teams_str)
            # For 1H, also check for "1h" prefix in team names
            if v2_match.market_type == "spreads_1h":
                return has_spread and "1h" in bs_teams_str
            return has_spread
        elif v2_match.market_type in ("h2h", "h2h_h1"):
            # h2h: match if no special markers in team names
            # Only match if bs_match teams DON'T contain O/U or spread markers
            is_plain = ("o/u" not in bs_teams_str and 
                    "(-" not in bs_teams_str and "(+" not in bs_teams_str
                    and "spread" not in bs_teams_str)
            # For 1H moneyline, check for "1h" in team names
            if v2_match.market_type == "h2h_h1":
                return is_plain and "1h" in bs_teams_str
            return is_plain
        
        return False
    
    def _align_v2_fair_probs(
        self, v2_match: AggregatedMatch, bs_match, fair1: float, fair2: float
    ) -> tuple:
        """Align v2 fair probs to BotState team order.
        
        V2 fair_prob1 corresponds to v2_match.team1.
        BotState fair_prob1 corresponds to bs_match.team1.
        
        We need to figure out if v2.team1 maps to bs.team1 or bs.team2,
        using the match_id which has normalized+sorted team names.
        
        For totals: fair_prob1 = Over (v2 outcome1_name), fair_prob2 = Under.
        The BotState team that contains "O/U" gets the Over prob.
        
        Returns (aligned_fair1, aligned_fair2) in BotState team order.
        """
        if v2_match.market_type == "totals":
            # For totals, v2 fair_prob1 = Over, fair_prob2 = Under
            # Check which bs_match side has "Over" vs "Under"
            t1_lower = (bs_match.team1 or "").lower()
            t2_lower = (bs_match.team2 or "").lower()
            
            # If team1 contains "O/U" it gets the Over prob (fair_prob1)
            # Standard convention: team1 = Over side, team2 = Under side
            if "under" in t1_lower or (t2_lower and "over" in t2_lower and "over" not in t1_lower):
                # BotState team1 is Under, team2 is Over → swap
                return fair2, fair1
            # Default: BotState team1 is Over, team2 is Under → no swap
            return fair1, fair2
        
        # For h2h and spreads: compare normalized team names via match_id
        # match_id format: "game:teamA:vs:teamB" where teamA < teamB alphabetically
        # Both v2 and BotState share the same base match_id, so team order 
        # in the match_id is identical. 
        # v2_match stores teams in the order from the-odds-api (may differ from match_id).
        # BotState stores teams in match_id order (sorted).
        # 
        # Strategy: normalize v2.team1 and check if it matches the first
        # or second team in the match_id to determine order.
        v2_t1_norm = normalize_team(v2_match.team1)
        
        # Extract teams from match_id: "football:teamA:vs:teamB"
        parts = v2_match.match_id.split(":vs:")
        if len(parts) == 2:
            mid_team1 = parts[0].split(":")[-1]  # After game prefix
            # If v2.team1 normalizes to the first team in match_id,
            # then v2 order matches match_id order (= BotState order) → no swap
            if v2_t1_norm == mid_team1:
                return fair1, fair2
            else:
                return fair2, fair1
        
        # Fallback: no swap
        return fair1, fair2
    
    async def _maybe_log_fair_values(self):
        """Log fair values periodically for historical analysis."""
        now = datetime.now(timezone.utc)
        
        # Check if enough time has passed since last log
        if self._last_fair_value_log:
            elapsed = (now - self._last_fair_value_log).total_seconds()
            if elapsed < self.fair_value_log_interval:
                return
        
        # Convert cache to list of dicts for recorder
        matches_to_log = []
        for match in self._cache.values():
            if not match.sources:
                continue
            # Skip live matches - only log pre-match fair values for analysis
            if match.is_live:
                continue
            
            sources_list = []
            for source_name, odds in match.sources.items():
                sources_list.append({
                    "source": source_name,
                    "fair_prob1": odds.fair_prob1,
                    "fair_prob2": odds.fair_prob2,
                    "odds1": odds.odds1,
                    "odds2": odds.odds2,
                })
            
            matches_to_log.append({
                "match_id": match.match_id,
                "team1": match.team1,
                "team2": match.team2,
                "game": match.game,
                "fair_prob1": match.fair_prob1,
                "fair_prob2": match.fair_prob2,
                "sources": sources_list,
            })
        
        if matches_to_log:
            try:
                recorder = get_recorder()
                count = await recorder.log_fair_values(matches_to_log)
                if count > 0:
                    self._last_fair_value_log = now
                    # Quiet log - only show occasionally
                    # print(f"📊 Logged {count} fair values")
            except Exception as e:
                print(f"⚠️ Failed to log fair values: {e}")
    
    async def _fetch_from_supabase(self, max_retries: int = 3):
        """Fetch latest odds from Supabase and update cache with retry logic."""
        last_error = None
        
        for attempt in range(max_retries):
            try:
                client = self._get_supabase()
                
                # CRITICAL: Use live table for LiveBot, pre-match for SportsBot
                if self.live_mode:
                    # Read from esports_odds_live (populated by run_live_scrapers.py)
                    records = client.get_live_odds(max_age_seconds=30)  # 30s freshness - strict for live trading
                    # No sports odds in live mode (rugby not live yet)
                else:
                    # Read from esports_odds (pre-match)
                    records = client.get_latest_odds(limit=2000)
                    
                    # Also get sports odds (rugby, soccer, etc.)
                    # These have 3-way odds - we use fair_prob1 for initial entry,
                    # but preserve fair_prob_draw for draw hedging later
                    try:
                        sports_records = client.get_latest_sports_odds(limit=2000)
                        # Convert sports records to esports format using no-draw proxy probs
                        for sr in sports_records:
                            # Map sport to game field - for rugby, map tournament to specific game type
                            sport = sr.get('sport', '')
                            tournament = sr.get('tournament', '').lower()
                            
                            if sport == 'rugby':
                                # Map tournament names to Polymarket game types
                                if 'premiership' in tournament or 'gallagher' in tournament:
                                    sr['game'] = 'rugby_premiership'
                                elif 'top 14' in tournament:
                                    sr['game'] = 'rugby_top14'
                                elif 'united rugby' in tournament or 'urc' in tournament:
                                    sr['game'] = 'rugby_urc'
                                elif 'six nations' in tournament:
                                    sr['game'] = 'rugby'  # Generic for now
                                else:
                                    sr['game'] = 'rugby'  # Fallback
                            else:
                                sr['game'] = sport
                        records.extend(sports_records)
                    except Exception as e:
                        # Sports table might not exist yet - continue with esports only
                        pass
                
                # Success - process records (rest of function continues below)
                break
                
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = 2 ** attempt  # 1s, 2s, 4s
                    print(f"⚠️ Supabase fetch failed (attempt {attempt + 1}/{max_retries}), retrying in {delay}s...")
                    await asyncio.sleep(delay)
                    # Force new connection on retry
                    self._supabase_client = None
                else:
                    print(f"❌ Supabase fetch error: {e}")
                    raise
        else:
            # All retries failed
            if last_error:
                print(f"❌ Supabase fetch error after {max_retries} retries: {last_error}")
            return
        
        # Continue processing records (moved from try block)
        try:
            
            # Convert to MatchOdds and aggregate
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(minutes=30)
            
            matches_by_key = {}
            
            for r in records:
                # Parse timestamp
                scraped_at_str = r.get("scraped_at", "")
                try:
                    if scraped_at_str:
                        # Handle ISO format from Supabase
                        scraped_at = datetime.fromisoformat(scraped_at_str.replace("Z", "+00:00"))
                    else:
                        scraped_at = now
                except:
                    scraped_at = now
                
                # Skip stale data
                if scraped_at < cutoff:
                    continue
                
                # Create MatchOdds
                team1 = r.get("team1", "")
                team2 = r.get("team2", "")
                game_raw = r.get("game", "").lower()
                
                # Normalize game using shared function (handles hyphens, all games)
                game = normalize_game(game_raw)
                
                # Calculate fair probability on the fly if missing (e.g. source_l sometimes has 0)
                odds1 = float(r.get("odds1", 0) or 0)
                odds2 = float(r.get("odds2", 0) or 0)
                fair_prob1 = float(r.get("fair_prob1", 0) or 0)
                fair_prob2 = float(r.get("fair_prob2", 0) or 0)
                
                if (fair_prob1 == 0 or fair_prob2 == 0) and odds1 > 1 and odds2 > 1:
                    try:
                        # Recompute via proportional vig removal — some scrapers
                        # (source_l especially) occasionally write 0 fair_prob
                        # despite valid raw odds.
                        from src.core.vig_removal import proportional_probabilities
                        fair1, fair2 = proportional_probabilities(odds1, odds2)
                        fair_prob1 = fair1 * 100
                        fair_prob2 = fair2 * 100
                    except:
                        pass
                
                # Skip matches with invalid/missing probabilities
                if fair_prob1 <= 0 or fair_prob2 <= 0:
                    continue
                
                # Get fair_prob_draw for 3-way sports (rugby, soccer)
                # This comes from sports_odds table for hedging draw orders
                fair_prob_draw = r.get('fair_prob_draw')
                if fair_prob_draw is not None:
                    fair_prob_draw = float(fair_prob_draw)
                
                match_odds = MatchOdds(
                    match_id=r.get("match_id", f"{team1}_{team2}"),
                    team1=team1,
                    team2=team2,
                    odds1=odds1,
                    odds2=odds2,
                    fair_prob1=fair_prob1,
                    fair_prob2=fair_prob2,
                    source=r.get("source", "unknown"),
                    game=game,
                    is_live=r.get("is_live", False),
                    fair_prob_draw=fair_prob_draw,  # For 3-way sports
                    timestamp=scraped_at,
                )
                
                # Aggregate by match
                match_key = self._make_match_id(team1, team2, game)
                
                # IMPORTANT: Ensure team1/team2 are in the SAME sorted order as match_key
                # This is critical for consistent fair_prob alignment with BotState
                t1_norm = self._normalize_team(team1)
                t2_norm = self._normalize_team(team2)
                if t1_norm > t2_norm:
                    # Teams need to be swapped to match sorted match_key
                    sorted_team1, sorted_team2 = team2, team1
                    sorted_fair1, sorted_fair2 = fair_prob2, fair_prob1
                else:
                    sorted_team1, sorted_team2 = team1, team2
                    sorted_fair1, sorted_fair2 = fair_prob1, fair_prob2
                
                if match_key not in matches_by_key:
                    matches_by_key[match_key] = AggregatedMatch(
                        match_id=match_key,
                        team1=sorted_team1,
                        team2=sorted_team2,
                        game=game,
                        is_live=match_odds.is_live,
                        timestamp=scraped_at,
                    )
                    # Update match_odds with sorted values for this first entry
                    match_odds = MatchOdds(
                        match_id=match_odds.match_id,
                        team1=sorted_team1,
                        team2=sorted_team2,
                        odds1=match_odds.odds1 if t1_norm <= t2_norm else match_odds.odds2,
                        odds2=match_odds.odds2 if t1_norm <= t2_norm else match_odds.odds1,
                        fair_prob1=sorted_fair1,
                        fair_prob2=sorted_fair2,
                        source=match_odds.source,
                        game=match_odds.game,
                        is_live=match_odds.is_live,
                        timestamp=match_odds.timestamp,
                    )
                
                agg = matches_by_key[match_key]
                
                # CRITICAL: Align team order with aggregated match
                # If this source has teams in opposite order, swap the probabilities
                agg_t1_norm = self._normalize_team(agg.team1)
                src_t1_norm = self._normalize_team(match_odds.team1)
                
                if agg_t1_norm != src_t1_norm:
                    # Teams are swapped - need to swap probabilities
                    match_odds = MatchOdds(
                        match_id=match_odds.match_id,
                        team1=match_odds.team2,  # Swap
                        team2=match_odds.team1,  # Swap
                        odds1=match_odds.odds2,  # Swap
                        odds2=match_odds.odds1,  # Swap
                        fair_prob1=match_odds.fair_prob2,  # Swap
                        fair_prob2=match_odds.fair_prob1,  # Swap
                        source=match_odds.source,
                        game=match_odds.game,
                        is_live=match_odds.is_live,
                        timestamp=match_odds.timestamp,
                    )
                
                agg.sources[match_odds.source] = match_odds
                agg.is_live = agg.is_live or match_odds.is_live
                if scraped_at > agg.timestamp:
                    agg.timestamp = scraped_at
            
            # Update cache
            self._cache = matches_by_key
            
        except Exception as e:
            print(f"❌ Supabase fetch error: {e}")
            import traceback
            traceback.print_exc()
    
    async def _fetch_from_supabase_v2(self):
        """Fetch multi-market odds from sports_odds_v2 and update v2 cache.
        
        Fetches h2h, spreads, and totals from the-odds-api data.
        """
        try:
            from src.services.odds_api_service import SPORT_KEY_TO_GAME
            
            client = self._get_supabase()
            records = client.get_latest_sports_odds_v2(
                market_types=["h2h", "spreads", "totals", "h2h_h1", "spreads_1h", "totals_1h", "map_winner"],
            )
            
            if not records:
                return
            
            now = datetime.now(timezone.utc)
            
            matches_by_key = {}
            
            for r in records:
                # Parse timestamp
                scraped_at_str = r.get("scraped_at", "")
                try:
                    if scraped_at_str:
                        scraped_at = datetime.fromisoformat(scraped_at_str.replace("Z", "+00:00"))
                    else:
                        scraped_at = now
                except Exception:
                    scraped_at = now
                
                # Map sport key to game
                sport_key = r.get("sport", "")
                game = SPORT_KEY_TO_GAME.get(sport_key, sport_key)
                
                team1 = r.get("team1", "")
                team2 = r.get("team2", "")
                market_type = r.get("market_type", "h2h")
                line = float(r.get("line", 0) or 0)
                
                fair_prob1 = float(r.get("fair_prob1", 0) or 0)
                fair_prob2 = float(r.get("fair_prob2", 0) or 0)
                
                if fair_prob1 <= 0 or fair_prob2 <= 0:
                    continue
                
                # Read fair_prob_draw for 3-way sports (football, hockey, rugby)
                fpd_raw = r.get("fair_prob_draw")
                fair_prob_draw = float(fpd_raw) if fpd_raw is not None else None
                
                odds1 = float(r.get("odds1", 0) or 0)
                odds2 = float(r.get("odds2", 0) or 0)
                
                match_id = make_match_id(team1, team2, game)
                
                # v2 aggregation key: match + market type + line + sport
                # sport included so EPL vs FA Cup stay separate
                agg_key = f"{match_id}:{market_type}:{line}:{sport_key}"
                
                match_odds = MatchOdds(
                    match_id=match_id,
                    team1=team1,
                    team2=team2,
                    odds1=odds1,
                    odds2=odds2,
                    fair_prob1=fair_prob1,
                    fair_prob2=fair_prob2,
                    fair_prob_draw=fair_prob_draw,
                    source=r.get("source", "the-odds-api"),
                    game=game,
                    is_live=r.get("is_live", False),
                    market_type=market_type,
                    line=line,
                    timestamp=scraped_at,
                )
                
                if agg_key not in matches_by_key:
                    # Read team1_spread_point from DB (nullable)
                    t1sp_raw = r.get("team1_spread_point")
                    t1sp = float(t1sp_raw) if t1sp_raw is not None else None
                    
                    matches_by_key[agg_key] = AggregatedMatch(
                        match_id=match_id,
                        team1=team1,
                        team2=team2,
                        game=game,
                        is_live=r.get("is_live", False),
                        market_type=market_type,
                        line=line,
                        outcome1_name=r.get("outcome1_name", ""),
                        outcome2_name=r.get("outcome2_name", ""),
                        team1_spread_point=t1sp,
                        timestamp=scraped_at,
                    )
                
                agg = matches_by_key[agg_key]
                agg.sources[match_odds.source] = match_odds
                if scraped_at > agg.timestamp:
                    agg.timestamp = scraped_at
            
            self._v2_cache = matches_by_key
            
        except Exception as e:
            print(f"⚠️ sports_odds_v2 fetch error: {e}")
    
    def _normalize_team(self, name: str) -> str:
        """Normalize team name for better matching. Uses shared module."""
        return normalize_team(name)

    def _make_match_id(self, team1: str, team2: str, game: str) -> str:
        """Create canonical match ID from teams. Uses shared module."""
        return make_match_id(team1, team2, game)
    
    def get_matches(
        self, 
        game: Optional[str] = None,
        min_sources: int = 1,
        fresh_only: bool = True,
        include_v2: bool = False,
    ) -> List[AggregatedMatch]:
        """Get aggregated matches from cache.
        
        Args:
            game: Filter by game type
            min_sources: Minimum number of sources
            fresh_only: Only return fresh data
            include_v2: Also include v2 cache entries (sports spreads/totals/h2h)
        """
        matches = []
        
        for match in self._cache.values():
            if game and match.game != game:
                continue
            if match.num_sources < min_sources:
                continue
            if fresh_only and not match.is_fresh():
                continue
            matches.append(match)
        
        if include_v2:
            for match in self._v2_cache.values():
                if game and match.game != game:
                    continue
                if match.num_sources < min_sources:
                    continue
                if fresh_only and not match.is_fresh():
                    continue
                matches.append(match)
        
        return matches
    
    def get_multi_market_matches(
        self,
        market_types: Optional[List[str]] = None,
        fresh_only: bool = True,
    ) -> List[AggregatedMatch]:
        """Get multi-market matches from v2 cache (spreads, totals, etc.).
        
        Args:
            market_types: Filter to specific types, e.g. ["spreads", "totals"].
                         None = all non-h2h types.
            fresh_only: Only return fresh data.
        """
        matches = []
        for match in self._v2_cache.values():
            if market_types and match.market_type not in market_types:
                continue
            if fresh_only and not match.is_fresh():
                continue
            matches.append(match)
        return matches
    
    def get_match(self, team1: str, team2: str, game: str) -> Optional[AggregatedMatch]:
        """Get a specific match by teams."""
        match_id = self._make_match_id(team1, team2, game)
        return self._cache.get(match_id)
    
    def get_match_by_single_team(self, team: str, game: str) -> Optional[AggregatedMatch]:
        """
        Find a match that contains a specific team.
        
        Used for rugby "Will X win?" markets where we only know one team.
        Returns the first matching match found.
        """
        if not team:
            return None
        
        team_norm = self._normalize_team(team)
        game_norm = normalize_game(game)
        
        for match in self._cache.values():
            # Check game matches (allow partial match for rugby sub-types)
            if game_norm == 'rugby':
                if 'rugby' not in match.game:
                    continue
            elif match.game != game_norm:
                continue
            
            # Check if team is in this match
            t1_norm = self._normalize_team(match.team1)
            t2_norm = self._normalize_team(match.team2)
            
            if team_norm == t1_norm or team_norm == t2_norm:
                return match
        
        return None
    
    def print_status(self):
        """Print current cache status."""
        print(f"\n📊 OddsService Status")
        print(f"   Last update: {self._last_update}")
        print(f"   Cached matches: {len(self._cache)}")
        
        by_game = {}
        for m in self._cache.values():
            by_game[m.game] = by_game.get(m.game, 0) + 1
        
        for game, count in by_game.items():
            print(f"   {game}: {count} matches")
    
    def get_live_match_ids(self) -> set[str]:
        """
        Get match_ids of all matches currently in esports_odds_live table.
        
        This is the ground truth for live match detection - if a match is in
        esports_odds_live, it's considered live regardless of Polymarket's is_live flag.
        
        Returns:
            Set of match_id strings
        """
        try:
            client = self._get_supabase()
            match_ids = client.get_all_live_match_ids()
            return set(match_ids)
        except Exception as e:
            print(f"⚠️ Error fetching live match IDs: {e}")
            return set()


async def demo():
    """Demo the odds service."""
    service = OddsService(refresh_interval=30)
    
    print("Starting OddsService demo...")
    
    try:
        await service.start()
        await service._fetch_from_supabase()
        service.print_status()
        
        matches = service.get_matches(min_sources=1)
        print(f"\n📋 Sample matches ({len(matches)} total):")
        for m in matches[:5]:
            print(f"   {m.team1} vs {m.team2} ({m.game})")
            print(f"      Fair: {m.fair_prob1:.1f}% / {m.fair_prob2:.1f}%")
            print(f"      Sources: {m.num_sources}")
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(demo())
