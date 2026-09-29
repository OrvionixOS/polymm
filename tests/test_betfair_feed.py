"""Tests for the Betfair Exchange Stream producer.

Everything here runs without credentials. The free Delayed App Key and the
paid Live App Key deliver the same message format at different conflation
rates, so the producer can be fully verified before any money is spent — the
key only changes how old `pt` is.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.feeds.betfair.producer import (
    MARKET_TYPE_MAP,
    SOURCE,
    MarketNames,
    SkipReason,
    mid_probability,
    parse_event_teams,
    polymm_market,
    rows_for_market,
    rows_from_state,
)
from src.feeds.betfair.stream_state import (
    MAX_PRICE,
    MIN_PRICE,
    RunnerBook,
    StreamState,
)
from src.feeds.conformance import IssueCode, check_batch, check_freshness, measure_latency

NOW = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)
PT = int(NOW.timestamp() * 1000)
MARKET = "1.234567"


def _match_odds_message(*, pt=PT, in_play=False, status="OPEN", conflate=180000):
    """A MATCH_ODDS initial image: Man City / Arsenal / The Draw."""
    return {
        "op": "mcm", "clk": "AAAA", "initialClk": "INIT", "pt": pt,
        "conflateMs": conflate,
        "mc": [{
            "id": MARKET, "img": True,
            "marketDefinition": {
                "marketType": "MATCH_ODDS", "status": status, "inPlay": in_play,
                "eventId": "32145678", "eventTypeId": "1",
                "marketTime": "2026-03-05T15:00:00.000Z",
                "runners": [
                    {"id": 111, "status": "ACTIVE", "sortPriority": 1},
                    {"id": 222, "status": "ACTIVE", "sortPriority": 2},
                    {"id": 333, "status": "ACTIVE", "sortPriority": 3},
                ],
            },
            "rc": [
                {"id": 111, "atb": [[2.08, 400], [2.06, 900]], "atl": [[2.12, 350]]},
                {"id": 222, "atb": [[3.55, 220]], "atl": [[3.65, 180]]},
                {"id": 333, "atb": [[3.35, 500]], "atl": [[3.45, 260]]},
            ],
        }],
    }


_NAMES = MarketNames(
    event_name="Man City v Arsenal",
    runner_names={111: "Man City", 222: "Arsenal", 333: "The Draw"},
)


def _state(message=None):
    state = StreamState()
    state.apply(message or _match_odds_message())
    return state


def _row(state=None, names=_NAMES, game="football"):
    state = state or _state()
    row, skip = rows_for_market(state.markets[MARKET], names, game=game)
    return row, skip


# ── The round trip must satisfy the conformance spec ───────────────────────

def test_betfair_row_conforms():
    row, skip = _row()
    assert skip is None
    verdict = check_batch([row], include_freshness=False)
    assert verdict.conforming, verdict.summary()


def test_betfair_row_match_id_is_canonical():
    row, _ = _row()
    assert row["match_id"] == "football:arsenal:vs:manchestercity"


def test_sport_column_must_map_back_to_the_game():
    """`sport` is written as the canonical game name on purpose: odds_service
    maps it through SPORT_KEY_TO_GAME with a raw fallback, and make_match_id
    must receive the mapped result. A Betfair competition name here would
    fall through unmapped and the market would never be matched.
    """
    from src.services.odds_api_service import SPORT_KEY_TO_GAME
    row, _ = _row()
    assert SPORT_KEY_TO_GAME.get(row["sport"], row["sport"]) == "football"
    assert check_batch([row], include_freshness=False).conforming


def test_three_way_probabilities_sum_to_100():
    row, _ = _row()
    total = row["fair_prob1"] + row["fair_prob2"] + row["fair_prob_draw"]
    assert total == pytest.approx(100.0, abs=0.1)


def test_stored_odds_reproduce_the_stored_probabilities():
    """The invariant the conformance spec checks: proportional vig removal
    over the stored odds must give back the stored probabilities. That is why
    odds1 is 1/mid-probability and not the raw back price.
    """
    row, _ = _row()
    legs = [1 / row["odds1"], 1 / row["odds2"], 1 / row["odds_draw"]]
    total = sum(legs)
    assert legs[0] / total * 100 == pytest.approx(row["fair_prob1"], abs=0.1)
    assert legs[1] / total * 100 == pytest.approx(row["fair_prob2"], abs=0.1)
    assert legs[2] / total * 100 == pytest.approx(row["fair_prob_draw"], abs=0.1)
    assert IssueCode.DEVIG_DISAGREEMENT not in {
        i.code for i in check_batch([row], include_freshness=False).issues
    }


def test_betfair_and_the_odds_api_rows_do_not_collide():
    """Different `source` values, so both producers coexist in the table."""
    betfair, _ = _row()
    odds_api = dict(betfair, source="the-odds-api")
    assert check_batch([betfair, odds_api], include_freshness=False).conforming


def test_source_is_distinct():
    row, _ = _row()
    assert row["source"] == SOURCE != "the-odds-api"


# ── Publish time is the only honest timestamp ──────────────────────────────

def test_scraped_at_comes_from_publish_time_not_the_local_clock():
    """A delayed key's conflation must be VISIBLE to the freshness gates.
    Stamping the local clock would present 180s-old data as fresh.
    """
    old_pt = int((NOW - timedelta(seconds=180)).timestamp() * 1000)
    row, _ = _row(_state(_match_odds_message(pt=old_pt)))
    assert row["scraped_at"].startswith("2026-03-05T11:57:00")
    assert measure_latency([row], now=NOW).worst_age == pytest.approx(180.0)


def test_delayed_key_latency_is_usable_prematch():
    """180s conflation clears the 600s fair-value reject."""
    old_pt = int((NOW - timedelta(seconds=180)).timestamp() * 1000)
    row, _ = _row(_state(_match_odds_message(pt=old_pt, in_play=False)))
    assert check_freshness(row, now=NOW) == ()


def test_delayed_key_latency_fails_the_live_gate():
    """And it does not clear the 60s live gate. This is the cost of the free
    key, stated as a test rather than a hope.
    """
    old_pt = int((NOW - timedelta(seconds=180)).timestamp() * 1000)
    row, _ = _row(_state(_match_odds_message(pt=old_pt, in_play=True)))
    codes = {i.code for i in check_freshness(row, now=NOW)}
    assert IssueCode.STALE_LIVE in codes


def test_live_key_latency_clears_the_live_gate():
    fresh_pt = int((NOW - timedelta(seconds=2)).timestamp() * 1000)
    row, _ = _row(_state(_match_odds_message(pt=fresh_pt, in_play=True, conflate=0)))
    assert check_freshness(row, now=NOW) == ()
    assert measure_latency([row], now=NOW).clears_live_gate


def test_market_without_publish_time_is_skipped():
    message = _match_odds_message()
    del message["pt"]
    row, skip = _row(_state(message))
    assert row is None
    assert skip.reason is SkipReason.NO_PUBLISH_TIME


def test_conflate_rate_from_the_stream_reveals_which_key_is_in_use():
    assert _state().last_conflate_ms == 180000
    assert _state(_match_odds_message(conflate=0)).last_conflate_ms == 0


def test_in_play_flag_is_carried_through():
    row, _ = _row(_state(_match_odds_message(in_play=True)))
    assert row["is_live"] is True


# ── Ladder deltas ──────────────────────────────────────────────────────────

def test_best_back_is_the_highest_and_best_lay_the_lowest():
    runner = RunnerBook(selection_id=1)
    runner.apply_full("back", [[2.00, 10], [2.02, 5], [1.98, 20]])
    runner.apply_full("lay", [[2.06, 10], [2.04, 5], [2.10, 20]])
    assert runner.best_back() == 2.02
    assert runner.best_lay() == 2.04


def test_zero_size_removes_a_price_level_rather_than_meaning_zero_money():
    runner = RunnerBook(selection_id=1)
    runner.apply_full("back", [[2.00, 10], [2.02, 5]])
    assert runner.best_back() == 2.02
    runner.apply_full("back", [[2.02, 0]])
    assert runner.best_back() == 2.00


def test_removing_every_level_leaves_no_price():
    runner = RunnerBook(selection_id=1)
    runner.apply_full("back", [[2.00, 10]])
    runner.apply_full("back", [[2.00, 0]])
    assert runner.best_back() is None


def test_deltas_accumulate_across_messages():
    state = _state()
    state.apply({
        "op": "mcm", "pt": PT + 1000,
        "mc": [{"id": MARKET, "rc": [{"id": 111, "atb": [[2.10, 50]]}]}],
    })
    assert state.markets[MARKET].runners[111].best_back() == 2.10


def test_image_replaces_prices_instead_of_merging():
    """Merging an image into stale state resurrects removed price levels."""
    state = _state()
    assert state.markets[MARKET].runners[111].best_back() == 2.08
    state.apply({
        "op": "mcm", "pt": PT + 1000,
        "mc": [{
            "id": MARKET, "img": True,
            "rc": [{"id": 111, "atb": [[1.90, 100]], "atl": [[1.95, 100]]}],
        }],
    })
    runner = state.markets[MARKET].runners[111]
    assert runner.best_back() == 1.90  # not 2.08
    assert runner.best_lay() == 1.95


def test_best_offer_level_format_is_supported():
    """batb/batl are (level, price, size) triples; level 0 is best."""
    runner = RunnerBook(selection_id=1)
    runner.apply_level("back", [[0, 2.02, 10], [1, 2.00, 20]])
    runner.apply_level("lay", [[0, 2.04, 10], [1, 2.06, 20]])
    assert runner.best_back() == 2.02
    assert runner.best_lay() == 2.04


def test_best_offer_level_zero_size_clears_that_level():
    runner = RunnerBook(selection_id=1)
    runner.apply_level("back", [[0, 2.02, 10], [1, 2.00, 20]])
    runner.apply_level("back", [[0, 0, 0]])
    assert runner.best_back() == 2.00


def test_full_ladder_takes_precedence_over_best_offers():
    runner = RunnerBook(selection_id=1)
    runner.apply_level("back", [[0, 1.50, 10]])
    runner.apply_full("back", [[2.02, 10]])
    assert runner.best_back() == 2.02


@pytest.mark.parametrize("price", [1.0, 0.5, 0.0, MAX_PRICE + 1, -2.0])
def test_prices_outside_the_betfair_ladder_are_ignored(price):
    runner = RunnerBook(selection_id=1)
    runner.apply_full("back", [[price, 100]])
    assert runner.best_back() is None


def test_ladder_bounds_themselves_are_tradeable():
    runner = RunnerBook(selection_id=1)
    runner.apply_full("back", [[MIN_PRICE, 10], [MAX_PRICE, 10]])
    assert runner.best_back() == MAX_PRICE


# ── Message plumbing ───────────────────────────────────────────────────────

def test_heartbeat_carries_no_market_change():
    state = _state()
    changed = state.apply({"op": "mcm", "ct": "HEARTBEAT", "pt": PT + 5000, "clk": "BBBB"})
    assert changed == ()
    assert state.last_publish_time_ms == PT + 5000
    assert state.last_clk == "BBBB"  # still tracked, or a resume replays wrongly


def test_non_market_operations_are_ignored():
    state = _state()
    assert state.apply({"op": "connection", "connectionId": "x"}) == ()
    assert state.apply({"op": "ocm", "pt": PT}) == ()


def test_resume_tokens_are_tracked():
    state = _state()
    assert state.initial_clk == "INIT"
    assert state.last_clk == "AAAA"


def test_stream_status_is_recorded():
    state = _state()
    state.apply({"op": "mcm", "pt": PT, "status": 503})
    assert state.stream_status == 503


def test_market_change_without_an_id_is_ignored():
    state = StreamState()
    assert state.apply({"op": "mcm", "pt": PT, "mc": [{"img": True}]}) == ()
    assert state.markets == {}


# ── Refusals ───────────────────────────────────────────────────────────────

def test_one_sided_book_is_refused():
    """A runner quoted on one side only has no midpoint, and pricing off the
    single available side is exactly how you get picked off.
    """
    message = _match_odds_message()
    message["mc"][0]["rc"][1] = {"id": 222, "atb": [[3.55, 220]]}  # no lay
    row, skip = _row(_state(message))
    assert row is None
    assert skip.reason is SkipReason.ONE_SIDED_BOOK
    assert "222" in skip.detail


@pytest.mark.parametrize("status", ["SUSPENDED", "CLOSED", "INACTIVE"])
def test_only_open_markets_are_quoted(status):
    row, skip = _row(_state(_match_odds_message(status=status)))
    assert row is None
    assert skip.reason is SkipReason.NOT_OPEN


def test_removed_runner_is_excluded_from_normalization():
    """A REMOVED selection's prices linger but its outcome cannot happen;
    including it would deflate every remaining probability.
    """
    message = _match_odds_message()
    message["mc"][0]["marketDefinition"]["runners"][2]["status"] = "REMOVED"
    row, skip = _row(_state(message))
    assert skip is None
    assert "outcome_draw_name" not in row
    assert row["fair_prob1"] + row["fair_prob2"] == pytest.approx(100.0, abs=0.1)
    assert check_batch([row], include_freshness=False).conforming


def test_unknown_market_type_is_refused():
    message = _match_odds_message()
    message["mc"][0]["marketDefinition"]["marketType"] = "ASIAN_HANDICAP"
    row, skip = _row(_state(message))
    assert row is None
    assert skip.reason is SkipReason.UNKNOWN_MARKET_TYPE


def test_market_without_catalogue_names_is_refused():
    row, skip = _row(names=None)
    assert row is None
    assert skip.reason is SkipReason.NAMES_UNAVAILABLE


def test_unnamed_runner_is_refused():
    names = MarketNames(event_name="Man City v Arsenal",
                        runner_names={111: "Man City", 222: "Arsenal"})
    row, skip = _row(names=names)
    assert row is None
    assert skip.reason is SkipReason.UNNAMED_RUNNER
    assert "333" in skip.detail


def test_three_runners_without_a_draw_is_refused():
    names = MarketNames(event_name="A v B",
                        runner_names={111: "A", 222: "B", 333: "C"})
    row, skip = _row(names=names)
    assert row is None
    assert skip.reason is SkipReason.NO_DRAW_RUNNER


def test_unexpected_runner_count_is_refused():
    message = _match_odds_message()
    message["mc"][0]["marketDefinition"]["runners"].append(
        {"id": 444, "status": "ACTIVE", "sortPriority": 4}
    )
    message["mc"][0]["rc"].append(
        {"id": 444, "atb": [[10.0, 5]], "atl": [[11.0, 5]]}
    )
    row, skip = _row(_state(message))
    assert row is None
    assert skip.reason is SkipReason.WRONG_RUNNER_COUNT


# ── Market type and event name mapping ─────────────────────────────────────

@pytest.mark.parametrize("betfair_type,expected", [
    ("MATCH_ODDS", ("h2h", 0.0)),
    ("BOTH_TEAMS_TO_SCORE", ("btts", 0.0)),
    ("OVER_UNDER_25", ("totals", 2.5)),
    ("OVER_UNDER_05", ("totals", 0.5)),
    ("OVER_UNDER_35", ("totals", 3.5)),
    ("OVER_UNDER_105", ("totals", 10.5)),
])
def test_market_type_mapping(betfair_type, expected):
    assert polymm_market(betfair_type) == expected


@pytest.mark.parametrize("unsupported", [
    "ASIAN_HANDICAP", "CORRECT_SCORE", "OVER_UNDER_X", "", "HALF_TIME",
])
def test_unsupported_market_types_map_to_nothing(unsupported):
    assert polymm_market(unsupported) is None


@pytest.mark.parametrize("event_name,expected", [
    ("Man City v Arsenal", ("Man City", "Arsenal")),
    ("Man City vs Arsenal", ("Man City", "Arsenal")),
    ("Man City vs. Arsenal", ("Man City", "Arsenal")),
    # US convention reverses the sides: "Away @ Home"
    ("Miami Heat @ Boston Celtics", ("Boston Celtics", "Miami Heat")),
])
def test_event_name_parsing(event_name, expected):
    assert parse_event_teams(event_name) == expected


@pytest.mark.parametrize("bad", ["", "Man City", "v Arsenal", "Man City v ", "   "])
def test_unparseable_event_name_returns_none_rather_than_guessing(bad):
    assert parse_event_teams(bad) is None


def test_totals_market_takes_teams_from_the_event_name():
    message = _match_odds_message()
    message["mc"][0]["marketDefinition"]["marketType"] = "OVER_UNDER_25"
    message["mc"][0]["marketDefinition"]["runners"] = [
        {"id": 111, "status": "ACTIVE", "sortPriority": 1},
        {"id": 222, "status": "ACTIVE", "sortPriority": 2},
    ]
    message["mc"][0]["rc"] = [
        {"id": 111, "atb": [[1.80, 100]], "atl": [[1.84, 100]]},
        {"id": 222, "atb": [[2.06, 100]], "atl": [[2.12, 100]]},
    ]
    names = MarketNames(event_name="Man City v Arsenal",
                        runner_names={111: "Over 2.5 Goals", 222: "Under 2.5 Goals"})
    row, skip = rows_for_market(_state(message).markets[MARKET], names, game="football")
    assert skip is None
    assert row["market_type"] == "totals"
    assert row["line"] == 2.5
    assert row["outcome1_name"] == "Over 2.5 Goals"
    assert row["match_id"] == "football:arsenal:vs:manchestercity"
    assert check_batch([row], include_freshness=False).conforming


def test_totals_market_with_unparseable_event_name_is_refused():
    message = _match_odds_message()
    message["mc"][0]["marketDefinition"]["marketType"] = "OVER_UNDER_25"
    message["mc"][0]["marketDefinition"]["runners"] = [
        {"id": 111, "status": "ACTIVE", "sortPriority": 1},
        {"id": 222, "status": "ACTIVE", "sortPriority": 2},
    ]
    message["mc"][0]["rc"] = [
        {"id": 111, "atb": [[1.80, 100]], "atl": [[1.84, 100]]},
        {"id": 222, "atb": [[2.06, 100]], "atl": [[2.12, 100]]},
    ]
    names = MarketNames(event_name="a strange title",
                        runner_names={111: "Over 2.5 Goals", 222: "Under 2.5 Goals"})
    row, skip = rows_for_market(_state(message).markets[MARKET], names, game="football")
    assert row is None
    assert skip.reason is SkipReason.TEAMS_UNPARSEABLE


def test_match_odds_map_is_not_accidentally_empty():
    assert "MATCH_ODDS" in MARKET_TYPE_MAP


# ── Probability maths ──────────────────────────────────────────────────────

def test_mid_probability_sits_between_the_two_reciprocals():
    back, lay = 2.00, 2.02
    mid = mid_probability(back, lay)
    assert 1 / lay < mid < 1 / back


def test_mid_probability_of_a_locked_market_is_the_price_itself():
    assert mid_probability(2.00, 2.00) == pytest.approx(0.5)


@pytest.mark.parametrize("back,lay", [(1.0, 2.0), (2.0, 1.0), (0.0, 2.0)])
def test_mid_probability_refuses_untradeable_prices(back, lay):
    assert mid_probability(back, lay) is None


# ── Batch construction ─────────────────────────────────────────────────────

def test_rows_from_state_reports_rows_and_refusals():
    state = _state()
    state.apply({
        "op": "mcm", "pt": PT,
        "mc": [{
            "id": "1.999", "img": True,
            "marketDefinition": {
                "marketType": "CORRECT_SCORE", "status": "OPEN", "inPlay": False,
                "runners": [],
            },
        }],
    })
    result = rows_from_state(
        state,
        {MARKET: _NAMES},
        game_by_market={MARKET: "football", "1.999": "football"},
    )
    assert len(result.rows) == 1
    assert len(result.skipped) == 1
    assert result.skipped[0].reason is SkipReason.UNKNOWN_MARKET_TYPE
    assert "unknown_market_type" in result.summary()


def test_rows_from_state_output_conforms_as_a_batch():
    result = rows_from_state(_state(), {MARKET: _NAMES},
                             game_by_market={MARKET: "football"})
    verdict = check_batch(result.rows, include_freshness=False)
    assert verdict.conforming, verdict.summary()


def test_market_without_a_game_mapping_is_skipped():
    result = rows_from_state(_state(), {MARKET: _NAMES}, game_by_market={})
    assert result.rows == ()
    assert result.skipped[0].reason is SkipReason.NAMES_UNAVAILABLE


# ── Guards the first mutation sweep found unpinned ─────────────────────────

def test_mid_is_taken_in_probability_space_not_odds_space():
    """On a wide spread the two are materially different.

    back=2.00/lay=3.00 gives 41.67% in probability space but 40.00% from
    averaging the odds — 1.67pp, against a 7% edge threshold. Averaging odds
    biases toward the longer price, so wide books would be systematically
    mispriced in the direction that looks like edge.
    """
    assert mid_probability(2.00, 3.00) == pytest.approx(0.41667, abs=1e-5)
    assert mid_probability(2.00, 3.00) != pytest.approx(1 / 2.5, abs=1e-4)
    # Tight spreads agree, which is why only a wide one pins this.
    assert mid_probability(2.00, 2.02) == pytest.approx(0.497525, abs=1e-6)


def test_stored_odds_are_the_tradeable_midpoint_and_keep_the_spread():
    """`odds1` must be the price the fair value came from, not 1/fair_prob.

    Betfair's back/lay spread makes the raw midpoint probabilities sum to
    slightly over 1. Storing 1/normalized_probability would erase that
    overround: the de-vig invariant would still hold (normalized probs
    trivially re-normalize to themselves), but `odds1` would become a
    synthetic price no one can trade at, and slightly longer than reality.
    """
    row, _ = _row()
    overround = 1 / row["odds1"] + 1 / row["odds2"] + 1 / row["odds_draw"]
    assert overround > 1.0, "stored odds lost the exchange spread"
    # And the fair probabilities, unlike the odds, do sum to 100.
    assert row["fair_prob1"] + row["fair_prob2"] + row["fair_prob_draw"] == pytest.approx(
        100.0, abs=0.1
    )


def test_stored_odds_match_the_actual_book_midpoint():
    """Runner 111: back 2.08 / lay 2.12 -> mid prob -> 1/mid as the price."""
    row, _ = _row()
    expected = 1 / mid_probability(2.08, 2.12)
    assert row["odds1"] == pytest.approx(expected, abs=0.002)


def test_heartbeat_carrying_market_data_is_ignored():
    """Per the schema `mc` is null on a heartbeat, so a heartbeat with `mc`
    is malformed. Ignoring it keeps a malformed frame from mutating the book.
    """
    state = _state()
    before = state.markets[MARKET].runners[111].best_back()
    changed = state.apply({
        "op": "mcm", "ct": "HEARTBEAT", "pt": PT + 5000,
        "mc": [{"id": MARKET, "img": True,
                "rc": [{"id": 111, "atb": [[1.01, 999]], "atl": [[1.02, 999]]}]}],
    })
    assert changed == ()
    assert state.markets[MARKET].runners[111].best_back() == before
