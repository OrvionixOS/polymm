"""
Unit tests for infra/redis_cache.py - Upstash Redis market metadata cache.

Uses mocked Redis client to test cache logic without network calls.
"""
import pytest
import json
from unittest.mock import Mock, patch, MagicMock


# ============================================================================
# Test MarketMetadataCache with mocked Redis
# ============================================================================

class TestMarketMetadataCache:
    """Tests for MarketMetadataCache operations."""
    
    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """Reset module-level singletons between tests."""
        import src.infra.redis_cache as mod
        mod._redis_client = None
        mod._redis_init_attempted = False
        mod._cache_instance = None
        yield
        mod._redis_client = None
        mod._redis_init_attempted = False
        mod._cache_instance = None
    
    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client and patch it into the module."""
        mock = MagicMock()
        with patch("src.infra.redis_cache._create_redis_client", return_value=mock):
            from src.infra.redis_cache import MarketMetadataCache
            cache = MarketMetadataCache()
            yield cache, mock
    
    @pytest.fixture
    def sample_market(self):
        """Sample market metadata dict."""
        return {
            "token_id": "0xabc123",
            "condition_id": "0xcond456",
            "question": "CS2: Team A vs Team B (BO1)",
            "outcomes": ["Team A", "Team B"],
            "clobTokenIds": ["0xabc123", "0xdef789"],
        }
    
    # --- get() tests ---
    
    @pytest.mark.asyncio
    async def test_get_cache_hit(self, mock_redis, sample_market):
        """Cache hit returns stored market data."""
        cache, redis = mock_redis
        redis.get.return_value = json.dumps(sample_market)
        
        result = await cache.get("0xabc123")
        
        assert result is not None
        assert result["condition_id"] == "0xcond456"
        assert result["question"] == "CS2: Team A vs Team B (BO1)"
        redis.get.assert_called_once_with("mkt:0xabc123")
    
    @pytest.mark.asyncio
    async def test_get_cache_miss(self, mock_redis):
        """Cache miss returns None."""
        cache, redis = mock_redis
        redis.get.return_value = None
        
        result = await cache.get("0xnonexistent")
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_handles_dict_response(self, mock_redis, sample_market):
        """Some Redis SDKs auto-deserialize JSON to dict."""
        cache, redis = mock_redis
        redis.get.return_value = sample_market  # Already a dict
        
        result = await cache.get("0xabc123")
        
        assert result == sample_market
    
    @pytest.mark.asyncio
    async def test_get_graceful_on_error(self, mock_redis):
        """Redis error returns None without crashing."""
        cache, redis = mock_redis
        redis.get.side_effect = Exception("Connection refused")
        
        result = await cache.get("0xabc123")
        
        assert result is None
    
    # --- set() tests ---
    
    @pytest.mark.asyncio
    async def test_set_stores_data(self, mock_redis, sample_market):
        """Set stores JSON-serialized data in Redis."""
        cache, redis = mock_redis
        
        result = await cache.set("0xabc123", sample_market)
        
        assert result is True
        redis.set.assert_called_once_with("mkt:0xabc123", json.dumps(sample_market))
    
    @pytest.mark.asyncio
    async def test_set_graceful_on_error(self, mock_redis, sample_market):
        """Set returns False on Redis error without crashing."""
        cache, redis = mock_redis
        redis.set.side_effect = Exception("Connection refused")
        
        result = await cache.set("0xabc123", sample_market)
        
        assert result is False
    
    # --- mget() tests ---
    
    @pytest.mark.asyncio
    async def test_mget_batch_fetch(self, mock_redis, sample_market):
        """Batch fetch returns dict of token_id -> market data."""
        cache, redis = mock_redis
        
        market2 = {**sample_market, "question": "Dota 2: X vs Y"}
        redis.mget.return_value = [json.dumps(sample_market), json.dumps(market2)]
        
        result = await cache.mget(["0xtoken1", "0xtoken2"])
        
        assert len(result) == 2
        assert result["0xtoken1"]["question"] == "CS2: Team A vs Team B (BO1)"
        assert result["0xtoken2"]["question"] == "Dota 2: X vs Y"
        redis.mget.assert_called_once_with("mkt:0xtoken1", "mkt:0xtoken2")
    
    @pytest.mark.asyncio
    async def test_mget_partial_hits(self, mock_redis, sample_market):
        """Batch fetch handles mix of hits and misses."""
        cache, redis = mock_redis
        redis.mget.return_value = [json.dumps(sample_market), None]
        
        result = await cache.mget(["0xhit", "0xmiss"])
        
        assert result["0xhit"] is not None
        assert result["0xmiss"] is None
    
    @pytest.mark.asyncio
    async def test_mget_empty_list(self, mock_redis):
        """Empty token list returns empty dict without calling Redis."""
        cache, redis = mock_redis
        
        result = await cache.mget([])
        
        assert result == {}
        redis.mget.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_mget_graceful_on_error(self, mock_redis):
        """Redis error returns empty dict without crashing."""
        cache, redis = mock_redis
        redis.mget.side_effect = Exception("Connection refused")
        
        result = await cache.mget(["0xtoken1"])
        
        assert result == {}
    
    # --- mset() tests ---
    
    @pytest.mark.asyncio
    async def test_mset_batch_store(self, mock_redis, sample_market):
        """Batch store serializes and stores multiple entries."""
        cache, redis = mock_redis
        
        mapping = {"0xtoken1": sample_market, "0xtoken2": sample_market}
        result = await cache.mset(mapping)
        
        assert result is True
        redis.mset.assert_called_once()
        call_arg = redis.mset.call_args[0][0]
        assert "mkt:0xtoken1" in call_arg
        assert "mkt:0xtoken2" in call_arg
    
    @pytest.mark.asyncio
    async def test_mset_empty_mapping(self, mock_redis):
        """Empty mapping returns False without calling Redis."""
        cache, redis = mock_redis
        
        result = await cache.mset({})
        
        assert result is False
        redis.mset.assert_not_called()


class TestCacheDisabled:
    """Tests for behavior when Redis is unavailable."""
    
    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """Reset module-level singletons between tests."""
        import src.infra.redis_cache as mod
        mod._redis_client = None
        mod._redis_init_attempted = False
        mod._cache_instance = None
        yield
        mod._redis_client = None
        mod._redis_init_attempted = False
        mod._cache_instance = None
    
    @pytest.mark.asyncio
    async def test_no_env_vars_returns_none(self):
        """Without UPSTASH env vars, all operations return None/empty."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("src.infra.redis_cache._create_redis_client", return_value=None):
                from src.infra.redis_cache import MarketMetadataCache
                cache = MarketMetadataCache()
                
                assert await cache.get("0xtoken") is None
                assert await cache.mget(["0xtoken"]) == {}
                assert await cache.set("0xtoken", {"q": "test"}) is False
                assert await cache.mset({"0xtoken": {"q": "test"}}) is False
    
    @pytest.mark.asyncio
    async def test_import_error_returns_none(self):
        """If upstash-redis not installed, operations return None/empty."""
        with patch("src.infra.redis_cache._create_redis_client", return_value=None):
            from src.infra.redis_cache import MarketMetadataCache
            cache = MarketMetadataCache()
            
            assert await cache.get("0xtoken") is None


class TestGetMarketCache:
    """Tests for the singleton factory function."""
    
    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """Reset module-level singletons between tests."""
        import src.infra.redis_cache as mod
        mod._redis_client = None
        mod._redis_init_attempted = False
        mod._cache_instance = None
        yield
        mod._redis_client = None
        mod._redis_init_attempted = False
        mod._cache_instance = None
    
    def test_returns_same_instance(self):
        """get_market_cache() returns singleton instance."""
        from src.infra.redis_cache import get_market_cache
        
        cache1 = get_market_cache()
        cache2 = get_market_cache()
        
        assert cache1 is cache2
