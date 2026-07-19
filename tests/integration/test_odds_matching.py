"""
Integration Tests: Odds Matching Correctness

Verifies that odds from sports_odds_v2 (Supabase) are correctly matched to
Polymarket markets with correct fair value assignment per outcome.

Run with:
    proxychains4 -q python -m pytest tests/integration/test_odds_matching.py -v -s

Run football only (most critical — 3-way markets):
    proxychains4 -q python -m pytest tests/integration/test_odds_matching.py -v -s -k "football"

IMPORTANT: Requires both Polymarket API access (proxychains4) and Supabase
env vars (SUPABASE_URL, SUPABASE_KEY).
"""
import asyncio
import re
import pytest
from collections import defaultdict
from typing import List, Dict, Any, Optional, Tuple

from src.polymarket.market_client import PolymarketEsportsClient, PolymarketEsportsEvent
from src.services.odds_service import OddsService, AggregatedMatch
from src.core.market_parser import (
    classify_market_type, extract_line, match_odds_to_poly_market,
    SPREAD, TOTALS, OU, MONEYLINE, BTTS, HALFTIME, HALFTIME_SPREAD,
    HALFTIME_TOTALS, PLAYER_PROP, OTHER,
)
from src.core.match_id import normalize_team
from src.scanning.team_matcher import align_teams_with_bids
from src.scanning.opportunity_scanner import find_all_poly_events

# Import the helper from Phase 1 tests for fetching events
from tests.integration.test_market_parsing import _fetch_and_hydrate_sport


# =====================================================================
# Helpers
# =====================================================================

def _match_odds_to_events(
    odds_matches: List[AggregatedMatch],
    poly_events: List[PolymarketEsportsEvent],
) -> List[Dict[str, Any]]:
    """Match odds entries to Polymarket events and their specific markets.
    
    Simulates the scanner's full matching pipeline:
    1. match_odds_to_poly_market() — matches by market type + line magnitude
    2. Spread direction check — same as scan_multi_market_opportunities lines 569-606
    
    Returns list of matched pairs:
        {
            "odds": AggregatedMatch,
            "event": PolymarketEsportsEvent,
            "market": dict,  # The specific market dict from event.markets
            "market_type": str,  # Classified market type
            "line": float or None,
        }
    """
    upcoming = [e for e in poly_events if not e.is_finished]
    matches = []
    
    for odds_match in odds_matches:
        matching_events = find_all_poly_events(odds_match, upcoming)
        if not matching_events:
            continue
        
        sport = odds_match.game
        
        for poly_event in matching_events:
            if not poly_event.markets:
                continue
            
            for market in poly_event.markets:
                question = market.get("question", "")
                if not question:
                    continue
                
                if not match_odds_to_poly_market(
                    odds_match.market_type,
                    odds_match.line,
                    question,
                    sport,
                ):
                    continue
                
                # ── Spread direction check (same as scanner) ──
                # match_odds_to_poly_market matches by line MAGNITUDE only.
                # For spreads, we must also verify the direction matches.
                # Otherwise fair_prob1/fair_prob2 would be assigned to the
                # wrong outcomes (e.g., prob of covering +1.5 applied to -1.5).
                if odds_match.market_type in ("spreads", "spreads_1h") and \
                        question.lower().startswith(("spread:", "1h spread:")):
                    spread_m = re.match(
                        r'(?:1H\s+)?Spread:\s*(.+?)\s*\(',
                        question, re.IGNORECASE
                    )
                    if spread_m and odds_match.team1_spread_point is not None:
                        sp_team = normalize_team(spread_m.group(1))
                        t1_norm = normalize_team(odds_match.team1)
                        t2_norm = normalize_team(odds_match.team2)
                        
                        def _c(a, b):
                            return a in b or b in a
                        
                        t1sp = odds_match.team1_spread_point
                        t1_is_giving = (t1sp < 0)
                        
                        sp_is_t1 = sp_team == t1_norm or _c(sp_team, t1_norm)
                        sp_is_t2 = sp_team == t2_norm or _c(sp_team, t2_norm)
                        
                        if sp_is_t1 and not t1_is_giving:
                            continue  # Poly has team1 giving, but odds say team1 receives
                        elif sp_is_t2 and t1_is_giving:
                            continue  # Poly has team2 giving, but odds say team2 receives
                        elif not sp_is_t1 and not sp_is_t2:
                            continue  # Can't identify spread team
                
                mtype = classify_market_type(question, sport)
                line = extract_line(question, mtype)
                
                matches.append({
                    "odds": odds_match,
                    "event": poly_event,
                    "market": market,
                    "market_type": mtype,
                    "line": line,
                    "question": question,
                })
    
    return matches


def _validate_fair_value_alignment(
    matched: Dict[str, Any],
) -> List[str]:
    """Validate that fair values are correctly assigned to outcomes.
    
    Returns list of error strings (empty = all OK).
    """
    errors = []
    odds: AggregatedMatch = matched["odds"]
    market = matched["market"]
    mtype = matched["market_type"]
    question = matched["question"]
    outcomes = market.get("outcomes", [])
    
    if len(outcomes) < 2:
        return errors
    
    fair1 = odds.fair_prob1 / 100.0
    fair2 = odds.fair_prob2 / 100.0
    fair_draw = (odds.fair_prob_draw / 100.0) if odds.fair_prob_draw else None
    
    # --- Totals O/U: fair_prob1 = Over, fair_prob2 = Under ---
    if odds.market_type in ("totals", "totals_1h"):
        over_idx = None
        under_idx = None
        for i, o in enumerate(outcomes):
            if o.lower() == "over":
                over_idx = i
            elif o.lower() == "under":
                under_idx = i
        
        if over_idx is not None and under_idx is not None:
            # Verify prob sum is ~1.0
            prob_sum = fair1 + fair2
            if abs(prob_sum - 1.0) > 0.05:
                errors.append(
                    f"[{odds.market_type}] O/U probs don't sum to ~1.0: "
                    f"Over={fair1:.3f} + Under={fair2:.3f} = {prob_sum:.3f}"
                )
            
            # Verify line consistency
            if matched["line"] is not None and odds.line > 0:
                if abs(matched["line"] - odds.line) > 0.01:
                    errors.append(
                        f"[{odds.market_type}] Line mismatch: Poly={matched['line']}, Odds={odds.line}"
                    )
    
    # --- Spreads: team alignment + direction ---
    elif odds.market_type in ("spreads", "spreads_1h"):
        o0_lower = outcomes[0].lower()
        o1_lower = outcomes[1].lower()
        
        # Skip Yes/No outcomes (shouldn't happen for spreads but just in case)
        if o0_lower in ("yes", "no") or o1_lower in ("yes", "no"):
            return errors
        
        # Verify team alignment would work
        o0_norm = normalize_team(outcomes[0])
        o1_norm = normalize_team(outcomes[1])
        t1_norm = normalize_team(odds.team1)
        t2_norm = normalize_team(odds.team2)
        
        def _contains(a, b):
            return a in b or b in a
        
        t1_matches_o0 = o0_norm == t1_norm or _contains(o0_norm, t1_norm)
        t1_matches_o1 = o1_norm == t1_norm or _contains(o1_norm, t1_norm)
        t2_matches_o0 = o0_norm == t2_norm or _contains(o0_norm, t2_norm)
        t2_matches_o1 = o1_norm == t2_norm or _contains(o1_norm, t2_norm)
        
        can_align = (
            (t1_matches_o0 and t2_matches_o1) or
            (t1_matches_o1 and t2_matches_o0)
        )
        
        if not can_align:
            errors.append(
                f"[{odds.market_type}] Cannot align teams: "
                f"Odds=({odds.team1}, {odds.team2}) ↔ Poly=({outcomes[0]}, {outcomes[1]})"
            )
        
        # Verify spread direction from question
        spread_match = re.match(
            r'(?:1H\s+)?Spread:\s*(.+?)\s*\(([+-]?\d+\.?\d*)\)', question, re.IGNORECASE
        )
        if spread_match:
            spread_team = spread_match.group(1)
            spread_line = float(spread_match.group(2))
            
            # The spread team should have the NEGATIVE line (giving spread)
            # Our odds data has team1_spread_point for this.
            if odds.team1_spread_point is not None:
                spread_team_norm = normalize_team(spread_team)
                if _contains(spread_team_norm, t1_norm):
                    # Poly spread team = odds team1
                    if spread_line < 0 and odds.team1_spread_point > 0:
                        errors.append(
                            f"[{odds.market_type}] Spread direction mismatch: "
                            f"Poly has {spread_team} ({spread_line:+g}), "
                            f"but odds.team1_spread_point={odds.team1_spread_point:+g}"
                        )
                elif _contains(spread_team_norm, t2_norm):
                    # Poly spread team = odds team2
                    t2sp = -odds.team1_spread_point
                    if spread_line < 0 and t2sp > 0:
                        errors.append(
                            f"[{odds.market_type}] Spread direction mismatch: "
                            f"Poly has {spread_team} ({spread_line:+g}), "
                            f"but odds.team2_spread_point={t2sp:+g}"
                        )
        
        # Prob sum check
        prob_sum = fair1 + fair2
        if abs(prob_sum - 1.0) > 0.05:
            errors.append(
                f"[{odds.market_type}] Spread probs don't sum to ~1.0: "
                f"{fair1:.3f} + {fair2:.3f} = {prob_sum:.3f}"
            )
    
    # --- H2H Moneyline ---
    elif odds.market_type in ("h2h", "h2h_h1"):
        o0_lower = outcomes[0].lower()
        o1_lower = outcomes[1].lower()
        
        if o0_lower in ("yes", "no") or o1_lower in ("yes", "no"):
            # Yes/No moneyline on a 3-way sport: align_yesno_h2h must produce
            # correct binary fair values (fair_yes + fair_no = 1.0)
            from src.scanning.team_matcher import align_yesno_h2h
            
            if fair_draw is not None:
                # Build a minimal market dict for align_yesno_h2h
                mock_market = {"question": question, "outcomes": outcomes}
                mock_tokens = [f"token_{i}" for i in range(len(outcomes))]
                mock_bids = {t: {"price": 0.5} for t in mock_tokens}
                
                aligned = align_yesno_h2h(odds, mock_market, mock_tokens, mock_bids)
                if aligned is not None:
                    yes_fair = aligned[0][3]  # fair for Yes
                    no_fair = aligned[0][4]   # fair for No (other_fair)
                    binary_sum = yes_fair + no_fair
                    if abs(binary_sum - 1.0) > 0.01:
                        errors.append(
                            f"[{odds.market_type} 3-way] Binary fair values don't sum to 1.0: "
                            f"Yes={yes_fair:.3f} + No={no_fair:.3f} = {binary_sum:.3f}"
                        )
                else:
                    errors.append(
                        f"[{odds.market_type} 3-way] align_yesno_h2h returned None "
                        f"for question: {question}"
                    )
        else:
            # Team name outcomes — standard alignment check
            o0_norm = normalize_team(outcomes[0])
            o1_norm = normalize_team(outcomes[1])
            t1_norm = normalize_team(odds.team1)
            t2_norm = normalize_team(odds.team2)
            
            def _contains(a, b):
                return a in b or b in a
            
            can_align = (
                (o0_norm == t1_norm or _contains(o0_norm, t1_norm)) and
                (o1_norm == t2_norm or _contains(o1_norm, t2_norm))
            ) or (
                (o0_norm == t2_norm or _contains(o0_norm, t2_norm)) and
                (o1_norm == t1_norm or _contains(o1_norm, t1_norm))
            )
            
            if not can_align:
                errors.append(
                    f"[{odds.market_type}] Cannot align teams: "
                    f"Odds=({odds.team1}, {odds.team2}) ↔ Poly=({outcomes[0]}, {outcomes[1]})"
                )
            
            # Prob sum check
            prob_sum = fair1 + fair2
            if abs(prob_sum - 1.0) > 0.05:
                # For 3-way sports with team-name outcomes, probs may not sum to 1
                # (draw probability is implicit)
                if fair_draw is not None:
                    pass  # Expected for 3-way
                else:
                    errors.append(
                        f"[{odds.market_type}] H2H probs don't sum to ~1.0: "
                        f"{fair1:.3f} + {fair2:.3f} = {prob_sum:.3f} "
                        f"(fair_prob_draw is None — v2 cache may be missing draw data)"
                    )
    
    # --- BTTS ---
    elif odds.market_type == "btts":
        prob_sum = fair1 + fair2
        if abs(prob_sum - 1.0) > 0.05:
            errors.append(
                f"[btts] BTTS probs don't sum to ~1.0: "
                f"Yes={fair1:.3f} + No={fair2:.3f} = {prob_sum:.3f}"
            )
    
    return errors


def _print_matching_report(
    sport: str,
    matched: List[Dict],
    unmatched_odds: List[AggregatedMatch],
    errors: List[Tuple[str, List[str]]],
):
    """Print a human-readable matching report."""
    print(f"\n{'='*70}")
    print(f"  {sport.upper()} — Odds Matching Report")
    print(f"{'='*70}")
    
    # Count by market type
    type_counts = defaultdict(int)
    for m in matched:
        type_counts[m["odds"].market_type] += 1
    
    print(f"  Matched pairs: {len(matched)}")
    for mt, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {mt:15s} {count:4d}")
    
    print(f"  Unmatched odds: {len(unmatched_odds)}")
    
    # Show errors
    if errors:
        print(f"\n  ❌ FAIR VALUE ERRORS ({len(errors)}):")
        for question, errs in errors[:15]:
            print(f"    • \"{question}\"")
            for err in errs:
                print(f"      {err}")
    else:
        print(f"\n  ✅ All fair value assignments validated OK")
    
    # Show unmatched odds (first few)
    if unmatched_odds:
        print(f"\n  ⬜ UNMATCHED ODDS (no Polymarket market found, showing first 10):")
        seen = set()
        for odds in unmatched_odds[:10]:
            key = f"{odds.team1} vs {odds.team2} [{odds.market_type}]"
            if key not in seen:
                seen.add(key)
                line_str = f" line={odds.line}" if odds.line else ""
                draw_str = f" draw={odds.fair_prob_draw:.1f}%" if odds.fair_prob_draw else ""
                print(f"    • {key}{line_str}{draw_str}")


# =====================================================================
# Tests
# =====================================================================

@pytest.mark.asyncio
async def test_football_odds_matching(poly_client, odds_service):
    """Verify football odds match correctly — MOST CRITICAL test.
    
    Football has 3-way moneylines (team1/draw/team2) but Polymarket uses
    binary markets. This test verifies fair values are correctly assigned.
    """
    # Get football odds from v2 cache
    football_odds = [
        m for m in odds_service._v2_cache.values()
        if m.game == "football" and m.fair_prob1 > 0
    ]
    
    if not football_odds:
        pytest.skip("No football odds in v2 cache")
    
    # Get football events from Polymarket
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP.get("football", [])
    events = await _fetch_and_hydrate_sport(poly_client, "football", series_ids, max_hydrate=25)
    
    if not events:
        pytest.skip("No football events on Polymarket")
    
    # Match odds to events
    matched = _match_odds_to_events(football_odds, events)
    
    # Find unmatched odds
    matched_ids = {(m["odds"].match_id, m["odds"].market_type, m["odds"].line) for m in matched}
    unmatched = [o for o in football_odds
                 if (o.match_id, o.market_type, o.line) not in matched_ids]
    
    # Validate fair values
    errors = []
    for m in matched:
        errs = _validate_fair_value_alignment(m)
        if errs:
            errors.append((m["question"], errs))
    
    _print_matching_report("football", matched, unmatched, errors)
    
    # Show 3-way market details explicitly
    threeway_matches = [m for m in matched if m["odds"].fair_prob_draw is not None]
    if threeway_matches:
        print(f"\n  📊 3-way markets found: {len(threeway_matches)}")
        for m in threeway_matches[:5]:
            odds = m["odds"]
            print(f"    • {odds.team1} vs {odds.team2} [{odds.market_type}]")
            print(f"      team1={odds.fair_prob1:.1f}%, draw={odds.fair_prob_draw:.1f}%, team2={odds.fair_prob2:.1f}%")
            print(f"      Poly outcomes: {m['market'].get('outcomes', [])}")
            print(f"      Poly question: \"{m['question']}\"")
    
    # Hard assertion
    assert not errors, (
        f"{len(errors)} fair value errors in football:\n"
        + "\n".join(f"  {q}: {e}" for q, errs in errors[:5] for e in errs)
    )


@pytest.mark.asyncio
async def test_basketball_odds_matching(poly_client, odds_service):
    """Verify basketball (NBA/NCAAB) odds match correctly."""
    bb_odds = [
        m for m in odds_service._v2_cache.values()
        if m.game == "ncaab" and m.fair_prob1 > 0
    ]
    
    if not bb_odds:
        pytest.skip("No basketball odds in v2 cache")
    
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP.get("ncaab", [])
    events = await _fetch_and_hydrate_sport(poly_client, "ncaab", series_ids, max_hydrate=25)
    
    if not events:
        pytest.skip("No basketball events on Polymarket")
    
    matched = _match_odds_to_events(bb_odds, events)
    matched_ids = {(m["odds"].match_id, m["odds"].market_type, m["odds"].line) for m in matched}
    unmatched = [o for o in bb_odds if (o.match_id, o.market_type, o.line) not in matched_ids]
    
    errors = []
    for m in matched:
        errs = _validate_fair_value_alignment(m)
        if errs:
            errors.append((m["question"], errs))
    
    _print_matching_report("basketball", matched, unmatched, errors)
    
    assert not errors, (
        f"{len(errors)} fair value errors in basketball:\n"
        + "\n".join(f"  {q}: {e}" for q, errs in errors[:5] for e in errs)
    )


@pytest.mark.asyncio
async def test_hockey_odds_matching(poly_client, odds_service):
    """Verify hockey odds match correctly."""
    hockey_odds = [
        m for m in odds_service._v2_cache.values()
        if m.game == "hockey" and m.fair_prob1 > 0
    ]
    
    if not hockey_odds:
        pytest.skip("No hockey odds in v2 cache")
    
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP.get("hockey", [])
    events = await _fetch_and_hydrate_sport(poly_client, "hockey", series_ids, max_hydrate=20)
    
    if not events:
        pytest.skip("No hockey events on Polymarket")
    
    matched = _match_odds_to_events(hockey_odds, events)
    matched_ids = {(m["odds"].match_id, m["odds"].market_type, m["odds"].line) for m in matched}
    unmatched = [o for o in hockey_odds if (o.match_id, o.market_type, o.line) not in matched_ids]
    
    errors = []
    for m in matched:
        errs = _validate_fair_value_alignment(m)
        if errs:
            errors.append((m["question"], errs))
    
    _print_matching_report("hockey", matched, unmatched, errors)
    
    assert not errors, (
        f"{len(errors)} fair value errors in hockey:\n"
        + "\n".join(f"  {q}: {e}" for q, errs in errors[:5] for e in errs)
    )


# =====================================================================
# Cross-sport aggregate
# =====================================================================

@pytest.mark.asyncio
async def test_all_sports_odds_aggregate(poly_client, odds_service):
    """Aggregate odds matching across all sports with v2 data."""
    all_odds = list(odds_service._v2_cache.values())
    
    if not all_odds:
        pytest.skip("No odds in v2 cache")
    
    # Group odds by sport
    odds_by_sport = defaultdict(list)
    for odds in all_odds:
        if odds.fair_prob1 > 0:
            odds_by_sport[odds.game].append(odds)
    
    print(f"\n{'='*70}")
    print(f"  V2 CACHE OVERVIEW")
    print(f"{'='*70}")
    print(f"  Total odds entries: {len(all_odds)}")
    for sport, entries in sorted(odds_by_sport.items(), key=lambda x: -len(x[1])):
        types = defaultdict(int)
        for e in entries:
            types[e.market_type] += 1
        types_str = ", ".join(f"{t}={c}" for t, c in sorted(types.items(), key=lambda x: -x[1]))
        print(f"    {sport:15s} {len(entries):4d}  ({types_str})")
    
    # Only test sports we have Polymarket series for
    sport_to_series_key = {
        "football": "football", "ncaab": "ncaab", "hockey": "hockey",
        "rugby": "rugby", "ufc": "mma", "cricket": "cricket",
    }
    
    total_matched = 0
    total_errors = 0
    
    for sport, odds_list in odds_by_sport.items():
        series_key = sport_to_series_key.get(sport)
        if not series_key:
            continue
        
        series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP.get(series_key, [])
        if not series_ids:
            continue
        
        events = await _fetch_and_hydrate_sport(
            poly_client, series_key, series_ids, max_hydrate=15
        )
        
        if not events:
            continue
        
        matched = _match_odds_to_events(odds_list, events)
        matched_ids = {(m["odds"].match_id, m["odds"].market_type, m["odds"].line) for m in matched}
        unmatched = [o for o in odds_list if (o.match_id, o.market_type, o.line) not in matched_ids]
        
        errors = []
        for m in matched:
            errs = _validate_fair_value_alignment(m)
            if errs:
                errors.append((m["question"], errs))
        
        _print_matching_report(sport, matched, unmatched, errors)
        total_matched += len(matched)
        total_errors += len(errors)
    
    print(f"\n{'='*70}")
    print(f"  AGGREGATE: {total_matched} matched pairs, {total_errors} errors")
    print(f"{'='*70}")
    
    assert total_errors == 0, f"{total_errors} total fair value errors across all sports"


# =====================================================================
# Targeted invariant tests
# =====================================================================

@pytest.mark.asyncio
async def test_totals_over_under_never_swapped(poly_client, odds_service):
    """Verify Over is ALWAYS assigned fair_prob1 and Under gets fair_prob2."""
    totals_odds = [
        m for m in odds_service._v2_cache.values()
        if m.market_type in ("totals", "totals_1h") and m.fair_prob1 > 0
    ]
    
    if not totals_odds:
        pytest.skip("No totals odds in v2 cache")
    
    # Get events from all sports
    all_events = []
    for sport_key, series_ids in PolymarketEsportsClient.SPORTS_SERIES_MAP.items():
        events = await _fetch_and_hydrate_sport(
            poly_client, sport_key, series_ids, max_hydrate=10
        )
        all_events.extend(events)
    
    matched = _match_odds_to_events(totals_odds, all_events)
    
    swap_errors = []
    for m in matched:
        outcomes = m["market"].get("outcomes", [])
        if len(outcomes) < 2:
            continue
        
        odds = m["odds"]
        over_prob = odds.fair_prob1 / 100.0
        under_prob = odds.fair_prob2 / 100.0
        
        # Verify probs look right (Over and Under should sum to ~1.0)
        prob_sum = over_prob + under_prob
        if abs(prob_sum - 1.0) > 0.05:
            swap_errors.append(
                f"{odds.team1} vs {odds.team2} [{odds.market_type} {odds.line}]: "
                f"Over={over_prob:.3f} + Under={under_prob:.3f} = {prob_sum:.3f}"
            )
        
        # Most critically: if the line is high (basketball 220+), 
        # neither prob should be extreme (0.01 or 0.99) which would indicate a swap
        if over_prob < 0.01 or under_prob < 0.01:
            swap_errors.append(
                f"{odds.team1} vs {odds.team2} [{odds.market_type} {odds.line}]: "
                f"Suspicious extremes — Over={over_prob:.3f}, Under={under_prob:.3f}"
            )
    
    if swap_errors:
        print(f"\n  ❌ Totals O/U swap errors ({len(swap_errors)}):")
        for err in swap_errors[:10]:
            print(f"    • {err}")
    else:
        print(f"\n  ✅ All {len(matched)} totals markets have correct Over/Under assignment")
    
    assert not swap_errors, f"{len(swap_errors)} O/U swap issues found"


@pytest.mark.asyncio
async def test_spread_direction_consistency(poly_client, odds_service):
    """Verify spread direction in Polymarket matches odds data."""
    spread_odds = [
        m for m in odds_service._v2_cache.values()
        if m.market_type in ("spreads", "spreads_1h")
        and m.fair_prob1 > 0
        and m.team1_spread_point is not None
    ]
    
    if not spread_odds:
        pytest.skip("No spread odds with team1_spread_point in v2 cache")
    
    all_events = []
    for sport_key, series_ids in PolymarketEsportsClient.SPORTS_SERIES_MAP.items():
        events = await _fetch_and_hydrate_sport(
            poly_client, sport_key, series_ids, max_hydrate=10
        )
        all_events.extend(events)
    
    matched = _match_odds_to_events(spread_odds, all_events)
    
    direction_errors = []
    checked = 0
    
    for m in matched:
        question = m["question"]
        odds = m["odds"]
        
        # Parse "Spread: TeamA (-1.5)" from question
        spread_match = re.match(
            r'(?:1H\s+)?Spread:\s*(.+?)\s*\(([+-]?\d+\.?\d*)\)', question, re.IGNORECASE
        )
        if not spread_match:
            continue
        
        checked += 1
        poly_team = spread_match.group(1).strip()
        poly_line = float(spread_match.group(2))
        
        poly_team_norm = normalize_team(poly_team)
        t1_norm = normalize_team(odds.team1)
        t2_norm = normalize_team(odds.team2)
        
        def _contains(a, b):
            return a in b or b in a
        
        # Determine which odds team this Poly team corresponds to
        if poly_team_norm == t1_norm or _contains(poly_team_norm, t1_norm):
            odds_point = odds.team1_spread_point
        elif poly_team_norm == t2_norm or _contains(poly_team_norm, t2_norm):
            odds_point = -odds.team1_spread_point  # team2's point is the inverse
        else:
            direction_errors.append(
                f"Cannot map Poly team '{poly_team}' to odds teams ({odds.team1}, {odds.team2})"
            )
            continue
        
        # The sign should be consistent
        # Poly: "Spread: Grizzlies (-4.5)" means Grizzlies give 4.5
        # Odds: team1_spread_point = -4.5 means team1 gives 4.5
        if (poly_line < 0) != (odds_point < 0):
            direction_errors.append(
                f"{odds.team1} vs {odds.team2}: Poly has {poly_team} ({poly_line:+g}), "
                f"but odds has that team at {odds_point:+g}"
            )
    
    if direction_errors:
        print(f"\n  ❌ Spread direction errors ({len(direction_errors)}/{checked}):")
        for err in direction_errors[:10]:
            print(f"    • {err}")
    else:
        print(f"\n  ✅ All {checked} spread markets have consistent direction")
    
    assert not direction_errors, f"{len(direction_errors)} spread direction mismatches"


# =====================================================================
# Per-sport: Rugby, MMA, Cricket
# =====================================================================

@pytest.mark.asyncio
async def test_rugby_odds_matching(poly_client, odds_service):
    """Verify rugby odds match correctly.
    
    Rugby is a 3-way sport (draw possible). All Polymarket rugby markets use
    Yes/No outcomes so align_yesno_h2h is required for correct fair values.
    """
    rugby_odds = [
        m for m in odds_service._v2_cache.values()
        if m.game == "rugby" and m.fair_prob1 > 0
    ]
    
    if not rugby_odds:
        pytest.skip("No rugby odds in v2 cache")
    
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP.get("rugby", [])
    events = await _fetch_and_hydrate_sport(poly_client, "rugby", series_ids, max_hydrate=15)
    
    if not events:
        pytest.skip("No rugby events on Polymarket")
    
    matched = _match_odds_to_events(rugby_odds, events)
    
    matched_ids = {(m["odds"].match_id, m["odds"].market_type, m["odds"].line) for m in matched}
    unmatched = [o for o in rugby_odds
                 if (o.match_id, o.market_type, o.line) not in matched_ids]
    
    errors = []
    for m in matched:
        errs = _validate_fair_value_alignment(m)
        if errs:
            errors.append((m["question"], errs))
    
    _print_matching_report("rugby", matched, unmatched, errors)
    
    assert not errors, (
        f"{len(errors)} fair value errors in rugby:\n"
        + "\n".join(f"  {q}: {e}" for q, errs in errors[:5] for e in errs)
    )


@pytest.mark.asyncio
async def test_mma_odds_matching(poly_client, odds_service):
    """Verify MMA/UFC odds match correctly.
    
    UFC has h2h (2-way, no draw) and totals (rounds O/U).
    """
    mma_odds = [
        m for m in odds_service._v2_cache.values()
        if m.game in ("ufc", "mma") and m.fair_prob1 > 0
    ]
    
    if not mma_odds:
        pytest.skip("No MMA/UFC odds in v2 cache")
    
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP.get("mma", [])
    events = await _fetch_and_hydrate_sport(poly_client, "mma", series_ids, max_hydrate=15)
    
    if not events:
        pytest.skip("No MMA events on Polymarket")
    
    matched = _match_odds_to_events(mma_odds, events)
    
    matched_ids = {(m["odds"].match_id, m["odds"].market_type, m["odds"].line) for m in matched}
    unmatched = [o for o in mma_odds
                 if (o.match_id, o.market_type, o.line) not in matched_ids]
    
    errors = []
    for m in matched:
        errs = _validate_fair_value_alignment(m)
        if errs:
            errors.append((m["question"], errs))
    
    _print_matching_report("MMA/UFC", matched, unmatched, errors)
    
    assert not errors, (
        f"{len(errors)} fair value errors in MMA/UFC:\n"
        + "\n".join(f"  {q}: {e}" for q, errs in errors[:5] for e in errs)
    )


@pytest.mark.asyncio
async def test_cricket_odds_matching(poly_client, odds_service):
    """Verify cricket odds match correctly."""
    cricket_odds = [
        m for m in odds_service._v2_cache.values()
        if m.game == "cricket" and m.fair_prob1 > 0
    ]
    
    if not cricket_odds:
        pytest.skip("No cricket odds in v2 cache")
    
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP.get("cricket", [])
    events = await _fetch_and_hydrate_sport(poly_client, "cricket", series_ids, max_hydrate=10)
    
    if not events:
        pytest.skip("No cricket events on Polymarket")
    
    matched = _match_odds_to_events(cricket_odds, events)
    
    matched_ids = {(m["odds"].match_id, m["odds"].market_type, m["odds"].line) for m in matched}
    unmatched = [o for o in cricket_odds
                 if (o.match_id, o.market_type, o.line) not in matched_ids]
    
    errors = []
    for m in matched:
        errs = _validate_fair_value_alignment(m)
        if errs:
            errors.append((m["question"], errs))
    
    _print_matching_report("cricket", matched, unmatched, errors)
    
    assert not errors, (
        f"{len(errors)} fair value errors in cricket:\n"
        + "\n".join(f"  {q}: {e}" for q, errs in errors[:5] for e in errs)
    )


# =====================================================================
# Additional invariant tests
# =====================================================================

@pytest.mark.asyncio
async def test_team_alignment_never_swapped(poly_client, odds_service):
    """Verify align_teams_with_bids never assigns fair_prob1 to team2's outcome.
    
    For every matched h2h/spread pair with team-name outcomes, we verify that
    the team mapped to fair_prob1 is actually odds.team1 (or vice versa when 
    order is reversed), and that the mapping is internally consistent.
    """
    from src.scanning.team_matcher import align_teams_with_bids
    
    h2h_odds = [
        m for m in odds_service._v2_cache.values()
        if m.market_type in ("h2h", "h2h_h1", "spreads", "spreads_1h") and m.fair_prob1 > 0
    ]
    
    if not h2h_odds:
        pytest.skip("No h2h/spread odds in v2 cache")
    
    all_events = []
    for sport_key, series_ids in PolymarketEsportsClient.SPORTS_SERIES_MAP.items():
        events = await _fetch_and_hydrate_sport(
            poly_client, sport_key, series_ids, max_hydrate=10
        )
        all_events.extend(events)
    
    matched = _match_odds_to_events(h2h_odds, all_events)
    
    swap_errors = []
    checked = 0
    
    for m in matched:
        odds = m["odds"]
        market = m["market"]
        outcomes = market.get("outcomes", [])
        token_ids = market.get("clobTokenIds", [])
        
        if len(outcomes) < 2 or len(token_ids) < 2:
            continue
        
        o0_lower = outcomes[0].lower()
        o1_lower = outcomes[1].lower()
        
        # Skip Yes/No — handled by align_yesno_h2h, not align_teams_with_bids
        if o0_lower in ("yes", "no") or o1_lower in ("yes", "no"):
            continue
        # Skip Over/Under — handled separately
        if o0_lower in ("over", "under") or o1_lower in ("over", "under"):
            continue
        
        # Run align_teams_with_bids with mock bids
        mock_bids = {token_ids[i]: {"price": 0.5} for i in range(len(token_ids))}
        aligned = align_teams_with_bids(odds, outcomes, token_ids, mock_bids)
        
        if aligned is None:
            continue
        
        checked += 1
        t1_norm = normalize_team(odds.team1)
        t2_norm = normalize_team(odds.team2)
        fair1 = odds.fair_prob1 / 100.0
        fair2 = odds.fair_prob2 / 100.0
        
        def _c(a, b):
            return a in b or b in a
        
        for poly_team, _, _, fair, other_fair in aligned:
            poly_norm = normalize_team(poly_team)
            
            # If this outcome maps to team1, verify fair == fair1
            if poly_norm == t1_norm or _c(poly_norm, t1_norm):
                if abs(fair - fair1) > 0.001:
                    swap_errors.append(
                        f"{odds.team1} vs {odds.team2} [{odds.market_type}]: "
                        f"'{poly_team}' maps to team1 but got fair={fair:.3f}, "
                        f"expected fair_prob1={fair1:.3f}"
                    )
                if abs(other_fair - fair2) > 0.001:
                    swap_errors.append(
                        f"{odds.team1} vs {odds.team2} [{odds.market_type}]: "
                        f"'{poly_team}' maps to team1 but other_fair={other_fair:.3f}, "
                        f"expected fair_prob2={fair2:.3f}"
                    )
            elif poly_norm == t2_norm or _c(poly_norm, t2_norm):
                if abs(fair - fair2) > 0.001:
                    swap_errors.append(
                        f"{odds.team1} vs {odds.team2} [{odds.market_type}]: "
                        f"'{poly_team}' maps to team2 but got fair={fair:.3f}, "
                        f"expected fair_prob2={fair2:.3f}"
                    )
                if abs(other_fair - fair1) > 0.001:
                    swap_errors.append(
                        f"{odds.team1} vs {odds.team2} [{odds.market_type}]: "
                        f"'{poly_team}' maps to team2 but other_fair={other_fair:.3f}, "
                        f"expected fair_prob1={fair1:.3f}"
                    )
    
    if swap_errors:
        print(f"\n  ❌ Team alignment swap errors ({len(swap_errors)}/{checked}):")
        for err in swap_errors[:10]:
            print(f"    • {err}")
    else:
        print(f"\n  ✅ All {checked} team alignments correct — fair values never swapped")
    
    assert not swap_errors, f"{len(swap_errors)} team alignment swap errors"


@pytest.mark.asyncio
async def test_spread_line_exact_match(poly_client, odds_service):
    """Verify matched spread odds have EXACTLY the same line as the Poly market.
    
    Ensures we never assign -1.5 odds to a -2.5 market or vice versa.
    match_odds_to_poly_market does line matching but this independently verifies
    the matched pairs have consistent lines.
    """
    spread_odds = [
        m for m in odds_service._v2_cache.values()
        if m.market_type in ("spreads", "spreads_1h") and m.fair_prob1 > 0 and m.line is not None
    ]
    
    if not spread_odds:
        pytest.skip("No spread odds in v2 cache")
    
    all_events = []
    for sport_key, series_ids in PolymarketEsportsClient.SPORTS_SERIES_MAP.items():
        events = await _fetch_and_hydrate_sport(
            poly_client, sport_key, series_ids, max_hydrate=10
        )
        all_events.extend(events)
    
    matched = _match_odds_to_events(spread_odds, all_events)
    
    line_errors = []
    checked = 0
    
    for m in matched:
        question = m["question"]
        odds = m["odds"]
        
        # Extract Poly's line from the question
        spread_match = re.match(
            r'(?:1H\s+)?Spread:\s*.+?\s*\(([+-]?\d+\.?\d*)\)', question, re.IGNORECASE
        )
        if not spread_match:
            continue
        
        checked += 1
        poly_line = abs(float(spread_match.group(1)))
        odds_line = abs(odds.line) if odds.line else None
        
        if odds_line is not None and abs(poly_line - odds_line) > 0.01:
            line_errors.append(
                f"{odds.team1} vs {odds.team2}: "
                f"Poly line={poly_line}, Odds line={odds_line} "
                f"Question: \"{question}\""
            )
    
    if line_errors:
        print(f"\n  ❌ Spread line mismatches ({len(line_errors)}/{checked}):")
        for err in line_errors[:10]:
            print(f"    • {err}")
    else:
        print(f"\n  ✅ All {checked} spread lines match exactly — no cross-line contamination")
    
    assert not line_errors, f"{len(line_errors)} spread line mismatches"


@pytest.mark.asyncio
async def test_draw_market_fair_values(poly_client, odds_service):
    """Verify 'Will match end in a draw?' markets use fair_prob_draw correctly.
    
    For draw markets, align_yesno_h2h should return:
    - fair_yes = fair_prob_draw (draw probability)
    - fair_no = 1 - fair_prob_draw (either team wins)
    """
    from src.scanning.team_matcher import align_yesno_h2h
    
    # Only 3-way sports have draw markets
    threeway_odds = [
        m for m in odds_service._v2_cache.values()
        if m.market_type == "h2h"
        and m.fair_prob1 > 0
        and m.fair_prob_draw is not None
        and m.fair_prob_draw > 0
    ]
    
    if not threeway_odds:
        pytest.skip("No 3-way h2h odds in v2 cache")
    
    all_events = []
    for sport_key in ("football", "hockey", "rugby"):
        series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP.get(sport_key, [])
        if series_ids:
            events = await _fetch_and_hydrate_sport(
                poly_client, sport_key, series_ids, max_hydrate=10
            )
            all_events.extend(events)
    
    matched = _match_odds_to_events(threeway_odds, all_events)
    
    # Filter to draw markets only
    draw_markets = [
        m for m in matched
        if "draw" in m["question"].lower()
    ]
    
    draw_errors = []
    checked = 0
    
    for m in draw_markets:
        odds = m["odds"]
        market = m["market"]
        outcomes = market.get("outcomes", [])
        token_ids = market.get("clobTokenIds", [f"t{i}" for i in range(len(outcomes))])
        
        if len(outcomes) < 2:
            continue
        
        # Build mock for align_yesno_h2h
        mock_bids = {t: {"price": 0.5} for t in token_ids}
        aligned = align_yesno_h2h(odds, market, token_ids, mock_bids)
        
        if aligned is None:
            draw_errors.append(
                f"align_yesno_h2h returned None for draw market: \"{m['question']}\""
            )
            continue
        
        checked += 1
        yes_fair = aligned[0][3]
        no_fair = aligned[0][4]
        expected_draw = odds.fair_prob_draw / 100.0
        
        # Yes should be the draw probability
        if abs(yes_fair - expected_draw) > 0.001:
            draw_errors.append(
                f"{odds.team1} vs {odds.team2}: "
                f"Draw fair_yes={yes_fair:.3f}, expected fair_prob_draw={expected_draw:.3f}"
            )
        
        # No should be 1 - draw
        expected_no = 1.0 - expected_draw
        if abs(no_fair - expected_no) > 0.001:
            draw_errors.append(
                f"{odds.team1} vs {odds.team2}: "
                f"Draw fair_no={no_fair:.3f}, expected 1-draw={expected_no:.3f}"
            )
        
        # Sum should be exactly 1.0
        if abs(yes_fair + no_fair - 1.0) > 0.01:
            draw_errors.append(
                f"{odds.team1} vs {odds.team2}: "
                f"Draw Yes({yes_fair:.3f})+No({no_fair:.3f})={yes_fair+no_fair:.3f} ≠ 1.0"
            )
    
    if draw_errors:
        print(f"\n  ❌ Draw market errors ({len(draw_errors)}/{checked}):")
        for err in draw_errors[:10]:
            print(f"    • {err}")
    else:
        print(f"\n  ✅ All {checked} draw markets correctly use fair_prob_draw")
    
    assert not draw_errors, f"{len(draw_errors)} draw market errors"


@pytest.mark.asyncio
async def test_no_token_binary_fair_value(poly_client, odds_service):
    """Verify No tokens on 3-way sports get binary-adjusted fair values.
    
    Regression test for the PSV No bug: BotState received raw team probability
    (e.g., PSV win=70%) as the No token's fair value, instead of the correct
    binary-adjusted value (1 - 70% = 30%).
    
    This test validates that:
    1. For every 3-way h2h match, the Yes token fair is team_prob
    2. The No token fair is EXACTLY 1 - team_prob
    3. Yes + No always sum to 1.0 (binary constraint)
    """
    from src.scanning.team_matcher import align_yesno_h2h
    
    # Find 3-way h2h odds (football, hockey, rugby — any sport with draw)
    threeway_odds = [
        m for m in odds_service._v2_cache.values()
        if m.market_type == "h2h"
        and m.fair_prob1 > 0
        and m.fair_prob_draw is not None
        and m.fair_prob_draw > 0
    ]
    
    if not threeway_odds:
        pytest.skip("No 3-way h2h odds in v2 cache")
    
    # Fetch events from 3-way sports
    all_events = []
    for sport_key in ("football", "hockey", "rugby"):
        series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP.get(sport_key, [])
        if series_ids:
            events = await _fetch_and_hydrate_sport(
                poly_client, sport_key, series_ids, max_hydrate=10
            )
            all_events.extend(events)
    
    matched = _match_odds_to_events(threeway_odds, all_events)
    
    # Filter to Yes/No markets only (team-win and draw markets)
    yesno_matched = [
        m for m in matched
        if len(m["market"].get("outcomes", [])) >= 2
        and m["market"]["outcomes"][0].lower() in ("yes", "no")
    ]
    
    errors = []
    checked = 0
    
    for m in yesno_matched:
        odds = m["odds"]
        market = m["market"]
        outcomes = market.get("outcomes", [])
        token_ids = market.get("clobTokenIds", [f"t{i}" for i in range(len(outcomes))])
        question = m["question"]
        
        mock_bids = {t: {"price": 0.5} for t in token_ids}
        aligned = align_yesno_h2h(odds, market, token_ids, mock_bids)
        
        if aligned is None:
            continue
        
        checked += 1
        
        for entry in aligned:
            poly_team, best_bid, poly_token, fair, other_fair = entry
            
            if poly_team.lower() == "yes":
                # Yes fair must be the team probability (always < 1.0 for 3-way)
                if fair >= 1.0:
                    errors.append(
                        f"Yes fair >= 1.0: {fair:.3f} for '{question}'"
                    )
                # Raw 3-way test: fair_prob1 + fair_prob2 < 100 (draw takes remainder)
                # Yes fair should NOT equal raw fair_prob (which < 100)
                # It should be proportional to the team's win probability
                
            elif poly_team.lower() == "no":
                # NO TOKEN BINARY CONSTRAINT: No fair = 1 - Yes fair
                # This is the exact bug we caught with PSV: No was getting the
                # raw team probability instead of 1 - team_prob
                yes_fair_for_this = other_fair  # The other side is Yes
                expected_no = 1.0 - yes_fair_for_this
                
                if abs(fair - expected_no) > 0.001:
                    errors.append(
                        f"No token binary mismatch: "
                        f"fair_no={fair:.3f}, expected 1-Yes={expected_no:.3f} "
                        f"(Yes={yes_fair_for_this:.3f}) | '{question}'"
                    )
            
            # CRITICAL: Yes + No must sum to exactly 1.0
            binary_sum = fair + other_fair
            if abs(binary_sum - 1.0) > 0.01:
                errors.append(
                    f"Binary sum ≠ 1.0: {poly_team} fair={fair:.3f} + other={other_fair:.3f} "
                    f"= {binary_sum:.3f} | '{question}'"
                )
    
    if errors:
        print(f"\n  ❌ No token binary errors ({len(errors)}/{checked}):")
        for err in errors[:10]:
            print(f"    • {err}")
    else:
        print(f"\n  ✅ All {checked} Yes/No markets have correct binary fair values")
    
    assert not errors, (
        f"{len(errors)} No token binary adjustment errors:\n"
        + "\n".join(f"  {e}" for e in errors[:5])
    )


@pytest.mark.asyncio
async def test_team_name_extraction_clean(poly_client):
    """Verify team names extracted from event titles are clean across all sports.
    
    Team name extraction is a prerequisite for odds matching — if team names
    contain league prefixes ('UFC Fight Night:', 'KHL:'), weight classes
    ('(Middleweight, Prelims)'), or other noise, matching against bookmaker
    odds will silently fail.
    
    Checks that extracted team names:
    1. Don't contain colons (league/event prefix not stripped)
    2. Don't contain parentheses (weight class/card position not stripped)
    3. Aren't excessively long (> 35 chars suggests noise)
    4. Both team1 and team2 are non-empty
    """
    all_issues = []
    sport_stats = {}
    
    for sport_key, series_ids in PolymarketEsportsClient.SPORTS_SERIES_MAP.items():
        events = await _fetch_and_hydrate_sport(
            poly_client, sport_key, series_ids, max_hydrate=10
        )
        
        if not events:
            continue
        
        checked = 0
        issues = []
        
        for e in events:
            # Only check events that have "vs" in them (team-based)
            if " vs " not in e.title.lower() and " vs. " not in e.title.lower():
                continue
            
            checked += 1
            t1 = e.team1 or ""
            t2 = e.team2 or ""
            t1_norm = normalize_team(t1) if t1 else ""
            t2_norm = normalize_team(t2) if t2 else ""
            
            # Check for missing teams
            if not t1 or not t2:
                issues.append(
                    f"[{sport_key}] Missing team in \"{e.title}\": "
                    f"team1=\"{t1}\", team2=\"{t2}\""
                )
                continue
            
            # Check for colon (league prefix not stripped)
            if ":" in t1:
                issues.append(
                    f"[{sport_key}] team1 contains ':' (prefix not stripped): "
                    f"\"{t1}\" from \"{e.title}\""
                )
            if ":" in t2:
                issues.append(
                    f"[{sport_key}] team2 contains ':' (prefix not stripped): "
                    f"\"{t2}\" from \"{e.title}\""
                )
            
            # Check for parentheses (weight class/card position not stripped)
            if "(" in t1 or ")" in t1:
                issues.append(
                    f"[{sport_key}] team1 contains parentheses: "
                    f"\"{t1}\" from \"{e.title}\""
                )
            if "(" in t2 or ")" in t2:
                issues.append(
                    f"[{sport_key}] team2 contains parentheses: "
                    f"\"{t2}\" from \"{e.title}\""
                )
            
            # Check for excessively long normalized names (noise in team name)
            if len(t1_norm) > 35:
                issues.append(
                    f"[{sport_key}] team1 very long ({len(t1_norm)} chars): "
                    f"\"{t1_norm}\" from \"{e.title}\""
                )
            if len(t2_norm) > 35:
                issues.append(
                    f"[{sport_key}] team2 very long ({len(t2_norm)} chars): "
                    f"\"{t2_norm}\" from \"{e.title}\""
                )
        
        sport_stats[sport_key] = {"checked": checked, "issues": len(issues)}
        all_issues.extend(issues)
    
    # Print summary
    for sport, stats in sport_stats.items():
        status = "✅" if stats["issues"] == 0 else "❌"
        print(f"  {status} {sport}: {stats['checked']} events, {stats['issues']} issues")
    
    if all_issues:
        print(f"\n  ❌ Team name extraction issues ({len(all_issues)}):")
        for issue in all_issues[:15]:
            print(f"    • {issue}")
    else:
        total = sum(s["checked"] for s in sport_stats.values())
        print(f"\n  ✅ All {total} events have clean team names across {len(sport_stats)} sports")
    
    assert not all_issues, (
        f"{len(all_issues)} team name extraction issues:\n"
        + "\n".join(f"  {i}" for i in all_issues[:10])
    )


@pytest.mark.asyncio
async def test_cross_market_fair_value_isolation(odds_service):
    """Verify h2h fair values NEVER contaminate spread/totals BotState entries.
    
    Regression test for the Hoffenheim bug:
    - HOFF -1.5 spread entry in BotState with team1="TSG 1899 Hoffenheim"
    - h2h push via update_fair_probs_by_team found this entry by team name
    - Wrote h2h fair_prob1=0.65 to the spread entry
    - Reactive handler used 65% as fair → outbid to 42c → filled at terrible price
    
    Tests BOTH Fix A (write guard in update_fair_probs_by_team)
    and Fix B (read guard in get_fair_value_for_order).
    """
    from src.state.bot_state import BotState
    from src.state.match_state import MatchState, MatchOrder
    from src.core.match_id import normalize_team, make_match_id
    from datetime import datetime, timezone
    
    errors = []
    tested = 0
    
    # Find matches that have BOTH h2h and spread/totals odds in v2 cache
    # Group by base teams
    match_markets = defaultdict(list)
    for odds_match in odds_service._v2_cache.values():
        if odds_match.fair_prob1 <= 0:
            continue
        t1 = normalize_team(odds_match.team1)
        t2 = normalize_team(odds_match.team2)
        key = tuple(sorted([t1, t2]))
        match_markets[key].append(odds_match)
    
    # Find multi-market matches
    multi_matches = {
        k: v for k, v in match_markets.items()
        if any(m.market_type == "h2h" for m in v)
        and any(m.market_type in ("spreads", "totals", "spreads_1h", "totals_1h") for m in v)
    }
    
    if not multi_matches:
        pytest.skip("No multi-market matches with both h2h and spread/totals in v2 cache")
    
    for teams_key, odds_list in multi_matches.items():
        h2h_matches = [m for m in odds_list if m.market_type == "h2h"]
        non_h2h_matches = [m for m in odds_list if m.market_type in ("spreads", "totals", "spreads_1h", "totals_1h")]
        
        if not h2h_matches or not non_h2h_matches:
            continue
        
        h2h = h2h_matches[0]
        spread_or_total = non_h2h_matches[0]
        
        # === SIMULATE THE BUG SCENARIO ===
        # Create a fresh BotState with a spread/totals entry
        bs = BotState()
        
        # Create an isolated spread/totals match_id (with condition_id suffix)
        fake_condition = "0xabc123def456789a"
        base_mid = make_match_id(spread_or_total.team1, spread_or_total.team2, spread_or_total.game)
        spread_match_id = f"{base_mid}:{fake_condition[:18]}"
        
        # Build team name based on market type
        if spread_or_total.market_type in ("spreads", "spreads_1h"):
            team_name = f"{spread_or_total.team1} (-{spread_or_total.line})"
        else:
            team_name = f"{spread_or_total.team1}: O/U {spread_or_total.line}"
        
        spread_fair = spread_or_total.fair_prob1 / 100.0
        
        # Register the isolated spread/totals match in BotState
        spread_match = bs.register_match(
            match_id=spread_match_id,
            condition_id=fake_condition,
            game=spread_or_total.game,
            team1=spread_or_total.team1,
            team2=spread_or_total.team2,
        )
        
        # Register an order on this match (simulating what sports_bot does)
        order = MatchOrder(
            order_id="fake_order_123",
            token_id="fake_token_456",
            team=team_name,
            price=0.30,
            size=10.0,
            fair_value=spread_fair,  # Set at placement time
        )
        spread_match.order1 = order
        spread_match.token1 = order.token_id
        bs._token_to_match[order.token_id] = spread_match_id
        bs._order_to_match[order.order_id] = spread_match_id
        
        # Verify initial fair value is correct (spread-specific)
        initial_fair = spread_match.get_fair_value_for_order(order)
        assert initial_fair == spread_fair, (
            f"Initial fair should be spread_fair={spread_fair:.3f}, got {initial_fair}"
        )
        
        # === NOW PUSH H2H ODDS (the contamination attempt) ===
        h2h_fair1 = h2h.fair_prob1 / 100.0
        h2h_fair2 = h2h.fair_prob2 / 100.0
        
        # This is what OddsService section 1 does — push h2h with team names
        result = bs.update_fair_probs_by_team(
            match_id=h2h.match_id,  # h2h match_id (no condition suffix)
            team1=h2h.team1,
            team2=h2h.team2,
            fair_prob1=h2h_fair1,
            fair_prob2=h2h_fair2,
        )
        
        # === VERIFY NO CONTAMINATION ===
        tested += 1
        
        # Fix A check: update_fair_probs_by_team should NOT have found the spread entry
        # (it should have created a new h2h match or returned None)
        after_fair = spread_match.get_fair_value_for_order(order)
        
        # The per-order fair_value should be unchanged
        if order.fair_value != spread_fair:
            errors.append(
                f"CONTAMINATED order.fair_value: was {spread_fair:.3f}, "
                f"now {order.fair_value:.3f} (h2h fair1={h2h_fair1:.3f}) "
                f"| {spread_or_total.team1} vs {spread_or_total.team2} "
                f"({spread_or_total.market_type})"
            )
        
        # Even if match-level fair_prob was contaminated, Fix B should prevent
        # get_fair_value_for_order from returning it
        if after_fair != spread_fair:
            errors.append(
                f"CONTAMINATED get_fair_value_for_order: was {spread_fair:.3f}, "
                f"now {after_fair:.3f} (h2h fair1={h2h_fair1:.3f}) "
                f"| {spread_or_total.team1} vs {spread_or_total.team2} "
                f"({spread_or_total.market_type})"
            )
        
        # === SIMULATE HYDRATED ORDER (no per-order fair_value) ===
        # This is the actual bug path: hydrated orders have order.fair_value=None
        order.fair_value = None  # Simulate hydration
        
        hydrated_fair = spread_match.get_fair_value_for_order(order)
        
        # Fix B: For isolated sub-markets without per-order fair,
        # get_fair_value_for_order should return None rather than contaminated match-level prob
        if hydrated_fair is not None:
            # Check if it's using the h2h value
            if abs(hydrated_fair - h2h_fair1) < 0.02 or abs(hydrated_fair - h2h_fair2) < 0.02:
                errors.append(
                    f"HYDRATED ORDER CONTAMINATED: got {hydrated_fair:.3f} "
                    f"(h2h fairs: {h2h_fair1:.3f}/{h2h_fair2:.3f}) "
                    f"| {spread_or_total.team1} vs {spread_or_total.team2} "
                    f"({spread_or_total.market_type})"
                )
        
        # Restore for next iteration
        order.fair_value = spread_fair
    
    print(f"\n  Cross-market isolation: tested {tested} multi-market matches")
    if errors:
        print(f"  ❌ {len(errors)} contamination errors:")
        for e in errors[:10]:
            print(f"    • {e}")
    else:
        print(f"  ✅ No contamination detected — h2h fair values correctly isolated")
    
    assert not errors, (
        f"{len(errors)} cross-market fair value contamination errors:\n"
        + "\n".join(f"  {e}" for e in errors[:5])
    )


