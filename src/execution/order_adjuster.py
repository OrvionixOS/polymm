"""
Order adjuster - processes pending order adjustments from WebSocket updates.
"""
import asyncio
from typing import Dict, Set

from src.polymarket.market_client import PolymarketEsportsClient
from src.execution.order_executor import OrderExecutor
from src.execution.hedge_finder import HedgeFinder
from src.monitoring.order_monitor import OrderMonitor


class OrderAdjuster:
    """
    Processes pending order adjustments detected via WebSocket.
    
    Maintains a queue of adjustments and processes them in batches.
    
    Requires injection of:
    - executor: OrderExecutor instance
    - hedge_finder: HedgeFinder instance
    - order_monitor: OrderMonitor instance (for fast adjustments)
    - config: CONFIG dict
    """
    
    def __init__(
        self,
        executor: OrderExecutor,
        hedge_finder: HedgeFinder,
        order_monitor: OrderMonitor,
        config: dict,
    ):
        self.executor = executor
        self.hedge_finder = hedge_finder
        self.order_monitor = order_monitor
        self.config = config
        
        # State tracking
        self._pending_adjustments: Dict[str, float] = {}  # token_id -> best_bid
        self._outbid_state: Dict[str, dict] = {}  # token_id -> {"best_bid": float, "failed": bool}
    
    def queue_adjustment(self, token_id: str, best_bid: float):
        """Queue an adjustment for processing."""
        self._pending_adjustments[token_id] = best_bid
    
    def has_pending_adjustments(self) -> bool:
        """Check if there are pending adjustments."""
        return len(self._pending_adjustments) > 0
    
    async def process_pending_adjustments(
        self,
        watcher,  # OrderWatcher - avoid circular import
        hedge_token_ids: Set[str],
        filter_tokens: Set[str] = None,  # If set, only process tokens in this set
        book_ws=None,  # Optional WS client for cached order books
    ):
        """Process outbid adjustments detected via WebSocket (parallel)."""
        # Take a snapshot and clear pending
        adjustments = self._pending_adjustments.copy()
        self._pending_adjustments.clear()
        
        if not adjustments:
            return
        
        # Filter if requested (LiveBot uses this for live-only)
        if filter_tokens is not None:
            adjustments = {
                token_id: bid for token_id, bid in adjustments.items()
                if token_id in filter_tokens
            }
            if not adjustments:
                return
        
        # Process all adjustments in parallel (capped at 10 concurrent CLOB API calls)
        # Safety: _adjusting_tokens lock inside adjust_order_fast prevents
        # concurrent modifications to the SAME token
        sem = asyncio.Semaphore(10)
        
        async def _adjust(token_id: str, best_bid: float, poly_client):
            async with sem:
                try:
                    await self.order_monitor.adjust_order_fast(
                        token_id=token_id,
                        best_bid=best_bid,
                        poly_client=poly_client,
                        watcher=watcher,
                        hedge_token_ids=hedge_token_ids,
                        book_ws=book_ws,
                    )
                except Exception as e:
                    print(f"❌ Failed to adjust {token_id[:20]}...: {e}")
        
        async with PolymarketEsportsClient() as poly_client:
            await asyncio.gather(
                *(_adjust(tid, bid, poly_client) for tid, bid in adjustments.items()),
                return_exceptions=True,
            )
