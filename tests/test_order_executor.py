"""
Unit tests for execution/order_executor.py - Order placement and cancellation.
"""
import pytest
from datetime import datetime
from unittest.mock import Mock, patch, AsyncMock

from src.execution.order_executor import (
    OrderSide,
    OrderStatus,
    Order,
    OrderExecutor,
)


class TestOrderSide:
    """Tests for OrderSide enum."""
    
    def test_order_sides_exist(self):
        """Verify BUY and SELL exist."""
        assert OrderSide.BUY.value == "BUY"
        assert OrderSide.SELL.value == "SELL"


class TestOrderStatus:
    """Tests for OrderStatus enum."""
    
    def test_all_statuses_exist(self):
        """Verify all order statuses exist."""
        statuses = [
            OrderStatus.PENDING,
            OrderStatus.OPEN,
            OrderStatus.PARTIAL,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.FAILED,
        ]
        assert len(statuses) == 6


class TestOrder:
    """Tests for Order dataclass."""
    
    def create_order(self, **kwargs) -> Order:
        """Helper to create an Order."""
        defaults = {
            "order_id": "order123",
            "market_id": "token456",
            "side": OrderSide.BUY,
            "price": 0.50,
            "size": 10.0,
        }
        defaults.update(kwargs)
        return Order(**defaults)
    
    def test_is_complete_filled(self):
        """Filled order is complete."""
        order = self.create_order(status=OrderStatus.FILLED)
        assert order.is_complete is True
    
    def test_is_complete_cancelled(self):
        """Cancelled order is complete."""
        order = self.create_order(status=OrderStatus.CANCELLED)
        assert order.is_complete is True
    
    def test_is_complete_failed(self):
        """Failed order is complete."""
        order = self.create_order(status=OrderStatus.FAILED)
        assert order.is_complete is True
    
    def test_is_complete_open(self):
        """Open order is not complete."""
        order = self.create_order(status=OrderStatus.OPEN)
        assert order.is_complete is False
    
    def test_is_complete_partial(self):
        """Partial order is not complete."""
        order = self.create_order(status=OrderStatus.PARTIAL)
        assert order.is_complete is False
    
    def test_remaining_size(self):
        """Remaining size = size - filled_size."""
        order = self.create_order(size=10.0, filled_size=3.0)
        assert abs(order.remaining_size - 7.0) < 0.01
    
    def test_remaining_size_unfilled(self):
        """Unfilled order has full remaining size."""
        order = self.create_order(size=10.0, filled_size=0.0)
        assert abs(order.remaining_size - 10.0) < 0.01


class TestOrderExecutorInit:
    """Tests for OrderExecutor initialization."""
    
    def test_paper_trading_default(self):
        """Paper trading is enabled by default."""
        executor = OrderExecutor()
        assert executor.paper_trading is True
    
    def test_paper_trading_no_key_required(self):
        """Paper trading doesn't require private key."""
        executor = OrderExecutor(paper_trading=True, private_key=None)
        assert executor.paper_trading is True
    
    @patch.dict("os.environ", {}, clear=True)
    def test_live_trading_requires_key(self):
        """Live trading requires private key."""
        with pytest.raises(ValueError, match="POLYMARKET_PRIVATE_KEY"):
            OrderExecutor(paper_trading=False, private_key=None)
    
    def test_live_trading_with_key(self):
        """Live trading works with private key."""
        executor = OrderExecutor(
            paper_trading=False,
            private_key="0x1234567890abcdef"
        )
        assert executor.paper_trading is False


class TestOrderExecutorCancelResponse:
    """Tests for cancel_order response parsing - THE CRITICAL BUG FIX."""
    
    @pytest.fixture
    def executor(self):
        """Create a live executor with mocked client."""
        executor = OrderExecutor(
            paper_trading=False,
            private_key="0x1234567890abcdef"
        )
        return executor
    
    @pytest.mark.asyncio
    async def test_cancel_success_returns_true_false(self, executor):
        """Successful cancel returns (True, False)."""
        # Mock the client to return success
        mock_client = Mock()
        mock_client.cancel.return_value = {
            "canceled": ["order123"],
            "not_canceled": {},
        }
        executor._client = mock_client
        
        success, was_already_complete = await executor.cancel_order(
            "order123", force=True
        )
        
        assert success is True
        assert was_already_complete is False
    
    @pytest.mark.asyncio
    async def test_cancel_not_canceled_returns_true_true(self, executor):
        """NOT_CANCELED response returns (True, True) to prevent duplicate placement."""
        # Mock the client to return not_canceled
        mock_client = Mock()
        mock_client.cancel.return_value = {
            "not_canceled": {
                "order123": "order can't be found - already canceled or matched"
            },
            "canceled": [],
        }
        executor._client = mock_client
        
        success, was_already_complete = await executor.cancel_order(
            "order123", force=True
        )
        
        # CRITICAL: was_already_complete must be True to prevent placing duplicate!
        assert success is True
        assert was_already_complete is True
    
    @pytest.mark.asyncio
    async def test_cancel_exception_already_canceled(self, executor):
        """Exception with 'already cancelled' returns (True, True)."""
        mock_client = Mock()
        mock_client.cancel.side_effect = Exception("Order already cancelled")
        executor._client = mock_client
        
        success, was_already_complete = await executor.cancel_order(
            "order123", force=True
        )
        
        assert success is True
        assert was_already_complete is True
    
    @pytest.mark.asyncio
    async def test_cancel_exception_not_found(self, executor):
        """Exception with 'not found' returns (True, True)."""
        mock_client = Mock()
        mock_client.cancel.side_effect = Exception("Order not found")
        executor._client = mock_client
        
        success, was_already_complete = await executor.cancel_order(
            "order123", force=True
        )
        
        assert success is True
        assert was_already_complete is True
    
    @pytest.mark.asyncio
    async def test_cancel_exception_other_error(self, executor):
        """Other exceptions return (False, False)."""
        mock_client = Mock()
        mock_client.cancel.side_effect = Exception("Connection timeout")
        executor._client = mock_client
        
        success, was_already_complete = await executor.cancel_order(
            "order123", force=True
        )
        
        assert success is False
        assert was_already_complete is False
    
    @pytest.mark.asyncio
    async def test_cancel_paper_trading(self):
        """Paper trading cancel always succeeds."""
        executor = OrderExecutor(paper_trading=True)
        
        # Register an order first
        order = Order(
            order_id="paper123",
            market_id="token456",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            status=OrderStatus.OPEN,
        )
        executor._orders["paper123"] = order
        
        success, was_already_complete = await executor.cancel_order("paper123")
        
        assert success is True
        assert was_already_complete is False
        assert order.status == OrderStatus.CANCELLED


class TestOrderExecutorPlaceOrder:
    """Tests for place_limit_order."""
    
    @pytest.mark.asyncio
    async def test_paper_order_generates_paper_id(self):
        """Paper orders have PAPER- prefix."""
        executor = OrderExecutor(paper_trading=True)
        
        order = await executor.place_limit_order(
            token_id="token123",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        
        assert order.order_id.startswith("PAPER-")
        # Paper orders auto-fill immediately in the simulator
        assert order.status in [OrderStatus.OPEN, OrderStatus.FILLED]
    
    @pytest.mark.asyncio
    async def test_paper_order_tracked(self):
        """Paper orders are tracked internally."""
        executor = OrderExecutor(paper_trading=True)
        
        order = await executor.place_limit_order(
            token_id="token123",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        
        assert order.order_id in executor._orders
    
    @pytest.mark.asyncio
    async def test_order_fields_set_correctly(self):
        """Order fields are set correctly."""
        executor = OrderExecutor(paper_trading=True)
        
        order = await executor.place_limit_order(
            token_id="token123",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        
        assert order.market_id == "token123"
        assert order.side == OrderSide.BUY
        assert order.price == 0.50
        assert order.size == 10.0
    
    @pytest.mark.asyncio
    async def test_live_order_with_mock_client(self):
        """Live order placement with mocked client."""
        executor = OrderExecutor(
            paper_trading=False,
            private_key="0x1234567890abcdef"
        )
        
        # Mock the client
        mock_client = Mock()
        mock_client.create_order.return_value = Mock()
        mock_client.post_order.return_value = {"orderID": "live_order_123"}
        executor._client = mock_client
        
        order = await executor.place_limit_order(
            token_id="token123",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        
        assert order.order_id == "live_order_123"
        assert order.status == OrderStatus.OPEN


class TestOrderExecutorGetOrders:
    """Tests for order retrieval methods."""
    
    @pytest.fixture
    def executor_with_orders(self):
        """Create executor with various orders."""
        executor = OrderExecutor(paper_trading=True)
        
        executor._orders = {
            "open1": Order(
                order_id="open1", market_id="t1", side=OrderSide.BUY,
                price=0.50, size=10.0, status=OrderStatus.OPEN
            ),
            "open2": Order(
                order_id="open2", market_id="t2", side=OrderSide.BUY,
                price=0.45, size=10.0, status=OrderStatus.OPEN
            ),
            "filled1": Order(
                order_id="filled1", market_id="t3", side=OrderSide.BUY,
                price=0.50, size=10.0, status=OrderStatus.FILLED
            ),
            "cancelled1": Order(
                order_id="cancelled1", market_id="t4", side=OrderSide.BUY,
                price=0.50, size=10.0, status=OrderStatus.CANCELLED
            ),
        }
        
        return executor
    
    def test_get_all_orders(self, executor_with_orders):
        """get_all_orders returns all orders."""
        orders = executor_with_orders.get_all_orders()
        assert len(orders) == 4
    
    def test_get_open_orders(self, executor_with_orders):
        """get_open_orders returns only open/partial orders."""
        orders = executor_with_orders.get_open_orders()
        assert len(orders) == 2
        assert all(o.order_id.startswith("open") for o in orders)
    
    def test_get_filled_orders(self, executor_with_orders):
        """get_filled_orders returns only filled orders."""
        orders = executor_with_orders.get_filled_orders()
        assert len(orders) == 1
        assert orders[0].order_id == "filled1"
    
    def test_order_exists_in_dict(self, executor_with_orders):
        """Orders are stored in _orders dict."""
        assert "open1" in executor_with_orders._orders
        assert executor_with_orders._orders["open1"].status == OrderStatus.OPEN
    
    def test_order_not_in_dict(self, executor_with_orders):
        """Unknown orders are not in _orders dict."""
        assert "unknown" not in executor_with_orders._orders

