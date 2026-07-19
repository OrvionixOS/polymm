"""
Polymarket User WebSocket - Real-time order and fill updates.

Subscribes to the authenticated user channel to receive:
- Order status updates (placed, matched, cancelled)
- Trade/fill notifications
- Position changes

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/user
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Callable
import aiohttp
import os

logger = logging.getLogger(__name__)


@dataclass
class OrderUpdate:
    """Real-time order update from WebSocket."""
    order_id: str
    status: str              # "LIVE", "MATCHED", "CANCELED", etc.
    asset_id: str            # token_id
    side: str                # "BUY" or "SELL"
    original_size: float
    size_matched: float      # Amount filled
    price: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def is_filled(self) -> bool:
        return self.status == "MATCHED" or self.size_matched >= self.original_size
    
    @property
    def is_partial(self) -> bool:
        return 0 < self.size_matched < self.original_size
    
    @property
    def is_live(self) -> bool:
        return self.status == "LIVE"
    
    @property
    def is_cancelled(self) -> bool:
        return self.status == "CANCELED"


@dataclass 
class TradeUpdate:
    """Real-time trade/fill notification."""
    trade_id: str
    order_id: str
    asset_id: str
    side: str
    size: float
    price: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PolymarketUserWebSocket:
    """
    Authenticated WebSocket client for real-time order updates.
    
    Features auto-reconnection with exponential backoff.
    
    Usage:
        ws = PolymarketUserWebSocket(api_key, api_secret, api_passphrase)
        await ws.connect()
        
        # Set callbacks
        ws.on_order_update = my_order_handler
        ws.on_trade = my_trade_handler
        
        # Keep running
        await ws.run()
    """
    
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    
    # Reconnection settings
    RECONNECT_MIN_DELAY = 1.0      # Start at 1 second
    RECONNECT_MAX_DELAY = 60.0     # Max 60 seconds  
    RECONNECT_MAX_ATTEMPTS = 0     # 0 = infinite retries (critical for order tracking)
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        condition_ids: Optional[List[str]] = None,
    ):
        """
        Initialize the user WebSocket.
        
        Args:
            api_key: L2 API key
            api_secret: L2 API secret
            api_passphrase: L2 API passphrase
            condition_ids: Optional list of condition IDs to filter (empty = all)
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.condition_ids = condition_ids or []
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        self._receive_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        
        # Reconnection state
        self._reconnect_attempts = 0
        self._is_reconnecting = False
        
        # Health monitoring
        self._last_message_time: Optional[datetime] = None
        self._health_check_interval = 30  # Check every 30 seconds
        self._stale_threshold = 90  # Consider stale after 90 seconds (user WS less active than book)
        
        # Callbacks
        self.on_order_update: Optional[Callable[[OrderUpdate], None]] = None
        self.on_trade: Optional[Callable[[TradeUpdate], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.on_reconnect: Optional[Callable[[], None]] = None
        
        # Order cache
        self._orders: Dict[str, OrderUpdate] = {}
    
    async def connect(self) -> bool:
        """Connect to the WebSocket and authenticate."""
        try:
            self._session = aiohttp.ClientSession()
            # heartbeat=30 enables automatic ping/pong every 30s to detect dead connections
            self._ws = await self._session.ws_connect(self.WS_URL, heartbeat=30)
            logger.info(f"Connected to {self.WS_URL}")
            
            # Send authentication message
            auth_msg = {
                "type": "user",
                "markets": self.condition_ids,
                "auth": {
                    "apiKey": self.api_key,
                    "secret": self.api_secret,
                    "passphrase": self.api_passphrase,
                }
            }
            await self._ws.send_json(auth_msg)
            logger.info("Sent authentication message")
            
            self._running = True
            self._last_message_time = datetime.now(timezone.utc)
            return True
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
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
        
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
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
        
        logger.info("User WebSocket closed")
    
    async def run(self):
        """
        Main loop: receive messages, send periodic pings, and monitor health.
        """
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())
        self._health_task = asyncio.create_task(self._health_check_loop())
        
        await asyncio.gather(self._receive_task, self._ping_task, self._health_task, return_exceptions=True)
    
    async def _ping_loop(self):
        """Send periodic pings to keep connection alive."""
        while self._running:
            try:
                if self._ws and not self._ws.closed:
                    await self._ws.send_str("PING")
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"Ping error: {e}")
                break
    
    async def _receive_loop(self):
        """Receive and process WebSocket messages."""
        try:
            while self._running and self._ws and not self._ws.closed:
                try:
                    msg = await self._ws.receive(timeout=30)
                    
                    # Update last message time for health monitoring
                    self._last_message_time = datetime.now(timezone.utc)
                    
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if msg.data == "PONG":
                            continue
                        await self._process_message(msg.data)
                        
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"WebSocket error: {self._ws.exception()}")
                        break
                        
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        logger.info("WebSocket closed by server")
                        break
                        
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"Receive error: {e}")
                    if self.on_error:
                        self.on_error(e)
                    break
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
                
                # Check if we haven't received messages for too long
                if self._last_message_time:
                    now = datetime.now(timezone.utc)
                    seconds_since_message = (now - self._last_message_time).total_seconds()
                    
                    if seconds_since_message > self._stale_threshold:
                        print(f"⚠️ User WebSocket stale ({seconds_since_message:.0f}s without messages), forcing reconnect...")
                        
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
        
        max_attempts = self.RECONNECT_MAX_ATTEMPTS if self.RECONNECT_MAX_ATTEMPTS > 0 else float('inf')
        
        while self._running and self._reconnect_attempts < max_attempts:
            self._reconnect_attempts += 1
            delay = min(
                self.RECONNECT_MIN_DELAY * (2 ** (self._reconnect_attempts - 1)),
                self.RECONNECT_MAX_DELAY
            )
            
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
                
                # Re-authenticate
                auth_msg = {
                    "type": "user",
                    "markets": self.condition_ids,
                    "auth": {
                        "apiKey": self.api_key,
                        "secret": self.api_secret,
                        "passphrase": self.api_passphrase,
                    }
                }
                await self._ws.send_json(auth_msg)
                
                # CRITICAL: Cancel old tasks before creating new ones to prevent accumulation
                if self._receive_task and not self._receive_task.done():
                    self._receive_task.cancel()
                    try:
                        await self._receive_task
                    except asyncio.CancelledError:
                        pass
                
                if self._ping_task and not self._ping_task.done():
                    self._ping_task.cancel()
                    try:
                        await self._ping_task
                    except asyncio.CancelledError:
                        pass
                
                if self._health_task and not self._health_task.done():
                    self._health_task.cancel()
                    try:
                        await self._health_task
                    except asyncio.CancelledError:
                        pass
                
                # Restart tasks
                self._receive_task = asyncio.create_task(self._receive_loop())
                self._ping_task = asyncio.create_task(self._ping_loop())
                self._health_task = asyncio.create_task(self._health_check_loop())
                self._last_message_time = datetime.now(timezone.utc)
                
                logger.info("✅ User WebSocket reconnected and re-authenticated")
                print("✅ User WebSocket reconnected")
                
                if self.on_reconnect:
                    self.on_reconnect()
                
                self._is_reconnecting = False
                self._reconnect_attempts = 0
                return
                
            except Exception as e:
                logger.error(f"Reconnection attempt {self._reconnect_attempts} failed: {e}")
        
        self._is_reconnecting = False
        logger.error(f"❌ User WebSocket reconnection failed")
        print("❌ User WebSocket reconnection failed - order updates may be delayed!")
    
    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is currently connected."""
        return self._ws is not None and not self._ws.closed and self._running
    
    async def _process_message(self, data: str):
        """Process a WebSocket message."""
        # Skip empty messages (keepalives, ping/pong, etc.)
        text = data.strip() if data else ""
        if not text:
            return
        
        # Skip known non-JSON keepalive messages
        if text in ("PONG", "pong", "PING", "ping", "ok", "OK"):
            return
        
        # Must start with { or [ to be JSON
        if not text.startswith(("{", "[")):
            return
        
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            # Silently skip - likely a malformed keepalive
            return
        
        msg_type = msg.get("event_type") or msg.get("type")
        
        if msg_type == "order":
            await self._handle_order_update(msg)
        elif msg_type == "trade":
            await self._handle_trade(msg)
        elif msg_type == "error":
            logger.error(f"Server error: {msg}")
            if self.on_error:
                self.on_error(Exception(str(msg)))
        else:
            logger.debug(f"Unknown message type: {msg_type}, data: {msg}")
    
    async def _handle_order_update(self, msg: dict):
        """Handle order status update."""
        try:
            order = OrderUpdate(
                order_id=msg.get("order_id", msg.get("id", "")),
                status=msg.get("status", msg.get("order_status", "UNKNOWN")),
                asset_id=msg.get("asset_id", msg.get("token_id", "")),
                side=msg.get("side", "BUY"),
                original_size=float(msg.get("original_size", msg.get("size", 0))),
                size_matched=float(msg.get("size_matched", msg.get("matched", 0))),
                price=float(msg.get("price", 0)),
            )
            
            self._orders[order.order_id] = order
            
            # DEBUG: Log the full message for cancelled orders to see the reason
            if order.is_cancelled:
                logger.debug(f"Cancel debug - full msg: {msg}")
                cancel_reason = msg.get("cancel_reason") or msg.get("reason") or msg.get("cancellation_reason")
                if cancel_reason:
                    logger.info(f"Order cancelled - reason: {cancel_reason}")
                    print(f"   🔍 Cancel reason: {cancel_reason}")
            
            if self.on_order_update:
                await self._maybe_await(self.on_order_update, order)
                
            logger.info(f"Order update: {order.order_id} - {order.status} ({order.size_matched}/{order.original_size})")
            
        except Exception as e:
            logger.error(f"Error processing order update: {e}, msg: {msg}")
    
    async def _handle_trade(self, msg: dict):
        """Handle trade/fill notification."""
        try:
            trade = TradeUpdate(
                trade_id=msg.get("trade_id", msg.get("id", "")),
                order_id=msg.get("order_id", ""),
                asset_id=msg.get("asset_id", msg.get("token_id", "")),
                side=msg.get("side", "BUY"),
                size=float(msg.get("size", 0)),
                price=float(msg.get("price", 0)),
            )
            
            # Update order cache
            if trade.order_id in self._orders:
                order = self._orders[trade.order_id]
                order.size_matched += trade.size
            
            if self.on_trade:
                await self._maybe_await(self.on_trade, trade)
                
            logger.info(f"Trade: {trade.trade_id} - {trade.side} {trade.size} @ {trade.price}")
            
        except Exception as e:
            logger.error(f"Error processing trade: {e}, msg: {msg}")
    
    async def _maybe_await(self, callback, *args):
        """Call a callback, awaiting if it's async."""
        result = callback(*args)
        if asyncio.iscoroutine(result):
            await result
    
    def get_order(self, order_id: str) -> Optional[OrderUpdate]:
        """Get cached order by ID."""
        return self._orders.get(order_id)
    
    def get_all_orders(self) -> Dict[str, OrderUpdate]:
        """Get all cached orders."""
        return self._orders.copy()
    
    async def __aenter__(self):
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


async def demo():
    """Demo the user WebSocket (requires credentials)."""
    from dotenv import load_dotenv
    from py_clob_client.client import ClobClient
    
    load_dotenv()
    
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    
    if not pk:
        print("❌ POLYMARKET_PRIVATE_KEY required")
        return
    
    # Get API credentials
    print("🔑 Deriving API credentials...")
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=pk,
        chain_id=137,
        signature_type=0,
        funder=funder,
    )
    creds = client.create_or_derive_api_creds()
    
    print(f"   API Key: {creds.api_key[:20]}...")
    
    # Connect to user WebSocket
    print("\n🔌 Connecting to User WebSocket...")
    
    async def on_order(order: OrderUpdate):
        print(f"   📦 Order: {order.order_id[:20]}... {order.status}")
    
    async def on_trade(trade: TradeUpdate):
        print(f"   💰 Trade: {trade.size} @ {trade.price}")
    
    ws = PolymarketUserWebSocket(
        api_key=creds.api_key,
        api_secret=creds.api_secret,
        api_passphrase=creds.api_passphrase,
    )
    
    ws.on_order_update = on_order
    ws.on_trade = on_trade
    
    if await ws.connect():
        print("✅ Connected! Waiting for order updates (Ctrl+C to stop)...")
        try:
            await ws.run()
        except KeyboardInterrupt:
            print("\n⏹️ Stopping...")
    else:
        print("❌ Failed to connect")
    
    await ws.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(demo())
