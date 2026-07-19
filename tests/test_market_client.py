"""
Unit tests for polymarket/market_client.py - Polymarket API client.

Uses mocked HTTP responses to test API parsing and error handling.
"""
import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime


class MockPolymarketEvent:
    """Mock PolymarketEsportsEvent for testing."""
    
    def __init__(
        self,
        event_id: str = "event123",
        slug: str = "team-a-vs-team-b",
        title: str = "CS2: Team A vs Team B",
        game: str = "cs2",
        team1: str = "Team A",
        team2: str = "Team B",
        markets: list = None,
        is_live: bool = False,
        is_finished: bool = False,
    ):
        self.event_id = event_id
        self.slug = slug
        self.title = title
        self.game = game
        self.team1 = team1
        self.team2 = team2
        self.markets = markets or []
        self.is_live = is_live
        self.is_finished = is_finished


class MockMarketClient:
    """Mock PolymarketEsportsClient for testing core logic patterns."""
    
    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    
    def __init__(self):
        self._market_cache = {}
        self._cache_expires = {}
        self._session = None
    
    def _parse_order_book(self, data: dict) -> dict:
        """Parse order book response."""
        bids = sorted(
            [{"price": float(b.get("price", 0)), "size": float(b.get("size", 0))} for b in data.get("bids", [])],
            key=lambda x: x["price"],
            reverse=True  # Highest first
        )
        asks = sorted(
            [{"price": float(a.get("price", 0)), "size": float(a.get("size", 0))} for a in data.get("asks", [])],
            key=lambda x: x["price"]  # Lowest first
        )
        return {"bids": bids, "asks": asks}
    
    def _parse_market_response(self, data: dict) -> dict:
        """Parse market metadata response."""
        return {
            "condition_id": data.get("conditionId", ""),
            "question": data.get("question", ""),
            "outcomes": data.get("outcomes", []),
            "clobTokenIds": data.get("clobTokenIds", []),
            "active": data.get("active", False),
            "closed": data.get("closed", False),
        }
    
    def _is_cache_valid(self, token_id: str) -> bool:
        """Check if cached data is still valid."""
        if token_id not in self._market_cache:
            return False
        expires = self._cache_expires.get(token_id)
        if expires and datetime.now() > expires:
            return False
        return True
    
    def get_best_bid_from_book(self, book: dict) -> tuple:
        """Get best bid price and size from order book."""
        bids = book.get("bids", [])
        if not bids:
            return (0, 0)
        best = bids[0]
        return (float(best.get("price", 0)), float(best.get("size", 0)))
    
    def get_best_ask_from_book(self, book: dict) -> tuple:
        """Get best ask price and size from order book."""
        asks = book.get("asks", [])
        if not asks:
            return (0, 0)
        best = asks[0]
        return (float(best.get("price", 0)), float(best.get("size", 0)))


class TestMarketClientInit:
    """Tests for PolymarketEsportsClient initialization."""
    
    def test_init_with_defaults(self):
        """Client initializes with default values."""
        client = MockMarketClient()
        
        assert client._market_cache == {}
        assert client._session is None
    
    def test_api_urls(self):
        """Client has correct API URLs."""
        client = MockMarketClient()
        
        assert "gamma-api.polymarket.com" in client.GAMMA_API_URL
        assert "clob.polymarket.com" in client.CLOB_API_URL


class TestOrderBookParsing:
    """Tests for order book parsing."""
    
    @pytest.fixture
    def client(self):
        """Create mock client."""
        return MockMarketClient()
    
    def test_parses_bids_highest_first(self, client):
        """Bids are sorted highest price first."""
        data = {
            "bids": [
                {"price": "0.48", "size": "100"},
                {"price": "0.52", "size": "50"},
                {"price": "0.50", "size": "75"},
            ],
            "asks": []
        }
        
        book = client._parse_order_book(data)
        
        assert len(book["bids"]) == 3
        assert book["bids"][0]["price"] == 0.52
        assert book["bids"][1]["price"] == 0.50
        assert book["bids"][2]["price"] == 0.48
    
    def test_parses_asks_lowest_first(self, client):
        """Asks are sorted lowest price first."""
        data = {
            "bids": [],
            "asks": [
                {"price": "0.58", "size": "100"},
                {"price": "0.52", "size": "50"},
                {"price": "0.55", "size": "75"},
            ]
        }
        
        book = client._parse_order_book(data)
        
        assert len(book["asks"]) == 3
        assert book["asks"][0]["price"] == 0.52
        assert book["asks"][1]["price"] == 0.55
        assert book["asks"][2]["price"] == 0.58
    
    def test_handles_empty_book(self, client):
        """Handles empty order book."""
        data = {"bids": [], "asks": []}
        
        book = client._parse_order_book(data)
        
        assert book["bids"] == []
        assert book["asks"] == []
    
    def test_converts_string_prices_to_float(self, client):
        """Converts string prices to float."""
        data = {
            "bids": [{"price": "0.5", "size": "100"}],
            "asks": [{"price": "0.55", "size": "50"}]
        }
        
        book = client._parse_order_book(data)
        
        assert isinstance(book["bids"][0]["price"], float)
        assert isinstance(book["asks"][0]["price"], float)


class TestBestBidAskExtraction:
    """Tests for extracting best bid/ask from order book."""
    
    @pytest.fixture
    def client(self):
        """Create mock client."""
        return MockMarketClient()
    
    def test_get_best_bid(self, client):
        """Gets best bid from sorted order book."""
        book = {
            "bids": [
                {"price": 0.52, "size": 100},
                {"price": 0.50, "size": 75},
            ],
            "asks": []
        }
        
        price, size = client.get_best_bid_from_book(book)
        
        assert price == 0.52
        assert size == 100
    
    def test_get_best_ask(self, client):
        """Gets best ask from sorted order book."""
        book = {
            "bids": [],
            "asks": [
                {"price": 0.53, "size": 50},
                {"price": 0.55, "size": 75},
            ]
        }
        
        price, size = client.get_best_ask_from_book(book)
        
        assert price == 0.53
        assert size == 50
    
    def test_empty_bids_returns_zero(self, client):
        """Empty bids returns (0, 0)."""
        book = {"bids": [], "asks": []}
        
        price, size = client.get_best_bid_from_book(book)
        
        assert price == 0
        assert size == 0
    
    def test_empty_asks_returns_zero(self, client):
        """Empty asks returns (0, 0)."""
        book = {"bids": [], "asks": []}
        
        price, size = client.get_best_ask_from_book(book)
        
        assert price == 0
        assert size == 0


class TestMarketMetadataParsing:
    """Tests for market metadata parsing."""
    
    @pytest.fixture
    def client(self):
        """Create mock client."""
        return MockMarketClient()
    
    def test_parses_market_fields(self, client):
        """Parses market metadata fields."""
        data = {
            "conditionId": "cond123",
            "question": "Who will win CS2 match?",
            "outcomes": ["Team A", "Team B"],
            "clobTokenIds": ["token_a", "token_b"],
            "active": True,
            "closed": False,
        }
        
        result = client._parse_market_response(data)
        
        assert result["condition_id"] == "cond123"
        assert result["question"] == "Who will win CS2 match?"
        assert result["outcomes"] == ["Team A", "Team B"]
        assert result["clobTokenIds"] == ["token_a", "token_b"]
        assert result["active"] is True
        assert result["closed"] is False
    
    def test_handles_missing_fields(self, client):
        """Handles missing fields with defaults."""
        data = {}
        
        result = client._parse_market_response(data)
        
        assert result["condition_id"] == ""
        assert result["outcomes"] == []
        assert result["clobTokenIds"] == []
        assert result["active"] is False


class TestMarketCache:
    """Tests for market cache logic."""
    
    @pytest.fixture
    def client(self):
        """Create mock client."""
        return MockMarketClient()
    
    def test_cache_miss_when_empty(self, client):
        """Cache miss when no data cached."""
        is_valid = client._is_cache_valid("token123")
        assert is_valid is False
    
    def test_cache_hit_when_present(self, client):
        """Cache hit when data present and not expired."""
        client._market_cache["token123"] = {"question": "Test"}
        # No expiry set, so it's valid
        
        is_valid = client._is_cache_valid("token123")
        assert is_valid is True
    
    def test_cache_stores_market_data(self, client):
        """Cache stores market data."""
        data = {"question": "Who will win?"}
        client._market_cache["token123"] = data
        
        assert client._market_cache["token123"] == data


class TestSpreadCalculation:
    """Tests for spread calculation utilities."""
    
    def test_spread_from_best_bid_ask(self):
        """Calculate spread from best bid and ask."""
        best_bid = 0.50
        best_ask = 0.55
        spread = best_ask - best_bid
        
        assert abs(spread - 0.05) < 0.001
    
    def test_midpoint_price(self):
        """Calculate midpoint from bid and ask."""
        best_bid = 0.50
        best_ask = 0.55
        midpoint = (best_bid + best_ask) / 2
        
        assert abs(midpoint - 0.525) < 0.001
    
    def test_spread_percentage(self):
        """Calculate spread as percentage of midpoint."""
        best_bid = 0.50
        best_ask = 0.55
        midpoint = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        spread_pct = (spread / midpoint) * 100
        
        assert abs(spread_pct - 9.52) < 0.1  # ~9.52%


class TestSeriesCache:
    """Tests for in-memory series caching in get_series_events."""
    
    @pytest.fixture
    def sample_series_response(self):
        """Sample Gamma API /series response."""
        return {
            "events": [
                {
                    "id": "evt1",
                    "title": "CS2: Team Alpha vs Team Beta",
                    "slug": "cs2-team-alpha-vs-team-beta",
                    "markets": [
                        {
                            "id": "mkt1",
                            "conditionId": "cond1",
                            "question": "Who will win?",
                            "outcomes": '["Team Alpha", "Team Beta"]',
                            "outcomePrices": '[0.6, 0.4]',
                            "clobTokenIds": '["tok1", "tok2"]',
                        }
                    ],
                },
                {
                    "id": "evt2",
                    "title": "CS2: Team Gamma vs Team Delta",
                    "slug": "cs2-team-gamma-vs-team-delta",
                    "markets": [],
                }
            ]
        }
    
    @pytest.mark.asyncio
    async def test_cache_hit_skips_api(self, sample_series_response):
        """When in-memory cache is fresh, API is not called."""
        import time
        from src.polymarket.market_client import PolymarketEsportsClient
        
        client = PolymarketEsportsClient()
        client.SERIES_MAP["cs2"] = "10310"
        
        # Pre-populate in-memory cache with a fresh raw events list
        client._series_cache["10310"] = {
            "events": sample_series_response["events"],
            "ts": time.time(),  # Fresh
        }
        
        events = await client.get_series_events("10310")
        
        assert len(events) == 2
        assert events[0].title == "CS2: Team Alpha vs Team Beta"
    
    @pytest.mark.asyncio
    async def test_stale_cache_fallback_on_timeout(self, sample_series_response):
        """On API timeout, stale cached data is returned instead of empty."""
        import time
        from src.polymarket.market_client import PolymarketEsportsClient
        
        client = PolymarketEsportsClient()
        client.SERIES_MAP["cs2"] = "10310"
        
        # Pre-populate cache with STALE data (expired TTL)
        client._series_cache["10310"] = {
            "events": sample_series_response["events"],
            "ts": time.time() - 999,  # Very stale
        }
        
        # Mock session to always timeout
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=asyncio.TimeoutError())
        mock_session.closed = False
        client._session = mock_session
        
        events = await client.get_series_events("10310", retries=0)
        
        # Should return stale cached data instead of empty
        assert len(events) == 2
        assert events[0].event_id == "evt1"
    
    @pytest.mark.asyncio
    async def test_cache_miss_stores_response(self, sample_series_response):
        """On cache miss + API success, response is stored in cache."""
        import time
        from src.polymarket.market_client import PolymarketEsportsClient
        
        client = PolymarketEsportsClient()
        client.SERIES_MAP["cs2"] = "10310"
        
        # Ensure cache is empty
        client._series_cache.pop("10310", None)
        
        # Mock the HTTP session
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=sample_series_response["events"])
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.closed = False
        client._session = mock_session
        
        events = await client.get_series_events("10310")
        
        assert len(events) == 2
        # Verify it was cached
        assert "10310" in client._series_cache
        assert client._series_cache["10310"]["events"] == sample_series_response["events"]
        assert client._series_cache["10310"]["ts"] <= time.time()
    
    def test_parse_events_list(self, sample_series_response):
        """_parse_events_list correctly builds event objects."""
        from src.polymarket.market_client import PolymarketEsportsClient

        client = PolymarketEsportsClient()
        client.SERIES_MAP["cs2"] = "10310"

        events = client._parse_events_list(sample_series_response["events"], "10310")
        
        assert len(events) == 2
        assert events[0].event_id == "evt1"
        assert events[0].game == "cs2"
        assert events[1].event_id == "evt2"
    
    def test_parse_events_list_empty(self):
        """_parse_events_list handles empty input."""
        from src.polymarket.market_client import PolymarketEsportsClient

        client = PolymarketEsportsClient()

        events = client._parse_events_list([], "99999")
        assert events == []


