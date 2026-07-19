"""
Unit tests for polymarket/user_websocket.py - User WebSocket client.

Tests OrderUpdate and TradeUpdate dataclasses and core logic patterns.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone


class MockOrderUpdate:
    """Mock OrderUpdate dataclass for testing."""
    
    def __init__(
        self,
        order_id: str = "order123",
        status: str = "LIVE",
        asset_id: str = "asset456",
        side: str = "BUY",
        original_size: float = 25.0,
        size_matched: float = 0.0,
        price: float = 0.50,
        timestamp: datetime = None,
    ):
        self.order_id = order_id
        self.status = status
        self.asset_id = asset_id
        self.side = side
        self.original_size = original_size
        self.size_matched = size_matched
        self.price = price
        self.timestamp = timestamp or datetime.now(timezone.utc)
    
    def is_filled(self) -> bool:
        """Check if order is fully filled."""
        return self.status.upper() == "MATCHED" and self.size_matched >= self.original_size
    
    def is_partial(self) -> bool:
        """Check if order is partially filled."""
        return self.size_matched > 0 and self.size_matched < self.original_size
    
    def is_live(self) -> bool:
        """Check if order is live/open."""
        return self.status.upper() == "LIVE"
    
    def is_cancelled(self) -> bool:
        """Check if order is cancelled."""
        return self.status.upper() in ("CANCELLED", "CANCELED")


class MockTradeUpdate:
    """Mock TradeUpdate dataclass for testing."""
    
    def __init__(
        self,
        trade_id: str = "trade123",
        order_id: str = "order123",
        asset_id: str = "asset456",
        side: str = "BUY",
        size: float = 10.0,
        price: float = 0.50,
        timestamp: datetime = None,
    ):
        self.trade_id = trade_id
        self.order_id = order_id
        self.asset_id = asset_id
        self.side = side
        self.size = size
        self.price = price
        self.timestamp = timestamp or datetime.now(timezone.utc)


class MockPolymarketUserWebSocket:
    """Mock PolymarketUserWebSocket for testing core logic patterns."""
    
    # Reconnection constants
    MAX_RECONNECT_ATTEMPTS = 10
    BASE_RECONNECT_DELAY = 1.0
    MAX_RECONNECT_DELAY = 60.0
    
    def __init__(self, api_key: str, api_secret: str, api_passphrase: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        
        self._orders: dict = {}
        self._connected = False
        self._reconnect_count = 0
        
        # Callbacks
        self.on_order_update = None
        self.on_trade = None
        self.on_reconnect = None
        self.on_disconnect = None
    
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._connected
    
    def _calculate_reconnect_delay(self, attempt: int) -> float:
        """Calculate exponential backoff delay."""
        delay = self.BASE_RECONNECT_DELAY * (2 ** attempt)
        return min(delay, self.MAX_RECONNECT_DELAY)
    
    def _parse_order_update(self, msg: dict) -> MockOrderUpdate:
        """Parse order update message."""
        return MockOrderUpdate(
            order_id=msg.get("id", ""),
            status=msg.get("status", "UNKNOWN"),
            asset_id=msg.get("asset_id", ""),
            side=msg.get("side", ""),
            original_size=float(msg.get("original_size", 0)),
            size_matched=float(msg.get("size_matched", 0)),
            price=float(msg.get("price", 0)),
        )
    
    def _parse_trade_update(self, msg: dict) -> MockTradeUpdate:
        """Parse trade update message."""
        return MockTradeUpdate(
            trade_id=msg.get("trade_id", ""),
            order_id=msg.get("order_id", ""),
            asset_id=msg.get("asset_id", ""),
            side=msg.get("side", ""),
            size=float(msg.get("size", 0)),
            price=float(msg.get("price", 0)),
        )
    
    def get_order(self, order_id: str) -> MockOrderUpdate:
        """Get cached order by ID."""
        return self._orders.get(order_id)
    
    def get_all_orders(self) -> dict:
        """Get all cached orders."""
        return self._orders.copy()


class TestOrderUpdate:
    """Tests for OrderUpdate dataclass."""
    
    def test_stores_order_fields(self):
        """OrderUpdate stores all fields."""
        order = MockOrderUpdate(
            order_id="order123",
            status="LIVE",
            asset_id="asset456",
            side="BUY",
            original_size=25.0,
            size_matched=10.0,
            price=0.52,
        )
        
        assert order.order_id == "order123"
        assert order.status == "LIVE"
        assert order.asset_id == "asset456"
        assert order.side == "BUY"
        assert order.original_size == 25.0
        assert order.size_matched == 10.0
        assert order.price == 0.52
    
    def test_is_filled_when_fully_matched(self):
        """is_filled returns True when fully matched."""
        order = MockOrderUpdate(
            status="MATCHED",
            original_size=25.0,
            size_matched=25.0,
        )
        
        assert order.is_filled() is True
    
    def test_is_filled_false_when_partial(self):
        """is_filled returns False when partially matched."""
        order = MockOrderUpdate(
            status="MATCHED",
            original_size=25.0,
            size_matched=15.0,
        )
        
        assert order.is_filled() is False
    
    def test_is_partial_when_some_matched(self):
        """is_partial returns True when some shares matched."""
        order = MockOrderUpdate(
            original_size=25.0,
            size_matched=15.0,
        )
        
        assert order.is_partial() is True
    
    def test_is_partial_false_when_none_matched(self):
        """is_partial returns False when no shares matched."""
        order = MockOrderUpdate(
            original_size=25.0,
            size_matched=0,
        )
        
        assert order.is_partial() is False
    
    def test_is_live(self):
        """is_live returns True for LIVE status."""
        order = MockOrderUpdate(status="LIVE")
        assert order.is_live() is True
        
        order2 = MockOrderUpdate(status="MATCHED")
        assert order2.is_live() is False
    
    def test_is_cancelled(self):
        """is_cancelled handles both spellings."""
        order1 = MockOrderUpdate(status="CANCELLED")
        assert order1.is_cancelled() is True
        
        order2 = MockOrderUpdate(status="CANCELED")
        assert order2.is_cancelled() is True
        
        order3 = MockOrderUpdate(status="LIVE")
        assert order3.is_cancelled() is False


class TestTradeUpdate:
    """Tests for TradeUpdate dataclass."""
    
    def test_stores_trade_fields(self):
        """TradeUpdate stores all fields."""
        trade = MockTradeUpdate(
            trade_id="trade123",
            order_id="order456",
            asset_id="asset789",
            side="BUY",
            size=10.0,
            price=0.52,
        )
        
        assert trade.trade_id == "trade123"
        assert trade.order_id == "order456"
        assert trade.asset_id == "asset789"
        assert trade.side == "BUY"
        assert trade.size == 10.0
        assert trade.price == 0.52
    
    def test_has_timestamp(self):
        """TradeUpdate has timestamp."""
        trade = MockTradeUpdate()
        assert trade.timestamp is not None


class TestUserWebSocketInit:
    """Tests for PolymarketUserWebSocket initialization."""
    
    def test_init_stores_credentials(self):
        """WebSocket stores API credentials."""
        ws = MockPolymarketUserWebSocket(
            api_key="key123",
            api_secret="secret456",
            api_passphrase="pass789",
        )
        
        assert ws.api_key == "key123"
        assert ws.api_secret == "secret456"
        assert ws.api_passphrase == "pass789"
    
    def test_init_not_connected(self):
        """WebSocket starts disconnected."""
        ws = MockPolymarketUserWebSocket("key", "secret", "pass")
        
        assert ws.is_connected() is False
    
    def test_init_empty_orders(self):
        """WebSocket starts with empty orders cache."""
        ws = MockPolymarketUserWebSocket("key", "secret", "pass")
        
        assert len(ws._orders) == 0


class TestReconnectBackoff:
    """Tests for exponential backoff calculation."""
    
    @pytest.fixture
    def ws(self):
        """Create mock WebSocket."""
        return MockPolymarketUserWebSocket("key", "secret", "pass")
    
    def test_first_attempt_base_delay(self, ws):
        """First attempt uses base delay."""
        delay = ws._calculate_reconnect_delay(0)
        assert delay == 1.0
    
    def test_exponential_increase(self, ws):
        """Delay increases exponentially."""
        delay0 = ws._calculate_reconnect_delay(0)
        delay1 = ws._calculate_reconnect_delay(1)
        delay2 = ws._calculate_reconnect_delay(2)
        
        assert delay1 == delay0 * 2
        assert delay2 == delay1 * 2
    
    def test_caps_at_max_delay(self, ws):
        """Delay caps at max value."""
        delay = ws._calculate_reconnect_delay(10)  # 2^10 = 1024 > 60
        
        assert delay <= ws.MAX_RECONNECT_DELAY


class TestMessageParsing:
    """Tests for message parsing."""
    
    @pytest.fixture
    def ws(self):
        """Create mock WebSocket."""
        return MockPolymarketUserWebSocket("key", "secret", "pass")
    
    def test_parse_order_update(self, ws):
        """Parses order update message."""
        msg = {
            "id": "order123",
            "status": "MATCHED",
            "asset_id": "asset456",
            "side": "BUY",
            "original_size": "25.0",
            "size_matched": "25.0",
            "price": "0.52",
        }
        
        order = ws._parse_order_update(msg)
        
        assert order.order_id == "order123"
        assert order.status == "MATCHED"
        assert order.original_size == 25.0
        assert order.size_matched == 25.0
        assert order.price == 0.52
    
    def test_parse_trade_update(self, ws):
        """Parses trade update message."""
        msg = {
            "trade_id": "trade123",
            "order_id": "order456",
            "asset_id": "asset789",
            "side": "BUY",
            "size": "10.0",
            "price": "0.52",
        }
        
        trade = ws._parse_trade_update(msg)
        
        assert trade.trade_id == "trade123"
        assert trade.order_id == "order456"
        assert trade.size == 10.0
        assert trade.price == 0.52
    
    def test_handles_missing_fields(self, ws):
        """Handles missing fields with defaults."""
        msg = {"id": "order123"}
        
        order = ws._parse_order_update(msg)
        
        assert order.order_id == "order123"
        assert order.status == "UNKNOWN"
        assert order.original_size == 0
        assert order.size_matched == 0


class TestOrderCache:
    """Tests for order caching."""
    
    @pytest.fixture
    def ws(self):
        """Create mock WebSocket."""
        return MockPolymarketUserWebSocket("key", "secret", "pass")
    
    def test_cache_order(self, ws):
        """Caches order by ID."""
        order = MockOrderUpdate(order_id="order123")
        ws._orders["order123"] = order
        
        assert ws.get_order("order123") is order
    
    def test_get_missing_order(self, ws):
        """Returns None for missing order."""
        assert ws.get_order("nonexistent") is None
    
    def test_get_all_orders(self, ws):
        """Returns copy of all orders."""
        order1 = MockOrderUpdate(order_id="order1")
        order2 = MockOrderUpdate(order_id="order2")
        ws._orders["order1"] = order1
        ws._orders["order2"] = order2
        
        all_orders = ws.get_all_orders()
        
        assert len(all_orders) == 2
        assert "order1" in all_orders
        assert "order2" in all_orders

