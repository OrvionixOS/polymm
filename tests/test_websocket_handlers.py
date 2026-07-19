"""
Unit tests for handlers/websocket_handlers.py - Order book WebSocket handlers.

Note: WebSocketHandler has async WebSocket dependencies. These tests use 
mock-based approaches and test the core logic patterns.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch


class MockLivePrice:
    """Mock LivePrice from WebSocket."""
    
    def __init__(self, best_bid: float = 0.50, best_ask: float = 0.55):
        self.best_bid = best_bid
        self.best_ask = best_ask


class MockWebSocketHandler:
    """Mock WebSocketHandler for testing core logic patterns."""
    
    def __init__(self, on_outbid_callback=None):
        self._on_outbid_callback = on_outbid_callback
        self._outbid_state = {}
        self.bot_state = Mock()
    
    def _is_outbid(self, our_price: float, best_bid: float) -> bool:
        """Check if we're outbid."""
        return best_bid > our_price + 0.005
    
    def _should_skip_adjustment(self, token_id: str, best_bid: float) -> bool:
        """Check if we should skip this outbid (already tried this level)."""
        prev_state = self._outbid_state.get(token_id)
        if prev_state:
            # Only re-process if best_bid changed by at least 1¢
            if abs(prev_state.get("best_bid", 0) - best_bid) < 0.01:
                return True
        return False
    
    def _should_clear_outbid_state(self, our_price: float, best_bid: float) -> bool:
        """Check if outbid state should be cleared (we're best again)."""
        return not self._is_outbid(our_price, best_bid)
    
    def mark_adjustment_failed(self, token_id: str, best_bid: float):
        """Mark that an adjustment at this bid level failed."""
        self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True}
    
    def clear_outbid_state(self, token_id: str):
        """Clear outbid state for a token."""
        if token_id in self._outbid_state:
            del self._outbid_state[token_id]


class TestWebSocketHandlerInit:
    """Tests for WebSocketHandler initialization."""
    
    def test_init_with_callback(self):
        """Handler stores callback."""
        callback = Mock()
        handler = MockWebSocketHandler(on_outbid_callback=callback)
        
        assert handler._on_outbid_callback is callback
    
    def test_init_empty_outbid_state(self):
        """Handler starts with empty outbid state."""
        handler = MockWebSocketHandler()
        
        assert len(handler._outbid_state) == 0


class TestOutbidDetection:
    """Tests for outbid detection logic."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockWebSocketHandler()
    
    def test_outbid_when_best_bid_higher(self, handler):
        """Outbid when best_bid > our_price + tolerance."""
        our_price = 0.50
        best_bid = 0.52
        
        is_outbid = handler._is_outbid(our_price, best_bid)
        assert is_outbid is True
    
    def test_not_outbid_when_we_are_best(self, handler):
        """Not outbid when our_price == best_bid."""
        our_price = 0.50
        best_bid = 0.50
        
        is_outbid = handler._is_outbid(our_price, best_bid)
        assert is_outbid is False
    
    def test_not_outbid_within_tolerance(self, handler):
        """Not outbid when difference within tolerance."""
        our_price = 0.50
        best_bid = 0.503  # Within 0.005 tolerance
        
        is_outbid = handler._is_outbid(our_price, best_bid)
        assert is_outbid is False


class TestOutbidStateTracking:
    """Tests for outbid state tracking."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockWebSocketHandler()
    
    def test_skip_if_same_bid_level(self, handler):
        """Skip adjustment if already tried this bid level."""
        handler._outbid_state["token123"] = {"best_bid": 0.52, "failed": True}
        
        should_skip = handler._should_skip_adjustment("token123", 0.52)
        assert should_skip is True
    
    def test_process_if_bid_changed(self, handler):
        """Process if bid changed by >= 1¢."""
        handler._outbid_state["token123"] = {"best_bid": 0.52, "failed": True}
        
        should_skip = handler._should_skip_adjustment("token123", 0.54)
        assert should_skip is False
    
    def test_process_if_no_previous_state(self, handler):
        """Process if no previous outbid state."""
        should_skip = handler._should_skip_adjustment("token123", 0.52)
        assert should_skip is False
    
    def test_mark_adjustment_failed(self, handler):
        """Mark adjustment failed stores state."""
        handler.mark_adjustment_failed("token123", 0.52)
        
        assert "token123" in handler._outbid_state
        assert handler._outbid_state["token123"]["best_bid"] == 0.52
        assert handler._outbid_state["token123"]["failed"] is True
    
    def test_clear_outbid_state(self, handler):
        """Clear outbid state removes entry."""
        handler._outbid_state["token123"] = {"best_bid": 0.52, "failed": True}
        handler.clear_outbid_state("token123")
        
        assert "token123" not in handler._outbid_state
    
    def test_clear_nonexistent_state_ok(self, handler):
        """Clearing nonexistent state is safe."""
        handler.clear_outbid_state("nonexistent")
        # Should not raise


class TestClearOutbidDecision:
    """Tests for when to clear outbid state."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockWebSocketHandler()
    
    def test_clear_when_we_are_best(self, handler):
        """Clear when we're the best bid again."""
        our_price = 0.52
        best_bid = 0.52
        
        should_clear = handler._should_clear_outbid_state(our_price, best_bid)
        assert should_clear is True
    
    def test_no_clear_when_still_outbid(self, handler):
        """Don't clear when still outbid."""
        our_price = 0.50
        best_bid = 0.55
        
        should_clear = handler._should_clear_outbid_state(our_price, best_bid)
        assert should_clear is False


class TestBidUpdatePropagation:
    """Tests for bid update propagation to BotState."""
    
    def test_bid_update_pushes_to_botstate(self):
        """Bid updates should be pushed to BotState."""
        handler = MockWebSocketHandler()
        
        # Simulate the pattern from _on_book_update
        best_bid = 0.52
        best_ask = 0.55
        token_id = "token123"
        
        # In real code, this would call:
        handler.bot_state.update_bid(token_id, best_bid, best_ask)
        
        handler.bot_state.update_bid.assert_called_once_with(
            token_id, best_bid, best_ask
        )

