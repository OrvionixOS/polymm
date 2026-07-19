"""
Weather Market Client - Fetches weather temperature markets from Polymarket.

Uses Gamma API to discover weather events and extract market data with spreads.
"""
import asyncio
import aiohttp
import json
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any


@dataclass
class WeatherMarket:
    """A single weather market (temperature bin)."""
    market_id: str
    condition_id: str
    question: str
    group_item: str  # e.g., "50-51°F"
    token_id: str  # Yes token
    no_token_id: str  # No token
    best_bid: float
    best_ask: float
    spread: float
    mid_price: Optional[float]
    volume: float
    liquidity: float
    active: bool
    accepting_orders: bool


@dataclass
class WeatherEvent:
    """A weather/stock/sports event containing multiple bin markets."""
    event_id: str
    slug: str
    title: str
    city: str
    date_str: str  # e.g., "january-29"
    markets: List[WeatherMarket] = field(default_factory=list)
    volume: float = 0.0
    liquidity: float = 0.0
    active: bool = True
    end_date: Optional[datetime] = None
    start_date: Optional[datetime] = None
    game_start_time: Optional[datetime] = None  # Actual game start (from API "startTime")
    market_type: str = "weather"  # "weather", "stock", or "sports"


class WeatherMarketClient:
    """Client for fetching weather temperature markets from Polymarket."""
    
    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    WEATHER_TAG_ID = "84"
    MENTIONS_TAG_ID = "100343"
    
    # Supported cities with their slug formats
    CITIES = {
        "seattle": ["seattle"],
        "dallas": ["dallas"],
        "london": ["london"],
        "nyc": ["nyc", "new-york"],
        "seoul": ["seoul"],
        "buenos-aires": ["buenos-aires"],
        "miami": ["miami"],
        "toronto": ["toronto"],
        "ankara": ["ankara"],
        "chicago": ["chicago"],
        "wellington": ["wellington"],
        "atlanta": ["atlanta"],
    }
    
    # Stock market symbols with their slug formats
    # Slug pattern: {ticker}-up-or-down-on-{month}-{day}-{year}
    STOCKS = {
        "TSLA": "tsla",
        "GOOGL": "googl",
        "AMZN": "amzn",
        "COIN": "coin",
        "NYA": "nya",
        "AAPL": "aapl",
        "RUT": "rut",       # Russell 2000
        "PLTR": "pltr",     # Palantir
        "NFLX": "nflx",     # Netflix
        "NVDA": "nvda",     # NVIDIA
        "GC": "gc",         # Gold futures
        "DAX": "dax",       # German DAX
        "OPEN": "open",     # Opendoor
        "HOOD": "hood",     # Robinhood
        "CL": "cl",         # Crude oil futures
        "SI": "si",         # Silver futures
        "UKX": "ukx",       # FTSE 100
        "DJI": "dji",       # Dow Jones
        "NDX": "ndx",       # Nasdaq 100
        "META": "meta",     # Meta
        "MSFT": "msft",     # Microsoft
    }
    
    # Basketball series IDs
    # NCAAB_SERIES_ID = "10470"        # NCAA Men's Basketball
    CWBB_SERIES_ID = "10471"         # NCAA Women's Basketball
    # Pro A, LNB, KBL, Serie A, Liga Endesa moved to market_client.py SPORTS_SERIES_MAP
    BB_CBA_SERIES_ID = "10875"       # CBA (China)
    
    # Rugby & Hockey series moved to market_client.py SPORTS_SERIES_MAP
    
    # Tennis series IDs
    TENNIS_ATP_SERIES_ID = "10365"
    TENNIS_WTA_SERIES_ID = "10366"
    
    # Cricket series IDs
    CRICKET_SERIES_IDS = [
        "44",      # IPL
        "10451",   # ODI International
        "10445",   # T20 International
        "10449",   # Big Bash
        "10446",   # Cricket South Africa
        "10528",   # International
        "10752",   # Australia
        "10750",   # England
        "10748",   # India
        "10751",   # Pakistan
        "10753",   # South Africa
        "10755",   # New Zealand
        "10754",   # UAE
        "10799",   # Bangladesh
        "10661",   # Test Matches
        "10453",   # Sheffield Shield
        "10450",   # SA T20
        "10448",   # Lanka Premier League
        "10447",   # Pakistan Tri-Series
        "10907",   # U19 World Cup
        "10908",   # WPL
        "10909",   # WNCL
        "10912",   # Women's T20 WC Qualifier
        "10910",   # Afghanistan Tri-Series
        "10911",   # Bhutan Tri-Series
    ]
    
    # UFC / MMA series IDs
    UFC_SERIES_ID = "38"       # UFC (slug: ufc, 221 events)
    ZUFFA_SERIES_ID = "10954"  # Zuffa Boxing (slug: zuffa, 24 events)
    
    # Football (Soccer) series IDs — only leagues NOT in market_client.py SPORTS_SERIES_MAP
    # All major leagues (EPL, La Liga, Bundesliga, etc.) are covered by SportsBot
    FOOTBALL_SERIES_IDS = [
        # "10188",   # EPL
        # "10193",   # La Liga
        # "10194",   # Bundesliga
        # "10195",   # Ligue 1
        # "10203",   # Serie A
        # "10204",   # Champions League
        # "10209",   # Europa League
        # "10230",   # EFL Championship
        # "10189",   # MLS
        # "10286",   # Eredivisie
        # "10290",   # Liga MX
        # "10285",   # Argentina Primera
        # "10289",   # Copa Libertadores
        # "10291",   # Copa Sudamericana
        # "10292",   # Süper Lig
        # "10330",   # Primeira Liga
        # "10359",   # Brazil Série A
        # "10360",   # J-League
        # "10361",   # Saudi Pro League
        # "10362",   # Norwegian Eliteserien
        # "10363",   # Danish Superliga
        "10364",   # Indian Super League
        # "10438",   # A-League
        # "10439",   # Chinese Super League
        "10443",   # J2 League
        # "10444",   # K-League
        # "10437",   # UEFA Conference League
        # "10315",   # Coupe de France
        # "10316",   # Copa del Rey
        # "10317",   # DFB-Pokal
        # "10307",   # FA Cup
        # "10306",   # Russian Premier
        "10863",   # Spanish Super Cup
        "10288",   # Leagues Cup
        "10246",   # CONMEBOL
        "10244",   # CONCACAF
        # "10243",   # UEFA WC Qualifiers
        "10240",   # CAF
        "10241",   # AFC
        "10294",   # OFC
        "10238",   # FIFA
        # "10786",   # Africa Cup of Nations
        "10968",   # Morocco
        "10969",   # Egypt
        "10970",   # Czechia
        "10966",   # Bolivia
        "10971",   # Romania
        # "10973",   # Brazil Série B
        "10967",   # Peru Liga 1
        "10964",   # Colombia
        # "10965",   # Chile
    ]
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_semaphore = asyncio.Semaphore(10)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
            )
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    def _generate_slugs(self, city: str, date: datetime) -> List[str]:
        """Generate possible event slugs for a city/date combo."""
        city_variants = self.CITIES.get(city.lower(), [city.lower()])
        
        # Format variations: "january-29", "jan-29"
        month_full = date.strftime("%B").lower()
        month_short = date.strftime("%b").lower()
        day = date.day
        year = date.year
        
        slugs = []
        for city_name in city_variants:
            # 2026+ markets include year suffix in the slug
            slugs.append(f"highest-temperature-in-{city_name}-on-{month_full}-{day}-{year}")
            slugs.append(f"highest-temperature-in-{city_name}-on-{month_short}-{day}-{year}")
            # Also try without year for backwards compatibility
            slugs.append(f"highest-temperature-in-{city_name}-on-{month_full}-{day}")
            slugs.append(f"highest-temperature-in-{city_name}-on-{month_short}-{day}")
        
        return slugs
    
    def _generate_stock_slugs(self, ticker: str, date: datetime) -> List[str]:
        """Generate possible event slugs for a stock/date combo."""
        ticker_slug = self.STOCKS.get(ticker.upper(), ticker.lower())
        
        month_full = date.strftime("%B").lower()
        month_short = date.strftime("%b").lower()
        day = date.day
        year = date.year
        
        # Stock pattern: tsla-up-or-down-on-february-5-2026
        return [
            f"{ticker_slug}-up-or-down-on-{month_full}-{day}-{year}",
            f"{ticker_slug}-up-or-down-on-{month_short}-{day}-{year}",
            f"{ticker_slug}-up-or-down-on-{month_full}-{day}",
            f"{ticker_slug}-up-or-down-on-{month_short}-{day}",
        ]
    
    async def get_market_by_token(self, token_id: str, retries: int = 2) -> Optional[dict]:
        """Fetch full market data for a token via Gamma API.
        
        Returns the raw Gamma API market dict with bestBid, bestAsk,
        question, conditionId, groupItemTitle, volume, liquidity, etc.
        Used by SpreadScanner.refresh_opportunity() to build SpreadOpportunity
        objects for tokens not in its cache (e.g., hydrated from previous sessions).
        """
        session = await self._get_session()
        url = f"{self.GAMMA_API_URL}/markets?clob_token_ids={token_id}"
        
        for attempt in range(retries + 1):
            async with self._request_semaphore:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list) and len(data) > 0:
                                return data[0]
                            return None  # Empty response, no point retrying
                        elif resp.status == 429 and attempt < retries:
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    if attempt < retries:
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    print(f"   ⚠️ Error fetching market for token {token_id[:16]}... after {retries + 1} attempts (timeout)")
                    return None
                except Exception as e:
                    print(f"   ⚠️ Error fetching market for token {token_id[:16]}...: {type(e).__name__}: {e}")
                    return None
        return None
    
    async def get_event_by_slug(self, slug: str, retries: int = 2) -> Optional[Dict]:
        """Fetch event data by slug."""
        session = await self._get_session()
        url = f"{self.GAMMA_API_URL}/events?slug={slug}"
        
        for attempt in range(retries + 1):
            async with self._request_semaphore:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list) and len(data) > 0:
                                return data[0]
                            return data if isinstance(data, dict) and data.get("id") else None
                        elif resp.status == 429 and attempt < retries:
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    if attempt < retries:
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    print(f"   ⚠️ Error fetching event {slug} after {retries + 1} attempts (timeout)")
                    return None
                except Exception as e:
                    print(f"   ⚠️ Error fetching event {slug}: {type(e).__name__}: {e}")
                    return None
        return None
    
    async def get_weather_events_by_tag(self, limit: int = 100, retries: int = 2) -> List[Dict]:
        """Fetch all active weather events."""
        session = await self._get_session()
        url = f"{self.GAMMA_API_URL}/events?tag_id={self.WEATHER_TAG_ID}&limit={limit}&active=true"
        
        for attempt in range(retries + 1):
            async with self._request_semaphore:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data if isinstance(data, list) else []
                        elif resp.status == 429 and attempt < retries:
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    if attempt < retries:
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    print(f"   ⚠️ Error fetching weather events after {retries + 1} attempts (timeout)")
                    return []
                except Exception as e:
                    print(f"   ⚠️ Error fetching weather events: {e}")
                    return []
        return []
    
    def _parse_event(self, data: Dict) -> Optional[WeatherEvent]:
        """Parse raw event data into WeatherEvent."""
        if not data:
            return None
        
        title = data.get("title", "")
        slug = data.get("slug", "")
        
        # Extract city from title (e.g., "Highest temperature in Seattle on January 28?")
        city = ""
        if " in " in title.lower() and " on " in title.lower():
            try:
                city = title.lower().split(" in ")[1].split(" on ")[0].strip()
            except:
                pass
        
        # Extract date string from slug
        date_str = ""
        if "-on-" in slug:
            date_str = slug.split("-on-")[-1]
        
        # Parse end date
        end_date = None
        end_str = data.get("endDate")
        if end_str:
            try:
                end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except:
                pass
        
        # Parse start date
        start_date = None
        start_str = data.get("startDate")
        if start_str:
            try:
                start_date = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except:
                pass
        
        # Parse actual game start time ("startTime" field = real kick-off/tip-off)
        game_start_time = None
        gst_str = data.get("startTime")
        if gst_str:
            try:
                game_start_time = datetime.fromisoformat(gst_str.replace("Z", "+00:00"))
            except:
                pass
        
        # Parse markets
        markets = []
        for m in data.get("markets", []):
            market = self._parse_market(m)
            if market:
                markets.append(market)
        
        return WeatherEvent(
            event_id=str(data.get("id", "")),
            slug=slug,
            title=title,
            city=city,
            date_str=date_str,
            markets=markets,
            volume=float(data.get("volume", 0) or 0),
            liquidity=float(data.get("liquidity", 0) or 0),
            active=data.get("active", True),
            end_date=end_date,
            start_date=start_date,
            game_start_time=game_start_time,
        )
    
    def _parse_market(self, m: Dict) -> Optional[WeatherMarket]:
        """Parse raw market data into WeatherMarket."""
        if not m:
            return None
        
        # Parse token IDs
        clob_token_ids = m.get("clobTokenIds", "[]")
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except:
                clob_token_ids = []
        
        if not clob_token_ids or len(clob_token_ids) < 2:
            return None
        
        # Get best bid/ask (already provided by Gamma API)
        best_bid = float(m.get("bestBid", 0) or 0)
        best_ask = float(m.get("bestAsk", 1) or 1)
        
        # CRITICAL: Recalculate spread from bid/ask instead of trusting API's spread field
        # The API's spread field can be stale or calculated differently
        spread = best_ask - best_bid if best_ask > best_bid else 0.0
        
        # Calculate mid price
        mid_price = None
        if best_bid > 0 and best_ask < 1:
            mid_price = (best_bid + best_ask) / 2
        
        return WeatherMarket(
            market_id=m.get("id", ""),
            condition_id=m.get("conditionId", ""),
            question=m.get("question", ""),
            group_item=m.get("groupItemTitle") or m.get("question", ""),
            token_id=clob_token_ids[0],  # Yes token
            no_token_id=clob_token_ids[1],  # No token
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            mid_price=mid_price,
            volume=float(m.get("volumeNum", 0) or 0),
            liquidity=float(m.get("liquidityNum", 0) or 0),
            active=m.get("active", True),
            accepting_orders=m.get("acceptingOrders", True),
        )
    
    async def get_event(self, city: str, date: datetime) -> Optional[WeatherEvent]:
        """Get weather event for a specific city and date."""
        slugs = self._generate_slugs(city, date)
        
        for slug in slugs:
            raw = await self.get_event_by_slug(slug)
            if raw:
                return self._parse_event(raw)
        
        return None
    
    async def get_todays_events(self) -> List[WeatherEvent]:
        """Get all weather events for today."""
        today = datetime.now(timezone.utc).date()
        return await self.get_events_for_date(datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc))
    
    async def get_tomorrows_events(self) -> List[WeatherEvent]:
        """Get all weather events for tomorrow."""
        tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
        return await self.get_events_for_date(datetime.combine(tomorrow, datetime.min.time()).replace(tzinfo=timezone.utc))
    
    async def get_events_for_date(self, date: datetime) -> List[WeatherEvent]:
        """Get all weather events for a specific date across all cities."""
        tasks = [self.get_event(city, date) for city in self.CITIES.keys()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        events = []
        for result in results:
            if isinstance(result, WeatherEvent):
                events.append(result)
        
        return events
    
    async def get_stock_event(self, ticker: str, date: datetime) -> Optional[WeatherEvent]:
        """Get stock event for a specific ticker and date."""
        slugs = self._generate_stock_slugs(ticker, date)
        
        for slug in slugs:
            event = await self.get_event_by_slug(slug)
            if event:
                # Parse using same structure as weather events
                parsed = self._parse_event(event[0] if isinstance(event, list) else event)
                if parsed:
                    # Add ticker as city for consistency
                    parsed.city = ticker.upper()
                    return parsed
        return None
    
    async def get_stock_events_for_date(self, date: datetime) -> List[WeatherEvent]:
        """Get all stock events for a specific date."""
        tasks = [self.get_stock_event(ticker, date) for ticker in self.STOCKS.keys()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        now = datetime.now(tz=timezone.utc)
        from src.core.config import SPREAD_CONFIG
        deadline_hours = SPREAD_CONFIG.get("deadline_stock_hours", 3)
        
        events = []
        for result in results:
            if isinstance(result, WeatherEvent):
                # Skip stocks within deadline_hours of market close
                if result.end_date:
                    ed = result.end_date if result.end_date.tzinfo else result.end_date.replace(tzinfo=timezone.utc)
                    hours_left = (ed - now).total_seconds() / 3600
                    if hours_left <= deadline_hours:
                        continue
                events.append(result)
        
        return events
    
    async def get_series_events(self, series_id: str) -> List[WeatherEvent]:
        """Fetch active events from a Polymarket series by ID.
        
        Uses the events?series_id= endpoint (NOT /series/{id}) because
        the latter doesn't hydrate market data properly.
        
        Paginates through all results since default limit is 20.
        """
        session = await self._get_session()
        all_events = []
        offset = 0
        limit = 100
        req_timeout = aiohttp.ClientTimeout(total=30, connect=10)
        
        while True:
            params = {
                "series_id": series_id,
                "closed": "false",
                "limit": str(limit),
                "offset": str(offset),
            }
            
            async with self._request_semaphore:
                try:
                    async with session.get(
                        f"{self.GAMMA_API_URL}/events",
                        params=params,
                        timeout=req_timeout,
                    ) as resp:
                        if resp.status != 200:
                            break
                        data = await resp.json()
                except Exception as e:
                    print(f"   ⚠️ Error fetching series {series_id} events: {e}")
                    break
            
            if not data:
                break
            
            for item in data:
                parsed = self._parse_event(item)
                if parsed and parsed.markets:
                    # Mark as sports event (not weather/stock)
                    parsed.market_type = "sports"
                    all_events.append(parsed)
            
            if len(data) < limit:
                break
            offset += limit
        
        return all_events
    
    
    async def get_basketball_events(self) -> List[WeatherEvent]:
        """Get all active basketball events (leagues NOT covered by SportsBot)."""
        results = await asyncio.gather(
            self.get_series_events(self.CWBB_SERIES_ID),
            self.get_series_events(self.BB_CBA_SERIES_ID),
            return_exceptions=True,
        )
        
        now = datetime.now(tz=timezone.utc)
        all_events = []
        for result in results:
            if isinstance(result, list):
                for event in result:
                    event.market_type = "basketball"
                    # Skip live events — game_start_time is in the past
                    gst = event.game_start_time
                    if gst and (gst if gst.tzinfo else gst.replace(tzinfo=timezone.utc)) <= now:
                        continue
                    all_events.extend([event])
        return all_events
    
    # get_rugby_events removed — rugby moved to market_client.py SPORTS_SERIES_MAP
    
    async def get_mentions_events(self) -> List[WeatherEvent]:
        """Fetch all active mentions markets (earnings calls, political speeches, etc).
        
        Mentions markets are multi-outcome events where each sub-market is a
        binary Yes/No on whether a word/phrase will be said during an event.
        Uses tag_id=100343 (Mentions tag).
        """
        session = await self._get_session()
        all_events = []
        offset = 0
        limit = 100
        req_timeout = aiohttp.ClientTimeout(total=30, connect=10)
        
        while True:
            params = {
                "tag_id": self.MENTIONS_TAG_ID,
                "active": "true",
                "closed": "false",
                "limit": str(limit),
                "offset": str(offset),
            }
            
            fetched = False
            for attempt in range(3):
                async with self._request_semaphore:
                    try:
                        async with session.get(
                            f"{self.GAMMA_API_URL}/events",
                            params=params,
                            timeout=req_timeout,
                        ) as resp:
                            if resp.status == 429 and attempt < 2:
                                await asyncio.sleep(1.0 * (attempt + 1))
                                continue
                            if resp.status != 200:
                                break
                            data = await resp.json()
                            fetched = True
                            break
                    except (asyncio.TimeoutError, aiohttp.ClientError):
                        if attempt < 2:
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        print(f"   ⚠️ Error fetching mentions events after 3 attempts (timeout)")
                        break
                    except Exception as e:
                        print(f"   ⚠️ Error fetching mentions events: {e}")
                        break
            if not fetched:
                break
            
            if not data:
                break
            
            for item in data:
                parsed = self._parse_event(item)
                if parsed and parsed.markets:
                    parsed.market_type = "mentions"
                    all_events.append(parsed)
            
            if len(data) < limit:
                break
            offset += limit
        
        now = datetime.now(tz=timezone.utc)
        filtered = []
        for event in all_events:
            # Skip mentions events that have already started
            gst = event.game_start_time
            if gst and (gst if gst.tzinfo else gst.replace(tzinfo=timezone.utc)) <= now:
                continue
            filtered.append(event)
        
        return filtered
    
    async def get_tennis_events(self) -> List[WeatherEvent]:
        """Get all active tennis events (ATP + WTA).
        
        Filters out events that have already started (live matches) to prevent
        placing prematch spread orders on live tennis events where O/U lines
        shift instantly with set scores.
        """
        results = await asyncio.gather(
            self.get_series_events(self.TENNIS_ATP_SERIES_ID),
            self.get_series_events(self.TENNIS_WTA_SERIES_ID),
            return_exceptions=True,
        )
        
        now = datetime.now(tz=timezone.utc)
        all_events = []
        for result in results:
            if isinstance(result, list):
                for event in result:
                    event.market_type = "tennis"
                    # Skip live events — game_start_time is in the past
                    gst = event.game_start_time
                    if gst and (gst if gst.tzinfo else gst.replace(tzinfo=timezone.utc)) <= now:
                        continue
                    all_events.append(event)
        return all_events
    
    async def get_cricket_events(self) -> List[WeatherEvent]:
        """Get all active cricket events across tournaments."""
        results = await asyncio.gather(
            *(self.get_series_events(sid) for sid in self.CRICKET_SERIES_IDS),
            return_exceptions=True,
        )
        
        now = datetime.now(tz=timezone.utc)
        all_events = []
        for result in results:
            if isinstance(result, list):
                for event in result:
                    event.market_type = "cricket"
                    gst = event.game_start_time
                    if gst and (gst if gst.tzinfo else gst.replace(tzinfo=timezone.utc)) <= now:
                        continue
                    all_events.append(event)
        return all_events
    
    # get_hockey_events removed — hockey moved to market_client.py SPORTS_SERIES_MAP
    
    async def get_ufc_events(self) -> List[WeatherEvent]:
        """Get all active UFC and Zuffa Boxing events."""
        results = await asyncio.gather(
            self.get_series_events(self.UFC_SERIES_ID),
            self.get_series_events(self.ZUFFA_SERIES_ID),
            return_exceptions=True,
        )
        
        now = datetime.now(tz=timezone.utc)
        all_events = []
        for result in results:
            if isinstance(result, list):
                for event in result:
                    event.market_type = "ufc"
                    gst = event.game_start_time
                    if gst and (gst if gst.tzinfo else gst.replace(tzinfo=timezone.utc)) <= now:
                        continue
                    all_events.append(event)
        return all_events
    
    async def get_football_events(self) -> List[WeatherEvent]:
        """Get all active football (soccer) events across leagues."""
        results = await asyncio.gather(
            *(self.get_series_events(sid) for sid in self.FOOTBALL_SERIES_IDS),
            return_exceptions=True,
        )
        
        now = datetime.now(tz=timezone.utc)
        all_events = []
        for result in results:
            if isinstance(result, list):
                for event in result:
                    event.market_type = "football"
                    gst = event.game_start_time
                    if gst and (gst if gst.tzinfo else gst.replace(tzinfo=timezone.utc)) <= now:
                        continue
                    all_events.append(event)
        return all_events
    
    async def get_all_active_events(self) -> List[WeatherEvent]:
        """Get all active events across all supported market types.
        
        - Weather: today + tomorrow + day after (3 days)
        - Stocks: today + next 3 calendar days (covers Fri→Mon weekend gap)
        - Basketball: NCAAB + CWBB + KBL + LNB + NBL + Serie A + Liga Endesa + Champions League + CBA + Pro A
        - Rugby: Premiership, Top14, URC, Six Nations, Champions Cup, Super Rugby, Championship
        - Tennis: ATP + WTA
        - Cricket: IPL, ODI, T20, Big Bash, and 20+ other series
        - Hockey: NHL, AHL, KHL, SHL, Czech Extraliga, DEL, Swiss NL
        - UFC: UFC
        - Football: EPL, La Liga, Bundesliga, Ligue 1, Serie A, UCL, UEL, MLS, and 40+ more
        """
        today = datetime.now(timezone.utc).date()
        tomorrow = today + timedelta(days=1)
        day_after = today + timedelta(days=2)
        day_3 = today + timedelta(days=3)
        today_dt = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        tomorrow_dt = datetime.combine(tomorrow, datetime.min.time()).replace(tzinfo=timezone.utc)
        day_after_dt = datetime.combine(day_after, datetime.min.time()).replace(tzinfo=timezone.utc)
        day_3_dt = datetime.combine(day_3, datetime.min.time()).replace(tzinfo=timezone.utc)
        
        # Fetch all market types in parallel
        (
            today_weather, tomorrow_weather, day_after_weather,
            today_stocks, tomorrow_stocks, day_after_stocks, day_3_stocks,
            basketball_events,
            mentions_events,
            tennis_events,
            cricket_events,
            ufc_events,
            football_events,
        ) = await asyncio.gather(
            self.get_events_for_date(today_dt),
            self.get_events_for_date(tomorrow_dt),
            self.get_events_for_date(day_after_dt),
            self.get_stock_events_for_date(today_dt),
            self.get_stock_events_for_date(tomorrow_dt),
            self.get_stock_events_for_date(day_after_dt),
            self.get_stock_events_for_date(day_3_dt),
            self.get_basketball_events(),
            self.get_mentions_events(),
            self.get_tennis_events(),
            self.get_cricket_events(),
            self.get_ufc_events(),
            self.get_football_events(),
        )
        
        # Deadline filtering for weather events (city-based peak temp cutoff)
        from src.core.config import SPREAD_CONFIG
        now = datetime.now(timezone.utc)
        cutoffs = SPREAD_CONFIG.get("weather_cutoff_utc", {})
        
        filtered_weather = []
        for event in today_weather + tomorrow_weather + day_after_weather:
            if event.city and event.end_date:
                cutoff = cutoffs.get(event.city.lower())
                if cutoff is not None:
                    # Only filter on event day (end_date is noon next day, so event_day = end_date - 1 day)
                    event_day = (event.end_date - timedelta(days=1)).date()
                    if now.date() == event_day and now.hour >= cutoff:
                        continue
            filtered_weather.append(event)
        
        return (filtered_weather +
                today_stocks + tomorrow_stocks + day_after_stocks + day_3_stocks +
                basketball_events + mentions_events + tennis_events +
                cricket_events + ufc_events + football_events)
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


# CLI for testing
async def main():
    async with WeatherMarketClient() as client:
        print("🌡️ Weather Market Discovery")
        print("=" * 60)
        
        events = await client.get_all_active_events()
        print(f"Found {len(events)} active weather events\n")
        
        for event in events:
            print(f"📅 {event.title}")
            print(f"   City: {event.city} | Date: {event.date_str}")
            print(f"   Volume: ${event.volume:,.0f} | Liquidity: ${event.liquidity:,.0f}")
            
            for m in event.markets:
                spread_cents = m.spread * 100
                spread_emoji = "🔥" if spread_cents >= 15 else "  "
                print(f"   {spread_emoji} {m.group_item}: bid ${m.best_bid:.3f} / ask ${m.best_ask:.3f} = {spread_cents:.1f}¢")
            print()


if __name__ == "__main__":
    asyncio.run(main())
