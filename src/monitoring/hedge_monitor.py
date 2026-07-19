"""
Hedge Monitor Mixin - Hedge order monitoring logic extracted from OrderMonitor.

This module contains hedge-specific order management:
- Fair value cap enforcement (never overpay)
- Profitability checks (min_profit threshold)
- Price improvement for hedges
- Outbid handling with fair value limits

Uses mixin pattern to integrate with OrderMonitor.
"""
from typing import Optional, Dict, Any

from src.execution.order_executor import OrderSide, OrderStatus
from src.state.bot_state import get_bot_state


class HedgeMonitorMixin:
    """
    Mixin providing hedge order monitoring for OrderMonitor.
    
    This class is designed to be inherited by OrderMonitor. It uses:
    - self._adjusting_tokens
    - self._hedge_fair_cap_warned
    - self.executor
    - self.config
    """
    
    async def _handle_hedge_order(
        self,
        token_id: str,
        order_id: str,
        order_info: Optional[Dict[str, Any]],
        our_price: float,
        our_fair: float,
        best_bid: float,
        bids: list,
        is_outbid: bool,
        watcher,
        position: Optional["Position"] = None,
    ):
        """Handle hedge order monitoring - never overpay, cap at fair value."""
        # Skip if already adjusting this token (prevent duplicates)
        if token_id in self._adjusting_tokens:
            return
        
        bot_state = get_bot_state()
        match = bot_state.get_match_by_token(token_id)
        
        # Get entry price for profitability calculation
        entry_price = None
        if match:
            if match.token1 == token_id and match.position2:
                entry_price = match.position2.avg_price
            elif match.token2 == token_id and match.position1:
                entry_price = match.position1.avg_price
        
        if not entry_price:
            return  # No entry position found
        
        team_name = order_info.get("team_name", "") if order_info else (position.entry_team if position else "")
        
        # If we're overpaying (price > fair), adjust down to fair value
        if our_price > our_fair:
            new_hedge_price = round(our_fair, 2)
            if new_hedge_price < our_price:
                # Mark as adjusting to prevent duplicates
                self._adjusting_tokens.add(token_id)
                # RACE FIX: Guard order_id so WS cancel handler doesn't clear_order() mid-replace
                guarded_order_id = order_id
                bot_state._replacing_order_ids.add(guarded_order_id)
                try:
                    cancelled, was_already_complete = await self.executor.cancel_order(order_id, force=True, team_name=team_name)
                    if cancelled and was_already_complete:
                        # Order already gone — release guard and clean up BotState
                        bot_state._replacing_order_ids.discard(guarded_order_id)
                        guarded_order_id = None
                        bot_state.clear_order(order_id)
                    elif cancelled and not was_already_complete:
                        order_size = order_info.get("size", self.config["default_shares"]) if order_info else self.config["default_shares"]
                        new_order = await self.executor.place_limit_order(
                            token_id=token_id,
                            side=OrderSide.BUY,
                            price=new_hedge_price,
                            size=order_size,
                            team_name=team_name,
                        )
                        if new_order and new_order.status != OrderStatus.FAILED:
                            print(f"   📉 Hedge overpaying: {team_name} {our_price:.2f} → {new_hedge_price:.2f} (fair: {our_fair:.0%})")
                            bot_state.replace_order(order_id, new_order.order_id, new_hedge_price, token_id=token_id)
                        else:
                            print(f"   ⚠️ Failed to place adjusted hedge order")
                            bot_state._replacing_order_ids.discard(guarded_order_id)
                            guarded_order_id = None
                            bot_state.clear_order(order_id)
                except Exception as e:
                    print(f"   ⚠️ Failed to adjust hedge: {e}")
                finally:
                    self._adjusting_tokens.discard(token_id)
                    if guarded_order_id:
                        bot_state._replacing_order_ids.discard(guarded_order_id)
            return
        # Try price improvement if we're the best bid with a gap
        # (Hedges also benefit from lower prices!)
        if not is_outbid:
            # Are we the best bid?
            we_are_best_bid = abs(our_price - best_bid) < 0.005
            if not we_are_best_bid:
                return  # Nothing to do
            
            # Find second-best bid
            second_best_bid = 0.0
            for bid in bids:
                bid_price = float(bid.get("price", 0))
                if bid_price < our_price - 0.005:
                    second_best_bid = bid_price
                    break
            
            if second_best_bid <= 0:
                return  # No second bid to improve against
            
            # Only try to improve if gap is significant (> 3¢)
            gap_to_second = our_price - second_best_bid
            if gap_to_second <= 0.03:
                return  # Gap too small
            
            # New price = just above second-best bid
            improved_price = second_best_bid + 0.01
            

            # Calculate profit at improved price
            total_cost = entry_price + improved_price
            profit = 1.0 - total_cost
            profit_pct = (profit / total_cost) * 100 if total_cost > 0 else 0
            
            # Check if still profitable enough (min_profit is typically 1%)
            if profit_pct < self.config["min_profit"] * 100:
                return  # Improved price not profitable enough
            
            # Check the price improvement is meaningful (at least 2¢ improvement)
            if our_price - improved_price < 0.02:
                return  # Not enough improvement to bother
            
            # Execute price improvement!
            self._adjusting_tokens.add(token_id)
            # RACE FIX: Guard order_id so WS cancel handler doesn't clear_order() mid-replace
            guarded_order_id = order_id
            bot_state._replacing_order_ids.add(guarded_order_id)
            try:
                cancelled, was_already_complete = await self.executor.cancel_order(order_id, force=True, team_name=team_name)
                if cancelled and was_already_complete:
                    # Order already gone — release guard and clean up BotState
                    bot_state._replacing_order_ids.discard(guarded_order_id)
                    guarded_order_id = None
                    bot_state.clear_order(order_id)
                elif cancelled and not was_already_complete:
                    order_size = order_info.get("size", self.config["default_shares"]) if order_info else self.config["default_shares"]
                    new_order = await self.executor.place_limit_order(
                        token_id=token_id,
                        side=OrderSide.BUY,
                        price=improved_price,
                        size=order_size,
                        team_name=team_name,
                    )
                    if new_order and new_order.status != OrderStatus.FAILED:
                        print(f"   💰 Hedge price improved: {team_name} {our_price:.2f} → {improved_price:.2f} (profit: {profit_pct:.1f}%)")
                        bot_state.replace_order(order_id, new_order.order_id, improved_price, token_id=token_id)
                    else:
                        print(f"   ⚠️ Failed to place improved hedge order")
                        bot_state._replacing_order_ids.discard(guarded_order_id)
                        guarded_order_id = None
                        bot_state.clear_order(order_id)
            except Exception as e:
                print(f"   ⚠️ Failed to improve hedge price: {e}")
            finally:
                self._adjusting_tokens.discard(token_id)
                if guarded_order_id:
                    bot_state._replacing_order_ids.discard(guarded_order_id)
            return
        
        # Calculate new entry price for adjustment
        new_entry = best_bid + 0.01
        
        # CRITICAL: Never chase hedge orders above fair value!
        if new_entry > our_fair:
            new_entry = round(our_fair, 2)
            
            # Skip if already at fair value (avoid useless cancel+replace)
            if abs(new_entry - our_price) < 0.005:
                return
            
            # Only log once per token to avoid spam
            if token_id not in self._hedge_fair_cap_warned:
                self._hedge_fair_cap_warned.add(token_id)
        
        # CRITICAL: Skip if new price is same as current price (avoid duplicate orders!)
        if abs(new_entry - our_price) < 0.005:
            return  # Already at this price, no adjustment needed
        
        # Check if still profitable
        total_cost = entry_price + new_entry
        profit = 1.0 - total_cost
        profit_pct = (profit / total_cost) * 100 if total_cost > 0 else 0
        
        if profit_pct < self.config["min_profit"] * 100:
            return  # Not profitable enough
        
        # Skip if already adjusting this token (prevent duplicates)
        if token_id in self._adjusting_tokens:
            return
        
        # Execute adjustment
        print(f"   🔒 Hedge adjustment: entry @ {entry_price:.2f} + hedge @ {new_entry:.2f} = {profit_pct:.1f}% profit")
        
        await self._execute_adjustment(
            token_id=token_id,
            order_id=order_id,
            order_info=order_info,
            new_price=new_entry,
            our_fair=our_fair,
            watcher=watcher,
            position=position,
            reason="outbid",
            stored_team=team_name,
        )
    
    def _handle_hedge_ws_fast(
        self,
        token_id: str,
        new_entry: float,
        our_fair: float,
        our_price: float,
        order_info: Dict[str, Any],
        best_bid: float,
    ) -> tuple:
        """
        Handle hedge-specific logic in fast WS adjustment.
        
        Returns: (should_adjust, new_entry, new_edge) or (False, None, None) if should not adjust.
        """
        # CRITICAL: Never chase hedge orders above fair value!
        if new_entry > our_fair:
            new_entry = round(our_fair, 2)
            new_edge = our_fair - new_entry
            
            # Skip if already at fair value (avoid useless cancel+replace)
            if abs(new_entry - our_price) < 0.005:
                return (False, None, None)
            
            # Only log once per token to avoid spam
            if token_id not in self._hedge_fair_cap_warned:
                team_name = order_info.get("team_name", token_id[:12]) if order_info else token_id[:12]
                self._hedge_fair_cap_warned.add(token_id)
        else:
            new_edge = our_fair - new_entry
        
        # Get entry price from BotState
        bot_state = get_bot_state()
        match = bot_state.get_match_by_token(token_id)
        if not match:
            return (False, None, None)
        
        entry_price = None
        if match.token1 == token_id and match.position2:
            entry_price = match.position2.avg_price
        elif match.token2 == token_id and match.position1:
            entry_price = match.position1.avg_price
        
        if not entry_price:
            return (False, None, None)
        
        total_cost = entry_price + new_entry
        profit = 1.0 - total_cost
        profit_pct = (profit / total_cost) * 100 if total_cost > 0 else 0
        
        if profit_pct >= self.config["min_profit"] * 100:
            return (True, new_entry, new_edge)
        else:
            return (False, None, None)
