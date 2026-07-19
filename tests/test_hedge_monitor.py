"""
Unit tests for monitoring/hedge_monitor.py - Hedge order monitoring.

Note: HedgeMonitorMixin has dependencies that cause circular imports when
imported directly. These tests use mock-based approaches and test the core
logic patterns used in the module.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch


class MockHedgeMonitor:
    """Mock HedgeMonitor for testing core logic patterns."""
    
    def __init__(self, config: dict):
        self._adjusting_tokens = set()
        self._hedge_fair_cap_warned = set()
        self.executor = Mock()
        self.config = config
    
    def _handle_hedge_ws_fast(
        self,
        token_id: str,
        new_entry: float,
        our_fair: float,
        our_price: float,
        order_info: dict,
        best_bid: float,
        get_bot_state_mock: Mock,
    ) -> tuple:
        """Handle hedge-specific logic in fast WS adjustment."""
        # Cap at fair value
        if new_entry > our_fair:
            new_entry = round(our_fair, 2)
            new_edge = our_fair - new_entry
            
            if abs(new_entry - our_price) < 0.005:
                return (False, None, None)
        else:
            new_edge = our_fair - new_entry
        
        # Get entry price from BotState
        bot_state = get_bot_state_mock()
        match = bot_state.get_match_by_token(token_id)
        if not match:
            return (False, None, None)
        
        entry_price = None
        if match.token1 == token_id and match.position2:
            entry_price = match.position2.avg_price
        elif match.token2 == token_id and match.position1:
            entry_price = match.position1.avg_price
        
        if not entry_price:
            return (False, None, None)
        
        total_cost = entry_price + new_entry
        profit = 1.0 - total_cost
        profit_pct = (profit / total_cost) * 100 if total_cost > 0 else 0
        
        if profit_pct >= self.config["min_profit"] * 100:
            return (True, new_entry, new_edge)
        else:
            return (False, None, None)


class TestHedgeMonitorMixinInit:
    """Tests for HedgeMonitorMixin setup."""
    
    @pytest.fixture
    def monitor(self):
        """Create mock monitor."""
        return MockHedgeMonitor(config={
            "min_edge": 0.05,
            "default_shares": 25,
            "min_profit": 0.01,
        })
    
    def test_adjusting_tokens_starts_empty(self, monitor):
        """_adjusting_tokens starts empty."""
        assert len(monitor._adjusting_tokens) == 0
    
    def test_hedge_fair_cap_warned_starts_empty(self, monitor):
        """_hedge_fair_cap_warned starts empty."""
        assert len(monitor._hedge_fair_cap_warned) == 0


class TestHandleHedgeWsFast:
    """Tests for _handle_hedge_ws_fast method patterns."""
    
    @pytest.fixture
    def monitor(self):
        """Create mock monitor."""
        return MockHedgeMonitor(config={
            "min_edge": 0.05,
            "default_shares": 25,
            "min_profit": 0.01,  # 1% minimum profit
        })
    
    def test_caps_at_fair_value_when_above(self, monitor):
        """Caps new_entry at fair value when it would exceed."""
        mock_get_state = Mock()
        mock_state = Mock()
        mock_match = Mock()
        mock_match.token1 = "token123"
        mock_match.token2 = "other_token"
        # Entry at 0.40, hedge capped at 0.55 = total 0.95 = 5.26% profit
        mock_match.position2 = Mock(avg_price=0.40)
        mock_state.get_match_by_token.return_value = mock_match
        mock_get_state.return_value = mock_state
        
        should_adjust, new_entry, new_edge = monitor._handle_hedge_ws_fast(
            token_id="token123",
            new_entry=0.60,  # Above fair
            our_fair=0.55,
            our_price=0.50,
            order_info={"team_name": "Team A"},
            best_bid=0.58,
            get_bot_state_mock=mock_get_state,
        )
        
        # Should adjust and cap at fair value
        assert should_adjust is True
        assert new_entry == 0.55
    
    def test_skips_when_already_at_fair_value(self, monitor):
        """Returns False when already at fair value."""
        mock_get_state = Mock()
        mock_state = Mock()
        mock_state.get_match_by_token.return_value = None
        mock_get_state.return_value = mock_state
        
        should_adjust, new_entry, new_edge = monitor._handle_hedge_ws_fast(
            token_id="token123",
            new_entry=0.55,  # Same as fair
            our_fair=0.55,
            our_price=0.55,  # Already at fair
            order_info={"team_name": "Team A"},
            best_bid=0.54,
            get_bot_state_mock=mock_get_state,
        )
        
        assert should_adjust is False
    
    def test_returns_false_when_no_match(self, monitor):
        """Returns False when no match found."""
        mock_get_state = Mock()
        mock_state = Mock()
        mock_state.get_match_by_token.return_value = None
        mock_get_state.return_value = mock_state
        
        should_adjust, new_entry, new_edge = monitor._handle_hedge_ws_fast(
            token_id="unknown_token",
            new_entry=0.52,
            our_fair=0.55,
            our_price=0.50,
            order_info={},
            best_bid=0.51,
            get_bot_state_mock=mock_get_state,
        )
        
        assert should_adjust is False
    
    def test_returns_false_when_not_profitable(self, monitor):
        """Returns False when adjustment not profitable enough."""
        mock_get_state = Mock()
        mock_state = Mock()
        mock_match = Mock()
        mock_match.token1 = "token123"
        mock_match.token2 = "other_token"
        # Entry at 0.55, hedge at 0.50 = total 1.05 = -5% profit (LOSS)
        mock_match.position2 = Mock(avg_price=0.55)
        mock_state.get_match_by_token.return_value = mock_match
        mock_get_state.return_value = mock_state
        
        should_adjust, new_entry, new_edge = monitor._handle_hedge_ws_fast(
            token_id="token123",
            new_entry=0.50,
            our_fair=0.55,
            our_price=0.48,
            order_info={"team_name": "Team A"},
            best_bid=0.49,
            get_bot_state_mock=mock_get_state,
        )
        
        assert should_adjust is False
    
    def test_returns_true_when_profitable(self, monitor):
        """Returns True when adjustment is profitable."""
        mock_get_state = Mock()
        mock_state = Mock()
        mock_match = Mock()
        mock_match.token1 = "token123"
        mock_match.token2 = "other_token"
        # Entry at 0.45, hedge at 0.50 = total 0.95 = 5.26% profit
        mock_match.position2 = Mock(avg_price=0.45)
        mock_state.get_match_by_token.return_value = mock_match
        mock_get_state.return_value = mock_state
        
        should_adjust, new_entry, new_edge = monitor._handle_hedge_ws_fast(
            token_id="token123",
            new_entry=0.50,
            our_fair=0.55,
            our_price=0.48,
            order_info={"team_name": "Team A"},
            best_bid=0.49,
            get_bot_state_mock=mock_get_state,
        )
        
        assert should_adjust is True
        assert new_entry == 0.50


class TestHedgeProfitability:
    """Tests for hedge profitability calculations."""
    
    def test_profitable_hedge(self):
        """Hedge is profitable when total cost < 1.0."""
        entry_price = 0.45
        hedge_price = 0.50
        total_cost = entry_price + hedge_price
        profit = 1.0 - total_cost
        profit_pct = (profit / total_cost) * 100
        
        assert abs(total_cost - 0.95) < 0.001
        assert abs(profit - 0.05) < 0.001
        assert abs(profit_pct - 5.26) < 0.1  # ~5.26% profit
    
    def test_unprofitable_hedge(self):
        """Hedge is unprofitable when total cost > 1.0."""
        entry_price = 0.55
        hedge_price = 0.50
        total_cost = entry_price + hedge_price
        profit = 1.0 - total_cost
        profit_pct = (profit / total_cost) * 100
        
        assert abs(total_cost - 1.05) < 0.001
        assert abs(profit - (-0.05)) < 0.001
        assert profit_pct < 0  # Negative profit
    
    def test_breakeven_hedge(self):
        """Hedge breaks even when total cost == 1.0."""
        entry_price = 0.50
        hedge_price = 0.50
        total_cost = entry_price + hedge_price
        profit = 1.0 - total_cost
        
        assert total_cost == 1.0
        assert profit == 0.0
    
    def test_min_profit_threshold(self):
        """Profit must exceed min_profit to be acceptable."""
        entry_price = 0.48
        hedge_price = 0.51
        total_cost = entry_price + hedge_price
        profit = 1.0 - total_cost
        profit_pct = (profit / total_cost) * 100
        min_profit = 0.01  # 1%
        
        is_profitable_enough = profit_pct >= min_profit * 100
        assert abs(profit_pct - 1.01) < 0.1  # ~1.01% profit
        assert is_profitable_enough is True


class TestFairValueCap:
    """Tests for fair value cap logic."""
    
    def test_new_entry_above_fair_gets_capped(self):
        """New entry above fair value should be capped to fair value."""
        new_entry = 0.60
        our_fair = 0.55
        
        if new_entry > our_fair:
            new_entry = round(our_fair, 2)
        
        assert new_entry == 0.55
    
    def test_new_entry_below_fair_unchanged(self):
        """New entry below fair value should not be changed."""
        new_entry = 0.52
        our_fair = 0.55
        original = new_entry
        
        if new_entry > our_fair:
            new_entry = round(our_fair, 2)
        
        assert new_entry == original
    
    def test_skip_if_at_fair_value(self):
        """Skip adjustment if already at fair value."""
        new_entry = 0.55
        our_price = 0.55
        our_fair = 0.55
        
        # Cap new_entry
        if new_entry > our_fair:
            new_entry = round(our_fair, 2)
        
        # Check if same as current price
        should_skip = abs(new_entry - our_price) < 0.005
        assert should_skip is True


class TestPriceImprovement:
    """Tests for hedge price improvement logic."""
    
    def test_gap_detection(self):
        """Detects gap between our price and second-best bid."""
        our_price = 0.55
        second_best_bid = 0.50
        gap = our_price - second_best_bid
        
        assert abs(gap - 0.05) < 0.001
    
    def test_gap_too_small_to_improve(self):
        """Skip improvement if gap <= 3¢."""
        our_price = 0.52
        second_best_bid = 0.50
        gap = our_price - second_best_bid
        
        should_skip = gap <= 0.03
        assert should_skip is True
    
    def test_gap_large_enough(self):
        """Gap > 3¢ allows improvement."""
        our_price = 0.55
        second_best_bid = 0.50
        gap = our_price - second_best_bid
        
        should_improve = gap > 0.03
        assert should_improve is True
    
    def test_improved_price_calculation(self):
        """Improved price = second_best_bid + 0.01."""
        second_best_bid = 0.50
        improved_price = second_best_bid + 0.01
        
        assert improved_price == 0.51


class TestDeduplicationLogic:
    """Tests for _hedge_fair_cap_warned deduplication."""
    
    @pytest.fixture
    def monitor(self):
        """Create mock monitor."""
        return MockHedgeMonitor(config={
            "min_edge": 0.05,
            "default_shares": 25,
            "min_profit": 0.01,
        })
    
    def test_first_warning_is_logged(self, monitor):
        """First fair cap warning should be logged."""
        token_id = "token123"
        should_log = token_id not in monitor._hedge_fair_cap_warned
        
        assert should_log is True
    
    def test_subsequent_warnings_suppressed(self, monitor):
        """Subsequent fair cap warnings should be suppressed."""
        token_id = "token123"
        monitor._hedge_fair_cap_warned.add(token_id)
        
        should_log = token_id not in monitor._hedge_fair_cap_warned
        assert should_log is False
    
    def test_different_token_gets_warning(self, monitor):
        """Different token should get its own warning."""
        monitor._hedge_fair_cap_warned.add("token123")
        
        should_log = "token456" not in monitor._hedge_fair_cap_warned
        assert should_log is True

