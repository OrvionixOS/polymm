"""
Reactive Book Handler - Event-driven order management triggered by book updates.

Every WebSocket book change flows through a single decision tree:
1. Spread collapse → cancel order
2. Outbid → adjust +1¢ 
3. Price improvement → lower bid to 2nd-best + increment

Replaces the polling-based monitoring for these decisions.
Deadline checks and hedge seeking remain polling-based.
"""
import asyncio
import logging
import time
from typing import Optional, Set, Dict, Callable

from src.polymarket.book_websocket import LivePrice
from src.state.bot_state import get_bot_state


logger = logging.getLogger(__name__)


# Max age of the cached WS book before we refuse to act on price-improvement
# decisions (in seconds). Shorter than outbid's 1s cooldown because improvement
# is a lower-urgency action — cheaper to skip than to act on stale data.
PRICE_IMPROVE_MAX_BOOK_AGE_S = 15.0

# How long a "failed outbid adjustment" marker lives before we allow a retry
# at the same level. Without this, a failed attempt at 0.45 would be
# permanently remembered — even after the outbidder leaves for minutes and
# returns with a fresh context. With this, the marker ages out and we retry.
# Chosen > outbid cooldown (1s) so one transient CLOB hiccup doesn't lock us
# out, but short enough that persistent issues (balance, market closed)
# surface quickly.
OUTBID_FAIL_TTL_S = 30.0


class ReactiveBookHandler:
    """
    Event-driven order management triggered by book updates.
    
    Called from WebSocketHandler on every book/price_change event.
    Makes all order-level trading decisions reactively:
    - Spread collapse detection and cancellation
    - Outbid detection and response
    - Price improvement when we're best bid with a gap
    
    Delegates actual order execution to OrderMonitor methods.
    """
    
    def __init__(
        self,
        order_monitor,  # OrderMonitor — avoid circular import
        executor,       # OrderExecutor
        config: dict,
        spread_cancel_threshold: float = 0.10,
    ):
        self.order_monitor = order_monitor
        self.executor = executor
        self.config = config
        self.spread_cancel_threshold = spread_cancel_threshold
        
        # Per-token state
        self._adjusting_tokens: Set[str] = set()
        
        # Throttle: token_id → last action timestamp (monotonic)
        self._last_action_time: Dict[str, float] = {}
        self._outbid_cooldown = 1.0          # seconds between outbid adjustments per token
        self._improvement_cooldown = 5.0     # seconds between price improvements per token
        
        # Outbid tracking: prevent re-processing same bid level
        self._outbid_state: Dict[str, dict] = {}  # token_id → {"best_bid": float, "failed": bool}
        
        # Callbacks — set by the bot for spread-bot-specific cancellation logic
        self.on_spread_cancel: Optional[Callable] = None  # async fn(match_id, order, spread)
        
        # Watcher reference — set by connect()
        self._watcher = None
        self._hedge_token_ids: Set[str] = set()
        
        # Book WS reference for opposite-side lookups
        self._book_ws = None
    
    def set_watcher(self, watcher, hedge_token_ids: Set[str]):
        """Set the watcher and hedge token set (called at startup)."""
        self._watcher = watcher
        self._hedge_token_ids = hedge_token_ids
    
    def update_hedge_tokens(self, hedge_token_ids: Set[str]):
        """Update hedge token set (called after state resync)."""
        self._hedge_token_ids = hedge_token_ids
    
    def set_book_ws(self, book_ws):
        """Set the Book WebSocket reference for order book lookups."""
        self._book_ws = book_ws
    
    def on_book_update(self, token_id: str, price: LivePrice):
        """
        Main entry point — called synchronously from WS receive loop.
        
        Does lightweight state reads to decide what action to take,
        then spawns an async task for any API calls needed.
        """
        bot_state = get_bot_state()
        
        # Quick exit: not a token we have an order on
        if not bot_state.is_token_active(token_id):
            return
        
        # Get our order info from BotState
        order_info = bot_state.get_order_info(token_id)
        if not order_info:
            return
        
        our_price = order_info.get("price", 0)
        if our_price <= 0:
            return
        
        best_bid = price.best_bid or 0
        best_ask = price.best_ask
        
        # Skip if token is currently being adjusted
        if token_id in self._adjusting_tokens:
            return
        
        # Also skip if token is reserved (in-progress placement/replacement)
        if token_id in bot_state._reserved_tokens:
            return
        
        is_hedge = order_info.get("is_hedge", False) or token_id in self._hedge_token_ids
        
        # === DECISION TREE ===
        
        # 1. SPREAD COLLAPSE — cancel if bid-ask spread < threshold
        if not is_hedge and best_ask and best_bid > 0:
            spread = best_ask - best_bid
            if spread < self.spread_cancel_threshold:
                if self._can_act(token_id, "spread"):
                    # Lock token immediately to prevent duplicate cancel tasks
                    self._adjusting_tokens.add(token_id)
                    asyncio.create_task(
                        self._handle_spread_collapse(token_id, order_info, spread)
                    )
                return
        
        # 2. OUTBID — someone bid higher than us
        if best_bid > our_price + 0.005:
            # Check if we already tried this bid level and failed — but only
            # honor the marker if it's fresh. TTL-based expiry prevents a
            # transient CLOB failure from permanently locking us out of
            # retrying when the same outbidder returns minutes later.
            prev = self._outbid_state.get(token_id)
            if prev and prev.get("failed"):
                marker_age = time.monotonic() - prev.get("at", 0.0)
                if marker_age <= OUTBID_FAIL_TTL_S and abs(prev.get("best_bid", 0) - best_bid) < 0.01:
                    return  # Already tried at this level, within TTL
                if marker_age > OUTBID_FAIL_TTL_S:
                    # Expire the stale marker so future lookups see a clean slate.
                    self._outbid_state.pop(token_id, None)

            if self._can_act(token_id, "outbid"):
                # Lock token BEFORE spawning task to prevent concurrent actions
                self._adjusting_tokens.add(token_id)
                asyncio.create_task(
                    self._handle_outbid(token_id, order_info, our_price, best_bid, is_hedge)
                )
            return

        # We're best bid. Do NOT eagerly clear the outbid state here: doing so
        # races with in-flight _handle_outbid tasks that are about to write a
        # failed marker — a transient "best_bid pulled" tick between our
        # check and the handler's write would wipe the marker, and a returning
        # outbidder at the same level would trigger an immediate retry (instead
        # of respecting the failure). The TTL check above handles aging out
        # stale markers on its own; successful handlers clear their own state
        # at the end of _handle_outbid.
        
        # 3. PRICE IMPROVEMENT — we're best bid, check gap to 2nd
        if not is_hedge and abs(best_bid - our_price) < 0.005:
            # Freshness gate: don't make price-improvement decisions against a
            # stale cached book. If we haven't received a WS update for the
            # token recently, the full-L2 view may be several levels behind
            # reality (post-reconnect / after a snapshot gap).
            book_is_fresh = True
            if self._book_ws and hasattr(self._book_ws, "_last_update_per_token"):
                last_ts = self._book_ws._last_update_per_token.get(token_id)
                if last_ts is not None:
                    age = (time.time() - last_ts.timestamp()) if hasattr(last_ts, "timestamp") else float("inf")
                    if age > PRICE_IMPROVE_MAX_BOOK_AGE_S:
                        book_is_fresh = False
            if not book_is_fresh:
                return

            # Use FULL cached book (not incremental price.bids which may only have our level)
            bids = None
            if self._book_ws:
                ws_book = self._book_ws.get_book(token_id)
                if ws_book:
                    bids = ws_book.get("bids", [])
            if not bids:
                # Fallback to incremental is safe only if the price timestamp
                # is recent (checked above via _last_update_per_token).
                bids = price.bids
            if len(bids) >= 2:
                second_bid = float(bids[1].get("price", 0)) if isinstance(bids[1], dict) else float(bids[1])
                gap = our_price - second_bid
                if gap >= 0.03:  # Only improve if 3¢+ gap
                    if self._can_act(token_id, "improve"):
                        # Lock token BEFORE spawning task to prevent concurrent actions
                        self._adjusting_tokens.add(token_id)
                        asyncio.create_task(
                            self._handle_price_improvement(
                                token_id, order_info, our_price, second_bid, bids
                            )
                        )
            return
    
    # =========================================================================
    # Throttling
    # =========================================================================
    
    def _can_act(self, token_id: str, action_type: str) -> bool:
        """Check if enough time has passed since last action on this token."""
        now = time.monotonic()
        key = f"{token_id}:{action_type}"
        last = self._last_action_time.get(key, 0)
        
        cooldown = {
            "outbid": self._outbid_cooldown,
            "improve": self._improvement_cooldown,
            "spread": 2.0,  # 2s cooldown for spread cancels
        }.get(action_type, 1.0)
        
        if now - last < cooldown:
            return False
        
        self._last_action_time[key] = now
        return True
    
    def mark_adjustment_failed(self, token_id: str, best_bid: float):
        """Mark that an outbid adjustment at this level failed."""
        self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True, "at": time.monotonic()}
    
    def clear_outbid_state(self, token_id: str):
        """Clear outbid state for a token."""
        self._outbid_state.pop(token_id, None)

    def _order_still_current(self, token_id: str, order_info: dict) -> bool:
        """Re-validate that the order we were asked to act on still exists.

        TOCTOU guard for handlers spawned from on_book_update: between the
        synchronous snapshot taken in on_book_update and this handler being
        scheduled, another coroutine could have cancelled/replaced the order
        or added the token to the reserved set (in-progress placement). In
        either case we must NOT act on the stale snapshot — a cancel against
        a replaced order_id would either no-op or (worse) cancel the wrong
        follow-up order.
        """
        expected_id = order_info.get("order_id")
        if not expected_id:
            return False
        bot_state = get_bot_state()
        if not bot_state.is_token_active(token_id):
            return False
        if token_id in bot_state._reserved_tokens:
            return False
        current = bot_state.get_order_info(token_id)
        if not current:
            return False
        return current.get("order_id") == expected_id

    # =========================================================================
    # Action handlers (async — spawned from on_book_update)
    # =========================================================================

    async def _handle_spread_collapse(
        self, token_id: str, order_info: dict, spread: float
    ):
        """Cancel an order because the bid-ask spread collapsed."""
        try:
            if not self._order_still_current(token_id, order_info):
                return
            order_id = order_info.get("order_id", "")
            team_name = order_info.get("team_name", token_id[:12])
            
            # Use callback if set (spread bot needs to update _weather_orders)
            bot_state = get_bot_state()
            match = bot_state.get_match_by_token(token_id)
            
            if self.on_spread_cancel and match:
                await self.on_spread_cancel(match, order_info, spread)
            else:
                # Default: cancel directly
                try:
                    cancelled, was_complete = await self.executor.cancel_order(
                        order_id, force=True, team_name=team_name
                    )
                    if cancelled or was_complete:
                        print(f"📉 [REACTIVE] Spread collapsed to {spread*100:.1f}c — cancelled {team_name}")
                        bot_state.cancel_order(order_id)
                except Exception as e:
                    print(f"   ❌ [REACTIVE] Spread cancel failed: {e}")
        finally:
            self._adjusting_tokens.discard(token_id)
    
    async def _handle_outbid(
        self,
        token_id: str,
        order_info: dict,
        our_price: float,
        best_bid: float,
        is_hedge: bool,
    ):
        """Handle being outbid — delegate to OrderMonitor.

        Token is already locked in _adjusting_tokens by on_book_update.
        """
        try:
            if self._watcher is None:
                return
            if not self._order_still_current(token_id, order_info):
                return

            # Two bot types share this handler:
            #
            # SportsBot: fair_value is ALWAYS set from bookmaker odds.
            #   If it's None here, something is wrong — refuse to adjust.
            #
            # SpreadBot: fair_value is ALWAYS None (no external odds).
            #   Uses cost-based approach: fair = 1.0 - opposite_side_price.
            #   SpreadBot only cares that spread doesn't collapse.
            our_fair = order_info.get("fair_value")
            
            if our_fair is None:
                # No odds-based fair → try cost-based (SpreadBot path)
                bot_state = get_bot_state()
                match = bot_state.get_match_by_token(token_id)
                if match:
                    if match.token1 == token_id:
                        if match.position2:
                            our_fair = 1.0 - match.position2.avg_price
                        elif match.order2:
                            our_fair = 1.0 - match.order2.price
                    elif match.token2 == token_id:
                        if match.position1:
                            our_fair = 1.0 - match.position1.avg_price
                        elif match.order1:
                            our_fair = 1.0 - match.order1.price
            
            if our_fair is None:
                self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True, "at": time.monotonic()}
                return
            
            # Get L2 bids from WS cache
            bids = None
            if self._book_ws:
                ws_book = self._book_ws.get_book(token_id)
                if ws_book:
                    bids = ws_book.get("bids", [])
            
            stored_team = order_info.get("team_name", "")
            
            await self.order_monitor._handle_outbid(
                token_id=token_id,
                order_id=order_info.get("order_id", ""),
                order_info=order_info,
                our_price=our_price,
                our_fair=our_fair,
                best_bid=best_bid,
                stored_team=stored_team,
                is_hedge=is_hedge,
                watcher=self._watcher,
                poly_client=None,  # Not needed — bids provided
                bids=bids,
                book_ws=self._book_ws,
            )
            
            # If we get here without exception, clear outbid state
            if token_id in self._outbid_state:
                del self._outbid_state[token_id]

        except asyncio.CancelledError:
            self._adjusting_tokens.discard(token_id)
            raise
        except (ValueError, TypeError, KeyError) as e:
            logger.exception("[REACTIVE] Outbid handler data error on %s: %s", token_id[:16], e)
            print(f"   ❌ [REACTIVE] Outbid handler data error ({type(e).__name__}): {e}")
            self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True, "at": time.monotonic()}
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.exception("[REACTIVE] Outbid handler IO error on %s: %s", token_id[:16], e)
            print(f"   ❌ [REACTIVE] Outbid handler IO error ({type(e).__name__}): {e}")
            self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True, "at": time.monotonic()}
        except Exception as e:
            logger.exception("[REACTIVE] Outbid handler unexpected error on %s: %s", token_id[:16], e)
            print(f"   ❌ [REACTIVE] Outbid handler unexpected ({type(e).__name__}): {e}")
            self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True, "at": time.monotonic()}
        finally:
            self._adjusting_tokens.discard(token_id)
    
    async def _handle_price_improvement(
        self,
        token_id: str,
        order_info: dict,
        our_price: float,
        second_bid: float,
        bids: list,
    ):
        """Try to improve our price when we're best bid with a gap.

        Token is already locked in _adjusting_tokens by on_book_update.
        """
        try:
            if not self._order_still_current(token_id, order_info):
                return
            # Same bot-type logic as _handle_outbid:
            # SportsBot → fair_value from odds. SpreadBot → cost-based from opposite side.
            our_fair = order_info.get("fair_value")
            
            if our_fair is None:
                bot_state = get_bot_state()
                match = bot_state.get_match_by_token(token_id)
                if match:
                    if match.token1 == token_id:
                        if match.position2:
                            our_fair = 1.0 - match.position2.avg_price
                        elif match.order2:
                            our_fair = 1.0 - match.order2.price
                    elif match.token2 == token_id:
                        if match.position1:
                            our_fair = 1.0 - match.position1.avg_price
                        elif match.order1:
                            our_fair = 1.0 - match.order1.price
            
            if our_fair is None:
                return
            
            current_edge = our_fair - our_price
            
            # Delegate to existing OrderMonitor method (has all the edge/profit checks)
            await self.order_monitor._try_price_improvement(
                token_id=token_id,
                order_id=order_info.get("order_id", ""),
                order_info=order_info,
                our_price=our_price,
                our_fair=our_fair,
                best_bid=our_price,  # We are the best bid
                bids=[{"price": str(our_price), "size": "0"}] + [
                    b if isinstance(b, dict) else {"price": str(b), "size": "0"} for b in bids[1:]
                ],
                current_edge=current_edge,
                watcher=self._watcher,
            )
        finally:
            self._adjusting_tokens.discard(token_id)
