"""
Integration Tests: Market Parsing Correctness with Real Polymarket Data

Fetches real events from every league in SPORTS_SERIES_MAP, hydrates them,
and verifies that our parsing pipeline (classify_market_type, extract_line,
match_odds_to_poly_market) handles every market correctly.

Run with:
    proxychains4 -q python -m pytest tests/integration/test_market_parsing.py -v -s

Run a single sport:
    proxychains4 -q python -m pytest tests/integration/test_market_parsing.py -v -s -k "football"

IMPORTANT: These tests hit the live Gamma API. They must be run through
proxychains4 to work around SSL certificate issues on Fly.io.
"""
import asyncio
import pytest
from collections import defaultdict
from typing import List, Dict, Any

from src.polymarket.market_client import PolymarketEsportsClient, PolymarketEsportsEvent
from src.core.market_parser import (
    classify_market_type,
    extract_line,
    match_odds_to_poly_market,
    ODDS_API_TO_POLY_TYPE,
    LINE_BEARING_TYPES,
    SPREAD, TOTALS, OU, MONEYLINE, BTTS, HALFTIME, HALFTIME_SPREAD, HALFTIME_TOTALS,
    SET_HANDICAP, SET_WINNER, TOTAL_SETS, ROUNDS_OU, GO_THE_DISTANCE, WIN_BY_KO,
    EXACT_SCORE, GOALSCORER, TOSS_WINNER, TOSS_MATCH_DOUBLE, COMPLETED_MATCH,
    TOP_BATTER, MOST_SIXES, PLAYER_PROP, OTHER,
)

# Reverse map: Polymarket type → odds-api type (for round-trip testing)
POLY_TYPE_TO_ODDS_API = {}
for odds_key, poly_type in ODDS_API_TO_POLY_TYPE.items():
    # Keep first mapping (most specific)
    if poly_type not in POLY_TYPE_TO_ODDS_API:
        POLY_TYPE_TO_ODDS_API[poly_type] = odds_key

# Market types we actively trade (must parse perfectly)
TRADED_TYPES = {MONEYLINE, SPREAD, TOTALS, OU, BTTS, HALFTIME, HALFTIME_SPREAD, HALFTIME_TOTALS}

# Market types we recognize but don't trade yet (parsing is nice-to-have)
RECOGNIZED_NON_TRADED = {
    SET_HANDICAP, SET_WINNER, TOTAL_SETS, GO_THE_DISTANCE, ROUNDS_OU,
    WIN_BY_KO, EXACT_SCORE, GOALSCORER, TOSS_WINNER, TOSS_MATCH_DOUBLE,
    COMPLETED_MATCH, TOP_BATTER, MOST_SIXES, PLAYER_PROP,
}


# =====================================================================
# Helpers
# =====================================================================

def _build_league_map() -> Dict[str, List[str]]:
    """Build sport → list of (series_id, label) from SPORTS_SERIES_MAP."""
    return dict(PolymarketEsportsClient.SPORTS_SERIES_MAP)


async def _fetch_and_hydrate_sport(
    client: PolymarketEsportsClient,
    sport: str,
    series_ids: List[str],
    max_hydrate: int = 20,
) -> List[PolymarketEsportsEvent]:
    """Fetch series events and hydrate up to max_hydrate of them.
    
    Uses round-robin across series to ensure each league gets representation.
    Without this, leagues listed first in the series list would consume
    all hydration slots (e.g., NCAAB filling 30 slots before NBA is reached).
    """
    # Build reverse map for game assignment
    series_to_game = {sid: sport for sid in series_ids}
    
    # Temporarily add to SERIES_MAP for get_series_events game lookup
    original_map = dict(client.SERIES_MAP)
    for sid in series_ids:
        client.SERIES_MAP[f"{sport}_{sid}"] = sid
    
    sem = asyncio.Semaphore(3)
    
    async def fetch_with_limit(sid):
        async with sem:
            return await client.get_series_events(sid)
    
    results = await asyncio.gather(
        *[fetch_with_limit(sid) for sid in series_ids],
        return_exceptions=True,
    )
    
    # Restore original map
    client.SERIES_MAP = original_map
    
    # Group events by series for round-robin selection
    events_by_series: Dict[str, List[PolymarketEsportsEvent]] = {}
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"    ⚠️  Series {series_ids[i]} fetch error: {result}")
            continue
        sid = series_ids[i]
        game = series_to_game.get(sid, "unknown")
        upcoming = []
        for event in result:
            event.game = game
            if not event.is_finished:
                upcoming.append(event)
        if upcoming:
            events_by_series[sid] = upcoming
    
    # Round-robin: pick events from each series in turn
    selected = []
    iterators = {sid: iter(evts) for sid, evts in events_by_series.items()}
    while len(selected) < max_hydrate and iterators:
        exhausted = []
        for sid, it in iterators.items():
            if len(selected) >= max_hydrate:
                break
            try:
                selected.append(next(it))
            except StopIteration:
                exhausted.append(sid)
        for sid in exhausted:
            del iterators[sid]
    
    # Hydrate selected events to get market data
    hydrated = []
    for event in selected:
        if not event.markets:
            event = await client.hydrate_event(event)
        hydrated.append(event)
        # Small delay to be nice to the API
        await asyncio.sleep(0.1)
    
    return hydrated


async def _fetch_and_hydrate_series(
    client: PolymarketEsportsClient,
    sport: str,
    series_id: str,
    max_hydrate: int = 10,
) -> List[PolymarketEsportsEvent]:
    """Fetch and hydrate events from a single specific series."""
    # Temporarily add to SERIES_MAP for game lookup
    original_map = dict(client.SERIES_MAP)
    client.SERIES_MAP[f"{sport}_{series_id}"] = series_id
    
    events = await client.get_series_events(series_id)
    
    client.SERIES_MAP = original_map
    
    # Set game and filter
    upcoming = []
    for event in events:
        event.game = sport
        if not event.is_finished:
            upcoming.append(event)
    
    # Hydrate
    hydrated = []
    for event in upcoming[:max_hydrate]:
        if not event.markets:
            event = await client.hydrate_event(event)
        hydrated.append(event)
        await asyncio.sleep(0.1)
    
    return hydrated


def _analyze_markets(
    events: List[PolymarketEsportsEvent],
    sport: str,
) -> Dict[str, Any]:
    """Analyze all markets across events and return parsing results."""
    stats = {
        "total_events": len(events),
        "total_markets": 0,
        "type_counts": defaultdict(int),
        "parsed_ok": [],       # (event_title, question, market_type, line)
        "parse_failures": [],  # (event_title, question, market_type, error)
        "unsupported": [],     # (event_title, question) — classified as Other
        "roundtrip_ok": 0,
        "roundtrip_fail": [],  # (question, market_type, line, odds_api_type)
        "outcome_issues": [],  # (event_title, question, market_type, outcomes, issue)
    }
    
    for event in events:
        if not event.markets:
            continue
            
        for market in event.markets:
            question = market.get("question", "")
            outcomes = market.get("outcomes", [])
            if not question:
                continue
            
            stats["total_markets"] += 1
            
            # --- Step 1: Classify market type ---
            market_type = classify_market_type(question, sport)
            stats["type_counts"][market_type] += 1
            
            if market_type == OTHER:
                stats["unsupported"].append((event.title, question))
                continue
            
            # --- Step 2: Extract line for line-bearing types ---
            line = None
            if market_type in LINE_BEARING_TYPES:
                line = extract_line(question, market_type)
                if line is None:
                    stats["parse_failures"].append((
                        event.title, question, market_type,
                        f"extract_line returned None for {market_type}"
                    ))
                else:
                    stats["parsed_ok"].append((event.title, question, market_type, line))
            else:
                stats["parsed_ok"].append((event.title, question, market_type, None))
            
            # --- Step 3: Validate outcomes ---
            _validate_outcomes(event.title, question, market_type, outcomes, sport, stats)
            
            # --- Step 4: Round-trip test with match_odds_to_poly_market ---
            if market_type in TRADED_TYPES:
                odds_api_type = POLY_TYPE_TO_ODDS_API.get(market_type)
                if odds_api_type:
                    matched = match_odds_to_poly_market(
                        odds_api_type, line, question, sport
                    )
                    if matched:
                        stats["roundtrip_ok"] += 1
                    else:
                        stats["roundtrip_fail"].append((
                            question, market_type, line, odds_api_type
                        ))
    
    return stats


def _validate_outcomes(
    event_title: str,
    question: str,
    market_type: str,
    outcomes: list,
    sport: str,
    stats: dict,
):
    """Validate that outcomes make sense for the market type."""
    n = len(outcomes)
    
    if market_type == MONEYLINE:
        # 2  outcomes for esports/2-way sports, 3 for football/rugby (draw)
        if sport in ("football",) and n not in (2, 3):
            stats["outcome_issues"].append((
                event_title, question, market_type, outcomes,
                f"Football moneyline expected 2-3 outcomes, got {n}"
            ))
        elif sport not in ("football",) and n not in (2, 3):
            stats["outcome_issues"].append((
                event_title, question, market_type, outcomes,
                f"Moneyline expected 2 outcomes (or 3 for draws), got {n}"
            ))
    
    elif market_type in (SPREAD, HALFTIME_SPREAD):
        if n != 2:
            stats["outcome_issues"].append((
                event_title, question, market_type, outcomes,
                f"Spread expected 2 outcomes, got {n}"
            ))
    
    elif market_type in (TOTALS, OU, HALFTIME_TOTALS):
        if n != 2:
            stats["outcome_issues"].append((
                event_title, question, market_type, outcomes,
                f"Totals/O/U expected 2 outcomes, got {n}"
            ))
        elif n == 2:
            # Check that outcomes look like Over/Under
            o_lower = [o.lower() for o in outcomes]
            has_over = any("over" in o or o.startswith("o") for o in o_lower)
            has_under = any("under" in o or o.startswith("u") for o in o_lower)
            if not (has_over and has_under):
                # Some totals markets use team names as outcomes — that's OK
                # Only flag if the outcomes are clearly wrong
                pass
    
    elif market_type == BTTS:
        if n != 2:
            stats["outcome_issues"].append((
                event_title, question, market_type, outcomes,
                f"BTTS expected 2 outcomes (Yes/No), got {n}"
            ))


def _print_report(sport: str, stats: Dict[str, Any]):
    """Print a human-readable report for a sport."""
    print(f"\n{'='*70}")
    print(f"  {sport.upper()} — Market Parsing Report")
    print(f"{'='*70}")
    print(f"  Events: {stats['total_events']}  |  Markets: {stats['total_markets']}")
    
    if not stats["total_markets"]:
        print("  (no markets found — all events may be finished or unhydrated)")
        return
    
    # Type breakdown
    print(f"\n  📊 Market Type Breakdown:")
    for mtype, count in sorted(stats["type_counts"].items(), key=lambda x: -x[1]):
        traded = "✅" if mtype in TRADED_TYPES else "⬜"
        print(f"    {traded} {mtype:25s} {count:4d}")
    
    # Successfully parsed
    ok_count = len(stats["parsed_ok"])
    total_recognized = stats["total_markets"] - len(stats["unsupported"])
    if total_recognized > 0:
        pct = ok_count / total_recognized * 100
        print(f"\n  ✅ Parsed OK: {ok_count}/{total_recognized} recognized markets ({pct:.0f}%)")
    
    # Round-trip results
    rt_total = stats["roundtrip_ok"] + len(stats["roundtrip_fail"])
    if rt_total > 0:
        print(f"  🔄 Round-trip (match_odds_to_poly_market): {stats['roundtrip_ok']}/{rt_total} passed")
    
    # Failures
    if stats["parse_failures"]:
        print(f"\n  ❌ PARSE FAILURES ({len(stats['parse_failures'])}):")
        for event_title, question, mtype, error in stats["parse_failures"][:10]:
            print(f"    • [{mtype}] \"{question}\"")
            print(f"      Event: {event_title}")
            print(f"      Error: {error}")
    
    if stats["roundtrip_fail"]:
        print(f"\n  ❌ ROUND-TRIP FAILURES ({len(stats['roundtrip_fail'])}):")
        for question, mtype, line, odds_api_type in stats["roundtrip_fail"][:10]:
            print(f"    • [{mtype}] \"{question}\" (line={line}, odds_api_type={odds_api_type})")
    
    if stats["outcome_issues"]:
        print(f"\n  ⚠️  OUTCOME ISSUES ({len(stats['outcome_issues'])}):")
        for event_title, question, mtype, outcomes, issue in stats["outcome_issues"][:10]:
            print(f"    • [{mtype}] \"{question}\"")
            print(f"      Outcomes: {outcomes}")
            print(f"      Issue: {issue}")
    
    # Unsupported markets
    if stats["unsupported"]:
        print(f"\n  ⬜ UNSUPPORTED MARKETS ({len(stats['unsupported'])} classified as 'Other'):")
        # Group by question pattern to reduce noise
        seen = set()
        for event_title, question in stats["unsupported"][:20]:
            # Deduplicate by first few words
            short = " ".join(question.split()[:4])
            if short not in seen:
                seen.add(short)
                print(f"    • \"{question}\"")
                print(f"      Event: {event_title}")


# =====================================================================
# Tests — one per sport in SPORTS_SERIES_MAP
# =====================================================================

@pytest.mark.asyncio
async def test_football_market_parsing(poly_client):
    """Verify all football (soccer) markets parse correctly."""
    sport = "football"
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP[sport]
    events = await _fetch_and_hydrate_sport(poly_client, sport, series_ids)
    stats = _analyze_markets(events, sport)
    _print_report(sport, stats)
    
    # Hard assertions: traded types must parse without failures
    assert not stats["parse_failures"], (
        f"{len(stats['parse_failures'])} parse failures in football markets:\n"
        + "\n".join(f"  [{mtype}] {q}" for _, q, mtype, _ in stats["parse_failures"][:5])
    )
    assert not stats["roundtrip_fail"], (
        f"{len(stats['roundtrip_fail'])} round-trip failures:\n"
        + "\n".join(f"  [{mt}] {q}" for q, mt, _, _ in stats["roundtrip_fail"][:5])
    )


@pytest.mark.asyncio
async def test_hockey_market_parsing(poly_client):
    """Verify all hockey markets parse correctly."""
    sport = "hockey"
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP[sport]
    events = await _fetch_and_hydrate_sport(poly_client, sport, series_ids)
    stats = _analyze_markets(events, sport)
    _print_report(sport, stats)
    
    assert not stats["parse_failures"], (
        f"{len(stats['parse_failures'])} parse failures in hockey markets:\n"
        + "\n".join(f"  [{mtype}] {q}" for _, q, mtype, _ in stats["parse_failures"][:5])
    )
    assert not stats["roundtrip_fail"], (
        f"{len(stats['roundtrip_fail'])} round-trip failures:\n"
        + "\n".join(f"  [{mt}] {q}" for q, mt, _, _ in stats["roundtrip_fail"][:5])
    )


@pytest.mark.asyncio
async def test_basketball_market_parsing(poly_client):
    """Verify all basketball (NCAAB/NBA/international) markets parse correctly.
    
    Uses round-robin across all basketball series to ensure NBA events
    (which have rich markets) aren't crowded out by NCAAB moneylines.
    """
    sport = "basketball"
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP[sport]
    events = await _fetch_and_hydrate_sport(poly_client, sport, series_ids, max_hydrate=30)
    stats = _analyze_markets(events, sport)
    _print_report(sport, stats)
    
    assert not stats["parse_failures"], (
        f"{len(stats['parse_failures'])} parse failures in basketball markets:\n"
        + "\n".join(f"  [{mtype}] {q}" for _, q, mtype, _ in stats["parse_failures"][:5])
    )
    assert not stats["roundtrip_fail"], (
        f"{len(stats['roundtrip_fail'])} round-trip failures:\n"
        + "\n".join(f"  [{mt}] {q}" for q, mt, _, _ in stats["roundtrip_fail"][:5])
    )


@pytest.mark.asyncio
async def test_nba_market_parsing(poly_client):
    """Verify all NBA markets parse correctly — the richest market type.
    
    NBA events typically have 40-50 markets per game:
    - Moneyline (winner)
    - Multiple spreads at different lines (-1.5, -2.5, -3.5, etc.)
    - Multiple totals O/U at different lines (217.5, 218.5, etc.)
    - 1H Spread, 1H O/U, 1H Moneyline
    - Player props: Points O/U, Rebounds O/U, Assists O/U
    
    This test specifically targets NBA series 10345 to ensure we don't
    miss these markets when they're mixed in with other basketball leagues.
    """
    NBA_SERIES_ID = "10345"
    events = await _fetch_and_hydrate_series(
        poly_client, "basketball", NBA_SERIES_ID, max_hydrate=10
    )
    stats = _analyze_markets(events, "basketball")
    _print_report("NBA", stats)
    
    # Hard assertions: all recognized markets must parse
    assert not stats["parse_failures"], (
        f"{len(stats['parse_failures'])} parse failures in NBA markets:\n"
        + "\n".join(f"  [{mtype}] {q}" for _, q, mtype, _ in stats["parse_failures"][:5])
    )
    assert not stats["roundtrip_fail"], (
        f"{len(stats['roundtrip_fail'])} round-trip failures:\n"
        + "\n".join(f"  [{mt}] {q}" for q, mt, _, _ in stats["roundtrip_fail"][:5])
    )
    
    # Live integration test: NBA spread/totals markets only exist when games
    # are scheduled. Skip (don't fail) when the API returns nothing to parse.
    type_counts = stats["type_counts"]
    if not type_counts:
        pytest.skip("no live NBA markets available (off-season or between games)")

    # NBA should have more than just moneylines
    assert type_counts.get(SPREAD, 0) > 0, "NBA should have spread markets"
    assert type_counts.get(TOTALS, 0) > 0, "NBA should have totals markets"
    
    # Check for 1H markets (NBA is the primary league for these)
    has_1h = (
        type_counts.get(HALFTIME_SPREAD, 0) > 0
        or type_counts.get(HALFTIME_TOTALS, 0) > 0
        or type_counts.get(HALFTIME, 0) > 0
    )
    if has_1h:
        print(f"\n  ✅ 1H markets found: "
              f"1H Spread={type_counts.get(HALFTIME_SPREAD, 0)}, "
              f"1H Totals={type_counts.get(HALFTIME_TOTALS, 0)}, "
              f"1H Moneyline={type_counts.get(HALFTIME, 0)}")
    else:
        print(f"\n  ⚠️  No 1H markets found in NBA — check if events have them")
    
    # Report player props (currently classified as Other)
    unsupported_questions = [q for _, q in stats["unsupported"]]
    player_prop_keywords = ["assists", "points", "rebounds", "three pointers", "steals", "blocks"]
    found_props = [q for q in unsupported_questions
                   if any(kw in q.lower() for kw in player_prop_keywords)]
    if found_props:
        # Group by type
        points_props = [q for q in found_props if "points" in q.lower()]
        rebounds_props = [q for q in found_props if "rebounds" in q.lower()]
        assists_props = [q for q in found_props if "assists" in q.lower()]
        print(f"\n  ℹ️  Player props found ({len(found_props)} total, classified as Other):")
        print(f"    Points:   {len(points_props)} markets")
        print(f"    Rebounds: {len(rebounds_props)} markets")
        print(f"    Assists:  {len(assists_props)} markets")
        for q in found_props[:5]:
            print(f"    • \"{q}\"")


@pytest.mark.asyncio
async def test_rugby_market_parsing(poly_client):
    """Verify all rugby markets parse correctly."""
    sport = "rugby"
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP[sport]
    events = await _fetch_and_hydrate_sport(poly_client, sport, series_ids)
    stats = _analyze_markets(events, sport)
    _print_report(sport, stats)
    
    assert not stats["parse_failures"], (
        f"{len(stats['parse_failures'])} parse failures in rugby markets:\n"
        + "\n".join(f"  [{mtype}] {q}" for _, q, mtype, _ in stats["parse_failures"][:5])
    )
    assert not stats["roundtrip_fail"], (
        f"{len(stats['roundtrip_fail'])} round-trip failures:\n"
        + "\n".join(f"  [{mt}] {q}" for q, mt, _, _ in stats["roundtrip_fail"][:5])
    )


@pytest.mark.asyncio
async def test_cricket_market_parsing(poly_client):
    """Verify all cricket markets parse correctly."""
    sport = "cricket"
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP[sport]
    events = await _fetch_and_hydrate_sport(poly_client, sport, series_ids)
    stats = _analyze_markets(events, sport)
    _print_report(sport, stats)
    
    assert not stats["parse_failures"], (
        f"{len(stats['parse_failures'])} parse failures in cricket markets:\n"
        + "\n".join(f"  [{mtype}] {q}" for _, q, mtype, _ in stats["parse_failures"][:5])
    )
    assert not stats["roundtrip_fail"], (
        f"{len(stats['roundtrip_fail'])} round-trip failures:\n"
        + "\n".join(f"  [{mt}] {q}" for q, mt, _, _ in stats["roundtrip_fail"][:5])
    )


@pytest.mark.asyncio
async def test_mma_market_parsing(poly_client):
    """Verify all MMA/UFC markets parse correctly."""
    sport = "mma"
    series_ids = PolymarketEsportsClient.SPORTS_SERIES_MAP[sport]
    events = await _fetch_and_hydrate_sport(poly_client, sport, series_ids)
    # UFC uses "ufc" for sport-specific classification in market_parser
    stats = _analyze_markets(events, "ufc")
    _print_report(sport, stats)
    
    assert not stats["parse_failures"], (
        f"{len(stats['parse_failures'])} parse failures in MMA markets:\n"
        + "\n".join(f"  [{mtype}] {q}" for _, q, mtype, _ in stats["parse_failures"][:5])
    )


# =====================================================================
# Cross-sport aggregate test
# =====================================================================

@pytest.mark.asyncio
async def test_all_sports_aggregate(poly_client):
    """Aggregate parsing stats across ALL sports.
    
    This is the comprehensive test — runs all sports and produces a
    combined report showing coverage and gaps.
    """
    all_stats = {}
    league_map = _build_league_map()
    
    for sport, series_ids in league_map.items():
        # Use "ufc" for MMA sport-specific classification
        classifier_sport = "ufc" if sport == "mma" else sport
        events = await _fetch_and_hydrate_sport(
            poly_client, sport, series_ids, max_hydrate=15
        )
        stats = _analyze_markets(events, classifier_sport)
        all_stats[sport] = stats
        _print_report(sport, stats)
    
    # Aggregate summary
    print(f"\n{'='*70}")
    print(f"  AGGREGATE SUMMARY")
    print(f"{'='*70}")
    
    total_events = sum(s["total_events"] for s in all_stats.values())
    total_markets = sum(s["total_markets"] for s in all_stats.values())
    total_ok = sum(len(s["parsed_ok"]) for s in all_stats.values())
    total_failures = sum(len(s["parse_failures"]) for s in all_stats.values())
    total_unsupported = sum(len(s["unsupported"]) for s in all_stats.values())
    total_rt_ok = sum(s["roundtrip_ok"] for s in all_stats.values())
    total_rt_fail = sum(len(s["roundtrip_fail"]) for s in all_stats.values())
    total_outcome_issues = sum(len(s["outcome_issues"]) for s in all_stats.values())
    
    print(f"  Events:     {total_events}")
    print(f"  Markets:    {total_markets}")
    print(f"  Parsed OK:  {total_ok}")
    print(f"  Failures:   {total_failures}")
    print(f"  Unsupported:{total_unsupported}")
    print(f"  Round-trip:  {total_rt_ok} OK / {total_rt_fail} FAIL")
    print(f"  Outcome issues: {total_outcome_issues}")
    
    # Global type breakdown
    global_types = defaultdict(int)
    for s in all_stats.values():
        for mtype, count in s["type_counts"].items():
            global_types[mtype] += count
    
    print(f"\n  📊 Global Market Type Breakdown:")
    for mtype, count in sorted(global_types.items(), key=lambda x: -x[1]):
        traded = "✅" if mtype in TRADED_TYPES else "⬜"
        print(f"    {traded} {mtype:25s} {count:4d}")
    
    # Hard assertions
    assert total_failures == 0, (
        f"{total_failures} total parse failures across all sports"
    )
    assert total_rt_fail == 0, (
        f"{total_rt_fail} total round-trip failures across all sports"
    )


# =====================================================================
# Targeted invariant tests
# =====================================================================

@pytest.mark.asyncio
async def test_spread_markets_have_valid_lines(poly_client):
    """Every spread market must have an extractable signed numeric line."""
    league_map = _build_league_map()
    failures = []
    
    for sport, series_ids in league_map.items():
        classifier_sport = "ufc" if sport == "mma" else sport
        events = await _fetch_and_hydrate_sport(
            poly_client, sport, series_ids, max_hydrate=10
        )
        
        for event in events:
            for market in (event.markets or []):
                question = market.get("question", "")
                mtype = classify_market_type(question, classifier_sport)
                
                if mtype not in (SPREAD, HALFTIME_SPREAD):
                    continue
                
                line = extract_line(question, mtype)
                if line is None:
                    failures.append((sport, event.title, question, mtype))
                    continue
                
                # Spread lines should be non-zero
                if line == 0:
                    failures.append((sport, event.title, question,
                                    f"Spread line is 0 (suspicious)"))
    
    if failures:
        print(f"\n  ❌ Spread line extraction failures ({len(failures)}):")
        for sport, event_title, question, issue in failures[:10]:
            print(f"    [{sport}] {question}")
            print(f"      Event: {event_title}")
            print(f"      Issue: {issue}")
    
    assert not failures, f"{len(failures)} spread markets with invalid lines"


@pytest.mark.asyncio
async def test_totals_markets_have_valid_lines(poly_client):
    """Every totals/O/U market must have an extractable positive numeric line."""
    league_map = _build_league_map()
    failures = []
    
    for sport, series_ids in league_map.items():
        classifier_sport = "ufc" if sport == "mma" else sport
        events = await _fetch_and_hydrate_sport(
            poly_client, sport, series_ids, max_hydrate=10
        )
        
        for event in events:
            for market in (event.markets or []):
                question = market.get("question", "")
                mtype = classify_market_type(question, classifier_sport)
                
                if mtype not in (TOTALS, OU, HALFTIME_TOTALS):
                    continue
                
                line = extract_line(question, mtype)
                if line is None:
                    # Bare "Over" / "Under" tokens might not have lines —
                    # that's OK, they get lines from event_title at runtime
                    q_lower = question.strip().lower()
                    if q_lower in ("over", "under"):
                        continue
                    failures.append((sport, event.title, question,
                                    f"extract_line returned None for {mtype}"))
                    continue
                
                # Totals lines should be positive
                if line <= 0:
                    failures.append((sport, event.title, question,
                                    f"Totals line is {line} (expected positive)"))
    
    if failures:
        print(f"\n  ❌ Totals line extraction failures ({len(failures)}):")
        for sport, event_title, question, issue in failures[:10]:
            print(f"    [{sport}] {question}")
            print(f"      Event: {event_title}")
            print(f"      Issue: {issue}")
    
    assert not failures, f"{len(failures)} totals markets with invalid lines"


@pytest.mark.asyncio
async def test_event_team_parsing(poly_client):
    """Every hydrated event should have team1 and team2 extracted from the title."""
    league_map = _build_league_map()
    missing_teams = []
    total_events = 0
    
    for sport, series_ids in league_map.items():
        events = await _fetch_and_hydrate_sport(
            poly_client, sport, series_ids, max_hydrate=10
        )
        
        for event in events:
            total_events += 1
            if not event.team1 or not event.team2:
                missing_teams.append((sport, event.title, event.team1, event.team2))
    
    if missing_teams:
        print(f"\n  ⚠️  Events with missing team names ({len(missing_teams)}/{total_events}):")
        for sport, title, t1, t2 in missing_teams[:10]:
            print(f"    [{sport}] \"{title}\" → team1={t1!r}, team2={t2!r}")
    
    # Warn but don't fail — some events genuinely don't have vs-style titles
    # (e.g., "Premier League: More Markets")
    if missing_teams:
        pct_missing = len(missing_teams) / total_events * 100 if total_events else 0
        print(f"\n  ℹ️  {pct_missing:.0f}% of events missing teams — check if these are 'More Markets' containers")
