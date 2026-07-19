"""Unit tests for the Rust sidecar opportunity-hydration helper."""
from types import SimpleNamespace

import pytest

from src.scanning.sidecar_hydrate import (
    HydrationError,
    hydrate_opportunities,
    hydrate_opportunity,
)


def _rust_opp(**overrides) -> dict:
    base = {
        "type": "opportunity",
        "tick_id": 1,
        "token_id": "tok_a",
        "team": "Team A",
        "hedge_team": "Team B",
        "hedge_token": "tok_b",
        "entry_price": 0.42,
        "fair": 0.55,
        "best_bid": 0.42,
        "edge": 0.09,
        "hedge_fair": 0.45,
        "expected_profit": 0.06,
        "match_id": "cs2:a:vs:b",
        "poly_event_id": "evt_1",
        "condition_id": "cond_1",
        "poly_event_team1": "Team A",
        "poly_event_team2": "Team B",
        "start_time": "2026-04-18T20:00:00Z",
        "game": "cs2",
    }
    base.update(overrides)
    return base


def test_hydrates_with_minimal_valid_opp():
    h = hydrate_opportunity(_rust_opp())

    assert h["token_id"] == "tok_a"
    assert h["team"] == "Team A"
    assert h["hedge_team"] == "Team B"
    assert h["hedge_token"] == "tok_b"
    assert h["entry_price"] == 0.42
    assert h["fair"] == 0.55
    assert h["edge"] == 0.09
    assert h["expected_profit"] == 0.06
    assert h["market_type"] == "h2h"
    assert h["line"] == 0


def test_poly_event_exposes_expected_attributes():
    h = hydrate_opportunity(_rust_opp())
    pe = h["poly_event"]

    assert pe.team1 == "Team A"
    assert pe.team2 == "Team B"
    assert pe.start_time == "2026-04-18T20:00:00Z"
    assert len(pe.markets) == 1
    assert pe.markets[0].get("condition_id") == "cond_1"
    assert pe.markets[0].get("outcomes") == ["Team A", "Team B"]


def test_odds_match_synthesized_when_cache_miss():
    h = hydrate_opportunity(_rust_opp(), odds_cache={})
    om = h["odds_match"]

    assert om.match_id == "cs2:a:vs:b"
    assert om.team1 == "Team A"
    assert om.team2 == "Team B"
    assert om.game == "cs2"


def test_odds_match_reuses_cached_aggregated_when_present():
    cached = SimpleNamespace(
        match_id="cs2:a:vs:b",
        team1="Team A",
        team2="Team B",
        game="cs2",
        is_live=False,
    )
    cache = {"cs2:a:vs:b": cached}
    h = hydrate_opportunity(_rust_opp(), odds_cache=cache)

    assert h["odds_match"] is cached


def test_missing_required_field_raises():
    opp = _rust_opp()
    del opp["token_id"]
    with pytest.raises(HydrationError):
        hydrate_opportunity(opp)


def test_empty_string_required_field_raises():
    opp = _rust_opp(hedge_team="")
    with pytest.raises(HydrationError):
        hydrate_opportunity(opp)


def test_none_start_time_allowed():
    # Python executor tolerates poly_event.start_time=None (no deadline).
    opp = _rust_opp(start_time=None)
    h = hydrate_opportunity(opp)
    assert h["poly_event"].start_time is None


def test_batch_drops_malformed_and_keeps_rest():
    good = _rust_opp()
    bad = _rust_opp(token_id="")
    other_good = _rust_opp(token_id="tok_c", team="Team C")

    out = hydrate_opportunities([good, bad, other_good])

    assert len(out) == 2
    assert out[0]["token_id"] == "tok_a"
    assert out[1]["token_id"] == "tok_c"


def test_batch_propagates_cache_to_each_opp():
    cached = SimpleNamespace(
        match_id="cs2:a:vs:b",
        team1="Team A",
        team2="Team B",
        game="cs2",
    )
    cache = {"cs2:a:vs:b": cached}
    out = hydrate_opportunities([_rust_opp()], odds_cache=cache)

    assert out[0]["odds_match"] is cached
