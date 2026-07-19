"""
Sports Bot - External Odds Edge Detection Strategy

Concrete implementation of BaseBot for sports betting:
- Uses external bookmaker odds to calculate fair probabilities
- Detects edge opportunities (fair value > market price)
- Executes with hedging strategy
- Reactive WS-driven order management (outbids, improvements, cancels)

This is the original/legacy strategy extracted from main.py.
"""
import asyncio
import logging
import os
import time as _time
from typing import Optional, Set

from src.bots.base_bot import BaseBot
from src.scanning import OpportunityScanner
from src.core.config import CONFIG
from src.handlers.reactive_book_handler import ReactiveBookHandler
from src.infra.scanner_sidecar import ScannerSidecar, ScannerSidecarError, TickResult
from src.scanning.shadow_diff import compute_diff, ShadowDiffFileSink
from src.scanning.sidecar_hydrate import hydrate_opportunities

logger = logging.getLogger(__name__)


# Max time to wait for the sidecar tick to finish AFTER the Python
# scanner has returned. Python is authoritative, so if the sidecar is
# lagging we drop the diff for that tick rather than hold up the
# trading loop.
SHADOW_DIFF_WAIT_SECONDS = 5.0

# When mode=primary, how long to wait for the sidecar before falling
# back to the Python scanner. Kept tight so a wedged sidecar cannot
# stall trading for long.
PRIMARY_SIDECAR_TIMEOUT = 30.0

SCANNER_MODE_SHADOW = "shadow"
SCANNER_MODE_PRIMARY = "primary"


def _resolve_scanner_mode() -> str:
    raw = (os.environ.get("POLYMM_SCANNER_MODE") or SCANNER_MODE_SHADOW).strip().lower()
    if raw not in (SCANNER_MODE_SHADOW, SCANNER_MODE_PRIMARY):
        logger.warning("invalid POLYMM_SCANNER_MODE=%r; defaulting to shadow", raw)
        return SCANNER_MODE_SHADOW
    return raw


class SportsBot(BaseBot):
    """
    Sports betting strategy implementation.
    
    Strategy: External Odds → Fair Probability → Edge Detection → Execution
    
    Scans for opportunities where:
    - External bookmaker odds imply a fair probability
    - Polymarket bid is below fair value (positive edge)
    - Edge exceeds minimum threshold (min_edge from config)
    
    Order management is REACTIVE via ReactiveBookHandler:
    - Outbids detected and handled instantly on WS book updates
    - Price improvements triggered on WS book updates
    - Edge-loss cancels use cached fair values from BotState
    """
    
    def __init__(self):
        super().__init__()
        
        # Strategy-specific: OpportunityScanner with odds + hedge integration
        self.scanner = OpportunityScanner(
            odds_service=self.odds_service,
            cache=self.cache,
            hedge_finder=self.hedge_finder,
            config=CONFIG,
        )
        
        # Reactive book handler — event-driven trading decisions
        # Fair values come from BotState (populated by OddsService updates)
        # NOTE: spread_cancel_threshold=0 disables spread collapse checks.
        # Spread collapse is only for SpreadBot (spread-based entries).
        # SportsBot uses edge-based entries (fair value from bookmaker odds).
        self.reactive_book_handler = ReactiveBookHandler(
            order_monitor=self.order_monitor,
            executor=self.executor,
            config=CONFIG,
            spread_cancel_threshold=0,  # Disabled — not relevant for edge-based trading
        )
        
        # Track completed order IDs to avoid repeated adjustment attempts
        # Prevents spam when clear_order fails to properly remove orders
        self._completed_orders: Set[str] = set()
        
        # Track tokens that failed placement to avoid retry loops (TTL: 10 min)
        self._failed_market_tokens: dict[str, float] = {}
        self._failed_token_ttl = 600  # 10 minutes

        # Phase A scanner sidecar — opt-in via POLYMM_SCANNER_BINARY.
        # Mode selects shadow (default, observe only) vs primary (use
        # sidecar opportunities for trading with Python fallback).
        self.sidecar: Optional[ScannerSidecar] = ScannerSidecar.from_env()
        self._sidecar_tick_counter = 0
        self._scanner_mode = _resolve_scanner_mode()
        self._shadow_diff_sink = ShadowDiffFileSink()

        print(f"   ⚡ Reactive trading: ENABLED")
        if self.sidecar is not None:
            print(f"   🦀 Rust scanner sidecar: ENABLED ({self._scanner_mode} mode)")
    

    # ===== Strategy Implementation =====
    
    async def _signal_loop(self):
        """Scan for opportunities and execute."""
        await asyncio.sleep(15)  # Wait for initial odds fetch
        
        while self._running:
            try:
                await self._scan_and_execute()
            except Exception as e:
                print(f"❌ Signal loop error: {e}")
                await self.alerts.on_error(str(e), "Signal Loop")
            
            await asyncio.sleep(CONFIG["signal_scan_interval"])
    
    async def _scan_and_execute(self):
        """Scan for opportunities using the scanner module."""
        # Check balance first
        balance = await self._check_balance()
        if balance < self._min_balance_for_orders:
            if not self._low_balance_warned:
                print(f"⚠️ Low balance: ${balance:.2f} - need ${self._min_balance_for_orders}")
                await self.alerts.on_low_balance(balance, self._min_balance_for_orders)
                self._low_balance_warned = True
            return
        
        # ============================================================
        # LIVE MATCH DETECTION (esports_odds_live - ground truth)
        # ============================================================
        # Get live matches from esports_odds_live table.
        # If a match is in this table, it's live regardless of Polymarket's is_live flag.
        live_match_ids = self.odds_service.get_live_match_ids()
        
        # Cancel orders on matches detected as live via esports_odds_live
        if live_match_ids:
            await self.live_event_handler.cancel_orders_on_live_matches(live_match_ids)

        # Cache for the final live-check in _execute_opportunity — the set
        # may have grown since the scanner captured its snapshot.
        self._last_live_match_ids = set(live_match_ids) if live_match_ids else set()
        
        # Define callback for cancelling live orders (Polymarket is_live flag - secondary)
        async def cancel_live_orders(live_events, poly_client):
            await self.live_event_handler.cancel_orders_on_live_events(
                live_events=live_events,
                poly_client=poly_client,
                watcher=self.watcher,
            )
        
        use_rust_primary = (
            self._scanner_mode == SCANNER_MODE_PRIMARY
            and self.sidecar is not None
            and self.sidecar.is_alive()
        )

        # Shadow-mode: kick off Rust sidecar tick alongside the Python
        # scanner. Result feeds the diff logger only; Python remains
        # authoritative for trading decisions.
        shadow_task: Optional[asyncio.Task] = None
        shadow_tick_id: Optional[int] = None
        skip_tokens: Set[str] = set()
        if not use_rust_primary and self.sidecar is not None and self.sidecar.is_alive():
            self._sidecar_tick_counter += 1
            shadow_tick_id = self._sidecar_tick_counter
            skip_tokens = self.watcher.bot_state.get_tokens_blocking_new_orders()
            shadow_task = asyncio.create_task(
                self._run_shadow_tick(shadow_tick_id, skip_tokens, set(live_match_ids)),
            )

        opportunities = None
        if use_rust_primary:
            opportunities = await self._run_primary_sidecar_tick(live_match_ids)

        if opportunities is None:
            # Shadow mode, or primary sidecar failed and we're falling back.
            opportunities = await self.scanner.scan_for_opportunities(
                watcher=self.watcher,
                cancel_live_orders_fn=cancel_live_orders,
                live_match_ids=live_match_ids,
            )

        # Shadow-mode diff: wait briefly for the sidecar tick to finish,
        # then compare opportunity sets. If the sidecar is still running
        # past the wait budget, drop this tick's diff rather than block
        # trading — the sidecar's own 60s watchdog will handle real hangs.
        if shadow_task is not None:
            try:
                rust_result = await asyncio.wait_for(
                    shadow_task, timeout=SHADOW_DIFF_WAIT_SECONDS,
                )
                if rust_result is not None:
                    self._log_shadow_diff(opportunities or [], rust_result)
            except asyncio.TimeoutError:
                logger.warning(
                    "[sidecar shadow] tick=%s still running after Python finished; dropping diff",
                    shadow_tick_id,
                )
                # Let the task keep running; if it finishes later its own
                # logging will surface the result.
            except Exception as e:  # noqa: BLE001 — diff must never break trading
                logger.warning("[sidecar shadow] tick=%s diff error: %s", shadow_tick_id, e)
        
        # Scan for multi-market opportunities (spreads, totals)
        try:
            from src.polymarket.market_client import PolymarketEsportsClient
            async with PolymarketEsportsClient() as poly_client:
                # Fetch sports events (football, hockey, basketball)
                sports_events = await poly_client.get_all_sports_events()
                
                if sports_events:
                    multi_opps = await self.scanner.scan_multi_market_opportunities(
                        watcher=self.watcher,
                        poly_client=poly_client,
                        poly_events=sports_events,
                    )
                    if multi_opps:
                        opportunities = (opportunities or []) + multi_opps
        except Exception as e:
            print(f"⚠️ Multi-market scan error: {e}")
        
        if opportunities:
            # Cleanup expired failed token entries
            now = _time.time()
            self._failed_market_tokens = {
                k: v for k, v in self._failed_market_tokens.items()
                if now - v < self._failed_token_ttl
            }
            
            # Deduplicate opportunities by token_id AND by match+team+market
            # Multiple markets (moneyline, spread -1.5, O/U 3.5) for the same game
            # generate different token_ids. We want ONE order per match+team+market_type+line.
            seen_tokens = set()
            seen_match_market = set()  # (match_id, team, market_type, line) tuples
            unique_opportunities = []
            for opp in opportunities:
                token_id = opp.get("token_id")
                odds_match = opp.get("odds_match")
                team = opp.get("team", "")
                market_type = opp.get("market_type", "h2h")
                line = opp.get("line", 0)
                match_key = (odds_match.match_id, team, market_type, line) if odds_match else None
                
                if token_id and token_id in seen_tokens:
                    continue
                if match_key and match_key in seen_match_market:
                    continue
                # Skip tokens that recently failed placement (10 min TTL)
                if token_id and token_id in self._failed_market_tokens:
                    continue
                
                if token_id:
                    seen_tokens.add(token_id)
                if match_key:
                    seen_match_market.add(match_key)
                unique_opportunities.append(opp)
            
            # Execute in batches to avoid overwhelming the API
            BATCH_SIZE = 5
            print(f"   🎯 Found {len(unique_opportunities)} sports opportunities to execute in batches of {BATCH_SIZE} (from {len(opportunities)} total)")
            
            for batch_start in range(0, len(unique_opportunities), BATCH_SIZE):
                batch = unique_opportunities[batch_start:batch_start + BATCH_SIZE]
                results = await asyncio.gather(*[
                    self._execute_opportunity(opp) for opp in batch
                ], return_exceptions=True)
                
                # Log any exceptions
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        print(f"   ❌ Execution error for {batch[i].get('team', 'unknown')}: {result}")
                
                # Small delay between batches to let API recover
                if batch_start + BATCH_SIZE < len(unique_opportunities):
                    await asyncio.sleep(0.5)
    
    async def _execute_opportunity(self, result: dict):
        """Execute a detected opportunity."""
        poly_event = result["poly_event"]
        odds_match = result["odds_match"]
        token_id = result["token_id"]
        team = result.get("team", "unknown")

        # DEADLINE GUARD: Don't place new orders on past-deadline matches
        existing_match = self.bot_state.get_match(odds_match.match_id)
        if existing_match and existing_match.is_past_deadline:
            return

        # FINAL LIVE-CHECK: third line of defense against placing entries on a
        # match that flipped live during the scan→execute await chain. The
        # scanner already did two checks; this catches the (rare) case where
        # esports_odds_live reclassified the match after the scan returned.
        scan_live_match_ids = result.get("scan_live_match_ids") or set()
        current_live_match_ids = getattr(self, "_last_live_match_ids", set()) or set()
        live_ids = scan_live_match_ids | current_live_match_ids
        match_id = getattr(odds_match, "match_id", None)
        if match_id and match_id in live_ids:
            print(f"   ⏭️ Skipping {team}: match {match_id} flipped live during scan")
            return
        if getattr(poly_event, "is_live", False):
            print(f"   ⏭️ Skipping {team}: poly_event.is_live became True during scan")
            return
        
        # ATOMIC CHECK AND RESERVE: Prevent race conditions
        if not self.watcher.bot_state.reserve_token(token_id):
            print(f"   ⚠️ Skipping {team}: token already reserved/active")
            return
        
        try:
            # Use existing match's team order from BotState, not Polymarket's
            existing_match = self.bot_state.get_match(odds_match.match_id)
            if existing_match and existing_match.team1 and existing_match.team2:
                team1 = existing_match.team1
                team2 = existing_match.team2
            else:
                # Use event-level team names (clean), NOT market outcomes
                # Market outcomes contain suffixes like "Bucks: 1H Moneyline"
                # which break match_id generation
                if poly_event.team1 and poly_event.team2:
                    team1 = poly_event.team1
                    team2 = poly_event.team2
                else:
                    # Last resort: use odds_match teams
                    team1 = odds_match.team1
                    team2 = odds_match.team2
            
            # Create position - get condition_id from market
            market = result.get("market") or (poly_event.markets[0] if poly_event.markets else {})
            condition_id = market.get("condition_id", "")
            
            # CRITICAL: Multi-market orders (spreads, totals) need unique match_ids
            # to avoid collisions with moneyline orders on the same match.
            # Append condition_id suffix to create isolated MatchState per market.
            # Yes/No h2h markets also need isolation — each "Will X win?" question
            # is a separate condition with its own token pair.
            market_type = result.get("market_type", "h2h")
            effective_match_id = odds_match.match_id
            needs_isolation = market_type in ("spreads", "totals", "spreads_1h", "totals_1h")
            if not needs_isolation and market_type in ("h2h", "h2h_h1"):
                outcomes = market.get("outcomes", [])
                if any(o.lower() in ("yes", "no") for o in outcomes):
                    needs_isolation = True
            if needs_isolation and condition_id:
                effective_match_id = f"{odds_match.match_id}:{condition_id[:18]}"
            
            # CRITICAL: For totals and spreads, include market markers in team names
            # so OddsService's _v2_match_corresponds can find this match and push
            # the correct per-order fair values.
            # Without this, team names are plain and _v2_match_corresponds can't
            # distinguish spread/totals orders from h2h orders, leading to h2h
            # moneyline fair values being used for spread orders (wrong!).
            line = result.get("line")
            if market_type in ("totals", "totals_1h") and line is not None:
                team1 = f"{team1}: O/U {line:g}"
            elif market_type in ("spreads", "spreads_1h") and line is not None:
                team1 = f"{team1}: Spread {line:g}"
            
            # Per-sport share sizing (new sports start smaller)
            game = odds_match.game if odds_match.game else "unknown"
            sport_shares = CONFIG.get("sport_shares", {})
            shares = sport_shares.get(game, CONFIG["default_shares"])
            
            position = await self.watcher.create_entry_position(
                match_id=effective_match_id,
                game=odds_match.game,
                team1=team1,
                team2=team2,
                entry_team=result["team"],
                entry_token_id=token_id,
                hedge_team=result["hedge_team"],
                hedge_token_id=result["hedge_token"],
                entry_price=result["entry_price"],
                fair_value=result["fair"],
                shares=shares,
                condition_id=condition_id,
                trading_deadline=poly_event.start_time,
            )
            
            if position:
                print(f"✅ Order placed: {result['team']} @ {result['entry_price']:.2f} | Edge: {result['edge']*100:.1f}% | Expected profit: {result['expected_profit']*100:.1f}%")
                
                # Subscribe to WebSocket for both tokens (entry + hedge)
                # Hedge token needed for spread_at_fill calculation
                await self.ws_handler.subscribe_to_token(token_id)
                if result.get("hedge_token"):
                    await self.ws_handler.subscribe_to_token(result["hedge_token"])
            else:
                print(f"   ⚠️ Order placement failed for {result['team']}")
                # Blacklist token to prevent retry loops on dead/invalid markets
                self._failed_market_tokens[token_id] = _time.time()
                self.watcher.bot_state.unreserve_token(token_id)
        except Exception as e:
            self.watcher.bot_state.unreserve_token(token_id)
            raise
    
    # ===== Reactive WS Integration =====
    
    async def run(self):
        """Main run loop with reactive WS handling.

        NOTE: No _ws_adjustment_loop — outbids are handled reactively
        by ReactiveBookHandler on each WS book update.
        """
        await self.start()

        # Wire up reactive handler AFTER ws_handler connects
        self.ws_handler.set_reactive_handler(self.reactive_book_handler)
        self.reactive_book_handler.set_watcher(self.watcher, self._hedge_token_ids)
        self.reactive_book_handler.set_book_ws(self.ws_handler.book_ws)

        # Start Rust scanner sidecar (if configured). Failure disables it
        # cleanly — Python scanner stays authoritative.
        if self.sidecar is not None:
            try:
                await self.sidecar.start()
            except ScannerSidecarError as e:
                logger.warning("scanner sidecar failed to start; disabling: %s", e)
                self.sidecar = None

        try:
            await asyncio.gather(
                self.odds_service.run(),
                self.user_ws.run() if self.user_ws else asyncio.sleep(0),
                self._signal_loop(),
                self._position_monitor_loop(),
                # NOTE: NO _ws_adjustment_loop — outbids handled reactively
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

    async def stop(self):
        """Stop services, including the Rust scanner sidecar."""
        if self.sidecar is not None:
            try:
                await self.sidecar.stop()
            except Exception as e:  # noqa: BLE001 — shutdown must not throw
                logger.warning("scanner sidecar stop error: %s", e)
        await super().stop()

    async def _run_primary_sidecar_tick(
        self,
        live_match_ids: Set[str],
    ) -> Optional[list[dict]]:
        """Run a primary-mode sidecar tick and hydrate its opportunities.

        Returns a list of hydrated opportunity dicts ready for
        `_execute_opportunity`, or `None` on any sidecar failure —
        caller falls back to the Python scanner.
        """
        if self.sidecar is None or not self.sidecar.is_alive():
            return None

        self._sidecar_tick_counter += 1
        tick_id = self._sidecar_tick_counter
        skip_tokens = self.watcher.bot_state.get_tokens_blocking_new_orders()

        try:
            result = await asyncio.wait_for(
                self.sidecar.run_tick(
                    tick_id=tick_id,
                    skip_tokens=skip_tokens,
                    live_match_ids=set(live_match_ids),
                ),
                timeout=PRIMARY_SIDECAR_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("[sidecar primary] tick=%d timed out; falling back to Python", tick_id)
            return None
        except ScannerSidecarError as e:
            logger.warning(
                "[sidecar primary] tick=%d sidecar error: %s; falling back to Python",
                tick_id, e,
            )
            return None
        except Exception as e:  # noqa: BLE001 — sidecar must not break the tick
            logger.warning(
                "[sidecar primary] tick=%d unexpected: %s; falling back to Python",
                tick_id, e,
            )
            return None

        odds_cache = getattr(self.odds_service, "_cache", None)
        hydrated = hydrate_opportunities(result.opportunities, odds_cache=odds_cache)
        logger.info(
            "[sidecar primary] tick=%d hydrated=%d (matched=%d rust_opps=%d dur=%dms)",
            tick_id, len(hydrated), result.matched_count,
            result.opportunity_count, result.duration_ms,
        )
        return hydrated

    async def _run_shadow_tick(
        self,
        tick_id: int,
        skip_tokens: Set[str],
        live_match_ids: Set[str],
    ) -> Optional[TickResult]:
        """Kick off a sidecar tick for shadow observation.

        Returns the sidecar result on success, or `None` on failure /
        timeout (failures are logged internally). Callers use the return
        value for set-diff comparison against the Python scanner output.
        """
        if self.sidecar is None or not self.sidecar.is_alive():
            return None
        try:
            return await self.sidecar.run_tick(
                tick_id=tick_id,
                skip_tokens=skip_tokens,
                live_match_ids=live_match_ids,
            )
        except asyncio.TimeoutError:
            logger.warning("[sidecar shadow] tick=%d timed out", tick_id)
        except ScannerSidecarError as e:
            logger.warning("[sidecar shadow] tick=%d sidecar error: %s", tick_id, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("[sidecar shadow] tick=%d unexpected: %s", tick_id, e)
        return None

    def _log_shadow_diff(self, python_opps: list[dict], rust_result: TickResult) -> None:
        """Compute + log the set-diff between Python and Rust opps.

        Per `rust/PHASE_A_PLAN.md` §7, opportunity identity is
        `(token_id, round(edge, 3))`. Divergence lines must be
        grep-friendly: the token + rounded edge appears in the message.
        """
        diff = compute_diff(rust_result.tick_id, python_opps, rust_result.opportunities)
        if diff.has_divergence:
            logger.warning(
                "[sidecar shadow] %s py_only=%r rs_only=%r",
                diff.summary(),
                diff.python_only,
                diff.rust_only,
            )
        else:
            logger.info("[sidecar shadow] %s", diff.summary())
        self._shadow_diff_sink.write(diff)

    async def _position_monitor_loop(self):
        """Safety-net monitor — reduced to hedge seeking + fair value refresh.
        
        Outbids and price improvements are now handled reactively by
        ReactiveBookHandler on each WS book update. This loop runs at
        reduced frequency (30s) for:
        - Refreshing cached fair values from OddsService
        - Seeking hedges for filled positions
        - Catching any reactive handler misses (safety net)
        """
        await asyncio.sleep(20)
        
        while self._running:
            try:
                await self._monitor_positions()
            except Exception as e:
                print(f"❌ Position monitor error: {e}")
                import traceback
                traceback.print_exc()
            
            await asyncio.sleep(30)  # Reduced frequency — reactive handles urgent actions
    
    async def _monitor_positions(self):
        """Optimized position monitor with batched REST fallback + completed order guard.
        
        Improvements over base_bot._monitor_positions:
        - Deadline protection: cancels orders past game start time
        - Completed order guard: skips orders already fully matched
        - Batched REST fallback: single call for all WS cache misses
        """
        # ============================================================
        # DEADLINE PROTECTION — cancel orders past game start time
        # ============================================================
        # Uses trading_deadline from MatchState (set from Polymarket's startDate).
        # This is a safety net alongside esports_odds_live and Polymarket's is_live.
        for match in self.bot_state.get_all_matches():
            if not match.is_past_deadline:
                continue
            # Past deadline — cancel all open orders on this match
            for order in [match.order1, match.order2]:
                if not order or not order.is_open:
                    continue
                match_label = ""
                if match.team1 and match.team2:
                    match_label = f" | {match.team1} vs {match.team2}"
                print(f"🏁 [DEADLINE] Past game start — cancelling {order.team} @ {order.price:.2f}{match_label}")
                try:
                    await self.executor.cancel_order(
                        order.order_id, force=True, team_name=order.team
                    )
                except Exception as e:
                    print(f"   ❌ Deadline cancel failed ({order.team}): {e}")
                # ALWAYS clear from BotState — if we've decided it's past deadline,
                # we want it dead regardless of whether the API cancel succeeded
                self.bot_state.cancel_order(order.order_id)
        
        hydrated_orders = self.bot_state.get_all_open_order_infos()
        positions_needing_hedge = self.bot_state.get_positions_needing_hedge()
        
        if not hydrated_orders and not positions_needing_hedge:
            return
        
        poly_client = self.poly_client
        live_event_tokens = await poly_client.get_live_token_ids()
        live_match_ids = self.odds_service.get_live_match_ids()
        
        # Pre-fetch order books from WS L2 cache
        book_ws = self.ws_handler.book_ws if self.ws_handler else None
        books = {}
        rest_fallback_tokens = []
        
        if book_ws:
            for token_id in hydrated_orders:
                ws_book = book_ws.get_book(token_id)
                if ws_book:
                    books[token_id] = ws_book
                else:
                    rest_fallback_tokens.append(token_id)
        else:
            rest_fallback_tokens = list(hydrated_orders.keys())
        
        # Batched REST fallback (single call instead of N individual fetches)
        if rest_fallback_tokens:
            try:
                rest_books = await poly_client.get_order_books_batch(rest_fallback_tokens)
                books.update(rest_books)
            except Exception as e:
                print(f"   ⚠️ Batched REST fallback failed: {e}")
        
        tasks = []
        
        # Monitor all open orders (with pre-fetched books + completed order guard)
        for token_id, order_info in list(hydrated_orders.items()):
            order_id = order_info.get("order_id", "")
            if order_id and order_id in self._completed_orders:
                continue
            
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
                    live_match_ids=live_match_ids,
                )
            )
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _book_validation_loop(self):
        """Validate WS book state — active seeding from REST with full L2 depth.
        
        Overrides base_bot version which detects stale books but doesn't
        fix them. This version actively seeds full REST L2 back into WS cache,
        ensuring bid war evaluation has real depth data.
        """
        from src.polymarket.market_client import PolymarketEsportsClient
        
        await asyncio.sleep(120)  # Wait 2 minutes before first check
        interval = CONFIG.get("book_validation_interval", 300)
        
        while self._running:
            try:
                book_ws = self.ws_handler.book_ws
                if not book_ws or not book_ws.is_connected:
                    await asyncio.sleep(interval)
                    continue
                
                tokens = list(book_ws._subscribed_tokens)
                if not tokens:
                    await asyncio.sleep(interval)
                    continue
                
                # Identify tokens that never received a WS book snapshot
                missing_snapshot = [t for t in tokens if t not in book_ws._book_received]
                
                # Fetch FULL order books (not just best bids) for proper L2 depth
                async with PolymarketEsportsClient() as client:
                    rest_books = await client.get_order_books_batch(tokens)
                
                seeded = await book_ws.seed_all_from_rest(rest_books)
                
                if missing_snapshot:
                    print(f"   📖 Book validation: {len(missing_snapshot)}/{len(tokens)} tokens never got WS snapshot, seeded {seeded} from REST")
                
            except Exception as e:
                print(f"⚠️ Book validation error: {e}")
            
            await asyncio.sleep(interval)
