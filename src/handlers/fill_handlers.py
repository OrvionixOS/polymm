"""
Fill handlers - processes order fill events.

Key feature: Natural arb detection.
When we fill an entry, we check if there's already an order/position on the
opposite side of the match. If so, it's a "natural arb" and no hedge is needed.
"""
from datetime import datetime, timezone
from typing import Optional

from src.services.odds_service import OddsService
from src.services.telegram_alerts import TelegramAlerts
from src.execution.hedge_finder import HedgeFinder
from src.execution.order_watcher import Position, PositionState
from src.data.recorder import DataRecorder, get_recorder
from src.state.bot_state import get_bot_state
from src.core.match_id import parse_teams_from_question


class FillHandler:
    """
    Handles order fill events for both entry and hedge orders.
    
    Requires injection of:
    - odds_service: OddsService instance
    - alerts: TelegramAlerts instance
    - hedge_finder: HedgeFinder instance
    - recorder: DataRecorder instance (optional, uses singleton if not provided)
    """
    
    def __init__(
        self,
        odds_service: OddsService,
        alerts: TelegramAlerts,
        hedge_finder: HedgeFinder,
        recorder: Optional[DataRecorder] = None,
        is_live: bool = False,
    ):
        self.odds_service = odds_service
        self.alerts = alerts
        self.hedge_finder = hedge_finder
        self.recorder = recorder or get_recorder()
        self.is_live = is_live
        self.book_ws = None  # Set by BaseBot after WebSocket connects
    
    def _get_market_type(self, game: str) -> str:
        """Determine market_type from game field."""
        # Spread-bot managed market types
        _SPREAD_GAME_TYPES = {
            "weather", "stock", "rugby", "mentions", "basketball",
            "tennis", "football", "hockey", "ufc", "cricket",
        }
        if game in _SPREAD_GAME_TYPES:
            return game
        elif self.is_live:
            return "esports_live"
        else:
            return "esports"
    
    async def on_fill(
        self,
        position: Position,
        filled_shares: float,
        watcher,  # OrderWatcher - avoid circular import
    ):
        """Handle entry fill - place hedge."""
        # Check if this is a hydrated order (from previous session)
        is_hydrated = getattr(position, 'is_hydrated', False)
        
        if is_hydrated:
            await self._handle_hydrated_fill(position, filled_shares)
            return
        
        await self._handle_entry_fill(position, filled_shares, watcher)
    
    async def _handle_hydrated_fill(self, position: Position, filled_shares: float):
        """Handle fill for hydrated order - send notification but can't auto-hedge."""
        team = position.entry_team
        price = position.entry_price
        match_name = getattr(position, 'match_name', f"(Hydrated) {team}")
        token_id = getattr(position, 'entry_token_id', None)
        
        # Get team1/team2 from position for fair value lookup
        team1 = getattr(position, 'team1', None)
        team2 = getattr(position, 'team2', None)
        
        # PRIORITY 1: Use BotState's canonical fair value (handles spreads/totals correctly)
        # This is the single source of truth — set by OddsService v2 push with proper alignment
        fair_value = None
        odds_age = None
        
        bot_state = get_bot_state()
        if token_id:
            match = bot_state.get_match_by_token(token_id)
            if match:
                order = match.get_order_for_token(token_id)
                if order:
                    fair_value = match.get_fair_value_for_order(order)
                if fair_value is None:
                    # No order found (already filled?) — try token-level lookup
                    fresh_fair = match.get_fair_for_token(token_id)
                    if fresh_fair is not None:
                        fair_value = fresh_fair
        
        # PRIORITY 2: Fresh lookup from OddsService (for h2h esports where per-order fair isn't set)
        if fair_value is None and team1 and team2:
            fair_value, odds_age = self._lookup_fresh_fair_value_with_age(
                team1, team2, team, game=getattr(position, 'game', None)
            )
        
        # PRIORITY 3: Parse match_name and look up from odds service
        if fair_value is None and match_name:
            fair_value = self._lookup_fair_value(match_name, team)
        
        # Check if this is a hedge fill (we have position on opposite side)
        is_hedge = False
        arb_profit_pct = None
        opposite_team = None
        opposite_price = None
        
        # Use bot_state for hedge detection
        bot_state = get_bot_state()
        match_id = getattr(position, 'match_id', None)
        
        if match_id:
            match = bot_state.get_match(match_id)
            if match:
                # Check if we have a position on the opposite side
                # MatchState has position1/position2 for each side
                opposite_pos = None
                if token_id == match.token1 and match.position2:
                    opposite_pos = match.position2
                elif token_id == match.token2 and match.position1:
                    opposite_pos = match.position1
                
                if opposite_pos:
                    # Found position on opposite side - this is a hedge!
                    is_hedge = True
                    opposite_team = opposite_pos.team
                    opposite_price = opposite_pos.avg_price
                    
                    # Calculate arb profit: $1 payout - total cost
                    total_cost = price + opposite_price
                    arb_profit_pct = ((1.0 - total_cost) / total_cost) * 100 if total_cost > 0 else 0
        
        # Calculate edge if we have fair value
        edge_pct = None
        if fair_value is not None and fair_value > 0:
            edge_pct = (fair_value - price) * 100
            edge_dollars = (fair_value - price) * filled_shares
            hedge_str = " (HEDGE)" if is_hedge else ""
            odds_age_str = f" [odds {int(odds_age)}s old]" if odds_age else ""
            print(f"🔔 HYDRATED ORDER FILLED{hedge_str}: {team} - {filled_shares} shares @ {price:.2f}{odds_age_str}")
            if is_hedge and arb_profit_pct is not None:
                print(f"   🎯 ARB COMPLETE: {opposite_team} @ {opposite_price:.2f} + {team} @ {price:.2f} = {arb_profit_pct:+.1f}% profit")
            else:
                print(f"   📊 Fair: {fair_value*100:.0f}% | Edge: {edge_pct:+.1f}% (${edge_dollars:+.2f})")
        else:
            hedge_str = " (HEDGE)" if is_hedge else ""
            print(f"🔔 HYDRATED ORDER FILLED{hedge_str}: {team} - {filled_shares} shares @ {price:.2f}")
            if is_hedge and arb_profit_pct is not None:
                print(f"   🎯 ARB COMPLETE: {opposite_team} @ {opposite_price:.2f} + {team} @ {price:.2f} = {arb_profit_pct:+.1f}% profit")
            else:
                # Only warn for esports markets — spread bot markets have no external odds
                game = getattr(position, 'game', None)
                _SPREAD_GAMES = {"weather", "stock", "basketball", "mentions", "rugby", "tennis", "cricket", "football", "hockey", "ufc"}
                if game not in _SPREAD_GAMES:
                    print(f"   ⚠️ Could not determine fair value from odds")
        
        # CRITICAL: Update BotState with the fill so hedge calculations are accurate!
        # Without this, get_positions_needing_hedge uses stale position data and places
        # duplicate hedge orders.
        order_id = getattr(position, 'order_id', None)
        if order_id:
            bot_state.update_order_fill(order_id, filled_shares, price)
        
        # Get live spread at fill time (before recording block so it's available for alert)
        spread_at_fill = await self._get_live_spread(token_id, price)
        
        # Record hydrated fill to database
        if order_id:
            # Get team names for match context
            team1 = getattr(position, 'team1', None) or team
            team2 = getattr(position, 'team2', None) or opposite_team or "Unknown"
            
            # Get raw odds snapshot
            raw_odds = self._get_raw_odds_snapshot(team1, team2)
            
            await self.recorder.record_filled_order(
                order_id=order_id,
                token_id=token_id,
                side="hedge" if is_hedge else "entry",
                team=team,
                price=price,
                shares=filled_shares,
                condition_id=position.condition_id if hasattr(position, 'condition_id') else None,
                match_id=match_id,
                game=getattr(position, 'game', None),
                team1=team1,
                team2=team2,
                market_type=self._get_market_type(getattr(position, 'game', '')),
                placed_at=getattr(position, 'placed_at', None),
                fair_value=fair_value,
                raw_odds=raw_odds,
                spread_at_fill=spread_at_fill,
                trading_deadline=self._get_trading_deadline(token_id),
            )
            
            # Record order lifecycle status as FILLED
            await self.recorder.record_order_final_status(
                order_id=order_id,
                status="FILLED",
            )
            
            # Record arbitrage if this is a hedge fill completing an arb
            if is_hedge and opposite_team and opposite_price is not None:
                # Get the entry order ID from the opposite side's order
                # The entry was already recorded as filled_order, so we can reference it
                entry_order_id = None
                entry_filled_at = None
                
                # Resolve proper team names from match state
                # For hydrated weather fills, `team` can be just "Yes"/"No" instead of
                # the canonical name like "ankara be:10°C". Use match.team1/team2 instead.
                resolved_hedge_team = team
                resolved_entry_team = opposite_team
                
                if match:
                    # Get the opposite order (entry order)
                    opposite_order = None
                    if token_id == match.token1 and match.order2:
                        opposite_order = match.order2
                        resolved_hedge_team = match.team1 or team
                        resolved_entry_team = match.team2 or opposite_team
                    elif token_id == match.token2 and match.order1:
                        opposite_order = match.order1
                        resolved_hedge_team = match.team2 or team
                        resolved_entry_team = match.team1 or opposite_team
                    
                    if opposite_order:
                        entry_order_id = opposite_order.order_id
                        entry_filled_at = getattr(opposite_order, 'placed_at', None)
                
                # Only record if we have a real entry order ID
                # (Skip if we can't find it - avoids FK constraint errors)
                if entry_order_id:
                    await self.recorder.record_arbitrage(
                        match_id=match_id,
                        condition_id=getattr(position, 'condition_id', None),
                        game=getattr(position, 'game', None),
                        team1=team1,
                        team2=team2,
                        entry_order_id=entry_order_id,
                        entry_team=resolved_entry_team,
                        entry_price=opposite_price,
                        entry_shares=filled_shares,
                        entry_filled_at=entry_filled_at,
                        hedge_order_id=order_id,
                        hedge_team=resolved_hedge_team,
                        hedge_price=price,
                        hedge_shares=filled_shares,
                        hedge_filled_at=datetime.now(timezone.utc),
                    )
        
        # Detect if this is a weather/spread-bot market (no fair value)
        game = getattr(position, 'game', None)
        is_weather = game in ("weather", "stock", "basketball", "mentions", "rugby", "tennis", "cricket", "football")
        
        await self.alerts.on_hydrated_fill(
            match=match_name if match_name else f"(Hydrated) {team}",
            team=team,
            price=price,
            shares=filled_shares,
            edge=edge_pct / 100 if edge_pct is not None else None,
            fair_value=fair_value * 100 if fair_value else None,
            is_hedge=is_hedge,
            opposite_team=opposite_team,
            opposite_price=opposite_price,
            arb_profit_pct=arb_profit_pct,
            is_live=self.is_live,
            is_weather=is_weather,
            odds_age_seconds=odds_age,
            spread_at_fill=spread_at_fill,
        )
    
    async def _handle_entry_fill(self, position: Position, filled_shares: float, watcher):
        """Handle fill for regular entry order - place hedge (unless natural arb)."""
        match_name = f"{position.team1} vs {position.team2}"
        match_id = getattr(position, 'match_id', None)
        
        # Look up FRESH fair value at fill time (not the stale value from order placement)
        fair_value, odds_age = self._lookup_fresh_fair_value_with_age(
            position.team1, position.team2, position.entry_team, game=getattr(position, 'game', None)
        )
        
        # Fallback to order's fair value if fresh lookup failed
        if fair_value is None:
            fair_value = getattr(position.entry_order, 'fair_value', 0) if position.entry_order else 0
        
        # Calculate edge using fresh fair value
        edge = fair_value - position.entry_avg_price if fair_value > 0 else 0
        
        # Get raw odds snapshot for recording
        raw_odds = self._get_raw_odds_snapshot(position.team1, position.team2)
        spread_at_fill = await self._get_live_spread(position.entry_token_id, position.entry_avg_price)
        
        # Record filled order
        order_id = position.entry_order.order.order_id if position.entry_order else None
        if order_id:
            await self.recorder.record_filled_order(
                order_id=order_id,
                token_id=position.entry_token_id,
                side="entry",
                team=position.entry_team,
                price=position.entry_avg_price,
                shares=filled_shares,
                condition_id=position.condition_id,
                match_id=match_id,
                game=position.game,
                team1=position.team1,
                team2=position.team2,
                market_type=self._get_market_type(position.game),
                placed_at=getattr(position.entry_order, 'placed_at', None) if position.entry_order else None,
                fair_value=fair_value if fair_value > 0 else None,
                raw_odds=raw_odds,
                spread_at_fill=spread_at_fill,
                trading_deadline=self._get_trading_deadline(position.entry_token_id),
            )
            
            # Record order lifecycle status as FILLED
            await self.recorder.record_order_final_status(
                order_id=order_id,
                status="FILLED",
            )
        
        # Update BotState with the fill
        bot_state = get_bot_state()
        if order_id:
            bot_state.update_order_fill(order_id, filled_shares, position.entry_avg_price)
        
        # Detect if this is a weather/spread-bot market (no fair value)
        game = getattr(position, 'game', None)
        is_weather = game in ("weather", "stock", "basketball", "mentions", "rugby", "tennis", "cricket", "football")
        
        # Send fill alert with edge and odds freshness
        await self.alerts.on_entry_fill(
            match=match_name,
            team=position.entry_team,
            price=position.entry_avg_price,
            shares=filled_shares,
            edge=edge,
            fair_value=fair_value * 100 if fair_value > 0 else 0,
            is_live=self.is_live,
            is_weather=is_weather,
            odds_age_seconds=odds_age,
            spread_at_fill=spread_at_fill,
        )
        
        # ===== NATURAL ARB CHECK =====
        # Before placing a hedge, check if we already have coverage on the opposite side.
        # If so, this is a "natural arb" - no hedge order needed!
        if watcher.has_opposite_coverage(position.entry_token_id):
            match_state = watcher.get_match_state(match_id)
            opposite_team = position.hedge_team
            
            # Get details of the opposite side for logging
            if match_state:
                if match_state.has_order_on_side1 or match_state.has_order_on_side2:
                    print(f"🎯 NATURAL ARB DETECTED: {position.entry_team} fill completes arb with open order on {opposite_team}")
                elif match_state.has_position_on_side1 or match_state.has_position_on_side2:
                    print(f"🎯 NATURAL ARB DETECTED: {position.entry_team} fill completes arb with position on {opposite_team}")
                else:
                    print(f"🎯 NATURAL ARB DETECTED: {position.entry_team} - already have coverage on {opposite_team}")
                
                bot_state.record_natural_arb_detected()
                
                # Mark position as hedged (will be completed when other side fills)
                position.state = PositionState.HEDGE_PENDING
                
                return
        
        # Calculate hedge
        hedge_fair = 1.0 - position.entry_order.fair_value
        requested_size = None
        if position.entry_order and getattr(position.entry_order, "order", None):
            requested_size = getattr(position.entry_order.order, "size", None)
        calc = self.hedge_finder.calculate_hedge(
            entry_price=position.entry_avg_price,
            entry_shares=position.entry_filled_shares,
            hedge_fair_value=hedge_fair,
            requested_shares=requested_size,
        )
        
        if not calc.can_hedge:
            print(f"⚠️ Cannot hedge profitably: {calc.reason}")
            return
        
        # Place hedge order
        await watcher.place_hedge_order(
            position=position,
            hedge_price=calc.hedge_price,
            shares=position.entry_filled_shares,
        )

    async def on_hedge_fill(self, position: Position, filled_shares: float):
        """Handle hedge fill - send notification and record."""
        match_name = f"{position.team1} vs {position.team2}"
        
        # Update BotState with the hedge fill
        bot_state = get_bot_state()
        
        # Record hedge fill
        hedge_order_id = position.hedge_order.order.order_id if position.hedge_order else None
        if hedge_order_id:
            bot_state.update_order_fill(hedge_order_id, filled_shares, position.hedge_avg_price)
            hedge_fair = 1.0 - (position.entry_order.fair_value if position.entry_order else 0.5)
            raw_odds = self._get_raw_odds_snapshot(position.team1, position.team2)
            spread_at_fill = await self._get_live_spread(position.hedge_token_id, position.hedge_avg_price)
            
            await self.recorder.record_filled_order(
                order_id=hedge_order_id,
                token_id=position.hedge_token_id,
                side="hedge",
                team=position.hedge_team,
                price=position.hedge_avg_price,
                shares=filled_shares,
                condition_id=position.condition_id,
                match_id=position.match_id,
                game=position.game,
                team1=position.team1,
                team2=position.team2,
                market_type=self._get_market_type(position.game),
                placed_at=getattr(position.hedge_order, 'placed_at', None) if position.hedge_order else None,
                fair_value=hedge_fair,
                raw_odds=raw_odds,
                spread_at_fill=spread_at_fill,
                trading_deadline=self._get_trading_deadline(position.hedge_token_id),
            )
            
            # Record order lifecycle status as FILLED
            await self.recorder.record_order_final_status(
                order_id=hedge_order_id,
                status="FILLED",
            )
        
        # Check if fully hedged
        if position.state == PositionState.HEDGED:
            # Record the completed arbitrage
            entry_order_id = position.entry_order.order.order_id if position.entry_order else None
            if entry_order_id and hedge_order_id:
                await self.recorder.record_arbitrage(
                    match_id=getattr(position, 'match_id', None),
                    condition_id=getattr(position, 'condition_id', None),
                    game=getattr(position, 'game', None),
                    team1=position.team1,
                    team2=position.team2,
                    entry_order_id=entry_order_id,
                    entry_team=position.entry_team,
                    entry_price=position.entry_avg_price,
                    entry_shares=position.entry_filled_shares,
                    entry_filled_at=getattr(position.entry_order, 'filled_at', None) if position.entry_order else None,
                    hedge_order_id=hedge_order_id,
                    hedge_team=position.hedge_team,
                    hedge_price=position.hedge_avg_price,
                    hedge_shares=position.hedge_filled_shares,
                    hedge_filled_at=datetime.now(timezone.utc),
                )
            
            # Detect if this is a weather/spread-bot market (no fair value)
            game = getattr(position, 'game', None)
            is_weather = game in ("weather", "stock", "basketball", "mentions", "rugby", "tennis", "cricket", "football")
            
            # Send fully hedged notification
            await self.alerts.on_position_hedged(
                match=match_name,
                entry_team=position.entry_team,
                hedge_team=position.hedge_team,
                total_cost=position.total_cost,
                profit=position.locked_profit,
                profit_pct=position.profit_percent,
                is_live=self.is_live,
                is_weather=is_weather,
            )
        else:
            # Partial hedge fill - just log for now
            print(f"📊 Partial hedge fill: {filled_shares} shares of {position.hedge_team}")
    
    def _get_raw_odds_snapshot(self, team1: str, team2: str) -> dict | None:
        """Get raw odds from all bookmakers for this match.
        
        IMPORTANT: Includes team names in each source entry so we know
        which fair_prob belongs to which team. This prevents bugs when
        Position.team1/team2 order differs from OddsService's alphabetical order.
        """
        try:
            from src.core.match_id import normalize_team
            
            matches = self.odds_service.get_matches(min_sources=1, fresh_only=False)
            
            # Use canonical normalize_team for consistent matching
            team1_norm = normalize_team(team1)
            team2_norm = normalize_team(team2)
            
            for odds_match in matches:
                t1_norm = normalize_team(odds_match.team1)
                t2_norm = normalize_team(odds_match.team2)
                
                # Check if this is our match (either order)
                if {team1_norm, team2_norm} == {t1_norm, t2_norm}:
                    # Build raw odds dict - sources is Dict[str, MatchOdds]
                    # CRITICAL: Include team names so we know which fair_prob belongs to whom
                    raw_odds = {}
                    for source_name, odds in odds_match.sources.items():
                        raw_odds[source_name] = {
                            "team1": odds.team1,  # Team that fair_prob1 belongs to
                            "team2": odds.team2,  # Team that fair_prob2 belongs to
                            "odds1": odds.odds1,
                            "odds2": odds.odds2,
                            "fair_prob1": odds.fair_prob1,
                            "fair_prob2": odds.fair_prob2,
                        }
                    return raw_odds if raw_odds else None
        except Exception as e:
            print(f"   ⚠️ Could not get raw odds snapshot: {e}")
        
        return None
    
    async def _get_live_spread(self, token_id: str, fill_price: float) -> float | None:
        """Get live bid-ask spread for the filled token.
        
        Returns best_ask - best_bid for this token's order book.
        Measures market tightness at fill time — useful for evaluating
        whether the spread collapse check should have cancelled the order.
        
        Uses WS book data first (instant), falls back to REST API call
        to guarantee spread is always recorded.
        """
        # Try WS book first (instant, zero API calls)
        if self.book_ws:
            try:
                price = self.book_ws.get_price(token_id)
                if price and price.best_ask is not None and price.best_bid is not None:
                    return round(price.best_ask - price.best_bid, 4)
            except Exception:
                pass
        
        # REST fallback — guarantees spread for hydrated fills where WS isn't seeded yet
        try:
            from src.polymarket.market_client import PolymarketEsportsClient
            async with PolymarketEsportsClient() as client:
                book = await client.get_order_book(token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids and asks:
                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])
                if best_ask > best_bid:
                    return round(best_ask - best_bid, 4)
        except Exception as e:
            print(f"   ⚠️ Could not get live spread (REST fallback): {e}")
        
        return None
    
    def _get_trading_deadline(self, token_id: str):
        """Get trading deadline from BotState's MatchState for this token."""
        try:
            bot_state = get_bot_state()
            match = bot_state.get_match_by_token(token_id)
            return match.trading_deadline if match else None
        except Exception:
            return None
    
    def _lookup_fair_value(self, match_name: str, team: str) -> float | None:
        """Try to look up fair value from odds service."""
        try:
            # Parse team names using canonical helper
            game, team1, team2 = parse_teams_from_question(match_name)
            
            if not team1 or not team2:
                return None
            
            # Use centralized function with game filter
            from src.scanning.team_matcher import get_fair_value_for_match
            
            fair, _ = get_fair_value_for_match(
                team1, team2, team, self.odds_service, game=game
            )
            return fair
        except Exception as e:
            print(f"   ⚠️ Could not look up fair value: {e}")
        
        return None
    
    def _lookup_fresh_fair_value_with_age(
        self, team1: str, team2: str, our_team: str, game: str = None
    ) -> tuple[float | None, float | None]:
        """
        Look up fresh fair value from odds service at fill time.
        
        Returns:
            tuple of (fair_value, odds_age_seconds) or (None, None) if not found
        """
        try:
            from src.scanning.team_matcher import get_fair_value_for_match
            from datetime import datetime, timezone
            
            # Use centralized function with game filter
            fair_value, matching_odds = get_fair_value_for_match(
                team1, team2, our_team, self.odds_service, game=game
            )
            
            if fair_value is None:
                return None, None
            
            # Calculate odds age from timestamp
            odds_age = None
            if matching_odds and hasattr(matching_odds, 'timestamp') and matching_odds.timestamp:
                now = datetime.now(timezone.utc)
                odds_age = (now - matching_odds.timestamp).total_seconds()
            
            return fair_value, odds_age
        except Exception as e:
            print(f"   ⚠️ Could not look up fresh fair value: {e}")
        
        return None, None

