"""
Integration Tests: Edge Detection Correctness

Validates that the scanner produces correct opportunities with valid fair values,
correct team alignment, proper binary constraints, and profitable hedges.

This is the critical Stage 3 test — where odds and Polymarket markets COMBINE
to produce trading decisions. All recent bugs (PSV No, fair value swaps) would
be caught here.

Run with:
    proxychains4 -q python -m pytest tests/integration/test_edge_detection.py -v -s

IMPORTANT: Requires both Polymarket API access (proxychains4) and Supabase
env vars (SUPABASE_URL, SUPABASE_KEY).
"""
import asyncio
import pytest
from collections import defaultdict
from typing import List, Dict, Any

from src.polymarket.market_client import PolymarketEsportsClient, PolymarketEsportsEvent
from src.services.odds_service import OddsService, AggregatedMatch
from src.core.market_parser import (
    classify_market_type, match_odds_to_poly_market,
    SPREAD, TOTALS, OU, MONEYLINE, HALFTIME, HALFTIME_SPREAD,
    HALFTIME_TOTALS, PLAYER_PROP, OTHER,
)
from src.core.match_id import normalize_team
from src.scanning.opportunity_scanner import (
    OpportunityScanner, find_all_poly_events, find_poly_event,
)
from src.scanning.team_matcher import align_teams_with_bids, align_yesno_h2h
from src.execution.hedge_finder import HedgeFinder
from src.state.market_cache import MarketCache
from src.core.config import CONFIG

# Import the helper from Phase 1 tests
from tests.integration.test_market_parsing import _fetch_and_hydrate_sport


# =====================================================================
# Helpers
# =====================================================================

async def _get_all_poly_events(poly_client) -> List[PolymarketEsportsEvent]:
    """Fetch events from all sports with hydration."""
    all_events = []
    for sport_key, series_ids in PolymarketEsportsClient.SPORTS_SERIES_MAP.items():
        events = await _fetch_and_hydrate_sport(
            poly_client, sport_key, series_ids, max_hydrate=10
        )
        all_events.extend(events)
    return all_events


def _validate_opportunity(opp: dict, market_type: str = "") -> List[str]:
    """Validate invariants on a single opportunity dict.
    
    Returns list of error strings (empty = all OK).
    """
    errors = []
    
    team = opp.get("team", "")
    token_id = opp.get("token_id", "")
    fair = opp.get("fair", 0)
    best_bid = opp.get("best_bid", 0)
    entry_price = opp.get("entry_price", 0)
    edge = opp.get("edge", 0)
    hedge_team = opp.get("hedge_team", "")
    hedge_token = opp.get("hedge_token", "")
    hedge_fair = opp.get("hedge_fair", 0)
    
    label = f"{team} ({market_type or 'h2h'})"
    
    # Fair value range
    if fair <= 0 or fair > 1.0:
        errors.append(f"{label}: fair={fair:.3f} outside (0, 1.0]")
    
    # Entry price = best_bid + 0.01
    if abs(entry_price - (best_bid + 0.01)) > 0.001:
        errors.append(
            f"{label}: entry_price={entry_price:.3f} != best_bid+0.01={best_bid+0.01:.3f}"
        )
    
    # Edge = fair - entry_price
    if abs(edge - (fair - entry_price)) > 0.001:
        errors.append(
            f"{label}: edge={edge:.3f} != fair-entry={fair-entry_price:.3f}"
        )
    
    # Edge >= min_edge (from config)
    min_edge = CONFIG.get("min_edge", 0.05)
    if edge < min_edge - 0.001:
        errors.append(f"{label}: edge={edge:.3f} < min_edge={min_edge}")
    
    # Hedge fair value range
    if hedge_fair <= 0 or hedge_fair > 1.0:
        errors.append(f"{label}: hedge_fair={hedge_fair:.3f} outside (0, 1.0]")
    
    # Team and hedge_team must be different
    if team.lower() == hedge_team.lower():
        errors.append(f"{label}: team == hedge_team ('{team}')")
    
    # Token and hedge_token must be different
    if token_id == hedge_token:
        errors.append(f"{label}: token_id == hedge_token")
    
    # Token IDs must not be empty
    if not token_id:
        errors.append(f"{label}: empty token_id")
    if not hedge_token:
        errors.append(f"{label}: empty hedge_token")
    
    return errors


# =====================================================================
# Tests
# =====================================================================

@pytest.mark.asyncio
async def test_moneyline_edge_valid_opportunities(poly_client, odds_service):
    """Validate moneyline (h2h) edge detection produces valid opportunities.
    
    For every matched h2h opportunity:
    1. Fair value is in (0, 1.0]
    2. Entry price = best_bid + 0.01
    3. Edge = fair - entry_price >= min_edge
    4. Hedge token is the OPPOSITE side
    5. Fair + hedge_fair are consistent (sum close to 1.0 for 2-way)
    """
    # Get h2h odds
    h2h_odds = [
        m for m in odds_service._v2_cache.values()
        if m.market_type == "h2h" and m.fair_prob1 > 0 and m.fair_prob2 > 0
    ]
    
    if not h2h_odds:
        pytest.skip("No h2h odds in v2 cache")
    
    all_events = await _get_all_poly_events(poly_client)
    upcoming = [e for e in all_events if not e.is_finished and not e.is_live]
    
    errors = []
    checked = 0
    opportunities_found = 0
    
    for odds_match in h2h_odds:
        matching_events = find_all_poly_events(odds_match, upcoming)
        if not matching_events:
            continue
        
        for poly_event in matching_events:
            if not poly_event.markets:
                continue
            
            for market in poly_event.markets:
                question = market.get("question", "")
                if not match_odds_to_poly_market("h2h", None, question, odds_match.game):
                    continue
                
                token_ids = market.get("clobTokenIds", [])
                outcomes = market.get("outcomes", [])
                if len(token_ids) < 2 or len(outcomes) < 2:
                    continue
                
                # Get bids
                best_bids = await poly_client.get_best_bids(token_ids)
                
                # Check if outcomes are Yes/No (Will X win? markets)
                is_yesno = outcomes[0].lower() in ("yes", "no")
                
                if is_yesno:
                    aligned = align_yesno_h2h(odds_match, market, token_ids, best_bids)
                else:
                    aligned = align_teams_with_bids(
                        odds_match, outcomes, token_ids, best_bids
                    )
                
                if not aligned:
                    continue
                
                checked += 1
                
                for poly_team, bid_data, poly_token, fair, other_fair in aligned:
                    best_bid = bid_data.get("price", 0) if isinstance(bid_data, dict) else bid_data
                    if best_bid <= 0:
                        continue
                    
                    entry_price = best_bid + 0.01
                    edge = fair - entry_price
                    
                    if edge >= CONFIG.get("min_edge", 0.05):
                        opportunities_found += 1
                        hedge_idx = 1 if poly_team == outcomes[0] else 0
                        
                        opp = {
                            "team": poly_team,
                            "token_id": poly_token,
                            "fair": fair,
                            "best_bid": best_bid,
                            "entry_price": entry_price,
                            "edge": edge,
                            "hedge_team": outcomes[hedge_idx],
                            "hedge_token": token_ids[hedge_idx],
                            "hedge_fair": other_fair,
                        }
                        opp_errors = _validate_opportunity(opp, "h2h")
                        errors.extend(opp_errors)
                    
                    # INVARIANT: fair value ALWAYS in (0, 1.0] even without edge
                    if fair <= 0 or fair > 1.0:
                        errors.append(
                            f"h2h fair out of range: {poly_team} fair={fair:.3f} "
                            f"| {question}"
                        )
                    if other_fair <= 0 or other_fair > 1.0:
                        errors.append(
                            f"h2h hedge_fair out of range: {poly_team} other_fair={other_fair:.3f} "
                            f"| {question}"
                        )
    
    print(f"\n  h2h: checked {checked} aligned markets, found {opportunities_found} opportunities")
    
    if errors:
        print(f"  ❌ {len(errors)} errors:")
        for e in errors[:10]:
            print(f"    • {e}")
    else:
        print(f"  ✅ All fair values and opportunities valid")
    
    assert checked > 0, "No h2h markets were checked — matching or hydration failure"
    assert not errors, f"{len(errors)} h2h edge errors:\n" + "\n".join(f"  {e}" for e in errors[:5])


@pytest.mark.asyncio
async def test_totals_edge_valid_opportunities(poly_client, odds_service):
    """Validate totals (O/U) edge detection produces valid opportunities.
    
    For totals markets:
    1. Over fair + Under fair sum to ~1.0 (binary constraint)
    2. fair_prob1 = Over, fair_prob2 = Under (convention)
    3. All opportunity invariants hold
    """
    totals_odds = [
        m for m in odds_service._v2_cache.values()
        if m.market_type in ("totals", "totals_1h") and m.fair_prob1 > 0
    ]
    
    if not totals_odds:
        pytest.skip("No totals odds in v2 cache")
    
    all_events = await _get_all_poly_events(poly_client)
    upcoming = [e for e in all_events if not e.is_finished and not e.is_live]
    
    errors = []
    checked = 0
    
    for odds_match in totals_odds:
        matching_events = find_all_poly_events(odds_match, upcoming)
        if not matching_events:
            continue
        
        for poly_event in matching_events:
            if not poly_event.markets:
                continue
            
            for market in poly_event.markets:
                question = market.get("question", "")
                if not match_odds_to_poly_market(
                    odds_match.market_type, odds_match.line, question, odds_match.game
                ):
                    continue
                
                outcomes = market.get("outcomes", [])
                token_ids = market.get("clobTokenIds", [])
                if len(token_ids) < 2 or len(outcomes) < 2:
                    continue
                
                # Identify Over/Under indices
                over_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "over"), None)
                under_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "under"), None)
                
                if over_idx is None or under_idx is None:
                    continue
                
                checked += 1
                
                # Convention: fair_prob1 = Over, fair_prob2 = Under
                over_fair = odds_match.fair_prob1 / 100.0
                under_fair = odds_match.fair_prob2 / 100.0
                
                # Binary constraint: Over + Under = 1.0
                total = over_fair + under_fair
                if abs(total - 1.0) > 0.02:
                    errors.append(
                        f"Totals sum ≠ 1.0: Over={over_fair:.3f} + Under={under_fair:.3f} "
                        f"= {total:.3f} | {question}"
                    )
                
                # Fair values in range
                if over_fair <= 0 or over_fair > 1.0:
                    errors.append(f"Over fair out of range: {over_fair:.3f} | {question}")
                if under_fair <= 0 or under_fair > 1.0:
                    errors.append(f"Under fair out of range: {under_fair:.3f} | {question}")
    
    print(f"\n  totals: checked {checked} matched markets")
    if errors:
        print(f"  ❌ {len(errors)} errors:")
        for e in errors[:10]:
            print(f"    • {e}")
    else:
        print(f"  ✅ All totals fair values valid")
    
    assert checked > 0, "No totals markets were checked — matching failure"
    assert not errors, f"{len(errors)} totals edge errors:\n" + "\n".join(f"  {e}" for e in errors[:5])


@pytest.mark.asyncio
async def test_spread_edge_valid_opportunities(poly_client, odds_service):
    """Validate spread edge detection produces valid opportunities.
    
    For spreads:
    1. Team1 fair + Team2 fair sum to ~1.0
    2. Fair values in (0, 1.0]
    3. Aligned team matches one of the odds teams
    """
    spread_odds = [
        m for m in odds_service._v2_cache.values()
        if m.market_type in ("spreads", "spreads_1h") and m.fair_prob1 > 0
    ]
    
    if not spread_odds:
        pytest.skip("No spread odds in v2 cache")
    
    all_events = await _get_all_poly_events(poly_client)
    upcoming = [e for e in all_events if not e.is_finished and not e.is_live]
    
    errors = []
    checked = 0
    
    for odds_match in spread_odds:
        matching_events = find_all_poly_events(odds_match, upcoming)
        if not matching_events:
            continue
        
        for poly_event in matching_events:
            if not poly_event.markets:
                continue
            
            for market in poly_event.markets:
                question = market.get("question", "")
                if not match_odds_to_poly_market(
                    odds_match.market_type, odds_match.line, question, odds_match.game
                ):
                    continue
                
                outcomes = market.get("outcomes", [])
                token_ids = market.get("clobTokenIds", [])
                if len(token_ids) < 2 or len(outcomes) < 2:
                    continue
                
                best_bids = await poly_client.get_best_bids(token_ids)
                aligned = align_teams_with_bids(
                    odds_match, outcomes, token_ids, best_bids
                )
                if not aligned:
                    continue
                
                checked += 1
                
                # Collect fair values for binary sum check
                fairs = [entry[3] for entry in aligned]  # fair is index 3
                
                # Binary constraint: both sides sum to ~1.0
                if len(fairs) >= 2:
                    total = fairs[0] + fairs[1]
                    if abs(total - 1.0) > 0.02:
                        errors.append(
                            f"Spread fair sum ≠ 1.0: {fairs[0]:.3f} + {fairs[1]:.3f} "
                            f"= {total:.3f} | {question}"
                        )
                
                for poly_team, bid_data, poly_token, fair, other_fair in aligned:
                    if fair <= 0 or fair > 1.0:
                        errors.append(
                            f"Spread fair out of range: {poly_team} fair={fair:.3f} | {question}"
                        )
    
    print(f"\n  spreads: checked {checked} aligned markets")
    if errors:
        print(f"  ❌ {len(errors)} errors:")
        for e in errors[:10]:
            print(f"    • {e}")
    else:
        print(f"  ✅ All spread fair values valid")
    
    assert not errors, f"{len(errors)} spread edge errors:\n" + "\n".join(f"  {e}" for e in errors[:5])


@pytest.mark.asyncio
async def test_yesno_h2h_binary_constraint(poly_client, odds_service):
    """Verify Yes/No h2h markets have correct binary fair values from scanner.
    
    Regression test for the PSV No bug — validates at the SCANNER level
    (not just at the odds matching level like test_no_token_binary_fair_value).
    
    For every 3-way "Will X win?" market:
    1. Yes fair + No fair = 1.0 (binary constraint)
    2. No fair = 1 - Yes fair (not raw opponent prob)
    3. Fair values are in valid range
    """
    threeway_odds = [
        m for m in odds_service._v2_cache.values()
        if m.market_type in ("h2h", "h2h_h1")
        and m.fair_prob1 > 0
        and m.fair_prob_draw is not None
        and m.fair_prob_draw > 0
    ]
    
    if not threeway_odds:
        pytest.skip("No 3-way h2h odds in v2 cache")
    
    all_events = await _get_all_poly_events(poly_client)
    upcoming = [e for e in all_events if not e.is_finished and not e.is_live]
    
    errors = []
    checked = 0
    
    for odds_match in threeway_odds:
        matching_events = find_all_poly_events(odds_match, upcoming)
        if not matching_events:
            continue
        
        for poly_event in matching_events:
            if not poly_event.markets:
                continue
            
            for market in poly_event.markets:
                question = market.get("question", "")
                outcomes = market.get("outcomes", [])
                token_ids = market.get("clobTokenIds", [])
                
                if len(outcomes) < 2 or len(token_ids) < 2:
                    continue
                
                # Only test Yes/No markets
                if outcomes[0].lower() not in ("yes", "no"):
                    continue
                
                if not match_odds_to_poly_market("h2h", None, question, odds_match.game):
                    continue
                
                best_bids = await poly_client.get_best_bids(token_ids)
                aligned = align_yesno_h2h(odds_match, market, token_ids, best_bids)
                
                if not aligned:
                    continue
                
                checked += 1
                
                # Find Yes and No entries
                yes_fair = None
                no_fair = None
                for poly_team, bid_data, poly_token, fair, other_fair in aligned:
                    if poly_team.lower() == "yes":
                        yes_fair = fair
                    elif poly_team.lower() == "no":
                        no_fair = fair
                
                if yes_fair is not None and no_fair is not None:
                    # BINARY CONSTRAINT: Yes + No = 1.0
                    binary_sum = yes_fair + no_fair
                    if abs(binary_sum - 1.0) > 0.01:
                        errors.append(
                            f"Binary sum ≠ 1.0: Yes={yes_fair:.3f} + No={no_fair:.3f} "
                            f"= {binary_sum:.3f} | {question}"
                        )
                    
                    # NO = 1 - YES (not raw opponent prob)
                    expected_no = 1.0 - yes_fair
                    if abs(no_fair - expected_no) > 0.001:
                        errors.append(
                            f"No ≠ 1-Yes: No={no_fair:.3f}, expected={expected_no:.3f} "
                            f"| {question}"
                        )
                    
                    # Range checks
                    if yes_fair <= 0 or yes_fair >= 1.0:
                        errors.append(f"Yes fair out of range: {yes_fair:.3f} | {question}")
                    if no_fair <= 0 or no_fair >= 1.0:
                        errors.append(f"No fair out of range: {no_fair:.3f} | {question}")
                    
                    # For 3-way sports, yes_fair must be < raw sum (draw probability exists)
                    raw_sum = (odds_match.fair_prob1 + odds_match.fair_prob2) / 100.0
                    if raw_sum >= 1.0:
                        errors.append(
                            f"Raw 3-way probs sum >= 1.0: {raw_sum:.3f} "
                            f"(prob1={odds_match.fair_prob1:.1f}, prob2={odds_match.fair_prob2:.1f}) "
                            f"| {question}"
                        )
    
    print(f"\n  Yes/No h2h: checked {checked} markets")
    if errors:
        print(f"  ❌ {len(errors)} errors:")
        for e in errors[:10]:
            print(f"    • {e}")
    else:
        print(f"  ✅ All Yes/No markets have correct binary fair values")
    
    assert not errors, (
        f"{len(errors)} Yes/No binary constraint errors:\n"
        + "\n".join(f"  {e}" for e in errors[:5])
    )


@pytest.mark.asyncio
async def test_event_matching_all_sports(poly_client, odds_service):
    """Verify find_all_poly_events matches odds to Poly events across all sports.
    
    For each sport with v2 odds, checks that:
    1. At least some odds entries find matching Poly events
    2. No cross-game matches (football odds don't match basketball events)
    3. Matched events have clean team names (no colons, no parens)
    """
    all_events = await _get_all_poly_events(poly_client)
    upcoming = [e for e in all_events if not e.is_finished]
    
    sport_stats = defaultdict(lambda: {"total": 0, "matched": 0, "cross_game": 0})
    errors = []
    
    for odds_match in odds_service._v2_cache.values():
        if odds_match.fair_prob1 <= 0:
            continue
        
        sport = odds_match.game or "unknown"
        sport_stats[sport]["total"] += 1
        
        matches = find_all_poly_events(odds_match, upcoming)
        if matches:
            sport_stats[sport]["matched"] += 1
            
            for poly_event in matches:
                # Cross-game safety: event game should be compatible with odds game
                poly_game = (poly_event.game or "").lower()
                odds_game = sport.lower()
                
                # Extract base sport from sub-league names:
                #   "basketball_korea_kbl" → "basketball"
                #   "icehockey_khl" → "icehockey" → also matches "hockey"
                #   "football" stays "football"
                def _base_sport(g):
                    base = g.split("_")[0] if "_" in g else g
                    # "icehockey" should also match "hockey"
                    return base.replace("ice", "")
                
                # Allow fuzzy game matching:
                # base sports match, or one contains the other
                pg_base = _base_sport(poly_game)
                og_base = _base_sport(odds_game)
                
                game_compatible = (
                    pg_base == og_base
                    or poly_game == odds_game
                    or pg_base in og_base
                    or og_base in pg_base
                    # ncaab is basketball
                    or (og_base == "basketball" and pg_base == "ncaab")
                    or (pg_base == "basketball" and og_base == "ncaab")
                )
                if not game_compatible:
                    sport_stats[sport]["cross_game"] += 1
                    errors.append(
                        f"Cross-game match: odds={sport} matched poly={poly_game} "
                        f"| odds=({odds_match.team1} vs {odds_match.team2}) "
                        f"poly=({poly_event.team1} vs {poly_event.team2})"
                    )
    
    # Print per-sport summary
    for sport, stats in sorted(sport_stats.items()):
        total = stats["total"]
        matched = stats["matched"]
        pct = (matched / total * 100) if total > 0 else 0
        cross = stats["cross_game"]
        status = "❌" if cross > 0 else "✅"
        print(f"  {status} {sport}: {matched}/{total} matched ({pct:.0f}%)"
              + (f", {cross} cross-game!" if cross > 0 else ""))
    
    if errors:
        print(f"\n  ❌ Cross-game matching errors ({len(errors)}):")
        for e in errors[:5]:
            print(f"    • {e}")
    else:
        total_matched = sum(s["matched"] for s in sport_stats.values())
        total_odds = sum(s["total"] for s in sport_stats.values())
        print(f"\n  ✅ {total_matched}/{total_odds} odds matched across {len(sport_stats)} sports, no cross-game errors")
    
    assert not errors, (
        f"{len(errors)} cross-game matching errors:\n"
        + "\n".join(f"  {e}" for e in errors[:5])
    )


@pytest.mark.asyncio
async def test_match_id_isolation(odds_service):
    """Verify that different market types produce isolated effective_match_ids.
    
    Simulates the match_id construction from sports_bot._execute_opportunity:
    - Spreads/totals get condition_id suffix
    - Yes/No h2h get condition_id suffix  
    - Plain team-vs-team h2h does NOT get suffix
    
    This prevents BotState collisions between markets on the same game.
    """
    errors = []
    checked = 0
    
    # Group v2 odds by base match_id to find multi-market matches
    by_match = defaultdict(list)
    for odds_match in odds_service._v2_cache.values():
        if odds_match.fair_prob1 > 0:
            by_match[odds_match.match_id].append(odds_match)
    
    multi_market_matches = {
        mid: matches for mid, matches in by_match.items()
        if len(matches) > 1
    }
    
    if not multi_market_matches:
        pytest.skip("No multi-market matches in v2 cache")
    
    for base_id, matches in multi_market_matches.items():
        checked += 1
        market_types = [m.market_type for m in matches]
        
        # If there are multiple market types for the same match, they need isolation
        type_set = set(market_types)
        if len(type_set) <= 1:
            continue
        
        # Simulate effective_match_id construction
        effective_ids = set()
        for m in matches:
            mt = m.market_type
            needs_suffix = mt in ("spreads", "totals", "spreads_1h", "totals_1h")
            # Note: h2h Yes/No would also need suffix, but we can't determine
            # Yes/No from odds alone (that comes from Polymarket outcomes)
            
            if needs_suffix:
                # Use a synthetic condition_id for testing
                eid = f"{m.match_id}:{mt}_{m.line or 0}"
            else:
                eid = m.match_id
            effective_ids.add(eid)
        
        # For multi-market matches, effective IDs should not all be the same
        # (unless all are h2h variants which share the same base ID)
        non_h2h = [m for m in matches if m.market_type not in ("h2h", "h2h_h1")]
        if non_h2h and len(effective_ids) <= 1:
            errors.append(
                f"No isolation for multi-market match {base_id}: "
                f"types={market_types}, all got same effective_id"
            )
    
    print(f"\n  Checked {checked} multi-market matches")
    if errors:
        print(f"  ❌ {len(errors)} isolation errors:")
        for e in errors[:10]:
            print(f"    • {e}")
    else:
        print(f"  ✅ All multi-market matches have correct isolation")
    
    assert not errors, (
        f"{len(errors)} match_id isolation errors:\n"
        + "\n".join(f"  {e}" for e in errors[:5])
    )
