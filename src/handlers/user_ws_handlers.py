"""
User WebSocket Handlers - Processes real-time order/fill events from Polymarket.

This module handles:
- Order status updates (fills, cancellations)
- Trade notifications
- Triggering callbacks for position updates
"""
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from src.state.bot_state import BotState


class HydratedFill:
    """Helper class representing a fill on a hydrated (previous session) order."""
    
    def __init__(self, order_info: dict, update, edge: Optional[float], fair_val: Optional[float], match: str):
        self.position_id = f"HYDRATED-{order_info.get('token_id', '')[:8]}"
        self.entry_team = order_info.get("team_name", "Unknown")
        self.entry_price = update.price
        self.entry_filled_shares = update.size_matched
        self.entry_size = order_info.get("size", update.size_matched)
        self.is_hydrated = True
        self.edge_pct = edge
        self.fair_value = fair_val
        self.match_name = match
        # Add match context for accurate fair value lookup
        self.entry_token_id = order_info.get("token_id", "")
        self.match_id = order_info.get("match_id", "")
        # CRITICAL: Required for database recording in fill_handlers.py
        self.order_id = order_info.get("order_id", "")
        # Game and team info for filled_orders recording
        self.game = order_info.get("game", "")
        self.team1 = order_info.get("team1", "")
        self.team2 = order_info.get("team2", "")
        # Condition ID and placement time for complete fill records
        self.condition_id = order_info.get("condition_id", "")
        self.placed_at = order_info.get("placed_at")  # datetime or None


class UserWebSocketHandler:
    """
    Handles real-time order updates from Polymarket User WebSocket.
    
    Processes fill events and triggers callbacks for position management.
    
    Args:
        bot_state: BotState instance for order lookups
        get_position_fn: Callback to get Position by position_id
        order_to_position_map: Dict mapping order_id -> position_id
        on_fill: Callback for entry fills
        on_hedge_fill: Callback for hedge fills
    """
    
    def __init__(
        self,
        bot_state: "BotState",
        get_position_fn: Callable[[str], Optional[object]],
        order_to_position_map: dict,
        on_fill: Optional[Callable] = None,
        on_hedge_fill: Optional[Callable] = None,
    ):
        self.bot_state = bot_state
        self._get_position = get_position_fn
        self._order_to_position = order_to_position_map
        self.on_fill = on_fill
        self.on_hedge_fill = on_hedge_fill
        self._user_ws = None
    
    def set_user_websocket(self, user_ws):
        """Set the user WebSocket and register handlers."""
        self._user_ws = user_ws
        user_ws.on_order_update = self._handle_ws_order_update
        user_ws.on_trade = self._handle_ws_trade
    
    async def _handle_ws_order_update(self, order_update):
        """Handle real-time order update from WebSocket."""
        order_id = order_update.order_id
        
        # Find the position for this order
        position_id = self._order_to_position.get(order_id)
        
        # Check if this is a hydrated order (from previous session)
        hydrated_order = None
        if not position_id:
            hydrated_order = self.bot_state.get_order_info_by_id(order_id)
            if not hydrated_order:
                return  # Not one of our orders
        
        # Handle hydrated order fill
        if hydrated_order:
            status = order_update.status.upper() if hasattr(order_update, 'status') else ""
            if status in ("CANCELED", "CANCELLED", "INVALID"):
                self.bot_state.clear_order(order_id)
            
            if order_update.size_matched > 0:
                team_name = hydrated_order.get("team_name", "Unknown")
                price = order_update.price
                filled = order_update.size_matched
                is_partial = order_update.is_partial
                fair_value = hydrated_order.get("fair_value")
                match_name = hydrated_order.get("match", "")
                
                edge_pct = None
                if fair_value:
                    edge_pct = (fair_value - price) * 100
                
                fill_type = "Partial fill" if is_partial else "Fill"
                edge_str = f" (edge: {edge_pct:+.1f}%)" if edge_pct is not None else ""
                print(f"📡 WS {fill_type}: {team_name} - {filled} shares @ {price:.2f}{edge_str} (hydrated order)")
                
                self.bot_state.update_order_fill(order_id, filled, price)
                
                if self.on_fill:
                    await self.on_fill(
                        HydratedFill(hydrated_order, order_update, edge_pct, fair_value, match_name),
                        filled
                    )
            return
        
        position = self._get_position(position_id)
        if not position:
            return
        
        # Update BotState for status changes
        status = order_update.status.upper() if hasattr(order_update, 'status') else ""
        if status in ("CANCELED", "CANCELLED", "INVALID"):
            self.bot_state.cancel_order(order_id)
        
        # Import PositionState here to avoid circular import
        from src.execution.position import PositionState
        
        # Update entry order
        if position.entry_order and position.entry_order.order.order_id == order_id:
            old_filled = position.entry_order.filled_shares
            new_filled = order_update.size_matched
            
            if new_filled > old_filled:
                position.entry_order.filled_shares = new_filled
                position.entry_filled_shares = new_filled
                position.entry_avg_price = order_update.price
                
                new_fills = new_filled - old_filled
                print(f"   ✅ Entry fill: {new_fills} shares @ {order_update.price:.2f}")
                
                self.bot_state.update_order_fill(order_id, new_filled, order_update.price)
                
                if order_update.is_filled:
                    position.state = PositionState.POSITION_OPEN
                elif order_update.is_partial:
                    position.state = PositionState.ENTRY_PARTIAL
                
                if self.on_fill:
                    await self.on_fill(position, new_fills)
        
        # Update hedge order
        elif position.hedge_order and position.hedge_order.order.order_id == order_id:
            old_filled = position.hedge_order.filled_shares
            new_filled = order_update.size_matched
            
            if new_filled > old_filled:
                position.hedge_order.filled_shares = new_filled
                position.hedge_filled_shares = new_filled
                position.hedge_avg_price = order_update.price
                
                new_fills = new_filled - old_filled
                print(f"   ✅ Hedge fill: {new_fills} shares @ {order_update.price:.2f}")
                
                self.bot_state.update_order_fill(order_id, new_filled, order_update.price)
                
                if order_update.is_filled:
                    position.state = PositionState.HEDGED
                    print(f"   🎉 Position {position.position_id} fully hedged!")
                    print(f"      Profit: ${position.locked_profit:.2f} ({position.profit_percent:.1f}%)")
                elif order_update.is_partial:
                    position.state = PositionState.HEDGE_PARTIAL
                
                if self.on_hedge_fill:
                    await self.on_hedge_fill(position, new_fills)
    
    async def _handle_ws_trade(self, trade_update):
        """Handle real-time trade notification from WebSocket."""
        order_id = trade_update.order_id
        position_id = self._order_to_position.get(order_id)
        
        if position_id:
            print(f"📡 WS Trade: {trade_update.size} @ {trade_update.price:.2f}")
