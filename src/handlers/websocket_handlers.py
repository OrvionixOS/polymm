"""
WebSocket handlers - manages order book WebSocket connection.

REACTIVE ARCHITECTURE:
- Delegates ALL trading decisions to ReactiveBookHandler on every book update
- Pushes bid updates to BotState for centralized state management
- BotState evaluates edge changes and emits events via ReactiveHandler
"""
import asyncio
from typing import Optional, Callable

from src.polymarket.book_websocket import PolymarketWebSocket, LivePrice
from src.services.telegram_alerts import TelegramAlerts
from src.state.bot_state import get_bot_state


class WebSocketHandler:
    """
    Manages connection to Polymarket order book WebSocket.
    
    REACTIVE: Every book update is delegated to ReactiveBookHandler
    for trading decisions (outbid, price improvement, spread collapse).
    Also pushes bid updates to BotState for edge evaluation.
    
    Requires injection of:
    - alerts: TelegramAlerts instance
    - on_outbid_callback: Callable to queue adjustments (legacy fallback)
    - poly_client: Optional PolymarketEsportsClient for REST snapshot fetching
    """
    
    def __init__(
        self,
        alerts: TelegramAlerts,
        on_outbid_callback: Callable[[str, float], None],
        poly_client=None,
    ):
        self.alerts = alerts
        self._on_outbid_callback = on_outbid_callback
        self.bot_state = get_bot_state()
        self.poly_client = poly_client  # For REST snapshot fetching
        
        self.book_ws: Optional[PolymarketWebSocket] = None
        
        # Reactive handler — set by bot via set_reactive_handler()
        self._reactive_handler = None
    
    def set_poly_client(self, poly_client):
        """Set the Polymarket client for REST snapshot fetching."""
        self.poly_client = poly_client
    
    def set_reactive_handler(self, handler):
        """Set the ReactiveBookHandler for event-driven trading decisions."""
        self._reactive_handler = handler
    
    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self.book_ws is not None and self.book_ws.is_connected
    
    async def connect(self, watcher) -> bool:
        """Connect to order book WebSocket and subscribe to active tokens."""
        try:
            self.book_ws = PolymarketWebSocket()
            
            # Set callback for book updates - pass watcher for lookups
            self.book_ws.on_book_update = lambda token_id, price: self._on_book_update(
                token_id, price, watcher
            )
            
            # Set reconnect callback
            self.book_ws.on_reconnect = lambda: asyncio.create_task(
                self.alerts.on_websocket_reconnect("Order Book WebSocket")
            )
            self.book_ws.on_disconnect = lambda: print("⚠️ Book WebSocket disconnected, reconnecting...")
            
            connected = await self.book_ws.connect()
            if not connected:
                print("⚠️ Failed to connect Book WebSocket - falling back to polling")
                self.book_ws = None
                return False
            
            # Subscribe to all active token IDs (from BotState - single source of truth)
            active_tokens = list(watcher.bot_state.get_active_token_ids())
            if active_tokens:
                await self.book_ws.subscribe(active_tokens)
                # WS sends initial "book" snapshots on subscribe — no REST seed needed.
                # The periodic _book_validation_loop syncs REST data every 5 min as safety net.
                print(f"✅ Book WebSocket connected, subscribed to {len(active_tokens)} tokens")
            else:
                print("✅ Book WebSocket connected, no tokens to subscribe yet")
            
            return True
            
        except Exception as e:
            print(f"⚠️ Book WebSocket error: {e} - falling back to polling")
            self.book_ws = None
            return False
    
    async def subscribe_to_token(self, token_id: str):
        """
        Subscribe to book updates for a new token.
        
        Also fetches REST snapshot to seed cache immediately,
        preventing missed initial state from WebSocket.
        """
        if self.book_ws:
            try:
                await self.book_ws.subscribe([token_id])
                # Seed from REST immediately after subscribing
                await self._seed_tokens_from_rest([token_id])
            except Exception as e:
                print(f"⚠️ Failed to subscribe to {token_id[:20]}...: {e}")
    
    async def _seed_tokens_from_rest(self, token_ids: list):
        """
        Fetch REST order books and seed the WebSocket cache with full L2 depth.
        
        This ensures we have initial state even if WS doesn't push it.
        """
        if not self.poly_client or not self.book_ws or not token_ids:
            return
        
        try:
            for token_id in token_ids:
                rest_book = await self.poly_client.get_order_book(token_id)
                if rest_book and (rest_book.get("bids") or rest_book.get("asks")):
                    await self.book_ws.seed_from_rest(token_id, rest_book)
        except Exception as e:
            # Non-fatal - WS updates will eventually populate cache
            print(f"   ⚠️ REST seed failed (non-fatal): {e}")
    
    async def disconnect(self):
        """Disconnect WebSocket."""
        if self.book_ws:
            try:
                await self.book_ws.disconnect()
            except Exception:
                pass
            self.book_ws = None
    
    def _on_book_update(self, token_id: str, price: LivePrice, watcher):
        """
        Callback when order book changes.
        
        Called synchronously from the WebSocket receive loop.
        
        REACTIVE: Delegates ALL trading decisions to ReactiveBookHandler.
        Also pushes bid updates to BotState for edge evaluation.
        """
        best_bid = price.best_bid or 0
        best_ask = price.best_ask
        
        # REACTIVE: Push bid update to BotState for edge evaluation
        # This triggers edge_lost events if fair_prob - bid drops below min_edge
        self.bot_state.update_bid(token_id, best_bid, best_ask)
        
        # Delegate ALL trading decisions to ReactiveBookHandler
        if self._reactive_handler:
            self._reactive_handler.on_book_update(token_id, price)
        else:
            # Legacy fallback: outbid-only callback
            if not watcher.bot_state.is_token_active(token_id):
                return
            order_info = watcher.bot_state.get_order_info(token_id)
            if not order_info:
                return
            our_price = order_info.get("price", 0)
            if our_price > 0 and best_bid > our_price + 0.005:
                self._on_outbid_callback(token_id, best_bid)
