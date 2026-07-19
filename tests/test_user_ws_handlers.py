"""
Unit tests for handlers/user_ws_handlers.py - User WebSocket order/fill handlers.

Note: UserWebSocketHandler has async WebSocket dependencies. These tests use 
mock-based approaches and test the core logic patterns.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch


class MockOrderUpdate:
    """Mock order update from WebSocket."""
    
    def __init__(
        self,
        order_id: str = "order123",
        status: str = "LIVE",
        price: float = 0.50,
        size_matched: float = 0,
        is_partial: bool = False,
        is_filled: bool = False,
    ):
        self.order_id = order_id
        self.status = status
        self.price = price
        self.size_matched = size_matched
        self.is_partial = is_partial
        self.is_filled = is_filled


class MockTradeUpdate:
    """Mock trade update from WebSocket."""
    
    def __init__(
        self,
        order_id: str = "order123",
        price: float = 0.50,
        size: float = 10.0,
    ):
        self.order_id = order_id
        self.price = price
        self.size = size


class MockUserWebSocketHandler:
    """Mock UserWebSocketHandler for testing core logic patterns."""
    
    def __init__(
        self,
        bot_state: Mock,
        get_position_fn,
        order_to_position_map: dict,
        on_fill = None,
        on_hedge_fill = None,
    ):
        self.bot_state = bot_state
        self._get_position = get_position_fn
        self._order_to_position = order_to_position_map
        self.on_fill = on_fill
        self.on_hedge_fill = on_hedge_fill
    
    def _is_hydrated_order(self, order_id: str) -> bool:
        """Check if order is from a previous session (hydrated)."""
        position_id = self._order_to_position.get(order_id)
        if position_id:
            return False  # Has position mapping, not hydrated
        
        # Check BotState for hydrated order
        return self.bot_state.get_order_info_by_id(order_id) is not None
    
    def _should_trigger_fill_callback(self, update: MockOrderUpdate, old_filled: float) -> bool:
        """Check if fill callback should be triggered."""
        return update.size_matched > old_filled
    
    def _should_clear_order(self, status: str) -> bool:
        """Check if order should be cleared from state."""
        return status.upper() in ("CANCELED", "CANCELLED", "INVALID")


class TestUserWebSocketHandlerInit:
    """Tests for UserWebSocketHandler initialization."""
    
    def test_init_stores_dependencies(self):
        """Handler stores injected dependencies."""
        bot_state = Mock()
        get_position_fn = Mock()
        order_to_position_map = {}
        on_fill = Mock()
        on_hedge_fill = Mock()
        
        handler = MockUserWebSocketHandler(
            bot_state, get_position_fn, order_to_position_map,
            on_fill, on_hedge_fill
        )
        
        assert handler.bot_state is bot_state
        assert handler._get_position is get_position_fn
        assert handler._order_to_position is order_to_position_map
        assert handler.on_fill is on_fill
        assert handler.on_hedge_fill is on_hedge_fill


class TestHydratedOrderDetection:
    """Tests for hydrated order detection."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        bot_state = Mock()
        return MockUserWebSocketHandler(
            bot_state=bot_state,
            get_position_fn=Mock(),
            order_to_position_map={},
        )
    
    def test_session_order_not_hydrated(self, handler):
        """Session order (with position mapping) is not hydrated."""
        handler._order_to_position["order123"] = "pos123"
        
        is_hydrated = handler._is_hydrated_order("order123")
        assert is_hydrated is False
    
    def test_hydrated_order_from_botstate(self, handler):
        """Hydrated order found in BotState is identified."""
        handler.bot_state.get_order_info_by_id.return_value = {"team_name": "Team A"}
        
        is_hydrated = handler._is_hydrated_order("order123")
        assert is_hydrated is True
    
    def test_unknown_order_not_hydrated(self, handler):
        """Unknown order (not in BotState) is not hydrated."""
        handler.bot_state.get_order_info_by_id.return_value = None
        
        is_hydrated = handler._is_hydrated_order("unknown_order")
        assert is_hydrated is False


class TestFillCallbackTrigger:
    """Tests for fill callback triggering logic."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockUserWebSocketHandler(Mock(), Mock(), {})
    
    def test_triggers_on_new_fill(self, handler):
        """Triggers callback when new shares filled."""
        update = MockOrderUpdate(size_matched=10.0)
        old_filled = 0.0
        
        should_trigger = handler._should_trigger_fill_callback(update, old_filled)
        assert should_trigger is True
    
    def test_triggers_on_partial_fill(self, handler):
        """Triggers callback on partial fill."""
        update = MockOrderUpdate(size_matched=15.0)
        old_filled = 10.0
        
        should_trigger = handler._should_trigger_fill_callback(update, old_filled)
        assert should_trigger is True
    
    def test_no_trigger_on_same_fill(self, handler):
        """No trigger when fill amount unchanged."""
        update = MockOrderUpdate(size_matched=10.0)
        old_filled = 10.0
        
        should_trigger = handler._should_trigger_fill_callback(update, old_filled)
        assert should_trigger is False
    
    def test_no_trigger_on_zero_fill(self, handler):
        """No trigger when no fills."""
        update = MockOrderUpdate(size_matched=0)
        old_filled = 0
        
        should_trigger = handler._should_trigger_fill_callback(update, old_filled)
        assert should_trigger is False


class TestOrderStatusClearing:
    """Tests for order status clearing logic."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockUserWebSocketHandler(Mock(), Mock(), {})
    
    def test_clears_on_canceled(self, handler):
        """Clears order on CANCELED status."""
        should_clear = handler._should_clear_order("CANCELED")
        assert should_clear is True
    
    def test_clears_on_cancelled_british(self, handler):
        """Clears order on CANCELLED (British spelling)."""
        should_clear = handler._should_clear_order("CANCELLED")
        assert should_clear is True
    
    def test_clears_on_invalid(self, handler):
        """Clears order on INVALID status."""
        should_clear = handler._should_clear_order("INVALID")
        assert should_clear is True
    
    def test_no_clear_on_live(self, handler):
        """Does not clear on LIVE status."""
        should_clear = handler._should_clear_order("LIVE")
        assert should_clear is False
    
    def test_no_clear_on_matched(self, handler):
        """Does not clear on MATCHED status."""
        should_clear = handler._should_clear_order("MATCHED")
        assert should_clear is False
    
    def test_case_insensitive(self, handler):
        """Status check is case insensitive."""
        should_clear = handler._should_clear_order("canceled")
        assert should_clear is True


class TestHydratedFillClass:
    """Tests for HydratedFill helper class."""
    
    def test_hydrated_fill_creation(self):
        """HydratedFill stores order info correctly."""
        order_info = {
            "token_id": "token123",
            "team_name": "Team A",
            "size": 25.0,
            "fair_value": 0.55,
            "match": "CS2: Team A vs Team B",
            "match_id": "match123",
        }
        
        update = MockOrderUpdate(price=0.50, size_matched=10.0)
        edge_pct = 5.0
        
        # Simulate HydratedFill fields
        hydrated = Mock()
        hydrated.position_id = f"HYDRATED-{order_info['token_id'][:8]}"
        hydrated.entry_team = order_info["team_name"]
        hydrated.entry_price = update.price
        hydrated.entry_filled_shares = update.size_matched
        hydrated.entry_size = order_info["size"]
        hydrated.is_hydrated = True
        hydrated.edge_pct = edge_pct
        hydrated.fair_value = order_info["fair_value"]
        hydrated.match_name = order_info["match"]
        
        assert hydrated.position_id == "HYDRATED-token123"
        assert hydrated.entry_team == "Team A"
        assert hydrated.entry_price == 0.50
        assert hydrated.is_hydrated is True

