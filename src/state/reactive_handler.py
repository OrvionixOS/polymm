"""
Reactive Handler - Evaluates state changes and takes action.

This is the "brain" that reacts to state changes:
- When fair_probs change → check if orders are still above min_edge
- When bids change → check for outbids, hedge adjustments
- When orders fill → check for natural arbs, place hedges
- When matches go live → cancel stale orders

The ReactiveHandler registers callbacks with BotState and contains
the business logic to decide WHAT to do when state changes.
"""
import asyncio
from typing import Optional, Callable, Any, TYPE_CHECKING
from datetime import datetime, timezone

# Hedge orders with edge worse than this get cancelled
# Entry orders use min_edge (usually 7%), hedges use a looser threshold
HEDGE_CANCEL_EDGE = -0.01  # -1%

from src.state.bot_state import (
    get_bot_state, BotState, StateEvent, StateEventType,
    MatchState, MatchOrder
)

if TYPE_CHECKING:
    from src.execution.order_executor import OrderExecutor


class ReactiveHandler:
    """
    Handles reactive responses to state changes.
    
    Registers callbacks with BotState and takes action when needed:
    - Cancel orders that lost edge
    - Adjust hedges that are overpaying
    - Skip placing hedges for natural arbs
    - Cancel orders when matches go live
    """
    
    def __init__(
        self,
        min_edge: float = 0.05,
        min_arb_profit: float = 0.05,
        stale_threshold: float = 300.0,  # 5 minutes default
        executor: Optional['OrderExecutor'] = None,
    ):
        self.min_edge = min_edge
        self.min_arb_profit = min_arb_profit
        self.stale_threshold = stale_threshold
        self.executor = executor
        self.bot_state = get_bot_state()
        
        # Configure BotState with our thresholds
        self.bot_state.min_edge = min_edge
        self.bot_state.min_arb_profit = min_arb_profit
        
        # External action handlers (set by main.py)
        self.on_cancel_order: Optional[Callable[[str, str], Any]] = None  # (order_id, reason) -> None
        self.on_skip_hedge: Optional[Callable[[str, str], Any]] = None  # (token_id, reason) -> None
        
        # NOTE: on_adjust_order was removed - hedge adjustments are handled by OrderMonitor
        # which properly preserves order size and does atomic cancel+replace
        
        # Stats
        self._stats = {
            "edge_lost_cancels": 0,
            "natural_arbs_skipped": 0,
            "live_cancels": 0,
            "stale_cancels": 0,
        }
        
        self._running = False
    
    def start(self):
        """Register callbacks with BotState."""
        if self._running:
            return
        
        self._running = True
        
        # Register async callbacks for events that need I/O
        self.bot_state.on_async(StateEventType.EDGE_LOST, self._handle_edge_lost)
        self.bot_state.on_async(StateEventType.MATCH_GONE_LIVE, self._handle_match_live)
        
        # Register sync callbacks for detection/logging
        self.bot_state.on(StateEventType.ORDER_FILLED, self._handle_order_filled)
        self.bot_state.on(StateEventType.FAIR_PROBS_UPDATED, self._handle_fair_probs_updated)
        self.bot_state.on(StateEventType.BID_UPDATED, self._handle_bid_updated)
        
        print("🔄 ReactiveHandler started")
    
    def stop(self):
        """Stop the reactive handler."""
        self._running = False
        print("🛑 ReactiveHandler stopped")
    
    # ===== Async Event Handlers (take action) =====
    
    async def _handle_edge_lost(self, event: StateEvent):
        """
        Handle edge lost event - cancel orders that lost edge.
        
        Entry orders: cancelled when edge < min_edge (7%)
        Hedge orders: cancelled when edge < -1% (severely overpaying)
        """
        if not self._running:
            return
        
        order = event.data
        if not isinstance(order, MatchOrder):
            return
        
        # Skip if order is already closed
        if not order.is_open:
            return
        
        edge = event.new_edge if event.new_edge is not None else 0
        
        # For hedge orders, only cancel when edge is severely negative (< -1%)
        # Moderate edge loss on hedges is OK — they protect positions
        if not order.is_entry:
            if edge >= HEDGE_CANCEL_EDGE:
                return  # Hedge edge is acceptable, skip
        
        edge_pct = edge * 100
        is_hedge = not order.is_entry
        label = "Hedge overpaying" if is_hedge else "Edge dropped to"
        reason = f"{label} {edge_pct:+.1f}%"
        
        # Add match context for Over/Under so we know which game
        match = event.match_state
        match_label = ""
        if match and match.team1 and match.team2:
            match_label = f" | {match.team1} vs {match.team2}"
        
        hedge_prefix = "🔒 " if is_hedge else ""
        print(f"⚡ [REACTIVE] {hedge_prefix}Edge lost: {order.team} @ {order.price:.2f} ({reason}){match_label}")
        
        if self.on_cancel_order:
            try:
                await self._call_async(self.on_cancel_order, order.order_id, reason)
                self._stats["edge_lost_cancels"] += 1
            except Exception as e:
                print(f"⚠️ ReactiveHandler cancel error: {e}")
    
    async def _handle_match_live(self, event: StateEvent):
        """
        Handle match going live - cancel stale orders.
        
        When a match goes live, our pre-match odds are stale and
        we should cancel any open orders.
        """
        if not self._running:
            return
        
        match = event.match_state
        
        for order in [match.order1, match.order2]:
            if not order or not order.is_open:
                continue
            
            reason = "Match went LIVE - odds stale"
            print(f"⚡ [REACTIVE] Live cancel: {order.team} @ {order.price:.2f}")
            
            if self.on_cancel_order:
                try:
                    await self._call_async(self.on_cancel_order, order.order_id, reason)
                    self._stats["live_cancels"] += 1
                except Exception as e:
                    print(f"⚠️ ReactiveHandler live cancel error: {e}")
    
    # ===== Sync Event Handlers (detection/logging) =====
    
    def _handle_order_filled(self, event: StateEvent):
        """
        Handle order fill - check for natural arbs.
        
        When an order fills, check if we already have coverage on
        the opposite side (natural arb) before placing a hedge.
        """
        if not self._running:
            return
        
        order = event.data
        if not isinstance(order, MatchOrder):
            return
        
        match = event.match_state
        
        # Check for natural arb
        if match.has_natural_arb:
            opposite_team = match.team2 if order.token_id == match.token1 else match.team1
            print(f"🎯 [REACTIVE] Natural arb: {order.team} filled, already have {opposite_team}")
            
            self._stats["natural_arbs_skipped"] += 1
            
            if self.on_skip_hedge:
                try:
                    self.on_skip_hedge(order.token_id, f"Natural arb with {opposite_team}")
                except Exception as e:
                    print(f"⚠️ ReactiveHandler skip hedge error: {e}")
    
    def _handle_fair_probs_updated(self, event: StateEvent):
        """
        Handle fair probability update.
        
        The actual edge checking is done in BotState.update_fair_probs()
        which emits EDGE_LOST events. This handler is for logging/debugging.
        """
        # Could add logging here if needed
        pass
    
    def _handle_bid_updated(self, event: StateEvent):
        """
        Handle bid update.
        
        The actual edge checking is done in BotState.update_bid()
        which emits EDGE_LOST events. This handler is for logging/debugging.
        """
        # Could add logging here if needed
        pass
    
    # ===== Periodic Checks (for anything missed) =====
    
    async def check_all_orders(self, filter_tokens: set = None) -> int:
        """
        Check all open orders for edge loss.
        
        This is a safety net that catches any edge losses that might
        have been missed due to timing or race conditions.
        
        Entry orders: cancelled when edge < min_edge (7%)
        Hedge orders: cancelled when edge < -1% (severely overpaying)
        
        Args:
            filter_tokens: If set, only check orders with tokens in this set (for live-only mode)
        
        Returns number of orders that need action.
        """
        # Include hedges so we can cancel severely negative ones
        orders_to_cancel = self.bot_state.get_orders_below_min_edge(include_hedges=True)
        
        action_count = 0
        
        for match, order, edge in orders_to_cancel:
            # Re-check if order is still open (may have been cancelled by reactive event)
            if not order.is_open:
                continue
            
            is_hedge = match.is_order_hedge(order)
            
            # For hedge orders, only cancel when edge is severely negative (< -1%)
            if is_hedge and edge >= HEDGE_CANCEL_EDGE:
                continue
            
            # Skip if token not in filter set (for LiveBot live-only mode)
            if filter_tokens is not None and order.token_id not in filter_tokens:
                continue
            
            # SKIP orders without fair probs - they'll get fair probs on next OddsService refresh
            # Don't cancel prematurely; only cancel when we KNOW edge is bad
            if edge <= -990:
                # Fair probs missing - wait for OddsService to sync
                continue
            
            edge_pct = edge * 100
            match_label = ""
            if match.team1 and match.team2:
                match_label = f" | {match.team1} vs {match.team2}"
            hedge_label = "🔒 " if is_hedge else ""
            reason = f"{hedge_label}Edge at {edge_pct:+.1f}% (periodic check){match_label}"
            
            print(f"⚡ [REACTIVE] Cancelling {order.team} @ {order.price:.2f}: {reason}")
            
            if self.on_cancel_order:
                try:
                    await self._call_async(self.on_cancel_order, order.order_id, reason)
                    action_count += 1
                except Exception as e:
                    print(f"⚠️ Periodic check cancel error: {e}")
        
        # NOTE: Hedge adjustments (hedges_to_adjust) are intentionally NOT handled here.
        # OrderMonitor handles hedge price adjustments with proper size preservation
        # and atomic cancel+replace. If we adjusted here, we'd create race conditions
        # with HedgeSeeker which would see "no order" and place a duplicate.
        
        # ===== STALENESS CHECK =====
        # Cancel entry orders where odds haven't been updated in too long
        stale_orders = self.bot_state.get_stale_order_matches(self.stale_threshold)
        
        for match, order, odds_age in stale_orders:
            # Re-check if order is still open
            if not order.is_open:
                continue
            
            # Skip if token not in filter set (for LiveBot live-only mode)
            if filter_tokens is not None and order.token_id not in filter_tokens:
                continue
            
            age_str = f"{int(odds_age)}s" if odds_age < 60 else f"{int(odds_age/60)}m"
            reason = f"Odds stale ({age_str})"
            
            print(f"⚡ [REACTIVE] Stale odds: {order.team} @ {order.price:.2f} ({reason})")
            
            if self.on_cancel_order:
                try:
                    await self._call_async(self.on_cancel_order, order.order_id, reason)
                    self._stats["stale_cancels"] += 1
                    action_count += 1
                except Exception as e:
                    print(f"⚠️ Stale check cancel error: {e}")
        
        return action_count
    
    # ===== Helpers =====
    
    async def _call_async(self, func: Callable, *args):
        """Call a function, awaiting if it's async."""
        result = func(*args)
        if asyncio.iscoroutine(result):
            return await result
        return result
    
    def get_stats(self) -> dict:
        """Get handler statistics."""
        return {
            **self._stats,
            "running": self._running,
        }


# Global singleton
_reactive_handler: Optional[ReactiveHandler] = None


def get_reactive_handler(
    min_edge: float = 0.05,
    min_arb_profit: float = 0.05,
    stale_threshold: float = 300.0,
) -> ReactiveHandler:
    """Get or create global reactive handler instance."""
    global _reactive_handler
    if _reactive_handler is None:
        _reactive_handler = ReactiveHandler(
            min_edge=min_edge,
            min_arb_profit=min_arb_profit,
            stale_threshold=stale_threshold,
        )
    return _reactive_handler


def reset_reactive_handler():
    """Reset the global reactive handler (for testing)."""
    global _reactive_handler
    if _reactive_handler:
        _reactive_handler.stop()
    _reactive_handler = None
