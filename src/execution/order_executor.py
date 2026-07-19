"""
Order Executor - places orders on Polymarket using py-clob-client.

Handles order placement, cancellation, and tracking for esports arbitrage.
"""
import os
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict
from enum import Enum
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class OrderSide(Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    """Order status."""
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class Order:
    """Represents an order on Polymarket."""
    order_id: str
    market_id: str  # Token ID
    side: OrderSide
    price: float  # 0-1
    size: float  # USDC amount
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    error_message: str = ""
    
    @property
    def is_complete(self) -> bool:
        """Check if order is in a terminal state."""
        return self.status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.FAILED]
    
    @property
    def remaining_size(self) -> float:
        """Get unfilled size."""
        return self.size - self.filled_size
    
    def __str__(self) -> str:
        return (
            f"Order({self.order_id[:8]}... {self.side.value} {self.size:.2f} @ {self.price:.3f} "
            f"[{self.status.value}])"
        )


class OrderExecutor:
    """
    Executes orders on Polymarket using py-clob-client.
    
    NOTE: py-clob-client uses synchronous `requests` internally.
    All blocking calls are wrapped in run_in_executor to avoid
    blocking the asyncio event loop (enables true parallel execution).
    
    Handles:
    - Order placement (limit orders)
    - Order cancellation
    - Order status tracking
    - API key/signature management
    
    Usage:
        executor = OrderExecutor()
        order = await executor.place_limit_order(
            token_id="0x...",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0
        )
    """
    
    # Polymarket CLOB endpoints
    CLOB_HOST = "https://clob.polymarket.com"
    CHAIN_ID = 137  # Polygon mainnet
    
    def __init__(
        self,
        private_key: Optional[str] = None,
        paper_trading: bool = True,
        funder_address: Optional[str] = None,
    ):
        """
        Initialize the order executor.
        
        Args:
            private_key: Ethereum private key for signing (from env if not provided)
            paper_trading: If True, simulate orders instead of placing real ones
            funder_address: Funder address override (for separate accounts like LiveBot)
        """
        self.private_key = private_key or os.getenv("POLYMARKET_PRIVATE_KEY")
        self.paper_trading = paper_trading
        self._funder_address = funder_address  # Store for use in _get_client
        self._client = None
        self._order_counter = 0
        self._orders: Dict[str, Order] = {}
        
        # Global rate limiter: cap concurrent CLOB API calls to avoid 429s
        # Each cancel+place is 2-3 calls; with sem=3 we get ~10 req/s max
        import asyncio
        self._api_semaphore = asyncio.Semaphore(3)
        
        # Cache of order IDs known to be dead (already cancelled/matched on exchange)
        # Prevents wasted API calls when adjust_order tries to cancel stale orders
        self._dead_order_ids: set = set()
        
        if not self.paper_trading and not self.private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY required for live trading")
    
    def _get_client(self):
        """Get or create the CLOB client."""
        if self._client is None and not self.paper_trading:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds
                
                # Patch httpx timeout: py-clob-client defaults to 5s which causes
                # false "Request exception!" errors under load. Orders succeed server-side
                # but the client times out before receiving the response.
                # See: https://github.com/Polymarket/py-clob-client/issues/273
                import httpx
                from py_clob_client.http_helpers import helpers
                helpers._http_client = httpx.Client(http2=True, timeout=30.0)
                
                # Get funder address - use override if provided, else env var
                funder = self._funder_address or os.getenv("POLYMARKET_FUNDER_ADDRESS")
                
                # Signature type:
                # 0 = EOA (MetaMask direct)
                # 1 = Email/Magic wallet
                # 2 = Browser wallet proxy
                sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))
                
                # Create client with funder address
                self._client = ClobClient(
                    host=self.CLOB_HOST,
                    key=self.private_key,
                    chain_id=self.CHAIN_ID,
                    signature_type=sig_type,
                    funder=funder,
                )
                
                # For EOA mode (sig_type=0), always derive credentials
                # Website API keys are tied to proxy wallet sessions
                if sig_type == 0:
                    creds = self._client.create_or_derive_api_creds()
                    self._client.set_api_creds(creds)
                    print("   Using derived API credentials (EOA mode)")
                else:
                    # Try to use website API credentials if provided (proxy mode)
                    api_key = os.getenv("POLYMARKET_API_KEY")
                    api_secret = os.getenv("POLYMARKET_API_SECRET")
                    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
                    
                    if api_key and api_secret and api_passphrase:
                        creds = ApiCreds(
                            api_key=api_key,
                            api_secret=api_secret,
                            api_passphrase=api_passphrase,
                        )
                        self._client.set_api_creds(creds)
                        print("   Using website API credentials")
                    else:
                        creds = self._client.create_or_derive_api_creds()
                        self._client.set_api_creds(creds)
                        print("   Using derived API credentials")
                
            except ImportError:
                raise ImportError(
                    "py-clob-client not installed. Run: pip install py-clob-client"
                )
        return self._client
    
    def _generate_order_id(self) -> str:
        """Generate a unique order ID."""
        self._order_counter += 1
        if self.paper_trading:
            return f"PAPER-{self._order_counter:06d}"
        return f"LIVE-{self._order_counter:06d}"
    
    async def place_limit_order(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
        team_name: str = "",
    ) -> Order:
        """
        Place a limit order on Polymarket.
        
        Args:
            token_id: The token to trade
            side: BUY or SELL
            price: Limit price (0.01-0.99)
            size: Number of shares
            team_name: Team name for logging (optional)
            
        Returns:
            Order object with status
        """
        order_id = self._generate_order_id()
        
        order = Order(
            order_id=order_id,
            market_id=token_id,
            side=side,
            price=price,
            size=size,
        )
        
        team_display = f" ({team_name})" if team_name else ""
        
        if self.paper_trading:
            # Simulate order fill
            order.status = OrderStatus.FILLED
            order.filled_size = size
            order.avg_fill_price = price
            order.updated_at = datetime.utcnow()
            print(f"📝 [PAPER] Placed{team_display}: {side.name} {size:.2f} @ {price:.2f}")
        else:
            # Real order placement with SAFE retry.
            # Key insight: create_order (signing) produces a unique nonce.
            # Retrying post_order with the SAME signed order is safe — Polymarket
            # rejects duplicate nonces, so no ghost duplicate orders.
            # The OLD bug was: create_order + post_order BOTH inside retry loop,
            # which generated a NEW nonce each retry → duplicate orders.
            import asyncio
            from py_clob_client.clob_types import OrderArgs
            
            client = self._get_client()
            max_retries = 2
            
            try:
                # Step 1: Build and sign the order ONCE (local, no network)
                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side="BUY" if side == OrderSide.BUY else "SELL",
                )
                
                loop = asyncio.get_event_loop()
                async with self._api_semaphore:
                    signed_order = await loop.run_in_executor(None, client.create_order, order_args)
                
                # Step 2: Post the signed order with retry (same nonce = safe)
                last_error = None
                for attempt in range(max_retries + 1):
                    try:
                        async with self._api_semaphore:
                            result = await loop.run_in_executor(None, client.post_order, signed_order)
                        
                        order.order_id = result.get("orderID", order_id)
                        order.status = OrderStatus.OPEN
                        order.updated_at = datetime.utcnow()
                        last_error = None
                        break  # Success
                        
                    except Exception as e:
                        error_str = str(e).lower()
                        
                        # "Duplicated" means the first attempt DID succeed — treat as success.
                        # Extract order ID from error: "order 0x... is invalid. Duplicated."
                        if "duplicated" in error_str:
                            import re
                            oid_match = re.search(r'order (0x[a-f0-9]+)', error_str)
                            if oid_match:
                                order.order_id = oid_match.group(1)
                            order.status = OrderStatus.OPEN
                            order.updated_at = datetime.utcnow()
                            last_error = None
                            break
                        
                        last_error = e
                        is_balance_error = "not enough balance" in error_str
                        retryable = (
                            is_balance_error or
                            "request exception" in error_str or
                            "connection" in error_str or
                            "timeout" in error_str or
                            "service not ready" in error_str or
                            "500" in str(e) or
                            "425" in str(e) or
                            "429" in str(e)
                        )
                        if attempt < max_retries and retryable:
                            # Balance errors need longer delay for concurrent orders to settle
                            delay = 3.0 if is_balance_error else 1.0 * (attempt + 1)
                            await asyncio.sleep(delay)
                            continue
                        break
                
                if last_error:
                    order.status = OrderStatus.FAILED
                    order.error_message = str(last_error)
                    print(f"❌ Order failed: {last_error}")
                    
            except Exception as e:
                order.status = OrderStatus.FAILED
                order.error_message = str(e)
                print(f"❌ Order failed: {e}")
        
        self._orders[order.order_id] = order
        return order
    
    async def cancel_order(self, order_id: str, force: bool = False, team_name: str = "") -> tuple[bool, bool]:
        """
        Cancel an open order.
        
        Args:
            order_id: The order ID to cancel
            force: If True, try to cancel on exchange even if not in local tracking
            team_name: Team name for logging (optional)
            
        Returns:
            Tuple of (success, was_already_complete):
            - (True, False): Successfully cancelled an active order
            - (True, True): Order was already complete (no action needed)
            - (False, False): Cancel failed
        """
        order = self._orders.get(order_id)
        team_display = f" ({team_name})" if team_name else ""
        
        # If not in local tracking and not forcing, fail
        if not order and not force:
            print(f"⚠️ Order {order_id} not found in local tracking")
            return False, False
        
        if order and order.is_complete:
            self._dead_order_ids.add(order_id)
            return True, True  # Success=True (cleanup OK), but was_already_complete=True
        
        # Skip API call for orders we already know are dead from previous cycles
        if order_id in self._dead_order_ids:
            return True, True
        
        if self.paper_trading:
            if order:
                order.status = OrderStatus.CANCELLED
                order.updated_at = datetime.utcnow()
            print(f"📝 [PAPER] Cancelled{team_display}")
            return True, False
        else:
            max_retries = 2  # 0, 1, 2 = 3 total attempts
            for cancel_attempt in range(max_retries + 1):
                try:
                    client = self._get_client()
                    # Offload to thread pool — py-clob-client uses synchronous requests
                    # Semaphore prevents rate limiting across all concurrent callers
                    loop = asyncio.get_event_loop()
                    async with self._api_semaphore:
                        result = await loop.run_in_executor(None, client.cancel, order_id)
                    
                    # CRITICAL: Actually check the API response!
                    # The API returns {'canceled': [...], 'not_canceled': {...}}
                    not_canceled = result.get("not_canceled", {}) if isinstance(result, dict) else {}
                    canceled = result.get("canceled", []) if isinstance(result, dict) else []
                    
                    # Check if our order is in the not_canceled dict
                    if order_id in not_canceled:
                        reason = not_canceled[order_id]
                        # print(f"⚠️ Order {order_id[:12]}... not cancelled: {reason}{team_display}") # LEAVE COMMENTED OUT!
                        if order:
                            order.status = OrderStatus.CANCELLED  # Mark as done to prevent retries
                            order.updated_at = datetime.utcnow()
                        # Cache as dead to skip future cancel attempts
                        self._dead_order_ids.add(order_id)
                        return True, True
                    
                    # Order was successfully cancelled (either in canceled list or empty not_canceled)
                    if order:
                        order.status = OrderStatus.CANCELLED
                        order.updated_at = datetime.utcnow()
                    return True, False
                except Exception as e:
                    error_str = str(e).lower()
                    # If the API says order is already cancelled/complete, treat as success (no retry)
                    if "not found" in error_str or "already" in error_str or "cancelled" in error_str or "canceled" in error_str:
                        if order:
                            order.status = OrderStatus.CANCELLED
                            order.updated_at = datetime.utcnow()
                        self._dead_order_ids.add(order_id)
                        return True, True  # Success=True but was_already_complete=True
                    # Network/transient error — retry with delay
                    if cancel_attempt < max_retries:
                        await asyncio.sleep(1.0)
                        continue
                    # All retries exhausted — give up
                    print(f"❌ Cancel failed after {max_retries + 1} attempts: {e}")
                    return False, False
    
    async def cancel_all_orders(self) -> int:
        """Cancel all open orders."""
        cancelled = 0
        for order_id, order in list(self._orders.items()):
            if not order.is_complete:
                success, _ = await self.cancel_order(order_id)
                if success:
                    cancelled += 1
        return cancelled
    
    async def get_order_status(self, order_id: str) -> Optional[Order]:
        """
        Get current status of an order.
        
        Args:
            order_id: The order ID
            
        Returns:
            Order object or None
        """
        order = self._orders.get(order_id)
        if not order:
            return None
        
        if not self.paper_trading and not order.is_complete:
            # Fetch latest status from API with retry
            for attempt in range(3):
                try:
                    client = self._get_client()
                    result = client.get_order(order_id)
                    
                    if result:
                        status_map = {
                            "live": OrderStatus.OPEN,
                            "filled": OrderStatus.FILLED,
                            "cancelled": OrderStatus.CANCELLED,
                        }
                        order.status = status_map.get(result.get("status", ""), order.status)
                        order.filled_size = float(result.get("size_matched", 0))
                        order.updated_at = datetime.utcnow()
                    break  # Success
                        
                except Exception as e:
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    print(f"⚠️ Could not fetch order status: {e}")
        
        return order
    
    def get_all_orders(self) -> List[Order]:
        """Get all orders."""
        return list(self._orders.values())
    
    def get_open_orders(self) -> List[Order]:
        """Get all open (unfilled) orders."""
        return [o for o in self._orders.values() if not o.is_complete]
    
    def get_filled_orders(self) -> List[Order]:
        """Get all filled orders."""
        return [o for o in self._orders.values() if o.status == OrderStatus.FILLED]
    
    async def get_orders_for_token(self, token_id: str) -> List[dict]:
        """
        Query the CLOB API for all open orders on a specific token.
        
        Uses asset_id filter so only orders for THIS token are returned,
        avoiding the cost of fetching all 6800+ orders.
        
        Returns:
            List of order dicts with keys: order_id, price, size, status
            Sorted by price descending (best bid first).
            Empty list on any error.
        """
        from py_clob_client.clob_types import OpenOrderParams
        
        for attempt in range(3):
            try:
                client = self._get_client()
                
                # Run synchronous CLOB call in executor to avoid blocking event loop
                loop = asyncio.get_event_loop()
                orders = await loop.run_in_executor(
                    None,
                    lambda: client.get_orders(OpenOrderParams(asset_id=token_id)),
                )
                
                if not orders:
                    return []
                
                # Filter to LIVE orders only and normalize format
                result = []
                for o in orders:
                    if isinstance(o, dict):
                        status = o.get("status", "").upper()
                        if status in ("LIVE", "OPEN", "PENDING"):
                            result.append({
                                "order_id": o.get("id") or o.get("order_id", ""),
                                "price": float(o.get("price", 0)),
                                "size": float(o.get("original_size", o.get("size", 0))),
                                "status": status,
                            })
                
                # Sort by price descending (best bid first)
                result.sort(key=lambda x: x["price"], reverse=True)
                return result
                
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                import logging
                logging.debug(f"get_orders_for_token({token_id[:16]}...) failed: {e}")
                return []


async def main():
    """Demo the order executor in paper trading mode."""
    print("=" * 60)
    print("🎯 Order Executor - Paper Trading Demo")
    print("=" * 60)
    
    executor = OrderExecutor(paper_trading=True)
    
    # Simulate placing some orders
    mock_token_id = "0x1234567890abcdef1234567890abcdef12345678"
    
    # Buy order
    order1 = await executor.place_limit_order(
        token_id=mock_token_id,
        side=OrderSide.BUY,
        price=0.45,
        size=50.0,
    )
    
    # Sell order
    order2 = await executor.place_limit_order(
        token_id=mock_token_id,
        side=OrderSide.SELL,
        price=0.55,
        size=30.0,
    )
    
    print(f"\n📊 Orders placed: {len(executor.get_all_orders())}")
    print(f"   Filled: {len(executor.get_filled_orders())}")
    print(f"   Open: {len(executor.get_open_orders())}")
    
    for order in executor.get_all_orders():
        print(f"\n   {order}")
        print(f"      Filled: {order.filled_size}/{order.size} @ {order.avg_fill_price:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
