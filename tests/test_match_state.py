"""
Unit tests for state/match_state.py - Match state data models.
"""
import pytest
from datetime import datetime, timezone

from src.state.match_state import (
    MatchSide,
    MatchOrder,
    MatchPosition,
    MatchState,
)


class TestMatchOrder:
    """Tests for MatchOrder dataclass."""
    
    def test_is_filled(self):
        """Order is filled when filled >= size."""
        order = MatchOrder(
            order_id="order1",
            token_id="token1",
            team="Team A",
            price=0.50,
            size=10.0,
            filled=10.0,
        )
        assert order.is_filled is True  # property, not method
    
    def test_is_partial(self):
        """Order is partial when 0 < filled < size."""
        order = MatchOrder(
            order_id="order1",
            token_id="token1",
            team="Team A",
            price=0.50,
            size=10.0,
            filled=5.0,
        )
        assert order.is_partial is True  # property, not method
        assert order.is_filled is False


class TestMatchState:
    """Tests for MatchState class."""
    
    def create_match_state(self) -> MatchState:
        """Helper to create a fresh MatchState."""
        return MatchState(
            match_id="cs2:teama:vs:teamb",
            team1="Team A",
            team2="Team B",
            token1="token_a",
            token2="token_b",
            game="cs2",
        )
    
    def test_has_natural_arb_both_orders(self):
        """Natural arb when orders on both sides."""
        state = self.create_match_state()
        state.order1 = MatchOrder(
            order_id="o1", token_id="token_a", team="Team A",
            price=0.45, size=10.0
        )
        state.order2 = MatchOrder(
            order_id="o2", token_id="token_b", team="Team B",
            price=0.45, size=10.0
        )
        assert state.has_natural_arb is True  # property
    
    def test_has_natural_arb_order_and_position(self):
        """Natural arb when order + position cover both sides."""
        state = self.create_match_state()
        state.order1 = MatchOrder(
            order_id="o1", token_id="token_a", team="Team A",
            price=0.45, size=10.0
        )
        state.position2 = MatchPosition(
            token_id="token_b", team="Team B",
            shares=10.0, avg_price=0.45
        )
        assert state.has_natural_arb is True
    
    def test_has_natural_arb_false_one_side(self):
        """No natural arb when only one side covered."""
        state = self.create_match_state()
        state.order1 = MatchOrder(
            order_id="o1", token_id="token_a", team="Team A",
            price=0.45, size=10.0
        )
        assert state.has_natural_arb is False
    
    def test_needs_hedge_returns_token(self):
        """Returns opposite token when position needs hedge."""
        state = self.create_match_state()
        state.position1 = MatchPosition(
            token_id="token_a", team="Team A",
            shares=10.0, avg_price=0.45
        )
        # Need hedge on Team B
        hedge_token = state.needs_hedge  # property
        assert hedge_token == "token_b"
    
    def test_needs_hedge_none_when_fully_hedged(self):
        """Returns None when both sides have positions."""
        state = self.create_match_state()
        state.position1 = MatchPosition(
            token_id="token_a", team="Team A",
            shares=10.0, avg_price=0.45
        )
        state.position2 = MatchPosition(
            token_id="token_b", team="Team B",
            shares=10.0, avg_price=0.45
        )
        assert state.needs_hedge is None
    
    def test_edge_calculation(self):
        """Edge = fair_prob - bid - 0.01."""
        state = self.create_match_state()
        state.fair_prob1 = 0.60  # 60% fair value
        state.bid1 = 0.50  # Best bid at 50¢
        
        # edge1 = 0.60 - (0.50 + 0.01) = 0.09
        expected_edge = 0.09
        assert abs(state.edge1 - expected_edge) < 0.001  # property
    
    def test_is_order_hedge(self):
        """Correctly identifies hedge vs entry orders."""
        state = self.create_match_state()
        
        # Position on side 1
        state.position1 = MatchPosition(
            token_id="token_a", team="Team A",
            shares=10.0, avg_price=0.45
        )
        
        # Order on side 2 is a hedge (opposite side has position)
        hedge_order = MatchOrder(
            order_id="o2", token_id="token_b", team="Team B",
            price=0.45, size=10.0
        )
        state.order2 = hedge_order
        
        assert state.is_order_hedge(hedge_order) is True  # method
        
        # Order on side 1 is NOT a hedge (same side as position)
        entry_order = MatchOrder(
            order_id="o1", token_id="token_a", team="Team A",
            price=0.45, size=10.0
        )
        state.order1 = entry_order
        assert state.is_order_hedge(entry_order) is False
    
    def test_arb_profit_calculation(self):
        """Profit = 1 - (entry_price + hedge_price)."""
        state = self.create_match_state()
        
        # Entry at 45¢, hedge at 45¢ = 90¢ total, 10¢ profit
        state.position1 = MatchPosition(
            token_id="token_a", team="Team A",
            shares=10.0, avg_price=0.45
        )
        state.position2 = MatchPosition(
            token_id="token_b", team="Team B",
            shares=10.0, avg_price=0.45
        )
        
        profit = state.get_arb_profit_percent()  # method, returns raw value
        # 1.0 - 0.45 - 0.45 = 0.10
        assert profit is not None
        assert abs(profit - 0.10) < 0.01  # 10% profit as decimal
    
    def test_is_fully_hedged(self):
        """True when positions on both sides."""
        state = self.create_match_state()
        
        assert state.is_fully_hedged is False  # property
        
        state.position1 = MatchPosition(
            token_id="token_a", team="Team A",
            shares=10.0, avg_price=0.45
        )
        assert state.is_fully_hedged is False
        
        state.position2 = MatchPosition(
            token_id="token_b", team="Team B",
            shares=10.0, avg_price=0.45
        )
        assert state.is_fully_hedged is True
    
    def test_unhedged_shares(self):
        """Calculate unhedged shares on each side."""
        state = self.create_match_state()
        
        state.position1 = MatchPosition(
            token_id="token_a", team="Team A",
            shares=20.0, avg_price=0.45
        )
        state.position2 = MatchPosition(
            token_id="token_b", team="Team B",
            shares=15.0, avg_price=0.45
        )
        
        # 20 Team A - 15 Team B covered = 5 unhedged on side 1
        assert state.unhedged_shares_side1 == 5.0  # property
        assert state.unhedged_shares_side2 == 0.0  # property
