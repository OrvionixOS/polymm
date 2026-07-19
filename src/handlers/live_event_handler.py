"""
Live event handler - cancels orders on events that have gone live.
"""
from src.execution.order_executor import OrderExecutor
from src.services.telegram_alerts import TelegramAlerts
from src.state.bot_state import get_bot_state


class LiveEventHandler:
    """
    Handles detection and cancellation of orders on live events.
    
    Live events have stale odds (scraped every 2 min) so we shouldn't
    have active orders on them.
    
    Requires injection of:
    - executor: OrderExecutor instance
    - alerts: TelegramAlerts instance
    """
    
    def __init__(
        self,
        executor: OrderExecutor,
        alerts: TelegramAlerts,
    ):
        self.executor = executor
        self.alerts = alerts
        # Track order IDs we've already cancelled to avoid repeated attempts
        self._cancelled_live_order_ids: set = set()
    
    async def cancel_orders_on_live_events(
        self,
        live_events: list,
        poly_client,
        watcher,  # OrderWatcher - avoid circular import
    ):
        """
        Cancel all orders on events that have gone live.
        
        Live events have stale odds (scraped every 2 min) so we shouldn't 
        have active orders on them.
        """
        if not live_events:
            return
        
        # Get token IDs from live events
        live_token_ids = set()
        live_event_titles = {}  # token_id -> event title for logging
        
        for event in live_events:
            # Hydrate to get market tokens if needed
            if not event.markets:
                event = await poly_client.hydrate_event(event)
            
            if event.markets:
                for market in event.markets:
                    for token_id in market.get("clobTokenIds", []):
                        live_token_ids.add(token_id)
                        live_event_titles[token_id] = event.title
        
        if not live_token_ids:
            return
        
        # Check our active orders against live tokens
        cancelled_count = 0
        
        # Check open orders via BotState (single source of truth)
        # This now covers all orders - both hydrated and session-created
        bot_state = get_bot_state()
        open_orders = bot_state.get_all_open_order_infos()
        for token_id, order_info in list(open_orders.items()):
            if token_id in live_token_ids:
                order_id = order_info.get("order_id")
                if order_id:
                    # Skip if we've already cancelled this order (prevents spam)
                    if order_id in self._cancelled_live_order_ids:
                        continue
                    
                    try:
                        team_name = order_info.get("team", "")
                        cancelled, _ = await self.executor.cancel_order(order_id, force=True, team_name=team_name)
                        if cancelled:
                            print(f"🔴 LIVE: Cancelled order on {live_event_titles.get(token_id, 'live event')}")
                            bot_state.clear_order(order_id)  # Removes from BotState tracking
                            # Track this order ID so we don't try to cancel it again
                            self._cancelled_live_order_ids.add(order_id)
                            cancelled_count += 1
                    except Exception as e:
                        print(f"⚠️ Failed to cancel live order: {e}")
        
        if cancelled_count > 0:
            await self.alerts.on_error(
                f"Cancelled {cancelled_count} orders on live events",
                "Live Event Protection"
            )
    
    async def cancel_orders_on_live_matches(
        self,
        live_match_ids: set[str],
    ):
        """
        Cancel orders on matches detected as live via esports_odds_live table.
        
        This is a secondary live detection mechanism - if a match exists in
        esports_odds_live, we consider it live regardless of Polymarket's is_live flag.
        
        Also emits MATCH_GONE_LIVE events for reactive handling downstream.
        
        Args:
            live_match_ids: Set of match_ids from esports_odds_live table
        """
        if not live_match_ids:
            return
        
        from src.state.bot_state import get_bot_state, StateEventType
        
        bot_state = get_bot_state()
        cancelled_count = 0
        
        for match_id in live_match_ids:
            match = bot_state.get_match(match_id)
            if not match:
                continue
            
            # Check for open orders on this match
            orders_to_cancel = []
            if match.order1 and match.order1.is_open:
                orders_to_cancel.append((match.order1, match.team1 or "Team1"))
            if match.order2 and match.order2.is_open:
                orders_to_cancel.append((match.order2, match.team2 or "Team2"))
            
            if not orders_to_cancel:
                continue
            
            # Cancel each order
            for order, team_name in orders_to_cancel:
                # Skip if already cancelled
                if order.order_id in self._cancelled_live_order_ids:
                    continue
                
                try:
                    cancelled, _ = await self.executor.cancel_order(
                        order.order_id, 
                        force=True, 
                        team_name=team_name
                    )
                    if cancelled:
                        print(f"🔴 LIVE (esports_odds_live): Cancelled order on {team_name} ({match_id})")
                        bot_state.clear_order(order.order_id)
                        self._cancelled_live_order_ids.add(order.order_id)
                        cancelled_count += 1
                except Exception as e:
                    print(f"⚠️ Failed to cancel live order: {e}")
            
            # Emit MATCH_GONE_LIVE event for reactive handling
            # This ensures downstream handlers (ReactiveHandler) also get notified
            bot_state.emit(StateEventType.MATCH_GONE_LIVE, match_state=match)
        
        if cancelled_count > 0:
            await self.alerts.on_error(
                f"Cancelled {cancelled_count} orders on live matches (esports_odds_live)",
                "Live Match Protection"
            )

