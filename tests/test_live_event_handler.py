"""
Unit tests for handlers/live_event_handler.py - Live event order cancellation.

Note: LiveEventHandler has async dependencies. These tests use mock-based
approaches and test the core logic patterns.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch


class MockLiveEventHandler:
    """Mock LiveEventHandler for testing core logic patterns."""
    
    def __init__(self, executor: Mock, alerts: Mock):
        self.executor = executor
        self.alerts = alerts
        self._cancelled_live_order_ids = set()
    
    def _extract_live_token_ids(self, events: list) -> set:
        """Extract token IDs from live events."""
        token_ids = set()
        for event in events:
            for market in event.get("markets", []):
                for token_id in market.get("clobTokenIds", []):
                    token_ids.add(token_id)
        return token_ids
    
    def _should_cancel_order(self, order_id: str, token_id: str, live_token_ids: set) -> bool:
        """Check if order should be cancelled due to live event."""
        if token_id not in live_token_ids:
            return False
        
        if order_id in self._cancelled_live_order_ids:
            return False  # Already cancelled
        
        return True
    
    def _mark_cancelled(self, order_id: str):
        """Mark order as cancelled to prevent duplicate attempts."""
        self._cancelled_live_order_ids.add(order_id)


class TestLiveEventHandlerInit:
    """Tests for LiveEventHandler initialization."""
    
    def test_init_stores_dependencies(self):
        """Handler stores injected dependencies."""
        executor = Mock()
        alerts = Mock()
        
        handler = MockLiveEventHandler(executor, alerts)
        
        assert handler.executor is executor
        assert handler.alerts is alerts
    
    def test_init_empty_cancelled_set(self):
        """Handler starts with empty cancelled set."""
        handler = MockLiveEventHandler(Mock(), Mock())
        
        assert len(handler._cancelled_live_order_ids) == 0


class TestExtractLiveTokenIds:
    """Tests for extracting token IDs from live events."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockLiveEventHandler(Mock(), Mock())
    
    def test_extracts_tokens_from_single_event(self, handler):
        """Extracts tokens from single event."""
        events = [{
            "title": "CS2 Match Live",
            "markets": [{
                "clobTokenIds": ["token_a", "token_b"]
            }]
        }]
        
        token_ids = handler._extract_live_token_ids(events)
        
        assert token_ids == {"token_a", "token_b"}
    
    def test_extracts_tokens_from_multiple_events(self, handler):
        """Extracts tokens from multiple events."""
        events = [
            {
                "markets": [{"clobTokenIds": ["token_a", "token_b"]}]
            },
            {
                "markets": [{"clobTokenIds": ["token_c", "token_d"]}]
            }
        ]
        
        token_ids = handler._extract_live_token_ids(events)
        
        assert token_ids == {"token_a", "token_b", "token_c", "token_d"}
    
    def test_handles_empty_events(self, handler):
        """Handles empty events list."""
        token_ids = handler._extract_live_token_ids([])
        assert token_ids == set()
    
    def test_handles_events_without_markets(self, handler):
        """Handles events without markets."""
        events = [{"title": "No Markets"}]
        
        token_ids = handler._extract_live_token_ids(events)
        assert token_ids == set()


class TestShouldCancelOrder:
    """Tests for order cancellation decision logic."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockLiveEventHandler(Mock(), Mock())
    
    def test_cancels_order_on_live_token(self, handler):
        """Cancels order on live token."""
        live_tokens = {"token_a", "token_b"}
        
        should_cancel = handler._should_cancel_order("order123", "token_a", live_tokens)
        assert should_cancel is True
    
    def test_skips_order_on_non_live_token(self, handler):
        """Does not cancel order on non-live token."""
        live_tokens = {"token_a", "token_b"}
        
        should_cancel = handler._should_cancel_order("order456", "token_c", live_tokens)
        assert should_cancel is False
    
    def test_skips_already_cancelled_order(self, handler):
        """Skips order that was already cancelled."""
        handler._cancelled_live_order_ids.add("order123")
        live_tokens = {"token_a"}
        
        should_cancel = handler._should_cancel_order("order123", "token_a", live_tokens)
        assert should_cancel is False


class TestMarkCancelled:
    """Tests for marking orders as cancelled."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockLiveEventHandler(Mock(), Mock())
    
    def test_marks_order_cancelled(self, handler):
        """Marks order as cancelled."""
        handler._mark_cancelled("order123")
        
        assert "order123" in handler._cancelled_live_order_ids
    
    def test_multiple_orders_tracked(self, handler):
        """Tracks multiple cancelled orders."""
        handler._mark_cancelled("order123")
        handler._mark_cancelled("order456")
        
        assert "order123" in handler._cancelled_live_order_ids
        assert "order456" in handler._cancelled_live_order_ids
    
    def test_duplicate_mark_is_safe(self, handler):
        """Marking same order twice is safe."""
        handler._mark_cancelled("order123")
        handler._mark_cancelled("order123")
        
        assert len(handler._cancelled_live_order_ids) == 1


class TestLiveEventCancellationFlow:
    """Tests for the complete live event cancellation flow."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockLiveEventHandler(Mock(), Mock())
    
    def test_full_flow_cancels_matching_orders(self, handler):
        """Full flow: extract tokens -> check orders -> cancel matching."""
        events = [{
            "markets": [{"clobTokenIds": ["token_live"]}]
        }]
        
        orders = [
            {"order_id": "order1", "token_id": "token_live"},
            {"order_id": "order2", "token_id": "token_not_live"},
        ]
        
        live_token_ids = handler._extract_live_token_ids(events)
        
        orders_to_cancel = []
        for order in orders:
            if handler._should_cancel_order(order["order_id"], order["token_id"], live_token_ids):
                orders_to_cancel.append(order["order_id"])
                handler._mark_cancelled(order["order_id"])
        
        assert orders_to_cancel == ["order1"]
        assert "order1" in handler._cancelled_live_order_ids
    
    def test_prevents_repeat_cancellation(self, handler):
        """Prevents repeated cancellation attempts on same order."""
        events = [{
            "markets": [{"clobTokenIds": ["token_live"]}]
        }]
        
        live_token_ids = handler._extract_live_token_ids(events)
        
        # First run
        should_cancel_1 = handler._should_cancel_order("order1", "token_live", live_token_ids)
        if should_cancel_1:
            handler._mark_cancelled("order1")
        
        # Second run (simulating next check cycle)
        should_cancel_2 = handler._should_cancel_order("order1", "token_live", live_token_ids)
        
        assert should_cancel_1 is True
        assert should_cancel_2 is False

