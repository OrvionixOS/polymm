"""
Spread Bot - Trades weather temperature markets based on spread opportunities.

Key characteristics:
- No external odds source - purely spread-based
- Enters when spread >= 15c, cancels when spread < 10c
- Hedges using min_profit with no upper price constraint
- Only trades TODAY and TOMORROW weather markets
"""
import asyncio
import logging
from typing import Optional, Set
from datetime import datetime, timezone, timedelta

from src.bots.base_bot import BaseBot
from src.polymarket.weather_client import WeatherMarketClient
from src.scanning.spread_scanner import SpreadScanner, SpreadOpportunity
from src.core.config import SPREAD_CONFIG
from src.handlers.reactive_book_handler import ReactiveBookHandler
from src.services.odds_coverage_filter import OddsCoverageFilter, parse_spread_opportunity_market


def get_smart_increment(price: float) -> float:
    """
    Determine bid increment based on price precision.
    
    - If price is a whole cent (e.g., 0.16), use 0.01 increment
    - If price has sub-cent precision (e.g., 0.162), use 0.001 increment
    """
    # Check if price has sub-cent precision (3 decimal places)
    rounded_to_cent = round(price, 2)
    if abs(price - rounded_to_cent) > 0.0001:
        # Price has sub-cent precision (e.g., 0.162)
        return 0.001
    else:
        # Price is a whole cent (e.g., 0.16)
        return 0.01


class SpreadBot(BaseBot):
    """
    Trades weather temperature markets based on spread opportunities.
    
    Key differences from LiveBot/SportsBot:
    - Uses SpreadScanner instead of OddsService
    - Entry signal: spread >= 15c (no fair value comparison)
    - Exit signal: spread < 10c (cancels orders)
    - Hedging: max bid = 1 - min_profit (no fair_prob constraint)
    - Markets: Weather temperature bins only
    """
    
    # Use separate Polymarket account for SpreadBot
    _private_key_env_var = "POLYMARKET_PRIVATE_KEY_SPREAD"
    _funder_address_env_var = "POLYMARKET_FUNDER_ADDRESS_SPREAD"
    
    def __init__(self):
        # SpreadBot trades toss/game sub-markets — don't cancel them during hydration
        self._skip_individual_game_filter = True
        super().__init__()
        
        # Override config for spread trading
        self._state_sync_interval = SPREAD_CONFIG["state_sync_interval"]
        self.order_monitor.config = SPREAD_CONFIG
        
        # Different port to avoid conflicts
        self.health_server._port = 8082
        
        # Weather market client and scanner
        self.weather_client = WeatherMarketClient()
        self.scanner = SpreadScanner(
            weather_client=self.weather_client,
            min_spread=SPREAD_CONFIG["min_spread"],
        )
        
        # Track active weather orders by token_id
        self._weather_orders: Set[str] = set()
        self._weather_orders_hydrated = False  # Populated from BotState on first scan
        
        # Track tokens that failed with 'market not found' (404) to avoid retrying
        # Maps token_id → timestamp of last failure; expires after 10 minutes
        self._failed_market_tokens: dict[str, float] = {}
        self._failed_token_ttl = 600  # 10 minutes

        
        # Track completed order IDs to avoid repeated adjustment attempts
        # This prevents spam when clear_order fails to properly remove orders
        self._completed_orders: Set[str] = set()
        
        # Reactive book handler — event-driven trading decisions
        self.reactive_book_handler = ReactiveBookHandler(
            order_monitor=self.order_monitor,
            executor=self.executor,
            config=SPREAD_CONFIG,
            spread_cancel_threshold=SPREAD_CONFIG["spread_cancel_threshold"],
        )
        self.reactive_book_handler.on_spread_cancel = self._reactive_spread_cancel
        
        # Odds coverage filter — skip markets already handled by SportsBot
        self.odds_filter = OddsCoverageFilter(refresh_interval=300)
        
        print("🌡️ SpreadBot initialized")
        print(f"   Min spread: {SPREAD_CONFIG['min_spread']*100:.0f}c")
        print(f"   Cancel threshold: {SPREAD_CONFIG['spread_cancel_threshold']*100:.0f}c")
        print(f"   Default shares: {SPREAD_CONFIG['default_shares']}")
        print(f"   ⚡ Reactive trading: ENABLED")
        print(f"   🚫 Odds coverage filter: ENABLED")
    
    def _is_past_deadline(self, opp: SpreadOpportunity) -> bool:
        """Check if a market is past its trading deadline.
        
        Consolidates all deadline checks:
        - Sports/esports/tennis/basketball: game_start_time <= now
        - Stocks: within deadline_stock_hours of endDate (market close)
        - Mentions: game_start_time <= now (startTime from API = event start)
        - Weather: past city-specific UTC cutoff hour on event day
        """
        now = datetime.now(timezone.utc)
        
        # Sports/esports/tennis/basketball/mentions: game has started
        # Skip for weather/stock — they have their own deadline logic below
        if opp.game_start_time and opp.market_type not in ("weather", "stock"):
            gst = opp.game_start_time if opp.game_start_time.tzinfo else opp.game_start_time.replace(tzinfo=timezone.utc)
            if gst <= now:
                return True
        
        # Stocks: within N hours of market close (endDate)
        if opp.end_date and opp.market_type == "stock":
            ed = opp.end_date if opp.end_date.tzinfo else opp.end_date.replace(tzinfo=timezone.utc)
            hours_left = (ed - now).total_seconds() / 3600
            if hours_left <= SPREAD_CONFIG.get("deadline_stock_hours", 3):
                return True
        
        # Weather: past 1 PM local (city-specific UTC cutoff) on event day
        if opp.market_type == "weather" and opp.city:
            cutoff = SPREAD_CONFIG.get("weather_cutoff_utc", {}).get(opp.city.lower())
            if cutoff is not None:
                # end_date is noon next day, so event_day = end_date - 1 day
                if opp.end_date:
                    event_day = (opp.end_date - timedelta(days=1)).date()
                    if now.date() == event_day and now.hour >= cutoff:
                        return True
        
        return False
    
    def _compute_deadline(self, opp: SpreadOpportunity) -> "Optional[datetime]":
        """Compute the absolute UTC deadline timestamp for a market.
        
        Returns the timestamp at which we'd stop trading this market.
        Same semantics as _is_past_deadline but returns the timestamp instead of bool.
        """
        # Sports/esports/tennis/basketball/mentions: game start = deadline
        # Skip for weather/stock — they have their own deadline logic below
        if opp.game_start_time and opp.market_type not in ("weather", "stock"):
            gst = opp.game_start_time if opp.game_start_time.tzinfo else opp.game_start_time.replace(tzinfo=timezone.utc)
            return gst
        
        # Stocks: N hours before market close
        if opp.end_date and opp.market_type == "stock":
            ed = opp.end_date if opp.end_date.tzinfo else opp.end_date.replace(tzinfo=timezone.utc)
            hours = SPREAD_CONFIG.get("deadline_stock_hours", 3)
            return ed - timedelta(hours=hours)
        
        # Weather: city-specific UTC cutoff hour on event day
        if opp.market_type == "weather" and opp.city and opp.end_date:
            cutoff = SPREAD_CONFIG.get("weather_cutoff_utc", {}).get(opp.city.lower())
            if cutoff is not None:
                event_day = (opp.end_date - timedelta(days=1)).date()
                return datetime(event_day.year, event_day.month, event_day.day, cutoff, tzinfo=timezone.utc)
        
        # Fallback: end_date itself (e.g. mentions without game_start_time)
        if opp.end_date:
            ed = opp.end_date if opp.end_date.tzinfo else opp.end_date.replace(tzinfo=timezone.utc)
            return ed
        
        return None
    
    # ===== Strategy Implementation =====
    
    async def _signal_loop(self):
        """Scan for spread opportunities."""
        await asyncio.sleep(5)  # Brief startup delay
        
        while self._running:
            try:
                await self._scan_and_execute()
            except Exception as e:
                print(f"❌ [SPREAD] Signal loop error: {e}")
                await self.alerts.on_error(str(e), "Spread Signal Loop")
            
            await asyncio.sleep(SPREAD_CONFIG["signal_scan_interval"])
    
    async def _scan_and_execute(self):
        """Scan for spread opportunities and execute."""
        # STARTUP RECOVERY: Populate _weather_orders from BotState on first scan.
        # Without this, orders from a previous session are hydrated into BotState
        # (and adjusted by the order monitor) but the scanner doesn't know about
        # them — causing duplicate placements.
        if not self._weather_orders_hydrated:
            for match in self.bot_state.get_all_matches():
                if match.order1 and match.order1.is_open and match.order1.token_id:
                    self._weather_orders.add(match.order1.token_id)
                if match.order2 and match.order2.is_open and match.order2.token_id:
                    self._weather_orders.add(match.order2.token_id)
            if self._weather_orders:
                print(f"   📊 [SPREAD] Recovered {len(self._weather_orders)} active tokens from BotState")
            
            # Backfill trading_deadline for hydrated matches that lack one
            for match in self.bot_state.get_all_matches():
                if match.trading_deadline:
                    continue
                # Try to get deadline from scanner opportunity
                token = match.token1 or (match.order1.token_id if match.order1 else None)
                if token:
                    opp = await self.scanner.refresh_opportunity(token)
                    if opp:
                        deadline = self._compute_deadline(opp)
                        if deadline:
                            match.trading_deadline = deadline
            
            self._weather_orders_hydrated = True
        
        # Check balance
        balance = await self._check_balance()
        if balance < self._min_balance_for_orders:
            if not self._low_balance_warned:
                print(f"⚠️ [SPREAD] Low balance: ${balance:.2f}")
                await self.alerts.on_low_balance(balance, self._min_balance_for_orders)
                self._low_balance_warned = True
            return
        
        # Note: spread collapse check is handled by _position_monitor_loop (every 5s)
        # — NOT here, to avoid double-cancels from two independent loops.
        
        
        # Scan for new opportunities
        opportunities = await self.scanner.scan()
        
        if not opportunities:
            return
        
        # Filter out tokens we already have orders or UNHEDGED positions on
        # ALLOW placing new orders on completed arbs (both sides filled)
        def has_unhedged_position(token_id):
            match = self.bot_state.get_match_by_token(token_id)
            if not match:
                return False
            
            # Check if this is a completed arb (both sides have positions)
            pos1_shares = match.position1.shares if match.position1 else 0.0
            pos2_shares = match.position2.shares if match.position2 else 0.0
            is_completed_arb = pos1_shares > 0 and pos2_shares > 0 and abs(pos1_shares - pos2_shares) < 5.0
            
            # Completed arb — no unhedged position, but DON'T modify _weather_orders here
            # (only _check_spread_collapse and _execute_opportunity may modify it)
            if is_completed_arb:
                return False
            
            # Otherwise, check if this token has an unhedged position
            if match.token1 == token_id and match.position1 and match.position1.shares > 0:
                return True
            if match.token2 == token_id and match.position2 and match.position2.shares > 0:
                return True
            return False
        
        def should_allow_opportunity(opp):
            """Check if opportunity should be allowed (not blocked by orders/positions).
            
            IMPORTANT: This function is READ-ONLY — it must NEVER modify _weather_orders.
            The order monitor runs concurrently and clears orders from BotState (clear_order),
            creating windows where a match has no open orders. If this function discarded
            tokens from _weather_orders during those windows, the scanner would immediately
            re-place — creating duplicates.
            
            _weather_orders is only modified by:
            - _execute_opportunity (add on successful placement)
            - _check_spread_collapse (remove on intentional cancellation)
            - Deadline cancellation (remove on deadline)
            """
            # Check if this market recently failed with 404 (market not found)
            import time
            now = time.time()
            for tok in (opp.token_id, opp.no_token_id):
                fail_time = self._failed_market_tokens.get(tok)
                if fail_time and (now - fail_time) < self._failed_token_ttl:
                    return False  # Recently failed — skip
            
            # Check if token is reserved (in-progress adjustment/placement)
            if (opp.token_id in self.bot_state._reserved_tokens or
                    opp.no_token_id in self.bot_state._reserved_tokens):
                return False
            
            # PRIMARY DUPLICATE PREVENTION: If token is in _weather_orders, ALWAYS block.
            # _weather_orders is only cleared by explicit cancellation (_check_spread_collapse)
            # or deadline removal — never by the scanner itself.
            if opp.token_id in self._weather_orders:
                return False
            
            # Also check NO token - scanner returns YES token but we track both
            if opp.no_token_id in self._weather_orders:
                return False
            
            # Check if this token has an unhedged position
            if has_unhedged_position(opp.token_id):
                return False
            
            # Check if there's an existing order blocking in BotState
            return self.bot_state.get_order_info(opp.token_id) is None
        
        new_opportunities = [
            opp for opp in opportunities
            if should_allow_opportunity(opp)
        ]
        
        if not new_opportunities:
            return
        
        # Filter out markets already covered by external odds (SportsBot handles these)
        await self.odds_filter.ensure_fresh()
        SPORTS_MARKET_TYPES = {"sports", "basketball", "tennis", "hockey", "ufc", "football", "cricket", "rugby"}
        pre_filter_count = len(new_opportunities)
        filtered_opps = []
        for opp in new_opportunities:
            if opp.market_type in SPORTS_MARKET_TYPES:
                from src.core.match_id import parse_match_question
                from src.state.order_state import _clean_team_name
                _, t1, t2 = parse_match_question(opp.event_title)
                t1 = _clean_team_name(t1 or "")
                t2 = _clean_team_name(t2 or "")
                if t1 and t2:
                    odds_mt, odds_line = parse_spread_opportunity_market(opp.bin_label or "", opp.question)
                    if self.odds_filter.is_covered(t1, t2, odds_mt, odds_line):
                        continue  # Skip — SportsBot handles this market+line
            filtered_opps.append(opp)
        
        skipped = pre_filter_count - len(filtered_opps)
        if skipped > 0:
            print(f"   🚫 [ODDS FILTER] Skipped {skipped}/{pre_filter_count} opportunities (covered by external odds)")
        
        new_opportunities = filtered_opps
        if not new_opportunities:
            return
        
        # Sort by spread (highest first) and take top few
        new_opportunities.sort(key=lambda x: x.spread, reverse=True)
        
        # Execute opportunities in batches of 5 (each opp = 2 orders)
        # Staggering prevents CLOB balance check from seeing 30+ orders
        # simultaneously, which causes "not enough balance" at startup.
        batch_size = 5
        for i in range(0, min(len(new_opportunities), 15), batch_size):
            batch = new_opportunities[i:i + batch_size]
            await asyncio.gather(
                *(self._execute_opportunity(opp) for opp in batch)
            )
            if i + batch_size < min(len(new_opportunities), 15):
                await asyncio.sleep(0.5)  # Let CLOB settle between batches
    
    async def _execute_opportunity(self, opp: SpreadOpportunity):
        """
        Execute a spread opportunity by placing BOTH Yes and No orders.
        
        Strategy:
        - Place Yes order at yes_best_bid + 1c
        - Place No order at no_best_bid + 1c  
        - Both will be tracked as separate entry orders for price improvement
        """
        # Skip past-deadline markets (live games, stocks near close, mentions started, weather peak temp)
        if self._is_past_deadline(opp):
            return
        
        # Reserve both tokens (silently skip if already reserved - expected after restart)
        if not self.watcher.bot_state.reserve_token(opp.token_id):
            return
        
        if not self.watcher.bot_state.reserve_token(opp.no_token_id):
            self.watcher.bot_state.unreserve_token(opp.token_id)
            return
        
        try:
            # Fetch BOTH order books in parallel from live CLOB (Gamma API data can be stale)
            yes_book, no_book = await asyncio.gather(
                self.poly_client.get_order_book(opp.token_id),
                self.poly_client.get_order_book(opp.no_token_id),
            )
            
            # Get YES side best bid/ask from live data
            yes_best_bid = 0.0
            yes_best_ask = 1.0
            if yes_book and "bids" in yes_book and yes_book["bids"]:
                yes_best_bid = float(yes_book["bids"][0]["price"])
            if yes_book and "asks" in yes_book and yes_book["asks"]:
                yes_best_ask = float(yes_book["asks"][0]["price"])
            
            # Get NO side best bid from live data
            no_best_bid = 0.0
            if no_book and "bids" in no_book and no_book["bids"]:
                no_best_bid = float(no_book["bids"][0]["price"])
            
            if yes_best_bid <= 0:
                self.watcher.bot_state.unreserve_token(opp.token_id)
                self.watcher.bot_state.unreserve_token(opp.no_token_id)
                return
            
            if no_best_bid <= 0:
                self.watcher.bot_state.unreserve_token(opp.token_id)
                self.watcher.bot_state.unreserve_token(opp.no_token_id)
                return
            
            # Calculate bid prices using LIVE data (increment above best bid for each side)
            # Use smart increment: 0.01 for whole cents, 0.001 for sub-cent prices
            yes_increment = get_smart_increment(yes_best_bid)
            no_increment = get_smart_increment(no_best_bid)
            yes_bid = min(yes_best_bid + yes_increment, yes_best_ask - yes_increment)
            no_bid = min(no_best_bid + no_increment, 0.99)  # Cap at 99c
            
            # Verify spread is still profitable
            # If both fill: payout = $1, cost = yes_bid + no_bid
            total_cost = yes_bid + no_bid
            expected_profit = 1.0 - total_cost
            
            if expected_profit < SPREAD_CONFIG["min_profit"]:
                # Silently skip - this is expected when live data differs from cached Gamma data
                self.watcher.bot_state.unreserve_token(opp.token_id)
                self.watcher.bot_state.unreserve_token(opp.no_token_id)
                return
            
            # Create canonical match_id consistent with hydration format
            # Uses make_match_id + condition_id suffix to match order_state.py logic
            from src.core.match_id import make_match_id, parse_match_question, sport_from_slug
            from src.state.order_state import _clean_team_name
            
            if opp.market_type in ("rugby", "cricket", "football"):
                # ===== RUGBY/CRICKET/FOOTBALL MARKETS ("Will X win?" binary sub-markets) =====
                # Each event has 3 sub-markets: Team A win / Team B win / Draw
                # Draw is filtered out by SpreadScanner._scan_event()
                # Question format: "Will Harlequins win?" → team = "Harlequins"
                import re
                win_match = re.match(r'Will (.+?)\s+win\??', opp.question, re.IGNORECASE)
                if win_match:
                    team_name = win_match.group(1).strip()
                else:
                    # Fallback: use question as team name
                    team_name = opp.question
                
                yes_team = team_name
                no_team = f"{team_name} No"
                yes_display = team_name
                no_display = f"Not {team_name}"
                # Short prefixes to avoid collision with SportsBot's 3-way hedging
                if opp.market_type == "rugby":
                    game = "rugby"
                elif opp.market_type == "cricket":
                    game = "cricket"
                else:
                    game = "football"
                
                base_match_id = make_match_id(yes_team, no_team, game)
                match_id = f"{base_match_id}:{opp.condition_id[:18]}"
                
                emoji = {"rugby": "🏉", "cricket": "🏏", "football": "⚽"}.get(opp.market_type, "🏉")
                display_line = f"{emoji} [SPREAD] Dual orders placed: {opp.event_title} | {team_name} | YES @ ${yes_bid:.3f} | NO @ ${no_bid:.3f}"
            elif opp.market_type in ("sports", "basketball", "tennis", "hockey", "ufc"):
                # ===== SPORTS MARKETS (e.g., NCAAB) =====
                # Event title always has "Team A vs. Team B" format
                # Individual market questions may be Winner, Spread, or O/U
                
                # Always extract teams from EVENT TITLE (reliable "vs" format)
                game, team1_raw, team2_raw = parse_match_question(opp.event_title)
                team1_raw = _clean_team_name(team1_raw)
                team2_raw = _clean_team_name(team2_raw)
                if not team1_raw or not team2_raw:
                    # Fallback: try the question itself
                    game, team1_raw, team2_raw = parse_match_question(opp.question)
                    team1_raw = _clean_team_name(team1_raw)
                    team2_raw = _clean_team_name(team2_raw)
                
                if not team1_raw or not team2_raw:
                    print(f"   ⚠️ [SPREAD] Could not parse teams from: {opp.event_title}")
                    self.watcher.bot_state.unreserve_token(opp.token_id)
                    self.watcher.bot_state.unreserve_token(opp.no_token_id)
                    return
                
                # Determine game from opp.market_type (authoritative, set at event-level
                # by get_cricket_events, get_football_events, etc.)
                # parse_match_question() may return league names like "t20 world cup" or
                # "liga mx" which are not canonical game types.
                _CANONICAL_GAMES = {"tennis", "hockey", "ufc", "football", "cricket", "basketball"}
                if opp.market_type in _CANONICAL_GAMES:
                    # Cross-check: slug may reveal a more specific sport than market_type
                    # e.g., football events sometimes get market_type="basketball" from generic series
                    slug_sport = sport_from_slug(getattr(opp, 'slug', '') or '')
                    if slug_sport and slug_sport != opp.market_type:
                        game = slug_sport
                    else:
                        game = opp.market_type
                elif opp.market_type == "sports" and not game:
                    game = sport_from_slug(getattr(opp, 'slug', '') or '') or "unknown"
                # else: keep game from parse_match_question() (e.g. cs2, dota2)
                
                # Determine sub-market type from bin_label or question
                bin_label = opp.bin_label.strip() if opp.bin_label else ""
                q_lower = opp.question.lower()
                
                # Sport-specific emoji
                sport_emojis = {"tennis": "🎾", "hockey": "🏒", "ufc": "🥊", "football": "⚽"}
                sport_emoji = sport_emojis.get(game, "🏀")
                
                if bin_label and (bin_label.startswith("Spread") or bin_label.startswith("O/U")):
                    # SPREAD or O/U market: include bin_label in team names for uniqueness
                    yes_team = f"{team1_raw}: {bin_label}"
                    no_team = f"{team2_raw}"
                    yes_display = bin_label
                    no_display = f"Not {bin_label}"
                    display_line = f"{sport_emoji} [SPREAD] {opp.event_title} | {bin_label} | YES @ ${yes_bid:.3f} | NO @ ${no_bid:.3f}"
                elif "spread" in q_lower:
                    # Spread question without bin_label — extract from question
                    import re
                    spread_match = re.search(r'Spread[:\s]+.*?\(([-+]?\d+\.?\d*)\)', opp.question)
                    spread_val = spread_match.group(1) if spread_match else "?"
                    suffix = f"Spread {spread_val}"
                    yes_team = f"{team1_raw}: {suffix}"
                    no_team = team2_raw
                    yes_display = suffix
                    no_display = f"Not {suffix}"
                    display_line = f"{sport_emoji} [SPREAD] {opp.event_title} | {suffix} | YES @ ${yes_bid:.3f} | NO @ ${no_bid:.3f}"
                else:
                    # WINNER market: use team names directly
                    yes_team = team1_raw
                    no_team = team2_raw
                    yes_display = team1_raw
                    no_display = team2_raw
                    display_line = f"{sport_emoji} [SPREAD] Dual orders placed: {opp.event_title} | {yes_team} @ ${yes_bid:.3f} | {no_team} @ ${no_bid:.3f}"
                
                # condition_id suffix prevents collision between different markets
                base_match_id = make_match_id(yes_team, no_team, game)
                match_id = f"{base_match_id}:{opp.condition_id[:18]}"
            elif opp.market_type == "mentions":
                # ===== MENTIONS MARKETS (earnings calls, political speeches) =====
                # Each sub-market is a binary bet on whether a word/phrase is said.
                # bin_label = outcome (e.g., "AI / Artificial Intelligence 3+ times")
                # event_title = context (e.g., "What will Coinbase say during...")
                
                bin_label = opp.bin_label.strip() if opp.bin_label else opp.question
                
                # Strip numeric count suffixes for team name construction
                # so team names match hydration (which uses the bare outcome name).
                # e.g., "Dollar 10+" → "Dollar", "AI 3+ times" → "AI"
                import re
                base_label = re.sub(r'\s+\d+\+?\s*(times?)?\s*$', '', bin_label, flags=re.IGNORECASE).strip()
                if not base_label:
                    base_label = bin_label  # Fallback to full label if stripping left nothing
                
                yes_team = f"mentions:{base_label}"
                no_team = f"mentions:Not {base_label}"
                yes_display = bin_label  # Full label for display
                no_display = f"Not {bin_label}"
                game = "mentions"
                
                base_match_id = make_match_id(yes_team, no_team, game)
                match_id = f"{base_match_id}:{opp.condition_id[:18]}"
                
                display_line = f"💬 [SPREAD] Dual orders placed: {opp.event_title} | {bin_label} | YES @ ${yes_bid:.3f} | NO @ ${no_bid:.3f}"
            else:
                # ===== WEATHER/STOCK MARKETS =====
                # CRITICAL: Normalize city to match parse_match_question's CITY_NORMALIZE
                CITY_NORMALIZE = {
                    "new york city": "new york",
                    "nyc": "new york",
                }
                city_lower = CITY_NORMALIZE.get(opp.city.lower(), opp.city.lower())
                yes_team = f"{city_lower}:{opp.bin_label}"
                no_team = f"{city_lower}:Not {opp.bin_label}"
                yes_display = opp.bin_label
                no_display = f"Not {opp.bin_label}"
                game = "weather"
                
                base_match_id = make_match_id(yes_team, no_team, game)
                match_id = f"{base_match_id}:{opp.condition_id[:18]}"
                
                display_line = f"🌡️ [SPREAD] Dual orders placed: {opp.city.title()} {opp.date_str} | Bin: {opp.bin_label} | YES @ ${yes_bid:.3f} | NO @ ${no_bid:.3f}"
            
            shares = SPREAD_CONFIG["default_shares"]
            
            # Guard: skip if team/bin name extraction failed
            if not yes_display or not yes_display.strip():
                logging.warning(f"Empty display name for {opp.market_type} event, skipping: {opp.event_title} (token={opp.token_id[:16]}...)")
                self.watcher.bot_state.unreserve_token(opp.token_id)
                self.watcher.bot_state.unreserve_token(opp.no_token_id)
                return
            
            from src.execution.order_executor import OrderSide, OrderStatus
            
            # Place BOTH orders in parallel
            yes_order, no_order = await asyncio.gather(
                self.executor.place_limit_order(
                    token_id=opp.token_id,
                    side=OrderSide.BUY,
                    price=yes_bid,
                    size=shares,
                    team_name=yes_display,
                ),
                self.executor.place_limit_order(
                    token_id=opp.no_token_id,
                    side=OrderSide.BUY,
                    price=no_bid,
                    size=shares,
                    team_name=no_display,
                ),
            )
            
            # Rollback if either failed
            yes_ok = yes_order.status != OrderStatus.FAILED
            no_ok = no_order.status != OrderStatus.FAILED
            
            if not yes_ok or not no_ok:
                if yes_ok:
                    await self.executor.cancel_order(yes_order.order_id, team_name=yes_display)
                if no_ok:
                    await self.executor.cancel_order(no_order.order_id, team_name=no_display)
                if not yes_ok:
                    print(f"   ❌ [SPREAD] YES order failed for {yes_display} | {opp.event_title} (token={opp.token_id[:16]}...)")
                    # Mark token as failed to prevent retry loops
                    import time
                    self._failed_market_tokens[opp.token_id] = time.time()
                if not no_ok:
                    print(f"   ❌ [SPREAD] NO order failed for {no_display} | {opp.event_title} (token={opp.no_token_id[:16]}...)")
                    import time
                    self._failed_market_tokens[opp.no_token_id] = time.time()
                self.watcher.bot_state.unreserve_token(opp.token_id)
                self.watcher.bot_state.unreserve_token(opp.no_token_id)
                return
            
            yes_order_id = yes_order.order_id
            no_order_id = no_order.order_id
            
            # ===== REGISTER BOTH WITH BOTSTATE =====
            # Register the match and both orders
            trading_deadline = self._compute_deadline(opp)
            self.bot_state.register_match(
                match_id=match_id,
                condition_id=opp.condition_id,
                game=game,
                team1=yes_team,
                team2=no_team,
                token1=opp.token_id,
                token2=opp.no_token_id,
                trading_deadline=trading_deadline,
            )
            
            # Register YES order
            self.bot_state.register_order(
                match_id=match_id,
                order_id=yes_order_id,
                token_id=opp.token_id,
                team=yes_team,
                price=yes_bid,
                size=shares,
                is_entry=True,
            )
            
            # Register NO order  
            self.bot_state.register_order(
                match_id=match_id,
                order_id=no_order_id,
                token_id=opp.no_token_id,
                team=no_team,
                price=no_bid,
                size=shares,
                is_entry=True,  # Both are "entry" orders for price improvement
            )
            
            # Record to DB for forensics (same as create_entry_position)
            try:
                from src.data.recorder import get_recorder
                recorder = get_recorder()
                await recorder.record_order_placed(
                    order_id=yes_order_id, token_id=opp.token_id,
                    condition_id=opp.condition_id, match_id=match_id,
                    team=yes_team, price=yes_bid, fair_value=1.0 - no_bid,
                )
                await recorder.record_order_placed(
                    order_id=no_order_id, token_id=opp.no_token_id,
                    condition_id=opp.condition_id, match_id=match_id,
                    team=no_team, price=no_bid, fair_value=1.0 - yes_bid,
                )
            except Exception as e:
                print(f"   ⚠️ Failed to record order placement: {e}")
            
            # Track for spread monitoring
            self._weather_orders.add(opp.token_id)
            self._weather_orders.add(opp.no_token_id)
            
            print(display_line)
            print(f"   Total cost: ${total_cost:.3f} → Expected profit: {expected_profit*100:.1f}c")
            
            # Subscribe to WebSocket for price updates on both
            await self.ws_handler.subscribe_to_token(opp.token_id)
            await self.ws_handler.subscribe_to_token(opp.no_token_id)
            
        except Exception as e:
            print(f"   ❌ [SPREAD] Execute error: {e}")
            self.watcher.bot_state.unreserve_token(opp.token_id)
            self.watcher.bot_state.unreserve_token(opp.no_token_id)
    
    async def _check_spread_collapse(self):
        """
        Check if any active spread orders have spread below threshold - cancel them.
        
        Uses WebSocket L2 order book data for instant spread calculation
        (zero API calls). Deadline checks use cached opportunity data.
        """
        cancel_threshold = SPREAD_CONFIG["spread_cancel_threshold"]
        book_ws = self.ws_handler.book_ws if self.ws_handler else None
        
        # ===== PHASE 1: Collect orders needing check =====
        orders_to_check = []
        for match_id, match in list(self.bot_state._matches.items()):
            if not (match_id.startswith("weather:") or match_id.startswith("stock:") or match_id.startswith("ncaab:") or match_id.startswith("mentions:") or match_id.startswith("spread:") or match_id.startswith("rugby:") or match_id.startswith("tennis:") or match_id.startswith("cricket:") or match_id.startswith("hockey:") or match_id.startswith("ufc:") or match_id.startswith("football:") or match_id.startswith("unknown:")):
                continue
            
            for order in [match.order1, match.order2]:
                if not order or not order.is_open:
                    continue
                if order.token_id in self._hedge_token_ids:
                    continue
                if order.order_id in self._completed_orders:
                    continue
                
                orders_to_check.append((match_id, match, order))
        
        if not orders_to_check:
            return
        
        # ===== PHASE 2: Read spread from WS books (instant, zero API calls) =====
        for (match_id, match, order) in orders_to_check:
            token_id = order.token_id
            
            # Get cached opportunity for deadline check + market_type info
            opp = self.scanner.get_active_opportunity(token_id)
            
            # DEADLINE PROTECTION — uses cached opportunity data (no API needed)
            if opp and self._is_past_deadline(opp):
                emoji = {"stock": "⏰", "mentions": "🎤", "weather": "🌡️", "cricket": "🏏", "hockey": "🏒", "ufc": "🥊", "football": "⚽"}.get(opp.market_type, "🔴")
                try:
                    cancelled, was_complete = await self.executor.cancel_order(order.order_id, force=True, team_name=order.team)
                    if cancelled:
                        print(f"{emoji} [DEADLINE] {opp.market_type} past deadline — cancelled {order.team} | {opp.event_title[:70]}")
                    if was_complete:
                        self._completed_orders.add(order.order_id)
                    self.bot_state.cancel_order(order.order_id)
                    if match.order1 and match.order1.order_id == order.order_id:
                        match.order1 = None
                    elif match.order2 and match.order2.order_id == order.order_id:
                        match.order2 = None
                    self._weather_orders.discard(token_id)
                except Exception as e:
                    print(f"   ❌ Deadline cancel failed ({order.team}): {e}")
                continue
            
            # Compute spread from WS book data (instant)
            ws_book = book_ws.get_book(token_id) if book_ws else None
            if not ws_book:
                continue  # No WS data yet — skip, will be checked next cycle
            
            bids = ws_book.get("bids", [])
            asks = ws_book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 1.0
            spread = best_ask - best_bid if best_ask > best_bid else 0.0
            spread_cents = spread * 100
            
            if spread < cancel_threshold:
                print(f"📉 [SPREAD] Spread collapsed to {spread_cents:.1f}c - cancelling {order.team}")
                try:
                    cancelled, was_complete = await self.executor.cancel_order(order.order_id, force=True, team_name=order.team)
                    if was_complete:
                        self._completed_orders.add(order.order_id)
                    self.bot_state.cancel_order(order.order_id)
                    if match.order1 and match.order1.order_id == order.order_id:
                        match.order1 = None
                    elif match.order2 and match.order2.order_id == order.order_id:
                        match.order2 = None
                    self._weather_orders.discard(token_id)
                except Exception as e:
                    print(f"   ❌ Cancel failed: {e}")
    
    async def _monitor_weather_orders(self):
        """
        Monitor all weather/stock/ncaab orders using OrderMonitor with cost-based fair values.
        
        Uses WebSocket L2 cache for order book data (zero REST calls):
        1. Collect all orders needing monitoring + their context
        2. Read order books from WS cache (full L2 depth stored since WS book events)
        3. Process each order with cached book data
        4. Fallback to single REST fetch for tokens without WS data (rare)
        """
        # Phase 1: Collect orders and their context
        orders_to_monitor = []
        
        for match_id, match in list(self.bot_state._matches.items()):
            if not (match_id.startswith("weather:") or match_id.startswith("stock:") or match_id.startswith("ncaab:") or match_id.startswith("mentions:") or match_id.startswith("spread:") or match_id.startswith("rugby:") or match_id.startswith("tennis:") or match_id.startswith("cricket:") or match_id.startswith("hockey:") or match_id.startswith("ufc:") or match_id.startswith("football:") or match_id.startswith("unknown:")):
                continue
            
            for order, opposite_order, opposite_position in [
                (match.order1, match.order2, match.position2),
                (match.order2, match.order1, match.position1),
            ]:
                if not order or not order.is_open:
                    continue
                if order.order_id in self._completed_orders:
                    continue
                
                # Skip past-deadline orders (will be cancelled by _check_spread_collapse)
                opp = self.scanner.get_active_opportunity(order.token_id)
                if opp and self._is_past_deadline(opp):
                    continue
                
                # Compute synthetic fair value (cost-based ceiling)
                if opposite_position:
                    cost_fair = 1.0 - opposite_position.avg_price
                elif opposite_order:
                    cost_fair = 1.0 - opposite_order.price
                else:
                    continue
                
                order_info = self.bot_state.get_order_info(order.token_id)
                if not order_info:
                    continue
                
                orders_to_monitor.append({
                    "token_id": order.token_id,
                    "order_info": order_info,
                    "cost_fair": cost_fair,
                })
        
        if not orders_to_monitor:
            return
        
        # Phase 2: Read order books from WS cache (no REST calls)
        book_ws = self.ws_handler.book_ws if self.ws_handler else None
        rest_fallback_tokens = []
        books = {}
        
        for order_ctx in orders_to_monitor:
            token_id = order_ctx["token_id"]
            ws_book = book_ws.get_book(token_id) if book_ws else None
            if ws_book:
                books[token_id] = ws_book
            else:
                rest_fallback_tokens.append(token_id)
        
        # REST fallback for tokens without WS data (e.g., just subscribed, stale)
        import time as _time
        if rest_fallback_tokens:
            t0 = _time.monotonic()
            rest_books = await self.poly_client.get_order_books_batch(rest_fallback_tokens)
            elapsed = _time.monotonic() - t0
            books.update(rest_books)
            print(f"   📊 [MONITOR] WS: {len(books) - len(rest_books)}, REST fallback: {len(rest_fallback_tokens)} tokens ({elapsed:.1f}s)")
        
        # Phase 3: Process all orders in parallel (cancel+replace is ~1-2s each)
        # _adjusting_tokens lock prevents concurrent modifications to the same token
        tasks = []
        for order_ctx in orders_to_monitor:
            token_id = order_ctx["token_id"]
            book = books.get(token_id)
            if not book:
                continue
            
            tasks.append(
                self.order_monitor.monitor_order(
                    token_id=token_id,
                    poly_client=self.poly_client,
                    watcher=self.watcher,
                    hedge_token_ids=self._hedge_token_ids,
                    order_info=order_ctx["order_info"],
                    cost_fair=order_ctx["cost_fair"],
                    book=book,
                    book_ws=book_ws,
                )
            )
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    # ===== Reactive spread cancel callback =====
    
    async def _reactive_spread_cancel(self, match, order_info, spread):
        """Called by ReactiveBookHandler when spread collapses below threshold."""
        order_id = order_info.get("order_id", "")
        team_name = order_info.get("team_name", "")
        token_id = order_info.get("token_id", "")
        
        # Check if already completed
        if order_id in self._completed_orders:
            return
        
        try:
            cancelled, was_complete = await self.executor.cancel_order(
                order_id, force=True, team_name=team_name
            )
            if was_complete:
                self._completed_orders.add(order_id)
            if cancelled or was_complete:
                print(f"📉 [REACTIVE] Spread collapsed to {spread*100:.1f}c — cancelled {team_name}")
            
            # ALWAYS clean up BotState — whether cancel succeeded or order was already gone.
            # Without this, a stale order stays in state and triggers infinite cancel retries.
            self.bot_state.cancel_order(order_id)
            # Update match's order references
            if match.order1 and match.order1.order_id == order_id:
                match.order1 = None
            elif match.order2 and match.order2.order_id == order_id:
                match.order2 = None
            self._weather_orders.discard(token_id)
        except Exception as e:
            print(f"   ❌ [REACTIVE] Spread cancel failed ({team_name}): {e}")
    
    # ===== Override position monitor for deadline checks + hedging =====
    
    async def _position_monitor_loop(self):
        """Reduced to deadline checks and hedge seeking only.
        
        Outbid, price improvement, and spread collapse are now
        handled reactively by ReactiveBookHandler on each WS book update.
        """
        await asyncio.sleep(15)
        
        while self._running:
            try:
                # Deadline protection — cancel orders past their trading deadline
                await self._check_deadlines()
                
                # Odds coverage — cancel orders on markets now covered by SportsBot
                await self._check_odds_coverage()
                
                # Seek hedges via HedgeSeeker for filled positions needing coverage
                positions_needing_hedge = self.bot_state.get_positions_needing_hedge()
                
                # Filter out positions past deadline — no point hedging dead markets
                filtered_positions = {}
                for key, hedge_info in positions_needing_hedge.items():
                    entry_token = hedge_info.get("entry_token_id", "")
                    opp = self.scanner.get_active_opportunity(entry_token)
                    if opp and self._is_past_deadline(opp):
                        continue  # Skip — market past deadline
                    filtered_positions[key] = hedge_info
                
                hedge_tasks = [
                    self.hedge_seeker.seek_hedge_for_position(
                        key=key,
                        hedge_info=hedge_info,
                        poly_client=self.poly_client,
                        watcher=self.watcher,
                        hedge_token_ids=self._hedge_token_ids,
                        book_ws=self.ws_handler.book_ws,
                    )
                    for key, hedge_info in filtered_positions.items()
                ]
                if hedge_tasks:
                    await asyncio.gather(*hedge_tasks, return_exceptions=True)
                
            except Exception as e:
                print(f"❌ [SPREAD] Position monitor error: {e}")
            
            await asyncio.sleep(30)  # Can run less frequently now — reactive handles urgent stuff
    
    async def _check_deadlines(self):
        """Check and cancel orders past their trading deadline.
        
        Extracted from the old _check_spread_collapse — deadline checks
        are clock-based and can't be reactive.
        """
        for match_id, match in list(self.bot_state._matches.items()):
            if not (match_id.startswith("weather:") or match_id.startswith("stock:") or match_id.startswith("ncaab:") or match_id.startswith("mentions:") or match_id.startswith("spread:") or match_id.startswith("rugby:") or match_id.startswith("tennis:") or match_id.startswith("cricket:") or match_id.startswith("hockey:") or match_id.startswith("ufc:") or match_id.startswith("football:") or match_id.startswith("unknown:")):
                continue
            
            for order in [match.order1, match.order2]:
                if not order or not order.is_open:
                    continue
                if order.token_id in self._hedge_token_ids:
                    continue
                if order.order_id in self._completed_orders:
                    continue
                
                # Get cached opportunity for deadline check
                opp = self.scanner.get_active_opportunity(order.token_id)
                if not opp or not self._is_past_deadline(opp):
                    continue
                
                emoji = {"stock": "⏰", "mentions": "🎤", "weather": "🌡️", "cricket": "🏏", "hockey": "🏒", "ufc": "🥊", "football": "⚽"}.get(opp.market_type, "🔴")
                try:
                    cancelled, was_complete = await self.executor.cancel_order(order.order_id, force=True, team_name=order.team)
                    if cancelled:
                        print(f"{emoji} [DEADLINE] {opp.market_type} past deadline — cancelled {order.team} | {opp.event_title[:70]}")
                    if was_complete:
                        self._completed_orders.add(order.order_id)
                    self.bot_state.cancel_order(order.order_id)
                    if match.order1 and match.order1.order_id == order.order_id:
                        match.order1 = None
                    elif match.order2 and match.order2.order_id == order.order_id:
                        match.order2 = None
                    self._weather_orders.discard(order.token_id)
                except Exception as e:
                    print(f"   ❌ Deadline cancel failed ({order.team}): {e}")
    
    async def _check_odds_coverage(self):
        """Cancel active orders on markets now covered by external odds.
        
        Runs periodically (same cadence as deadline checks). When new odds
        arrive in sports_odds_v2 for a match the SpreadBot already has orders
        on, we cancel those orders so the SportsBot can take over.
        
        Uses match_id for team extraction (always available, even for hydrated
        orders) and order.team for market type detection (contains O/U/Spread info).
        """
        await self.odds_filter.ensure_fresh()
        if self.odds_filter.match_count == 0:
            return
        
        # match_id prefixes that correspond to sports with external odds
        SPORTS_PREFIXES = (
            "football:", "hockey:", "ncaab:", "rugby:", "cricket:",
            "ufc:", "basketball:", "tennis:",
        )
        cancelled_count = 0
        
        for match_id, match in list(self.bot_state._matches.items()):
            # Only check sports matches (not weather/stock/mentions)
            if not any(match_id.startswith(p) for p in SPORTS_PREFIXES):
                continue
            
            # Skip matches that already have filled positions (don't cancel hedges)
            if match.has_position_on_side1 or match.has_position_on_side2:
                continue
            
            for order in [match.order1, match.order2]:
                if not order or not order.is_open:
                    continue
                if order.token_id in self._hedge_token_ids:
                    continue
                if order.order_id in self._completed_orders:
                    continue
                
                # Extract teams from match state (always populated, even for hydrated orders)
                t1 = match.team1 or ""
                t2 = match.team2 or ""
                if not t1 or not t2:
                    continue
                
                # Strip O/U and Spread suffixes from team names for matching
                import re
                t1_clean = re.sub(r'[:\s]+(?:Set \d+ (?:Games )?)?O/U\s+[\d.]+$', '', t1)
                t2_clean = re.sub(r'[:\s]+(?:Set \d+ (?:Games )?)?O/U\s+[\d.]+$', '', t2)
                t1_clean = re.sub(r'^Spread:\s*', '', t1_clean).strip()
                t2_clean = re.sub(r'^Spread:\s*', '', t2_clean).strip()
                t1_clean = re.sub(r'\s*\([+-]?[\d.]+\)$', '', t1_clean).strip()
                t2_clean = re.sub(r'\s*\([+-]?[\d.]+\)$', '', t2_clean).strip()
                
                # For O/U markets where team1 is like "Over" and team2 is like "Under",
                # extract real teams from match_id
                if t1_clean.lower() in ("over", "under", "yes", "no") or t2_clean.lower() in ("over", "under", "yes", "no"):
                    parts = match_id.split(":")
                    if ":vs:" in match_id and len(parts) >= 4:
                        # match_id format: game:team1:vs:team2
                        vs_idx = parts.index("vs")
                        t1_clean = " ".join(parts[1:vs_idx])
                        t2_clean = " ".join(parts[vs_idx+1:])
                    else:
                        continue
                
                if not t1_clean or not t2_clean:
                    continue
                
                # Determine market type from order.team
                order_team = order.team or ""
                odds_mt, odds_line = parse_spread_opportunity_market(order_team, "")
                
                if not self.odds_filter.is_covered(t1_clean, t2_clean, odds_mt, odds_line):
                    continue
                
                # This market is now covered by external odds — cancel
                try:
                    cancelled, was_complete = await self.executor.cancel_order(
                        order.order_id, force=True, team_name=order.team
                    )
                    if cancelled:
                        cancelled_count += 1
                    if was_complete:
                        self._completed_orders.add(order.order_id)
                    self.bot_state.cancel_order(order.order_id)
                    if match.order1 and match.order1.order_id == order.order_id:
                        match.order1 = None
                    elif match.order2 and match.order2.order_id == order.order_id:
                        match.order2 = None
                    self._weather_orders.discard(order.token_id)
                except Exception as e:
                    print(f"   ❌ Odds coverage cancel failed ({order.team}): {e}")
        
        if cancelled_count > 0:
            print(f"   🚫 [ODDS COVERAGE] Cancelled {cancelled_count} orders now covered by external odds")
    
    # ===== Override timing loops =====
    
    async def _book_validation_loop(self):
        """Validate WebSocket book state periodically."""
        from src.polymarket.market_client import PolymarketEsportsClient
        
        await asyncio.sleep(60)  # 60s initial delay
        interval = SPREAD_CONFIG["book_validation_interval"]
        
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
                
                async with PolymarketEsportsClient() as client:
                    rest_bids = await client.get_best_bids(tokens)
                
                # Seed from REST
                await book_ws.seed_all_from_rest(rest_bids)
                
            except Exception as e:
                print(f"⚠️ [SPREAD] Book validation error: {e}")
            
            await asyncio.sleep(interval)
    
    async def run(self):
        """Main run loop - SpreadBot skips OddsService.
        
        NOTE: No _ws_adjustment_loop — outbids are handled reactively
        by ReactiveBookHandler on each WS book update.
        """
        await self.start()
        
        # Wire up reactive handler AFTER ws_handler connects
        self.ws_handler.set_reactive_handler(self.reactive_book_handler)
        self.reactive_book_handler.set_watcher(self.watcher, self._hedge_token_ids)
        self.reactive_book_handler.set_book_ws(self.ws_handler.book_ws)
        
        try:
            await asyncio.gather(
                # NOTE: NO odds_service.run() - SpreadBot doesn't need scraped odds
                # NOTE: NO _ws_adjustment_loop - outbids handled reactively
                self.user_ws.run() if self.user_ws else asyncio.sleep(0),
                self._signal_loop(),
                self._position_monitor_loop(),
                self._status_loop(),
                self._state_resync_loop(),
                self._book_validation_loop(),
                self.health_server.run_forever(lambda: self._running),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()
    
    async def stop(self):
        """Stop bot and cleanup."""
        await super().stop()
        await self.weather_client.close()
