"""
Base Trading Bot - Shared Infrastructure

Abstract base class containing all shared infrastructure:
- Core services (executor, alerts, poly_client, etc.)
- WebSocket management
- Position tracking and monitoring
- Health server
- Reactive handling
- State synchronization

Strategy-specific implementations should inherit from this class
and implement the abstract methods for opportunity scanning and execution.
"""
import asyncio
import os
import signal
import sys
import builtins
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import Optional

# Override print with timestamped version
_original_print = builtins.print
def _timestamped_print(*args, **kwargs):
    """Print with timestamp prefix."""
    local_tz = timezone(timedelta(hours=2))  # UTC+2
    now = datetime.now(local_tz)
    timestamp = now.strftime("%H:%M:%S")
    
    if args:
        first_arg = str(args[0])
        if first_arg.strip() and not first_arg.startswith("==="):
            args = (f"[{timestamp}] {first_arg}",) + args[1:]
    
    _original_print(*args, **kwargs)

builtins.print = _timestamped_print

from dotenv import load_dotenv
load_dotenv()

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Core services
from src.services.odds_service import OddsService
from src.services.telegram_alerts import TelegramAlerts
from src.execution.order_executor import OrderExecutor, OrderSide
from src.execution.order_watcher import OrderWatcher
from src.execution.hedge_finder import HedgeFinder
from src.polymarket.market_client import PolymarketEsportsClient
from src.polymarket.user_websocket import PolymarketUserWebSocket
from src.state.market_cache import get_market_cache
from src.state.bot_state import get_bot_state
from src.state.reactive_handler import get_reactive_handler

# Modular components
from src.monitoring import OrderMonitor, HedgeSeeker
from src.execution import OrderAdjuster
from src.handlers import FillHandler, WebSocketHandler, LiveEventHandler
from src.infra import HealthServer

# Configuration from central config
from src.core.config import CONFIG


class BaseBot(ABC):
    """
    Abstract base trading bot with shared infrastructure.
    
    Provides:
    - Core service initialization (executor, alerts, websockets)
    - Position and order management
    - Health monitoring
    - State synchronization
    - Data cleanup
    
    Subclasses must implement:
    - _signal_loop(): Strategy-specific scanning loop
    - _scan_and_execute(): Opportunity detection logic
    - _execute_opportunity(): Order execution logic
    """
    
    # Environment variable names - can be overridden by subclasses for separate accounts
    _private_key_env_var = "POLYMARKET_PRIVATE_KEY"
    _funder_address_env_var = "POLYMARKET_FUNDER_ADDRESS"
    
    def __init__(self):
        # Get credentials from env vars (can be overridden by subclasses)
        self.private_key = os.getenv(self._private_key_env_var)
        if not self.private_key:
            raise ValueError(f"{self._private_key_env_var} not set")
        
        funder_address = os.getenv(self._funder_address_env_var)
        if funder_address:
            print(f"   💳 Using funder: {funder_address[:10]}...{funder_address[-6:]}")
        
        # ===== Core Services =====
        # Note: _live_only_mode must be set BEFORE calling super().__init__() in subclasses
        live_mode = getattr(self, '_live_only_mode', False)
        self.odds_service = OddsService(refresh_interval=CONFIG["odds_refresh_interval"], live_mode=live_mode)
        self.alerts = TelegramAlerts()
        self.executor = OrderExecutor(paper_trading=False, private_key=self.private_key, funder_address=funder_address)
        self.hedge_finder = HedgeFinder(min_profit_percent=CONFIG["min_profit"])
        self.cache = get_market_cache()
        
        # Persistent Polymarket client - reused across all operations for caching
        self.poly_client = PolymarketEsportsClient()
        
        # Bot state for match-level awareness
        self.bot_state = get_bot_state()
        
        # Reactive handler for event-driven actions
        self.reactive_handler = get_reactive_handler(
            min_edge=CONFIG["min_edge"],
            min_arb_profit=CONFIG["min_profit"],
        )
        
        # Order watcher - callbacks set later
        self.watcher = OrderWatcher(executor=self.executor, config=CONFIG, bot_state=self.bot_state)
        
        # ===== Modular Components =====
        
        # Monitoring - unified OrderMonitor for all orders
        self.order_monitor = OrderMonitor(
            cache=self.cache,
            odds_service=self.odds_service,
            hedge_finder=self.hedge_finder,
            executor=self.executor,
            config=CONFIG,
        )
        
        self.hedge_seeker = HedgeSeeker(
            executor=self.executor,
            alerts=self.alerts,
            config=CONFIG,
        )
        
        # Adjustments
        self.order_adjuster = OrderAdjuster(
            executor=self.executor,
            hedge_finder=self.hedge_finder,
            order_monitor=self.order_monitor,
            config=CONFIG,
        )
        
        # Handlers
        self.fill_handler = FillHandler(
            odds_service=self.odds_service,
            alerts=self.alerts,
            hedge_finder=self.hedge_finder,
            is_live=live_mode,
        )
        
        self.ws_handler = WebSocketHandler(
            alerts=self.alerts,
            on_outbid_callback=self.order_adjuster.queue_adjustment,
        )
        
        self.live_event_handler = LiveEventHandler(
            executor=self.executor,
            alerts=self.alerts,
        )
        
        self.health_server = HealthServer(
            get_status_fn=self._get_health_status,
            port=8080,
        )
        
        # ===== Wire up callbacks =====
        self.watcher.on_fill = lambda pos, shares: asyncio.create_task(
            self.fill_handler.on_fill(pos, shares, self.watcher)
        )
        self.watcher.on_hedge_fill = lambda pos, shares: asyncio.create_task(
            self.fill_handler.on_hedge_fill(pos, shares)
        )
        
        # ===== Wire up reactive handler =====
        self.reactive_handler.on_cancel_order = self._reactive_cancel_order
        self.reactive_handler.on_skip_hedge = self._reactive_skip_hedge
        
        # ===== WebSocket =====
        self.user_ws: Optional[PolymarketUserWebSocket] = None
        
        # ===== State =====
        self._running = False
        self._start_time: Optional[datetime] = None
        self._hedge_token_ids: set = set()
        self._live_only_mode: bool = getattr(self, '_live_only_mode', False)  # Preserve if set by subclass
        
        # Balance tracking
        self._last_balance: float = 0.0
        self._low_balance_warned: bool = False
        self._min_balance_for_orders: float = 5.0
        
        self._api_creds = None
        
        # State re-sync tracking
        self._last_state_sync: Optional[datetime] = None
        self._state_sync_interval = 60  # 1 minute
    
    # ===== Abstract Methods (Strategy-Specific) =====
    
    @abstractmethod
    async def _signal_loop(self):
        """
        Strategy-specific scanning loop.
        
        Called by run() as part of the main asyncio.gather.
        Should scan for opportunities according to strategy logic.
        """
        pass
    
    @abstractmethod
    async def _scan_and_execute(self):
        """
        Scan for and execute opportunities.
        
        Strategy-specific implementation of opportunity detection.
        Called periodically by _signal_loop().
        """
        pass
    
    @abstractmethod
    async def _execute_opportunity(self, result: dict):
        """
        Execute a detected opportunity.
        
        Strategy-specific order placement logic.
        """
        pass
    
    # ===== Shared Infrastructure Methods =====
    
    def _get_health_status(self) -> dict:
        """Get health status for HTTP endpoint."""
        bot_state_stats = self.bot_state.get_stats()
        reactive_stats = self.reactive_handler.get_stats()
        
        return {
            "status": "ok",
            "running": self._running,
            "uptime": str(datetime.now(timezone.utc) - self._start_time) if self._start_time else "0",
            "open_positions": len(self.bot_state.get_open_match_positions()),
            "hedged_positions": len(self.bot_state.get_hedged_match_positions()),
            "matches_tracked": bot_state_stats.get("matches_tracked", 0),
            "natural_arbs_detected": bot_state_stats.get("natural_arbs_detected", 0),
            "edge_lost_events": bot_state_stats.get("edge_lost_events", 0),
            "open_orders": bot_state_stats.get("open_orders", 0),
            "reactive_cancels": reactive_stats.get("edge_lost_cancels", 0),
        }
    
    async def _check_balance(self) -> float:
        """Check USDC balance on Polymarket exchange."""
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        
        for attempt in range(2):
            try:
                client = self.executor._get_client()
                # Use signature_type=2 for proxy wallets (default for Polymarket website accounts)
                sig_type = 2
                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=sig_type
                )
                result = client.get_balance_allowance(params)
                
                balance = float(result.get("balance", 0)) / 1e6
                self._last_balance = balance
                
                if balance > self._min_balance_for_orders:
                    self._low_balance_warned = False
                
                return balance
                
            except Exception as e:
                if attempt == 0:
                    await asyncio.sleep(2)
                else:
                    print(f"⚠️ Balance check failed (after retry): {e}")
        
        return self._last_balance
    
    # ===== Reactive Callbacks =====
    
    async def _reactive_cancel_order(self, order_id: str, reason: str):
        """Cancel an order reactively (edge lost, match went live)."""
        try:
            order = None
            match = self.bot_state.get_match_by_order(order_id)
            if match:
                if match.order1 and match.order1.order_id == order_id:
                    order = match.order1
                elif match.order2 and match.order2.order_id == order_id:
                    order = match.order2
                
                if order:
                    match_label = ""
                    if match.team1 and match.team2:
                        match_label = f" | {match.team1} vs {match.team2}"
                    print(f"⚡ [REACTIVE] Cancelling {order.team} @ {order.price:.2f}: {reason}{match_label}")
            
            team_name = order.team if order else ""
            cancelled, _ = await self.executor.cancel_order(order_id, force=True, team_name=team_name)
            
            if cancelled:
                self.bot_state.cancel_order(order_id)
            else:
                print(f"⚠️ [REACTIVE] Failed to cancel order {order_id[:16]}...")
                self.bot_state.cancel_order(order_id)
                    
        except Exception as e:
            print(f"⚠️ [REACTIVE] Cancel error: {e}")
            # Still clean up BotState to prevent infinite retry loops
            # (the order is likely already gone from the CLOB)
            self.bot_state.cancel_order(order_id)
    
    def _reactive_skip_hedge(self, token_id: str, reason: str):
        """Called when a hedge is skipped due to natural arb."""
        match = self.bot_state.get_match_by_token(token_id)
        if match:
            entry_team = match.team1 if token_id == match.token1 else match.team2
            print(f"🎯 [REACTIVE] Skipping hedge for {entry_team}: {reason}")
    
    # ===== Lifecycle Methods =====
    
    async def start(self):
        """Start all services."""
        print("=" * 60)
        print(f"🤖 {self.__class__.__name__.upper()}")
        print("=" * 60)
        
        self._running = True
        self._start_time = datetime.now(timezone.utc)
        
        # Derive API credentials
        client = self.executor._get_client()
        creds = client.creds
        self._api_creds = {
            "api_key": creds.api_key,
            "api_secret": creds.api_secret,
            "api_passphrase": creds.api_passphrase,
        }
        
        # Start User WebSocket
        self.user_ws = PolymarketUserWebSocket(
            api_key=self._api_creds["api_key"],
            api_secret=self._api_creds["api_secret"],
            api_passphrase=self._api_creds["api_passphrase"],
        )
        self.watcher.set_user_websocket(self.user_ws)
        
        self.user_ws.on_reconnect = lambda: asyncio.create_task(
            self.alerts.on_websocket_reconnect("User Order WebSocket")
        )
        self.user_ws.on_disconnect = lambda: print("⚠️ User WebSocket disconnected, reconnecting...")
        
        await self.user_ws.connect()
        print("✅ User WebSocket connected")
        
        # Load odds BEFORE hydrating positions
        await self.odds_service._fetch_from_supabase()
        # CRITICAL: Also fetch v2 multi-market odds (spreads, totals) for sports
        # Without this, v2_cache is empty at startup → sports orders get no fair values.
        if not self._live_only_mode:
            try:
                await self.odds_service._fetch_from_supabase_v2()
            except Exception:
                pass  # v2 table may not exist yet
        self.odds_service._push_fair_probs_to_bot_state()
        
        # Hydrate existing orders and filled positions in parallel
        # (orders from CLOB API, positions from Data API — independent)
        await asyncio.gather(
            self.watcher.hydrate_active_orders(
                poly_client=self.poly_client,
                odds_service=self.odds_service,
                skip_individual_game_filter=getattr(self, '_skip_individual_game_filter', False),
            ),
            self.watcher.hydrate_filled_positions(
                poly_client=self.poly_client,
                odds_service=self.odds_service,
                live_only=self._live_only_mode,
            ),
        )
        
        # Identify existing hedge orders
        self._hedge_token_ids = self.bot_state.get_hedge_token_ids()
        
        # Push fair probs again after hydration
        self.odds_service._push_fair_probs_to_bot_state()
        
        # Print hydrated orders (live_only for LiveBot)
        self.watcher.print_hydrated_orders(
            hedge_tokens=self._hedge_token_ids,
            live_only=self._live_only_mode,
            odds_service=self.odds_service,
        )
        
        # Log BotState status
        bot_state_stats = self.bot_state.get_stats()
        if bot_state_stats.get("matches_tracked", 0) > 0:
            print(f"   📊 BotState: {bot_state_stats['matches_tracked']} matches tracked")
        
        # Start Book WebSocket
        self.ws_handler.set_poly_client(self.poly_client)
        await self.ws_handler.connect(self.watcher)
        
        # Give fill handler access to live book data for spread tracking
        self.fill_handler.book_ws = self.ws_handler.book_ws
        
        # Start Reactive Handler
        self.reactive_handler.start()
        
        # Start BotState event processor
        await self.bot_state.start_event_processor()
        
        # Start Health Server
        await self.health_server.start()
        
        print(f"📊 Config: min_edge={CONFIG['min_edge']*100:.0f}%")
        print("🚀 Starting trading loop...\n")
    
    async def stop(self):
        """Stop all services gracefully."""
        print("\n🛑 Shutting down...")
        self._running = False
        
        # Stop reactive systems
        self.reactive_handler.stop()
        await self.bot_state.stop_event_processor()

        if self.user_ws:
            await self.user_ws.close()
        
        await self.ws_handler.disconnect()
        await self.health_server.stop()
        await self.odds_service.stop()
        await self.poly_client.close()
    
    async def run(self):
        """Main run loop."""
        await self.start()
        
        try:
            await asyncio.gather(
                self.odds_service.run(),
                self.user_ws.run() if self.user_ws else asyncio.sleep(0),
                self._signal_loop(),  # Strategy-specific (abstract)
                self._position_monitor_loop(),
                self._ws_adjustment_loop(),
                self._status_loop(),
                self._state_resync_loop(),
                self._reactive_check_loop(),
                self._data_cleanup_loop(),
                self._book_validation_loop(),
                self.health_server.run_forever(lambda: self._running),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()
    
    # ===== Monitoring Loops =====
    
    async def _position_monitor_loop(self):
        """Monitor open positions."""
        await asyncio.sleep(20)
        
        while self._running:
            try:
                await self._monitor_positions()
            except Exception as e:
                print(f"❌ Position monitor error: {e}")
                import traceback
                traceback.print_exc()
            
            await asyncio.sleep(CONFIG.get("monitor_interval", 10))
    
    async def _monitor_positions(self):
        """Check and adjust open orders and seek hedges for filled positions."""
        hydrated_orders = self.bot_state.get_all_open_order_infos()
        positions_needing_hedge = self.bot_state.get_positions_needing_hedge()
        
        if not hydrated_orders and not positions_needing_hedge:
            return
        
        poly_client = self.poly_client
        
        # Get live events to skip hedging for live matches
        live_event_tokens = await poly_client.get_live_token_ids()
        
        # Get live match IDs from esports_odds_live (ground truth)
        live_match_ids = self.odds_service.get_live_match_ids()
        
        # Pre-fetch order books from WS L2 cache (zero REST calls when cache is warm)
        book_ws = self.ws_handler.book_ws if self.ws_handler else None
        books = {}
        
        if book_ws:
            for token_id in hydrated_orders:
                ws_book = book_ws.get_book(token_id)
                if ws_book:
                    books[token_id] = ws_book
                # Cache misses get book=None → monitor_order fetches via REST internally
        
        
        tasks = []
        
        # Monitor all open orders (with pre-fetched books)
        for token_id, order_info in list(hydrated_orders.items()):
            tasks.append(
                self.order_monitor.monitor_order(
                    token_id=token_id,
                    poly_client=poly_client,
                    watcher=self.watcher,
                    hedge_token_ids=self._hedge_token_ids,
                    order_info=order_info,
                    book=books.get(token_id),
                    book_ws=book_ws,
                )
            )
        
        # Seek hedges for filled positions
        for key, hedge_info in list(positions_needing_hedge.items()):
            tasks.append(
                self.hedge_seeker.seek_hedge_for_position(
                    key=key,
                    hedge_info=hedge_info,
                    poly_client=poly_client,
                    watcher=self.watcher,
                    hedge_token_ids=self._hedge_token_ids,
                    book_ws=self.ws_handler.book_ws,
                    live_event_tokens=live_event_tokens,
                    live_match_ids=live_match_ids,  # Ground truth live detection
                )
            )
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _ws_adjustment_loop(self):
        """Process WebSocket-detected outbids."""
        await asyncio.sleep(5)
        
        while self._running:
            try:
                if self.order_adjuster.has_pending_adjustments():
                    await self.order_adjuster.process_pending_adjustments(
                        watcher=self.watcher,
                        hedge_token_ids=self._hedge_token_ids,
                        book_ws=self.ws_handler.book_ws,
                    )
            except Exception as e:
                print(f"❌ WS adjustment error: {e}")
            
            await asyncio.sleep(0.5)
    
    async def _status_loop(self):
        """Periodic status updates and Telegram summary."""
        await asyncio.sleep(30)
        await self._update_telegram_summary()
        
        while self._running:
            await asyncio.sleep(60)
            await self._update_telegram_summary()
    
    async def _update_telegram_summary(self):
        """Update the pinned Telegram summary message."""
        try:
            stats = self.bot_state.get_summary_stats()
            balance = getattr(self, '_last_balance', 0.0)
            
            await self.alerts.update_pinned_summary(
                open_orders_count=stats.get("open_orders_count", 0),
                open_orders_risk=stats.get("open_orders_risk", 0.0),
                completed_wins=stats.get("completed_wins", 0),
                completed_losses=stats.get("completed_losses", 0),
                completed_pnl=stats.get("completed_pnl", 0.0),
                arb_count=stats.get("arb_count", 0),
                arb_pnl=stats.get("arb_pnl", 0.0),
                active_positions=stats.get("active_positions", 0),
                active_value=stats.get("active_value", 0.0),
                active_expected_profit=stats.get("active_expected_profit", 0.0),
                balance=balance,
            )
        except Exception as e:
            print(f"⚠️ Telegram summary update error: {e}")
    
    async def _state_resync_loop(self):
        """Periodically re-sync state from Polymarket."""
        await asyncio.sleep(self._state_sync_interval)
        
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                poly_client = self.poly_client
                
                # Store current state for comparison
                old_active_count = len(self.bot_state.get_active_token_ids())
                old_hydrated_count = len(self.bot_state.get_all_open_order_infos())
                old_hedge_queue = len(self.bot_state.get_positions_needing_hedge())
                
                old_hydrated_orders = {
                    info.get("order_id", ""): {
                        "team": info.get("team_name", "?"),
                        "price": info.get("price", 0),
                    }
                    for token_id, info in self.bot_state.get_all_open_order_infos().items()
                }
                old_hedge_positions = {
                    key: info.get("entry_team", "?")
                    for key, info in self.bot_state.get_positions_needing_hedge().items()
                }
                
                old_bot_state_stats = self.bot_state.get_stats()
                
                # Re-hydrate active orders and filled positions in parallel
                await asyncio.gather(
                    self.watcher.hydrate_active_orders(
                        poly_client=poly_client,
                        odds_service=self.odds_service,
                        quiet=True,
                        skip_individual_game_filter=getattr(self, '_skip_individual_game_filter', False),
                    ),
                    self.watcher.hydrate_filled_positions(
                        poly_client=poly_client,
                        odds_service=self.odds_service,
                        quiet=True,
                        live_only=self._live_only_mode,
                    ),
                )
                
                # Re-mark hedge tokens
                self._hedge_token_ids = self.bot_state.get_hedge_token_ids()
                
                # Update reactive handler if present
                if hasattr(self, 'reactive_book_handler'):
                    self.reactive_book_handler.update_hedge_tokens(self._hedge_token_ids)
                
                # Re-subscribe WebSocket to all active tokens
                all_tokens = list(self.bot_state.get_active_token_ids())
                if all_tokens and self.ws_handler.book_ws:
                    await self.ws_handler.book_ws.subscribe(all_tokens)
                
                # Report changes
                new_active_count = len(self.bot_state.get_active_token_ids())
                new_hydrated_count = len(self.bot_state.get_all_open_order_infos())
                new_hedge_queue = len(self.bot_state.get_positions_needing_hedge())
                new_bot_state_stats = self.bot_state.get_stats()
                
                new_hydrated_orders = {
                    info.get("order_id", ""): {
                        "team": info.get("team_name", "?"),
                        "price": info.get("price", 0),
                    }
                    for token_id, info in self.bot_state.get_all_open_order_infos().items()
                }
                new_hedge_positions = {
                    key: info.get("entry_team", "?")
                    for key, info in self.bot_state.get_positions_needing_hedge().items()
                }
                
                # Calculate diffs
                added_orders = set(new_hydrated_orders.keys()) - set(old_hydrated_orders.keys())
                removed_orders = set(old_hydrated_orders.keys()) - set(new_hydrated_orders.keys())
                added_hedges = set(new_hedge_positions.keys()) - set(old_hedge_positions.keys())
                removed_hedges = set(old_hedge_positions.keys()) - set(new_hedge_positions.keys())
                
                cache_stats = poly_client.get_cache_stats()
                
                old_matches = old_bot_state_stats.get('matches_tracked', 0)
                new_matches = new_bot_state_stats.get('matches_tracked', 0)
                
                something_changed = (
                    old_active_count != new_active_count or
                    old_hydrated_count != new_hydrated_count or
                    old_hedge_queue != new_hedge_queue or
                    old_matches != new_matches or
                    added_orders or removed_orders or
                    added_hedges or removed_hedges
                )
                    
                self._last_state_sync = now
                
            except Exception as e:
                print(f"⚠️ State re-sync error: {e}")
                import traceback
                traceback.print_exc()
            
            await asyncio.sleep(self._state_sync_interval)
    
    async def _reactive_check_loop(self):
        """Periodic safety net for reactive system."""
        await asyncio.sleep(30)
        
        while self._running:
            try:
                actions_taken = await self.reactive_handler.check_all_orders()
                if actions_taken > 0:
                    print(f"🔄 Reactive check: {actions_taken} actions taken")
            except Exception as e:
                print(f"⚠️ Reactive check error: {e}")
            
            await asyncio.sleep(60)
    
    async def _data_cleanup_loop(self):
        """Periodically cleanup old data based on retention policies."""
        from src.data.recorder import get_recorder
        
        await asyncio.sleep(6 * 3600)  # Wait 6 hours before first cleanup
        
        while self._running:
            try:
                recorder = get_recorder()
                
                deleted = await recorder.cleanup_old_data(
                    order_history_days=90,
                    fair_value_log_days=30,
                )
                
                stats = await recorder.get_storage_stats()
                if stats:
                    total_records = sum(s.get("count", 0) for s in stats.values())
                    print(f"📊 Data storage: {total_records} total records across {len(stats)} tables")
                
            except Exception as e:
                print(f"⚠️ Data cleanup error: {e}")
            
            await asyncio.sleep(24 * 3600)  # Run daily
    
    async def _book_validation_loop(self):
        """Periodically validate WebSocket cached book state against REST API."""
        from src.polymarket.market_client import PolymarketEsportsClient
        
        await asyncio.sleep(120)  # Wait 2 minutes before first check
        
        validation_interval = 300  # 5 minutes
        
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
                
                # seeded_count = await book_ws.seed_all_from_rest(rest_bids)
                
                # total_checked = len(subscribed_tokens)
                # fresh_count = total_checked - len(stale_tokens)
                
                # if stale_tokens:
                #     print(f"⚠️ Book validation: {fresh_count}/{total_checked} fresh, {len(stale_tokens)} stale (all synced):")
                #     for s in stale_tokens[:5]:
                #         token_short = s['token'][:16] + "..."
                #         if s["ws_bid"] is None:
                #             print(f"   📍 {token_short} REST={s['rest_bid']:.2f} WS=MISSING → synced")
                #         else:
                #             print(f"   📍 {token_short} REST={s['rest_bid']:.2f} WS={s['ws_bid']:.2f} (Δ{s['diff']}) → synced")
                #     if len(stale_tokens) > 5:
                #         print(f"   ... and {len(stale_tokens) - 5} more")
                #     print(f"✅ Full REST refresh: {seeded_count} tokens synced")
                # else:
                #     print(f"✅ Book validation: {total_checked} tokens checked, all fresh ({seeded_count} synced)")
                    
            except Exception as e:
                print(f"⚠️ Book validation error: {e}")
            
            await asyncio.sleep(validation_interval)
