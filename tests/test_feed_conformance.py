"""Tests for src/feeds/conformance.py — the spec a sports_odds_v2 producer must pass.

The point of the harness is to catch producer bugs that fail SILENTLY, so most
of these tests take a known-good row and break it in one specific way, then
assert the harness names that specific failure.
"""
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from src.feeds.conformance import (
    DRAW_FIELDS,
    MAX_AGE_HARD_REJECT_SECONDS,
    MAX_AGE_LIVE_SECONDS,
    MAX_AGE_PREMATCH_SECONDS,
    REQUIRED_FIELDS,
    UNIQUE_KEY,
    FeedVerdict,
    IssueCode,
    check_batch,
    check_freshness,
    check_row,
    measure_latency,
    parse_scraped_at,
)
from src.services.odds_service import AggregatedMatch
from src.scanning.team_matcher import MAX_ODDS_AGE_SECONDS


NOW = datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc)


def _two_way_row(**overrides):
    """A conforming 2-way row. Probabilities follow from the odds."""
    # 1/1.80 = .5556, 1/2.10 = .4762; total .10317 -> 53.85 / 46.15
    row = {
        "match_id": "basketball:bostonceltics:vs:miamiheat",
        "source": "candidate-feed",
        "sport": "basketball_nba",
        "team1": "Boston Celtics",
        "team2": "Miami Heat",
        "market_type": "h2h",
        "line": 0,
        "outcome1_name": "Boston Celtics",
        "outcome2_name": "Miami Heat",
        "odds1": 1.80,
        "odds2": 2.10,
        "fair_prob1": 53.85,
        "fair_prob2": 46.15,
        "bookmaker_count": 6,
        "is_live": False,
        "scraped_at": NOW.isoformat(),
    }
    row.update(overrides)
    return row


def _three_way_row(**overrides):
    """A conforming 3-way row. All three legs sum to 100."""
    # 1/2.10 + 1/3.40 + 1/3.60 = .47619 + .29412 + .27778 = 1.04809
    row = {
        "match_id": "football:arsenal:vs:manchestercity",
        "source": "candidate-feed",
        "sport": "soccer_epl",
        "team1": "Man City",
        "team2": "Arsenal",
        "market_type": "h2h",
        "line": 0,
        "outcome1_name": "Man City",
        "outcome2_name": "Arsenal",
        "outcome_draw_name": "Draw",
        "odds1": 2.10,
        "odds2": 3.60,
        "odds_draw": 3.40,
        "fair_prob1": 45.44,
        "fair_prob2": 26.50,
        "fair_prob_draw": 28.06,
        "bookmaker_count": 8,
        "is_live": False,
        "scraped_at": NOW.isoformat(),
    }
    row.update(overrides)
    return row


def _codes(issues):
    return {i.code for i in issues}


# ── Baseline: the fixtures must actually conform ───────────────────────────

def test_two_way_row_conforms():
    assert check_row(_two_way_row()) == ()


def test_three_way_row_conforms():
    assert check_row(_three_way_row()) == ()


def test_batch_of_conforming_rows_is_conforming():
    verdict = check_batch([_two_way_row(), _three_way_row()], now=NOW)
    assert verdict.conforming, verdict.summary()
    assert verdict.rows_checked == 2


# ── Gates are read from the enforcing code, not restated ───────────────────

def test_gates_track_the_modules_that_enforce_them():
    params = inspect.signature(AggregatedMatch.is_fresh).parameters
    assert MAX_AGE_LIVE_SECONDS == params["max_age_live"].default
    assert MAX_AGE_PREMATCH_SECONDS == params["max_age_prematch"].default
    assert MAX_AGE_HARD_REJECT_SECONDS == MAX_ODDS_AGE_SECONDS


# ── Identity: the silent killer ────────────────────────────────────────────

def test_match_id_disagreeing_with_make_match_id_is_caught():
    # Unsorted teams — plausible-looking, and it never matches a market.
    row = _two_way_row(match_id="basketball:miamiheat:vs:bostonceltics")
    issues = check_row(row)
    assert IssueCode.MATCH_ID_MISMATCH in _codes(issues)


def test_match_id_using_raw_sport_key_instead_of_game_is_caught():
    row = _two_way_row(match_id="basketball_nba:bostonceltics:vs:miamiheat")
    assert IssueCode.MATCH_ID_MISMATCH in _codes(check_row(row))


def test_match_id_without_normalization_is_caught():
    row = _two_way_row(match_id="basketball:Boston Celtics:vs:Miami Heat")
    assert IssueCode.MATCH_ID_MISMATCH in _codes(check_row(row))


# ── Absence is never a value ───────────────────────────────────────────────

@pytest.mark.parametrize("field_name", REQUIRED_FIELDS)
def test_every_required_field_is_required(field_name):
    row = _two_way_row()
    del row[field_name]
    assert IssueCode.MISSING_FIELD in _codes(check_row(row))


@pytest.mark.parametrize("field_name", REQUIRED_FIELDS)
def test_null_required_field_is_distinguished_from_absent(field_name):
    issues = check_row(_two_way_row(**{field_name: None}))
    codes = _codes(issues)
    assert IssueCode.NULL_FIELD in codes
    assert IssueCode.MISSING_FIELD not in codes


def test_missing_odds_is_not_treated_as_zero():
    """The read path coerces a missing numeric to 0.0; the harness must not."""
    row = _two_way_row()
    del row["odds1"]
    codes = _codes(check_row(row))
    assert IssueCode.MISSING_FIELD in codes
    # And it must NOT report 'odds <= 1' as if a zero had been observed.
    assert IssueCode.ODDS_IMPLAUSIBLE not in codes


def test_non_numeric_odds_is_caught():
    assert IssueCode.NOT_NUMERIC in _codes(check_row(_two_way_row(odds1="n/a")))


def test_numeric_string_odds_is_accepted():
    """Supabase returns DECIMAL columns as strings; that is not a defect."""
    assert check_row(_two_way_row(odds1="1.80", odds2="2.10")) == ()


# ── Probabilities must follow from the row's own odds ──────────────────────

def test_probabilities_that_do_not_follow_from_the_odds_are_caught():
    # Odds unchanged, probabilities from a different match.
    row = _two_way_row(fair_prob1=70.0, fair_prob2=30.0)
    assert IssueCode.DEVIG_DISAGREEMENT in _codes(check_row(row))


def test_equal_split_fallback_is_caught():
    """odds_api_service._build_h2h_record writes 33.33/33.33/33.34 when its own
    maths throws, and the reader accepts it because it is > 0. That is a
    fabricated probability entering the trading path; the harness rejects it.
    """
    row = _three_way_row(
        odds1=0, odds2=0, odds_draw=0,
        fair_prob1=33.33, fair_prob2=33.34, fair_prob_draw=33.33,
    )
    codes = _codes(check_row(row))
    assert IssueCode.ODDS_IMPLAUSIBLE in codes


def test_probabilities_not_summing_to_100_are_caught():
    # Vig left in: raw implied probabilities sum to >100.
    row = _two_way_row(fair_prob1=55.56, fair_prob2=47.62)
    assert IssueCode.PROB_SUM_INVALID in _codes(check_row(row))


def test_three_way_probabilities_must_include_the_draw_in_the_sum():
    row = _three_way_row(fair_prob1=63.15, fair_prob2=36.85, fair_prob_draw=28.06)
    assert IssueCode.PROB_SUM_INVALID in _codes(check_row(row))


def test_zero_probability_is_reported_as_dropped_not_as_valid():
    codes = _codes(check_row(_two_way_row(fair_prob1=0)))
    assert IssueCode.SILENTLY_DROPPED in codes
    assert IssueCode.PROB_OUT_OF_RANGE in codes


def test_probability_on_0_to_1_scale_is_caught():
    """A producer using 0-1 instead of 0-100 passes `> 0` and reads as ~0.5%."""
    row = _two_way_row(fair_prob1=0.5385, fair_prob2=0.4615)
    assert IssueCode.PROB_SUM_INVALID in _codes(check_row(row))


# ── Draw fields travel together ────────────────────────────────────────────

@pytest.mark.parametrize("dropped", DRAW_FIELDS)
def test_partial_draw_fields_are_caught(dropped):
    row = _three_way_row(**{dropped: None})
    assert IssueCode.DRAW_FIELDS_PARTIAL in _codes(check_row(row))


def test_two_way_row_with_no_draw_fields_is_fine():
    assert IssueCode.DRAW_FIELDS_PARTIAL not in _codes(check_row(_two_way_row()))


# ── Odds plausibility ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [1.0, 0.95, 0.0, -2.0])
def test_decimal_odds_at_or_below_one_are_caught(bad):
    assert IssueCode.ODDS_IMPLAUSIBLE in _codes(check_row(_two_way_row(odds1=bad)))


# ── Unique key collisions ──────────────────────────────────────────────────

def test_duplicate_unique_key_in_one_batch_is_caught():
    verdict = check_batch([_two_way_row(), _two_way_row()], now=NOW)
    assert IssueCode.SOURCE_COLLISION in {i.code for i in verdict.issues}


def test_same_match_from_different_sources_does_not_collide():
    rows = [_two_way_row(source="feed-a"), _two_way_row(source="feed-b")]
    verdict = check_batch(rows, now=NOW)
    assert verdict.conforming, verdict.summary()


def test_line_zero_as_int_and_float_collide():
    """The unique index treats 0 and 0.0 as one key; so must the harness."""
    rows = [_two_way_row(line=0), _two_way_row(line=0.0)]
    verdict = check_batch(rows, now=NOW)
    assert IssueCode.SOURCE_COLLISION in {i.code for i in verdict.issues}


def test_different_lines_do_not_collide():
    rows = [
        _two_way_row(market_type="totals", line=2.5, outcome1_name="Over",
                     outcome2_name="Under"),
        _two_way_row(market_type="totals", line=3.5, outcome1_name="Over",
                     outcome2_name="Under"),
    ]
    assert check_batch(rows, now=NOW).conforming


# ── Freshness ──────────────────────────────────────────────────────────────

def _aged(seconds, **overrides):
    return _two_way_row(scraped_at=(NOW - timedelta(seconds=seconds)).isoformat(), **overrides)


def test_live_row_inside_the_live_gate_is_fresh():
    assert check_freshness(_aged(MAX_AGE_LIVE_SECONDS - 1, is_live=True), now=NOW) == ()


def test_live_row_past_the_live_gate_is_stale():
    issues = check_freshness(_aged(MAX_AGE_LIVE_SECONDS + 1, is_live=True), now=NOW)
    assert _codes(issues) == {IssueCode.STALE_LIVE}


def test_row_exactly_at_the_live_gate_is_fresh():
    """`is_fresh` uses `age <= max_age`, so the boundary itself is fresh.
    Pinned because `>` vs `>=` here is a silent one-second disagreement with
    the gate the bot enforces.
    """
    assert check_freshness(_aged(MAX_AGE_LIVE_SECONDS, is_live=True), now=NOW) == ()


def test_row_exactly_at_the_hard_reject_is_accepted():
    """team_matcher rejects on `odds_age > max_age`, so the boundary passes."""
    assert check_freshness(_aged(MAX_AGE_HARD_REJECT_SECONDS), now=NOW) == ()


def test_row_exactly_at_the_prematch_gate_is_past_the_hard_reject():
    """The two gates disagree, and the stricter one binds.

    `is_fresh` would call an 1800s-old prematch row fresh, but team_matcher
    rejects anything over 600s, so it has no fair value. Both are reported.
    """
    codes = _codes(check_freshness(_aged(MAX_AGE_PREMATCH_SECONDS, is_live=False), now=NOW))
    assert IssueCode.STALE_HARD_REJECT in codes
    assert IssueCode.STALE_PREMATCH not in codes  # 1800 is the boundary, not past it


def test_prematch_row_between_the_two_gates_fails_only_fair_value():
    """600s < age < 1800s: the scanner still yields the match, but nothing
    can price it. This gap is a property of polymm's own constants, not of
    the candidate feed — worth seeing rather than hiding.
    """
    assert MAX_AGE_HARD_REJECT_SECONDS < MAX_AGE_PREMATCH_SECONDS
    midpoint = (MAX_AGE_HARD_REJECT_SECONDS + MAX_AGE_PREMATCH_SECONDS) / 2
    codes = _codes(check_freshness(_aged(midpoint, is_live=False), now=NOW))
    assert codes == {IssueCode.STALE_HARD_REJECT}


def test_very_old_prematch_row_fails_both_gates():
    codes = _codes(check_freshness(_aged(MAX_AGE_PREMATCH_SECONDS + 1, is_live=False), now=NOW))
    assert codes == {IssueCode.STALE_HARD_REJECT, IssueCode.STALE_PREMATCH}


def test_very_old_live_row_fails_both_gates():
    codes = _codes(check_freshness(_aged(MAX_AGE_HARD_REJECT_SECONDS + 1, is_live=True), now=NOW))
    assert codes == {IssueCode.STALE_HARD_REJECT, IssueCode.STALE_LIVE}


def test_prematch_row_past_the_live_gate_is_still_fresh():
    assert check_freshness(_aged(MAX_AGE_LIVE_SECONDS + 1, is_live=False), now=NOW) == ()


def test_prematch_row_just_past_the_hard_reject_is_rejected():
    issues = check_freshness(_aged(MAX_AGE_HARD_REJECT_SECONDS + 1), now=NOW)
    assert _codes(issues) == {IssueCode.STALE_HARD_REJECT}


def test_five_minute_polling_cannot_clear_the_live_gate():
    """The shipped the-odds-api producer polls every 300s against a 60s live
    gate. This is the hole the (absent) local scrapers filled, and the reason
    a replacement feed is the whole job.
    """
    poll_interval = 300
    issues = check_freshness(_aged(poll_interval, is_live=True), now=NOW)
    assert IssueCode.STALE_LIVE in _codes(issues)


def test_future_timestamp_is_caught():
    row = _two_way_row(scraped_at=(NOW + timedelta(seconds=30)).isoformat())
    assert IssueCode.TIMESTAMP_IN_FUTURE in _codes(check_freshness(row, now=NOW))


def test_unparseable_timestamp_is_caught_rather_than_read_as_now():
    row = _two_way_row(scraped_at="not-a-timestamp")
    assert IssueCode.TIMESTAMP_UNPARSEABLE in _codes(check_row(row))
    # And freshness must not claim it is fine.
    assert check_freshness(row, now=NOW) == ()


def test_naive_timestamp_is_read_as_utc():
    parsed = parse_scraped_at("2026-03-05T12:00:00")
    assert parsed == NOW


def test_z_suffix_timestamp_is_parsed():
    assert parse_scraped_at("2026-03-05T12:00:00Z") == NOW


def test_freshness_can_be_excluded_for_fixed_fixtures():
    old = _aged(MAX_AGE_HARD_REJECT_SECONDS * 10)
    assert check_batch([old], now=NOW, include_freshness=False).conforming


# ── Latency measurement ────────────────────────────────────────────────────

def test_latency_report_fails_the_live_gate_on_the_worst_live_row():
    rows = [
        _aged(5, is_live=True, source="a"),
        _aged(MAX_AGE_LIVE_SECONDS + 30, is_live=True, source="b"),
        _aged(10, is_live=False, source="c"),
    ]
    report = measure_latency(rows, now=NOW)
    assert report.rows == 3
    assert report.live_rows == 2
    assert not report.clears_live_gate
    assert report.worst_live_age == pytest.approx(MAX_AGE_LIVE_SECONDS + 30)


def test_latency_report_clears_the_gate_when_all_live_rows_are_fresh():
    rows = [_aged(5, is_live=True, source="a"), _aged(20, is_live=True, source="b")]
    assert measure_latency(rows, now=NOW).clears_live_gate


def test_no_live_rows_is_not_a_pass():
    """A feed that has never been measured on a live market has not proven
    anything; absence of evidence is not a cleared gate.
    """
    report = measure_latency([_aged(1), _aged(2)], now=NOW)
    assert report.live_rows == 0
    assert not report.clears_live_gate
    assert "unproven" in report.summary()


def test_unparseable_timestamps_are_excluded_not_counted_as_fresh():
    report = measure_latency([_two_way_row(scraped_at="garbage"), _aged(5)], now=NOW)
    assert report.rows == 1


def test_empty_input_measures_nothing():
    report = measure_latency([], now=NOW)
    assert report.rows == 0
    assert not report.clears_live_gate
    assert report.summary() == "no rows measured"


# ── Verdict reporting ──────────────────────────────────────────────────────

def test_verdict_summary_names_the_failure_classes():
    verdict = check_batch([_two_way_row(match_id="wrong")], now=NOW)
    assert not verdict.conforming
    assert IssueCode.MATCH_ID_MISMATCH.value in verdict.summary()


def test_verdict_by_code_filters():
    verdict = check_batch([_two_way_row(match_id="wrong")], now=NOW)
    assert len(verdict.by_code(IssueCode.MATCH_ID_MISMATCH)) == 1
    assert verdict.by_code(IssueCode.STALE_LIVE) == ()


def test_conforming_verdict_summary_is_explicit():
    assert "conforming" in FeedVerdict(rows_checked=3).summary()


def test_issue_str_includes_row_index():
    verdict = check_batch([_two_way_row(), _two_way_row(match_id="wrong")], now=NOW)
    mismatch = verdict.by_code(IssueCode.MATCH_ID_MISMATCH)[0]
    assert str(mismatch).startswith("row 1:")


def test_unique_key_matches_the_sql_index():
    assert UNIQUE_KEY == ("match_id", "market_type", "line", "source", "sport")


# ── The spec must agree with the reference producer ────────────────────────

def _bookmaker(key, home, away, draw=None):
    outcomes = [{"name": home[0], "price": home[1]}, {"name": away[0], "price": away[1]}]
    if draw:
        outcomes.append({"name": "Draw", "price": draw})
    return {"key": key, "markets": [{"key": "h2h", "outcomes": outcomes}]}


@pytest.fixture
def odds_api_service(monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "dummy-key-for-parse-only")
    from src.services.odds_api_service import OddsApiService
    return OddsApiService()


def test_reference_producer_output_conforms_three_way(odds_api_service):
    """`OddsApiService` is the one working producer in the tree. If the spec
    rejects its output, the spec is wrong — this is the test that keeps the
    harness honest rather than merely strict.
    """
    event = {
        "id": "evt-epl-1",
        "home_team": "Manchester City",
        "away_team": "Arsenal",
        "commence_time": "2026-03-05T15:00:00Z",
        "bookmakers": [
            _bookmaker("bk1", ("Manchester City", 2.10), ("Arsenal", 3.60), 3.40),
            _bookmaker("bk2", ("Manchester City", 2.05), ("Arsenal", 3.70), 3.45),
            _bookmaker("bk3", ("Manchester City", 2.15), ("Arsenal", 3.55), 3.35),
        ],
    }
    records = odds_api_service._parse_event(event, "soccer_epl", "football")
    assert records, "reference producer emitted nothing"
    rows = [r.to_dict() for r in records]

    verdict = check_batch(rows, include_freshness=False)
    assert verdict.conforming, verdict.summary()


def test_reference_producer_output_conforms_two_way(odds_api_service):
    event = {
        "id": "evt-nba-1",
        "home_team": "Boston Celtics",
        "away_team": "Miami Heat",
        "commence_time": "2026-03-05T19:00:00Z",
        "bookmakers": [
            _bookmaker("bk1", ("Boston Celtics", 1.80), ("Miami Heat", 2.10)),
            _bookmaker("bk2", ("Boston Celtics", 1.83), ("Miami Heat", 2.05)),
        ],
    }
    records = odds_api_service._parse_event(event, "basketball_nba", "basketball")
    assert records, "reference producer emitted nothing"
    rows = [r.to_dict() for r in records]

    verdict = check_batch(rows, include_freshness=False)
    assert verdict.conforming, verdict.summary()


def test_reference_producer_match_ids_survive_the_identity_check(odds_api_service):
    """Regression guard for the failure mode with no symptom: if normalization
    ever drifts between the producer and `make_match_id`, markets stop being
    matched and nothing logs it.
    """
    event = {
        "id": "evt-epl-2",
        "home_team": "Tottenham Hotspur",
        "away_team": "Wolverhampton Wanderers",
        "commence_time": "2026-03-06T15:00:00Z",
        "bookmakers": [
            _bookmaker("bk1", ("Tottenham Hotspur", 1.70), ("Wolverhampton Wanderers", 4.80), 4.00),
        ],
    }
    rows = [r.to_dict() for r in odds_api_service._parse_event(event, "soccer_epl", "football")]
    assert rows
    for row in rows:
        assert check_row(row) == ()
