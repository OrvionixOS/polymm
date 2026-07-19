"""
Redis-backed persistent cache for immutable market metadata.

Uses Upstash Redis (REST API) so it works through proxychains and across
multiple machines (local + VPS share the same cache).

Cache hierarchy:
  1. In-memory _market_cache (per-process, instant)
  2. Redis (persistent, ~10ms per lookup, shared across machines)
  3. CLOB / Gamma API (slow, ~300ms per call)

Market metadata (question, outcomes, clobTokenIds, condition_id) is immutable,
so entries are stored without TTL and never invalidated.
"""
import json
import os
import asyncio
from typing import Dict, Optional

# Lazy-loaded to avoid import errors if upstash-redis not installed
_redis_client = None
_redis_init_attempted = False
_redis_lock = asyncio.Lock()
_env_prefix = ""  # Configurable per-bot: "" for spread, "SPORT_" for sports


def configure_redis_env_prefix(prefix: str):
    """Set the env var prefix for Redis connection (e.g. 'SPORT_').
    
    Must be called BEFORE any Redis operations. Each bot sets its own prefix:
      - Sports bot: configure_redis_env_prefix("SPORT_")  → reads SPORT_UPSTASH_REDIS_REST_URL
      - Spread bot: (default, no call needed)              → reads UPSTASH_REDIS_REST_URL
    """
    global _env_prefix
    _env_prefix = prefix


def _create_redis_client():
    """Create Upstash Redis client from environment variables."""
    try:
        from upstash_redis import Redis
    except ImportError:
        print("   ⚠️ upstash-redis not installed, Redis cache disabled")
        return None
    
    url = os.getenv(f"{_env_prefix}UPSTASH_REDIS_REST_URL")
    token = os.getenv(f"{_env_prefix}UPSTASH_REDIS_REST_TOKEN")
    
    if not url or not token:
        print(f"   ⚠️ {_env_prefix}UPSTASH_REDIS_REST_URL/TOKEN not set, Redis cache disabled")
        return None
    
    try:
        client = Redis(url=url, token=token)
        return client
    except Exception as e:
        print(f"   ⚠️ Failed to create Redis client: {e}")
        return None


def _get_redis():
    """Get or create the Redis client singleton (synchronous)."""
    global _redis_client, _redis_init_attempted
    
    if _redis_init_attempted:
        return _redis_client
    
    _redis_init_attempted = True
    _redis_client = _create_redis_client()
    return _redis_client


# Key prefix to namespace our cache entries
_KEY_PREFIX = "mkt:"


class MarketMetadataCache:
    """
    Persistent cache for Polymarket market metadata.
    
    Uses Upstash Redis REST API. All methods are synchronous (Upstash REST
    SDK is synchronous) but wrapped for async callers. Operations are
    fire-and-forget for writes (don't block the caller on cache population).
    
    Graceful degradation: if Redis is unavailable, all methods return None
    without raising exceptions.
    """
    
    def __init__(self):
        self._redis = None
        self._available = None  # None = not checked yet
    
    def _ensure_client(self) -> bool:
        """Ensure Redis client is available. Returns True if ready."""
        if self._available is not None:
            return self._available
        
        self._redis = _get_redis()
        self._available = self._redis is not None
        
        if self._available:
            print("   ✅ Redis market cache connected")
        
        return self._available
    
    async def get(self, token_id: str) -> Optional[dict]:
        """
        Get cached market metadata for a token.
        
        Returns the market dict or None on cache miss/error.
        """
        if not self._ensure_client():
            return None
        
        try:
            key = f"{_KEY_PREFIX}{token_id}"
            data = self._redis.get(key)
            if data is None:
                return None
            # upstash-redis auto-deserializes JSON strings
            if isinstance(data, str):
                return json.loads(data)
            if isinstance(data, dict):
                return data
            return None
        except Exception:
            return None
    
    async def mget(self, token_ids: list) -> Dict[str, Optional[dict]]:
        """
        Batch-get cached market metadata for multiple tokens.
        
        Returns dict mapping token_id -> market_dict (or None for misses).
        Uses Redis MGET for efficiency (~1 round-trip for N keys).
        """
        if not token_ids or not self._ensure_client():
            return {}
        
        try:
            keys = [f"{_KEY_PREFIX}{tid}" for tid in token_ids]
            # MGET returns list of values in same order as keys
            values = self._redis.mget(*keys)
            
            result = {}
            for tid, val in zip(token_ids, values):
                if val is None:
                    result[tid] = None
                elif isinstance(val, str):
                    try:
                        result[tid] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        result[tid] = None
                elif isinstance(val, dict):
                    result[tid] = val
                else:
                    result[tid] = None
            return result
        except Exception:
            return {}
    
    async def set(self, token_id: str, data: dict) -> bool:
        """
        Cache market metadata for a token. No TTL (metadata is immutable).
        
        Returns True on success, False on error.
        """
        if not self._ensure_client():
            return False
        
        try:
            key = f"{_KEY_PREFIX}{token_id}"
            self._redis.set(key, json.dumps(data))
            return True
        except Exception:
            return False
    
    async def mset(self, mapping: Dict[str, dict]) -> bool:
        """
        Batch-cache market metadata for multiple tokens.
        
        Args:
            mapping: dict of token_id -> market_dict
            
        Returns True on success, False on error.
        """
        if not mapping or not self._ensure_client():
            return False
        
        try:
            # Build {prefixed_key: json_string} mapping
            redis_mapping = {
                f"{_KEY_PREFIX}{tid}": json.dumps(data)
                for tid, data in mapping.items()
            }
            self._redis.mset(redis_mapping)
            return True
        except Exception:
            return False
    
    async def count(self) -> int:
        """Get approximate number of cached market entries."""
        if not self._ensure_client():
            return 0
        
        try:
            # Use DBSIZE as rough count (includes all keys, not just ours)
            return self._redis.dbsize()
        except Exception:
            return 0


# Singleton instance
_cache_instance: Optional[MarketMetadataCache] = None


def get_market_cache() -> MarketMetadataCache:
    """Get the singleton MarketMetadataCache instance."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = MarketMetadataCache()
    return _cache_instance


# ============================================================
# Condition ID → Game Type Cache
# ============================================================

_GAME_KEY_PREFIX = "game:"


def set_condition_game(condition_id: str, game: str) -> bool:
    """
    Store condition_id → game mapping in Redis.
    
    Called during order placement when we KNOW the correct game type.
    Survives bot restarts so hydration can look it up.
    """
    redis = _get_redis()
    if not redis or not condition_id or not game:
        return False
    try:
        redis.set(f"{_GAME_KEY_PREFIX}{condition_id}", game)
        return True
    except Exception:
        return False


def get_condition_game(condition_id: str) -> Optional[str]:
    """
    Look up game type for a condition_id from Redis.
    
    Returns game string (e.g., "football") or None on miss/error.
    """
    redis = _get_redis()
    if not redis or not condition_id:
        return None
    try:
        result = redis.get(f"{_GAME_KEY_PREFIX}{condition_id}")
        return result if isinstance(result, str) else None
    except Exception:
        return None


def mget_condition_games(condition_ids: list) -> Dict[str, Optional[str]]:
    """
    Batch lookup game types for multiple condition_ids.
    
    Returns dict mapping condition_id → game (or None for misses).
    """
    redis = _get_redis()
    if not redis or not condition_ids:
        return {}
    try:
        keys = [f"{_GAME_KEY_PREFIX}{cid}" for cid in condition_ids]
        values = redis.mget(*keys)
        return {
            cid: (val if isinstance(val, str) else None)
            for cid, val in zip(condition_ids, values)
        }
    except Exception:
        return {}


# ============================================================
# Series Data Cache (Gamma API /series responses)
# ============================================================
# Caches raw Gamma API series responses with TTL to avoid
# re-fetching slowly-changing event metadata every scan cycle.

_SERIES_KEY_PREFIX = "series:"
_SERIES_TTL = 120  # 2 minutes — events change slowly


def set_series_data(series_id: str, data: dict) -> bool:
    """
    Cache Gamma API series response with TTL.
    
    Args:
        series_id: The series ID (e.g., "10345" for NBA)
        data: Raw Gamma API JSON response
        
    Returns True on success, False on error.
    """
    redis = _get_redis()
    if not redis or not series_id:
        return False
    try:
        redis.set(
            f"{_SERIES_KEY_PREFIX}{series_id}",
            json.dumps(data),
            ex=_SERIES_TTL,
        )
        return True
    except Exception:
        return False


def get_series_data(series_id: str) -> Optional[dict]:
    """
    Get cached Gamma API series response.
    
    Returns the parsed JSON dict or None on miss/expiry/error.
    """
    redis = _get_redis()
    if not redis or not series_id:
        return None
    try:
        result = redis.get(f"{_SERIES_KEY_PREFIX}{series_id}")
        if result is None:
            return None
        if isinstance(result, str):
            return json.loads(result)
        if isinstance(result, dict):
            return result
        return None
    except Exception:
        return None

