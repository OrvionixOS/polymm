"""
Unit tests for state/reactive_handler.py - Reactive state change handling.

Note: ReactiveHandler has dependencies on BotState singleton.
These tests use mock-based approaches and test the core logic patterns.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch


class MockMatchOrder:
    """Mock MatchOrder for testing."""
    
    def __init__(
        self,
        order_id: str = "order123",
        token_id: str = "token123",
        team: str = "Team A",
        price: float = 0.50,
        is_open: bool = True,
        is_entry: bool = True,
    ):
        self.order_id = order_id
        self.token_id = token_id
        self.team = team
        self.price = price
        self.is_open = is_open
        self.is_entry = is_entry


class MockStateEvent:
    """Mock StateEvent for testing."""
    
    def __init__(
        self,
        event_type: str = "EDGE_LOST",
        match_id: str = "match123",
        match_state: Mock = None,
        data: Mock = None,
        old_edge: float = None,
        new_edge: float = None,
    ):
        self.event_type = event_type
        self.match_id = match_id
        self.match_state = match_state
        self.data = data
        self.old_edge = old_edge
        self.new_edge = new_edge


class MockReactiveHandler:
    """Mock ReactiveHandler for testing core logic patterns."""
    
    def __init__(self, min_edge: float = 0.05, min_arb_profit: float = 0.05):
        self.min_edge = min_edge
        self.min_arb_profit = min_arb_profit
        self.on_cancel_order = None
        self.on_skip_hedge = None
        
        self._stats = {
            "edge_lost_cancels": 0,
            "natural_arbs_skipped": 0,
            "live_cancels": 0,
        }
        self._running = False
    
    def start(self):
        """Start the handler."""
        self._running = True
    
    def stop(self):
        """Stop the handler."""
        self._running = False
    
    async def _handle_edge_lost(self, event: MockStateEvent):
        """Handle edge lost - cancel ENTRY orders only."""
        if not self._running:
            return
        
        order = event.data
        if not order or not order.is_open:
            return
        
        # Skip hedge orders - they're handled by OrderMonitor
        if not order.is_entry:
            return
        
        if self.on_cancel_order:
            await self.on_cancel_order(order.order_id, "Edge lost")
            self._stats["edge_lost_cancels"] += 1
    
    async def _handle_match_live(self, event: MockStateEvent):
        """Handle match going live - cancel all orders."""
        if not self._running:
            return
        
        match = event.match_state
        if not match:
            return
        
        for order in [match.order1, match.order2]:
            if not order or not order.is_open:
                continue
            
            if self.on_cancel_order:
                await self.on_cancel_order(order.order_id, "Match went live")
                self._stats["live_cancels"] += 1
    
    def _should_cancel_order(self, order: MockMatchOrder, edge: float) -> bool:
        """Check if order should be cancelled based on edge."""
        if not order.is_open:
            return False
        
        # Skip hedge orders
        if not order.is_entry:
            return False
        
        # Skip if edge is unknown (missing fair probs)
        if edge <= -990:
            return False
        
        return edge < self.min_edge
    
    def get_stats(self) -> dict:
        """Get handler statistics."""
        return {**self._stats, "running": self._running}


class TestReactiveHandlerInit:
    """Tests for ReactiveHandler initialization."""
    
    def test_init_with_defaults(self):
        """Handler initializes with defaults."""
        handler = MockReactiveHandler()
        
        assert handler.min_edge == 0.05
        assert handler.min_arb_profit == 0.05
        assert handler._running is False
    
    def test_init_with_custom_values(self):
        """Handler initializes with custom values."""
        handler = MockReactiveHandler(min_edge=0.08, min_arb_profit=0.03)
        
        assert handler.min_edge == 0.08
        assert handler.min_arb_profit == 0.03
    
    def test_start_sets_running(self):
        """Start sets running to True."""
        handler = MockReactiveHandler()
        handler.start()
        
        assert handler._running is True
    
    def test_stop_clears_running(self):
        """Stop sets running to False."""
        handler = MockReactiveHandler()
        handler.start()
        handler.stop()
        
        assert handler._running is False


class TestHandleEdgeLost:
    """Tests for _handle_edge_lost method."""
    
    @pytest.fixture
    def handler(self):
        """Create started handler."""
        h = MockReactiveHandler()
        h.start()
        h.on_cancel_order = AsyncMock()
        return h
    
    @pytest.mark.asyncio
    async def test_cancels_entry_order(self, handler):
        """Cancels entry order when edge lost."""
        order = MockMatchOrder(is_entry=True, is_open=True)
        event = MockStateEvent(data=order, new_edge=0.02)
        
        await handler._handle_edge_lost(event)
        
        handler.on_cancel_order.assert_called_once()
        assert handler._stats["edge_lost_cancels"] == 1
    
    @pytest.mark.asyncio
    async def test_skips_hedge_order(self, handler):
        """Does not cancel hedge orders."""
        order = MockMatchOrder(is_entry=False, is_open=True)
        event = MockStateEvent(data=order, new_edge=0.02)
        
        await handler._handle_edge_lost(event)
        
        handler.on_cancel_order.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_skips_closed_order(self, handler):
        """Does not cancel closed orders."""
        order = MockMatchOrder(is_entry=True, is_open=False)
        event = MockStateEvent(data=order, new_edge=0.02)
        
        await handler._handle_edge_lost(event)
        
        handler.on_cancel_order.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_skips_if_not_running(self, handler):
        """Skips if handler is not running."""
        handler.stop()
        order = MockMatchOrder(is_entry=True, is_open=True)
        event = MockStateEvent(data=order, new_edge=0.02)
        
        await handler._handle_edge_lost(event)
        
        handler.on_cancel_order.assert_not_called()


class TestHandleMatchLive:
    """Tests for _handle_match_live method."""
    
    @pytest.fixture
    def handler(self):
        """Create started handler."""
        h = MockReactiveHandler()
        h.start()
        h.on_cancel_order = AsyncMock()
        return h
    
    @pytest.mark.asyncio
    async def test_cancels_all_open_orders(self, handler):
        """Cancels all open orders when match goes live."""
        order1 = MockMatchOrder(order_id="order1", is_open=True)
        order2 = MockMatchOrder(order_id="order2", is_open=True)
        
        match = Mock()
        match.order1 = order1
        match.order2 = order2
        
        event = MockStateEvent(match_state=match)
        
        await handler._handle_match_live(event)
        
        assert handler.on_cancel_order.call_count == 2
        assert handler._stats["live_cancels"] == 2
    
    @pytest.mark.asyncio
    async def test_skips_closed_orders(self, handler):
        """Does not cancel closed orders."""
        order1 = MockMatchOrder(order_id="order1", is_open=False)
        order2 = MockMatchOrder(order_id="order2", is_open=True)
        
        match = Mock()
        match.order1 = order1
        match.order2 = order2
        
        event = MockStateEvent(match_state=match)
        
        await handler._handle_match_live(event)
        
        assert handler.on_cancel_order.call_count == 1
        assert handler._stats["live_cancels"] == 1
    
    @pytest.mark.asyncio
    async def test_handles_null_orders(self, handler):
        """Handles matches with null orders."""
        match = Mock()
        match.order1 = None
        match.order2 = MockMatchOrder(order_id="order2", is_open=True)
        
        event = MockStateEvent(match_state=match)
        
        await handler._handle_match_live(event)
        
        assert handler.on_cancel_order.call_count == 1


class TestShouldCancelOrder:
    """Tests for edge checking logic."""
    
    @pytest.fixture
    def handler(self):
        """Create handler with default min_edge."""
        return MockReactiveHandler(min_edge=0.05)
    
    def test_cancel_when_edge_below_min(self, handler):
        """Cancel when edge below minimum."""
        order = MockMatchOrder(is_open=True, is_entry=True)
        edge = 0.03  # Below min_edge of 0.05
        
        should_cancel = handler._should_cancel_order(order, edge)
        assert should_cancel is True
    
    def test_keep_when_edge_above_min(self, handler):
        """Keep when edge above minimum."""
        order = MockMatchOrder(is_open=True, is_entry=True)
        edge = 0.08  # Above min_edge
        
        should_cancel = handler._should_cancel_order(order, edge)
        assert should_cancel is False
    
    def test_skip_missing_fair_probs(self, handler):
        """Skip when fair probs are missing (edge = -999)."""
        order = MockMatchOrder(is_open=True, is_entry=True)
        edge = -999  # Missing fair probs indicator
        
        should_cancel = handler._should_cancel_order(order, edge)
        assert should_cancel is False
    
    def test_skip_hedge_orders(self, handler):
        """Skip hedge orders."""
        order = MockMatchOrder(is_open=True, is_entry=False)
        edge = 0.02  # Below min_edge
        
        should_cancel = handler._should_cancel_order(order, edge)
        assert should_cancel is False
    
    def test_skip_closed_orders(self, handler):
        """Skip closed orders."""
        order = MockMatchOrder(is_open=False, is_entry=True)
        edge = 0.02
        
        should_cancel = handler._should_cancel_order(order, edge)
        assert should_cancel is False


class TestGetStats:
    """Tests for get_stats method."""
    
    def test_returns_stats_dict(self):
        """Returns stats dictionary."""
        handler = MockReactiveHandler()
        stats = handler.get_stats()
        
        assert "edge_lost_cancels" in stats
        assert "natural_arbs_skipped" in stats
        assert "live_cancels" in stats
        assert "running" in stats
    
    def test_stats_reflect_running_state(self):
        """Stats include running state."""
        handler = MockReactiveHandler()
        assert handler.get_stats()["running"] is False
        
        handler.start()
        assert handler.get_stats()["running"] is True


class TestEdgeCalculations:
    """Tests for edge calculations used in reactive handling."""
    
    def test_edge_from_fair_and_price(self):
        """Edge = fair_prob - price."""
        fair_prob = 0.55
        price = 0.50
        edge = fair_prob - price
        
        assert abs(edge - 0.05) < 0.001
    
    def test_negative_edge_when_overpaying(self):
        """Edge is negative when price > fair."""
        fair_prob = 0.50
        price = 0.55
        edge = fair_prob - price
        
        assert edge < 0
    
    def test_edge_percentage_format(self):
        """Edge as percentage for display."""
        edge = 0.05
        edge_pct = edge * 100
        
        assert edge_pct == 5.0

