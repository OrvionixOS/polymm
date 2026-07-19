"""
Polymarket WebSocket client for real-time price updates.

Provides streaming orderbook and trade data via WebSocket connection
to wss://ws-subscriptions-clob.polymarket.com/ws/market
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Callable, Any
import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class LivePrice:
    """Real-time price data for a market outcome."""
    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    last_trade: Optional[float] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Full L2 order book depth from WS book events
    bids: List[dict] = field(default_factory=list)
    asks: List[dict] = field(default_factory=list)
    
    @property
    def mid_price(self) -> Optional[float]:
        """Calculate mid price from bid/ask."""
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.last_trade
    
    @property
    def spread(self) -> Optional[float]:
        """Calculate bid-ask spread."""
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None


@dataclass
class MarketPrices:
    """Aggregated prices for a market (Yes/No outcomes)."""
    market_id: str
    question: str
    yes_price: Optional[LivePrice] = None
    no_price: Optional[LivePrice] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def yes_probability(self) -> Optional[float]:
        """Get Yes probability from mid price."""
        if self.yes_price and self.yes_price.mid_price:
            return self.yes_price.mid_price
        return None
    
    @property
    def no_probability(self) -> Optional[float]:
        """Get No probability from mid price."""
        if self.no_price and self.no_price.mid_price:
            return self.no_price.mid_price
        return None


class PolymarketWebSocket:
    """
    WebSocket client for real-time Polymarket price updates.
    
    Features auto-reconnection with exponential backoff.
    
    Usage:
        ws = PolymarketWebSocket()
        await ws.connect()
        await ws.subscribe(token_ids)
        
        # Get prices
        price = ws.get_price(token_id)
        
        # Or register callback
        ws.on_price_update = my_callback
        
        await ws.close()
    """
    
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    # Reconnection settings
    RECONNECT_MIN_DELAY = 1.0      # Start at 1 second
    RECONNECT_MAX_DELAY = 60.0     # Max 60 seconds
    RECONNECT_MAX_ATTEMPTS = 10    # Max reconnection attempts before giving up
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._prices: Dict[str, LivePrice] = {}
        self._token_to_market: Dict[str, str] = {}  # token_id -> market label
        self._running = False
        self._receive_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        
        # Track subscriptions for reconnection
        self._subscribed_tokens: List[str] = []
        
        # Track which tokens received their initial WS book snapshot
        self._book_received: set = set()
        
        # Per-token staleness tracking
        self._last_update_per_token: Dict[str, datetime] = {}
        
        # Reconnection state
        self._reconnect_attempts = 0
        self._is_reconnecting = False
        
        # Health monitoring
        self._last_message_time: Optional[datetime] = None
        self._last_book_update_time: Optional[datetime] = None  # Track ACTUAL book data, not pings
        self._health_check_interval = 30  # Check every 30 seconds
        self._stale_threshold = 60  # Consider stale after 60 seconds without BOOK DATA
        
        # Callbacks
        self.on_price_update: Optional[Callable[[str, LivePrice], None]] = None
        self.on_book_update: Optional[Callable[[str, LivePrice], None]] = None
        self.on_trade: Optional[Callable[[str, float], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.on_reconnect: Optional[Callable[[], None]] = None
    
    async def connect(self) -> bool:
        """Establish WebSocket connection."""
        try:
            self._session = aiohttp.ClientSession()
            # heartbeat=30 enables automatic ping/pong every 30s to detect dead connections
            self._ws = await self._session.ws_connect(self.WS_URL, heartbeat=30)
            self._running = True
            self._last_message_time = datetime.now(timezone.utc)
            self._last_book_update_time = datetime.now(timezone.utc)  # Initialize book tracker
            
            # Start receiving messages in background
            self._receive_task = asyncio.create_task(self._receive_loop())
            
            # Start health monitoring
            self._health_task = asyncio.create_task(self._health_check_loop())
            
            logger.info("Connected to Polymarket WebSocket")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to WebSocket: {e}")
            return False
    
    async def close(self):
        """Close the WebSocket connection."""
        self._running = False
        
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        
        if self._ws:
            await self._ws.close()
        
        if self._session:
            await self._session.close()
        
        logger.info("Closed Polymarket WebSocket")
    
    async def subscribe(self, token_ids: List[str], token_labels: Optional[Dict[str, str]] = None):
        """
        Subscribe to price updates for given tokens.
        
        Args:
            token_ids: List of clobTokenIds to subscribe to
            token_labels: Optional mapping of token_id -> human-readable label
        """
        if not self._ws or self._ws.closed:
            logger.warning("WebSocket not connected, queueing subscription")
            # Store for later subscription on reconnect
            for tid in token_ids:
                if tid not in self._subscribed_tokens:
                    self._subscribed_tokens.append(tid)
            return
        
        # Store token labels for logging
        if token_labels:
            self._token_to_market.update(token_labels)
        
        # Track subscriptions for reconnection
        for tid in token_ids:
            if tid not in self._subscribed_tokens:
                self._subscribed_tokens.append(tid)
        
        # Send subscription message
        subscribe_msg = {
            "type": "subscribe",
            "channel": "market",
            "assets_ids": token_ids,
        }
        
        try:
            await self._ws.send_json(subscribe_msg)
            logger.info(f"Subscribed to {len(token_ids)} market tokens")
        except Exception as e:
            logger.error(f"Failed to subscribe: {e}")
            # Trigger reconnection
            if not self._is_reconnecting:
                asyncio.create_task(self._reconnect())
    
    async def _receive_loop(self):
        """Background task to receive and process WebSocket messages."""
        try:
            async for msg in self._ws:
                if not self._running:
                    break
                
                # Update last message time for connection health (includes pings)
                self._last_message_time = datetime.now(timezone.utc)
                
                if msg.type == aiohttp.WSMsgType.TEXT:
                    # Skip empty messages and keepalives
                    text = msg.data.strip() if msg.data else ""
                    if not text:
                        continue
                    
                    # Skip known non-JSON keepalive messages (don't update book timer)
                    if text in ("PONG", "pong", "PING", "ping", "ok", "OK"):
                        continue
                    
                    # Must start with { or [ to be JSON
                    if not text.startswith(("{", "[")):
                        continue
                    
                    try:
                        data = json.loads(text)
                        
                        # CRITICAL: Update book timer ONLY when we get actual book data
                        # This ensures stale detection triggers even if pings are flowing
                        self._last_book_update_time = datetime.now(timezone.utc)
                        
                        # Handle batch messages
                        if isinstance(data, list):
                            # Initial snapshot: count tokens with L2 data
                            l2_count = 0
                            for item in data:
                                self._process_message(item)
                                if isinstance(item, dict) and item.get("event_type") == "book":
                                    bids = item.get("bids", [])
                                    if bids:
                                        l2_count += 1
                            if len(data) > 1:
                                print(f"   📊 [BOOK WS] Snapshot: {len(data)} tokens, {l2_count} with bids")
                        else:
                            self._process_message(data)
                            
                    except json.JSONDecodeError as e:
                        # Log unexpected parse errors (but not too verbosely)
                        logger.debug(f"Failed to parse message: {e}")
                
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {self._ws.exception()}")
                    break
                
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.info("WebSocket closed by server")
                    break
                    
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in receive loop: {e}")
        finally:
            # Trigger reconnection if we're still supposed to be running
            if self._running and not self._is_reconnecting:
                if self.on_disconnect:
                    self.on_disconnect()
                asyncio.create_task(self._reconnect())
    
    async def _health_check_loop(self):
        """Periodically check WebSocket health and force reconnect if stale."""
        while self._running:
            try:
                await asyncio.sleep(self._health_check_interval)
                
                if not self._running:
                    break
                
                # Skip if already reconnecting - prevents duplicate warnings
                if self._is_reconnecting:
                    continue
                
                # Check if we have subscriptions but no recent BOOK DATA (not just pings)
                # This catches the case where the connection is alive (pings flowing)
                # but we're not getting actual order book updates
                if self._subscribed_tokens and self._last_book_update_time:
                    now = datetime.now(timezone.utc)
                    seconds_since_book_update = (now - self._last_book_update_time).total_seconds()
                    
                    if seconds_since_book_update > self._stale_threshold:
                        print(f"⚠️ Book WebSocket stale ({seconds_since_book_update:.0f}s without book data), forcing reconnect...")
                        
                        # Force close and reconnect
                        if self._ws and not self._ws.closed:
                            await self._ws.close()
                        
                        asyncio.create_task(self._reconnect())
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")
    
    async def _reconnect(self):
        """Attempt to reconnect with exponential backoff."""
        if self._is_reconnecting:
            return
        
        self._is_reconnecting = True
        self._reconnect_attempts = 0
        print("⚠️ Book WebSocket disconnected, attempting reconnect...")
        
        while self._running and self._reconnect_attempts < self.RECONNECT_MAX_ATTEMPTS:
            self._reconnect_attempts += 1
            delay = min(
                self.RECONNECT_MIN_DELAY * (2 ** (self._reconnect_attempts - 1)),
                self.RECONNECT_MAX_DELAY
            )
            
            print(f"🔄 Book WS reconnecting in {delay:.1f}s (attempt {self._reconnect_attempts}/{self.RECONNECT_MAX_ATTEMPTS})")
            await asyncio.sleep(delay)
            
            if not self._running:
                break
            
            # Close old connections
            try:
                if self._ws and not self._ws.closed:
                    await self._ws.close()
            except:
                pass
            
            # Try to reconnect
            try:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
                
                self._ws = await self._session.ws_connect(self.WS_URL, heartbeat=30)
                
                # Re-subscribe to all tokens BEFORE starting receive loop
                if self._subscribed_tokens:
                    subscribe_msg = {
                        "type": "subscribe",
                        "channel": "market",
                        "assets_ids": self._subscribed_tokens,
                    }
                    await self._ws.send_json(subscribe_msg)
                    print(f"✅ Book WS reconnected, re-subscribed to {len(self._subscribed_tokens)} tokens")
                else:
                    print("✅ Book WebSocket reconnected")
                
                # CRITICAL: Cancel old tasks before creating new ones to prevent accumulation
                if self._receive_task and not self._receive_task.done():
                    self._receive_task.cancel()
                    try:
                        await self._receive_task
                    except asyncio.CancelledError:
                        pass
                
                if self._health_task and not self._health_task.done():
                    self._health_task.cancel()
                    try:
                        await self._health_task
                    except asyncio.CancelledError:
                        pass
                
                # Restart receive loop and health check AFTER subscription
                self._receive_task = asyncio.create_task(self._receive_loop())
                self._health_task = asyncio.create_task(self._health_check_loop())
                self._last_message_time = datetime.now(timezone.utc)
                self._last_book_update_time = datetime.now(timezone.utc)  # Reset book update tracker
                
                if self.on_reconnect:
                    try:
                        self.on_reconnect()
                    except Exception as e:
                        logger.error(f"on_reconnect callback error: {e}")
                
                self._is_reconnecting = False
                self._reconnect_attempts = 0
                return
                
            except Exception as e:
                print(f"⚠️ Book WS reconnect attempt {self._reconnect_attempts} failed: {e}")
        
        self._is_reconnecting = False
        print(f"❌ Book WebSocket reconnection FAILED after {self.RECONNECT_MAX_ATTEMPTS} attempts!")
        logger.error(f"❌ WebSocket reconnection failed after {self.RECONNECT_MAX_ATTEMPTS} attempts")
    
    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is currently connected."""
        return self._ws is not None and not self._ws.closed and self._running
    
    def _process_message(self, data: dict):
        """Process a single WebSocket message."""
        event_type = data.get("event_type", data.get("type", ""))
        asset_id = data.get("asset_id", "")
        
        if event_type == "price_change":
            # price_change messages have asset_id INSIDE price_changes[], not at top level
            self._process_price_changes(data)
            return
        
        if not asset_id:
            return
        
        # Get or create price object
        if asset_id not in self._prices:
            self._prices[asset_id] = LivePrice(token_id=asset_id)
        
        price = self._prices[asset_id]
        price.timestamp = datetime.now(timezone.utc)
        
        if event_type == "book":
            # Order book snapshot — store full L2 depth
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            if bids:
                price.best_bid = max(float(b["price"]) for b in bids)
                price.bids = sorted(bids, key=lambda b: float(b["price"]), reverse=True)
            if asks:
                price.best_ask = min(float(a["price"]) for a in asks)
                price.asks = sorted(asks, key=lambda a: float(a["price"]))
            
            # Track that this token received its WS book snapshot
            self._book_received.add(asset_id)
            
            # Track per-token update time for staleness detection
            self._last_update_per_token[asset_id] = datetime.now(timezone.utc)
            
            if self.on_book_update:
                self.on_book_update(asset_id, price)
            if self.on_price_update:
                self.on_price_update(asset_id, price)
                
            label = self._token_to_market.get(asset_id, asset_id[:20])
            logger.debug(f"Book update: {label} bid={price.best_bid} ask={price.best_ask} bids={len(bids)} asks={len(asks)}")
        
        elif event_type == "last_trade_price":
            # Trade occurred
            trade_price = data.get("price")
            if trade_price:
                price.last_trade = float(trade_price)
                
                if self.on_trade:
                    self.on_trade(asset_id, price.last_trade)
                if self.on_price_update:
                    self.on_price_update(asset_id, price)
                    
                label = self._token_to_market.get(asset_id, asset_id[:20])
                logger.debug(f"Trade: {label} price={price.last_trade}")
    
    def _process_price_changes(self, data: dict):
        """
        Process price_change events.
        
        These contain incremental L2 updates with best_bid/best_ask per asset.
        Format: {"event_type": "price_change", "price_changes": [
            {"asset_id": "...", "price": "0.14", "size": "60", "side": "BUY",
             "best_bid": "0.20", "best_ask": "0.25"}, ...
        ]}
        """
        now = datetime.now(timezone.utc)
        changes = data.get("price_changes", [])
        
        for change in changes:
            asset_id = change.get("asset_id", "")
            if not asset_id:
                continue
            
            if asset_id not in self._prices:
                self._prices[asset_id] = LivePrice(token_id=asset_id)
            
            price = self._prices[asset_id]
            price.timestamp = now
            
            # Update best bid/ask from the change event
            best_bid_str = change.get("best_bid")
            best_ask_str = change.get("best_ask")
            
            if best_bid_str:
                price.best_bid = float(best_bid_str)
            if best_ask_str:
                price.best_ask = float(best_ask_str)
            
            # Update L2 incrementally: add/update the changed price level
            change_price = change.get("price", "")
            change_size = change.get("size", "0")
            side = change.get("side", "")
            
            if change_price and side:
                entry = {"price": change_price, "size": change_size}
                
                if side == "BUY":
                    # Update bid level
                    price.bids = [b for b in price.bids if b["price"] != change_price]
                    if float(change_size) > 0:
                        price.bids.append(entry)
                        price.bids.sort(key=lambda b: float(b["price"]), reverse=True)
                elif side == "SELL":
                    # Update ask level
                    price.asks = [a for a in price.asks if a["price"] != change_price]
                    if float(change_size) > 0:
                        price.asks.append(entry)
                        price.asks.sort(key=lambda a: float(a["price"]))
            
            # Track per-token staleness
            self._last_update_per_token[asset_id] = now
            
            # Fire callbacks
            if self.on_book_update:
                self.on_book_update(asset_id, price)
            if self.on_price_update:
                self.on_price_update(asset_id, price)
    
    def get_price(self, token_id: str) -> Optional[LivePrice]:
        """Get the current price for a token."""
        return self._prices.get(token_id)
    
    def get_book(self, token_id: str) -> Optional[dict]:
        """
        Get the cached order book for a token.
        
        Returns a dict matching the REST /book response format:
        {"bids": [{"price": str, "size": str}, ...], "asks": [...]}
        Returns None if no WS data exists for this token.
        
        Falls back to synthesizing L2 from best_bid/best_ask when full L2
        lists are empty (e.g., REST-seeded tokens).
        """
        price = self._prices.get(token_id)
        if not price:
            return None
        
        bids = price.bids
        asks = price.asks
        
        # Synthesize minimal L2 from best_bid/best_ask if full L2 is empty
        if not bids and price.best_bid and price.best_bid > 0:
            bids = [{"price": str(price.best_bid), "size": "0"}]
        if not asks and price.best_ask and price.best_ask > 0:
            asks = [{"price": str(price.best_ask), "size": "0"}]
        
        if not bids and not asks:
            return None
        
        return {"bids": bids, "asks": asks}
    
    def get_all_prices(self) -> Dict[str, LivePrice]:
        """Get all current prices."""
        return self._prices.copy()
    
    def get_mid_price(self, token_id: str) -> Optional[float]:
        """Get the mid price for a token."""
        price = self._prices.get(token_id)
        return price.mid_price if price else None
    
    def get_stale_tokens(self, threshold_seconds: float = 120) -> List[str]:
        """
        Identify tokens that haven't received updates recently.
        
        Args:
            threshold_seconds: Tokens without updates for this long are considered stale
            
        Returns:
            List of token IDs that are stale
        """
        if not self._subscribed_tokens:
            return []
        
        now = datetime.now(timezone.utc)
        stale = []
        
        for token_id in self._subscribed_tokens:
            last_update = self._last_update_per_token.get(token_id)
            if last_update is None:
                # Never received an update - definitely stale
                stale.append(token_id)
            elif (now - last_update).total_seconds() > threshold_seconds:
                stale.append(token_id)
        
        return stale
    
    def get_token_last_update(self, token_id: str) -> Optional[datetime]:
        """Get the last update time for a specific token."""
        return self._last_update_per_token.get(token_id)
    
    async def resubscribe_tokens(self, token_ids: List[str]) -> bool:
        """
        Force resubscription for specific tokens.
        
        Sends unsubscribe then subscribe to reset the server-side subscription.
        This can help recover tokens that stopped receiving updates.
        
        Args:
            token_ids: List of token IDs to resubscribe
            
        Returns:
            True if resubscription messages were sent successfully
        """
        if not self._ws or self._ws.closed:
            logger.warning("Cannot resubscribe - WebSocket not connected")
            return False
        
        if not token_ids:
            return True
        
        try:
            # Step 1: Unsubscribe from stale tokens
            unsubscribe_msg = {
                "type": "unsubscribe",
                "channel": "market",
                "assets_ids": token_ids,
            }
            await self._ws.send_json(unsubscribe_msg)
            
            # Brief pause to let server process unsubscribe
            await asyncio.sleep(0.1)
            
            # Step 2: Resubscribe to same tokens
            subscribe_msg = {
                "type": "subscribe",
                "channel": "market",
                "assets_ids": token_ids,
            }
            await self._ws.send_json(subscribe_msg)
            
            # Reset per-token tracking for resubscribed tokens
            # (They should receive fresh updates soon)
            for token_id in token_ids:
                self._last_update_per_token.pop(token_id, None)
            
            logger.info(f"Resubscribed to {len(token_ids)} stale tokens")
            return True
            
        except Exception as e:
            logger.error(f"Failed to resubscribe tokens: {e}")
            return False
    
    async def seed_from_rest(self, token_id: str, rest_book: dict):
        """
        Seed the cache with full REST order book data.
        
        Populates both best_bid/best_ask AND full L2 bids/asks lists,
        so get_book() returns real depth for bid war evaluation.
        
        Args:
            token_id: Token to seed
            rest_book: REST order book dict with 'bids' and 'asks' arrays
        """
        if token_id not in self._prices:
            self._prices[token_id] = LivePrice(token_id=token_id)
        
        price = self._prices[token_id]
        now = datetime.now(timezone.utc)
        
        bids = rest_book.get("bids", [])
        asks = rest_book.get("asks", [])
        
        if bids:
            price.best_bid = max(float(b["price"]) for b in bids)
            price.bids = sorted(bids, key=lambda b: float(b["price"]), reverse=True)
        if asks:
            price.best_ask = min(float(a["price"]) for a in asks)
            price.asks = sorted(asks, key=lambda a: float(a["price"]))
        
        price.timestamp = now
        self._last_update_per_token[token_id] = now
    
    async def seed_all_from_rest(self, rest_bids: Dict[str, Dict]) -> int:
        """
        Seed the cache with REST data for ALL tokens.
        
        Called during periodic sync to ensure WS cache matches reality.
        Any WS updates arriving right after will overwrite with fresher data.
        
        Accepts either:
        - Full book dicts: {"bids": [...], "asks": [...]}
        - Simple bid dicts: {"price": float, "size": float}
        
        Args:
            rest_bids: Dict of token_id -> book or bid dict
            
        Returns:
            Number of tokens seeded
        """
        seeded = 0
        now = datetime.now(timezone.utc)
        
        for token_id, rest_data in rest_bids.items():
            if token_id not in self._prices:
                self._prices[token_id] = LivePrice(token_id=token_id)
            
            price = self._prices[token_id]
            
            # Check if this is a full book dict or a simple bid dict
            if "bids" in rest_data or "asks" in rest_data:
                # Full book — populate L2
                bids = rest_data.get("bids", [])
                asks = rest_data.get("asks", [])
                if bids:
                    price.best_bid = max(float(b["price"]) for b in bids)
                    price.bids = sorted(bids, key=lambda b: float(b["price"]), reverse=True)
                if asks:
                    price.best_ask = min(float(a["price"]) for a in asks)
                    price.asks = sorted(asks, key=lambda a: float(a["price"]))
                price.timestamp = now
                self._last_update_per_token[token_id] = now
                seeded += 1
            else:
                # Simple bid — just set best_bid
                rest_bid = rest_data.get("price", 0)
                if rest_bid > 0:
                    price.best_bid = rest_bid
                    price.timestamp = now
                    self._last_update_per_token[token_id] = now
                    seeded += 1
        
        return seeded
    
    async def __aenter__(self):
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
