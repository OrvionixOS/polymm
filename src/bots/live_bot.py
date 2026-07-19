"""
Live Bot - Trades only LIVE esports matches.

Uses esports_odds_live table with:
- 30 second max odds freshness
- 5 second scan interval
- 8c minimum spread
- 10 second state re-sync
- 30 second book validation

Same arbitrage logic as SportsBot, just inverts the live filter.
"""
import asyncio
from typing import Optional
from datetime import datetime, timezone

from src.bots.base_bot import BaseBot
from src.scanning.live_opportunity_scanner import LiveOpportunityScanner
from src.core.config import LIVE_CONFIG


class LiveBot(BaseBot):
    """
    Trades only LIVE esports matches.
    
    Key differences from SportsBot:
    - Only scans live events (is_live=True)
    - Uses esports_odds_live table with 30s freshness
    - Min 8c spread (vs 7c for pre-match)
    - Smaller position sizes (live_shares=10)
    - Faster scan interval (5s vs 30s)
    - Faster re-sync (10s vs 60s)
    - Faster book validation (30s vs 5 min)
    - Does NOT manage pre-match positions
    - Uses SEPARATE Polymarket account for analytics isolation
    """
    
    # Use separate Polymarket account for LiveBot
    _private_key_env_var = "POLYMARKET_PRIVATE_KEY_LIVE"
    _funder_address_env_var = "POLYMARKET_FUNDER_ADDRESS_LIVE"
    
    def __init__(self):
        # CRITICAL: Set live_only_mode BEFORE super().__init__() so OddsService uses live table
        self._live_only_mode = True
        
        super().__init__()
        
        # Override state sync interval for live
        self._state_sync_interval = LIVE_CONFIG["state_sync_interval"]
        
        # Use different port to avoid conflict with SportsBot (8080)
        self.health_server._port = 8081
        
        # Strategy-specific: LiveOpportunityScanner with live odds
        self.scanner = LiveOpportunityScanner(
            odds_service=self.odds_service,
            cache=self.cache,
            hedge_finder=self.hedge_finder,
            config=LIVE_CONFIG,
        )
    
    # ===== Strategy Implementation =====
    
    async def _signal_loop(self):
        """Scan for live opportunities with 5s interval."""
        await asyncio.sleep(5)  # Brief wait for initial state
        
        while self._running:
            try:
                await self._scan_and_execute()
            except Exception as e:
                print(f"❌ [LIVE] Signal loop error: {e}")
                await self.alerts.on_error(str(e), "Live Signal Loop")
            
            await asyncio.sleep(LIVE_CONFIG["signal_scan_interval"])
    
    async def _scan_and_execute(self):
        """Scan for LIVE opportunities using the live scanner."""
        # Check balance first
        balance = await self._check_balance()
        if balance < self._min_balance_for_orders:
            if not self._low_balance_warned:
                print(f"⚠️ [LIVE] Low balance: ${balance:.2f}")
                await self.alerts.on_low_balance(balance, self._min_balance_for_orders)
                self._low_balance_warned = True
            return
        
        # CRITICAL: Cancel orders on FINISHED events before scanning
        await self._cancel_orders_on_finished_events()
        
        # Scan for ALL live opportunities
        opportunities = await self.scanner.scan_for_opportunities(
            watcher=self.watcher,
            cancel_live_orders_fn=None,  # Live bot doesn't cancel on live events
        )
        
        if opportunities:
            # Deduplicate opportunities by token_id BEFORE executing
            seen_tokens = set()
            unique_opportunities = []
            for opp in opportunities:
                token_id = opp.get("token_id")
                if token_id and token_id not in seen_tokens:
                    seen_tokens.add(token_id)
                    unique_opportunities.append(opp)
                elif token_id:
                    print(f"   ⚠️ [LIVE] Skipping duplicate opportunity for token {token_id[:12]}...")
            
            # Execute unique opportunities in parallel
            results = await asyncio.gather(*[
                self._execute_opportunity(opp) for opp in unique_opportunities
            ], return_exceptions=True)
            
            # Log any exceptions
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    print(f"   ❌ [LIVE] Execution error for {unique_opportunities[i].get('team', 'unknown')}: {result}")
    
    async def _execute_opportunity(self, result: dict):
        """Execute a detected LIVE opportunity."""
        poly_event = result["poly_event"]
        odds_match = result["odds_match"]
        token_id = result["token_id"]
        team = result.get("team", "unknown")
        
        # ATOMIC CHECK AND RESERVE
        if not self.watcher.bot_state.reserve_token(token_id):
            print(f"   ⚠️ [LIVE] Skipping {team}: token already reserved/active")
            return
        
        try:
            # Use existing match's team order from BotState, not Polymarket's
            existing_match = self.bot_state.get_match(odds_match.match_id)
            if existing_match and existing_match.team1 and existing_match.team2:
                team1 = existing_match.team1
                team2 = existing_match.team2
            else:
                # Fallback: use Polymarket outcomes for new matches
                market = result.get("market") or (poly_event.markets[0] if poly_event.markets else {})
                outcomes = market.get("outcomes", [])
                team1 = outcomes[0] if len(outcomes) > 0 else result["team"]
                team2 = outcomes[1] if len(outcomes) > 1 else result["hedge_team"]
            
            # Create position - get condition_id from market
            market = result.get("market") or (poly_event.markets[0] if poly_event.markets else {})
            condition_id = market.get("condition_id", "")
            
            position = await self.watcher.create_entry_position(
                match_id=odds_match.match_id,
                game=odds_match.game,
                team1=team1,
                team2=team2,
                entry_team=result["team"],
                entry_token_id=token_id,
                hedge_team=result["hedge_team"],
                hedge_token_id=result["hedge_token"],
                entry_price=result["entry_price"],
                fair_value=result["fair"],
                shares=LIVE_CONFIG.get("live_shares", 10),  # 10 shares for live
                condition_id=condition_id,
            )
            
            if position:
                print(f"🔴 [LIVE] Order placed: {result['team']} @ {result['entry_price']:.2f}")
                print(f"   Edge: {result['edge']*100:.1f}% | Expected profit: {result['expected_profit']*100:.1f}%")
                
                # Subscribe to WebSocket for this token
                await self.ws_handler.subscribe_to_token(token_id)
            else:
                print(f"   ⚠️ [LIVE] Order placement failed for {result['team']}")
                self.watcher.bot_state.unreserve_token(token_id)
        except Exception as e:
            self.watcher.bot_state.unreserve_token(token_id)
            raise
    
    async def _cancel_orders_on_finished_events(self):
        """Cancel any open orders on events that have finished."""
        # Get all our open orders
        open_orders = self.bot_state.get_all_open_order_infos()
        if not open_orders:
            return
        
        # Get finished token IDs from Polymarket
        finished_tokens = set()
        try:
            all_events = await self.poly_client.get_all_esports_events()
            for event in all_events:
                if event.is_finished:
                    if not event.markets:
                        event = await self.poly_client.hydrate_event(event)
                    for market in (event.markets or []):
                        for token_id in market.get("clobTokenIds", []):
                            finished_tokens.add(token_id)
        except Exception as e:
            print(f"⚠️ [LIVE] Error fetching finished events: {e}")
            return
        
        if not finished_tokens:
            return
        
        # Cancel orders on finished events
        for token_id, order_info in list(open_orders.items()):
            if token_id in finished_tokens:
                team = order_info.get("team", token_id[:12])
                print(f"🏁 [LIVE] Cancelling order on FINISHED event: {team}")
                try:
                    order_id = order_info.get("order_id")
                    if order_id:
                        cancelled, _ = await self.executor.cancel_order(order_id, force=True, team_name=team)
                        if cancelled:
                            self.bot_state.clear_order(order_id)
                            print(f"   ✅ Cancelled order for {team}")
                except Exception as e:
                    print(f"   ❌ Failed to cancel: {e}")
    
    # ===== Override loops - LiveBot filters for LIVE positions only =====
    
    async def _get_live_token_ids(self) -> set:
        """Get token IDs for currently live Polymarket events."""
        return await self.poly_client.get_live_token_ids()
    
    async def _position_monitor_loop(self):
        """Monitor LIVE positions only - skip pre-match."""
        await asyncio.sleep(20)
        
        while self._running:
            try:
                # Get live event tokens
                live_tokens = await self._get_live_token_ids()
                
                # Filter hydrated orders to live-only
                all_orders = self.bot_state.get_all_open_order_infos()
                live_orders = {
                    token_id: info for token_id, info in all_orders.items()
                    if token_id in live_tokens
                }
                
                # Filter positions needing hedge to live-only
                all_positions = self.bot_state.get_positions_needing_hedge()
                
                # DEBUG: Log hedge filtering
                if all_positions:
                    live_positions = {}
                    for key, info in all_positions.items():
                        entry_token = info.get("entry_token_id")
                        hedge_token = info.get("hedge_token_id")
                        # A position is "live" if EITHER the entry OR hedge token is in live_tokens
                        # (they should be from the same match, so both should be live if one is)
                        is_live = entry_token in live_tokens or hedge_token in live_tokens
                        if is_live:
                            live_positions[key] = info
                else:
                    live_positions = {}
                
                if not live_orders and not live_positions:
                    await asyncio.sleep(LIVE_CONFIG.get("monitor_interval", 5))
                    continue
                
                # Log when we find live positions to hedge
                if live_positions:
                    print(f"   🔴 [LIVE] Hedging {len(live_positions)} live positions...")
                
                tasks = []
                
                # Monitor live orders only
                for token_id, order_info in list(live_orders.items()):
                    tasks.append(
                        self.order_monitor.monitor_order(
                            token_id=token_id,
                            poly_client=self.poly_client,
                            watcher=self.watcher,
                            hedge_token_ids=self._hedge_token_ids,
                            order_info=order_info,
                            book_ws=self.ws_handler.book_ws,
                        )
                    )
                
                # Seek hedges for live positions only
                for key, hedge_info in list(live_positions.items()):
                    tasks.append(
                        self.hedge_seeker.seek_hedge_for_position(
                            key=key,
                            hedge_info=hedge_info,
                            poly_client=self.poly_client,
                            watcher=self.watcher,
                            hedge_token_ids=self._hedge_token_ids,
                            book_ws=self.ws_handler.book_ws,
                            live_event_tokens=live_tokens,
                            allow_live_hedges=True,  # LiveBot: allow hedging live positions
                        )
                    )
                
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    
            except Exception as e:
                print(f"❌ [LIVE] Position monitor error: {e}")
            
            await asyncio.sleep(LIVE_CONFIG.get("monitor_interval", 5))
    
    async def _ws_adjustment_loop(self):
        """Process WebSocket-detected outbids for LIVE orders only."""
        await asyncio.sleep(5)
        
        while self._running:
            try:
                if self.order_adjuster.has_pending_adjustments():
                    # Get live tokens to filter
                    live_tokens = await self._get_live_token_ids()
                    
                    # Only process adjustments for live tokens
                    await self.order_adjuster.process_pending_adjustments(
                        watcher=self.watcher,
                        hedge_token_ids=self._hedge_token_ids,
                        filter_tokens=live_tokens,  # Only adjust live orders
                        book_ws=self.ws_handler.book_ws,
                    )
            except Exception as e:
                print(f"❌ [LIVE] WS adjustment error: {e}")
            
            await asyncio.sleep(0.5)
    
    async def _reactive_check_loop(self):
        """Check edge on LIVE orders only."""
        await asyncio.sleep(30)
        
        while self._running:
            try:
                # Get live tokens
                live_tokens = await self._get_live_token_ids()
                
                # Check only live orders
                actions_taken = await self.reactive_handler.check_all_orders(
                    filter_tokens=live_tokens
                )
                if actions_taken > 0:
                    print(f"🔴 [LIVE] Reactive check: {actions_taken} actions taken")
            except Exception as e:
                print(f"⚠️ [LIVE] Reactive check error: {e}")
            
            await asyncio.sleep(30)  # 30s for live (faster than base 60s)
    
    # ===== Override timing loops =====
    
    async def _book_validation_loop(self):
        """Periodically validate WebSocket cached book state - 30s for live."""
        from src.polymarket.market_client import PolymarketEsportsClient
        
        await asyncio.sleep(30)  # Wait 30s before first check (faster than base)
        
        validation_interval = LIVE_CONFIG["book_validation_interval"]  # 30 seconds
        
        while self._running:
            try:
                book_ws = self.ws_handler.book_ws
                if not book_ws or not book_ws.is_connected:
                    await asyncio.sleep(validation_interval)
                    continue
                
                subscribed_tokens = list(book_ws._subscribed_tokens)
                if not subscribed_tokens:
                    await asyncio.sleep(validation_interval)
                    continue
                
                async with PolymarketEsportsClient() as poly_client:
                    rest_bids = await poly_client.get_best_bids(subscribed_tokens)
                
                ws_prices = book_ws.get_all_prices()
                
                stale_tokens = []
                
                for token_id in subscribed_tokens:
                    rest_data = rest_bids.get(token_id, {})
                    rest_bid = rest_data.get("price", 0)
                    
                    ws_price = ws_prices.get(token_id)
                    ws_bid = ws_price.best_bid if ws_price else None
                    
                    if rest_bid > 0:
                        if ws_bid is None:
                            stale_tokens.append({
                                "token": token_id,
                                "rest_bid": rest_bid,
                                "ws_bid": None,
                                "diff": "missing",
                            })
                        elif abs(rest_bid - ws_bid) > 0.001:
                            stale_tokens.append({
                                "token": token_id,
                                "rest_bid": rest_bid,
                                "ws_bid": ws_bid,
                                "diff": f"{rest_bid - ws_bid:+.2f}",
                            })
                
                seeded_count = await book_ws.seed_all_from_rest(rest_bids)
                
                total_checked = len(subscribed_tokens)
                
                if stale_tokens:
                    print(f"🔴 [LIVE] Book validation: {len(stale_tokens)} stale (synced)")
                    
            except Exception as e:
                print(f"⚠️ [LIVE] Book validation error: {e}")
            
            await asyncio.sleep(validation_interval)
