"""
Unified Order Monitor - Monitors all open orders (session orders AND hydrated orders).

Handles:
- Outbid detection and response
- Fair value changes and edge validation
- Hedge order management (fair value cap, profitability checks)
- Price improvement when we're the top bid
- BotState integration for match-level awareness

This is the single source of truth for order monitoring logic, replacing
the separate EntryMonitor and HydratedMonitor.
"""
import logging
from typing import Set, Optional, Dict, Any, Callable, Awaitable

from src.state.market_cache import MarketCache
from src.services.odds_service import OddsService
from src.execution.hedge_finder import HedgeFinder
from src.execution.order_executor import OrderExecutor, OrderSide, OrderStatus
from src.execution.order_watcher import Position, WatchedOrder
from src.scanning.team_matcher import normalize_team_name, get_fair_value_for_match
from src.core.match_id import normalize_team, is_will_win_market, extract_will_win_team
from src.data.recorder import get_recorder
from src.state.bot_state import get_bot_state
from src.monitoring.hedge_monitor import HedgeMonitorMixin


logger = logging.getLogger(__name__)

# How many fallback hits on the same token before we escalate log severity.
# The fallback path is unsafe for sub-markets (spread/totals/Yes-No) because
# it looks up h2h odds only. A single hit is usually benign (OddsService just
# hasn't pushed yet), but repeated hits mean canonical fair-value delivery is
# genuinely stuck and the position could be mispriced.
FALLBACK_FAIR_LOOKUP_ESCALATE_HITS = 3


class OrderMonitor(HedgeMonitorMixin):
    """
    Unified monitor for all open orders.
    
    Monitors:
    - Session orders (created this session, have full Position context)
    - Hydrated orders (from previous sessions, loaded at startup)
    
    Features:
    - Outbid detection and response
    - Fair value tracking and edge validation
    - Hedge-specific logic (never overpay, fair value cap)
    - Price improvement (lower bid when we're top and there's a gap)
    - Cache-aware to skip unchanged state
    
    Requires injection of:
    - cache: MarketCache instance
    - odds_service: OddsService instance
    - hedge_finder: HedgeFinder instance
    - executor: OrderExecutor instance
    - config: CONFIG dict
    """
    
    def __init__(
        self,
        cache: MarketCache,
        odds_service: OddsService,
        hedge_finder: HedgeFinder,
        executor: OrderExecutor,
        config: dict,
    ):
        self.cache = cache
        self.odds_service = odds_service
        self.hedge_finder = hedge_finder
        self.executor = executor
        self.config = config
        
        # State tracking
        self._outbid_state: dict = {}  # token_id -> {"best_bid": float, "failed": bool}
        self._warned_team_mismatch: set = set()  # Prevent duplicate warnings
        self._ws_edge_logged: set = set()  # Deduplicate edge-too-high logs
        self._adjusting_tokens: set = set()  # Tokens currently being adjusted (prevent duplicates)
        self._hedge_fair_cap_warned: set = set()  # Suppress repeated "hedge outbid above fair" logs
        self._ws_no_market_warned: set = set()  # Suppress repeated "WS: No market info" logs
        self._fallback_fair_hits: Dict[str, int] = {}  # token_id -> consecutive fallback-path hits
    
    # =========================================================================
    # Main monitoring entry point
    # =========================================================================
    
    async def monitor_order(
        self,
        token_id: str,
        poly_client,
        watcher,  # OrderWatcher - avoid circular import
        hedge_token_ids: Set[str],
        # Session order context (optional - for orders created this session)
        position: Optional[Position] = None,
        cancel_position_fn: Optional[Callable[..., Awaitable]] = None,
        # Hydrated order context (optional - for orders loaded at startup)
        order_info: Optional[Dict[str, Any]] = None,
        # Cost-based fair value (for weather/spread markets without bookmaker odds)
        cost_fair: Optional[float] = None,
        # Pre-fetched order book (skip API call when provided)
        book: Optional[dict] = None,
        # WebSocket book client for cached lookups (avoids REST calls)
        book_ws=None,
    ):
        """
        Monitor a single order for outbids, edge changes, and adjustment opportunities.
        
        This is the unified entry point that handles both:
        - Session orders (pass position + cancel_position_fn)
        - Hydrated orders (pass order_info)
        - Cost-based orders (pass cost_fair for weather/spread markets)
        
        Uses cache to skip processing when state hasn't changed.
        Pass `book` to provide a pre-fetched order book (avoids per-order API call).
        """
        # Determine order source and extract common fields
        if position and position.entry_order:
            # Session order - has full context
            our_order = position.entry_order.order
            if our_order.is_complete:
                return
            
            our_price = our_order.price
            our_fair = position.entry_order.fair_value
            order_id = our_order.order_id
            stored_team = position.entry_team
            is_hedge = token_id in hedge_token_ids
            is_session_order = True
        elif order_info:
            # Hydrated order - limited context
            # IMPORTANT: Verify order still exists in BotState (may have been filled/cancelled)
            bot_state = get_bot_state()
            order_id = order_info.get("order_id", "")
            if order_id and not bot_state.get_order_info_by_id(order_id):
                return  # Order no longer tracked - was filled or cancelled
            
            our_price = order_info.get("price", 0)
            stored_team = order_info.get("team_name", "")
            our_fair = order_info.get("fair_value")  # May be None
            # Use BotState's is_hedge field (always current) instead of stale hedge_token_ids cache
            # This fixes bug where newly placed hedges get cancelled before cache refreshes
            is_hedge = order_info.get("is_hedge", False) or token_id in hedge_token_ids
            is_session_order = False
            
            if our_price <= 0:
                return
        else:
            return  # No valid order context
        
        # 1. Fetch current order book (skip if pre-fetched)
        if book is None:
            book = await poly_client.get_order_book(token_id)
        if not book:
            return
        
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        
        if not bids:
            return
        
        best_bid = float(bids[0].get("price", 0))
        best_bid_size = float(bids[0].get("size", 0))
        best_ask = float(asks[0].get("price", 0)) if asks else 0
        best_ask_size = float(asks[0].get("size", 0)) if asks else 0
        
        # 2. Update cache with order book state
        book_changed = await self.cache.update_order_book(
            token_id=token_id,
            best_bid=best_bid,
            best_bid_size=best_bid_size,
            best_ask=best_ask,
            best_ask_size=best_ask_size,
        )
        
        # 3. Get fair value - either from cost basis (weather) or bookmaker odds
        if cost_fair is not None:
            # Cost-based markets (weather/spread): synthetic fair value provided by caller
            new_fair = cost_fair
            fair_changed = False
        else:
            # Odds-based markets (esports/sports): use canonical fair value lookup.
            # Priority: per-order fair_value (v2 push) > match-level fair_prob (h2h push) > external odds
            bot_state_fv = get_bot_state()
            bs_match = bot_state_fv.get_match_by_token(token_id)
            bs_order = bs_match.get_order_for_token(token_id) if bs_match else None
            
            # CANONICAL LOOKUP: handles per-order fair_value AND match-level fair_probs
            canonical_fair = bs_match.get_fair_value_for_order(bs_order) if bs_match and bs_order else None
            
            if canonical_fair is not None:
                new_fair = canonical_fair
                fair_changed = (our_fair is not None and abs(new_fair - our_fair) > 0.01)
                match = None
                # Happy path — reset any prior fallback streak for this token.
                self._fallback_fair_hits.pop(token_id, None)
            else:
                # FALLBACK: Neither per-order nor match-level fair value available.
                # This means OddsService hasn't pushed fair probs for this match yet.
                # WARNING: This path has NO market-type filter and can return wrong
                # values for spread/totals/Yes-No sub-markets (all reuse the h2h
                # team names). Count hits and escalate log severity for genuine
                # sub-markets — detected via match_id carrying a condition_id suffix.
                from src.state.bot_state import _match_id_has_condition_suffix
                _mid = getattr(bs_match, 'match_id', '?') if bs_match else 'no_match'
                _fv = getattr(bs_order, 'fair_value', '?') if bs_order else 'no_order'
                is_sub_market = bool(bs_match and _match_id_has_condition_suffix(bs_match))
                hits = self._fallback_fair_hits.get(token_id, 0) + 1
                self._fallback_fair_hits[token_id] = hits
                msg = (
                    f"FALLBACK fair value lookup for {stored_team} (token {token_id[:16]}...) "
                    f"— no canonical fair value available. "
                    f"mid={_mid}, fv={_fv}, hits={hits}, sub_market={is_sub_market}"
                )
                if is_sub_market or hits >= FALLBACK_FAIR_LOOKUP_ESCALATE_HITS:
                    # Sub-market hit is immediately wrong; repeated h2h hits mean
                    # the OddsService push pipeline is stuck — both deserve ERROR.
                    logger.error(msg)
                else:
                    logger.warning(msg)
                new_fair, fair_changed, match = await self._get_fresh_fair_value(
                    token_id=token_id,
                    stored_team=stored_team,
                    stored_fair=our_fair,
                    poly_client=poly_client,
                    position=position,
                )
            
            if new_fair is None:
                return  # Couldn't find odds, skip
        
        # 4. Early exit if nothing changed
        is_outbid = best_bid > our_price + 0.005
        fair_moved = our_fair is not None and abs(new_fair - our_fair) > 0.03
        
        if not book_changed and not fair_changed and not is_outbid and not fair_moved:
            if cost_fair is None:
                return  # Nothing to do for odds-based markets
            # Cost-based markets (weather/spread): always proceed past cache check.
            # The cache only tracks best_bid, but price improvement depends on the 
            # SECOND-best bid which can change while best_bid stays the same
            # (e.g., when our order IS the best bid). API call already happened above.
        
        # 5. Calculate current edge
        current_edge = new_fair - our_price
        
        # 6. Handle hedge orders - special logic
        if is_hedge:
            await self._handle_hedge_order(
                token_id=token_id,
                order_id=order_id,
                order_info=order_info,
                our_price=our_price,
                our_fair=new_fair,
                best_bid=best_bid,
                bids=bids,
                is_outbid=is_outbid,
                watcher=watcher,
                position=position,
            )
            return
        
        # 7. Handle entry orders - edge validation
        if current_edge < self.config["min_edge"]:
            # Edge lost - cancel the order
            await self._cancel_for_edge_loss(
                token_id=token_id,
                order_id=order_id,
                order_info=order_info,
                stored_team=stored_team,
                our_price=our_price,
                our_fair=our_fair,
                new_fair=new_fair,
                current_edge=current_edge,
                watcher=watcher,
                position=position,
                cancel_position_fn=cancel_position_fn,
            )
            return
        
        # 8. Handle price improvement opportunity (when not outbid)
        if not is_outbid:
            await self._try_price_improvement(
                token_id=token_id,
                order_id=order_id,
                order_info=order_info,
                our_price=our_price,
                our_fair=new_fair,
                best_bid=best_bid,
                bids=bids,
                current_edge=current_edge,
                watcher=watcher,
                position=position,
            )
            return
        
        # 9. Handle outbid - try to adjust
        await self._handle_outbid(
            token_id=token_id,
            order_id=order_id,
            order_info=order_info,
            our_price=our_price,
            our_fair=new_fair,
            best_bid=best_bid,
            stored_team=stored_team,
            is_hedge=False,
            watcher=watcher,
            position=position,
            poly_client=poly_client,
            bids=bids,
            book_ws=book_ws,
        )
    
    # =========================================================================
    # WebSocket-triggered fast adjustment
    # =========================================================================
    
    async def adjust_order_fast(
        self,
        token_id: str,
        best_bid: float,
        poly_client,
        watcher,
        hedge_token_ids: Set[str],
        book_ws=None,  # Optional WS client for cached order books
    ):
        """
        Fast adjustment using already-known best_bid from WebSocket.
        
        This is called when the book WebSocket detects we've been outbid.
        Skips the order book fetch since we already have the best_bid.
        """
        # CRITICAL: Prevent duplicate adjustments when WS fires multiple times
        # before the first adjustment completes.
        # Must acquire lock IMMEDIATELY — otherwise _execute_adjustment can slip in
        # during the async calls below (get_market_by_token, get_order_book, etc.)
        if token_id in self._adjusting_tokens:
            return  # Already adjusting this token
        self._adjusting_tokens.add(token_id)
        
        try:
            return await self._adjust_order_fast_inner(
                token_id, best_bid, poly_client, watcher, hedge_token_ids, book_ws
            )
        finally:
            self._adjusting_tokens.discard(token_id)
    
    async def _adjust_order_fast_inner(
        self,
        token_id: str,
        best_bid: float,
        poly_client,
        watcher,
        hedge_token_ids: Set[str],
        book_ws=None,
    ):
        """Inner implementation of adjust_order_fast (lock already held)."""
        order_info = watcher.bot_state.get_order_info(token_id)
        if not order_info:
            return
        
        # Skip past-deadline matches — no point outbidding or improving
        match = watcher.bot_state.get_match_by_token(token_id)
        if match and match.is_past_deadline:
            return
        
        our_price = order_info.get("price", 0)
        order_id = order_info.get("order_id", "")
        
        # Already at or above best bid
        if best_bid <= our_price + 0.005:
            return
        
        new_entry = best_bid + 0.01
        
        # Try to find odds for this market
        market_info = await poly_client.get_market_by_token(token_id)
        if not market_info:
            if token_id not in self._ws_no_market_warned:
                print(f"   ⚠️ WS: No market info for {token_id[:20]}...")
                self._ws_no_market_warned.add(token_id)
            return
        
        # Extract teams from question
        market_question = market_info.get("question", "")
        teams = []
        if " vs " in market_question:
            clean_q = market_question
            if ": " in clean_q:
                clean_q = clean_q.split(": ", 1)[1]
            if " (" in clean_q:
                clean_q = clean_q.split(" (")[0]
            if " vs " in clean_q:
                teams = [t.strip() for t in clean_q.split(" vs ")]
        
        if len(teams) < 2:
            return
        
        # Get fair value - CRITICAL: filter by game type!
        # Without this, "Heretics vs Karmine Corp" could match LoL (20%) instead of Valorant (63%)
        stored_team = order_info.get("team_name", "")
        
        # PRIORITY: Use canonical fair value from BotState (per-order > match-level > token)
        bs_match = get_bot_state().get_match_by_token(token_id)
        bs_order = bs_match.get_order_for_token(token_id) if bs_match else None
        
        canonical_fair = bs_match.get_fair_value_for_order(bs_order) if bs_match and bs_order else None
        
        if canonical_fair is not None:
            our_fair = canonical_fair
        else:
            # FALLBACK: Neither per-order nor match-level fair value available.
            # This means OddsService hasn't pushed fair probs for this match yet.
            # WARNING: This path has NO market-type filter and can return wrong values!
            import logging
            logging.warning(
                f"FALLBACK fair value lookup (WS outbid) for {stored_team} "
                f"(token {token_id[:16]}...) — no canonical fair value available. "
                f"This may return wrong market type!"
            )
            # Extract game type from match_id (format: "game:team1:vs:team2")
            match_id = order_info.get("match_id", "")
            game_type = match_id.split(":")[0] if match_id and ":vs:" in match_id else None
            
            our_fair, matching_odds = get_fair_value_for_match(
                teams[0], teams[1], stored_team, self.odds_service, game=game_type
            )
        
        if our_fair is None:
            return
        
        team_name = order_info.get("team_name", token_id[:12])
        
        # Calculate edge at our CURRENT order price
        current_edge = our_fair - our_price
        
        is_hedge = token_id in hedge_token_ids
        
        # Entry orders: cancel if edge lost
        if not is_hedge and current_edge < self.config["min_edge"]:
            self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True}
            
            try:
                cancelled, was_already_complete = await self.executor.cancel_order(order_id, force=True)
                if cancelled and not was_already_complete:
                    print(f"   🚫 WS: Edge lost ({current_edge*100:+.1f}%) - CANCELLED {team_name} @ {our_price:.2f}")
                    bot_state = get_bot_state()
                    bot_state._cancelled_order_ids.add(order_id)
                    bot_state.clear_order(order_id)  # Removes from BotState tracking
                    # Record cancel
                    try:
                        recorder = get_recorder()
                        await recorder.record_order_final_status(
                            order_id=order_id,
                            status="CANCELLED",
                            cancel_reason="EDGE_LOST",
                            fair_value_at_cancel=our_fair,
                            best_bid_at_cancel=best_bid,
                        )
                    except Exception as e:
                        print(f"⚠️ Failed to record cancel: {e}")
                else:
                    print(f"   ⚠️ WS: Edge lost ({current_edge*100:+.1f}%) but cancel failed for {team_name}...")
            except Exception as e:
                print(f"   ⚠️ WS: Failed to cancel low-edge order: {e}")
            return
        
        # Calculate edge at hypothetical new price
        new_edge = our_fair - new_entry
        
        # Entry orders: can't adjust if new edge too low
        if not is_hedge and new_edge < self.config["min_edge"]:
            return
        
        # Bid war detection for entry orders: use WS cached book, fallback to REST
        if not is_hedge:
            max_bid_gap = self.config.get("max_bid_gap", 0.05)
            book = (book_ws.get_book(token_id) if book_ws else None) or await poly_client.get_order_book(token_id)
            if book:
                bids = book.get("bids", [])
                # Get match context for logging
                match_label = team_name
                bot_state_bw = get_bot_state()
                match_bw = bot_state_bw.get_match_by_token(token_id)
                if match_bw and match_bw.team1 and match_bw.team2:
                    match_label = f"{match_bw.team1} vs {match_bw.team2}"
                if self._is_bid_war(bids, our_price, new_entry, max_bid_gap, team_name, match_label):
                    self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True}
                    return
        

        # Check profitability
        should_adjust = False
        
        if is_hedge:
            # CRITICAL: Never chase hedge orders above fair value!
            if new_entry > our_fair:
                new_entry = round(our_fair, 2)
                new_edge = our_fair - new_entry
                
                # Skip if already at fair value (avoid useless cancel+replace)
                if abs(new_entry - our_price) < 0.005:
                    return
                
                # Only log once per token to avoid spam
                if token_id not in self._hedge_fair_cap_warned:
                    self._hedge_fair_cap_warned.add(token_id)
            
            # Get entry price from BotState
            bot_state = get_bot_state()
            match = bot_state.get_match_by_token(token_id)
            if match:
                entry_price = None
                if match.token1 == token_id and match.position2:
                    entry_price = match.position2.avg_price
                elif match.token2 == token_id and match.position1:
                    entry_price = match.position1.avg_price
                
                if entry_price:
                    total_cost = entry_price + new_entry
                    profit = 1.0 - total_cost
                    profit_pct = (profit / total_cost) * 100 if total_cost > 0 else 0
                    
                    if profit_pct >= self.config["min_profit"] * 100:
                        should_adjust = True
                    else:
                        return
                else:
                    return
            else:
                return
        else:
            # CRITICAL: Check against ACTUAL hedge price if one exists
            actual_hedge_price = self._get_opposite_price(bs_match, token_id) if bs_match else None
            if actual_hedge_price is not None:
                total_cost = new_entry + actual_hedge_price
                profit_pct = ((1.0 - total_cost) / total_cost) * 100 if total_cost > 0 else 0
                should_adjust = profit_pct >= self.config["min_profit"] * 100
            else:
                # No hedge yet — hypothetical hedge at complementary fair value
                other_fair = 1.0 - our_fair
                calc = self.hedge_finder.calculate_hedge(new_entry, self.config["default_shares"], other_fair)
                should_adjust = calc.can_hedge and calc.profit_percent >= self.config["min_profit"]
        
        if not should_adjust:
            return
        
        # Lock already held by adjust_order_fast wrapper.
        # Reserve token to prevent opportunity scanner from creating duplicates
        # during the cancel→place→replace window
        bot_state = get_bot_state()
        bot_state._reserved_tokens.add(token_id)
        
        # RACE GUARD: Prevent clear_order from deleting _order_to_match mapping
        # during the cancel→place→replace window. Without this, cancel_order removes
        # the old order_id mapping, and replace_order can't find it.
        bot_state._replacing_order_ids.add(order_id)
        
        try:
            # Execute adjustment
            print(f"⚡ WS: Adjusting bid {our_price:.2f} → {new_entry:.2f} (edge: {new_edge*100:.1f}%)")
            
            # PRE-TRACK: Add to cancelled set BEFORE cancel call to close hydration race.
            # State resync (every 60s) can run during the cancel API call; Polymarket's
            # API cache still shows the old order as LIVE for ~15s. Pre-tracking ensures
            # hydration filters it out even if resync happens mid-cancel.
            bot_state._cancelled_order_ids.add(order_id)
            
            cancelled, was_already_complete = await self.executor.cancel_order(order_id, force=True, team_name=team_name)
            if cancelled and not was_already_complete:
                
                # CRITICAL: Use the order's stored size, NOT default_shares!
                # default_shares is the new-order default; the original order
                # may have used a different size (per-sport override, dynamic
                # bump for $1-min, or a previous default). Using default_shares
                # here once caused 10x amplification per outbid cycle.
                replacement_size = order_info.get("size", self.config["default_shares"])
                # Subtract any filled amount to get remaining size
                filled_amount = order_info.get("filled", 0.0)
                if filled_amount > 0:
                    replacement_size = replacement_size - filled_amount
                if replacement_size < 5.0:
                    print(f"   ⚠️ WS: Remaining size {replacement_size:.1f} too small for adjustment (min 5)")
                    return
                
                new_order = await self.executor.place_limit_order(
                    token_id=token_id,
                    side=OrderSide.BUY,
                    price=new_entry,
                    size=replacement_size,
                    team_name=team_name,
                )
                
                # Record adjustment
                try:
                    recorder = get_recorder()
                    await recorder.record_order_adjustment(
                        order_id=order_id,
                        new_price=new_entry,
                        reason="ws_outbid",
                    )
                except Exception as e:
                    print(f"⚠️ Failed to record adjustment: {e}")
                
                # Update BotState
                bot_state.replace_order(order_id, new_order.order_id, new_entry, token_id=token_id)
                # BotState is now the single source of truth for order info
                
                # CRITICAL: Update _order_to_position for WebSocket fill detection
                # Without this, fills on the new order_id won't trigger alerts!
                position_id = watcher._order_to_position.pop(order_id, None)
                if position_id:
                    watcher._order_to_position[new_order.order_id] = position_id
                    # Also update the Position object's order reference
                    position = watcher.get_position(position_id)
                    if position and position.entry_order:
                        position.entry_order.order = new_order
                
                # Clear outbid state on success
                if token_id in self._outbid_state:
                    del self._outbid_state[token_id]
            elif was_already_complete:
                # CRITICAL: Clean up stale order from BotState to prevent repeated warnings
                bot_state._replacing_order_ids.discard(order_id)  # Release guard before clearing
                bot_state._cancelled_order_ids.add(order_id)
                bot_state.clear_order(order_id)
                print(f"   ⚠️ WS: Order {order_id[:12]}... already complete, cleared from state")
        finally:
            # Always clear reservation and replacing guard when done
            bot_state._reserved_tokens.discard(token_id)
            bot_state._replacing_order_ids.discard(order_id)
    
    # =========================================================================
    # Private helper methods
    # =========================================================================
    
    def _get_opposite_price(self, match, token_id: str) -> Optional[float]:
        """
        Get the price of the order or position on the opposite side of a match.
        
        Checks open orders first (pending hedge), then filled positions.
        Returns None if no opposite-side coverage exists.
        """
        if not match:
            return None
        if match.token1 == token_id:
            if match.order2 and match.order2.is_open:
                return match.order2.price
            if match.position2:
                return match.position2.avg_price
        elif match.token2 == token_id:
            if match.order1 and match.order1.is_open:
                return match.order1.price
            if match.position1:
                return match.position1.avg_price
        return None
    
    async def _get_fresh_fair_value(
        self,
        token_id: str,
        stored_team: str,
        stored_fair: Optional[float],
        poly_client,
        position: Optional[Position] = None,
    ) -> tuple:
        """
        Get fresh fair value from bookmaker odds.
        
        Returns: (new_fair, fair_changed, matching_odds_match)
        """
        if position:
            # Session order - use position context
            match = self.odds_service.get_match(position.team1, position.team2, position.game)
            
            if not match:
                return stored_fair, False, None
            
            # Determine which team we're betting on using EXACT normalized matching
            entry_norm = normalize_team(position.entry_team)
            team1_norm = normalize_team(match.team1)
            team2_norm = normalize_team(match.team2)
            
            new_fair = stored_fair
            if entry_norm == team1_norm:
                new_fair = match.fair_prob1 / 100
            elif entry_norm == team2_norm:
                new_fair = match.fair_prob2 / 100
            else:
                # No exact match found - this is a critical error, log it
                warn_key = (position.entry_team, match.team1, match.team2)
                if warn_key not in self._warned_team_mismatch:
                    print(f"   ⚠️ Could not match team '{position.entry_team}' (norm: '{entry_norm}') to match {match.team1} vs {match.team2}")
                    self._warned_team_mismatch.add(warn_key)
                return stored_fair, False, None
            
            # Update cache
            fair_changed = await self.cache.update_fair_value(
                match_id=position.match_id,
                team=position.entry_team,
                fair_prob=new_fair,
                token_id=token_id,
            )
            
            return new_fair, fair_changed, match
        else:
            # Hydrated order - lookup from market info
            try:
                market_info = await poly_client.get_market_by_token(token_id)
                
                if not market_info:
                    return stored_fair, False, None
                
                # Parse team names from market question
                market_question = market_info.get("question", market_info.get("slug", ""))
                teams_from_question = []
                
                # RUGBY FIX: Handle "Will X win?" or "Will X win on DATE?" format
                q_lower = market_question.lower()
                if is_will_win_market(q_lower):
                    # Extract team name: "Will Glasgow Warriors win?" -> "Glasgow Warriors"
                    team_name = extract_will_win_team(market_question)
                    if team_name:
                        # Look up opponent from odds service
                        odds_match = self.odds_service.get_match_by_single_team(team_name, "rugby")
                        if odds_match:
                            teams_from_question = [odds_match.team1, odds_match.team2]
                elif " vs " in market_question:
                    clean_q = market_question
                    if ": " in clean_q:
                        clean_q = clean_q.split(": ", 1)[1]
                    if " (" in clean_q:
                        clean_q = clean_q.split(" (")[0]
                    if " vs " in clean_q:
                        teams_from_question = [t.strip() for t in clean_q.split(" vs ")]
                
                if len(teams_from_question) < 2:
                    return stored_fair, False, None
                
                # Extract game from question prefix (e.g., "Valorant: Team A vs Team B")
                game_type = None
                if ": " in market_question:
                    prefix = market_question.split(": ")[0].lower()
                    # Map common prefixes to game types
                    game_map = {
                        "valorant": "valorant",
                        "cs2": "cs2", "counter-strike": "cs2", "csgo": "cs2",
                        "dota2": "dota2", "dota 2": "dota2",
                        "lol": "lol", "league of legends": "lol",
                        "mlbb": "mlbb", "mobile legends": "mlbb",
                        "hok": "hok", "honor of kings": "hok",
                        "r6": "r6", "rainbow six": "r6",
                        "cod": "cod", "call of duty": "cod",
                    }
                    game_type = game_map.get(prefix)
                
                # Use centralized function with game filter
                our_fair, matching_odds = get_fair_value_for_match(
                    teams_from_question[0], teams_from_question[1], 
                    stored_team, self.odds_service, game=game_type
                )
                
                if our_fair is None:
                    if matching_odds:
                        print(f"   ⚠️ Could not match stored team '{stored_team}' to bookmaker teams ({matching_odds.team1} vs {matching_odds.team2})")
                    return stored_fair, False, None
                
                # Update cache
                match_key = f"{teams_from_question[0]}_{teams_from_question[1]}"
                fair_changed = await self.cache.update_fair_value(
                    match_id=match_key,
                    team=stored_team,
                    fair_prob=our_fair,
                    token_id=token_id,
                )
                
                return our_fair, fair_changed, matching_odds
                
            except Exception as e:
                return stored_fair, False, None
    

    async def _cancel_for_edge_loss(
        self,
        token_id: str,
        order_id: str,
        order_info: Optional[Dict[str, Any]],
        stored_team: str,
        our_price: float,
        our_fair: Optional[float],
        new_fair: float,
        current_edge: float,
        watcher,
        position: Optional[Position] = None,
        cancel_position_fn: Optional[Callable[..., Awaitable]] = None,
    ):
        """Cancel an order because edge has been lost."""
        if position and cancel_position_fn:
            # Session order - use position callback
            print(f"   ⚠️ Edge lost! Cancelling {position.entry_team} @ {our_price:.2f}")
            old_fair = our_fair or new_fair
            print(f"      Fair: {old_fair:.2f} → {new_fair:.2f} | Edge: {(old_fair - our_price)*100:.1f}% → {current_edge*100:.1f}%")
            await cancel_position_fn(position, "Edge lost - fair value moved")
        else:
            # Hydrated order - cancel directly
            try:
                cancelled, _ = await self.executor.cancel_order(order_id, force=True)
                if cancelled:
                    match_label = ""
                    bs_match = get_bot_state().get_match_by_token(token_id)
                    if bs_match and bs_match.team1 and bs_match.team2:
                        match_label = f" | {bs_match.team1} vs {bs_match.team2}"
                    print(f"   🚫 Edge below {self.config['min_edge']*100:.0f}% ({current_edge*100:+.1f}%) - CANCELLED {stored_team} @ {our_price:.2f}{match_label}")
                    bot_state = get_bot_state()
                    bot_state._cancelled_order_ids.add(order_id)
                    bot_state.clear_order(order_id)  # Removes from BotState tracking
                    # Record cancel
                    try:
                        recorder = get_recorder()
                        await recorder.record_order_final_status(
                            order_id=order_id,
                            status="CANCELLED",
                            cancel_reason="EDGE_LOST",
                            fair_value_at_cancel=new_fair,
                        )
                    except Exception as e:
                        print(f"⚠️ Failed to record cancel: {e}")
                else:
                    print(f"   ⚠️ Edge gone but cancel failed for {token_id[:12]}...")
            except Exception as e:
                print(f"   ⚠️ Failed to cancel low-edge hydrated order: {e}")
    
    async def _try_price_improvement(
        self,
        token_id: str,
        order_id: str,
        order_info: Optional[Dict[str, Any]],
        our_price: float,
        our_fair: float,
        best_bid: float,
        bids: list,
        current_edge: float,
        watcher,
        position: Optional[Position] = None,
    ):
        """Try to improve our price (lower bid) when we're the top bid with a gap."""
        team_name = order_info.get("team_name", "") if order_info else (position.entry_team if position else "")
        
        # Are we the best bid?
        we_are_best_bid = abs(our_price - best_bid) < 0.005
        if not we_are_best_bid:
            return
        
        # Find second-best bid
        second_best_bid = 0.0
        for bid in bids:
            bid_price = float(bid.get("price", 0))
            if bid_price < our_price - 0.001:
                second_best_bid = bid_price
                break
        
        if second_best_bid <= 0:
            match_id = order_info.get("match_id", "") if order_info else ""
            # print(f"   🔍 DEBUG improve: {team_name} - no second bid found (we're the only bidder) [{match_id}]")
            return
        
        # Only try to improve if gap is significant (>= 3¢)
        gap_to_second = our_price - second_best_bid
        if gap_to_second < 0.01:
            return
        
        # New price = just above second-best bid
        improved_price = second_best_bid + 0.01
        improved_edge = our_fair - improved_price
        

        # Only improve if it gives us at least min_edge + 2% buffer
        edge_improvement = improved_edge - current_edge
        
        if improved_edge < self.config["min_edge"] + 0.02 or edge_improvement < 0.02:
            return
        
        team_name = order_info.get("team_name", "") if order_info else (position.entry_team if position else "")
        
        # Prevent concurrent adjustments to the same token
        # (matches the guard in _execute_adjustment)
        if token_id in self._adjusting_tokens:
            return
        self._adjusting_tokens.add(token_id)
        # RACE FIX: Guard order_id so WS cancel handler doesn't clear_order() mid-replace
        guarded_order_id = order_id
        get_bot_state()._replacing_order_ids.add(guarded_order_id)
        try:
            # PRE-TRACK: Add to cancelled set BEFORE cancel call to close hydration race.
            get_bot_state()._cancelled_order_ids.add(order_id)
            
            cancelled, was_already_complete = await self.executor.cancel_order(order_id, force=True, team_name=team_name)
            if cancelled and was_already_complete:
                # Order already gone — clean up BotState
                get_bot_state()._replacing_order_ids.discard(guarded_order_id)
                guarded_order_id = None
                get_bot_state().clear_order(order_id)
            elif cancelled and not was_already_complete:
                new_order = await self.executor.place_limit_order(
                    token_id=token_id,
                    side=OrderSide.BUY,
                    price=improved_price,
                    size=order_info.get("size", self.config["default_shares"]) if order_info else self.config["default_shares"],
                    team_name=team_name,
                )
                if new_order and new_order.status != OrderStatus.FAILED:
                    # Build match label for log
                    match_label = ""
                    bs = get_bot_state()
                    m = bs.get_match_by_token(token_id)
                    if m and m.team1 and m.team2:
                        match_label = f" [{m.team1} vs {m.team2}]"
                    print(f"   💰 Price improved: {team_name} {our_price:.2f} → {improved_price:.2f} (edge: {current_edge*100:.1f}% → {improved_edge*100:.1f}%){match_label}")
                    bot_state = get_bot_state()
                    bot_state.replace_order(order_id, new_order.order_id, improved_price, token_id=token_id)
                    # BotState is now the single source of truth for order info
                    # Record improvement
                    try:
                        recorder = get_recorder()
                        await recorder.record_order_adjustment(
                            order_id=order_id,
                            new_price=improved_price,
                            reason="price_improvement",
                        )
                    except Exception:
                        pass
        except Exception as e:
            print(f"   ⚠️ Price improvement failed: {e}")
        finally:
            self._adjusting_tokens.discard(token_id)
            # RACE FIX: Release the replacing guard (if not already released above)
            if guarded_order_id:
                get_bot_state()._replacing_order_ids.discard(guarded_order_id)
    
    async def _handle_outbid(
        self,
        token_id: str,
        order_id: str,
        order_info: Optional[Dict[str, Any]],
        our_price: float,
        our_fair: float,
        best_bid: float,
        stored_team: str,
        is_hedge: bool,
        watcher,
        position: Optional[Position] = None,
        poly_client=None,
        bids: Optional[list] = None,
        book_ws=None,
    ):
        """Handle being outbid - try to adjust price."""
        # Check if we already tried and failed at this level
        prev_state = self._outbid_state.get(token_id)
        if prev_state:
            if prev_state.get("failed") and abs(prev_state.get("best_bid", 0) - best_bid) < 0.02:
                return  # Already tried and failed at similar level
        
        # Calculate new entry price
        new_entry = best_bid + 0.01
        new_edge = our_fair - new_entry
        
        # Check if edge is still valid
        if new_edge < self.config["min_edge"]:
            # Record failed state
            self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True}
            return
        
        # Bid war detection: check if we're in a 1v1 bidding war with no market depth
        # Look at the 3rd best bid (after the outbidder and our current order).
        # If the gap is too large, someone is manipulating us upward.
        if bids and not is_hedge:
            # If WS book has insufficient depth for bid war evaluation, try a single REST fetch
            # This handles tokens that didn't get their initial WS snapshot
            eval_bids = bids
            has_synthetic = any(b.get("size") == "0" for b in bids)
            if (len(bids) < 3 or has_synthetic) and poly_client and new_entry > 0.10:
                try:
                    rest_book = await poly_client.get_order_book(token_id)
                    if rest_book:
                        rest_bids = rest_book.get("bids", [])
                        if len(rest_bids) > len(bids):
                            eval_bids = rest_bids
                except Exception:
                    pass  # Non-fatal, use WS data
            
            max_bid_gap = self.config.get("max_bid_gap", 0.05)  # 5c default
            # Get match context for logging
            match_label = stored_team
            bot_state_bw = get_bot_state()
            match_bw = bot_state_bw.get_match_by_token(token_id)
            if match_bw and match_bw.team1 and match_bw.team2:
                match_label = f"{match_bw.team1} vs {match_bw.team2}"
            if self._is_bid_war(eval_bids, our_price, new_entry, max_bid_gap, stored_team, match_label):
                self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True}
                return
        
        # Check live spread: don't outbid if market spread is too tight
        spread_threshold = self.config.get("spread_cancel_threshold")
        if spread_threshold and poly_client:
            try:
                bot_state = get_bot_state()
                match = bot_state.get_match_by_token(token_id)
                if match and match.token1 and match.token2:
                    opposite_token = match.token2 if token_id == match.token1 else match.token1
                    # Use WS cache first (zero latency), fallback to REST
                    opp_book = (book_ws.get_book(opposite_token) if book_ws else None) or await poly_client.get_order_book(opposite_token)
                    if opp_book:
                        opp_bids = opp_book.get("bids", [])
                        if opp_bids:
                            opp_best_bid = float(opp_bids[0].get("price", 0))
                            live_spread = 1.0 - new_entry - opp_best_bid
                            if live_spread < spread_threshold:
                                self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True}
                                return
            except Exception:
                pass  # Non-critical, proceed with adjustment
        

        # Check if hedge would still be profitable
        # CRITICAL: Check against ACTUAL hedge price if one exists
        bot_state_hc = get_bot_state()
        match_hc = bot_state_hc.get_match_by_token(token_id)
        actual_hedge_price = self._get_opposite_price(match_hc, token_id) if match_hc else None
        
        if actual_hedge_price is not None:
            # Real hedge exists — use its ACTUAL price
            total_cost = new_entry + actual_hedge_price
            profit_pct = ((1.0 - total_cost) / total_cost) * 100 if total_cost > 0 else 0
            if profit_pct < self.config["min_profit"] * 100:
                self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True}
                return
        else:
            # No hedge yet — hypothetical hedge at complementary fair value
            other_fair = 1.0 - our_fair
            calc = self.hedge_finder.calculate_hedge(new_entry, 5, other_fair)
            if not calc.can_hedge or calc.profit_percent < self.config["min_profit"]:
                self._outbid_state[token_id] = {"best_bid": best_bid, "failed": True}
                return
        
        # Build match label for log
        match_label = ""
        m_log = get_bot_state().get_match_by_token(token_id)
        if m_log and m_log.team1 and m_log.team2:
            match_label = f" [{m_log.team1} vs {m_log.team2}]"
        # Execute adjustment
        print(f"📊 {stored_team}: Outbid! Adjusting {our_price:.2f} → {new_entry:.2f} (edge: {new_edge*100:.1f}%){match_label}")
        
        await self._execute_adjustment(
            token_id=token_id,
            order_id=order_id,
            order_info=order_info,
            new_price=new_entry,
            our_fair=our_fair,
            watcher=watcher,
            position=position,
            reason="outbid",
            stored_team=stored_team,
        )
        
        # Clear outbid state on success
        if token_id in self._outbid_state:
            del self._outbid_state[token_id]
    
    def _is_bid_war(
        self,
        bids: list,
        our_price: float,
        new_entry: float,
        max_gap: float,
        team_name: str,
        match_label: str = "",
    ) -> bool:
        """
        Detect bid war manipulation by checking book depth.
        
        Looks at the order book after filtering out our current bid.
        If the gap between the outbidder (best_bid) and the next
        real market bid is too large, we're in a 1v1 bid war.
        
        Book example during manipulation:
          bids[0] = 0.48 (manipulator)
          bids[1] = 0.47 (our current bid, about to be replaced)
          bids[2] = 0.07 (real market)
          gap = 0.49 - 0.07 = 42c >> 5c threshold → bid war detected
        """
        label = match_label or team_name
        
        # Skip thin book check if bids are synthetic (size="0" from WS best_bid fallback)
        # — we don't have real L2 depth data, so can't evaluate book thickness
        has_synthetic_bids = any(b.get("size") == "0" for b in bids)
        
        if len(bids) < 3 and not has_synthetic_bids:
            # Not enough depth to evaluate — conservative: block outbid
            if new_entry > 0.10:  # Only care about meaningful prices
                print(f"   🛑 {team_name}: Thin book (only {len(bids)} bids) - skipping outbid to {new_entry:.2f} [{label}]")
                return True
            return False
        
        # Filter out our current bid from the book to find real market depth
        market_bids = []
        our_bid_removed = False
        for bid in bids:
            bid_price = float(bid.get("price", 0))
            # Remove our bid (match within 0.005 tolerance)
            if not our_bid_removed and abs(bid_price - our_price) < 0.005:
                our_bid_removed = True
                continue
            market_bids.append(bid_price)
        
        # After removing our bid, market_bids[0] = outbidder, market_bids[1+] = rest of market
        if len(market_bids) < 2:
            if new_entry > 0.10:
                print(f"   🛑 {team_name}: No market depth behind outbidder - skipping outbid to {new_entry:.2f} [{label}]")
                return True
            return False
        
        # The "real market" bid = first bid after the outbidder
        real_market_bid = market_bids[1]
        gap = new_entry - real_market_bid
        
        if gap > max_gap:
            # print(f"   🛑 {team_name}: Bid war detected! {new_entry:.2f} vs market @ {real_market_bid:.2f} (gap: {gap*100:.0f}c > {max_gap*100:.0f}c) [{label}]")
            return True
        
        return False
    
    async def _execute_adjustment(
        self,
        token_id: str,
        order_id: str,
        order_info: Optional[Dict[str, Any]],
        new_price: float,
        our_fair: float,
        watcher,
        position: Optional[Position] = None,
        reason: str = "adjustment",
        stored_team: str = "",
    ):
        """Execute an order price adjustment (cancel + replace)."""
        # Prevent concurrent adjustments to the same token
        if token_id in self._adjusting_tokens:
            return
        self._adjusting_tokens.add(token_id)
        
        # CRITICAL: Reserve token to prevent opportunity scanner from creating duplicates
        # during the cancel→place→replace window
        bot_state = get_bot_state()
        bot_state._reserved_tokens.add(token_id)
        
        # Track the order_id we're going to guard in _replacing_order_ids.
        # This may be re-assigned by double-checked locking below.
        guarded_order_id = None
        
        try:
            # DOUBLE-CHECKED LOCKING: Re-verify order state after acquiring lock
            # Another adjustment path (WS or periodic) may have already adjusted this order
            current_order_info = bot_state.get_order_info(token_id)
            if not current_order_info:
                # Order no longer exists - was cancelled or filled
                return
            current_price = current_order_info.get("price", 0)
            current_order_id = current_order_info.get("order_id", "")
            
            # CRITICAL FIX: If order_id has changed, use the CURRENT order_id, not the stale one!
            # This happens when an adjustment completed but the caller still has the old order_id
            if current_order_id != order_id:
                # The order was already adjusted - use the current order_id
                order_id = current_order_id
                # Also update the price to the current price
                if order_info:
                    order_info = dict(order_info)  # Copy to avoid mutating caller's dict
                    order_info["price"] = current_price
                    order_info["order_id"] = current_order_id
            
            # Get REMAINING order size (original size - filled amount)
            # This is critical: after a partial fill, we only want to replace the unfilled portion!
            order_size = self.config["default_shares"]  # fallback
            filled_amount = 0.0
            
            if order_info:
                original_size = order_info.get("size", self.config["default_shares"])
                filled_amount = order_info.get("filled", 0.0)
                order_size = original_size - filled_amount
            elif position and position.entry_order and position.entry_order.order:
                original_size = position.entry_order.order.size
                filled_amount = position.entry_order.order.filled_size
                order_size = original_size - filled_amount
            
            # Don't place tiny orders
            if order_size < 5.0:
                print(f"   ⚠️ Remaining size {order_size:.1f} too small for adjustment (min 5)")
                return
            
            # RACE FIX: Guard order_id so WS cancel handler doesn't clear_order() mid-replace.
            # Track which ID we guarded so we can always clean it up in finally.
            guarded_order_id = order_id
            bot_state._replacing_order_ids.add(guarded_order_id)
            
            # PRE-TRACK: Add to cancelled set BEFORE cancel call to close hydration race.
            bot_state._cancelled_order_ids.add(order_id)
            
            cancelled, was_already_complete = await self.executor.cancel_order(order_id, force=True, team_name=stored_team)
            if not cancelled:
                print(f"   ❌ Failed to cancel order {order_id[:12]}...")
                return
            
            # CRITICAL: Don't place new order if original was already gone!
            if was_already_complete:
                print(f"   ⚠️ Order {order_id[:12]}... was already complete, skipping adjustment ({stored_team})")
                # Release the guard BEFORE clear_order so it can actually clean up
                bot_state._replacing_order_ids.discard(guarded_order_id)
                guarded_order_id = None
                bot_state.clear_order(order_id)
                return
            
            # Place new order with REMAINING size (not original size!)
            new_order = await self.executor.place_limit_order(
                token_id=token_id,
                side=OrderSide.BUY,
                price=new_price,
                size=order_size,
                team_name=stored_team,
            )
            
            # Record adjustment
            try:
                recorder = get_recorder()
                await recorder.record_order_adjustment(
                    order_id=order_id,
                    new_price=new_price,
                    reason=reason,
                )
            except Exception as e:
                print(f"⚠️ Failed to record adjustment: {e}")
            
            if position:
                # Session order - update position
                if position.entry_order:
                    watcher._order_to_position.pop(order_id, None)
                    position.entry_order.order = new_order
                    position.entry_order.fair_value = our_fair
                    watcher._order_to_position[new_order.order_id] = position.position_id
            
            # Update BotState (single source of truth for order info)
            bot_state.replace_order(order_id, new_order.order_id, new_price, token_id=token_id)
            
        finally:
            # Always release the lock and reservation
            self._adjusting_tokens.discard(token_id)
            bot_state._reserved_tokens.discard(token_id)
            # RACE FIX: Release the replacing guard (if not already released above)
            if guarded_order_id:
                bot_state._replacing_order_ids.discard(guarded_order_id)
