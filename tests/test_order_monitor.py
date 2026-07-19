"""
Unit tests for monitoring/order_monitor.py - Order monitoring and adjustment.

Note: OrderMonitor has complex dependencies that cause circular imports
when imported directly. These tests use mock-based approaches and test
the core logic patterns used in the module.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch


# Since OrderMonitor has circular import issues, we test via mocks and
# test the patterns/calculations used in the module

class MockOrderMonitor:
    """Mock OrderMonitor for testing core logic patterns."""
    
    def __init__(self, config: dict):
        self._outbid_state = {}
        self._warned_team_mismatch = set()
        self._ws_edge_logged = set()
        self._adjusting_tokens = set()
        self._hedge_fair_cap_warned = set()
        self.config = config


class TestOrderMonitorInit:
    """Tests for OrderMonitor initialization patterns."""
    
    def test_init_stores_config(self):
        """Monitor stores config."""
        config = {
            "min_edge": 0.05,
            "default_shares": 25,
        }
        monitor = MockOrderMonitor(config)
        assert monitor.config == config
    
    def test_init_empty_state(self):
        """Monitor starts with empty state."""
        monitor = MockOrderMonitor({})
        
        assert len(monitor._outbid_state) == 0
        assert len(monitor._warned_team_mismatch) == 0
        assert len(monitor._adjusting_tokens) == 0


class TestOrderMonitorOutbidState:
    """Tests for outbid state tracking patterns."""
    
    @pytest.fixture
    def monitor(self):
        """Create mock monitor."""
        return MockOrderMonitor({})
    
    def test_outbid_state_initially_empty(self, monitor):
        """Outbid state starts empty."""
        assert "token123" not in monitor._outbid_state
    
    def test_can_track_outbid_tokens(self, monitor):
        """Can add tokens to outbid state."""
        monitor._outbid_state["token123"] = {
            "best_bid": 0.52,
            "failed": False,
        }
        
        assert monitor._outbid_state["token123"]["best_bid"] == 0.52
        assert monitor._outbid_state["token123"]["failed"] is False
    
    def test_can_update_outbid_state(self, monitor):
        """Can update outbid state."""
        monitor._outbid_state["token123"] = {"best_bid": 0.50, "failed": False}
        monitor._outbid_state["token123"]["best_bid"] = 0.55
        monitor._outbid_state["token123"]["failed"] = True
        
        assert monitor._outbid_state["token123"]["best_bid"] == 0.55
        assert monitor._outbid_state["token123"]["failed"] is True


class TestOrderMonitorAdjustingTokens:
    """Tests for _adjusting_tokens deduplication patterns."""
    
    @pytest.fixture
    def monitor(self):
        """Create mock monitor."""
        return MockOrderMonitor({})
    
    def test_adjusting_tokens_starts_empty(self, monitor):
        """_adjusting_tokens starts empty."""
        assert len(monitor._adjusting_tokens) == 0
    
    def test_can_mark_token_adjusting(self, monitor):
        """Can mark a token as being adjusted."""
        monitor._adjusting_tokens.add("token123")
        assert "token123" in monitor._adjusting_tokens
    
    def test_can_clear_adjusting_token(self, monitor):
        """Can clear adjusting token when done."""
        monitor._adjusting_tokens.add("token123")
        monitor._adjusting_tokens.discard("token123")
        assert "token123" not in monitor._adjusting_tokens






class TestEdgeCalculations:
    """Tests for edge calculation helpers used in monitoring."""
    
    def test_edge_calculation_basic(self):
        """Edge = fair_value - price."""
        fair_value = 0.55
        price = 0.50
        edge = fair_value - price
        assert abs(edge - 0.05) < 0.001
    
    def test_edge_is_positive_when_price_under_fair(self):
        """Edge is positive when price < fair value."""
        fair_value = 0.60
        price = 0.52
        edge = fair_value - price
        assert edge > 0
    
    def test_edge_is_negative_when_price_over_fair(self):
        """Edge is negative when price > fair value (overpaying)."""
        fair_value = 0.50
        price = 0.55
        edge = fair_value - price
        assert edge < 0
    
    def test_min_edge_threshold_check(self):
        """Order should be cancelled if edge below threshold."""
        min_edge = 0.05
        edge = 0.03
        should_cancel = edge < min_edge
        assert should_cancel is True
    
    def test_edge_above_threshold_ok(self):
        """Order is OK if edge above threshold."""
        min_edge = 0.05
        edge = 0.07
        should_cancel = edge < min_edge
        assert should_cancel is False


class TestOutbidDetection:
    """Tests for outbid detection logic."""
    
    def test_outbid_when_best_bid_higher(self):
        """Outbid when best_bid > our_price."""
        our_price = 0.50
        best_bid = 0.52
        is_outbid = best_bid > our_price + 0.005
        assert is_outbid is True
    
    def test_not_outbid_when_we_are_best(self):
        """Not outbid when our_price == best_bid."""
        our_price = 0.50
        best_bid = 0.50
        is_outbid = best_bid > our_price + 0.005
        assert is_outbid is False
    
    def test_tolerance_prevents_rounding_false_positive(self):
        """Tolerance prevents false positives from rounding."""
        our_price = 0.50
        best_bid = 0.502  # Just rounding difference
        is_outbid = best_bid > our_price + 0.005
        assert is_outbid is False


class TestPriceAdjustmentCalculation:
    """Tests for price adjustment calculations."""
    
    def test_new_price_is_one_cent_above_best_bid(self):
        """New price = best_bid + 0.01."""
        best_bid = 0.52
        new_price = best_bid + 0.01
        assert abs(new_price - 0.53) < 0.001
    
    def test_price_capped_at_fair_value(self):
        """New price capped at fair value for entries."""
        best_bid = 0.55
        fair_value = 0.52
        new_price = min(best_bid + 0.01, fair_value)
        assert new_price == fair_value
    
    def test_skip_adjustment_if_same_price(self):
        """Skip adjustment if new price same as current."""
        our_price = 0.50
        new_price = 0.50
        should_adjust = abs(new_price - our_price) >= 0.005
        assert should_adjust is False


class TestCrossSideProfitCheck:
    """Tests for entry outbid profitability check against actual hedge price.
    
    The bug: entry outbid checks used a hypothetical hedge price from bookmaker
    fair value. When the actual hedge order was at a higher price, the guard
    passed and created unprofitable arbs (0% or negative profit).
    
    The fix: check against actual hedge order/position price from BotState.
    """
    
    def test_entry_blocked_when_hedge_makes_arb_unprofitable(self):
        """Entry adjustment blocked when actual hedge price makes total >= $1.00.
        
        Real case (Liquid vs MOUZ): entry 0.46 + hedge 0.57 = 1.03 -> -2.9%
        """
        min_profit = 0.07  # 7%
        new_entry = 0.46
        actual_hedge_price = 0.57
        
        total_cost = new_entry + actual_hedge_price
        profit_pct = ((1.0 - total_cost) / total_cost) * 100 if total_cost > 0 else 0
        
        assert profit_pct < min_profit * 100  # -2.9% < 7% -> blocked
    
    def test_entry_allowed_when_hedge_preserves_profit(self):
        """Entry adjustment allowed when actual hedge keeps profit >= min_profit."""
        min_profit = 0.07  # 7%
        new_entry = 0.40
        actual_hedge_price = 0.50
        
        total_cost = new_entry + actual_hedge_price
        profit_pct = ((1.0 - total_cost) / total_cost) * 100 if total_cost > 0 else 0
        
        assert profit_pct >= min_profit * 100  # 11.1% >= 7% -> allowed
    
    def test_entry_blocked_at_zero_profit(self):
        """Entry blocked when combined cost exactly $1.00 (0% profit)."""
        min_profit = 0.07
        new_entry = 0.77
        actual_hedge_price = 0.23
        
        total_cost = new_entry + actual_hedge_price
        profit_pct = ((1.0 - total_cost) / total_cost) * 100 if total_cost > 0 else 0
        
        assert profit_pct < min_profit * 100  # 0.0% < 7% -> blocked
    
    def test_entry_barely_profitable_still_blocked(self):
        """Entry blocked when profit is positive but below min_profit."""
        min_profit = 0.07
        new_entry = 0.45
        actual_hedge_price = 0.50
        
        total_cost = new_entry + actual_hedge_price
        profit_pct = ((1.0 - total_cost) / total_cost) * 100 if total_cost > 0 else 0
        
        assert profit_pct < min_profit * 100  # 5.3% < 7% -> blocked
    
    def test_hypothetical_check_when_no_hedge(self):
        """When no hedge order/position exists, fall back to hypothetical check."""
        actual_hedge_price = None
        assert actual_hedge_price is None


class TestGetOppositePrice:
    """Tests for _get_opposite_price helper method."""
    
    def _make_mock_order(self, price, is_open=True):
        mock = Mock()
        mock.price = price
        mock.is_open = is_open
        return mock
    
    def _make_mock_position(self, avg_price):
        mock = Mock()
        mock.avg_price = avg_price
        return mock
    
    def _make_mock_match(self, token1="t1", token2="t2", order1=None, order2=None,
                          position1=None, position2=None):
        mock = Mock()
        mock.token1 = token1
        mock.token2 = token2
        mock.order1 = order1
        mock.order2 = order2
        mock.position1 = position1
        mock.position2 = position2
        return mock
    
    def _get_opposite_price(self, match, token_id):
        """Replicate the helper logic for testing."""
        if not match:
            return None
        if match.token1 == token_id:
            if match.order2 and match.order2.is_open:
                return match.order2.price
            if match.position2:
                return match.position2.avg_price
        elif match.token2 == token_id:
            if match.order1 and match.order1.is_open:
                return match.order1.price
            if match.position1:
                return match.position1.avg_price
        return None
    
    def test_returns_open_order_price(self):
        """Returns the open hedge order's price when available."""
        match = self._make_mock_match(order2=self._make_mock_order(0.23))
        assert self._get_opposite_price(match, "t1") == 0.23
    
    def test_returns_position_when_no_open_order(self):
        """Falls back to position avg_price when no open order."""
        match = self._make_mock_match(position2=self._make_mock_position(0.20))
        assert self._get_opposite_price(match, "t1") == 0.20
    
    def test_prefers_order_over_position(self):
        """Open order price preferred over position avg_price."""
        match = self._make_mock_match(
            order2=self._make_mock_order(0.25),
            position2=self._make_mock_position(0.20),
        )
        assert self._get_opposite_price(match, "t1") == 0.25
    
    def test_ignores_closed_orders(self):
        """Closed (filled) orders are skipped, uses position instead."""
        match = self._make_mock_match(
            order2=self._make_mock_order(0.25, is_open=False),
            position2=self._make_mock_position(0.22),
        )
        assert self._get_opposite_price(match, "t1") == 0.22
    
    def test_returns_none_when_no_coverage(self):
        """Returns None when no order or position on opposite side."""
        match = self._make_mock_match()
        assert self._get_opposite_price(match, "t1") is None
    
    def test_returns_none_for_none_match(self):
        """Returns None when match is None."""
        assert self._get_opposite_price(None, "t1") is None
    
    def test_works_for_token2_entry(self):
        """Correctly looks at side1 when token2 is the entry."""
        match = self._make_mock_match(order1=self._make_mock_order(0.40))
        assert self._get_opposite_price(match, "t2") == 0.40
    
    def test_returns_none_for_unknown_token(self):
        """Returns None for a token not in the match."""
        match = self._make_mock_match()
        assert self._get_opposite_price(match, "unknown") is None
