"""
Live Opportunity Scanner - Finds opportunities in LIVE matches only.

Adapted from OpportunityScanner with key differences:
- Only scans live matches (inverts the is_live filter)
- Uses esports_odds_live table via OddsService
- Requires 30 second freshness for live odds
- Same arbitrage logic as pre-match scanning
"""
from typing import Optional, List

from src.services.odds_service import OddsService, AggregatedMatch
from src.polymarket.market_client import PolymarketEsportsClient, PolymarketEsportsEvent
from src.state.market_cache import MarketCache
from src.execution.hedge_finder import HedgeFinder
from src.scanning.team_matcher import align_teams_with_bids
from src.core.match_id import is_individual_game_market


class LiveOpportunityScanner:
    """
    Scans for trading opportunities in LIVE matches only.
    
    Key differences from OpportunityScanner:
    - Only processes live events (is_live=True)
    - Uses esports_odds_live table with 30s freshness requirement
    - Same arbitrage/edge calculation logic
    """
    
    def __init__(
        self,
        odds_service: OddsService,
        cache: MarketCache,
        hedge_finder: HedgeFinder,
        config: dict,
    ):
        self.odds_service = odds_service
        self.cache = cache
        self.hedge_finder = hedge_finder
        self.config = config
        
        # Deduplication sets for warnings
        self._warned_no_markets: set = set()
        
        # Force printing on first scan
        self._first_scan: bool = True
    
    async def scan_for_opportunities(
        self,
        watcher,  # OrderWatcher - avoid circular import
        cancel_live_orders_fn=None,  # Not used in live mode - we WANT live orders
    ) -> list[dict]:
        """
        Find ALL trading opportunities in LIVE matches.
        
        Returns list of opportunity dicts (may be empty).
        """
        # Get fresh LIVE odds from bookmakers (max 30 second freshness)
        matches = self._get_live_matches()
        
        if self._first_scan:
            print(f"🔴 [LIVE] First scan: {len(matches) if matches else 0} live matches from odds service")
        
        if not matches:
            if self._first_scan:
                print("⚠️ [LIVE] No live matches with fresh odds")
            return []
        
        opportunities = []  # Collect ALL opportunities
        found_tokens = set()  # Track tokens found in this scan to prevent duplicates
        matched_count = 0
        
        try:
            async with PolymarketEsportsClient() as poly_client:
                # Fetch esports events - we only want LIVE ones
                poly_events = await poly_client.get_all_esports_events()
                
                # CRITICAL: Only keep LIVE events (opposite of SportsBot)
                live_events = [e for e in poly_events if e.is_live and not e.is_finished]
                
                if self._first_scan:
                    print(f"🔴 [LIVE] Found {len(live_events)} live Polymarket events")
                
                for odds_match in matches:
                    # Skip if we have unhedged position (same as SportsBot)
                    has_unhedged_position = watcher.bot_state.has_unhedged_position_for_match(odds_match.match_id)
                    if has_unhedged_position:
                        # Still show match in monitoring mode for visibility
                        poly_event = self._find_poly_event(odds_match, live_events)
                        if poly_event and poly_event.markets:
                            market = self._find_winner_market(poly_event)
                            if market and market.get("outcomePrices"):
                                await self.debug_edge(
                                    odds_match, market, poly_client,
                                    watcher.bot_state.get_active_token_ids(),
                                    watcher.bot_state.get_filled_token_ids(),
                                    monitoring_mode=True
                                )
                        continue
                    
                    # Find matching LIVE Polymarket event
                    poly_event = self._find_poly_event(odds_match, live_events)
                    if not poly_event:
                        continue
                    
                    matched_count += 1
                    
                    # LIVE BOT: We REQUIRE is_live (opposite of SportsBot)
                    if not poly_event.is_live:
                        continue
                    
                    # Hydrate event to get market data
                    if not poly_event.markets:
                        poly_event = await poly_client.hydrate_event(poly_event)
                    
                    # Skip if no markets after hydration
                    if not poly_event.markets:
                        if poly_event.title not in self._warned_no_markets:
                            print(f"   ⚠️ [LIVE] No markets for: {poly_event.title}")
                            self._warned_no_markets.add(poly_event.title)
                        continue
                    
                    # Find the WINNER market (moneyline), skip handicap/spread
                    market = self._find_winner_market(poly_event)
                    if not market:
                        continue
                    
                    # Skip if no prices
                    if not market.get("outcomePrices"):
                        continue
                    
                    # Check for opportunity (skip_tokens prevents duplicate orders)
                    active_tokens = watcher.bot_state.get_tokens_blocking_new_orders()
                    skip_tokens = active_tokens | found_tokens
                    
                    opportunity = await self.check_opportunity(
                        odds_match, market, poly_client, 
                        skip_tokens=skip_tokens
                    )
                    
                    if opportunity:
                        token_id = opportunity.get("token_id")
                        team = opportunity.get("team", "")
                        
                        # Double-check we haven't already found this token
                        if token_id and token_id in found_tokens:
                            continue
                        
                        print(f"   🔴 [LIVE] OPPORTUNITY: {odds_match.match_id}")
                        # Track this token to prevent duplicates within same scan
                        if token_id:
                            found_tokens.add(token_id)
                        
                        # Collect opportunity with context for execution
                        opportunities.append({
                            **opportunity,
                            "odds_match": odds_match,
                            "poly_event": poly_event,
                            "market": market,
                        })
                    else:
                        # Debug: show why no edge
                        await self.debug_edge(
                            odds_match, market, poly_client,
                            watcher.bot_state.get_active_token_ids(),
                            watcher.bot_state.get_filled_token_ids(),
                            monitoring_mode=False
                        )
                
        except Exception as e:
            print(f"❌ [LIVE] Scan error: {e}")
            import traceback
            traceback.print_exc()

        # Print summary on first scan
        if self._first_scan:
            print(f"🔴 [LIVE] First scan complete: {matched_count} matches with Polymarket events")
        
        # Clear first scan flag after completing a scan
        self._first_scan = False
        
        if opportunities:
            print(f"   🔴 [LIVE] Found {len(opportunities)} opportunities to execute in parallel")
        
        return opportunities
    
    def _get_live_matches(self) -> List[AggregatedMatch]:
        """
        Get live matches with fresh odds from esports_odds_live table.
        
        CRITICAL: Uses get_live_odds() which reads from esports_odds_live table,
        NOT the regular esports_odds table that OddsService uses.
        """
        from src.data.supabase_client import get_supabase_client
        from src.core.match_id import normalize_team, normalize_game, make_match_id
        from src.core.vig_removal import proportional_probabilities
        from datetime import datetime, timezone
        
        client = get_supabase_client()
        
        # Fetch from esports_odds_live with 30 second freshness
        records = client.get_live_odds(max_age_seconds=30)
        
        if not records:
            return []
        
        # Convert records to AggregatedMatch format
        matches_by_key: dict = {}
        now = datetime.now(timezone.utc)
        
        for r in records:
            team1 = r.get("team1", "")
            team2 = r.get("team2", "")
            game_raw = r.get("game", "")
            game = normalize_game(game_raw)
            
            # Get or calculate fair probabilities
            odds1 = float(r.get("odds1", 0) or 0)
            odds2 = float(r.get("odds2", 0) or 0)
            fair_prob1 = float(r.get("fair_prob1", 0) or 0)
            fair_prob2 = float(r.get("fair_prob2", 0) or 0)
            
            # Skip end-game matches (odds indicate game is essentially over)
            # Odds < 1.10 = >90% probability, Odds > 10.0 = <10% probability
            END_GAME_ODDS_LOW = 1.10
            END_GAME_ODDS_HIGH = 10.0
            if odds1 > 0 and (odds1 < END_GAME_ODDS_LOW or odds1 > END_GAME_ODDS_HIGH):
                continue
            if odds2 > 0 and (odds2 < END_GAME_ODDS_LOW or odds2 > END_GAME_ODDS_HIGH):
                continue
            
            # Calculate fair probs if missing
            if (fair_prob1 == 0 or fair_prob2 == 0) and odds1 > 1 and odds2 > 1:
                try:
                    fair1, fair2 = proportional_probabilities(odds1, odds2)
                    fair_prob1 = fair1 * 100
                    fair_prob2 = fair2 * 100
                except:
                    pass
            
            if fair_prob1 <= 0 or fair_prob2 <= 0:
                continue
            
            # Create match key with consistent team ordering
            match_key = make_match_id(team1, team2, game)
            
            # Normalize for sorting
            t1_norm = normalize_team(team1)
            t2_norm = normalize_team(team2)
            if t1_norm > t2_norm:
                sorted_team1, sorted_team2 = team2, team1
                sorted_fair1, sorted_fair2 = fair_prob2, fair_prob1
            else:
                sorted_team1, sorted_team2 = team1, team2
                sorted_fair1, sorted_fair2 = fair_prob1, fair_prob2
            
            if match_key not in matches_by_key:
                from src.services.odds_service import AggregatedMatch, MatchOdds
                matches_by_key[match_key] = AggregatedMatch(
                    match_id=match_key,
                    team1=sorted_team1,
                    team2=sorted_team2,
                    game=game,
                    is_live=True,  # All records from esports_odds_live are live
                    timestamp=now,
                )
            
            # Add source to match
            agg = matches_by_key[match_key]
            source = r.get("source", "unknown")
            from src.services.odds_service import MatchOdds
            agg.sources[source] = MatchOdds(
                match_id=match_key,
                team1=sorted_team1,
                team2=sorted_team2,
                odds1=odds1,
                odds2=odds2,
                fair_prob1=sorted_fair1,
                fair_prob2=sorted_fair2,
                source=source,
                game=game,
                is_live=True,
                timestamp=now,
            )
        
        return list(matches_by_key.values())
    
    def _find_poly_event(
        self,
        odds_match: AggregatedMatch,
        poly_events: list,
    ) -> Optional[PolymarketEsportsEvent]:
        """Find matching Polymarket event. Delegates to shared function."""
        from src.scanning.opportunity_scanner import find_poly_event
        return find_poly_event(odds_match, poly_events)
    
    def _find_winner_market(self, poly_event: PolymarketEsportsEvent) -> Optional[dict]:
        """Find the winner/moneyline market from a Polymarket event."""
        if not poly_event.markets:
            return None
        
        for m in poly_event.markets:
            q = m.get("question", "")
            q_lower = q.lower()
            
            # WHITELIST: Moneyline markets MUST contain " vs "
            is_vs_market = " vs " in q
            if not is_vs_market:
                continue
            
            # BLACKLIST: Skip handicap, spread, map-related, over/under markets
            if any(x in q_lower for x in ["handicap", "spread", "map ", "maps", "over", "under", "total", "rounds"]):
                continue
            # Skip individual game/round markets
            if is_individual_game_market(q):
                continue
            
            return m
        
        return None
    
    async def debug_edge(
        self,
        odds_match: AggregatedMatch,
        market: dict,
        poly_client,
        active_tokens: set = None,
        filled_tokens: set = None,
        monitoring_mode: bool = False,
    ):
        """Debug why no edge was found. Uses cache to skip unchanged markets."""
        active_tokens = active_tokens or set()
        filled_tokens = filled_tokens or set()
        outcomes = market.get("outcomes", [])
        token_ids = market.get("clobTokenIds", [])
        
        if len(outcomes) < 2 or len(token_ids) < 2:
            return
        
        # Try to use cached bids first
        cached_bids = self.cache.get_cached_bids_batch(token_ids, max_age_seconds=10.0)  # Tighter for live
        
        if all(cached_bids.get(tid) is not None for tid in token_ids):
            best_bids = {tid: {"price": cached_bids[tid], "size": 0} for tid in token_ids}
        else:
            best_bids = await poly_client.get_best_bids(token_ids)
        
        # Skip Yes/No markets
        skip_outcomes = ("yes", "no", "over", "under")
        if outcomes[0].lower() in skip_outcomes or outcomes[1].lower() in skip_outcomes:
            return
        
        aligned = align_teams_with_bids(odds_match, outcomes, token_ids, best_bids)
        if not aligned or len(aligned) < 2:
            return
        
        # Show both teams in one row
        t1_name, t1_bid, t1_token, t1_fair, _ = aligned[0]
        t2_name, t2_bid, t2_token, t2_fair, _ = aligned[1]
        
        # CHECK CACHE: Skip if both tokens' state unchanged
        t1_changed = await self.cache.update_order_book(t1_token, t1_bid, 0, 0, 0)
        t2_changed = await self.cache.update_order_book(t2_token, t2_bid, 0, 0, 0)
        
        # Also check if fair values changed
        match_key = f"{odds_match.team1}:{odds_match.team2}"
        fair1_changed = await self.cache.update_fair_value(match_key, t1_name, t1_fair)
        fair2_changed = await self.cache.update_fair_value(match_key, t2_name, t2_fair)
        
        # Skip printing if nothing changed (unless first scan)
        if not self._first_scan and not (t1_changed or t2_changed or fair1_changed or fair2_changed):
            return
        
        # Skip printing for markets where we already have active orders (unless monitoring)
        if not monitoring_mode and t1_token in active_tokens and t2_token in active_tokens:
            return
        
        t1_entry = t1_bid + 0.01 if t1_bid > 0 else 0
        t2_entry = t2_bid + 0.01 if t2_bid > 0 else 0
        t1_edge = (t1_fair - t1_entry) * 100 if t1_bid > 0 else 0
        t2_edge = (t2_fair - t2_entry) * 100 if t2_bid > 0 else 0
        
        # Build status indicators
        def get_status(token_id):
            has_position = token_id in filled_tokens
            has_open_order = token_id in active_tokens and token_id not in filled_tokens
            if has_position:
                return "✅"
            elif has_open_order:
                return "⏳"
            return ""
        
        t1_active = get_status(t1_token)
        t2_active = get_status(t2_token)
        
        t1_short = t1_name[:12]
        t2_short = t2_name[:12]
        
        # Check if both sides have edge
        min_edge = self.config.get("min_edge", 0.08)
        both_have_edge = t1_edge >= min_edge * 100 and t2_edge >= min_edge * 100
        
        # CRITICAL: Push live fair probs to BotState so adjustments use correct values
        # This ensures OrderMonitor and ReactiveHandler use live fair, not stale pre-match
        from src.state.bot_state import get_bot_state
        bot_state = get_bot_state()
        bot_state.update_fair_probs_by_team(
            match_id=odds_match.match_id,
            team1=t1_name,
            team2=t2_name,
            fair_prob1=t1_fair,
            fair_prob2=t2_fair,
        )
        
        if monitoring_mode:
            print(f"   👀 [LIVE] {t1_short}{t1_active} vs {t2_short}{t2_active} | Fair: {t1_fair:.0%}/{t2_fair:.0%} | Bid: {t1_bid:.3f}/{t2_bid:.3f} | Edge: {t1_edge:+.1f}%/{t2_edge:+.1f}%")
        elif both_have_edge:
            print(f"   🔴 [LIVE] {t1_short} vs {t2_short} | Fair: {t1_fair:.0%}/{t2_fair:.0%} | Bid: {t1_bid:.3f}/{t2_bid:.3f} | Edge: {t1_edge:+.1f}%/{t2_edge:+.1f}% | BOTH SIDES!")
        else:
            print(f"      🔴 {t1_short}{t1_active} vs {t2_short}{t2_active} | Fair: {t1_fair:.0%}/{t2_fair:.0%} | Bid: {t1_bid:.3f}/{t2_bid:.3f} | Edge: {t1_edge:+.1f}%/{t2_edge:+.1f}%")

    async def check_opportunity(
        self, 
        odds_match: AggregatedMatch,
        market: dict,
        poly_client,
        skip_tokens: set = None,
    ) -> Optional[dict]:
        """
        Check if there's a trading opportunity using order book.
        
        Same logic as OpportunityScanner but uses config['min_edge'] (8c for live).
        """
        skip_tokens = skip_tokens or set()
        token_ids = market.get("clobTokenIds", [])
        outcomes = market.get("outcomes", [])
        
        if len(token_ids) < 2 or len(outcomes) < 2:
            return None
        
        # Skip Yes/No markets
        skip_outcomes = ("yes", "no", "over", "under")
        if outcomes[0].lower() in skip_outcomes or outcomes[1].lower() in skip_outcomes:
            return None
        
        # Fetch order books for both outcomes
        best_bids = await poly_client.get_best_bids(token_ids)
        
        # Align team order between bookmaker and Polymarket
        aligned = align_teams_with_bids(odds_match, outcomes, token_ids, best_bids)
        if not aligned:
            return None
        
        # CHECK SPREAD: Get bids on both sides
        # Spread = 1 - (bid1 + bid2) - i.e., the gap in the middle
        bid1 = aligned[0][1] if len(aligned) > 0 else 0
        bid2 = aligned[1][1] if len(aligned) > 1 else 0
        
        if bid1 > 0 and bid2 > 0:
            spread = 1.0 - (bid1 + bid2)
            min_spread = self.config.get("min_spread", 0.08)  # 8c minimum spread
            
            if spread < min_spread:
                # Market is too tight - not worth trading
                return None
        
        for poly_team, best_bid, poly_token, fair, other_fair in aligned:
            # Skip tokens we already have active orders on
            if poly_token in skip_tokens:
                continue
            if best_bid <= 0:
                continue
            
            # Our entry price = best_bid + 0.01 (queue jump)
            entry_price = best_bid + 0.01
            
            # Edge = fair value - our entry price
            edge = fair - entry_price
            
            # NOTE: We always enter at best_bid + 1c regardless of edge size.
            # If we get outbid, we adjust upward until min_edge is reached.
            
            # Use config min_edge (8c for live)
            min_edge = self.config.get("min_edge", 0.08)
            if edge >= min_edge:
                # Edge is sufficient - take the opportunity now
                hedge_idx = 1 if poly_team == outcomes[0] else 0
                
                # Calculate expected profit if we hedge at fair value
                calc = self.hedge_finder.calculate_hedge(entry_price, 5, other_fair)
                expected_profit = calc.profit_percent if calc.can_hedge else 0
                
                return {
                    "team": poly_team,
                    "token_id": poly_token,
                    "fair": fair,
                    "best_bid": best_bid,
                    "entry_price": entry_price,
                    "edge": edge,
                    "hedge_team": outcomes[hedge_idx],
                    "hedge_token": token_ids[hedge_idx],
                    "hedge_fair": other_fair,
                    "expected_profit": expected_profit,
                }
        
        return None
