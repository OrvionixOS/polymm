"""Regression tests for analytics.underdog_analysis.

The bug these guard against: underdog_analysis read ONLY /v1/closed-positions,
which books realized P&L from SELLS — so it is winner-biased (losing legs sit
in /positions at ~0 and never appear there). Summing it alone over-stated P&L
~3x (+$17k vs the true ~$5k). The fix delegates economics to
capital_analysis.analyze_portfolio (ledger-aware, symmetric) and classifies its
per-leg win/loss details into favorite/underdog buckets.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics import capital_analysis
from analytics.underdog_analysis import analyze_underdog_performance


@pytest.fixture(autouse=True)
def _no_supabase(monkeypatch):
    """analyze_portfolio calls analyze_prediction_accuracy (hits Supabase).
    Stub it so these stay pure-unit."""
    monkeypatch.setattr(capital_analysis, "analyze_prediction_accuracy",
                        lambda *a, **k: {"message": "stubbed"})


def _arb_market():
    """One resolved binary market where the favorite (0.70) WON and the
    underdog (0.30) LOST — a perfect hedge that should net to $0.

    Winner leg appears in /closed-positions (realizedPnl booked from the exit);
    loser leg sits in /positions at curPrice 0. This is exactly the asymmetry
    the old code mishandled.
    """
    closed = [{
        "conditionId": "0xARB", "outcome": "TeamFav", "title": "CS2: Fav vs Dog",
        "realizedPnl": 30.0, "totalBought": 100.0, "avgPrice": 0.70,
        "curPrice": 1.0, "endDate": "2026-03-01", "eventSlug": "fav-dog",
    }]
    positions = [{
        "conditionId": "0xARB", "outcome": "TeamDog", "oppositeOutcome": "TeamFav",
        "title": "CS2: Fav vs Dog", "asset": "tok-dog", "size": 100.0,
        "avgPrice": 0.30, "curPrice": 0.0, "cashPnl": -30.0, "redeemable": False,
        "endDate": "2026-03-01", "eventSlug": "fav-dog",
    }]
    return {"positions": positions, "closed_positions": closed, "activity": []}


def test_losing_leg_is_counted_not_just_the_winner():
    """The core fix: the underdog's losing leg must be counted, so the arb nets
    to ~$0 instead of the winner-only +$30."""
    res = analyze_underdog_performance(_arb_market(), [])
    assert res["total_pnl"] == pytest.approx(0.0, abs=0.01)
    assert res["favorites"]["wins"] == 1
    assert res["favorites"]["total_pnl"] == pytest.approx(30.0, abs=0.01)
    assert res["underdogs"]["losses"] == 1
    assert res["underdogs"]["total_pnl"] == pytest.approx(-30.0, abs=0.01)


def test_both_legs_detected_as_hedged():
    """Both legs share a conditionId, so the symmetric set marks them hedged."""
    res = analyze_underdog_performance(_arb_market(), [])
    assert res["hedged"]["count"] == 2
    assert res["unhedged"]["count"] == 0


def test_window_filter_excludes_out_of_range_enddate():
    """A leg whose endDate is outside [start, end) must be dropped."""
    from datetime import datetime, timezone
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    # the arb market resolved 2026-03-01 -> before the window -> excluded
    res = analyze_underdog_performance(_arb_market(), [], start=start, end=end)
    assert res.get("total", 0) == 0


def test_no_phantom_inflation_from_winner_only_feed():
    """Sanity: with the winner present but NO loser leg anywhere, total is the
    winner's real P&L — never doubled or imputed from a missing leg."""
    data = _arb_market()
    data["positions"] = []  # drop the loser leg entirely
    res = analyze_underdog_performance(data, [])
    assert res["total_pnl"] == pytest.approx(30.0, abs=0.01)
    assert res["underdogs"].get("count", 0) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
