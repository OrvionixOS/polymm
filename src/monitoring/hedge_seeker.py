"""
Hedge seeker - actively places hedge orders for filled positions.

Features:
- De-duplication of warning messages
- Real-time BotState updates (hedge orders registered for coverage tracking)
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Set, Optional, Dict

from src.execution.order_executor import OrderExecutor, OrderSide
from src.services.telegram_alerts import TelegramAlerts
from src.state.bot_state import get_bot_state

logger = logging.getLogger(__name__)


class HedgeSeeker:
    """
    Actively seeks to place hedge orders for filled positions that need hedging.
    
    Requires injection of:
    - executor: OrderExecutor instance
    - alerts: TelegramAlerts instance
    - config: CONFIG dict
    
    """
    
    def __init__(
        self,
        executor: OrderExecutor,
        alerts: TelegramAlerts,
        config: dict,
    ):
        self.executor = executor
        self.alerts = alerts
        self.config = config
        self.bot_state = get_bot_state()
        
        # Warning de-duplication
        self._warned_unprofitable: Dict[str, float] = {}  # key -> last_hedge_entry warned

        # Skip set for ended matches (proposed/resolved — no point retrying)
        self._ended_matches: Set[str] = set()

        # Per-token hedge locks — serialize concurrent hedge attempts on the
        # same hedge_token. Without this, two near-simultaneous seeks can
        # both pass the `existing_order` check and both register a new hedge.
        self._hedge_token_locks: Dict[str, asyncio.Lock] = {}

    def _get_hedge_lock(self, token_id: str) -> asyncio.Lock:
        """Return a lock for a given hedge token, creating it on first use."""
        lock = self._hedge_token_locks.get(token_id)
        if lock is None:
            lock = asyncio.Lock()
            self._hedge_token_locks[token_id] = lock
        return lock
    
    async def _seek_weather_hedge(
        self,
        key: str,
        hedge_info: dict,
        poly_client,
        watcher,
        hedge_token_ids: Set[str],
        book_ws: Optional[object] = None,
    ) -> bool:
        """
        Seek hedge for weather market positions using cost-based logic.
        
        Weather markets don't have external fair value, so we use:
        - Profitable if: entry_price + hedge_price < 1.0 - min_profit
        - max_hedge_price = 1.0 - entry_price - min_profit
        """
        from src.core.config import SPREAD_CONFIG
        
        entry_team = hedge_info.get("entry_team", "")
        entry_price = hedge_info.get("entry_price", 0)
        shares = hedge_info.get("shares", 0)
        total_entry_shares = hedge_info.get("total_entry_shares", shares)
        hedge_token = hedge_info.get("hedge_token_id", "")
        hedge_team = hedge_info.get("hedge_team", "")
        match_id = hedge_info.get("match_id", "")
        entry_token = hedge_info.get("entry_token_id", "")
        hedge_fair_value = hedge_info.get("hedge_fair_value")
        
        # If hedge_token is missing (nulled by same-token guard), look up opposite token
        if not hedge_token and entry_token:
            if entry_team.startswith("Not ") or entry_team.endswith(" No"):
                # Entry is NO side → need YES token to hedge
                hedge_token = await self._get_same_market_yes_token(
                    entry_token, poly_client, position_team=entry_team
                )
                if hedge_token:
                    hedge_team = entry_team.removeprefix("Not ").removesuffix(" No")
                    import re
                    hedge_team = re.sub(r'\s*\([-+]?\d+\.?\d*\)\s*$', '', hedge_team).strip()
            else:
                # Entry is YES side → need NO token to hedge
                hedge_token = await self._get_same_market_no_token(entry_token, poly_client)
                if hedge_token:
                    hedge_team = f"Not {entry_team}"
        
        if not hedge_token or shares < 5.0:
            if not hedge_token and shares >= 5.0:
                print(f"   [HEDGE] No hedge token for '{entry_team}' ({shares:.0f} shares) - skipping")
            return False

        
        # Calculate max hedge price for profitability
        min_profit = SPREAD_CONFIG.get("min_profit", 0.10)  # 10c default
        max_hedge_price = 1.0 - entry_price - min_profit
        
        # EDGE CAP: hedge price must also respect fair value (edge at worst -1%)
        # This prevents placing hedges wildly above fair value that get immediately cancelled
        HEDGE_MAX_EDGE_OVERPAY = 0.01  # 1% above fair is the max we'll pay
        if hedge_fair_value is not None:
            max_edge_price = hedge_fair_value + HEDGE_MAX_EDGE_OVERPAY
            max_hedge_price = min(max_hedge_price, max_edge_price)
        
        if max_hedge_price < 0.01:
            print(f"   ⚠️ [WEATHER] Cannot hedge profitably: {entry_team} @ {entry_price:.2f}")
            return False

        # Serialize concurrent hedge attempts on the same hedge_token — this
        # section reads BotState, decides to cancel+replace, and registers a
        # new hedge order. Without this lock, two near-simultaneous seeks can
        # both pass the existing_order check and both register new hedges.
        async with self._get_hedge_lock(hedge_token):
            return await self._seek_weather_hedge_locked(
                key=key,
                hedge_info=hedge_info,
                poly_client=poly_client,
                watcher=watcher,
                hedge_token_ids=hedge_token_ids,
                book_ws=book_ws,
                entry_team=entry_team,
                entry_price=entry_price,
                shares=shares,
                hedge_token=hedge_token,
                hedge_team=hedge_team,
                match_id=match_id,
                max_hedge_price=max_hedge_price,
            )

    async def _seek_weather_hedge_locked(
        self,
        *,
        key: str,
        hedge_info: dict,
        poly_client,
        watcher,
        hedge_token_ids: Set[str],
        book_ws: Optional[object],
        entry_team: str,
        entry_price: float,
        shares: float,
        hedge_token: str,
        hedge_team: str,
        match_id: str,
        max_hedge_price: float,
    ) -> bool:
        """Body of _seek_weather_hedge that must run under the per-token lock."""
        # Check ALL matches for an existing open order on this token.
        # CRITICAL: Can't rely on get_match_by_token() alone because _token_to_match
        # gets overwritten during rehydration. Position hydration sets token2 to the
        # same-market NO token, but hedge orders use the opponent's YES token.
        # Each rehydration cycle overwrites token2, breaking the lookup.
        existing_order = None
        for m in self.bot_state.get_all_matches():
            for order in [m.order1, m.order2]:
                if order and order.is_open and order.token_id == hedge_token:
                    existing_order = order
                    break
            if existing_order:
                break
        
        if existing_order:
            existing_unfilled = existing_order.size - existing_order.filled
            if existing_unfilled >= shares - 0.5:
                return False  # Existing order covers enough
            
            # Existing order is too small — cancel it and replace with full-sized order
            # shares already has existing coverage subtracted, so add it back
            print(f"   🔍 [HEDGE DEBUG] Existing order {existing_order.order_id[:12]}... covers {existing_unfilled:.0f}/{shares:.0f} shares — cancel+replace")
            shares = shares + existing_unfilled
            try:
                # force=True: hydrated orders aren't in executor._orders, so
                # without force it silently skips the actual Polymarket cancel
                await self.executor.cancel_order(existing_order.order_id, force=True, team_name=hedge_team)
                # cancel_order (not clear_order!) adds to _cancelled_order_ids,
                # which prevents hydration from re-registering this stale order
                self.bot_state.cancel_order(existing_order.order_id)
                print(f"   🔄 [HEDGE] Cancelled undersized order ({existing_unfilled:.0f} shares) to replace with {shares:.0f} shares")
            except Exception as e:
                print(f"   ⚠️ [HEDGE] Failed to cancel undersized order: {e}")
                return False
        else:
            # BotState doesn't know about any order for this hedge token.
            # Before placing, check the CLOB API directly — there might be
            # an un-hydrated order from a previous session/cycle.
            clob_orders = await self.executor.get_orders_for_token(hedge_token)
            if clob_orders:
                best = clob_orders[0]  # Highest price (sorted desc)
                best_size = best["size"]
                
                # CRITICAL: Don't re-register orders that were already cancelled
                # (deadline cancel, edge-lost cancel, etc.) — Polymarket API has cache lag
                # and still returns cancelled orders for a few cycles
                if best["order_id"] in self.bot_state._cancelled_order_ids:
                    return False  # Already cancelled, skip
                
                if best_size >= shares - 0.5:
                    # Existing CLOB order fully covers the hedge need
                    print(f"   🔍 [HEDGE] Discovered existing CLOB order {best['order_id'][:12]}... "
                          f"@ {best['price']:.2f} ({best_size:.0f} shares) for {hedge_team} — registering in BotState")
                    
                    # Register in BotState so future cycles don't re-check
                    if match_id:
                        self.bot_state.register_order(
                            match_id=match_id,
                            order_id=best["order_id"],
                            token_id=hedge_token,
                            team=hedge_team,
                            price=best["price"],
                            size=best_size,
                            is_entry=False,
                        )
                    return False  # No new order needed
                else:
                    # Existing CLOB order is undersized — cancel and replace
                    print(f"   🔍 [HEDGE] Discovered undersized CLOB order {best['order_id'][:12]}... "
                          f"@ {best['price']:.2f} ({best_size:.0f}/{shares:.0f} shares) — cancel+replace")
                    try:
                        await self.executor.cancel_order(best["order_id"], force=True, team_name=hedge_team)
                        self.bot_state.cancel_order(best["order_id"])
                    except Exception as e:
                        print(f"   ⚠️ [HEDGE] Failed to cancel undersized CLOB order: {e}")
                        return False
                    # Fall through to place full-sized order below
        
        try:
            # Get order book
            book = await poly_client.get_order_book(hedge_token)
            if not book:

                return False
            
            bids = book.get("bids", [])
            if not bids:

                return False
            
            best_bid = float(bids[0].get("price", 0))
            
            # Determine hedge price: best_bid + 1c, capped at max_hedge_price
            hedge_price = min(best_bid + 0.01, max_hedge_price)
            
            if hedge_price < 0.01:
                return False
            
            # Calculate expected profit
            total_cost = entry_price + hedge_price
            expected_profit = 1.0 - total_cost
            
            hedge_label = "WEATHER HEDGE" if match_id.startswith("weather") else "COST HEDGE"
            print(f"🎯 [{hedge_label}] {entry_team} @ {entry_price:.2f}")
            print(f"   Hedging with {hedge_team} @ {hedge_price:.2f}")
            print(f"   Total cost: ${total_cost:.2f} → Profit: {expected_profit*100:.1f}c")
            
            # Place hedge order
            order = await self.executor.place_limit_order(
                token_id=hedge_token,
                side=OrderSide.BUY,
                price=hedge_price,
                size=max(shares, 5.0),
                team_name=hedge_team,
            )
            
            if order:
                hedge_token_ids.add(hedge_token)

                # Register with BotState
                if match_id:
                    reg_result = self.bot_state.register_order(
                        match_id=match_id,
                        order_id=order.order_id,
                        token_id=hedge_token,
                        team=hedge_team,
                        price=hedge_price,
                        size=shares,
                        is_entry=False,
                    )
                    # Verify registration reached order_state fully:
                    #   1. register_order returned a MatchState (non-None)
                    #   2. order_id → match_id lookup resolves
                    #   3. the MatchState exposes the order via match.order1/order2
                    # A dropped registration means this CLOB order will sit
                    # unhedged — we must cancel it and alert instead.
                    verify_found = False
                    if reg_result is not None and self.bot_state._order_to_match.get(order.order_id) == match_id:
                        for o in (reg_result.order1, reg_result.order2):
                            if o and o.order_id == order.order_id and o.is_open and o.token_id == hedge_token:
                                verify_found = True
                                break
                    if not verify_found:
                        alert_msg = (
                            f"[HEDGE BUG] Hedge order {order.order_id[:12]}... "
                            f"for {hedge_team} (match_id={match_id[:40]}) "
                            f"placed on CLOB but NOT registered in BotState — cancelling to prevent unhedged exposure"
                        )
                        print(f"   🚨 {alert_msg}")
                        try:
                            await self.alerts.send(alert_msg)
                        except Exception as alert_err:
                            print(f"   ⚠️ telegram alert failed: {alert_err}")
                        try:
                            await self.executor.cancel_order(order.order_id, force=True, team_name=hedge_team)
                            self.bot_state.cancel_order(order.order_id)
                        except Exception as cancel_err:
                            print(f"   ❌ CRITICAL: failed to cancel unregistered hedge {order.order_id[:12]}: {cancel_err}")
                        return False

                # Subscribe to WebSocket
                if book_ws and hasattr(book_ws, 'is_connected') and book_ws.is_connected:
                    await book_ws.subscribe([hedge_token])

                return True

            return False
            
        except Exception as e:

            print(f"   ❌ Weather hedge failed: {e}")
            return False



    async def _get_same_market_no_token(self, yes_token_id: str, poly_client) -> Optional[str]:
        """
        Get the No token ID from the same binary Yes/No market.
        
        For rugby "Will X win?" markets:
        - Yes token = betting on X winning
        - No token = betting on X NOT winning (opponent wins OR draw)
        
        Returns the No token ID if found, None otherwise.
        """
        try:
            market_info = await poly_client.get_market_by_token(yes_token_id)
            if not market_info:
                return None
            
            import json
            tokens_raw = market_info.get("clobTokenIds", [])
            if isinstance(tokens_raw, str):
                tokens = json.loads(tokens_raw)
            else:
                tokens = tokens_raw
            
            # Binary market should have exactly 2 tokens
            if len(tokens) == 2 and yes_token_id in tokens:
                # Return the other token (No side)
                return tokens[1] if tokens[0] == yes_token_id else tokens[0]
            
            return None
        except Exception as e:
            print(f"   ⚠️ Failed to get No token: {e}")
            return None

    async def _get_same_market_yes_token(self, no_token_id: str, poly_client, position_team: str = "") -> Optional[str]:
        """
        Get the opposite token ID from the same binary market.
        
        All Polymarket markets are 2-token binary markets.
        Given one token, returns the other.
        """
        try:
            market_info = await poly_client.get_market_by_token(no_token_id)
            if not market_info:
                print(f"   ⚠️ No market info for token {no_token_id[:16]}...")
                return None
            
            import json
            tokens_raw = market_info.get("clobTokenIds", [])
            if isinstance(tokens_raw, str):
                tokens = json.loads(tokens_raw)
            else:
                tokens = tokens_raw
            
            # Binary market: return the other token
            if len(tokens) == 2 and no_token_id in tokens:
                return tokens[1] if tokens[0] == no_token_id else tokens[0]
            
            print(f"   ⚠️ Token {no_token_id[:16]}... not in market or not a 2-token market")
            return None
        except Exception as e:
            print(f"   ⚠️ Failed to get opposite token: {e}")
            return None
    

    
    async def seek_hedge_for_position(
        self,
        key: str,
        hedge_info: dict,
        poly_client,
        watcher,  # OrderWatcher - avoid circular import
        hedge_token_ids: Set[str],  # Set to track hedge tokens (will be modified)
        book_ws: Optional[object] = None,  # WebSocket for subscribing
        live_event_tokens: Optional[Set[str]] = None,  # Tokens on live events
        allow_live_hedges: bool = False,  # If True, allow hedging live positions (LiveBot)
        live_match_ids: Optional[Set[str]] = None,  # Match IDs from esports_odds_live (ground truth)
    ) -> bool:
        """
        Actively seek to place a hedge order for an existing filled position.
        
        This is called for positions that were filled in previous sessions
        and still need a hedge on the opposite side.
        
        Args:
            allow_live_hedges: If True, allow hedging live positions (for LiveBot).
                              If False (default), skip live positions (for SportsBot).
            live_match_ids: Match IDs from esports_odds_live - skip hedging for these matches.
        
        Returns True if hedge order was placed, False otherwise.
        """
        match_id = hedge_info.get("match_id", "")
        
        # Skip ended matches (proposed/resolved — no point retrying)
        if key in self._ended_matches:
            return False
        
        # CRITICAL: Skip hedging for matches in esports_odds_live (ground truth live detection)
        # This is more reliable than Polymarket's is_live flag
        if not allow_live_hedges and live_match_ids and match_id in live_match_ids:
            return False
        

        # === STANDARD 2-WAY LOGIC ===
        entry_team = hedge_info.get("entry_team", "")
        hedge_team = hedge_info.get("hedge_team", "")
        entry_price = hedge_info.get("entry_price", 0)
        shares = hedge_info.get("shares", 0)  # Unhedged amount (additional shares needed)
        total_entry_shares = hedge_info.get("total_entry_shares", shares)  # Total entry position size
        hedge_token = hedge_info.get("hedge_token_id", "")
        hedge_fair_value = hedge_info.get("hedge_fair_value")  # Fair value for hedge team
        entry_token = hedge_info.get("entry_token_id", "")
        
        # CRITICAL FIX: When hedge_token is None (same-token guard or NO token detection),
        # look up the opposite token using existing _get_same_market_* methods.
        # Handles: "X No" (rugby), "Not X" (spread), and plain YES entries (same-token guard)
        if not hedge_token and entry_token:
            is_no_entry = entry_team.endswith(" No") or entry_team.startswith("Not ")
            if is_no_entry:
                hedge_token = await self._get_same_market_yes_token(entry_token, poly_client, position_team=entry_team)
            else:
                hedge_token = await self._get_same_market_no_token(entry_token, poly_client)
            if not hedge_token:
                print(f"   ⚠️ Could not find opposite token for {entry_team} - cannot hedge")
                return False
        
        # Skip live matches unless allow_live_hedges is True
        # SportsBot (default): skip live positions
        # LiveBot: passes allow_live_hedges=True to hedge live positions
        if not allow_live_hedges and live_event_tokens and hedge_token in live_event_tokens:
            return False
        
        if not hedge_token or shares <= 0:
            return False
        
        # Only hedge positions >= 5 shares (Polymarket minimum order size)
        # It's wasteful to place 5-share hedges for smaller positions
        if shares < 5.0:
            return False
        
        # SPREAD BOT MARKET SUPPORT: No fair value needed - use cost-based hedging
        # For spread-bot markets, hedge is profitable if: entry_price + hedge_price < 1.0 - min_profit
        is_weather_market = match_id.startswith("weather") or match_id.startswith("stock:") or match_id.startswith("ncaab:") or match_id.startswith("mentions:") or match_id.startswith("spread:") or match_id.startswith("rugby:") or match_id.startswith("tennis:") or match_id.startswith("cricket:") or match_id.startswith("hockey:") or match_id.startswith("ufc:") or match_id.startswith("football:") or match_id.startswith("basketball:")
        
        if is_weather_market:
            # Use cost-based hedge evaluation for weather markets
            return await self._seek_weather_hedge(
                key=key,
                hedge_info=hedge_info,
                poly_client=poly_client,
                watcher=watcher,
                hedge_token_ids=hedge_token_ids,
                book_ws=book_ws,
            )
        
        # If we don't have fair value, we can't evaluate profitability
        if hedge_fair_value is None:
            return False
        
        # END-GAME DETECTION (LiveBot only): If hedge fair value is extreme, the game is essentially over
        # Same logic as opportunity scanner - skip finished games
        # Odds < 0.10 = team has <10% chance (loser), Odds > 0.90 = team has >90% chance (winner)
        # Only apply to LiveBot (allow_live_hedges=True) - SportsBot handles prematch markets differently
        if allow_live_hedges:
            END_GAME_FAIR_LOW = 0.10
            END_GAME_FAIR_HIGH = 0.90
            
            if hedge_fair_value < END_GAME_FAIR_LOW or hedge_fair_value > END_GAME_FAIR_HIGH:
                print(f"   🏁 Game ended: {hedge_team} fair value {hedge_fair_value*100:.0f}% is extreme - skipping hedge")
                
                # Cancel any existing hedge order for this position
                match = self.bot_state.get_match_by_token(hedge_token)
                if match:
                    existing_order = None
                    if match.token1 == hedge_token and match.order1 and match.order1.is_open:
                        existing_order = match.order1
                    elif match.token2 == hedge_token and match.order2 and match.order2.is_open:
                        existing_order = match.order2
                    
                    if existing_order:
                        try:
                            print(f"   🗑️ Cancelling stale hedge order: {hedge_team} @ {existing_order.price:.2f}")
                            cancelled, _ = await self.executor.cancel_order(existing_order.order_id, force=True, team_name=hedge_team)
                            if cancelled:
                                self.bot_state.clear_order(existing_order.order_id)
                        except Exception as e:
                            print(f"   ⚠️ Failed to cancel stale order: {e}")
                
                return False
        

        

        # Check if market has ended (proposed or resolved) - skip hedge attempts
        try:
            market_status = await poly_client.get_market_status(hedge_token)
            uma_status = market_status.get("umaResolutionStatus")
            if uma_status in ("proposed", "resolved"):
                # Match has ended - determine outcome and log
                entry_token = hedge_info.get("entry_token_id", "")
                match_id = hedge_info.get("match_id", "")
                
                # Get outcome prices to determine winner
                import json
                prices_str = market_status.get("outcomePrices", "[]")
                try:
                    prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
                except:
                    prices = []
                
                # Determine if entry side won (entry price would be ~0.999 if won)
                # We need to match entry_token to the outcome
                # For now, just log that the match ended and skip hedge
                status_text = "RESOLVED" if uma_status == "resolved" else "ENDED"
                
                # Calculate P&L for unhedged position
                if prices and len(prices) >= 2:
                    # Find which side entry_team is on
                    entry_won = False
                    market_info = await poly_client.get_market_by_token(hedge_token)
                    if market_info:
                        tokens = market_info.get("clobTokenIds", [])
                        import json
                        if isinstance(tokens, str):
                            tokens = json.loads(tokens)
                        if tokens and entry_token in tokens:
                            entry_idx = tokens.index(entry_token)
                            entry_won = float(prices[entry_idx]) > 0.5
                        elif tokens and hedge_token in tokens:
                            # hedge_token should be the opposite side
                            hedge_idx = tokens.index(hedge_token)
                            entry_won = float(prices[hedge_idx]) < 0.5
                    
                    if entry_won:
                        profit = total_entry_shares * (1.0 - entry_price)
                        print(f"🎯 [UNHEDGED WIN] {entry_team} @ {entry_price:.2f} | +${profit:.2f} ({(1.0-entry_price)/entry_price*100:.1f}%)")
                    else:
                        loss = total_entry_shares * entry_price
                        print(f"💔 [UNHEDGED LOSS] {entry_team} @ {entry_price:.2f} | -${loss:.2f}")
                else:
                    print(f"🏁 [{status_text}] {entry_team} match ended - skipping hedge")
                
                # Mark as permanently given up (match is over)
                self._ended_matches.add(key)
                return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Market-status probe failed — we still continue so a legitimate hedge
            # isn't blocked by a transient Gamma/CLOB hiccup, but we must not
            # swallow the error silently. If this path fires repeatedly for the
            # same hedge_token, we risk posting hedges on a match that already
            # resolved (wasted spend, UMA dispute window).
            logger.warning(
                "market_status probe failed for hedge_token=%s... (%s): %s — proceeding with hedge attempt",
                hedge_token[:16] if hedge_token else "?",
                hedge_team,
                e,
            )

        # Check if we already have an ORDER on the hedge token
        # Note: Having a POSITION on the hedge side is fine - we may need more shares
        existing_hedge_order = None
        cancelled_existing_order = False  # Track if we cancelled an order (need full replacement)
        match = self.bot_state.get_match_by_token(hedge_token)
        
        if match:
            # Determine which side is the hedge side and check for open order
            if match.token1 == hedge_token and match.order1 and match.order1.is_open:
                existing_hedge_order = match.order1
            elif match.token2 == hedge_token and match.order2 and match.order2.is_open:
                existing_hedge_order = match.order2
            
            if existing_hedge_order:
                # If existing ORDER covers the UNHEDGED amount, no action needed
                # Use shares (unhedged amount), not total_entry_shares
                if existing_hedge_order.size >= shares - 0.5:  # Allow small tolerance
                    return False  # Already have sufficient hedge order
                
                # Existing hedge order is undersized vs UNHEDGED amount - cancel and resize
                print(f"   📐 Hedge undersized: {existing_hedge_order.size:.1f} shares vs needed {shares:.1f} shares")
                try:
                    cancelled, _ = await self.executor.cancel_order(existing_hedge_order.order_id, force=True, team_name=hedge_team)
                    if cancelled:
                        self.bot_state.clear_order(existing_hedge_order.order_id)  # Removes from BotState tracking
                        cancelled_existing_order = True  # We cancelled, need replacement
                        print(f"   ✅ Cancelled undersized hedge, will place new {shares:.1f}-share order")
                except Exception as e:
                    print(f"   ⚠️ Failed to cancel undersized hedge: {e}")
                    return False
            # else: No open order on hedge side - we'll place one below
            # (Having a position is fine, we may need more shares to complete the hedge)
        

        
        try:
            # CRITICAL: Always evaluate hedge profitability at FAIR VALUE, not best bid
            # If best bid is above fair value, we place at fair value instead
            # This ensures we never chase hedges above fair value
            
            # Calculate profitability at fair value
            entry_cost = total_entry_shares * entry_price
            hedge_cost_at_fair = total_entry_shares * hedge_fair_value
            total_cost = entry_cost + hedge_cost_at_fair
            payout = total_entry_shares * 1.0
            profit = payout - total_cost
            profit_pct = (profit / total_cost * 100) if total_cost > 0 else 0
            
            # Calculate the MAXIMUM price we can pay for hedge and still be profitable
            # Entry cost + hedge cost <= $1 - min_profit
            # hedge_price <= 1.0 - entry_price - min_profit (in absolute terms)
            min_profit_abs = self.config["min_profit"]  # e.g. 0.05 for 5%
            max_hedge_price = 1.0 - entry_price - min_profit_abs
            
            # Ensure max_hedge_price is reasonable (at least $0.01)
            if max_hedge_price < 0.01:
                # Entry price too high to hedge profitably at any price
                last_warned = self._warned_unprofitable.get(key)
                if last_warned is None or abs(entry_price - last_warned) >= 0.02:
                    print(f"   ⚠️ Cannot hedge profitably: {entry_team} @ {entry_price:.2f} - no room for 5% profit")
                    self._warned_unprofitable[key] = entry_price
                return False
            
            # Get current order book to check best bid. Empty book / no bids
            # means we can't price the hedge at all — worth surfacing because
            # the position stays UNHEDGED until the book recovers. Dedup via
            # _warned_unprofitable so a persistently-empty book logs once.
            book = await poly_client.get_order_book(hedge_token)
            if not book:
                last_warned = self._warned_unprofitable.get(key)
                if last_warned is None or abs(entry_price - (last_warned or 0)) >= 0.02:
                    logger.warning(
                        "no order book for hedge token %s... (%s) — %s unhedged",
                        hedge_token[:16], hedge_team, entry_team,
                    )
                    self._warned_unprofitable[key] = entry_price
                return False

            bids = book.get("bids", [])
            if not bids:
                last_warned = self._warned_unprofitable.get(key)
                if last_warned is None or abs(entry_price - (last_warned or 0)) >= 0.02:
                    logger.warning(
                        "no bids on hedge token %s... (%s) — %s unhedged",
                        hedge_token[:16], hedge_team, entry_team,
                    )
                    self._warned_unprofitable[key] = entry_price
                return False
            
            best_bid = float(bids[0].get("price", 0))
            
            # Determine our hedge entry price:
            # Use the MINIMUM of:
            # 1. best_bid + 0.01 (to be at top of book)
            # 2. hedge_fair_value (never pay above fair value)
            # 3. max_hedge_price (must be profitable)
            if best_bid < max_hedge_price:
                # Market is cheap enough - bid above current best bid (but not above our limits)
                hedge_entry = min(best_bid + 0.01, hedge_fair_value, max_hedge_price)
            else:
                # Market is too expensive - place passive order at max profitable price
                hedge_entry = max_hedge_price
                print(f"   📌 Passive hedge: {hedge_team} @ {hedge_entry:.2f} (market {best_bid:.2f} too high)")
            
            # Check if hedge price is reasonable (< 0.95). Above this ceiling
            # we give up — but this leaves NAKED EXPOSURE on the entry side,
            # so the skip must be visible in logs (not a silent return).
            # Dedup on entry_price so a ticking book doesn't spam the feed.
            if hedge_entry >= 0.95:
                last_warned = self._warned_unprofitable.get(key)
                if last_warned is None or abs(entry_price - last_warned) >= 0.02:
                    print(
                        f"   ⚠️ Hedge too expensive: {entry_team} @ {entry_price:.2f} "
                        f"would need hedge @ {hedge_entry:.2f} (≥0.95 ceiling) — "
                        f"position remains UNHEDGED"
                    )
                    self._warned_unprofitable[key] = entry_price
                return False
            
            # Determine order size:
            # Always use shares (unhedged amount) - this is what we actually need
            # (Polymarket minimum order size is 5 shares)
            order_shares = max(shares, 5.0)  # 'shares' = unhedged amount
            
            # CRITICAL: Check minimum order value ($1 for marketable orders)
            # Low-priced hedges (e.g., 37 shares @ $0.01 = $0.37) will be rejected
            min_order_value = 1.0
            order_value = order_shares * hedge_entry
            if order_value < min_order_value:
                # Either bump shares to meet minimum, or skip if too small
                min_shares_needed = min_order_value / hedge_entry
                if min_shares_needed <= 100:  # Reasonable to bump up
                    order_shares = min(min_shares_needed, 100)
                    print(f"   📐 Bumping hedge size: {shares:.0f} → {order_shares:.0f} shares (min $1 order)")
                else:
                    # Would need >100 shares - skip this hedge
                    print(f"   ⚠️ Hedge too small: {shares:.0f} shares @ ${hedge_entry:.2f} = ${order_value:.2f} (<$1 min)")
                    return False
            
            # Calculate actual profit at our hedge price
            actual_total_cost = entry_price + hedge_entry
            actual_profit_abs = 1.0 - actual_total_cost
            actual_profit_pct = (actual_profit_abs / actual_total_cost * 100) if actual_total_cost > 0 else 0
            
            print(f"🎯 HEDGE OPPORTUNITY: {entry_team} @ {entry_price:.2f} + {hedge_team} @ {hedge_entry:.2f}")
            print(f"   Placing {order_shares:.0f} shares ({actual_profit_pct:.1f}% profit on full arb)")
            
            # Place the hedge order
            order = await self.executor.place_limit_order(
                token_id=hedge_token,
                side=OrderSide.BUY,
                price=hedge_entry,
                size=order_shares,
                team_name=hedge_team,
            )
            
            if order:
                print(f"   ✅ Hedge order placed: {order.order_id[:12]}...")
                

                
                # Mark as hedge (for display purposes)
                hedge_token_ids.add(hedge_token)
                

                
                # Register hedge order with BotState - this is KEY!
                # BotState tracking is now the single source of truth for active tokens
                # BotState.get_positions_needing_hedge() will now exclude this position
                # because has_coverage_on_sideX will be True (we have an open order)
                # If the order is cancelled, cancel_order() will make has_coverage False
                # and the position will automatically reappear in get_positions_needing_hedge()
                bot_state = get_bot_state()
                match_id = hedge_info.get("match_id", "")
                if match_id:
                    bot_state.register_order(
                        match_id=match_id,
                        order_id=order.order_id,
                        token_id=hedge_token,
                        team=hedge_team,
                        price=hedge_entry,
                        size=order_shares,
                        is_entry=False,  # This is a hedge order
                    )
                
                # Subscribe to WebSocket for this token
                if book_ws and hasattr(book_ws, 'is_connected') and book_ws.is_connected:
                    await book_ws.subscribe([hedge_token])
                
                return True
            

            return False
        
        except Exception as e:
            print(f"❌ Failed to place hedge for {entry_team}: {e}")
            return False
    
