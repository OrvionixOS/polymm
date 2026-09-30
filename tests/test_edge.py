"""Tests for src/core/edge.py — the one place that decides edge, net of fees."""
import math

import pytest

from src.core.edge import (
    EdgeVerdict,
    KalshiFeeModel,
    ProportionalFeeModel,
    UnknownFeeModel,
    ZeroFeeModel,
    evaluate_edge,
    fee_free_threshold,
)

MIN_EDGE = 0.07  # config.py's sports min_edge


# ── The old behaviour, preserved where it was correct ──────────────────────

def test_zero_fee_venue_matches_the_old_inline_arithmetic():
    """On a zero-fee venue this must agree with `edge = fair - entry_price`,
    so wiring it into the scanners is a no-op on the international CLOB.
    """
    decision = evaluate_edge(0.65, 0.55, threshold=MIN_EDGE, fees=ZeroFeeModel())
    assert decision.gross_edge == pytest.approx(0.10)
    assert decision.net_edge == pytest.approx(0.10)
    assert decision.clears


def test_edge_below_threshold_is_rejected_with_the_shortfall_named():
    decision = evaluate_edge(0.60, 0.55, threshold=MIN_EDGE, fees=ZeroFeeModel())
    assert decision.verdict is EdgeVerdict.BELOW_THRESHOLD
    assert "short of threshold by 2.00c" in decision.detail


def test_exactly_at_threshold_clears():
    decision = evaluate_edge(0.62, 0.55, threshold=MIN_EDGE, fees=ZeroFeeModel())
    assert decision.net_edge == pytest.approx(0.07)
    assert decision.clears


# ── An unmodelled fee is never zero ────────────────────────────────────────

def test_unknown_fee_schedule_blocks_the_trade_rather_than_assuming_zero():
    """The whole point. A venue whose schedule is not established must not
    inherit Polymarket's free trading and book phantom edge.
    """
    decision = evaluate_edge(0.75, 0.55, threshold=MIN_EDGE, fees=UnknownFeeModel())
    assert decision.verdict is EdgeVerdict.FEE_UNKNOWN
    assert not decision.clears
    # The gross edge is still reported — it is the fee that is unknown.
    assert decision.gross_edge == pytest.approx(0.20)
    assert decision.net_edge is None
    assert "not established" in decision.detail


def test_unknown_fee_blocks_even_an_enormous_edge():
    decision = evaluate_edge(0.99, 0.02, threshold=MIN_EDGE, fees=UnknownFeeModel())
    assert not decision.clears


# ── Kalshi's fee curve ─────────────────────────────────────────────────────

def test_kalshi_maker_pays_nothing_on_a_standard_series():
    """This bot posts inside the spread, so the free side is the default."""
    decision = evaluate_edge(0.62, 0.55, threshold=MIN_EDGE,
                             fees=KalshiFeeModel(), is_maker=True)
    assert decision.fee_per_contract == pytest.approx(0.0)
    assert decision.clears


@pytest.mark.parametrize("price,expected_cents", [
    (0.50, 1.75), (0.30, 1.47), (0.70, 1.47),
    (0.10, 0.63), (0.90, 0.63), (0.20, 1.12),
])
def test_kalshi_taker_fee_curve(price, expected_cents):
    """0.07 x P x (1-P), peaking at 50c.

    Computed on 100 contracts so the round-up-to-the-next-cent does not mask
    the curve; the order total in dollars is then numerically equal to the
    per-contract cost in cents.
    """
    fee = KalshiFeeModel().total_fee(price, 100, is_maker=False)
    assert fee == pytest.approx(expected_cents, abs=0.01)


def test_kalshi_taker_fee_peaks_at_fifty_cents():
    model = KalshiFeeModel()
    at_50 = model.total_fee(0.50, 100, is_maker=False)
    for price in (0.05, 0.25, 0.45, 0.55, 0.75, 0.95):
        assert model.total_fee(price, 100, is_maker=False) < at_50


def test_kalshi_taker_fee_rounds_up_to_the_next_cent():
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> rounds up to 2c on a single contract.
    assert KalshiFeeModel().total_fee(0.50, 1, is_maker=False) == pytest.approx(0.02)


def test_kalshi_taker_fee_takes_a_quarter_of_a_seven_cent_edge_at_fifty_cents():
    decision = evaluate_edge(0.5875, 0.50, threshold=MIN_EDGE,
                             fees=KalshiFeeModel(), contracts=100, is_maker=False)
    assert decision.gross_edge == pytest.approx(0.0875)
    assert decision.fee_per_contract == pytest.approx(0.0175, abs=0.0001)
    assert decision.net_edge == pytest.approx(0.07, abs=0.0001)
    assert decision.clears  # only just


def test_taking_can_turn_a_clearing_trade_into_a_rejected_one():
    """Same prices, same threshold; only the maker/taker flag differs."""
    args = dict(threshold=MIN_EDGE, fees=KalshiFeeModel(), contracts=100)
    assert evaluate_edge(0.575, 0.50, is_maker=True, **args).clears
    assert not evaluate_edge(0.575, 0.50, is_maker=False, **args).clears


def test_round_trip_pays_the_fee_on_both_legs():
    """polymm hedges, so a hedged position is charged twice."""
    args = dict(threshold=MIN_EDGE, fees=KalshiFeeModel(), contracts=100, is_maker=False)
    one_leg = evaluate_edge(0.60, 0.50, legs=1, **args)
    two_legs = evaluate_edge(0.60, 0.50, legs=2, **args)
    assert two_legs.fee_per_contract == pytest.approx(one_leg.fee_per_contract * 2)
    assert one_leg.net_edge > two_legs.net_edge


def test_fees_can_consume_the_entire_edge():
    decision = evaluate_edge(0.51, 0.50, threshold=MIN_EDGE,
                             fees=KalshiFeeModel(), contracts=100,
                             is_maker=False, legs=2)
    assert decision.verdict is EdgeVerdict.NEGATIVE_AFTER_FEES
    assert decision.net_edge < 0
    assert "consume" in decision.detail


def test_the_draw_threshold_goes_underwater_at_mid_prices_when_taking():
    """config.py's draw_min_edge is 1.5c, under the 1.75c taker fee at 50c.

    A trade with exactly 1.5c of gross edge — the least that cleared the bar
    before fees existed — is now a loss, not merely a rejection.
    """
    decision = evaluate_edge(0.515, 0.50, threshold=0.015,
                             fees=KalshiFeeModel(), contracts=100, is_maker=False)
    assert decision.gross_edge == pytest.approx(0.015)
    assert decision.verdict is EdgeVerdict.NEGATIVE_AFTER_FEES
    assert decision.net_edge < 0


def test_a_draw_trade_that_clears_gross_but_not_net_is_reported_as_such():
    """2c gross against a 1.5c bar clears before fees and fails after."""
    args = dict(threshold=0.015, contracts=100)
    assert evaluate_edge(0.52, 0.50, fees=ZeroFeeModel(), **args).clears
    after = evaluate_edge(0.52, 0.50, fees=KalshiFeeModel(), is_maker=False, **args)
    assert not after.clears
    assert after.verdict is EdgeVerdict.BELOW_THRESHOLD
    assert after.net_edge == pytest.approx(0.0025, abs=0.0001)


# ── A proportional schedule, for a venue that publishes one ────────────────

def test_proportional_fee_is_charged_on_notional():
    fees = ProportionalFeeModel(rate=0.02)
    decision = evaluate_edge(0.70, 0.50, threshold=MIN_EDGE, fees=fees, contracts=10)
    assert decision.fee_per_contract == pytest.approx(0.01)  # 2% of 50c
    assert decision.net_edge == pytest.approx(0.19)


def test_proportional_model_can_price_makers_differently():
    fees = ProportionalFeeModel(rate=0.02, maker_rate=0.005)
    taker = evaluate_edge(0.70, 0.50, threshold=MIN_EDGE, fees=fees, is_maker=False)
    maker = evaluate_edge(0.70, 0.50, threshold=MIN_EDGE, fees=fees, is_maker=True)
    assert maker.fee_per_contract < taker.fee_per_contract


# ── Missing and invalid inputs ─────────────────────────────────────────────

def test_missing_fair_value_is_not_an_edge_of_zero():
    decision = evaluate_edge(None, 0.55, threshold=MIN_EDGE, fees=ZeroFeeModel())
    assert decision.verdict is EdgeVerdict.NO_FAIR_VALUE
    assert decision.gross_edge is None
    assert not decision.clears


@pytest.mark.parametrize("bad", [None, 0.0, 1.0, -0.1, 1.5])
def test_invalid_entry_price_is_refused(bad):
    decision = evaluate_edge(0.65, bad, threshold=MIN_EDGE, fees=ZeroFeeModel())
    assert decision.verdict is EdgeVerdict.INVALID_PRICE
    assert not decision.clears


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
def test_fair_value_outside_the_unit_interval_is_refused(bad):
    decision = evaluate_edge(bad, 0.55, threshold=MIN_EDGE, fees=ZeroFeeModel())
    assert decision.verdict is EdgeVerdict.NO_FAIR_VALUE


def test_zero_legs_is_a_programming_error():
    with pytest.raises(ValueError):
        evaluate_edge(0.65, 0.55, threshold=MIN_EDGE, fees=ZeroFeeModel(), legs=0)


def test_negative_gross_edge_is_reported_as_negative_not_merely_below():
    decision = evaluate_edge(0.50, 0.55, threshold=MIN_EDGE, fees=ZeroFeeModel())
    assert decision.verdict is EdgeVerdict.NEGATIVE_AFTER_FEES
    assert decision.gross_edge < 0


# ── Scanning helper ────────────────────────────────────────────────────────

def test_fee_free_threshold_raises_the_bar_by_the_fee():
    at_50 = fee_free_threshold(MIN_EDGE, 0.50, fees=KalshiFeeModel(), is_maker=False)
    assert at_50 == pytest.approx(MIN_EDGE + 0.02)  # rounded up on one contract


def test_fee_free_threshold_is_the_plain_threshold_on_a_zero_fee_venue():
    assert fee_free_threshold(MIN_EDGE, 0.50, fees=ZeroFeeModel()) == pytest.approx(MIN_EDGE)


def test_fee_free_threshold_is_unknown_when_the_schedule_is():
    assert fee_free_threshold(MIN_EDGE, 0.50, fees=UnknownFeeModel()) is None


# ── Reporting ──────────────────────────────────────────────────────────────

def test_decision_str_shows_the_whole_arithmetic():
    text = str(evaluate_edge(0.5875, 0.50, threshold=MIN_EDGE,
                             fees=KalshiFeeModel(), contracts=100, is_maker=False))
    assert "gross 8.75c" in text
    assert "fee 1.75c" in text
    assert "kalshi" in text


def test_decision_str_for_a_missing_fair_value_says_why():
    text = str(evaluate_edge(None, 0.55, threshold=MIN_EDGE, fees=ZeroFeeModel()))
    assert "no_fair_value" in text


# ── The config comment that prompted this ──────────────────────────────────

def test_config_min_profit_comment_now_has_code_behind_it():
    """config.py:82 said "10c minimum expected profit (after fees)" while no
    fee was subtracted anywhere. This is the arithmetic it described.
    """
    from src.core.config import SPREAD_CONFIG
    min_profit = SPREAD_CONFIG["min_profit"]
    decision = evaluate_edge(0.62, 0.50, threshold=min_profit,
                             fees=KalshiFeeModel(), contracts=100, is_maker=False)
    assert decision.fee_per_contract > 0
    assert decision.net_edge == pytest.approx(0.12 - 0.0175, abs=0.0001)
