"""
Team name matching utilities for aligning bookmaker and Polymarket team names.

Uses the canonical normalize_team from match_id module - no separate implementation.
"""
from datetime import datetime, timezone
from typing import Optional, Tuple

from src.services.odds_service import AggregatedMatch
from src.core.match_id import normalize_team


# Maximum age for odds data before it's considered stale (10 minutes)
# Odds older than this will be rejected to prevent trading on outdated information
MAX_ODDS_AGE_SECONDS = 600

# Track matches we've already warned about staleness (deduplicate log spam)
_stale_odds_warned: set = set()

# Re-export normalize_team for backward compatibility
normalize_team_name = normalize_team


def teams_match_exactly(name1: str, name2: str) -> bool:
    """Check if two team names match after normalization."""
    return normalize_team(name1) == normalize_team(name2)


def find_matching_odds(
    market_team1: str,
    market_team2: str,
    odds_matches: list,  # List of AggregatedMatch
) -> Tuple[Optional[AggregatedMatch], bool]:
    """
    Find the bookmaker odds that exactly match the market teams.
    
    Returns:
        (matching_odds, is_swapped) where is_swapped indicates if team order is reversed
        (None, False) if no match found
    """
    m1_norm = normalize_team(market_team1)
    m2_norm = normalize_team(market_team2)
    
    for odds_match in odds_matches:
        # Skip matches with invalid fair values
        if odds_match.fair_prob1 <= 0 or odds_match.fair_prob2 <= 0:
            continue
        
        b1_norm = normalize_team(odds_match.team1)
        b2_norm = normalize_team(odds_match.team2)
        
        # Direct order: market_team1 = book.team1, market_team2 = book.team2
        if m1_norm == b1_norm and m2_norm == b2_norm:
            return (odds_match, False)
        
        # Swapped order: market_team1 = book.team2, market_team2 = book.team1
        if m1_norm == b2_norm and m2_norm == b1_norm:
            return (odds_match, True)
    
    return (None, False)


def get_fair_value_for_team(
    team_name: str,
    odds_match: AggregatedMatch,
) -> Optional[float]:
    """
    Get the fair value for a specific team from the odds match.
    
    Handles:
    - "No" tokens (e.g., "Pau No") by inverting the YES fair value
    - Totals outcomes ("Over", "Under", "1H Over", "1H Under") by using
      v2 convention: fair_prob1 = Over, fair_prob2 = Under
    
    Returns fair value as decimal (0.0-1.0) or None if team not found.
    """
    # Handle totals outcomes directly (Over/Under don't match bookmaker team names)
    market_type = getattr(odds_match, 'market_type', 'h2h')
    if market_type in ("totals", "totals_1h"):
        if team_name in ("Over", "1H Over"):
            return odds_match.fair_prob1 / 100  # v2 convention: prob1 = Over
        elif team_name in ("Under", "1H Under"):
            return odds_match.fair_prob2 / 100  # v2 convention: prob2 = Under
    
    # Check if this is a "No" token
    is_no_token = team_name.endswith(" No")
    base_team_name = team_name[:-3] if is_no_token else team_name
    
    team_norm = normalize_team(base_team_name)
    t1_norm = normalize_team(odds_match.team1)
    t2_norm = normalize_team(odds_match.team2)
    
    yes_fair = None
    if team_norm == t1_norm:
        yes_fair = odds_match.fair_prob1 / 100
    elif team_norm == t2_norm:
        yes_fair = odds_match.fair_prob2 / 100
    
    if yes_fair is None:
        return None
    
    # For No tokens, return inverted fair value
    if is_no_token:
        return 1 - yes_fair
    
    return yes_fair


def get_fair_value_for_match(
    team1: str,
    team2: str,
    our_team: str,
    odds_service,
    game: str = None,
    max_age_seconds: float = None,
) -> tuple[Optional[float], Optional[AggregatedMatch]]:
    """
    Get fair value for a team with game-type filtering.
    
    This is the SAFE function to use - it prevents cross-game mismatches where
    the same team names exist in different games with different fair values.
    
    Args:
        team1: First team name (from Polymarket)
        team2: Second team name (from Polymarket)
        our_team: The team we bet on (to get fair value for)
        odds_service: OddsService instance
        game: Optional game type filter (e.g., "valorant", "lol")
        max_age_seconds: Optional max age override (defaults to MAX_ODDS_AGE_SECONDS)
    
    Returns:
        (fair_value, matching_odds) or (None, None) if not found or stale
    """
    matches = odds_service.get_matches(min_sources=1, fresh_only=False, include_v2=True)
    
    # Filter by game type to prevent cross-game mismatches
    if game:
        game_lower = game.lower()
        matches = [m for m in matches if m.game and m.game.lower() == game_lower]
    
    matching_odds, _ = find_matching_odds(team1, team2, matches)
    
    if not matching_odds:
        return None, None
    
    # CRITICAL: Check staleness before using odds
    # This prevents using 20+ minute old odds that may be completely wrong
    max_age = max_age_seconds if max_age_seconds is not None else MAX_ODDS_AGE_SECONDS
    if hasattr(matching_odds, 'timestamp') and matching_odds.timestamp:
        now = datetime.now(timezone.utc)
        odds_age = (now - matching_odds.timestamp).total_seconds()
        
        if odds_age > max_age:
            # Log warning only once per match to avoid spam
            match_key = f"{team1}:{team2}"
            if match_key not in _stale_odds_warned:
                age_mins = odds_age / 60
                print(f"   ⚠️ STALE ODDS REJECTED: {team1} vs {team2} - odds are {age_mins:.1f}m old (max: {max_age/60:.0f}m)")
                _stale_odds_warned.add(match_key)
            return None, None
    
    fair = get_fair_value_for_team(our_team, matching_odds)
    return fair, matching_odds


def align_teams_with_bids(
    odds_match: AggregatedMatch,
    outcomes: list,
    token_ids: list,
    best_bids: dict,
) -> Optional[list]:
    """
    Align bookmaker team order with Polymarket outcomes.
    
    Uses exact normalized matching - both teams must match exactly.
    
    Returns list of (poly_team, best_bid_price, poly_token, bookmaker_fair, other_fair)
    for each team, or None if can't align.
    """
    fair1 = odds_match.fair_prob1 / 100
    fair2 = odds_match.fair_prob2 / 100
    
    # Skip if fair probabilities are invalid
    if fair1 <= 0 or fair2 <= 0:
        return None
    
    outcome0 = outcomes[0]
    outcome1 = outcomes[1]
    
    # Skip Yes/No/Over/Under markets - can't reliably align without parsing market question
    skip_outcomes = ("yes", "no", "over", "under")
    if outcome0.lower() in skip_outcomes or outcome1.lower() in skip_outcomes:
        return None
    
    # Get best bids
    bid0 = best_bids.get(token_ids[0], {}).get("price", 0)
    bid1 = best_bids.get(token_ids[1], {}).get("price", 0)
    
    # Normalize all team names using the SAME function
    b1_norm = normalize_team(odds_match.team1)
    b2_norm = normalize_team(odds_match.team2)
    o0_norm = normalize_team(outcome0)
    o1_norm = normalize_team(outcome1)
    
    # Check direct order: outcome0 = book.team1, outcome1 = book.team2
    if o0_norm == b1_norm and o1_norm == b2_norm:
        return [
            (outcomes[0], bid0, token_ids[0], fair1, fair2),
            (outcomes[1], bid1, token_ids[1], fair2, fair1),
        ]
    
    # Check swapped order: outcome0 = book.team2, outcome1 = book.team1
    if o0_norm == b2_norm and o1_norm == b1_norm:
        return [
            (outcomes[0], bid0, token_ids[0], fair2, fair1),
            (outcomes[1], bid1, token_ids[1], fair1, fair2),
        ]
    
    # No exact match found
    # Fallback: containment matching for sports (e.g., "sharks" in "sanjosesharks")
    def contains_match(norm_a, norm_b):
        return norm_a in norm_b or norm_b in norm_a
    
    if contains_match(o0_norm, b1_norm) and contains_match(o1_norm, b2_norm):
        return [
            (outcomes[0], bid0, token_ids[0], fair1, fair2),
            (outcomes[1], bid1, token_ids[1], fair2, fair1),
        ]
    if contains_match(o0_norm, b2_norm) and contains_match(o1_norm, b1_norm):
        return [
            (outcomes[0], bid0, token_ids[0], fair2, fair1),
            (outcomes[1], bid1, token_ids[1], fair1, fair2),
        ]
    
    return None


def align_yesno_h2h(
    odds_match: AggregatedMatch,
    market: dict,
    token_ids: list,
    best_bids: dict,
) -> Optional[list]:
    """
    Align Yes/No outcomes for moneyline markets on 3-way sports.
    
    Handles two question formats:
    1. "Will {Team} win on {date}?" → fair_yes = team_prob, fair_no = 1 - team_prob
    2. "Will {TeamA} vs. {TeamB} end in a draw?" → fair_yes = draw_prob, fair_no = 1 - draw_prob
    
    For 3-way sports, fair_no = draw + opponent (or team1 + team2 for draw market),
    which is the correct binary fair value for the No token.
    
    Returns list of (outcome_name, best_bid_price, token_id, fair, other_fair)
    for each side (Yes and No), or None if can't align.
    """
    import re
    
    question = market.get("question", "")
    outcomes = market.get("outcomes", [])
    
    if len(outcomes) < 2 or len(token_ids) < 2:
        return None
    
    # Identify Yes and No indices
    yes_idx = None
    no_idx = None
    for i, o in enumerate(outcomes):
        if o.lower() == "yes":
            yes_idx = i
        elif o.lower() == "no":
            no_idx = i
    
    if yes_idx is None or no_idx is None:
        return None
    
    fair1 = odds_match.fair_prob1 / 100
    fair2 = odds_match.fair_prob2 / 100
    fair_draw = odds_match.fair_prob_draw
    if fair_draw is not None:
        fair_draw = fair_draw / 100
    
    t1_norm = normalize_team(odds_match.team1)
    t2_norm = normalize_team(odds_match.team2)
    
    def _contains(a, b):
        return a in b or b in a
    
    fair_yes = None
    
    # Pattern 1: "Will {TeamA} vs. {TeamB} end in a draw?"
    if "end in a draw" in question.lower() or "draw" in question.lower():
        if fair_draw is not None and fair_draw > 0:
            fair_yes = fair_draw
        else:
            return None  # Can't trade draw without draw probability
    
    # Pattern 2: "Will {Team} win on {date}?" or "Will {Team} win?"
    elif re.search(r'will\s+(.+?)\s+win', question, re.IGNORECASE):
        team_match = re.search(r'will\s+(.+?)\s+win', question, re.IGNORECASE)
        team_name = team_match.group(1).strip()
        team_norm = normalize_team(team_name)
        
        if team_norm == t1_norm or _contains(team_norm, t1_norm):
            fair_yes = fair1
        elif team_norm == t2_norm or _contains(team_norm, t2_norm):
            fair_yes = fair2
        else:
            return None  # Can't identify team
    else:
        return None  # Unrecognized question format
    
    if fair_yes is None or fair_yes <= 0:
        return None
    
    # Binary adjustment: No = everything that's NOT Yes
    fair_no = 1.0 - fair_yes
    
    # Get best bids
    bid_yes = best_bids.get(token_ids[yes_idx], {})
    bid_no = best_bids.get(token_ids[no_idx], {})
    bid_yes_price = bid_yes.get("price", 0) if isinstance(bid_yes, dict) else bid_yes
    bid_no_price = bid_no.get("price", 0) if isinstance(bid_no, dict) else bid_no
    
    return [
        (outcomes[yes_idx], bid_yes_price, token_ids[yes_idx], fair_yes, fair_no),
        (outcomes[no_idx], bid_no_price, token_ids[no_idx], fair_no, fair_yes),
    ]


# team_match_score() has been REMOVED - it enabled dangerous fuzzy matching.
# Use find_matching_odds() or get_fair_value_for_team() for exact matching instead.
