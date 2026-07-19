"""
Opportunity scanning - finds arbitrage opportunities between bookmakers and Polymarket.
"""
from typing import Optional, List

import asyncio
import logging

from src.services.odds_service import OddsService, AggregatedMatch
from src.polymarket.market_client import PolymarketEsportsClient, PolymarketEsportsEvent
from src.state.market_cache import MarketCache
from src.execution.hedge_finder import HedgeFinder
from src.scanning.team_matcher import align_teams_with_bids, align_yesno_h2h
from src.core.match_id import is_individual_game_market, normalize_team
from src.scanning.sidecar_hydrate import HydrationError


logger = logging.getLogger(__name__)


class OpportunityScanner:
    """
    Scans for trading opportunities by comparing bookmaker odds with Polymarket prices.
    
    Requires injection of:
    - odds_service: OddsService instance
    - cache: MarketCache instance  
    - hedge_finder: HedgeFinder instance
    - config: CONFIG dict
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
        self._warned_zero_bid_tokens: set = set()  # Tokens with no visible bid (silent-continue surfacing)
        
        # Force printing on first scan
        self._first_scan: bool = True
        
        # Diagnostics: Track scan counts and state sizes
        self._scan_count: int = 0
        self._last_odds_count: int = 0
        self._last_poly_count: int = 0
        self._last_blocking_count: int = 0
    
    async def scan_for_opportunities(
        self,
        watcher,  # OrderWatcher - avoid circular import
        cancel_live_orders_fn,  # Callback to cancel orders on live events
        live_match_ids: set[str] = None,  # Match IDs from esports_odds_live (ground truth)
    ) -> list[dict]:
        """
        Find ALL trading opportunities in a single scan.
        
        Args:
            watcher: OrderWatcher instance
            cancel_live_orders_fn: Callback to cancel orders on Polymarket live events
            live_match_ids: Match IDs from esports_odds_live - if match is in this set,
                           skip scanning entirely (ground truth live detection)
        
        Returns list of opportunity dicts (may be empty).
        """
        # Get fresh odds from bookmakers
        matches = self.odds_service.get_matches(
            min_sources=self.config["min_sources"],
            fresh_only=True,
        )
        
        if self._first_scan:
            print(f"📊 First scan: {len(matches) if matches else 0} matches from odds service")
        
        if not matches:
            all_matches = self.odds_service.get_matches(min_sources=1, fresh_only=True)
            if all_matches:
                print(f"⚠️ {len(all_matches)} matches found but need min_sources={self.config['min_sources']}")
            elif self._first_scan:
                print("⚠️ No odds data available yet - is the scraper running?")
            return []
        
        opportunities = []  # Collect ALL opportunities
        found_tokens = set()  # Track tokens found in this scan to prevent duplicates
        matched_count = 0
        try:
            async with PolymarketEsportsClient() as poly_client:
                # Fetch esports events
                poly_events = await poly_client.get_all_esports_events()
                
                upcoming = [e for e in poly_events if not e.is_finished]
                
                if self._first_scan:
                    print(f"📊 Found {len(upcoming)} upcoming Polymarket events")
                
                # Check for live events and cancel any orders on them
                live_events = [e for e in poly_events if e.is_live]
                await cancel_live_orders_fn(live_events, poly_client)
                
                matched_count = 0
                live_match_ids = live_match_ids or set()
                
                for odds_match in matches:
                    # CRITICAL: Skip matches in esports_odds_live (ground truth live detection)
                    # This is more reliable than Polymarket's is_live flag
                    if odds_match.match_id in live_match_ids:
                        if self._first_scan:
                            print(f"   ⏭️ Skipping live match (esports_odds_live): {odds_match.match_id}")
                        continue
                    
                    # NOTE: We no longer skip matches where we have orders.
                    # The skip_tokens logic in check_opportunity handles preventing
                    # duplicate orders on the same token. We WANT to place orders
                    # on both sides of a match when both have edge.
                    #
                    # Only skip if we have an UNHEDGED POSITION (not just an order).
                    # This prevents piling on when we're already overexposed.
                    has_unhedged_position = watcher.bot_state.has_unhedged_position_for_match(odds_match.match_id)
                    
                    if has_unhedged_position:
                        # Still show match in monitoring mode for visibility
                        poly_event = find_poly_event(odds_match, upcoming)
                        if poly_event and poly_event.markets:
                            market = next((m for m in poly_event.markets 
                                         if not any(x in m.get("question", "").lower() 
                                                   for x in ["handicap", "spread", "map ", "maps", "over", "under", "total", "rounds"])
                                         and not is_individual_game_market(m.get("question", ""))), None)
                            if market and market.get("outcomePrices"):
                                await self.debug_edge(
                                    odds_match, market, poly_client,
                                    watcher.bot_state.get_active_token_ids(),
                                    watcher.bot_state.get_filled_token_ids(),
                                    monitoring_mode=True
                                )
                        continue  # Skip to next match
                    
                    # Find matching Polymarket event
                    poly_event = find_poly_event(odds_match, upcoming)
                    if not poly_event:
                        # Debug: Log failed rugby matches on first scan
                        # if self._first_scan and is_rugby:
                        #     print(f"   ⚠️ Rugby no match: {odds_match.team1} vs {odds_match.team2} ({odds_match.game})")
                        continue
                    
                    matched_count += 1
                    
                    # CRITICAL: Skip LIVE events - odds are stale, don't bet live
                    if poly_event.is_live:
                        continue

                    # Hydrate event to get market data (prices, token IDs)
                    if not poly_event.markets:
                        poly_event = await poly_client.hydrate_event(poly_event)

                    # Double-check liveness after hydration.
                    # Ground-truth source (esports_odds_live) is re-consulted
                    # here because it can flip between the pre-hydration check
                    # (line ~108) and now — the hydration await is the race
                    # window.
                    if poly_event.is_live:
                        continue
                    if odds_match.match_id in live_match_ids:
                        continue
                    
                    # Skip if still no markets after hydration
                    if not poly_event.markets:
                        if poly_event.title not in self._warned_no_markets:
                            print(f"   ⚠️ No markets for: {poly_event.title}")
                            self._warned_no_markets.add(poly_event.title)
                        continue
                    
                    # ============================================================
                    # Find the WINNER market (moneyline), skip handicap/spread/totals
                    # ============================================================
                    market = None
                    for m in poly_event.markets:
                        q = m.get("question", "")
                        q_lower = q.lower()
                        
                        # WHITELIST: Moneyline markets MUST contain " vs " (Team A vs Team B)
                        # This filters out prop bets like "Series: Most kills?"
                        is_vs_market = " vs " in q
                        if not is_vs_market:
                            continue
                        
                        # BLACKLIST: Skip handicap, spread, map-related, and over/under markets
                        if any(x in q_lower for x in ["handicap", "spread", "map ", "maps", "over", "under", "total", "rounds"]):
                            continue
                        # Skip individual game/round markets (e.g., "Game 1 Winner")
                        # We don't have odds for these
                        if is_individual_game_market(q):
                            continue
                        # This should be a winner/moneyline market
                        market = m
                        break
                    
                    if not market:
                        continue
                    
                    # Skip if no prices (draft markets without liquidity)
                    if not market.get("outcomePrices"):
                        continue
                    
                    # Check for opportunity (skip_tokens prevents duplicate orders)
                    # CRITICAL: Build skip_tokens that includes:
                    # 1. Tokens where we shouldn't place new orders (open orders, unhedged positions)
                    # 2. Tokens we've already found opportunities for in THIS scan
                    # NOTE: Uses get_tokens_blocking_new_orders which EXCLUDES completed arb tokens,
                    # allowing new orders with edge on completed arbs!
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
                            continue  # Skip silently - already found
                        
                        print(f"   ✨ OPPORTUNITY FOUND: {odds_match.match_id}")
                        # Track this token to prevent duplicates within same scan
                        if token_id:
                            found_tokens.add(token_id)
                        
                        # Collect opportunity with context for execution
                        opportunities.append({
                            **opportunity,
                            "odds_match": odds_match,
                            "poly_event": poly_event,
                            "market": market,  # Pass the selected moneyline market
                            # Final-check payload: executor will re-verify that
                            # neither flag has flipped since the scan window.
                            "scan_live_match_ids": live_match_ids,
                        })
                    else:
                        # Debug: show why no edge
                        await self.debug_edge(
                            odds_match, market, poly_client,
                            watcher.bot_state.get_active_token_ids(),
                            watcher.bot_state.get_filled_token_ids(),
                            monitoring_mode=False
                        )

                
        except asyncio.CancelledError:
            raise
        except (HydrationError, KeyError, ValueError, TypeError) as e:
            current_match_id = getattr(odds_match, "match_id", "<unknown>") if "odds_match" in locals() else "<pre-loop>"
            logger.exception(
                "Scan data error (match_id=%s): %s: %s",
                current_match_id, type(e).__name__, e,
            )
            print(f"❌ Scan data error on {current_match_id}: {type(e).__name__}: {e}")
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.exception("Scan IO error: %s: %s", type(e).__name__, e)
            print(f"❌ Scan IO error: {type(e).__name__}: {e}")
        except Exception as e:
            current_match_id = getattr(odds_match, "match_id", "<unknown>") if "odds_match" in locals() else "<pre-loop>"
            logger.exception(
                "Unexpected scan error (match_id=%s): %s: %s",
                current_match_id, type(e).__name__, e,
            )
            print(f"❌ Unexpected scan error on {current_match_id}: {type(e).__name__}: {e}")

        # Increment scan counter
        self._scan_count += 1
        
        # Get current state sizes for tracking
        current_matches = len(matches) if matches else 0
        blocking_tokens = watcher.bot_state.get_tokens_blocking_new_orders()
        current_blocking = len(blocking_tokens)
        warned_count = len(self._warned_no_markets)
        
        # Check for significant changes that might indicate issues
        new_odds = current_matches - self._last_odds_count if self._last_odds_count > 0 else 0
        new_blocking = current_blocking - self._last_blocking_count if self._last_blocking_count > 0 else 0
        
        # Print summary on first scan
        if self._first_scan:
            print(f"📊 First scan complete: {matched_count} matches with Polymarket events")
            print(f"   📈 State: {current_blocking} blocking tokens, {warned_count} warned markets")
        
        # Periodic stats every 10 scans (approx every 5 min with 30s interval)
        elif self._scan_count % 10 == 0:
            print(f"📊 Scan #{self._scan_count}: {current_matches} odds, {matched_count} PM matches, {len(opportunities)} opportunities")
            print(f"   📈 State: {current_blocking} blocking (+{new_blocking}), {warned_count} warned (cache)")
            
            # Cleanup stale matches to prevent blocking token accumulation
            watcher.bot_state.cleanup_stale_matches()
            
            # Alert if state is growing unexpectedly
            if new_blocking > 20:
                print(f"   ⚠️ ALERT: Blocking tokens grew by {new_blocking} - possible state leak!")
            if warned_count > 500:
                print(f"   ⚠️ ALERT: Warning cache has {warned_count} entries - may need clearing")
        
        # Update tracking for next scan
        self._last_odds_count = current_matches
        self._last_blocking_count = current_blocking
        
        # Clear first scan flag after completing a scan
        self._first_scan = False
        
        if opportunities:
            print(f"   🎯 Found {len(opportunities)} opportunities to execute in parallel")
        
        return opportunities
    
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
        
        # Try to use cached bids first (from WebSocket updates). Tightened
        # from 30s to 10s because debug_edge diagnostics inform operator
        # decisions about why edge disappeared — a 29-second-old cached bid
        # can mislead triage. 10s is still well within WS update cadence
        # on an active market; quieter markets will fall through to the
        # fresh fetch, which is fine for a diagnostic path.
        cached_bids = self.cache.get_cached_bids_batch(token_ids, max_age_seconds=10.0)

        # If all bids are cached and fresh, use them; otherwise fetch from API
        if all(cached_bids.get(tid) is not None for tid in token_ids):
            best_bids = {tid: {"price": cached_bids[tid], "size": 0} for tid in token_ids}
        else:
            best_bids = await poly_client.get_best_bids(token_ids)

        # Skip Yes/No and Over/Under markets silently - can't align team names to these outcomes
        skip_outcomes = ("yes", "no", "over", "under")
        if outcomes[0].lower() in skip_outcomes or outcomes[1].lower() in skip_outcomes:
            return

        aligned = align_teams_with_bids(odds_match, outcomes, token_ids, best_bids)
        if not aligned or len(aligned) < 2:
            pair_key = f"{odds_match.team1}:{odds_match.team2}:{outcomes[0]}:{outcomes[1]}"
            if pair_key not in self._warned_no_markets:
                print(f"      ⚠️ Could not align: {odds_match.team1}/{odds_match.team2} ↔ {outcomes[0]}/{outcomes[1]}")
                self._warned_no_markets.add(pair_key)
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
        # In monitoring mode, we WANT to see matches we're already in
        if not monitoring_mode and t1_token in active_tokens and t2_token in active_tokens:
            return
        
        t1_entry = t1_bid + 0.01 if t1_bid > 0 else 0
        t2_entry = t2_bid + 0.01 if t2_bid > 0 else 0
        t1_edge = (t1_fair - t1_entry) * 100 if t1_bid > 0 else 0
        t2_edge = (t2_fair - t2_entry) * 100 if t2_bid > 0 else 0
        
        # Build status indicators: ✅ = filled position, ⏳ = open order
        # active_tokens contains both open orders AND filled positions
        # filled_tokens only contains tokens with filled shares
        # So: open order only = in active but not in filled
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
        
        # Check if both sides have edge (potential arbitrage)
        min_edge = self.config.get("min_edge", 0.08)
        both_have_edge = t1_edge >= min_edge * 100 and t2_edge >= min_edge * 100
        
        # Calculate combined probability and potential arb
        combined_prob = t1_fair + t2_fair
        combined_entry = t1_entry + t2_entry if t1_bid > 0 and t2_bid > 0 else 0
        arb_margin = 1.0 - combined_entry if combined_entry > 0 else 0
        
        if monitoring_mode:
            # Show matches we're already in (monitoring both sides)
            print(f"   👀 {t1_short}{t1_active} vs {t2_short}{t2_active} | Fair: {t1_fair:.0%}/{t2_fair:.0%} | Bid: {t1_bid:.3f}/{t2_bid:.3f} | Edge: {t1_edge:+.1f}%/{t2_edge:+.1f}%")
        elif both_have_edge:
            # Highlight two-sided opportunities
            print(f"   🎯 {t1_short} vs {t2_short} | Fair: {t1_fair:.0%}/{t2_fair:.0%} | Bid: {t1_bid:.3f}/{t2_bid:.3f} | Edge: {t1_edge:+.1f}%/{t2_edge:+.1f}% | BOTH SIDES! Combined: {combined_entry:.2f} → margin: {arb_margin:.1%}")
        else:
            print(f"      {t1_short}{t1_active} vs {t2_short}{t2_active} | Fair: {t1_fair:.0%}/{t2_fair:.0%} | Bid: {t1_bid:.3f}/{t2_bid:.3f} | Edge: {t1_edge:+.1f}%/{t2_edge:+.1f}%")

    async def check_opportunity(
        self, 
        odds_match: AggregatedMatch,
        market: dict,
        poly_client,
        skip_tokens: set = None,
    ) -> Optional[dict]:
        """
        Check if there's a trading opportunity using order book.
        
        Strategy:
        1. Get best bid for each outcome from order book
        2. Our entry = best_bid + $0.01 (to get queue priority)
        3. Check if fair_value - entry >= min_edge
        """
        skip_tokens = skip_tokens or set()
        token_ids = market.get("clobTokenIds", [])
        outcomes = market.get("outcomes", [])
        
        if len(token_ids) < 2 or len(outcomes) < 2:
            return None
        
        # Skip Yes/No and Over/Under markets - can't align team names to these outcomes
        # Note: Synthesized rugby markets have team names as outcomes, not Yes/No
        skip_outcomes = ("yes", "no", "over", "under")
        if outcomes[0].lower() in skip_outcomes or outcomes[1].lower() in skip_outcomes:
            return None
        
        # Fetch order books for both outcomes
        best_bids = await poly_client.get_best_bids(token_ids)
        
        # Align team order between bookmaker and Polymarket
        aligned = align_teams_with_bids(odds_match, outcomes, token_ids, best_bids)
        if not aligned:
            return None
        
        for poly_team, best_bid, poly_token, fair, other_fair in aligned:
            # Skip tokens we already have active orders on
            if poly_token in skip_tokens:
                continue
            if best_bid <= 0:
                if poly_token not in self._warned_zero_bid_tokens:
                    logger.info(
                        "no visible bid for token %s... (%s) — skipping scan entry",
                        poly_token[:16], poly_team,
                    )
                    self._warned_zero_bid_tokens.add(poly_token)
                continue

            # Our entry price = best_bid + 0.01 (queue jump)
            entry_price = best_bid + 0.01
            
            # Edge = fair value - our entry price
            edge = fair - entry_price
            

            
            if edge >= self.config["min_edge"]:
                # Edge is sufficient - take the opportunity now, hedge later
                hedge_idx = 1 if poly_team == outcomes[0] else 0
                
                # Calculate expected profit if we hedge at fair value (for logging only)
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
    # ===== Multi-Market Scanning =====
    
    async def scan_multi_market_opportunities(
        self,
        watcher,
        poly_client,  # PolymarketEsportsClient
        poly_events: list,  # Pre-fetched PolymarketEsportsEvent list
    ) -> list[dict]:
        """Scan for spread/totals opportunities using the-odds-api data.
        
        Args:
            watcher: OrderWatcher instance
            poly_client: PolymarketEsportsClient for order book queries
            poly_events: Pre-fetched Polymarket events (from sports series IDs)
            
        Returns:
            List of opportunity dicts (same format as check_opportunity)
        """
        from src.core.market_parser import match_odds_to_poly_market
        import logging
        logger = logging.getLogger(__name__)
        
        # Get multi-market odds (h2h + spreads + totals + btts + h2h_h1 + 1H markets) from v2 cache
        multi_matches = self.odds_service.get_multi_market_matches(
            market_types=["h2h", "spreads", "totals", "btts", "h2h_h1", "spreads_1h", "totals_1h", "map_winner"],
            fresh_only=True,
        )
        
        if not multi_matches:
            return []
        
        if self._first_scan:
            print(f"📊 Multi-market scan: {len(multi_matches)} spread/totals odds, {len(poly_events)} Poly events")
        
        upcoming = [e for e in poly_events if not e.is_finished]
        opportunities = []
        matched_count = 0
        
        # Build skip tokens from bot_state (includes reserved, collision, open orders, positions)
        # NOTE: We use TOKEN-level skipping, not match-level. This allows the bot to
        # trade O/U 3.5 on a match even if it already has a BAR -1.5 spread order.
        # Each market (moneyline, spread ±1.5, O/U 2.5, etc.) has its own token IDs,
        # so token-level skip naturally prevents duplicate orders on the same sub-market.
        skip_tokens = set()
        bot_state_ref = None
        if hasattr(watcher, 'bot_state'):
            bot_state_ref = watcher.bot_state
            skip_tokens = bot_state_ref.get_tokens_blocking_new_orders()
        elif hasattr(watcher, 'get_all_positions'):
            for pos in watcher.get_all_positions():
                if pos.entry_token_id:
                    skip_tokens.add(pos.entry_token_id)
                if pos.hedge_token_id:
                    skip_tokens.add(pos.hedge_token_id)
        
        MAX_OPPORTUNITIES = 10  # Cap to avoid excessive hydration/API calls
        
        for odds_match in multi_matches:
            # Stop collecting once we have enough
            if len(opportunities) >= MAX_OPPORTUNITIES:
                break
            
            # Find ALL corresponding Polymarket events (main + "More Markets")
            matching_events = find_all_poly_events(odds_match, upcoming)
            if not matching_events:
                continue
            
            matched_count += 1
            
            sport = odds_match.game  # "football", "hockey", etc.
            found_opp = False
            
            for poly_event in matching_events:
                # Skip live events
                if poly_event.is_live:
                    continue
                
                # Hydrate event to get market data
                if not poly_event.markets:
                    poly_event = await poly_client.hydrate_event(poly_event)
                
                if not poly_event.markets:
                    continue
                
                # For each market in the Polymarket event, check match
                for market in poly_event.markets:
                    question = market.get("question", "")
                    
                    if not match_odds_to_poly_market(
                        odds_match.market_type,
                        odds_match.line,
                        question,
                        sport,
                    ):
                        continue
                    
                    # SPREAD DIRECTION CHECK: Use team1_spread_point to determine
                    # which team is giving vs receiving the spread.
                    # Polymarket "Spread: TeamA (-1.5)" means TeamA gives the spread.
                    # We only match if our odds data has that team on the NEGATIVE side.
                    if odds_match.market_type in ("spreads", "spreads_1h") and question.lower().startswith(("spread:", "1h spread:")):
                        import re
                        spread_team_match = re.match(r'(?:1H\s+)?Spread:\s*(.+?)\s*\(', question, re.IGNORECASE)
                        if spread_team_match:
                            spread_team_norm = normalize_team(spread_team_match.group(1))
                            odds_t1_norm = normalize_team(odds_match.team1)
                            odds_t2_norm = normalize_team(odds_match.team2)
                            
                            t1sp = odds_match.team1_spread_point
                            if t1sp is not None:
                                # We know team1's signed spread point
                                # team1 has point t1sp, team2 has opposite sign
                                t1_is_giving = (t1sp < 0)  # team1 is giving the spread (-X)
                                
                                # Check: does the Polymarket spread team match the team GIVING the spread?
                                spread_matches_t1 = (
                                    spread_team_norm == odds_t1_norm or
                                    spread_team_norm in odds_t1_norm or odds_t1_norm in spread_team_norm
                                )
                                spread_matches_t2 = (
                                    spread_team_norm == odds_t2_norm or
                                    spread_team_norm in odds_t2_norm or odds_t2_norm in spread_team_norm
                                )
                                
                                if spread_matches_t1 and not t1_is_giving:
                                    # Polymarket asks about team1 giving spread, but team1 is RECEIVING
                                    continue  # Wrong direction
                                elif spread_matches_t2 and t1_is_giving:
                                    # Polymarket asks about team2 giving spread, but team2 is RECEIVING
                                    continue  # Wrong direction
                                elif not spread_matches_t1 and not spread_matches_t2:
                                    continue  # No team match at all
                            else:
                                # No signed point info — fall back to old check
                                if spread_team_norm != odds_t1_norm and not (
                                    spread_team_norm in odds_t1_norm or odds_t1_norm in spread_team_norm
                                ):
                                    continue
                    
                    # UNHEDGED POSITION GUARD: Prevent piling on when we already
                    # have an unhedged position on this market.
                    # This was MISSING (unlike moneyline scan which has it at line 118).
                    # Without this guard, the scanner kept placing new entries after fills,
                    # leading to 6100 shares of one-sided exposure.
                    condition_id = market.get("condition_id", "")
                    if bot_state_ref and odds_match.market_type in ("spreads", "totals", "spreads_1h", "totals_1h", "map_winner") and condition_id:
                        effective_mid = f"{odds_match.match_id}:{condition_id[:18]}"
                        if bot_state_ref.has_unhedged_position_for_match(effective_mid):
                            continue
                    elif bot_state_ref:
                        if bot_state_ref.has_unhedged_position_for_match(odds_match.match_id):
                            continue
                    
                    # Found a match! Check for trading opportunity (both sides)
                    #
                    # CROSS-EVENT COLLISION (proactive):
                    # Before checking for opportunities, collect all tokens from
                    # this market. If ANY token for this market type+line is
                    # already in skip_tokens (from a previous placement), then
                    # register ALL tokens from other events as collision tokens.
                    # This handles the edge case where Event A was placed in
                    # cycle 1 but Event B wasn't hydrated until this cycle.
                    market_tokens = market.get("clobTokenIds", [])
                    any_token_blocked = any(t in skip_tokens for t in market_tokens)
                    if any_token_blocked and bot_state_ref and len(matching_events) > 1:
                        for alt_token in market_tokens:
                            if alt_token not in skip_tokens:
                                skip_tokens.add(alt_token)
                                bot_state_ref._collision_tokens.add(alt_token)
                        # Also block tokens from OTHER events' matching markets
                        for other_event in matching_events:
                            if other_event is poly_event:
                                continue
                            for other_market in (other_event.markets or []):
                                other_q = other_market.get("question", "")
                                if match_odds_to_poly_market(
                                    odds_match.market_type,
                                    odds_match.line,
                                    other_q,
                                    sport,
                                ):
                                    for alt_token in other_market.get("clobTokenIds", []):
                                        if alt_token not in skip_tokens:
                                            skip_tokens.add(alt_token)
                                            bot_state_ref._collision_tokens.add(alt_token)
                    
                    opps = await self.check_multi_market_opportunity(
                        odds_match, market, poly_client, skip_tokens=skip_tokens,
                    )
                    for opp in opps:
                        opp["poly_event"] = poly_event
                        opp["odds_match"] = odds_match
                        opp["market"] = market
                        opportunities.append(opp)
                        found_opp = True
                        line_str = f" ({odds_match.line})" if odds_match.line else ""
                        print(f"   ✨ SPORTS OPP: {opp['team']} | {odds_match.game} {odds_match.market_type}{line_str} | {odds_match.team1} vs {odds_match.team2} | Edge: {opp['edge']*100:.1f}% | Fair: {opp['fair']*100:.0f}% | Bid: {opp['best_bid']:.2f}")
                        
                        # CROSS-EVENT COLLISION (reactive):
                        # When an opportunity IS generated, register tokens from
                        # OTHER events for the same market as collision tokens.
                        # This ensures future scan cycles block those alternates.
                        accepted_token = opp.get("token_id")
                        if accepted_token and bot_state_ref:
                            for other_event in matching_events:
                                if other_event is poly_event:
                                    continue
                                for other_market in (other_event.markets or []):
                                    other_q = other_market.get("question", "")
                                    if match_odds_to_poly_market(
                                        odds_match.market_type,
                                        odds_match.line,
                                        other_q,
                                        sport,
                                    ):
                                        for alt_token in other_market.get("clobTokenIds", []):
                                            if alt_token != accepted_token:
                                                skip_tokens.add(alt_token)
                                                bot_state_ref._collision_tokens.add(alt_token)


        
        if self._first_scan and matched_count > 0:
            print(f"   🔗 Matched {matched_count} multi-market odds to Polymarket events")
        
        return opportunities
    
    async def check_multi_market_opportunity(
        self,
        odds_match: AggregatedMatch,
        market: dict,
        poly_client,
        skip_tokens: set = None,
    ) -> List[dict]:
        """Check for edge on non-moneyline markets (spreads, totals).
        
        Returns ALL sides with edge (e.g., both Over AND Under can have
        opportunities simultaneously). Unlike check_opportunity(), this handles:
        - Totals: Over/Under outcome alignment (fair_prob1=Over, fair_prob2=Under)
        - Spreads: Team name alignment (same as h2h, uses align_teams_with_bids)
        """
        skip_tokens = skip_tokens or set()
        token_ids = market.get("clobTokenIds", [])
        outcomes = market.get("outcomes", [])
        results = []
        
        if len(token_ids) < 2 or len(outcomes) < 2:
            return results
        
        # Fetch order books for both outcomes
        best_bids = await poly_client.get_best_bids(token_ids)
        
        if odds_match.market_type in ("totals", "totals_1h"):
            # Totals: Outcomes are "Over" and "Under"
            # odds_match.fair_prob1 = Over probability, fair_prob2 = Under probability
            over_idx = None
            under_idx = None
            for i, outcome in enumerate(outcomes):
                if outcome.lower() == "over":
                    over_idx = i
                elif outcome.lower() == "under":
                    under_idx = i
            
            if over_idx is None or under_idx is None:
                return results
            
            # Check both sides for edge
            sides = [
                (over_idx, under_idx, odds_match.fair_prob1 / 100.0, odds_match.fair_prob2 / 100.0),
                (under_idx, over_idx, odds_match.fair_prob2 / 100.0, odds_match.fair_prob1 / 100.0),
            ]
            
            for entry_idx, hedge_idx, fair, other_fair in sides:
                token = token_ids[entry_idx]
                if token in skip_tokens:
                    continue
                
                bid_data = best_bids.get(token, {})
                best_bid = bid_data.get("price", 0) if isinstance(bid_data, dict) else bid_data
                if best_bid <= 0:
                    if token not in self._warned_zero_bid_tokens:
                        logger.info(
                            "no visible bid for totals token %s... (%s line=%s) — skipping scan entry",
                            token[:16], outcomes[entry_idx], getattr(odds_match, "line", "?"),
                        )
                        self._warned_zero_bid_tokens.add(token)
                    continue

                entry_price = best_bid + 0.01
                edge = fair - entry_price

                if edge >= self.config["min_edge"]:
                    calc = self.hedge_finder.calculate_hedge(entry_price, 5, other_fair)
                    expected_profit = calc.profit_percent if calc.can_hedge else 0

                    results.append({
                        "team": outcomes[entry_idx],
                        "token_id": token,
                        "fair": fair,
                        "best_bid": best_bid,
                        "entry_price": entry_price,
                        "edge": edge,
                        "hedge_team": outcomes[hedge_idx],
                        "hedge_token": token_ids[hedge_idx],
                        "hedge_fair": other_fair,
                        "expected_profit": expected_profit,
                        "market_type": odds_match.market_type,
                        "line": odds_match.line,
                    })
        
        elif odds_match.market_type in ("spreads", "spreads_1h", "h2h", "h2h_h1", "map_winner"):
            # Check for Yes/No outcomes (football/hockey moneyline, draw markets)
            o0_lower = outcomes[0].lower()
            o1_lower = outcomes[1].lower()
            is_yesno = o0_lower in ("yes", "no") or o1_lower in ("yes", "no")

            if is_yesno and odds_match.market_type in ("h2h", "h2h_h1"):
                # Yes/No moneyline — use binary-adjusted fair values
                aligned = align_yesno_h2h(odds_match, market, token_ids, best_bids)
            else:
                # Team-name outcomes (spreads, h2h, per-map winner) — align by team
                # name; map winner is a plain 2-way team market like h2h.
                aligned = align_teams_with_bids(odds_match, outcomes, token_ids, best_bids)
            
            if not aligned:
                return results
            
            for poly_team, bid_data, poly_token, fair, other_fair in aligned:
                if poly_token in skip_tokens:
                    continue
                best_bid = bid_data.get("price", 0) if isinstance(bid_data, dict) else bid_data
                if best_bid <= 0:
                    if poly_token not in self._warned_zero_bid_tokens:
                        logger.info(
                            "no visible bid for spreads/h2h token %s... (%s market_type=%s) — skipping scan entry",
                            poly_token[:16], poly_team, getattr(odds_match, "market_type", "?"),
                        )
                        self._warned_zero_bid_tokens.add(poly_token)
                    continue

                entry_price = best_bid + 0.01
                edge = fair - entry_price

                if edge >= self.config["min_edge"]:
                    hedge_idx = 1 if poly_team == outcomes[0] else 0
                    calc = self.hedge_finder.calculate_hedge(entry_price, 5, other_fair)
                    expected_profit = calc.profit_percent if calc.can_hedge else 0

                    results.append({
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
                        "market_type": odds_match.market_type,
                        "line": odds_match.line,
                    })
        
        return results


def find_poly_event(
    odds_match: AggregatedMatch,
    poly_events: list,
) -> Optional[PolymarketEsportsEvent]:
    """Find matching Polymarket event for an odds match.
    
    CRITICAL: Matches on BOTH team names AND game type to prevent
    cross-game token collisions (e.g., CS2 Team Liquid vs Dota2 Team Liquid).
    
    Matching strategy (in priority order):
    1. Exact normalized team names (works for esports + sports with aliases)
    2. Containment matching for sports (Polymarket nickname inside odds-api full name)
    3. Title substring fallback (for events without parsed team names)
    """
    from src.core.match_id import normalize_team
    
    game = odds_match.game.lower() if odds_match.game else ""
    
    # Normalize bookmaker team names using canonical function (applies aliases)
    book_t1_norm = normalize_team(odds_match.team1)
    book_t2_norm = normalize_team(odds_match.team2)
    book_teams = {book_t1_norm, book_t2_norm}
    
    # Esports: match via title prefix ("CS2:", "LoL:", etc.)
    # Sports: match via event.game field (titles don't have game prefixes)
    ESPORTS_TITLE_PATTERNS = {
        "cs2": ["cs2:", "counter-strike:", "csgo:", "cs:"],
        "dota2": ["dota2:", "dota 2:", "dota:"],
        "lol": ["lol:", "league of legends:"],
        "valorant": ["valorant:"],
        "cod": ["cod:", "call of duty:"],
        "mlbb": ["mlbb:", "mobile legends bang bang:", "mobile legends:"],
        "hok": ["hok:", "honor of kings:"],
        "r6": ["r6:", "rainbow six:", "rainbow six siege:"],
        "sc2": ["sc2:", "starcraft:", "starcraft 2:", "starcraft ii:"],
        "overwatch": ["overwatch:", "overwatch 2:", "ow2:"],
        "rugby": ["rugby:", "premiership rugby:", "rugby premiership:", "top 14:", "united rugby championship:", "urc:", "six nations:"],
        "rugby_premiership": ["premiership rugby:", "rugby premiership:"],
        "rugby_top14": ["top 14:"],
        "rugby_urc": ["united rugby championship:", "urc:"],
    }
    
    # Sports matched by event.game field (no title prefix)
    SPORTS_GAMES = {"football", "basketball", "hockey", "ufc", "cricket"}
    
    title_patterns = ESPORTS_TITLE_PATTERNS.get(game, [])
    is_sport = game in SPORTS_GAMES
    
    for event in poly_events:
        # ── Game filter ──
        if title_patterns:
            # Esports: require title prefix
            if not any(p in event.title.lower() for p in title_patterns):
                continue
        elif is_sport:
            # Sports: require event.game match
            if event.game and event.game.lower() != game:
                continue
        
        # ── Team matching ──
        if event.team1 and event.team2:
            poly_t1_norm = normalize_team(event.team1)
            poly_t2_norm = normalize_team(event.team2)
            poly_teams = {poly_t1_norm, poly_t2_norm}
            
            # 1. Exact match (handles esports + aliased sports names)
            if book_teams == poly_teams:
                return event
            
            # 2. Containment match for sports (short nickname inside full name)
            # e.g. "nuggets" in "denvernuggets", "bournemouth" in "afcbournemouth"
            if is_sport:
                def teams_contain(a: set, b: set) -> bool:
                    """Check if each name in set a is contained in some name in set b."""
                    for name_a in a:
                        if not any(name_a in name_b for name_b in b):
                            return False
                    return True
                
                if teams_contain(poly_teams, book_teams) or teams_contain(book_teams, poly_teams):
                    return event
        
        # 3. Fallback: title substring matching (events without parsed teams)
        elif not event.team1 and not event.team2:
            title = event.title.lower()
            t1_lower = odds_match.team1.lower()
            t2_lower = odds_match.team2.lower()
            if t1_lower in title and t2_lower in title:
                return event
    
    return None


def find_all_poly_events(
    odds_match: AggregatedMatch,
    poly_events: list,
) -> list:
    """Find ALL matching Polymarket events for an odds match.
    
    Same matching logic as find_poly_event but returns all matches,
    not just the first. This is needed because Polymarket often has
    separate events for the same match:
    - Main event (moneyline, "Will X win?" markets)
    - "More Markets" event (spreads, O/U, BTTS)
    """
    from src.core.match_id import normalize_team
    
    game = odds_match.game.lower() if odds_match.game else ""
    
    book_t1_norm = normalize_team(odds_match.team1)
    book_t2_norm = normalize_team(odds_match.team2)
    book_teams = {book_t1_norm, book_t2_norm}
    
    ESPORTS_TITLE_PATTERNS = {
        "cs2": ["cs2:", "counter-strike:", "csgo:", "cs:"],
        "dota2": ["dota2:", "dota 2:", "dota:"],
        "lol": ["lol:", "league of legends:"],
        "valorant": ["valorant:"],
        "cod": ["cod:", "call of duty:"],
        "mlbb": ["mlbb:", "mobile legends bang bang:", "mobile legends:"],
        "hok": ["hok:", "honor of kings:"],
        "r6": ["r6:", "rainbow six:", "rainbow six siege:"],
        "sc2": ["sc2:", "starcraft:", "starcraft 2:", "starcraft ii:"],
        "overwatch": ["overwatch:", "overwatch 2:", "ow2:"],
        "rugby": ["rugby:", "premiership rugby:", "rugby premiership:", "top 14:", "united rugby championship:", "urc:", "six nations:"],
    }
    
    SPORTS_GAMES = {"football", "basketball", "hockey", "ufc", "cricket"}
    
    title_patterns = ESPORTS_TITLE_PATTERNS.get(game, [])
    is_sport = game in SPORTS_GAMES
    
    results = []
    for event in poly_events:
        if title_patterns:
            if not any(p in event.title.lower() for p in title_patterns):
                continue
        elif is_sport:
            if event.game and event.game.lower() != game:
                continue
        
        if event.team1 and event.team2:
            poly_t1_norm = normalize_team(event.team1)
            poly_t2_norm = normalize_team(event.team2)
            poly_teams = {poly_t1_norm, poly_t2_norm}
            
            if book_teams == poly_teams:
                results.append(event)
                continue
            
            if is_sport:
                def teams_contain(a: set, b: set) -> bool:
                    for name_a in a:
                        if not any(name_a in name_b for name_b in b):
                            return False
                    return True
                
                if teams_contain(poly_teams, book_teams) or teams_contain(book_teams, poly_teams):
                    results.append(event)
                    continue
        
        elif not event.team1 and not event.team2:
            title = event.title.lower()
            t1_lower = odds_match.team1.lower()
            t2_lower = odds_match.team2.lower()
            if t1_lower in title and t2_lower in title:
                results.append(event)
    
    return results

