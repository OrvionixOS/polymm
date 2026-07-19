"""
Integration Tests: No-Token Fair Value Inversion

Validates that all fair value code paths correctly handle "No" token
orders (from "Will X win?" binary markets in 3-way sports).

Three code paths compute No-token fair values:
1. get_fair_value_for_team("X No", match) — scanner lookup
2. find_v2_fair_value(outcome="No") — hydration fallback
3. Hydration team extraction — extracts correct teams for find_v2_fair_value

All must produce: no_fair = 1 - base_team_fair (includes draw + opponent)

Run with:
    proxychains4 -q python -m pytest tests/integration/test_hydrated_order_edges.py -v -s
"""
import re
import pytest
from typing import Optional, Tuple

from src.services.odds_service import OddsService, AggregatedMatch
from src.core.match_id import normalize_team, make_match_id
from src.scanning.team_matcher import get_fair_value_for_team


# =====================================================================
# Helpers
# =====================================================================

def _parse_match_teams(match_display: str) -> Tuple[str, str]:
    """Parse 'Team A vs Team B' into (team1, team2)."""
    parts = match_display.split(" vs ")
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return match_display, ""


def _find_v2_match(odds_service: OddsService, team1: str, team2: str,
                    game: str, market_type: str = "h2h") -> Optional[AggregatedMatch]:
    """Find a v2 cache entry matching the given teams and game.
    
    Uses the EXACT same matching logic as find_v2_fair_value to ensure
    we find the same entry and get consistent fair values.
    """
    t1_norm = normalize_team(team1)
    t2_norm = normalize_team(team2)

    def _tm(a: str, b: str) -> bool:
        if not a or not b:
            return False
        return a == b or a in b or b in a

    for v2_match in odds_service._v2_cache.values():
        if v2_match.fair_prob1 <= 0 or v2_match.fair_prob2 <= 0:
            continue

        # Same market_type filter as find_v2_fair_value (line 422-425)
        if market_type:
            if v2_match.market_type != market_type:
                if not (market_type == "h2h" and v2_match.market_type in ("h2h_h1",)):
                    continue

        om_t1 = normalize_team(v2_match.team1)
        om_t2 = normalize_team(v2_match.team2)

        # Both teams must match (either order) — same logic as find_v2_fair_value
        if ((_tm(t1_norm, om_t1) and _tm(t2_norm, om_t2)) or
            (_tm(t1_norm, om_t2) and _tm(t2_norm, om_t1))):
            return v2_match

    return None


def _get_all_3way_h2h_matches(odds_service: OddsService):
    """Get all 3-way h2h v2 matches (those with draw probability)."""
    results = []
    for v2_match in odds_service._v2_cache.values():
        if v2_match.market_type != "h2h":
            continue
        if v2_match.fair_prob1 <= 0 or v2_match.fair_prob2 <= 0:
            continue
        if not v2_match.fair_prob_draw or v2_match.fair_prob_draw <= 0:
            continue
        results.append(v2_match)
    return results


# =====================================================================
# Test cases: representative No-token orders from real hydration
# =====================================================================

NO_TOKEN_CASES = [
    # (base_team, match_display, game)
    # Football — base team is alphabetically first in match_id
    ("Sweden", "Sweden vs Ukraine", "football"),
    ("Romania", "Romania vs Türkiye", "football"),
    # Football — base team is alphabetically SECOND in match_id
    ("Northern Ireland", "Italy vs Northern Ireland", "football"),
    ("North Macedonia", "Denmark vs North Macedonia", "football"),
    ("Wales", "Bosnia and Herzegovina vs Wales", "football"),
    # Football — various
    ("NEC", "NEC vs PSV", "football"),
    ("Al Najmah Saudi Club", "Al Najmah Saudi Club vs Damac Saudi Club", "football"),
    ("Slovakia", "Kosovo vs Slovakia", "football"),
    # Rugby
    ("Stormers", "Bulls vs Stormers", "rugby"),
    ("Scarlets", "Connacht vs Scarlets", "rugby"),
]


# =====================================================================
# Tests
# =====================================================================

@pytest.mark.asyncio
async def test_get_fair_value_for_team_no_token_inversion(odds_service):
    """Validate get_fair_value_for_team correctly inverts No tokens.

    For each 3-way h2h match, verifies:
    1. get_fair_value_for_team("X No", match) = 1 - get_fair_value_for_team("X", match)
    2. Yes + No = 1.0 (binary constraint)

    This is the scanner-level code path used by OpportunityScanner.
    """
    matches_3way = _get_all_3way_h2h_matches(odds_service)
    if not matches_3way:
        pytest.skip("no 3-way h2h matches in v2 cache (needs live Supabase sports_odds_v2 data)")

    errors = []
    checked = 0

    for v2_match in matches_3way:
        t1 = v2_match.team1
        t2 = v2_match.team2

        # Test both teams as No-token base
        for base_team in [t1, t2]:
            yes_fair = get_fair_value_for_team(base_team, v2_match)
            no_fair = get_fair_value_for_team(f"{base_team} No", v2_match)

            if yes_fair is None or no_fair is None:
                continue

            checked += 1

            # Binary constraint: Yes + No must equal 1.0
            binary_sum = yes_fair + no_fair
            if abs(binary_sum - 1.0) > 0.001:
                errors.append(
                    f"  {base_team} No | {t1} vs {t2}: "
                    f"Yes={yes_fair:.4f} + No={no_fair:.4f} = {binary_sum:.4f} ≠ 1.0"
                )

            # No fair must be 1 - yes_fair
            expected_no = 1.0 - yes_fair
            if abs(no_fair - expected_no) > 0.001:
                errors.append(
                    f"  {base_team} No | {t1} vs {t2}: "
                    f"No={no_fair:.4f}, expected={expected_no:.4f} (1 - {yes_fair:.4f})"
                )

    print(f"\n  Checked {checked} team/No pairs across {len(matches_3way)} 3-way matches")
    if errors:
        print(f"\n  ❌ {len(errors)} errors:")
        for e in errors[:10]:
            print(e)
    else:
        print(f"  ✅ All get_fair_value_for_team No inversions correct")

    assert not errors, f"{len(errors)} inversion errors:\n" + "\n".join(errors[:5])


@pytest.mark.asyncio
async def test_find_v2_fair_value_no_token_inversion(odds_service):
    """Validate find_v2_fair_value correctly inverts No outcomes.

    For each test case, calls find_v2_fair_value with:
    - outcome=base_team → should return team's raw probability
    - outcome="No" → should return 1 - team_prob

    This is the hydration-level code path used during startup.
    """
    errors = []
    checked = 0

    for base_team, match_display, game in NO_TOKEN_CASES:
        team1, team2 = _parse_match_teams(match_display)

        h2h_match = _find_v2_match(odds_service, team1, team2, game, "h2h")
        if not h2h_match:
            continue
        if not h2h_match.fair_prob_draw or h2h_match.fair_prob_draw <= 0:
            continue

        checked += 1

        # Get base team probability from v2
        base_norm = normalize_team(base_team)
        v2_t1_norm = normalize_team(h2h_match.team1)
        v2_t2_norm = normalize_team(h2h_match.team2)

        if base_norm in v2_t1_norm or v2_t1_norm in base_norm:
            base_prob = h2h_match.fair_prob1 / 100.0
        elif base_norm in v2_t2_norm or v2_t2_norm in base_norm:
            base_prob = h2h_match.fair_prob2 / 100.0
        else:
            errors.append(f"  Cannot find {base_team} in {h2h_match.team1} vs {h2h_match.team2}")
            continue

        expected_no = 1.0 - base_prob

        # Determine opponent for proper two-team lookup
        opponent = team2 if normalize_team(base_team) in normalize_team(team1) or normalize_team(team1) in normalize_team(base_team) else team1

        # For "No" outcome: team1 MUST be the base team (find_v2_fair_value inverts team1's prob)
        no_fair = odds_service.find_v2_fair_value(
            team1=base_team, team2=opponent,
            outcome="No", market_type="h2h",
        )
        # For team outcome: order doesn't matter
        yes_fair = odds_service.find_v2_fair_value(
            team1=team1, team2=team2,
            outcome=base_team, market_type="h2h",
        )

        label = f"{base_team} No ({match_display})"

        if no_fair is None:
            errors.append(f"  {label}: find_v2_fair_value(No) returned None")
            continue
        if yes_fair is None:
            errors.append(f"  {label}: find_v2_fair_value({base_team}) returned None")
            continue

        no_diff = abs(no_fair - expected_no)
        if no_diff > 0.01:
            errors.append(
                f"  {label}: No={no_fair*100:.1f}%, expected={expected_no*100:.1f}% "
                f"(1-{base_prob*100:.1f}%) Δ={no_diff*100:.1f}pp"
            )

        binary_sum = yes_fair + no_fair
        if abs(binary_sum - 1.0) > 0.02:
            errors.append(
                f"  {label}: Yes+No={binary_sum:.3f} ≠ 1.0 "
                f"(Yes={yes_fair:.3f}, No={no_fair:.3f})"
            )

    print(f"\n  Checked {checked} No-token cases via find_v2_fair_value")
    if errors:
        print(f"\n  ❌ {len(errors)} errors:")
        for e in errors:
            print(e)
    else:
        print(f"  ✅ All find_v2_fair_value No inversions correct")

    if checked == 0:
        pytest.skip("no 3-way matches in v2 cache (needs live Supabase sports_odds_v2 data)")
    assert not errors, f"{len(errors)} errors:\n" + "\n".join(errors[:5])


@pytest.mark.asyncio
async def test_hydration_team_extraction_for_no_tokens(odds_service):
    """Validate the hydration team extraction handles Yes/No market collapse.

    The hydration code strips " No" from BotState team names before calling
    find_v2_fair_value. For "Will X win?" markets, team1="X" and team2="X No"
    both clean to "X". The fix extracts real teams from the match_id.

    This test simulates the exact hydration code path:
    1. Create BotState-like team names (base, base+" No")
    2. Clean them using the same regex as hydration
    3. Detect collapse and extract from match_id
    4. Verify find_v2_fair_value returns correct No-token fair value
    """
    errors = []
    checked = 0

    for base_team, match_display, game in NO_TOKEN_CASES:
        team1, team2 = _parse_match_teams(match_display)

        h2h_match = _find_v2_match(odds_service, team1, team2, game, "h2h")
        if not h2h_match:
            continue
        if not h2h_match.fair_prob_draw or h2h_match.fair_prob_draw <= 0:
            continue

        checked += 1

        # Simulate BotState team names for a "Will X win?" market
        bs_team1 = base_team        # Yes side
        bs_team2 = f"{base_team} No"  # No side

        # Simulate the hydration cleaning (exact same regex as hydration.py line 750-751)
        fb_t1 = re.sub(r'[:\s]*(?:O/U\s*[\d.]+|Spread\s*[-+]?[\d.]+|\bNo\b)$', '', bs_team1).strip()
        fb_t2 = re.sub(r'[:\s]*(?:O/U\s*[\d.]+|Spread\s*[-+]?[\d.]+|\bNo\b)$', '', bs_team2).strip()

        # Detect collapse and fix (exact same logic as the fix in hydration.py)
        if fb_t1 and fb_t2 and normalize_team(fb_t1) == normalize_team(fb_t2):
            base_team_norm = normalize_team(fb_t1)  # base team before collapse
            # Build match_id as sports_bot would
            match_id = make_match_id(team1, team2, game) + ":0xFAKE"
            mid_parts = match_id.split(":vs:")
            if len(mid_parts) == 2:
                mid_t1 = mid_parts[0].split(":", 1)[-1]
                mid_t2 = mid_parts[1].split(":")[0]
                if mid_t1 and mid_t2:
                    # Put base team first — find_v2_fair_value inverts fb_t1's prob
                    mid_t1_norm = normalize_team(mid_t1)
                    if mid_t1_norm == base_team_norm or base_team_norm in mid_t1_norm or mid_t1_norm in base_team_norm:
                        fb_t1, fb_t2 = mid_t1, mid_t2
                    else:
                        fb_t1, fb_t2 = mid_t2, mid_t1

        # Now call find_v2_fair_value with the extracted teams (same as hydration)
        no_fair = odds_service.find_v2_fair_value(
            team1=fb_t1, team2=fb_t2,
            outcome="No", market_type="h2h",
        )

        label = f"{base_team} No ({match_display})"

        if no_fair is None:
            errors.append(f"  {label}: find_v2_fair_value returned None with fb_t1={fb_t1}, fb_t2={fb_t2}")
            continue

        # Expected: 1 - base_team_prob
        base_norm = normalize_team(base_team)
        v2_t1_norm = normalize_team(h2h_match.team1)
        v2_t2_norm = normalize_team(h2h_match.team2)

        if base_norm in v2_t1_norm or v2_t1_norm in base_norm:
            base_prob = h2h_match.fair_prob1 / 100.0
        elif base_norm in v2_t2_norm or v2_t2_norm in base_norm:
            base_prob = h2h_match.fair_prob2 / 100.0
        else:
            errors.append(f"  {label}: Cannot determine base prob")
            continue

        expected_no = 1.0 - base_prob
        diff = abs(no_fair - expected_no)

        if diff > 0.01:
            errors.append(
                f"  {label}: No={no_fair*100:.1f}%, expected={expected_no*100:.1f}% "
                f"(1-{base_prob*100:.1f}%) Δ={diff*100:.1f}pp | "
                f"fb_t1={fb_t1}, fb_t2={fb_t2}"
            )

    print(f"\n  Simulated hydration team extraction for {checked} Yes/No markets")
    if errors:
        print(f"\n  ❌ {len(errors)} errors:")
        for e in errors:
            print(e)
    else:
        print(f"  ✅ All hydration team extractions produce correct No fair values")

    if checked == 0:
        pytest.skip("no 3-way matches in v2 cache (needs live Supabase sports_odds_v2 data)")
    assert not errors, f"{len(errors)} errors:\n" + "\n".join(errors[:5])
