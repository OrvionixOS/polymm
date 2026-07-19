"""Regression tests for analytics.position_breakdown.analyze_positions.

Guards the win/loss labeling of closed positions: the data-API reports
realizedPnl=0 for positions that have RESOLVED but aren't redeemed/settled yet.
The old `is_winner = realizedPnl >= 0` heuristic mislabeled those (including
outright losses) as "Win $0". Win/loss must come from the resolution price.
"""
import pytest

from analytics.position_breakdown import analyze_positions


def _closed(cid, outcome, cur, realized, bought=10.0, avg=0.5):
    return {
        "conditionId": cid, "outcome": outcome,
        "title": f"CS2: {outcome} vs Opp", "totalBought": bought,
        "avgPrice": avg, "curPrice": cur, "realizedPnl": realized,
        "endDate": "2026-06-26T12:00:00Z",
    }


def _specs(closed):
    return analyze_positions({"open": [], "closed": closed}, {})["specs"]


def test_resolved_loss_with_zero_realized_pnl_is_a_loss():
    # curPrice=0 → the held outcome lost; realizedPnl=0 because it's unredeemed.
    (spec,) = _specs([_closed("c1", "Loser", cur=0.0, realized=0.0)])
    assert spec["is_winner"] is False
    assert spec["pnl"] == pytest.approx(-5.0)  # -cost (10 * 0.5)


def test_resolved_win_with_zero_realized_pnl_uses_resolution_value():
    # curPrice=1 → won; realizedPnl still 0 (unredeemed) → fall back to payout.
    (spec,) = _specs([_closed("c2", "Winner", cur=1.0, realized=0.0)])
    assert spec["is_winner"] is True
    assert spec["pnl"] == pytest.approx(5.0)  # shares(10) - cost(5)


def test_realized_pnl_used_when_nonzero():
    (spec,) = _specs([_closed("c3", "Sold", cur=1.0, realized=7.25)])
    assert spec["is_winner"] is True
    assert spec["pnl"] == pytest.approx(7.25)


def test_no_position_is_labeled_win_with_zero_pnl():
    """The exact bug: a 'Win' must never have $0 P&L from realizedPnl=0."""
    closed = [
        _closed("a", "L1", cur=0.0, realized=0.0),
        _closed("b", "W1", cur=1.0, realized=0.0),
        _closed("c", "L2", cur=0.0, realized=0.0, bought=3.0, avg=0.4),
    ]
    specs = _specs(closed)
    assert not any(s["is_winner"] and abs(s["pnl"]) < 1e-9 for s in specs)
    assert sum(1 for s in specs if s["is_winner"]) == 1   # only W1
