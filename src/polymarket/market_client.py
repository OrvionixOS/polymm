"""
Polymarket Esports market discovery and matching.

Uses the Gamma API /series endpoint to find esports events (CS2, Dota 2).
"""
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field


@dataclass
class PolymarketEsportsEvent:
    """An esports event on Polymarket."""
    event_id: str
    slug: str
    title: str
    game: str  # 'cs2' or 'dota2'
    team1: str
    team2: str
    markets: List[Dict[str, Any]] = field(default_factory=list)
    is_live: bool = False
    is_finished: bool = False
    start_time: Optional[datetime] = None
    volume: float = 0.0
    
    @property
    def moneyline_market(self) -> Optional[Dict[str, Any]]:
        """Get the main winner/moneyline market."""
        for m in self.markets:
            q = m.get("question", "").lower()
            # Look for winner/moneyline market (not map/game specific)
            if ("winner" in q or "win" in q) and "map" not in q and "game " not in q:
                return m
        # Fallback to first market
        return self.markets[0] if self.markets else None


class PolymarketEsportsClient:
    """Client for discovering and fetching esports markets on Polymarket."""
    
    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    
    # Series IDs for esports (from /sports endpoint)
    DOTA2_SERIES_ID = "10309"
    CS2_SERIES_ID = "10310"
    LOL_SERIES_ID = "10311"
    VALORANT_SERIES_ID = "10369"
    MLBB_SERIES_ID = "10426"
    COD_SERIES_ID = "10427"
    HOK_SERIES_ID = "10434"
    R6_SERIES_ID = "10432"
    SC2_SERIES_ID = "10435"
    
    # Rugby series IDs (3-way sports with draw)
    RUGBY_PREMIERSHIP_SERIES_ID = "10840"
    RUGBY_TOP14_SERIES_ID = "10841"
    RUGBY_SIX_NATIONS_SERIES_ID = "10880"
    RUGBY_URC_SERIES_ID = "10881"
    RUGBY_CHAMPIONS_CUP_SERIES_ID = "10882"
    RUGBY_SUPER_RUGBY_SERIES_ID = "10883"
    RUGBY_CHAMPIONSHIP_SERIES_ID = "10884"
    
    SERIES_MAP = {
        "dota2": "10309",
        "cs2": "10310",
        "lol": "10311",
        "valorant": "10369",
        "mlbb": "10426",
        "cod": "10427",
        "hok": "10434",
        "r6": "10432",
        "sc2": "10435",
        "overwatch": "10430",  # discovered 2026-06-11 — was previously missing
        # Rugby tournaments (3-way)
        "rugby_premiership": "10840",
        "rugby_top14": "10841",
        "rugby_six_nations": "10880",
        "rugby_urc": "10881",
        "rugby_champions_cup": "10882",
        "rugby_super_rugby": "10883",
        "rugby_championship": "10884",
    }
    
    # Sports series IDs for multi-market scanning (spreads, totals)
    # Maps game name → list of series IDs
    SPORTS_SERIES_MAP = {
        "football": [
            "10188",  # EPL
            "10193",  # La Liga
            "10194",  # Bundesliga
            "10195",  # Ligue 1
            "10203",  # Serie A
            "10204",  # Champions League
            "10209",  # Europa League
            "10230",  # EFL Championship
            "10189",  # MLS
            "10286",  # Eredivisie
            "10290",  # Liga MX
            "10285",  # Argentina Primera
            "10289",  # Copa Libertadores
            "10291",  # Copa Sudamericana
            "10292",  # Süper Lig
            "10330",  # Primeira Liga
            "10359",  # Brazil Série A
            "10360",  # J-League
            "10438",  # A-League
            "10444",  # K-League
            "10437",  # UEFA Conference League
            "10307",  # FA Cup
            "10306",  # Russian Premier
            "10965",  # Chile Primera
            "10315",  # Coupe de France
            "10316",  # Copa del Rey
            "10317",  # DFB-Pokal
            "10361",  # Saudi Pro League
            "10329",  # EFL Cup
            "10675",  # Ligue 2
            "10670",  # Bundesliga 2
            "10674",  # Scottish Premiership
            "10676",  # Serie B
            "10243",  # UEFA WC Qualifiers
            "10287",  # Coppa Italia
            "10439",  # Chinese Super League
            "10362",  # Norway Eliteserien
            "10363",  # Danish Superliga
            "10786",  # Africa Cup of Nations
            "10973",  # Brazil Série B
        ],
        "hockey": [
            "10346",  # NHL
            "10699",  # AHL
            "10700",  # KHL
            "10695",  # Swedish Hockey League
            "10702",  # Czech Extraliga
            "10701",  # DEL (German)
            "102911", # Swiss National League
        ],
        "basketball": [
            "10470",  # NCAAB
            "10345",  # NBA
            "10879",  # Euroleague
            "10876",  # NBL
            "10872",  # Pro A (France)
            "10873",  # LNB (Argentina)
            "10874",  # KBL (Korea)
            "10877",  # Serie A (Italy)
            "10878",  # Liga Endesa (Spain)
        ],
        "rugby": [
            "10840",  # Premiership
            "10841",  # Top 14
            "10880",  # Six Nations
            "10881",  # URC
            "10882",  # European Champions Cup
            "10883",  # Super Rugby
            "10884",  # Rugby Championship
        ],
        "cricket": [
            "44",      # IPL
            "10449",   # Big Bash
            "10451",   # ODI
            "10445",   # T20 International
            "10661",   # Test Match
        ],
        "mma": [
            "38",      # UFC
        ],
    }
    
    # Limit concurrent API requests to avoid Gamma/CLOB rate limiting
    # With 2 hydration paths running in parallel, this is the TOTAL concurrency cap
    MAX_CONCURRENT_REQUESTS = 10
    
    # Class-level caches shared across all instances.
    # Critical because many call sites create ephemeral instances via
    # `async with PolymarketEsportsClient()` (order_adjuster, spread_bot, etc.)
    _market_cache: Dict[str, dict] = {}  # token_id -> market_info (immutable metadata)
    _dead_token_ids: set = set()  # tokens confirmed 404 (resolved/delisted)
    
    # In-memory series cache: series_id -> {"data": dict, "ts": float}
    # Avoids network calls entirely (Redis goes through same proxy as Gamma)
    _series_cache: Dict[str, dict] = {}
    _SERIES_CACHE_TTL = 120  # 2 minutes — events change slowly
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._connector: Optional[aiohttp.TCPConnector] = None
        self._request_semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_REQUESTS)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Use connection pooling with limits matching our semaphore
            # limit=10: max 10 simultaneous connections (conservative to avoid rate limits)
            # ttl_dns_cache=300: cache DNS lookups for 5 minutes
            # keepalive_timeout=30: keep connections alive for reuse
            self._connector = aiohttp.TCPConnector(
                limit=10,
                ttl_dns_cache=300,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                connector=self._connector,
                timeout=aiohttp.ClientTimeout(total=30, connect=5),  # Generous for proxy
            )
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        if self._connector and not self._connector.closed:
            await self._connector.close()
    
    async def pre_warm_cache(self, token_ids: list) -> int:
        """
        Bulk pre-fetch market metadata from Redis into in-memory cache.
        
        Uses MGET for efficiency (~0.1s for 500 keys vs ~24s sequential).
        Call this BEFORE hydration loops to avoid per-token Redis latency.
        
        Returns number of cache hits.
        """
        # Filter out tokens already in memory
        missing = [tid for tid in token_ids if tid not in self._market_cache]
        if not missing:
            return 0
        
        try:
            from src.infra.redis_cache import get_market_cache
            redis_cache = get_market_cache()
            
            hits = 0
            # MGET in batches of 200 (safe for Upstash REST API payload limits)
            for i in range(0, len(missing), 200):
                batch = missing[i:i+200]
                results = await redis_cache.mget(batch)
                for tid, data in results.items():
                    if data is not None:
                        self._market_cache[tid] = data
                        hits += 1
            
            return hits
        except Exception:
            return 0
    
    async def get_series_events(self, series_id: str, hydrate: bool = False, retries: int = 2) -> List[PolymarketEsportsEvent]:
        """
        Get events from a specific series.

        Uses Gamma's `/events?series_id=X` endpoint with proper pagination.
        We used to call `/series/{id}` and parse the embedded `events` array,
        but that endpoint silently caps at the first ~50–100 events for
        large series and orders them by something that buries the
        truly-upcoming ones at the bottom — for CS2 (100+ open events) we
        were returning *zero* upcoming matches because they all fell off
        the cap. Verified 2026-06-11.

        The Gamma `/events` endpoint also has a quirk: with `ascending=true`
        ordering, the first page returns oldest events; many series'
        upcoming events only show up under `ascending=false`. We query
        both orderings and dedupe by `id` so we get everything.

        Uses in-memory cache (120s TTL) to avoid redundant API calls.
        On API failure, returns stale cached data if available.
        """
        import time

        cached_entry = self._series_cache.get(series_id)
        if cached_entry:
            age = time.time() - cached_entry["ts"]
            if age < self._SERIES_CACHE_TTL:
                return self._parse_events_list(cached_entry["events"], series_id)

        session = await self._get_session()
        url = f"{self.GAMMA_API_URL}/events"
        common_params = {
            "series_id": series_id,
            "limit": 500,
            "closed": "false",
            "archived": "false",
            "active": "true",
            "order": "startDate",
        }

        seen_ids = set()
        all_events: List[Dict[str, Any]] = []
        any_success = False

        # Combine ascending + descending pulls; paginate within each direction
        # until we either run out of results or every event in a page is one
        # we've already seen (signals the orderings have converged).
        for ascending in ("true", "false"):
            for offset in (0, 500, 1000, 1500):
                params = {**common_params, "ascending": ascending, "offset": offset}
                batch = None
                for attempt in range(retries + 1):
                    try:
                        async with session.get(url, params=params) as resp:
                            if resp.status == 200:
                                batch = await resp.json()
                                any_success = True
                                break
                            elif resp.status == 429 and attempt < retries:
                                await asyncio.sleep(1 * (attempt + 1))
                                continue
                            break
                    except asyncio.TimeoutError:
                        if attempt < retries:
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
                    except Exception as e:
                        if attempt < retries:
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
                        print(f"Error fetching series {series_id} ({ascending}, offset={offset}): {e}")
                if not batch:
                    break
                new_in_page = 0
                for e in batch:
                    eid = e.get("id")
                    if eid is None or eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    all_events.append(e)
                    new_in_page += 1
                # Stop paginating this direction if the page was short or
                # contained no new events.
                if len(batch) < 500 or new_in_page == 0:
                    break

        # On total failure with no cache, return empty. On total failure
        # but stale cache available, fall back to the cache.
        if not any_success:
            if cached_entry:
                return self._parse_events_list(cached_entry["events"], series_id)
            return []

        # Cache the raw events list (smaller + simpler than the old wrapped form)
        self._series_cache[series_id] = {"events": all_events, "ts": time.time()}
        return self._parse_events_list(all_events, series_id)

    def _parse_events_list(self, items: List[Dict[str, Any]], series_id: str) -> List[PolymarketEsportsEvent]:
        """Parse a raw Gamma /events list into PolymarketEsportsEvent objects."""
        game = next(
            (g for g, sid in self.SERIES_MAP.items() if sid == series_id),
            "unknown"
        )
        out: List[PolymarketEsportsEvent] = []
        for item in items:
            event = self._parse_esports_event(item, game)
            if event:
                out.append(event)
        return out
    
    async def get_all_sports_events(self) -> List[PolymarketEsportsEvent]:
        """Fetch events from all sports series (football, hockey, basketball).
        
        Used by multi-market scanner for spread/totals opportunities.
        Returns PolymarketEsportsEvent objects with all their markets.
        """
        # Build reverse map: series_id → game name
        series_to_game = {}
        for game, series_ids in self.SPORTS_SERIES_MAP.items():
            for sid in series_ids:
                series_to_game[sid] = game
        
        # Temporarily add to SERIES_MAP for get_series_events game lookup
        original_map = dict(self.SERIES_MAP)
        for sid, game in series_to_game.items():
            self.SERIES_MAP[game + "_" + sid] = sid
        
        all_series_ids = []
        for series_ids in self.SPORTS_SERIES_MAP.values():
            all_series_ids.extend(series_ids)
        
        # Fetch with semaphore limiting concurrency to avoid API/proxy overload
        sem = asyncio.Semaphore(3)
        
        async def fetch_with_limit(sid):
            async with sem:
                return await self.get_series_events(sid)
        
        results = await asyncio.gather(
            *[fetch_with_limit(sid) for sid in all_series_ids],
            return_exceptions=True,
        )
        
        # Restore original map
        self.SERIES_MAP = original_map
        
        events = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                continue
            # Set correct game for each event
            sid = all_series_ids[i]
            game = series_to_game.get(sid, "unknown")
            for event in result:
                event.game = game
            events.extend(result)
        
        return events
    
    async def hydrate_event(self, event: PolymarketEsportsEvent, retries: int = 2) -> PolymarketEsportsEvent:
        """
        Fetch full event data including markets.
        
        The /series endpoint doesn't include market data, so we need to 
        fetch /events/{id} to get prices and token IDs.
        
        Includes retry logic with backoff for timeouts.
        """
        session = await self._get_session()
        url = f"{self.GAMMA_API_URL}/events/{event.event_id}"
        
        for attempt in range(retries + 1):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        hydrated = self._parse_esports_event(data, event.game)
                        if hydrated:
                            # Preserve original metadata
                            hydrated.is_live = event.is_live
                            hydrated.is_finished = event.is_finished
                            return hydrated
                    elif resp.status == 429:
                        # Rate limited - wait and retry
                        if attempt < retries:
                            await asyncio.sleep(1 * (attempt + 1))
                            continue
            except asyncio.TimeoutError:
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                # Only log on final failure
                print(f"Timeout hydrating event {event.event_id}")
            except Exception as e:
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                # Include exception type and details for debugging
                error_msg = str(e) if str(e) else type(e).__name__
                print(f"Error hydrating event {event.event_id}: {error_msg}")
        
        # Return original event if hydration fails (graceful degradation)
        return event
    
    async def get_order_book(self, token_id: str, retries: int = 2) -> dict:
        """
        Fetch order book from CLOB API for a token.
        
        Returns dict with 'bids' and 'asks' arrays, sorted best-first:
        - bids: sorted by price DESCENDING (highest bid first)
        - asks: sorted by price ASCENDING (lowest ask first)
        
        Uses semaphore to limit concurrent requests and avoid rate limiting.
        """
        session = await self._get_session()
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        
        for attempt in range(retries + 1):
            try:
                # Use semaphore to limit concurrent requests
                async with self._request_semaphore:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            
                            # Sort bids descending (best bid = highest price first)
                            bids = data.get("bids", [])
                            bids_sorted = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
                            
                            # Sort asks ascending (best ask = lowest price first)
                            asks = data.get("asks", [])
                            asks_sorted = sorted(asks, key=lambda x: float(x.get("price", 1)), reverse=False)
                            
                            return {
                                **data,
                                "bids": bids_sorted,
                                "asks": asks_sorted,
                            }
                        elif resp.status == 429:
                            # Rate limited - wait and retry
                            await asyncio.sleep(1 * (attempt + 1))
                            continue
            except asyncio.TimeoutError:
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                # Only log on final failure
                print(f"Timeout fetching order book for {token_id[:20]}...")
            except Exception as e:
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                print(f"Error fetching order book: {e}")
        
        return {"bids": [], "asks": []}
    
    async def get_best_bids(self, token_ids: list) -> dict:
        """
        Get best bid (highest) for each token ID.
        Fetches order books with controlled concurrency (semaphore limits parallel requests).
        
        Returns dict mapping token_id -> {'price': float, 'size': float}
        """
        if not token_ids:
            return {}
        
        async def fetch_one(token_id: str):
            book = await self.get_order_book(token_id)
            bids = book.get("bids", [])
            if bids:
                return token_id, {
                    "price": float(bids[0].get("price", 0)),
                    "size": float(bids[0].get("size", 0)),
                }
            return token_id, {"price": 0.0, "size": 0.0}
        
        # Semaphore inside get_order_book limits actual concurrency
        results = await asyncio.gather(*[fetch_one(tid) for tid in token_ids], return_exceptions=True)
        
        # Filter out exceptions
        output = {}
        for r in results:
            if isinstance(r, tuple) and len(r) == 2:
                output[r[0]] = r[1]
        return output
    
    async def get_order_books_batch(self, token_ids: list) -> Dict[str, dict]:
        """
        Fetch order books for multiple tokens with controlled concurrency.
        
        Returns dict mapping token_id -> order_book
        """
        if not token_ids:
            return {}
        
        async def fetch_one(token_id: str):
            book = await self.get_order_book(token_id)
            return token_id, book
        
        # Semaphore inside get_order_book limits actual concurrency
        results = await asyncio.gather(*[fetch_one(tid) for tid in token_ids], return_exceptions=True)
        
        # Filter out exceptions
        output = {}
        for r in results:
            if isinstance(r, tuple) and len(r) == 2:
                output[r[0]] = r[1]
        return output
    
    async def get_market_by_token(self, token_id: str, retries: int = 3, skip_cache: bool = False) -> Optional[dict]:
        """
        Look up market info by token ID from CLOB API.
        
        Returns market dict with outcomes, clobTokenIds, etc. or None if not found.
        
        Uses caching: market metadata doesn't change, so we cache results to avoid
        repeated API calls (follows Polymarket best practices).
        
        Args:
            token_id: The CLOB token ID
            retries: Number of retry attempts on failure
            skip_cache: If True, bypass cache and fetch fresh (rarely needed)
        """
        # Check cache first (market metadata is immutable)
        if not skip_cache and token_id in self._market_cache:
            return self._market_cache[token_id]
        
        # L2: Check Redis persistent cache (shared across restarts and machines)
        if not skip_cache:
            try:
                from src.infra.redis_cache import get_market_cache
                redis_result = await get_market_cache().get(token_id)
                if redis_result:
                    self._market_cache[token_id] = redis_result  # Promote to L1
                    return redis_result
            except Exception:
                pass  # Redis unavailable — fall through to API
        
        # Skip dead tokens silently (confirmed 404 from CLOB = resolved/delisted)
        if token_id in self._dead_token_ids:
            return None
        
        session = await self._get_session()
        last_clob_status = None
        last_gamma_status = None
        last_error = None
        result = None
        
        # Use semaphore to limit concurrent API requests (avoid rate limiting)
        async with self._request_semaphore:
            for attempt in range(retries + 1):
                # First try the CLOB market endpoint
                try:
                    url = f"https://clob.polymarket.com/markets/{token_id}"
                    async with session.get(url) as resp:
                        last_clob_status = resp.status
                        if resp.status == 200:
                            data = await resp.json()
                            
                            # CLOB API returns tokens as array of objects:
                            # [{"token_id": "abc", "outcome": "Yes"}, {"token_id": "def", "outcome": "No"}]
                            # We need to extract these into separate arrays to match Gamma format
                            raw_tokens = data.get("tokens", [])
                            if raw_tokens and isinstance(raw_tokens[0], dict):
                                # Token objects - extract IDs and outcomes
                                clob_token_ids = [t.get("token_id", "") for t in raw_tokens]
                                outcomes = [t.get("outcome", "") for t in raw_tokens]
                            else:
                                # Fallback: already string arrays (shouldn't happen)
                                clob_token_ids = raw_tokens
                                outcomes = raw_tokens
                            
                            result = {
                                "token_id": token_id,
                                "condition_id": data.get("condition_id"),
                                "question": data.get("question", ""),
                                "description": data.get("description", ""),
                                "outcomes": outcomes,
                                "clobTokenIds": clob_token_ids,
                                "tokens": raw_tokens,  # Keep raw for compatibility
                            }
                            # Enrich with Gamma metadata (event_slug, title, groupItemTitle)
                            # CLOB API doesn't return these fields needed for sport classification
                            try:
                                gamma_url = f"{self.GAMMA_API_URL}/markets?clob_token_ids={token_id}"
                                async with session.get(gamma_url) as gamma_resp:
                                    if gamma_resp.status == 200:
                                        gamma_data = await gamma_resp.json()
                                        if gamma_data and len(gamma_data) > 0:
                                            gm = gamma_data[0]
                                            # Extract event_slug from nested events array
                                            # Gamma /markets response has events as nested objects
                                            events_list = gm.get("events", [])
                                            if isinstance(events_list, str):
                                                import json as _json
                                                try:
                                                    events_list = _json.loads(events_list)
                                                except Exception:
                                                    events_list = []
                                            event_slug = ""
                                            event_title = ""
                                            if events_list and isinstance(events_list, list) and len(events_list) > 0:
                                                event_slug = events_list[0].get("slug", "")
                                                event_title = events_list[0].get("title", "")
                                            # Fall back to market slug (starts with same prefix)
                                            if not event_slug:
                                                event_slug = gm.get("slug", "")
                                            result["event_slug"] = event_slug
                                            result["title"] = event_title or gm.get("groupItemTitle", "")
                                            result["groupItemTitle"] = gm.get("groupItemTitle", "")
                                            # Time fields for trading_deadline
                                            result["gameStartTime"] = gm.get("gameStartTime")
                                            result["endDate"] = gm.get("endDate")
                            except Exception:
                                pass  # Best effort — classification falls back to defaults
                            # Cache successful result (L1 in-memory + L2 Redis)
                            self._market_cache[token_id] = result
                            try:
                                from src.infra.redis_cache import get_market_cache
                                await get_market_cache().set(token_id, result)
                            except Exception:
                                pass
                            return result
                        elif resp.status == 429:
                            if attempt < retries:
                                await asyncio.sleep(1 * (attempt + 1))
                                continue
                except asyncio.TimeoutError:
                    last_error = "CLOB timeout"
                    if attempt < retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                except Exception as e:
                    last_error = f"CLOB error: {e}"
                
                # Fallback: Search gamma API for the market
                try:
                    url = f"{self.GAMMA_API_URL}/markets?clob_token_ids={token_id}"
                    async with session.get(url) as resp:
                        last_gamma_status = resp.status
                        if resp.status == 200:
                            data = await resp.json()
                            if data and len(data) > 0:
                                market = data[0]
                                # Extract event_slug from nested events array
                                events_list = market.get("events", [])
                                if isinstance(events_list, str):
                                    import json as _json
                                    try:
                                        events_list = _json.loads(events_list)
                                    except Exception:
                                        events_list = []
                                event_slug = ""
                                event_title = ""
                                if events_list and isinstance(events_list, list) and len(events_list) > 0:
                                    event_slug = events_list[0].get("slug", "")
                                    event_title = events_list[0].get("title", "")
                                # Fall back to market slug (starts with same prefix)
                                if not event_slug:
                                    event_slug = market.get("slug", "")
                                result = {
                                    "market_id": market.get("id"),
                                    "condition_id": market.get("conditionId"),
                                    "question": market.get("question", ""),
                                    "title": event_title or market.get("groupItemTitle", ""),
                                    "slug": market.get("slug", ""),
                                    "outcomes": market.get("outcomes", []),
                                    "outcome_prices": market.get("outcomePrices", []),
                                    "clobTokenIds": market.get("clobTokenIds", []),
                                    "event_slug": event_slug,
                                    "groupItemTitle": market.get("groupItemTitle", ""),
                                    # Time fields for trading_deadline (used by hydration)
                                    "gameStartTime": market.get("gameStartTime"),
                                    "endDate": market.get("endDate"),
                                }
                                # Cache successful result (L1 in-memory + L2 Redis)
                                self._market_cache[token_id] = result
                                try:
                                    from src.infra.redis_cache import get_market_cache
                                    await get_market_cache().set(token_id, result)
                                except Exception:
                                    pass
                                return result
                            else:
                                last_gamma_status = "200 (empty)"
                        elif resp.status == 429:
                            # Rate limited - retry with exponential backoff
                            if attempt < retries:
                                await asyncio.sleep(2.0 * (attempt + 1))  # Longer backoff for Gamma
                                continue
                except asyncio.TimeoutError:
                    last_error = "Gamma timeout"
                    if attempt < retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                except Exception as e:
                    last_error = f"Gamma error: {e}"
                    if attempt < retries:
                        continue
        
        # Log why we're returning None
        print(f"   ⚠️ get_market_by_token failed for {token_id[:16]}... | CLOB: {last_clob_status} | Gamma: {last_gamma_status} | Error: {last_error}")
        # Cache as dead if CLOB confirmed 404 (market resolved/delisted, won't come back)
        if last_clob_status == 404:
            self._dead_token_ids.add(token_id)
        return None
    
    async def get_market_status(self, token_id: str, retries: int = 2) -> dict:
        """
        Get market status including resolution state.
        
        Returns dict with:
        - umaResolutionStatus: null (live), "proposed" (ended), "resolved" (settled)
        - active: bool
        - closed: bool
        - outcomePrices: list of prices (reflects winner if match ended)
        
        This is useful for detecting if a match has ended and we should stop
        trying to hedge.
        """
        session = await self._get_session()
        
        async with self._request_semaphore:
            for attempt in range(retries + 1):
                try:
                    url = f"{self.GAMMA_API_URL}/markets?clob_token_ids={token_id}"
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data and len(data) > 0:
                                market = data[0]
                                return {
                                    "umaResolutionStatus": market.get("umaResolutionStatus"),
                                    "active": market.get("active", True),
                                    "closed": market.get("closed", False),
                                    "outcomePrices": market.get("outcomePrices", "[]"),
                                    "acceptingOrders": market.get("acceptingOrders", True),
                                }
                        elif resp.status == 429:
                            if attempt < retries:
                                await asyncio.sleep(1 * (attempt + 1))
                                continue
                except asyncio.TimeoutError:
                    if attempt < retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                except Exception:
                    if attempt < retries:
                        continue
        
        # Return default (assume active) if we can't fetch
        return {"umaResolutionStatus": None, "active": True, "closed": False}
    
    def get_cache_stats(self) -> dict:
        """Get cache statistics for debugging/monitoring."""
        return {
            "market_cache_size": len(self._market_cache),
            "cached_tokens": list(self._market_cache.keys())[:10],  # First 10 for brevity
        }
    
    def clear_market_cache(self, token_id: Optional[str] = None):
        """
        Clear market cache.
        
        Args:
            token_id: If provided, only clear this token. Otherwise clear all.
        """
        if token_id:
            self._market_cache.pop(token_id, None)
        else:
            self._market_cache.clear()
    
    async def get_event(self, event_id: str) -> Optional[PolymarketEsportsEvent]:
        """
        Get full event details including markets.
        """
        session = await self._get_session()
        url = f"{self.GAMMA_API_URL}/events/{event_id}"
        
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # We need to know the game to parse it correctly, but the event data 
                    # might not explicitly say "cs2" vs "dota2" in a simple field 
                    # except via tags or we can guess from title or existing object.
                    # For now, let's use "unknown" or try to infer.
                    # Actually, better to pass the game/existing event if possible.
                    # But for now let's parse with "unknown" -> _parse_esports_event handles it fine mostly.
                    return self._parse_esports_event(data, "unknown")
        except Exception as e:
            print(f"Error fetching event {event_id}: {e}")
        return None
    
    async def get_dota2_events(self) -> List[PolymarketEsportsEvent]:
        """Get all Dota 2 events."""
        return await self.get_series_events(self.DOTA2_SERIES_ID)
    
    async def get_cs2_events(self) -> List[PolymarketEsportsEvent]:
        """Get all CS2 events."""
        return await self.get_series_events(self.CS2_SERIES_ID)
    
    async def get_valorant_events(self) -> List[PolymarketEsportsEvent]:
        """Get all Valorant events."""
        return await self.get_series_events(self.VALORANT_SERIES_ID)
    
    async def get_mlbb_events(self) -> List[PolymarketEsportsEvent]:
        """Get all Mobile Legends: Bang Bang events."""
        return await self.get_series_events(self.MLBB_SERIES_ID)
    
    async def get_cod_events(self) -> List[PolymarketEsportsEvent]:
        """Get all Call of Duty events."""
        return await self.get_series_events(self.COD_SERIES_ID)
    
    async def get_hok_events(self) -> List[PolymarketEsportsEvent]:
        """Get all Honor of Kings events."""
        return await self.get_series_events(self.HOK_SERIES_ID)
    
    async def get_r6_events(self) -> List[PolymarketEsportsEvent]:
        """Get all Rainbow Six Siege events."""
        return await self.get_series_events(self.R6_SERIES_ID)
    
    async def get_sc2_events(self) -> List[PolymarketEsportsEvent]:
        """Get all StarCraft 2 events."""
        return await self.get_series_events(self.SC2_SERIES_ID)
    
    # Rugby event methods (3-way sports with draw)
    async def get_rugby_premiership_events(self) -> List[PolymarketEsportsEvent]:
        """Get Premiership Rugby events."""
        return await self.get_series_events(self.RUGBY_PREMIERSHIP_SERIES_ID)
    
    async def get_rugby_top14_events(self) -> List[PolymarketEsportsEvent]:
        """Get Rugby Top 14 events."""
        return await self.get_series_events(self.RUGBY_TOP14_SERIES_ID)
    
    async def get_rugby_urc_events(self) -> List[PolymarketEsportsEvent]:
        """Get United Rugby Championship events."""
        return await self.get_series_events(self.RUGBY_URC_SERIES_ID)
    
    async def get_all_rugby_events(self) -> List[PolymarketEsportsEvent]:
        """Get all rugby events across available tournaments."""
        results = await asyncio.gather(*[
            self.get_series_events(self.RUGBY_PREMIERSHIP_SERIES_ID),
            self.get_series_events(self.RUGBY_TOP14_SERIES_ID),
            self.get_series_events(self.RUGBY_URC_SERIES_ID),
        ], return_exceptions=True)
        
        all_events = []
        for result in results:
            if isinstance(result, list):
                all_events.extend(result)
        return all_events
    
    async def get_all_esports_events(
        self,
        live_only: bool = False,
    ) -> List[PolymarketEsportsEvent]:
        """
        Get all esports events across all games.
        
        Args:
            live_only: Only return live events
            
        Returns:
            List of PolymarketEsportsEvent objects
        """
        # Fetch from all esports series IN PARALLEL for speed
        # This reduces worst-case latency from 6 * timeout to 1 * timeout
        results = await asyncio.gather(*[
            self.get_series_events(series_id) 
            for series_id in self.SERIES_MAP.values()
        ], return_exceptions=True)
        
        all_events = []
        for result in results:
            if isinstance(result, list):
                all_events.extend(result)
            # Silently skip exceptions (already logged in get_series_events)
        
        if live_only:
            all_events = [e for e in all_events if e.is_live]
        
        return all_events
    
    async def get_live_esports_events(self) -> List[PolymarketEsportsEvent]:
        """Get only LIVE esports events."""
        return await self.get_all_esports_events(live_only=True)
    
    async def get_live_token_ids(self) -> set[str]:
        """
        Get all token IDs for currently LIVE Polymarket events.
        
        This is the centralized method for live token detection.
        It iterates all esports events, hydrates live ones if needed,
        and collects all token IDs from their markets.
        
        Returns:
            Set of token IDs that belong to LIVE events
        """
        live_tokens = set()
        try:
            all_events = await self.get_all_esports_events()
            for event in all_events:
                if event.is_live:
                    # Hydrate to get market tokens if needed
                    if not event.markets:
                        event = await self.hydrate_event(event)
                    for market in (event.markets or []):
                        for token_id in market.get("clobTokenIds", []):
                            live_tokens.add(token_id)
        except Exception:
            pass  # Return empty set on failure (graceful degradation)
        return live_tokens

    
    async def get_upcoming_esports_events(
        self,
        game: Optional[str] = None,
        limit: int = 20,
    ) -> List[PolymarketEsportsEvent]:
        """
        Get upcoming esports events (not finished, not live).
        
        Args:
            game: Filter by game ('cs2', 'dota2', 'valorant') or None for all
            limit: Maximum number of events
            
        Returns:
            List of upcoming events sorted by start time
        """
        if game and game in self.SERIES_MAP:
            events = await self.get_series_events(self.SERIES_MAP[game])
        else:
            events = await self.get_all_esports_events()
        
        # Filter to upcoming only
        upcoming = [e for e in events if not e.is_finished and not e.is_live]
        
        # Sort by start time
        upcoming.sort(key=lambda e: e.start_time or datetime.max)
        
        return upcoming[:limit]
    
    def _parse_esports_event(
        self, 
        data: dict, 
        game: str
    ) -> Optional[PolymarketEsportsEvent]:
        """Parse an esports event from API response."""
        if not data:
            return None
        
        title = data.get("title", "")
        slug = data.get("slug", "")
        
        # Extract team names from title (format: "Team1 vs Team2" or similar)
        team1, team2 = "", ""
        title_clean = title
        
        # Remove common prefixes (market type prefixes)
        for prefix in ["Winner: ", "Match Winner: ", "Moneyline: "]:
            if title_clean.startswith(prefix):
                title_clean = title_clean[len(prefix):]
        
        # Remove game/league/event prefixes generically.
        # Sports titles come as "League/Event: Team1 vs Team2" with varied prefixes:
        #   "UFC Fight Night: Trocoli vs Kondratavicius (Middleweight, Prelims)"
        #   "KHL: Lokomotiv vs Severstal", "Six Nations: Ireland vs Scotland"
        #   "European Rugby Champions Cup: Saints vs Castres"
        # Strategy: if there's a "vs" in the title and a colon BEFORE the "vs",
        # strip everything up to and including the LAST colon before "vs".
        import re
        vs_pos = title_clean.lower().find(" vs")
        if vs_pos > 0:
            colon_pos = title_clean.rfind(":", 0, vs_pos)
            if colon_pos > 0:
                title_clean = title_clean[colon_pos + 1:].strip()
        else:
            # Fallback: use the old hardcoded pattern for non-vs titles
            game_prefix_pattern = r'^(Valorant|LoL|CS2|Counter-Strike|Dota 2|Call of Duty|Mobile Legends|MLBB|Honor of Kings|HoK|Rainbow Six|R6|StarCraft|SC2|Premiership Rugby|Top 14|United Rugby Championship|URC|Rugby):\s*'
            title_clean = re.sub(game_prefix_pattern, '', title_clean, flags=re.IGNORECASE)
        
        # Remove format suffixes like "(BO1)", "(BO3)", "(BO5)", "- Game 1 Winner", etc.
        format_suffix_pattern = r'\s*\(BO\d+\)\s*$|\s*-\s*(Game|Map)\s+\d+\s*Winner\s*$'
        title_clean = re.sub(format_suffix_pattern, '', title_clean, flags=re.IGNORECASE)
        
        # Remove parenthetical suffixes from the FULL title before splitting.
        # MMA: "(Middleweight, Prelims)", "(Women's Strawweight, Main Card)"
        # These appear after team2 and must be stripped before extraction.
        title_clean = re.sub(r'\s*\([^)]*(?:weight|card|prelim|main)[^)]*\)\s*$', '', title_clean, flags=re.IGNORECASE)
        
        # Normalize "vs." → "vs" for consistent parsing (sports titles use "vs.", esports use "vs")
        title_clean = title_clean.replace(" vs. ", " vs ")
        
        if " vs " in title_clean.lower():
            parts = title_clean.lower().split(" vs ")
            team1 = parts[0].strip().title()
            if len(parts) > 1:
                # Take first word/team name before any extra info
                team2_full = parts[1].strip()
                team2 = team2_full.split(" - ")[0].strip().title()
        elif " - " in title_clean and " vs " not in title_clean.lower():
            parts = title_clean.split(" - ")
            if len(parts) >= 2:
                team1 = parts[0].strip()
                team2 = parts[1].strip()
        
        # Parse markets
        import json
        markets = []
        for m in data.get("markets", []):
            # Helper to parse potential JSON strings
            def parse_list(val):
                if isinstance(val, str):
                    try: return json.loads(val)
                    except: return []
                return val or []

            markets.append({
                "market_id": m.get("id", ""),
                "condition_id": m.get("conditionId", ""),
                "question": m.get("question", ""),
                "outcomes": parse_list(m.get("outcomes")),
                "outcomePrices": parse_list(m.get("outcomePrices")),
                "clobTokenIds": parse_list(m.get("clobTokenIds")),
                "liveBids": m.get("liveBids", []),
                "liveAsks": m.get("liveAsks", []),
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("liquidity", 0) or 0),
            })
        
        # Parse start time (check multiple field names - API is inconsistent)
        start_time = None
        start_str = (
            data.get("startTime")
            or data.get("gameStartTime")
            or data.get("eventStartTime")
            or data.get("startDate")
        )
        # Also check inside markets — market-level gameStartTime can exist
        # when event-level fields are missing
        if not start_str and data.get("markets"):
            for m in data["markets"]:
                start_str = m.get("gameStartTime") or m.get("eventStartTime")
                if start_str:
                    break
        if start_str:
            try:
                start_time = datetime.fromisoformat(
                    str(start_str).replace("Z", "+00:00")
                )
            except:
                pass
        
        # Determine live/finished status
        is_live = data.get("live", False)
        is_finished = data.get("ended", False) or data.get("closed", False)
        
        # CRITICAL FALLBACK: Polymarket API often has stale `live` status.
        # If the match has started (start_time in past) but API doesn't report live/finished,
        # infer live status from start_time to prevent placing orders on live matches.
        if start_time and not is_live and not is_finished:
            now = datetime.now(timezone.utc)
            
            if game.startswith("rugby"):
                # Rugby matches last ~100-120 minutes (80 min play + halftime/stoppages)
                match_duration = timedelta(hours=2, minutes=30)
            else:
                # Esports matches: LoL/Dota ~30-60 min, CS2/Valorant can go 1-2+ hours
                match_duration = timedelta(hours=2)
            
            if now > start_time:
                if now < start_time + match_duration:
                    is_live = True  # Match has started but not finished (API stale)
                else:
                    is_finished = True  # Match likely finished
        
        return PolymarketEsportsEvent(
            event_id=str(data.get("id", "")),
            slug=slug,
            title=title,
            game=game,
            team1=team1,
            team2=team2,
            markets=markets,
            is_live=is_live,
            is_finished=is_finished,
            start_time=start_time,
            volume=float(data.get("volume", 0) or 0),
        )
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


async def find_esports_markets():
    """Find all active esports markets on Polymarket."""
    async with PolymarketEsportsClient() as client:
        print("🎮 Fetching Polymarket Esports Markets")
        print("=" * 60)
        
        for game_name, series_id in [("Dota 2", "10309"), ("CS2", "10310"), ("LoL", "10311"), ("Valorant", "10369"), ("MLBB", "10426"), ("Call of Duty", "10427")]:
            print(f"\n📌 {game_name} (series: {series_id})")
            print("-" * 40)
            
            events = await client.get_series_events(series_id)
            
            if not events:
                print("   ❌ No events found")
                continue
            
            live = [e for e in events if e.is_live]
            upcoming = [e for e in events if not e.is_finished and not e.is_live]
            
            print(f"   ✅ {len(events)} total ({len(live)} live, {len(upcoming)} upcoming)")
            
            for e in events[:5]:
                status = "🔴 LIVE" if e.is_live else ("✅ DONE" if e.is_finished else "⏰")
                print(f"\n   {status} {e.title[:50]}")
                print(f"      Teams: {e.team1} vs {e.team2}")
                print(f"      Slug: {e.slug}")
                if e.moneyline_market:
                    mm = e.moneyline_market
                    print(f"      Market: {mm.get('market_id', 'N/A')[:20]}...")
                    prices = mm.get("outcomePrices", [])
                    if prices:
                        print(f"      Prices: {prices}")
        
        print("\n" + "=" * 60)


if __name__ == "__main__":
    asyncio.run(find_esports_markets())
