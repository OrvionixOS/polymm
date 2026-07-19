"""
Shared market type parsing for Polymarket positions.

Classifies Polymarket market titles into types (Spread, Totals, BTTS, etc.)
and extracts numeric lines. Used by both the dashboard (analytics) and the
trading pipeline (matching odds to Polymarket markets).

Functions:
    classify_market_type(title, sport) -> str
    extract_line(title, market_type, outcome, event_title) -> Optional[float]
    extract_line_label(title, market_type, outcome, event_title) -> str
    match_odds_to_poly_market(odds_type, odds_line, poly_title, poly_sport) -> bool
"""
import re
from typing import Optional


# --- Market Type Names (canonical) ---
SPREAD = "Spread"
TOTALS = "Totals"
OU = "O/U"
BTTS = "Both Teams to Score"
EXACT_SCORE = "Exact Score"
GOALSCORER = "Goalscorer"
MONEYLINE = "Moneyline"
HALFTIME = "Halftime Result"
HALFTIME_SPREAD = "1H Spread"
HALFTIME_TOTALS = "1H Totals"
SET_HANDICAP = "Set Handicap"
SET_WINNER = "Set Winner"
TOTAL_SETS = "Total Sets"
MAP_WINNER = "Map Winner"  # esports per-map/game winner (Polymarket child_moneyline); line = map #
GO_THE_DISTANCE = "Go the Distance"
ROUNDS_OU = "Rounds O/U"
WIN_BY_KO = "Win by KO/TKO"
TOSS_WINNER = "Toss Winner"
TOSS_MATCH_DOUBLE = "Toss/Match Double"
COMPLETED_MATCH = "Completed Match"
TOP_BATTER = "Top Batter"
MOST_SIXES = "Most Sixes"
PLAYER_PROP = "Player Prop"
MENTIONS = "Mentions"
TEMPERATURE = "Temperature"
DIRECTION = "Direction"
OTHER = "Other"

# Market types that have numeric lines (for line-level breakdowns / matching)
LINE_BEARING_TYPES = {SPREAD, TOTALS, OU, SET_HANDICAP, TOTAL_SETS, ROUNDS_OU, HALFTIME_SPREAD, HALFTIME_TOTALS, PLAYER_PROP, MAP_WINNER}

# the-odds-api market type to Polymarket market type mapping
ODDS_API_TO_POLY_TYPE = {
    "h2h": MONEYLINE,
    "h2h_h1": HALFTIME,
    "spreads": SPREAD,
    "spreads_1h": HALFTIME_SPREAD,
    "totals": TOTALS,
    "totals_1h": HALFTIME_TOTALS,
    "btts": BTTS,
}


def classify_market_type(title: str, sport: str = "") -> str:
    """Classify a Polymarket market by its title into a market type.

    Args:
        title: The market title (e.g., "Spread: Team (-2.5)", "O/U 148.5").
        sport: The sport name for sport-specific classification
               (e.g., "football", "tennis", "ufc", "cricket").

    Returns:
        One of the canonical market type constants.
    """
    if not title:
        return OTHER
    t = title.lower()
    s = sport.lower() if sport else ""

    # --- 1H (Halftime) sub-markets --- MUST be before general spread/totals
    # to prevent "1H LAC -3.5" matching bare spread or "X vs Y: 1H O/U" matching Totals
    if t.startswith("1h spread:") or "1h spreads" in t:
        return HALFTIME_SPREAD
    if re.match(r'^1h\s+.+\s+[+-]\d+\.?\d*$', t):
        return HALFTIME_SPREAD  # Bare 1H spread token: "1H TEAM -3.5"
    if "1h totals" in t or re.match(r'^1h\s+[ou]\s+\d+', t) or "1h o/u" in t:
        return HALFTIME_TOTALS
    if "1h moneyline" in t or "halftime result" in t:
        return HALFTIME

    # --- Tennis sub-markets (most specific first) ---
    if t.startswith("set handicap:"):
        return SET_HANDICAP
    if re.match(r'^set \d+ winner:', t):
        return SET_WINNER
    if "total sets" in t and "o/u" in t:
        return TOTAL_SETS
    if re.match(r'^set \d+ games', t) or t.startswith("match o/u"):
        return TOTALS  # Set games O/U or Match O/U

    # --- Player prop markets (NBA: Points, Rebounds, Assists, etc.) ---
    # Pattern: "PlayerName: StatType O/U X.Y" (e.g., "Jamal Murray: Points O/U 29.5")
    # MUST be checked BEFORE spread detection to prevent "Player +X.Y" false matches.
    _prop_stats = ("points", "rebounds", "assists", "steals", "blocks",
                   "three pointers", "threes", "turnovers", "double doubles",
                   "triple doubles", "free throws")
    if any(f": {stat} " in t or f": {stat}" == t[-len(f": {stat}"):] for stat in _prop_stats):
        return PLAYER_PROP

    # --- Spread markets (all sports) ---
    if t.startswith("spread:"):
        return SPREAD
    # Bare spread tokens: "TEAM -X.Y" or "TEAM +X.Y"
    if re.match(r'^.+\s+[+-]\d+\.?\d*$', t) and "handicap" not in t:
        return SPREAD

    # --- Totals/O/U tokens ---
    if t.strip() in ("over", "under"):
        return OU
    # "O 145.5" / "U 139.5"
    if re.match(r'^[ou]\s+\d+\.?\d*$', t):
        return TOTALS
    # "Over 2.5" / "Under 23.5"
    if re.match(r'^(?:over|under)\s+\d+\.?\d*$', t):
        return OU

    # --- UFC sub-markets ---
    if s == "ufc":
        if "go the distance" in t:
            return GO_THE_DISTANCE
        if re.match(r'^o/u\s+\d+\.?\d*\s+rounds?$', t):
            return ROUNDS_OU
        if "win by ko" in t or "won by ko" in t or "by ko or tko" in t:
            return WIN_BY_KO
        if "fight be won by ko" in t or "fight be won by tko" in t:
            return WIN_BY_KO
        if t.startswith("will ") and any(kw in t for kw in ["say ", "said ", "mention "]):
            return MENTIONS

    # --- Football sub-markets ---
    if s == "football":
        if "both teams to score" in t:
            return BTTS
        if t.strip() == "draw":
            return MONEYLINE
        if "exact score" in t:
            return EXACT_SCORE
        if "goalscorer" in t or "anytime goalscorer" in t:
            return GOALSCORER

    # --- Cricket sub-markets ---
    if s == "cricket":
        if "who wins the toss" in t or "toss winner" in t:
            return TOSS_WINNER
        if "toss" in t and ("match double" in t or "double" in t):
            return TOSS_MATCH_DOUBLE
        if "top batter" in t:
            return TOP_BATTER
        if "most sixes" in t:
            return MOST_SIXES
        if "completed match" in t:
            return COMPLETED_MATCH

    # --- Non-sport markets ---
    if "temperature" in t:
        return TEMPERATURE
    if "up or down" in t:
        return DIRECTION
    if s == "mentions":
        return MENTIONS

    # --- Esports per-map/game winner (Polymarket child_moneyline) ---
    # "LoL: A vs B - Game 1 Winner" / "Map 2 Winner" / "Game 3 Winner". MUST be
    # before the " vs " moneyline fallback, which would otherwise swallow it.
    if re.search(r'\b(?:game|map)\s+\d+\s+winner\b', t):
        return MAP_WINNER

    # --- Moneyline / Totals from event-level titles ---
    if " vs " in t or " vs. " in t:
        if "o/u" in t:
            return TOTALS
        if "spread" not in t:
            return MONEYLINE
    if t.startswith("will ") and "win" in t:
        return MONEYLINE

    return OTHER


def extract_line(title: str, market_type: str, outcome: str = "",
                 event_title: str = "") -> Optional[float]:
    """Extract the numeric line from a Polymarket market title.

    Args:
        title: The market title.
        market_type: The classified market type (from classify_market_type).
        outcome: The outcome name (e.g., "Over", "Under") for direction.
        event_title: Parent event title for fallback parsing.

    Returns:
        The numeric line value, or None if not applicable / not found.

    Examples:
        >>> extract_line("Spread: Team (-2.5)", "Spread")
        -2.5
        >>> extract_line("O/U 148.5", "Totals")
        148.5
        >>> extract_line("Set Handicap: Player (-1.5) vs Player (+1.5)", "Set Handicap")
        -1.5
    """
    t = title.strip()

    # --- Spreads (full-game and 1H) ---
    if market_type in (SPREAD, HALFTIME_SPREAD):
        # "Spread: Team (-9.5)" or "1H Spread: Team (-3.5)" → -9.5 / -3.5
        m = re.search(r'\(([+-]\d+\.?\d*)\)', t)
        if m:
            return float(m.group(1))
        # "TEAM -X.Y" at end of title
        m = re.search(r'([+-]\d+\.?\d*)\s*$', t)
        if m:
            return float(m.group(1))
        # Any +/- number in title
        m = re.search(r'[+-]\d+\.?\d*', t)
        if m:
            return float(m.group(0))
        return None

    # --- Set Handicap (Tennis) ---
    if market_type == SET_HANDICAP:
        m = re.search(r'\(([+-]\d+\.?\d*)\)', t)
        if m:
            return float(m.group(1))
        return None

    # --- Map Winner (esports) — the "line" is the map/game number ---
    if market_type == MAP_WINNER:
        m = re.search(r'\b(?:game|map)\s+(\d+)\b', t, re.IGNORECASE)
        if m:
            return float(m.group(1))
        return None

    # --- Player Props (same O/U extraction as Totals) ---
    if market_type == PLAYER_PROP:
        # "Player: Points O/U 29.5" → 29.5
        m = re.search(r'O/U\s+(\d+\.?\d*)', t, re.IGNORECASE)
        if m:
            return float(m.group(1))
        return None

    # --- Totals / O/U / Total Sets / Rounds O/U ---
    if market_type in (TOTALS, OU, TOTAL_SETS, ROUNDS_OU, HALFTIME_TOTALS):
        # "O 148.5" / "U 148.5"
        m = re.match(r'^[OU]\s+(\d+\.?\d*)$', t, re.IGNORECASE)
        if m:
            return float(m.group(1))
        # "Over 2.5" / "Under 2.5"
        m = re.match(r'^(?:Over|Under)\s+(\d+\.?\d*)$', t, re.IGNORECASE)
        if m:
            return float(m.group(1))
        # "O/U X.Y Rounds"
        m = re.match(r'^O/U\s+(\d+\.?\d*)\s+Rounds?$', t, re.IGNORECASE)
        if m:
            return float(m.group(1))
        # "Total Sets O/U 2.5" / "Set 1 Games O/U 9.5" / "Match O/U 23.5"
        m = re.search(r'O/U\s+(\d+\.?\d*)', t, re.IGNORECASE)
        if m:
            return float(m.group(1))
        # Event-level: "Team A vs Team B: O/U 148.5"
        m = re.search(r'[Oo]/[Uu]\s+(\d+\.?\d*)', t)
        if m:
            return float(m.group(1))
        # Bare "Over" or "Under" — fall back to event_title
        if t.lower() in ("over", "under") and event_title:
            m2 = re.search(r'[Oo]/[Uu]\s+(\d+\.?\d*)', event_title)
            if m2:
                return float(m2.group(1))
            m2 = re.search(r'(\d+\.5)', event_title)
            if m2:
                return float(m2.group(1))
        return None

    return None


def extract_line_label(title: str, market_type: str, outcome: str = "",
                       event_title: str = "") -> str:
    """Extract a display label for the line (e.g., "-9.5", "O 148.5", "U 2.5").

    Returns empty string if not applicable, "Unknown" if parsing failed.
    Used for dashboard line-level breakdowns.
    """
    if market_type not in LINE_BEARING_TYPES:
        return ""

    line = extract_line(title, market_type, outcome, event_title)
    if line is None:
        return "Unknown"

    outcome_lower = outcome.lower() if outcome else ""

    if market_type in (SPREAD, HALFTIME_SPREAD):
        return f"{line:+g}"  # "+2.5" or "-2.5"

    if market_type == SET_HANDICAP:
        return f"{line:+g}"

    if market_type in (TOTALS, OU, TOTAL_SETS, HALFTIME_TOTALS):
        if "over" in outcome_lower or outcome_lower.startswith("o"):
            return f"O {line:g}"
        elif "under" in outcome_lower or outcome_lower.startswith("u"):
            return f"U {line:g}"
        return f"O/U {line:g}"

    if market_type == ROUNDS_OU:
        if "over" in outcome_lower:
            return f"O {line:g} Rds"
        elif "under" in outcome_lower:
            return f"U {line:g} Rds"
        return f"O/U {line:g} Rds"

    if market_type == PLAYER_PROP:
        if "over" in outcome_lower or outcome_lower == "yes":
            return f"O {line:g}"
        elif "under" in outcome_lower or outcome_lower == "no":
            return f"U {line:g}"
        return f"O/U {line:g}"

    return ""


def match_odds_to_poly_market(odds_market_type: str, odds_line: Optional[float],
                                poly_title: str, poly_sport: str = "") -> bool:
    """Check if an odds-api market matches a Polymarket market.

    Maps the-odds-api market types to Polymarket title patterns.

    Args:
        odds_market_type: the-odds-api market key ("h2h", "spreads", "totals", "btts").
        odds_line: The line from the-odds-api (e.g., 2.5, -1.5). None for h2h/btts.
        poly_title: The Polymarket market title.
        poly_sport: The sport for context (e.g., "football", "tennis").

    Returns:
        True if the odds market matches the Polymarket market.

    Examples:
        >>> match_odds_to_poly_market("spreads", -1.5, "Spread: Real Madrid (-1.5)", "football")
        True
        >>> match_odds_to_poly_market("totals", 2.5, "Real Madrid vs Barcelona: O/U 2.5", "football")
        True
        >>> match_odds_to_poly_market("h2h", None, "Real Madrid vs Barcelona", "football")
        True
    """
    poly_type = classify_market_type(poly_title, poly_sport)

    # Map odds-api type to expected Polymarket type(s)
    expected_types = {
        "h2h": {MONEYLINE},
        "h2h_h1": {HALFTIME},
        "spreads": {SPREAD, SET_HANDICAP},
        "spreads_1h": {HALFTIME_SPREAD},
        "totals": {TOTALS, OU, TOTAL_SETS, ROUNDS_OU},
        "totals_1h": {HALFTIME_TOTALS},
        "btts": {BTTS},
        "map_winner": {MAP_WINNER},  # esports per-map winner; line = map #
    }

    expected = expected_types.get(odds_market_type, set())
    if poly_type not in expected:
        return False

    # For line-bearing markets, verify the line matches
    if odds_line is not None and poly_type in LINE_BEARING_TYPES:
        poly_line = extract_line(poly_title, poly_type)
        if poly_line is not None:
            # Lines must match (spreads can be +/- so compare absolute)
            if poly_type in (SPREAD, SET_HANDICAP):
                # Spread can be from either team's perspective
                if abs(abs(poly_line) - abs(odds_line)) > 0.01:
                    return False
            else:
                # Totals / map number must match exactly
                if abs(poly_line - odds_line) > 0.01:
                    return False

    return True
