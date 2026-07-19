"""
Unit tests for execution/position.py - Position and order tracking data models.
"""
import pytest
from datetime import datetime
from unittest.mock import Mock

from src.execution.position import (
    OrderType,
    PositionState,
    WatchedOrder,
    Position,
)


class TestOrderType:
    """Tests for OrderType enum."""
    
    def test_order_types_exist(self):
        """Verify all order types exist."""
        assert OrderType.ENTRY.value == "ENTRY"
        assert OrderType.HEDGE.value == "HEDGE"


class TestPositionState:
    """Tests for PositionState enum."""
    
    def test_all_states_exist(self):
        """Verify all position states exist."""
        states = [
            PositionState.ENTRY_PENDING,
            PositionState.ENTRY_PARTIAL,
            PositionState.POSITION_OPEN,
            PositionState.HEDGE_PENDING,
            PositionState.HEDGE_PARTIAL,
            PositionState.HEDGED,
            PositionState.CANCELLED,
        ]
        assert len(states) == 7


class TestWatchedOrder:
    """Tests for WatchedOrder dataclass."""
    
    def create_mock_order(self):
        """Create a mock Order object."""
        order = Mock()
        order.order_id = "order123"
        return order
    
    def test_is_entry(self):
        """Entry order has is_entry=True."""
        watched = WatchedOrder(
            order=self.create_mock_order(),
            order_type=OrderType.ENTRY,
            token_id="token1",
            team_name="Team A",
            fair_value=0.55,
        )
        assert watched.is_entry is True
        assert watched.is_hedge is False
    
    def test_is_hedge(self):
        """Hedge order has is_hedge=True."""
        watched = WatchedOrder(
            order=self.create_mock_order(),
            order_type=OrderType.HEDGE,
            token_id="token1",
            team_name="Team A",
            fair_value=0.45,
        )
        assert watched.is_hedge is True
        assert watched.is_entry is False


class TestPosition:
    """Tests for Position dataclass."""
    
    def create_position(
        self,
        entry_shares: float = 10.0,
        entry_price: float = 0.45,
        hedge_shares: float = 0.0,
        hedge_price: float = 0.0,
    ) -> Position:
        """Helper to create a Position."""
        return Position(
            position_id="pos123",
            match_id="cs2:teama:vs:teamb",
            game="cs2",
            team1="Team A",
            team2="Team B",
            entry_team="Team A",
            entry_token_id="token_a",
            entry_filled_shares=entry_shares,
            entry_avg_price=entry_price,
            hedge_team="Team B",
            hedge_token_id="token_b",
            hedge_filled_shares=hedge_shares,
            hedge_avg_price=hedge_price,
        )
    
    def test_entry_cost(self):
        """Entry cost = shares * price."""
        pos = self.create_position(entry_shares=10.0, entry_price=0.45)
        assert abs(pos.entry_cost - 4.50) < 0.01
    
    def test_hedge_cost(self):
        """Hedge cost = shares * price."""
        pos = self.create_position(
            entry_shares=10.0, entry_price=0.45,
            hedge_shares=10.0, hedge_price=0.45
        )
        assert abs(pos.hedge_cost - 4.50) < 0.01
    
    def test_total_cost(self):
        """Total cost = entry + hedge."""
        pos = self.create_position(
            entry_shares=10.0, entry_price=0.45,
            hedge_shares=10.0, hedge_price=0.45
        )
        assert abs(pos.total_cost - 9.00) < 0.01
    
    def test_potential_payout(self):
        """Payout = min(entry, hedge) shares * $1."""
        pos = self.create_position(
            entry_shares=10.0, entry_price=0.45,
            hedge_shares=8.0, hedge_price=0.45
        )
        # Min(10, 8) = 8 shares → $8 payout
        assert abs(pos.potential_payout - 8.0) < 0.01
    
    def test_locked_profit(self):
        """Profit = hedged_shares * $1 - entry_cost - hedge_cost."""
        pos = self.create_position(
            entry_shares=10.0, entry_price=0.45,
            hedge_shares=10.0, hedge_price=0.45
        )
        # 10 * 1.0 - (10 * 0.45) - (10 * 0.45) = 10 - 4.5 - 4.5 = $1
        assert abs(pos.locked_profit - 1.0) < 0.01
    
    def test_locked_profit_partial_hedge(self):
        """Profit calculated on hedged shares only."""
        pos = self.create_position(
            entry_shares=10.0, entry_price=0.40,
            hedge_shares=5.0, hedge_price=0.50
        )
        # Hedged = min(10, 5) = 5 shares
        # Profit = 5 * 1.0 - (5 * 0.40) - (5 * 0.50) = 5 - 2 - 2.5 = $0.50
        assert abs(pos.locked_profit - 0.50) < 0.01
    
    def test_locked_profit_no_hedge(self):
        """No profit when not hedged."""
        pos = self.create_position(entry_shares=10.0, entry_price=0.45)
        assert pos.locked_profit == 0.0
    
    def test_profit_percent(self):
        """Profit percent = profit / total_cost * 100."""
        pos = self.create_position(
            entry_shares=10.0, entry_price=0.45,
            hedge_shares=10.0, hedge_price=0.45
        )
        # Profit = $1, Total cost = $9 → 11.1%
        assert abs(pos.profit_percent - 11.1) < 0.5
    
    def test_profit_percent_zero_cost(self):
        """Profit percent = 0 when no cost."""
        pos = Position(
            position_id="pos123",
            match_id="cs2:teama:vs:teamb",
            game="cs2",
            team1="Team A",
            team2="Team B",
            entry_team="Team A",
            entry_token_id="token_a",
        )
        assert pos.profit_percent == 0.0
    
    def test_default_state_is_entry_pending(self):
        """Default state is ENTRY_PENDING."""
        pos = self.create_position()
        assert pos.state == PositionState.ENTRY_PENDING
