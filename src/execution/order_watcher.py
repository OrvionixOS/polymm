"""
Order Watcher - Monitors active orders and manages the order lifecycle.

Handles:
- Tracking active entry and hedge orders
- Detecting fills (full and partial)
- Triggering hedge orders when entry fills
- Adjusting bids when outbid
- Validating edge is still valid
- Match-level awareness via BotState (natural arb detection)
"""
import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Callable, Set
from enum import Enum

from .order_executor import OrderExecutor, Order, OrderSide, OrderStatus
from src.data.recorder import get_recorder
from src.state.bot_state import BotState, get_bot_state, MatchState
from src.core.config import CONFIG
from src.core.match_id import is_individual_game_market

# Import from split modules (backward compatibility maintained via __init__.py)
from .position import (
    OrderType, PositionState, WatchedOrder, Position,
)

# Import hydration functions
from .hydration import (
    hydrate_active_orders as _hydrate_active_orders,
    hydrate_filled_positions as _hydrate_filled_positions,
    print_hydrated_orders as _print_hydrated_orders,
)

# Import WS handler
from src.handlers.user_ws_handlers import UserWebSocketHandler


class OrderWatcher:
    """
    Monitors active orders and manages position lifecycle.
    
    Responsibilities:
    - Track all active entry and hedge orders
    - Receive real-time updates via WebSocket (or poll as fallback)
    - Detect fills and trigger hedging
    - Adjust bids when outbid (if edge still valid)
    - Cancel orders when edge is lost
    """
    
    def __init__(
        self,
        executor: OrderExecutor,
        on_fill_callback: Optional[Callable] = None,
        on_hedge_fill_callback: Optional[Callable] = None,
        config: Optional[Dict] = None,
        bot_state: Optional[BotState] = None,
    ):
        self.executor = executor
        self.on_fill = on_fill_callback
        self.on_hedge_fill = on_hedge_fill_callback
        if config is None:
            raise ValueError("config is required for OrderWatcher")
        self.config = config
        
        # ===== SINGLE SOURCE OF TRUTH: BotState =====
        # All order/position tracking is delegated to BotState.
        # OrderWatcher only keeps session-specific state (Position objects, callbacks).
        self.bot_state = bot_state or get_bot_state()
        
        # Session-specific Position objects (not in BotState - has lifecycle state)
        self._positions: Dict[str, Position] = {}
        self._order_to_position: Dict[str, str] = {}  # order_id -> position_id
        self._running = False
        self._position_counter = 0
        
        # WebSocket for real-time updates (optional)
        self._user_ws = None
        
        # NOTE: _active_token_ids and _filled_token_ids REMOVED
        # Use self.bot_state.is_token_active() and related methods instead
        
        # NOTE: _hydrated_orders and _hydrated_order_id_map REMOVED
        # BotState is now the single source of truth for order info.
        # Use: bot_state.get_order_info(token_id)
        #      bot_state.get_order_info_by_id(order_id)
        #      bot_state.get_all_open_order_infos()
        #      bot_state.get_summary_stats()  # For Telegram summary
        #      bot_state.set_summary_stats()  # Called during hydration
        
        # Hydration stats (only track what can't be derived)
        self._hydrated_duplicate_count = 0
        
    async def hydrate_active_orders(self, poly_client=None, odds_service=None, quiet=False, skip_individual_game_filter=False):
        """Fetch open orders from Polymarket - delegates to hydration module."""
        self._hydrated_duplicate_count = await _hydrate_active_orders(
            bot_state=self.bot_state,
            executor=self.executor,
            poly_client=poly_client,
            odds_service=odds_service,
            get_position_order_ids_fn=self.get_position_order_ids,
            quiet=quiet,
            skip_individual_game_filter=skip_individual_game_filter,
        )
    
    def print_hydrated_orders(self, hedge_tokens: set = None, quiet: bool = False, live_only: bool = False, odds_service=None):
        """Print hydrated orders with hedge status - delegates to hydration module."""
        _print_hydrated_orders(
            bot_state=self.bot_state,
            hedge_tokens=hedge_tokens,
            duplicate_count=self._hydrated_duplicate_count,
            quiet=quiet,
            live_only=live_only,
            odds_service=odds_service,
        )
    
    async def hydrate_filled_positions(self, poly_client=None, odds_service=None, quiet=False, live_only=False):
        """Fetch filled positions from Polymarket - delegates to hydration module."""
        await _hydrate_filled_positions(
            bot_state=self.bot_state,
            executor=self.executor,
            poly_client=poly_client,
            odds_service=odds_service,
            quiet=quiet,
            live_only=live_only,
        )

    def is_token_active(self, token_id: str) -> bool:
        """Check if we have an active order OR filled position for this token."""
        return self.bot_state.is_token_active(token_id)
    
    def get_positions_needing_hedge(self) -> Dict[str, dict]:
        """Get all filled positions that need hedges."""
        return self.bot_state.get_positions_needing_hedge()
    
    def set_user_websocket(self, user_ws):
        """Set the user WebSocket for real-time order updates."""
        self._user_ws = user_ws
        # Create handler and wire it up
        self._ws_handler = UserWebSocketHandler(
            bot_state=self.bot_state,
            get_position_fn=lambda pid: self._positions.get(pid),
            order_to_position_map=self._order_to_position,
            on_fill=self.on_fill,
            on_hedge_fill=self.on_hedge_fill,
        )
        self._ws_handler.set_user_websocket(user_ws)
    
    def _generate_position_id(self) -> str:
        self._position_counter += 1
        return f"POS-{self._position_counter:04d}"
    
    async def create_entry_position(
        self,
        match_id: str,
        game: str,
        team1: str,
        team2: str,
        entry_team: str,
        entry_token_id: str,
        hedge_team: str,
        hedge_token_id: str,
        entry_price: float,
        fair_value: float,
        shares: int = 5,
        condition_id: str = "",
        trading_deadline: "Optional[datetime]" = None,
    ) -> Optional[Position]:
        """
        Create a new position and place the entry order.
        
        Args:
            match_id: Unique match identifier
            game: Game type (cs2, dota2, lol)
            team1, team2: Team names
            entry_team: Team we're betting on
            entry_token_id: Polymarket token ID for entry team
            hedge_team: Opposite team (for hedging later)
            hedge_token_id: Polymarket token ID for hedge team
            entry_price: Limit price for entry
            fair_value: Bookmaker fair probability
            shares: Number of shares (default 5 = minimum)
            condition_id: Polymarket condition ID (for cross-referencing)
            
        Returns:
            Position if created, None if token already active
        """
        # NOTE: The caller (_execute_opportunity) has already reserved this token
        # via bot_state.reserve_token() before calling this function.
        # We skip the is_token_active check here since reservation guarantees exclusivity.
        
        position_id = self._generate_position_id()
        
        try:
            # Place entry order
            order = await self.executor.place_limit_order(
                token_id=entry_token_id,
                side=OrderSide.BUY,
                price=entry_price,
                size=shares,
                team_name=entry_team,
            )
        except Exception as e:
            # If order fails, release reservation
            self.bot_state.unreserve_token(entry_token_id)
            raise e
        
        # Check if order actually succeeded (place_limit_order may return FAILED status)
        if order.status == OrderStatus.FAILED:
            self.bot_state.unreserve_token(entry_token_id)
            print(f"   ⚠️ Order placement failed, not creating position")
            return None
        
        # Record order placement for lifecycle tracking
        try:
            recorder = get_recorder()
            await recorder.record_order_placed(
                order_id=order.order_id,
                token_id=entry_token_id,
                condition_id=None,  # Could be added if available
                match_id=match_id,
                team=entry_team,
                price=entry_price,
                fair_value=fair_value,
            )
        except Exception as e:
            print(f"⚠️ Failed to record order placement: {e}")
        
        # Create watched order
        watched = WatchedOrder(
            order=order,
            order_type=OrderType.ENTRY,
            token_id=entry_token_id,
            team_name=entry_team,
            fair_value=fair_value,
            placed_at=datetime.now(timezone.utc),
        )
        
        # Create position
        # Use passed condition_id, or fallback to BotState if not provided
        if not condition_id:
            match_state = self.bot_state.get_match(match_id)
            condition_id = match_state.condition_id if match_state else ""
        
        position = Position(
            position_id=position_id,
            match_id=match_id,
            game=game,
            team1=team1,
            team2=team2,
            entry_team=entry_team,
            entry_token_id=entry_token_id,
            condition_id=condition_id,
            entry_order=watched,
            hedge_team=hedge_team,
            hedge_token_id=hedge_token_id,
            state=PositionState.ENTRY_PENDING,
        )
        
        self._positions[position_id] = position
        self._order_to_position[order.order_id] = position_id
        
        # Register with BotState for match-level tracking
        # For rugby "No" tokens (e.g., "Pau No"), strip " No" for team matching
        entry_team_base = entry_team[:-3] if entry_team.endswith(" No") else entry_team
        is_entry_team1 = entry_team_base == team1 or entry_team == team1
        
        self.bot_state.register_match(
            match_id=match_id,
            game=game,
            team1=team1,
            team2=team2,
            token1=entry_token_id if is_entry_team1 else hedge_token_id,
            token2=hedge_token_id if is_entry_team1 else entry_token_id,
            trading_deadline=trading_deadline,
        )
        self.bot_state.register_order(
            match_id=match_id,
            order_id=order.order_id,
            token_id=entry_token_id,
            team=entry_team,
            price=entry_price,
            size=shares,
            is_entry=True,
            fair_value=fair_value,
        )
        
        print(f"📝 Created position {position_id}: {entry_team} @ {entry_price:.2f}")
        return position
    
    async def place_hedge_order(
        self,
        position: Position,
        hedge_price: float,
        shares: Optional[float] = None,
    ) -> Optional[WatchedOrder]:
        """
        Place a hedge order for a position.
        
        Args:
            position: The position to hedge
            hedge_price: Limit price for hedge
            shares: Number of shares (defaults to entry filled shares)
        """
        if shares is None:
            shares = position.entry_filled_shares
        
        # Minimum 5 shares to hedge (Polymarket min order size)
        min_hedge_shares = 5
        if shares < min_hedge_shares:
            print(f"⚠️ Cannot hedge {shares} shares (min {min_hedge_shares})")
            return None
        
        # Get fair value for hedge team from bookmakers
        # TODO: Fetch from bookmaker aggregator
        hedge_fair = 1.0 - position.entry_order.fair_value if position.entry_order else 0.5
        
        order = await self.executor.place_limit_order(
            token_id=position.hedge_token_id,
            side=OrderSide.BUY,
            price=hedge_price,
            size=shares,
            team_name=position.hedge_team,
        )
        
        # Check if order actually succeeded
        if order.status == OrderStatus.FAILED:
            print(f"   ⚠️ Hedge order placement failed")
            return None
        
        # Record hedge order placement
        try:
            recorder = get_recorder()
            await recorder.record_order_placed(
                order_id=order.order_id,
                token_id=position.hedge_token_id,
                condition_id=None,
                match_id=position.match_id,
                team=position.hedge_team,
                price=hedge_price,
                fair_value=hedge_fair,
            )
        except Exception as e:
            print(f"⚠️ Failed to record hedge order placement: {e}")
        
        watched = WatchedOrder(
            order=order,
            order_type=OrderType.HEDGE,
            token_id=position.hedge_token_id,
            team_name=position.hedge_team,
            fair_value=hedge_fair,
            placed_at=datetime.now(timezone.utc),
        )
        
        position.hedge_order = watched
        position.state = PositionState.HEDGE_PENDING
        self._order_to_position[order.order_id] = position.position_id
        
        # Register hedge order with BotState
        self.bot_state.register_order(
            match_id=position.match_id,
            order_id=order.order_id,
            token_id=position.hedge_token_id,
            team=position.hedge_team,
            price=hedge_price,
            size=shares,
            is_entry=False,
        )
        
        print(f"📝 Placed hedge for {position.position_id}: {position.hedge_team} @ {hedge_price:.2f}")
        return watched
    
    async def check_order_status(self, position: Position) -> bool:
        """
        Check and update order status for a position.
        Returns True if state changed.
        """
        changed = False
        
        # Check entry order
        if position.entry_order and not position.entry_order.order.is_complete:
            updated = await self.executor.get_order_status(position.entry_order.order.order_id)
            if updated:
                old_filled = position.entry_order.filled_shares
                position.entry_order.filled_shares = updated.filled_size
                position.entry_order.order = updated
                position.entry_order.last_checked = datetime.utcnow()
                
                if updated.filled_size > old_filled:
                    # New fill!
                    new_fills = updated.filled_size - old_filled
                    position.entry_filled_shares = updated.filled_size
                    position.entry_avg_price = updated.avg_fill_price or position.entry_order.order.price
                    
                    print(f"✅ Entry fill: {new_fills} shares @ {position.entry_avg_price:.2f}")
                    
                    # Update state
                    if updated.status == OrderStatus.FILLED:
                        position.state = PositionState.POSITION_OPEN
                    elif updated.status == OrderStatus.PARTIAL:
                        position.state = PositionState.ENTRY_PARTIAL
                    
                    # Trigger hedge callback
                    if self.on_fill:
                        await self.on_fill(position, new_fills)
                    
                    changed = True
        
        # Check hedge order
        if position.hedge_order and not position.hedge_order.order.is_complete:
            updated = await self.executor.get_order_status(position.hedge_order.order.order_id)
            if updated:
                old_filled = position.hedge_order.filled_shares
                position.hedge_order.filled_shares = updated.filled_size
                position.hedge_order.order = updated
                position.hedge_order.last_checked = datetime.utcnow()
                
                if updated.filled_size > old_filled:
                    new_fills = updated.filled_size - old_filled
                    position.hedge_filled_shares = updated.filled_size
                    position.hedge_avg_price = updated.avg_fill_price or position.hedge_order.order.price
                    
                    print(f"✅ Hedge fill: {new_fills} shares @ {position.hedge_avg_price:.2f}")
                    
                    # Update state
                    if updated.status == OrderStatus.FILLED:
                        position.state = PositionState.HEDGED
                        print(f"🎉 Position {position.position_id} fully hedged! Profit: ${position.locked_profit:.2f} ({position.profit_percent:.1f}%)")
                    elif updated.status == OrderStatus.PARTIAL:
                        position.state = PositionState.HEDGE_PARTIAL
                    
                    changed = True
        
        return changed
    
    async def check_outbid(
        self,
        position: Position,
        current_best_bid: float,
        bid_size_usd: float,
    ) -> bool:
        """
        Check if we've been significantly outbid and adjust if edge still valid.
        
        Returns True if we adjusted our bid.
        """
        if not position.entry_order or position.entry_order.order.is_complete:
            return False
        
        our_bid = position.entry_order.order.price
        fair = position.entry_order.fair_value
        min_edge = self.config["min_edge"]
        
        # Check if outbid by significant amount
        if current_best_bid <= our_bid:
            return False  # We're still best or tied
        
        if bid_size_usd < self.config["outbid_threshold"]:
            return False  # Outbid is too small to care
        
        # Calculate new bid (1¢ above competitor)
        new_bid = current_best_bid + self.config["adjust_increment"]
        max_bid = fair - min_edge  # Edge limit
        
        if new_bid > max_bid:
            print(f"⚠️ Outbid but cannot adjust (edge would be {(fair - new_bid)*100:.1f}% < {min_edge*100:.0f}%)")
            return False
        
        # Cancel old order and place new one
        cancelled, was_already_complete = await self.executor.cancel_order(position.entry_order.order.order_id, team_name=position.entry_team)
        
        # Don't place new order if original was already gone
        if was_already_complete:
            print(f"⚠️ Order already complete, skipping adjustment")
            return False
        
        new_order = await self.executor.place_limit_order(
            token_id=position.entry_token_id,
            side=OrderSide.BUY,
            price=new_bid,
            size=position.entry_order.order.size,
            team_name=position.entry_team,
        )
        
        # Update watched order
        del self._order_to_position[position.entry_order.order.order_id]
        position.entry_order.order = new_order
        self._order_to_position[new_order.order_id] = position.position_id
        
        print(f"📈 Adjusted bid: {our_bid:.2f} → {new_bid:.2f} (edge: {(fair - new_bid)*100:.1f}%)")
        return True
    
    async def check_edge_valid(self, position: Position, new_fair_value: float) -> bool:
        """
        Check if our edge is still valid with updated fair value.
        Cancels order if edge is lost.
        
        Returns True if edge is still valid.
        """
        if not position.entry_order or position.entry_order.order.is_complete:
            return True
        
        our_bid = position.entry_order.order.price
        min_edge = self.config["min_edge"]
        
        edge = new_fair_value - our_bid
        
        if edge < min_edge:
            # Edge lost - cancel order
            print(f"⚠️ Edge lost! Fair value {new_fair_value:.2f}, our bid {our_bid:.2f}, edge {edge*100:.1f}%")
            cancelled, _ = await self.executor.cancel_order(position.entry_order.order.order_id)
            if cancelled:
                position.state = PositionState.CANCELLED
            return False
        
        # Update fair value
        position.entry_order.fair_value = new_fair_value
        return True
    
    def get_position(self, position_id: str) -> Optional[Position]:
        return self._positions.get(position_id)
    
    def get_all_positions(self) -> List[Position]:
        return list(self._positions.values())
    
    def get_open_positions(self) -> List[Position]:
        """Get positions that are not fully hedged or cancelled."""
        return [
            p for p in self._positions.values()
            if p.state not in [PositionState.HEDGED, PositionState.CANCELLED]
        ]
    
    def get_position_order_ids(self) -> set:
        """Get all active order IDs from all tracked sources.
        
        Includes:
        - Session positions (OrderWatcher._positions)
        - BotState orders (registered by HedgeSeeker, spread scanner, etc.)
        
        This prevents hydrate_active_orders from cancelling valid orders
        as duplicates during periodic state resync.
        """
        order_ids = set()
        for p in self._positions.values():
            if p.state in [PositionState.HEDGED, PositionState.CANCELLED]:
                continue
            if p.entry_order and p.entry_order.order and p.entry_order.order.order_id:
                order_ids.add(p.entry_order.order.order_id)
            if p.hedge_order and p.hedge_order.order and p.hedge_order.order.order_id:
                order_ids.add(p.hedge_order.order.order_id)
        
        # Also include all orders tracked by BotState
        # (HedgeSeeker, spread scanner, etc. register orders directly in BotState
        #  without creating session Position objects)
        for token_id, info in self.bot_state.get_all_open_order_infos().items():
            oid = info.get("order_id", "")
            if oid:
                order_ids.add(oid)
        
        return order_ids
    
    def get_hedged_positions(self) -> List[Position]:
        """Get fully hedged positions."""
        return [p for p in self._positions.values() if p.state == PositionState.HEDGED]
    
    # NOTE: has_position_for_match() REMOVED
    # Use bot_state.has_exposure_for_match(match_id) directly instead
    
    def has_natural_arb(self, match_id: str) -> bool:
        """
        Check if this match has a natural arb (orders/positions on both sides).
        
        This is THE key check to prevent placing redundant hedge orders.
        If we have both sides covered, the other order will complete the arb.
        """
        match_state = self.bot_state.get_match(match_id)
        if match_state:
            return match_state.has_natural_arb
        return False
    
    def has_opposite_coverage(self, token_id: str) -> bool:
        """
        Check if we have coverage on the opposite side of this token's match.
        
        Returns True if we have an order or position on the other side,
        meaning no hedge is needed for this token.
        """
        return self.bot_state.has_opposite_coverage(token_id)
    
    def get_match_state(self, match_id: str) -> Optional[MatchState]:
        """Get the full match state from BotState."""
        return self.bot_state.get_match(match_id)