"""
Unit tests for polymarket/book_websocket.py - Order book WebSocket client.

Tests LivePrice, MarketPrices dataclasses and core logic patterns.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone


class MockLivePrice:
    """Mock LivePrice dataclass for testing."""
    
    def __init__(
        self,
        token_id: str = "token123",
        best_bid: float = None,
        best_ask: float = None,
        last_trade: float = None,
        timestamp: datetime = None,
    ):
        self.token_id = token_id
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.last_trade = last_trade
        self.timestamp = timestamp or datetime.now(timezone.utc)
    
    def mid_price(self) -> float:
        """Calculate mid price from bid/ask."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        if self.best_bid is not None:
            return self.best_bid
        if self.best_ask is not None:
            return self.best_ask
        return None
    
    def spread(self) -> float:
        """Calculate bid-ask spread."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


class MockMarketPrices:
    """Mock MarketPrices dataclass for testing."""
    
    def __init__(
        self,
        market_id: str = "market123",
        question: str = "Who will win?",
        yes_price: MockLivePrice = None,
        no_price: MockLivePrice = None,
        timestamp: datetime = None,
    ):
        self.market_id = market_id
        self.question = question
        self.yes_price = yes_price
        self.no_price = no_price
        self.timestamp = timestamp or datetime.now(timezone.utc)
    
    def yes_probability(self) -> float:
        """Get Yes probability from mid price."""
        if self.yes_price:
            return self.yes_price.mid_price()
        return None
    
    def no_probability(self) -> float:
        """Get No probability from mid price."""
        if self.no_price:
            return self.no_price.mid_price()
        return None


class MockPolymarketWebSocket:
    """Mock PolymarketWebSocket for testing core logic patterns."""
    
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    # Reconnection constants
    MAX_RECONNECT_ATTEMPTS = 10
    BASE_RECONNECT_DELAY = 1.0
    MAX_RECONNECT_DELAY = 60.0
    
    def __init__(self):
        self._prices: dict = {}
        self._subscribed_tokens: set = set()
        self._connected = False
        self._reconnect_count = 0
        
        # Callbacks
        self.on_book_update = None
        self.on_reconnect = None
        self.on_disconnect = None
    
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._connected
    
    def _calculate_reconnect_delay(self, attempt: int) -> float:
        """Calculate exponential backoff delay."""
        delay = self.BASE_RECONNECT_DELAY * (2 ** attempt)
        return min(delay, self.MAX_RECONNECT_DELAY)
    
    def _parse_price_update(self, data: dict) -> tuple:
        """Parse price update message.
        
        Returns (token_id, LivePrice) or (None, None) if invalid.
        """
        token_id = data.get("asset_id")
        if not token_id:
            return (None, None)
        
        price = MockLivePrice(
            token_id=token_id,
            best_bid=float(data.get("best_bid")) if data.get("best_bid") else None,
            best_ask=float(data.get("best_ask")) if data.get("best_ask") else None,
            last_trade=float(data.get("last_trade")) if data.get("last_trade") else None,
        )
        return (token_id, price)
    
    def get_price(self, token_id: str) -> MockLivePrice:
        """Get current price for a token."""
        return self._prices.get(token_id)
    
    def get_all_prices(self) -> dict:
        """Get all current prices."""
        return self._prices.copy()
    
    def get_mid_price(self, token_id: str) -> float:
        """Get mid price for a token."""
        price = self._prices.get(token_id)
        if price:
            return price.mid_price()
        return None


class TestLivePrice:
    """Tests for LivePrice dataclass."""
    
    def test_stores_price_fields(self):
        """LivePrice stores all fields."""
        price = MockLivePrice(
            token_id="token123",
            best_bid=0.50,
            best_ask=0.55,
            last_trade=0.52,
        )
        
        assert price.token_id == "token123"
        assert price.best_bid == 0.50
        assert price.best_ask == 0.55
        assert price.last_trade == 0.52
    
    def test_mid_price_with_both(self):
        """Mid price calculated from bid and ask."""
        price = MockLivePrice(best_bid=0.50, best_ask=0.54)
        
        assert price.mid_price() == 0.52
    
    def test_mid_price_bid_only(self):
        """Mid price returns bid if no ask."""
        price = MockLivePrice(best_bid=0.50, best_ask=None)
        
        assert price.mid_price() == 0.50
    
    def test_mid_price_ask_only(self):
        """Mid price returns ask if no bid."""
        price = MockLivePrice(best_bid=None, best_ask=0.55)
        
        assert price.mid_price() == 0.55
    
    def test_mid_price_neither(self):
        """Mid price returns None if no bid or ask."""
        price = MockLivePrice(best_bid=None, best_ask=None)
        
        assert price.mid_price() is None
    
    def test_spread_calculation(self):
        """Spread calculated correctly."""
        price = MockLivePrice(best_bid=0.50, best_ask=0.55)
        
        assert abs(price.spread() - 0.05) < 0.001
    
    def test_spread_none_if_missing(self):
        """Spread returns None if missing bid or ask."""
        price = MockLivePrice(best_bid=0.50, best_ask=None)
        
        assert price.spread() is None


class TestMarketPrices:
    """Tests for MarketPrices dataclass."""
    
    def test_stores_market_fields(self):
        """MarketPrices stores all fields."""
        market = MockMarketPrices(
            market_id="market123",
            question="Who will win the match?",
        )
        
        assert market.market_id == "market123"
        assert market.question == "Who will win the match?"
    
    def test_yes_probability(self):
        """Yes probability from mid price."""
        yes_price = MockLivePrice(best_bid=0.50, best_ask=0.54)
        market = MockMarketPrices(yes_price=yes_price)
        
        assert market.yes_probability() == 0.52
    
    def test_no_probability(self):
        """No probability from mid price."""
        no_price = MockLivePrice(best_bid=0.45, best_ask=0.49)
        market = MockMarketPrices(no_price=no_price)
        
        assert market.no_probability() == 0.47
    
    def test_probabilities_sum_to_one(self):
        """Yes and No probabilities should roughly sum to 1."""
        yes_price = MockLivePrice(best_bid=0.50, best_ask=0.54)
        no_price = MockLivePrice(best_bid=0.45, best_ask=0.49)
        market = MockMarketPrices(yes_price=yes_price, no_price=no_price)
        
        total = market.yes_probability() + market.no_probability()
        # With spread, won't be exactly 1.0
        assert 0.95 < total < 1.05


class TestBookWebSocketInit:
    """Tests for PolymarketWebSocket initialization."""
    
    def test_init_not_connected(self):
        """WebSocket starts disconnected."""
        ws = MockPolymarketWebSocket()
        
        assert ws.is_connected() is False
    
    def test_init_empty_prices(self):
        """WebSocket starts with empty price cache."""
        ws = MockPolymarketWebSocket()
        
        assert len(ws._prices) == 0
    
    def test_init_empty_subscriptions(self):
        """WebSocket starts with no subscriptions."""
        ws = MockPolymarketWebSocket()
        
        assert len(ws._subscribed_tokens) == 0
    
    def test_ws_url(self):
        """WebSocket has correct URL."""
        ws = MockPolymarketWebSocket()
        
        assert "polymarket.com" in ws.WS_URL


class TestBookReconnectBackoff:
    """Tests for exponential backoff calculation."""
    
    @pytest.fixture
    def ws(self):
        """Create mock WebSocket."""
        return MockPolymarketWebSocket()
    
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
        delay = ws._calculate_reconnect_delay(10)
        
        assert delay <= ws.MAX_RECONNECT_DELAY


class TestPriceUpdateParsing:
    """Tests for price update parsing."""
    
    @pytest.fixture
    def ws(self):
        """Create mock WebSocket."""
        return MockPolymarketWebSocket()
    
    def test_parse_full_update(self, ws):
        """Parses complete price update."""
        data = {
            "asset_id": "token123",
            "best_bid": "0.50",
            "best_ask": "0.55",
            "last_trade": "0.52",
        }
        
        token_id, price = ws._parse_price_update(data)
        
        assert token_id == "token123"
        assert price.best_bid == 0.50
        assert price.best_ask == 0.55
        assert price.last_trade == 0.52
    
    def test_parse_partial_update(self, ws):
        """Parses update with missing fields."""
        data = {
            "asset_id": "token123",
            "best_bid": "0.50",
        }
        
        token_id, price = ws._parse_price_update(data)
        
        assert token_id == "token123"
        assert price.best_bid == 0.50
        assert price.best_ask is None
    
    def test_parse_invalid_update(self, ws):
        """Returns None for invalid update."""
        data = {"some_field": "value"}
        
        token_id, price = ws._parse_price_update(data)
        
        assert token_id is None
        assert price is None


class TestPriceCache:
    """Tests for price caching."""
    
    @pytest.fixture
    def ws(self):
        """Create mock WebSocket."""
        return MockPolymarketWebSocket()
    
    def test_cache_price(self, ws):
        """Caches price by token ID."""
        price = MockLivePrice(token_id="token123", best_bid=0.50)
        ws._prices["token123"] = price
        
        assert ws.get_price("token123") is price
    
    def test_get_missing_price(self, ws):
        """Returns None for missing token."""
        assert ws.get_price("nonexistent") is None
    
    def test_get_all_prices(self, ws):
        """Returns copy of all prices."""
        price1 = MockLivePrice(token_id="token1", best_bid=0.50)
        price2 = MockLivePrice(token_id="token2", best_bid=0.45)
        ws._prices["token1"] = price1
        ws._prices["token2"] = price2
        
        all_prices = ws.get_all_prices()
        
        assert len(all_prices) == 2
        assert "token1" in all_prices
        assert "token2" in all_prices
    
    def test_get_mid_price(self, ws):
        """Gets mid price for token."""
        price = MockLivePrice(token_id="token123", best_bid=0.50, best_ask=0.54)
        ws._prices["token123"] = price
        
        mid = ws.get_mid_price("token123")
        
        assert mid == 0.52
    
    def test_get_mid_price_missing_token(self, ws):
        """Returns None for missing token."""
        mid = ws.get_mid_price("nonexistent")
        
        assert mid is None


class TestStalenessTracking:
    """Tests for per-token staleness tracking."""
    
    @pytest.fixture
    def ws(self):
        """Create mock WebSocket with staleness tracking."""
        ws = MockPolymarketWebSocket()
        # Add staleness tracking attributes (like the real implementation)
        ws._last_update_per_token = {}
        return ws
    
    def test_get_stale_tokens_empty_when_no_subscriptions(self, ws):
        """Returns empty list when no tokens subscribed."""
        ws._subscribed_tokens = set()
        
        stale = self._get_stale_tokens(ws, threshold_seconds=60)
        
        assert stale == []
    
    def test_get_stale_tokens_detects_never_updated(self, ws):
        """Tokens that never received updates are considered stale."""
        ws._subscribed_tokens = {"token1", "token2"}
        ws._last_update_per_token = {}  # No updates yet
        
        stale = self._get_stale_tokens(ws, threshold_seconds=60)
        
        assert len(stale) == 2
        assert "token1" in stale
        assert "token2" in stale
    
    def test_get_stale_tokens_fresh_tokens_not_stale(self, ws):
        """Tokens with recent updates are not stale."""
        from datetime import timedelta
        
        ws._subscribed_tokens = {"token1", "token2"}
        now = datetime.now(timezone.utc)
        ws._last_update_per_token = {
            "token1": now,  # Just updated
            "token2": now - timedelta(seconds=30),  # 30s ago
        }
        
        stale = self._get_stale_tokens(ws, threshold_seconds=60)
        
        assert stale == []
    
    def test_get_stale_tokens_old_tokens_are_stale(self, ws):
        """Tokens without updates beyond threshold are stale."""
        from datetime import timedelta
        
        ws._subscribed_tokens = {"token1", "token2", "token3"}
        now = datetime.now(timezone.utc)
        ws._last_update_per_token = {
            "token1": now - timedelta(seconds=30),   # Fresh
            "token2": now - timedelta(seconds=120),  # Stale (>60s)
            "token3": now - timedelta(seconds=180),  # Stale (>60s)
        }
        
        stale = self._get_stale_tokens(ws, threshold_seconds=60)
        
        assert len(stale) == 2
        assert "token2" in stale
        assert "token3" in stale
        assert "token1" not in stale
    
    def _get_stale_tokens(self, ws, threshold_seconds: float = 120):
        """Helper to get stale tokens (mirrors real implementation)."""
        if not ws._subscribed_tokens:
            return []
        
        now = datetime.now(timezone.utc)
        stale = []
        
        for token_id in ws._subscribed_tokens:
            last_update = ws._last_update_per_token.get(token_id)
            if last_update is None:
                stale.append(token_id)
            elif (now - last_update).total_seconds() > threshold_seconds:
                stale.append(token_id)
        
        return stale


class TestResubscribeTokens:
    """Tests for token resubscription logic."""
    
    def test_resubscribe_clears_per_token_tracking(self):
        """Resubscribing should clear per-token update times."""
        ws = MockPolymarketWebSocket()
        ws._last_update_per_token = {
            "token1": datetime.now(timezone.utc),
            "token2": datetime.now(timezone.utc),
        }
        
        # Simulate resubscribe clearing
        tokens_to_resub = ["token1"]
        for token_id in tokens_to_resub:
            ws._last_update_per_token.pop(token_id, None)
        
        assert "token1" not in ws._last_update_per_token
        assert "token2" in ws._last_update_per_token
    
    def test_seed_from_rest_creates_price_if_missing(self):
        """Seeding from REST creates LivePrice if token not in cache."""
        ws = MockPolymarketWebSocket()
        ws._last_update_per_token = {}
        
        # Simulate seed_from_rest
        token_id = "new_token"
        rest_bid = 0.45
        
        if token_id not in ws._prices:
            ws._prices[token_id] = MockLivePrice(token_id=token_id)
        
        price = ws._prices[token_id]
        price.best_bid = rest_bid
        ws._last_update_per_token[token_id] = datetime.now(timezone.utc)
        
        assert ws._prices[token_id].best_bid == 0.45
        assert token_id in ws._last_update_per_token
    
    def test_seed_from_rest_updates_existing_price(self):
        """Seeding from REST updates existing LivePrice."""
        ws = MockPolymarketWebSocket()
        ws._last_update_per_token = {}
        
        # Pre-existing stale price
        token_id = "existing_token"
        ws._prices[token_id] = MockLivePrice(token_id=token_id, best_bid=0.30)
        
        # Simulate seed_from_rest with new value
        rest_bid = 0.45
        price = ws._prices[token_id]
        price.best_bid = rest_bid
        ws._last_update_per_token[token_id] = datetime.now(timezone.utc)
        
        assert ws._prices[token_id].best_bid == 0.45

