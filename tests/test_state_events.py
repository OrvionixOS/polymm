"""
Unit tests for state/state_events.py - Event types for reactive architecture.
"""
import pytest
from datetime import datetime, timezone

from src.state.state_events import StateEventType, StateEvent
from src.state.match_state import MatchState


class TestStateEventType:
    """Tests for StateEventType enum."""
    
    def test_order_events_exist(self):
        """Verify all order event types exist."""
        assert StateEventType.ORDER_REGISTERED.value == "order_registered"
        assert StateEventType.ORDER_FILLED.value == "order_filled"
        assert StateEventType.ORDER_CANCELLED.value == "order_cancelled"
        assert StateEventType.ORDER_REPLACED.value == "order_replaced"
    
    def test_market_data_events_exist(self):
        """Verify market data event types exist."""
        assert StateEventType.FAIR_PROBS_UPDATED.value == "fair_probs_updated"
        assert StateEventType.BID_UPDATED.value == "bid_updated"
    
    def test_position_events_exist(self):
        """Verify position event types exist."""
        assert StateEventType.POSITION_CREATED.value == "position_created"
        assert StateEventType.POSITION_HYDRATED.value == "position_hydrated"
    
    def test_match_events_exist(self):
        """Verify match event types exist."""
        assert StateEventType.MATCH_GONE_LIVE.value == "match_gone_live"
        assert StateEventType.NATURAL_ARB_DETECTED.value == "natural_arb_detected"
    
    def test_edge_events_exist(self):
        """Verify edge event types exist."""
        assert StateEventType.EDGE_LOST.value == "edge_lost"
        assert StateEventType.EDGE_GAINED.value == "edge_gained"
    
    def test_total_event_types(self):
        """Verify total number of event types."""
        assert len(StateEventType) == 13


class TestStateEvent:
    """Tests for StateEvent dataclass."""
    
    def create_match_state(self) -> MatchState:
        """Helper to create a MatchState."""
        return MatchState(
            match_id="cs2:teama:vs:teamb",
            team1="Team A",
            team2="Team B",
            token1="token_a",
            token2="token_b",
            game="cs2",
        )
    
    def test_basic_construction(self):
        """StateEvent can be constructed with required fields."""
        match_state = self.create_match_state()
        event = StateEvent(
            event_type=StateEventType.ORDER_REGISTERED,
            match_id="cs2:teama:vs:teamb",
            match_state=match_state,
        )
        
        assert event.event_type == StateEventType.ORDER_REGISTERED
        assert event.match_id == "cs2:teama:vs:teamb"
        assert event.match_state is match_state
        assert event.data is None
        assert event.timestamp is not None
    
    def test_with_data(self):
        """StateEvent can carry additional data."""
        match_state = self.create_match_state()
        event = StateEvent(
            event_type=StateEventType.ORDER_FILLED,
            match_id="cs2:teama:vs:teamb",
            match_state=match_state,
            data={"shares": 10.0, "price": 0.45},
        )
        
        assert event.data == {"shares": 10.0, "price": 0.45}
    
    def test_edge_event_fields(self):
        """Edge events carry old_edge, new_edge, token_id."""
        match_state = self.create_match_state()
        event = StateEvent(
            event_type=StateEventType.EDGE_LOST,
            match_id="cs2:teama:vs:teamb",
            match_state=match_state,
            old_edge=0.12,
            new_edge=0.05,
            token_id="token_a",
            order_id="order123",
        )
        
        assert event.old_edge == 0.12
        assert event.new_edge == 0.05
        assert event.token_id == "token_a"
        assert event.order_id == "order123"
    
    def test_timestamp_is_utc(self):
        """Timestamp should be timezone-aware UTC."""
        match_state = self.create_match_state()
        event = StateEvent(
            event_type=StateEventType.BID_UPDATED,
            match_id="cs2:teama:vs:teamb",
            match_state=match_state,
        )
        
        assert event.timestamp.tzinfo is not None
        assert event.timestamp.tzinfo == timezone.utc
    
    def test_optional_fields_default_none(self):
        """Optional fields default to None."""
        match_state = self.create_match_state()
        event = StateEvent(
            event_type=StateEventType.NATURAL_ARB_DETECTED,
            match_id="cs2:teama:vs:teamb",
            match_state=match_state,
        )
        
        assert event.old_edge is None
        assert event.new_edge is None
        assert event.token_id is None
        assert event.order_id is None
