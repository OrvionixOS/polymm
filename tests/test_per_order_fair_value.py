"""Tests for per-order fair value isolation (multi-market fix).

Verifies that spread/totals orders with fair_value set are NOT affected
when moneyline odds update the match-level fair_prob.
"""
import pytest
from src.state.match_state import MatchOrder, MatchState
from src.state.bot_state import BotState
from src.state.state_events import StateEventType


class TestPerOrderFairValue:
    """Test that order.fair_value isolates edge calculation from match fair_prob."""
    
    def test_order_with_fair_value_ignores_match_fair_prob(self):
        """Spread order at 67% fair should keep edge even when moneyline overwrites to 19%."""
        match = MatchState(
            match_id="football:fiorentina:vs:rakow",
            team1="Fiorentina", team2="Rakow",
            token1="tok1", token2="tok2",
        )
        # Order placed on spread market with 67% fair value
        order = MatchOrder(
            order_id="ord1", token_id="tok2", team="Rakow",
            price=0.57, size=10, fair_value=0.67,
        )
        match.order2 = order
        
        # Edge should use order.fair_value (67%), not match fair_prob
        edge = match.get_order_edge(order)
        assert edge is not None
        assert abs(edge - 0.10) < 0.01  # 67% - 57% = 10%
        
        # Now moneyline odds overwrite match fair_prob to 19%
        match.fair_prob2 = 0.19
        
        # Edge should STILL use order.fair_value (67%), unchanged
        edge_after = match.get_order_edge(order)
        assert edge_after is not None
        assert abs(edge_after - 0.10) < 0.01  # Still 10%, not -38%
    
    def test_order_without_fair_value_uses_match_fair_prob(self):
        """Hydrated orders (no fair_value) should fall back to match fair_prob."""
        match = MatchState(
            match_id="cs2:navi:vs:g2",
            team1="Navi", team2="G2",
            token1="tok1", token2="tok2",
            fair_prob1=0.60, fair_prob2=0.40,
        )
        # Hydrated order — no fair_value set
        order = MatchOrder(
            order_id="ord1", token_id="tok1", team="Navi",
            price=0.50, size=100,
        )
        match.order1 = order
        
        edge = match.get_order_edge(order)
        assert edge is not None
        assert abs(edge - 0.10) < 0.01  # 60% - 50% = 10%
    
    def test_update_order_fair_value_triggers_edge_lost(self):
        """Updating order fair_value below threshold should emit EDGE_LOST."""
        bot_state = BotState(min_edge=0.05)
        match = bot_state.register_match(
            match_id="football:team1:vs:team2",
            team1="Team1", team2="Team2",
            token1="tok1", token2="tok2",
        )
        bot_state.register_order(
            match_id="football:team1:vs:team2",
            order_id="ord1", token_id="tok1", team="Team1",
            price=0.50, size=10, fair_value=0.60,
        )
        
        # Track emitted events
        events = []
        bot_state.on(StateEventType.EDGE_LOST, lambda e: events.append(e))
        
        # Update order fair to below threshold (edge = 0.52 - 0.50 = 0.02 < 0.05)
        order = match.order1
        bot_state.update_order_fair_value("football:team1:vs:team2", order, 0.52)
        
        assert len(events) == 1
        assert order.fair_value == 0.52
    
    def test_update_order_fair_value_no_event_when_still_above(self):
        """Updating fair_value while still above threshold should NOT emit."""
        bot_state = BotState(min_edge=0.05)
        match = bot_state.register_match(
            match_id="football:a:vs:b",
            team1="A", team2="B",
            token1="tok1", token2="tok2",
        )
        bot_state.register_order(
            match_id="football:a:vs:b",
            order_id="ord1", token_id="tok1", team="A",
            price=0.50, size=10, fair_value=0.60,
        )
        
        events = []
        bot_state.on(StateEventType.EDGE_LOST, lambda e: events.append(e))
        
        # Update fair from 60% to 58% — edge = 8% still above 5% threshold
        order = match.order1
        bot_state.update_order_fair_value("football:a:vs:b", order, 0.58)
        
        assert len(events) == 0
        assert order.fair_value == 0.58
    
    def test_moneyline_push_doesnt_cancel_spread_order(self):
        """Simulates the full pipeline: spread order survives moneyline fair_prob update."""
        bot_state = BotState(min_edge=0.05)
        
        # Register a spread market match (with condition_id suffix)
        match = bot_state.register_match(
            match_id="football:fiorentina:vs:rakow:0xabc123",
            team1="Fiorentina", team2="Rakow",
            token1="tok1", token2="tok2",
        )
        # Place spread order with 67% fair value
        bot_state.register_order(
            match_id="football:fiorentina:vs:rakow:0xabc123",
            order_id="ord1", token_id="tok2", team="Rakow",
            price=0.57, size=10, fair_value=0.67,
        )
        
        edge_lost_events = []
        bot_state.on(StateEventType.EDGE_LOST, lambda e: edge_lost_events.append(e))
        
        # Moneyline push overwrites match fair_prob to 19% for Rakow
        bot_state.update_fair_probs(
            match_id="football:fiorentina:vs:rakow:0xabc123",
            fair_prob1=0.56,  # Fiorentina moneyline
            fair_prob2=0.19,  # Rakow moneyline
        )
        
        # Should NOT have edge_lost — order.fair_value (67%) shields it
        assert len(edge_lost_events) == 0
        
        # Verify edge is still correct (uses order.fair_value)
        order = match.order2
        edge = match.get_order_edge(order)
        assert abs(edge - 0.10) < 0.01  # 67% - 57% = 10%
