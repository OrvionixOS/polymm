"""
Bot State - Unified state management with match-level awareness.

This is the single source of truth for:
- Match state (both sides of each market)
- Order tracking (entry and hedge by match)
- Position tracking (filled shares by match)
- Natural arb detection (orders on both sides = no hedge needed)

Key insight: Markets have TWO tokens (Team A, Team B). We need to track
orders at the MATCH level to detect when we already have both sides covered.

REACTIVE ARCHITECTURE:
- State changes trigger callbacks
- Callbacks evaluate and take action (cancel orders, place hedges, etc.)
- Push-based updates from Supabase Realtime and Polymarket WS
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Callable, Set, Any, Tuple
from enum import Enum
import asyncio

import time as _time

from src.core.match_id import normalize_team, normalize_game, make_match_id, parse_match_question, is_individual_game_market

# Import from split modules (backward compatibility maintained via __init__.py)
from src.state.state_events import StateEventType, StateEvent
from src.state.match_state import MatchSide, MatchOrder, MatchPosition, MatchState
from src.state.order_state import OrderStateMixin


# Collision tokens auto-expire after this window. Keeps the set from growing
# unbounded over days/weeks of uptime — a token that was a collision alternate
# of a long-gone event shouldn't block scans forever. Value is intentionally
# generous so that real same-match duplicates inside one event cycle still
# stay blocked.
COLLISION_TOKEN_TTL_SECONDS = 30 * 60  # 30 minutes


def _match_id_has_condition_suffix(match_state) -> bool:
    """True if the match_id carries an embedded condition_id suffix.

    Previously this was detected by a heuristic: `parts[-1].startswith("0x")
    and len >= 10`. That is brittle — Polymarket could change their ID format,
    or another field ending in ":0x…" could trip the check.

    The robust check: compare the trailing `:…` fragment against the
    match's own `condition_id` field. Only treat the match_id as a
    sub-market identifier if the suffix is a genuine prefix of that
    condition_id (Polymarket condition_ids are long; we encode just the
    first N chars into match_ids for readability).
    """
    if not match_state.condition_id or not match_state.match_id:
        return False
    if ":" not in match_state.match_id:
        return False
    suffix = match_state.match_id.rsplit(":", 1)[-1]
    if not suffix:
        return False
    # Authoritative check: the suffix must actually match this match's
    # condition_id (either as a prefix when encoding is truncated, or a
    # full-length match). 10-char minimum guards against false positives
    # from shorter fields that happen to live at the tail.
    if len(suffix) < 10:
        return False
    return match_state.condition_id.startswith(suffix) or suffix == match_state.condition_id


class _CollisionTokenSet:
    """TTL-backed set-like view over collision tokens.

    Supports the minimal set protocol used by BotState (`in`, `add`,
    `discard`, iteration, `len`). Each `add()` extends the token's expiry
    by `ttl_seconds`. Expired entries are lazily purged on read.
    """

    __slots__ = ("_expiry", "_ttl")

    def __init__(self, ttl_seconds: float):
        self._expiry: Dict[str, float] = {}
        self._ttl = float(ttl_seconds)

    def _purge(self) -> None:
        now = _time.monotonic()
        stale = [t for t, exp in self._expiry.items() if exp <= now]
        for t in stale:
            self._expiry.pop(t, None)

    def add(self, token_id: str) -> None:
        self._expiry[token_id] = _time.monotonic() + self._ttl

    def discard(self, token_id: str) -> None:
        self._expiry.pop(token_id, None)

    def clear(self) -> None:
        self._expiry.clear()

    def __contains__(self, token_id: object) -> bool:
        if not isinstance(token_id, str):
            return False
        exp = self._expiry.get(token_id)
        if exp is None:
            return False
        if exp <= _time.monotonic():
            # Lazy purge on miss
            self._expiry.pop(token_id, None)
            return False
        return True

    def __iter__(self):
        self._purge()
        return iter(self._expiry)

    def __len__(self) -> int:
        self._purge()
        return len(self._expiry)



class BotState(OrderStateMixin):
    """
    Unified state manager for the trading bot.
    
    Single source of truth for all match and order state.
    Provides match-level awareness to detect natural arb pairs.
    
    REACTIVE ARCHITECTURE:
    - State changes emit StateEvents
    - Callbacks are typed by event type for efficient routing
    - Async callbacks supported for actions that need I/O
    
    Updated by:
    - OrderWatcher: Order and position changes
    - OddsService: Fair probability updates (now via Supabase Realtime)
    - Book WS: Bid/ask updates
    """
    
    def __init__(self, min_edge: float = 0.05, min_arb_profit: float = 0.05):
        # Configuration
        self.min_edge = min_edge  # Minimum edge for entry orders
        self.min_arb_profit = min_arb_profit  # Minimum profit for arbs
        
        # Match state by match_id
        self._matches: Dict[str, MatchState] = {}
        
        # Quick lookups
        self._token_to_match: Dict[str, str] = {}  # token_id -> match_id
        self._order_to_match: Dict[str, str] = {}  # order_id -> match_id
        self._condition_to_match: Dict[str, str] = {}  # condition_id -> match_id
        
        # Reserved tokens - prevents duplicate orders during placement
        # Token is reserved BEFORE order placement, then registered when order succeeds
        self._reserved_tokens: Set[str] = set()
        
        # Collision tokens - tokens that couldn't be registered due to slot conflict
        # or that are alternates of an already-chosen token (cross-event collision).
        # Each token is tracked with an expiry monotonic timestamp. A token
        # whose expiry has passed is treated as not-colliding anymore.
        # Stored under the legacy attribute name as a MutableSet-compatible
        # wrapper so callers using `self._collision_tokens` / `in` / `add` /
        # `discard` continue to work without edits.
        self._collision_tokens: "_CollisionTokenSet" = _CollisionTokenSet(
            ttl_seconds=COLLISION_TOKEN_TTL_SECONDS
        )
        

        
        # Recently cancelled order IDs - prevents re-registration during resync
        # When Polymarket API has cache lag, cancelled orders may still appear in API
        # This prevents the hydration from re-registering them into BotState
        self._cancelled_order_ids: Set[str] = set()
        
        # Orders currently being replaced (cancel + place new order cycle).
        # During _execute_adjustment / _try_price_improvement, we cancel the old order
        # and place a new one. The Polymarket WebSocket may send a CANCELED event for the
        # old order DURING this window. Without this guard, the WS handler calls
        # clear_order(), deleting the old_order_id from _order_to_match and None-ing the
        # match slot BEFORE replace_order can update them. This set prevents that race.
        self._replacing_order_ids: Set[str] = set()
        
        # Typed callbacks for reactive updates
        self._callbacks: Dict[StateEventType, List[Callable[[StateEvent], None]]] = {
            event_type: [] for event_type in StateEventType
        }
        
        # Async callbacks (for actions needing I/O like cancel/place orders)
        self._async_callbacks: Dict[StateEventType, List[Callable[[StateEvent], Any]]] = {
            event_type: [] for event_type in StateEventType
        }
        
        # Event queue for async processing
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._event_processor_task: Optional[asyncio.Task] = None
        
        # Stats
        self._stats = {
            "matches_tracked": 0,
            "natural_arbs_detected": 0,
            "hedges_skipped": 0,
            "edge_lost_events": 0,
            "edge_gained_events": 0,
        }
        
        # Summary stats (updated during hydration, used for Telegram pinned message)
        self._summary_stats = {
            "open_orders_count": 0,
            "open_orders_risk": 0.0,
            "completed_wins": 0,
            "completed_losses": 0,
            "completed_pnl": 0.0,
            "arb_count": 0,
            "arb_pnl": 0.0,
            "active_positions": 0,
            "active_value": 0.0,
            "active_expected_profit": 0.0,
        }
        

    
    # ===== Match ID Generation (MUST match OddsService) =====
    
    # ===== Match ID Generation (uses shared module) =====
    
    @staticmethod
    def _normalize_game(game: str) -> str:
        """Normalize game name. Uses shared module."""
        return normalize_game(game)
    
    @staticmethod
    def _normalize_team(name: str) -> str:
        """Normalize team name. Uses shared module."""
        return normalize_team(name)
    
    @staticmethod
    def _make_match_id(team1: str, team2: str, game: str) -> str:
        """Create canonical match ID. Uses shared module."""
        return make_match_id(team1, team2, game)
    
    # ===== Match Registration =====
    
    def register_match(
        self,
        match_id: str,
        condition_id: str = "",
        game: str = "",
        team1: str = "",
        team2: str = "",
        token1: str = "",
        token2: str = "",
        trading_deadline: "Optional[datetime]" = None,
    ) -> MatchState:
        """
        Register a new match or get existing one.
        
        Should be called when discovering new markets or placing orders.
        """
        # DEFENSE-IN-DEPTH: If a match with this condition_id already exists under
        # a different match_id, reuse the existing match. Primary match_id alignment
        # is handled by normalize_game() and two-team format in register_hydrated_order.
        # This catches remaining edge cases (e.g., spread/O/U when event title is
        # unavailable from the API).
        if condition_id:
            existing_match_id = self._condition_to_match.get(condition_id)
            if existing_match_id and existing_match_id in self._matches:
                existing = self._matches[existing_match_id]
                # Reuse existing match — update any missing fields
                if token1 and not existing.token1:
                    existing.token1 = token1
                    self._token_to_match[token1] = existing_match_id
                if token2 and not existing.token2:
                    existing.token2 = token2
                    self._token_to_match[token2] = existing_match_id
                wrong_team_names = {"over", "under", "yes", "no"}
                if team1 and (not existing.team1 or normalize_team(existing.team1) in wrong_team_names):
                    existing.team1 = team1
                if team2 and (not existing.team2 or normalize_team(existing.team2) in wrong_team_names):
                    existing.team2 = team2
                if game and not existing.game:
                    existing.game = game
                if trading_deadline and not existing.trading_deadline:
                    existing.trading_deadline = trading_deadline
                # Add alias so the new match_id also resolves to this match
                self._matches[match_id] = existing
                existing.touch()
                return existing
        
        if match_id in self._matches:
            # Update existing match with any new info
            match = self._matches[match_id]
            if token1 and not match.token1:
                match.token1 = token1
                self._token_to_match[token1] = match_id
            if token2 and not match.token2:
                match.token2 = token2
                self._token_to_match[token2] = match_id
            if condition_id and not match.condition_id:
                match.condition_id = condition_id
                self._condition_to_match[condition_id] = match_id
            # Update team names if they were empty or are clearly wrong (Over/Under)
            wrong_team_names = {"over", "under", "yes", "no"}
            if team1 and (not match.team1 or normalize_team(match.team1) in wrong_team_names):
                match.team1 = team1
            if team2 and (not match.team2 or normalize_team(match.team2) in wrong_team_names):
                match.team2 = team2
            if trading_deadline and not match.trading_deadline:
                match.trading_deadline = trading_deadline
            match.touch()
            return match
        
        # Create new match
        match = MatchState(
            match_id=match_id,
            condition_id=condition_id,
            game=game,
            team1=team1,
            team2=team2,
            token1=token1,
            token2=token2,
            trading_deadline=trading_deadline,
        )
        
        self._matches[match_id] = match
        self._stats["matches_tracked"] += 1
        
        # Register lookups
        if token1:
            self._token_to_match[token1] = match_id
        if token2:
            self._token_to_match[token2] = match_id
        if condition_id:
            self._condition_to_match[condition_id] = match_id
            # Persist game type to Redis so hydration can look it up on restart
            if game:
                try:
                    from src.infra.redis_cache import set_condition_game
                    set_condition_game(condition_id, game)
                except Exception:
                    pass  # Redis unavailable — non-fatal
        

        return match
    

    # ===== Order Management (inherited from OrderStateMixin) =====
    # register_order, update_order_fill, cancel_order, replace_order, clear_order
    # register_hydrated_order, register_hydrated_position
    # get_order_info, get_all_open_order_infos, get_order_info_by_id
    # get_orders_below_min_edge, get_hedges_overpaying, get_open_orders, get_hedge_token_ids
    
    # ===== Market Data Updates =====
    
    def update_fair_probs(
        self,
        match_id: str,
        fair_prob1: float,
        fair_prob2: float,
    ) -> Optional[MatchState]:
        """
        Update fair probabilities from bookmakers.
        
        REACTIVE: Checks if any orders lost/gained edge and emits events.
        """
        match = self._matches.get(match_id)
        if not match:
            return None
        
        # Capture old edges before update
        old_edges = {}
        if match.order1 and match.order1.is_open:
            old_edges[match.order1.order_id] = match.get_order_edge(match.order1)
        if match.order2 and match.order2.is_open:
            old_edges[match.order2.order_id] = match.get_order_edge(match.order2)
        
        old_fair1 = match.fair_prob1
        old_fair2 = match.fair_prob2
        
        # Update
        match.fair_prob1 = fair_prob1
        match.fair_prob2 = fair_prob2
        match.odds_updated_at = datetime.now(timezone.utc)  # Track odds freshness
        match.touch()
        
        # Emit fair_probs_updated event
        self._emit(StateEvent(
            event_type=StateEventType.FAIR_PROBS_UPDATED,
            match_id=match_id,
            match_state=match,
            data={"old_fair1": old_fair1, "old_fair2": old_fair2, 
                  "new_fair1": fair_prob1, "new_fair2": fair_prob2},
        ))
        
        # Check for edge changes on open orders
        for order in [match.order1, match.order2]:
            if not order or not order.is_open:
                continue
            
            # Skip hedge orders - they're not subject to edge checks
            if match.is_order_hedge(order):
                continue
            
            old_edge = old_edges.get(order.order_id)
            new_edge = match.get_order_edge(order)
            
            # If we can't calculate new_edge, skip
            if new_edge is None:
                continue
            
            # Check if edge is below threshold
            is_above = new_edge >= self.min_edge
            was_above = old_edge >= self.min_edge if old_edge is not None else True
            
            if not is_above:
                # Edge is below threshold - check if we should emit
                # Only emit if: edge just dropped, OR this is first check and edge is low
                should_emit = was_above or old_edge is None
                
                if should_emit:
                    self._stats["edge_lost_events"] += 1
                    self._emit(StateEvent(
                        event_type=StateEventType.EDGE_LOST,
                        match_id=match_id,
                        match_state=match,
                        data=order,
                        old_edge=old_edge,
                        new_edge=new_edge,
                        token_id=order.token_id,
                        order_id=order.order_id,
                    ))
            elif old_edge is not None and not was_above and is_above:
                # Edge gained!
                self._stats["edge_gained_events"] += 1
                self._emit(StateEvent(
                    event_type=StateEventType.EDGE_GAINED,
                    match_id=match_id,
                    match_state=match,
                    data=order,
                    old_edge=old_edge,
                    new_edge=new_edge,
                    token_id=order.token_id,
                    order_id=order.order_id,
                ))
        

        
        return match
    
    def update_order_fair_value(
        self,
        match_id: str,
        order: 'MatchOrder',
        new_fair_value: float,
    ):
        """
        Update a specific order's per-order fair_value and re-evaluate edge.
        
        Used by OddsService v2 push to update spread/totals orders individually,
        without overwriting the shared match-level fair_prob1/fair_prob2.
        
        Emits EDGE_LOST/EDGE_GAINED if the edge crosses the min_edge threshold.
        """
        match = self._matches.get(match_id)
        if not match or not order or not order.is_open:
            return
        
        # ALWAYS set fair_value — the monitor needs it for ALL orders
        # (including hedges) to calculate profitability and avoid FALLBACK.
        old_edge = match.get_order_edge(order)
        order.fair_value = new_fair_value
        new_edge = match.get_order_edge(order)
        
        # Skip edge event emission for hedge orders
        # (we don't want to cancel hedges based on edge changes)
        if match.is_order_hedge(order):
            return
        
        if old_edge is None or new_edge is None:
            return
        
        was_above = old_edge >= self.min_edge
        is_above = new_edge >= self.min_edge
        
        if was_above and not is_above:
            self._stats["edge_lost_events"] += 1
            self._emit(StateEvent(
                event_type=StateEventType.EDGE_LOST,
                match_id=match_id,
                match_state=match,
                data=order,
                old_edge=old_edge,
                new_edge=new_edge,
                token_id=order.token_id,
                order_id=order.order_id,
            ))
        elif not was_above and is_above:
            self._stats["edge_gained_events"] += 1
            self._emit(StateEvent(
                event_type=StateEventType.EDGE_GAINED,
                match_id=match_id,
                match_state=match,
                data=order,
                old_edge=old_edge,
                new_edge=new_edge,
                token_id=order.token_id,
                order_id=order.order_id,
            ))
    
    def update_fair_probs_by_team(
        self,
        match_id: str,
        team1: str,
        team2: str,
        fair_prob1: float,
        fair_prob2: float,
    ) -> Optional[MatchState]:
        """
        Update fair probabilities from bookmakers, with team name alignment.
        
        This ensures fair_prob1 is correctly assigned to BotState's team1 even
        if the source's team order is different.
        
        Args:
            match_id: Match identifier
            team1: Source's team1 name (fair_prob1 corresponds to this team)
            team2: Source's team2 name (fair_prob2 corresponds to this team)
            fair_prob1: Fair probability for team1 (0-1 scale)
            fair_prob2: Fair probability for team2 (0-1 scale)
        """
        match = self._matches.get(match_id)
        
        # FALLBACK: If match_id lookup fails, search by team names
        # This handles cases where BotState has a different match_id format than OddsService
        # CRITICAL: Require BOTH teams to match exactly - single-team matching is DANGEROUS
        # because it can match the wrong game (e.g., "Bad Luck vs Yawara" could match "Players vs Bad Luck")
        if not match:
            source_t1 = normalize_team(team1.removesuffix(" No") if team1 else "")
            source_t2 = normalize_team(team2.removesuffix(" No") if team2 else "")
            
            for m in self._matches.values():
                # CROSS-MARKET GUARD: Skip BotState matches whose match_id carries
                # an embedded condition_id suffix. These are isolated
                # spread/totals/Yes-No sub-markets managed exclusively by the v2
                # push path (which has market-type-specific correspondence checks).
                # Without this guard, h2h fair probs (e.g., 65%) get pushed to
                # spread entries that share the same team names → catastrophically
                # wrong edges. Example: HOFF h2h=65% overwrites HOFF spread -1.5
                # entry → bot outbids to 42c.
                if _match_id_has_condition_suffix(m):
                    continue  # Skip — managed by v2 push with market type guard
                
                bot_t1 = normalize_team(m.team1.removesuffix(" No") if m.team1 else "")
                bot_t2 = normalize_team(m.team2.removesuffix(" No") if m.team2 else "")
                
                # REQUIRE BOTH TEAMS TO MATCH EXACTLY (in either order)
                # We removed single-team matching - it caused wrong fair values!
                if bot_t1 and bot_t2:  # Only match if BotState has BOTH teams
                    if (source_t1 == bot_t1 and source_t2 == bot_t2) or \
                       (source_t1 == bot_t2 and source_t2 == bot_t1):
                        match = m
                        break
        
        if not match:
            return None
        
        # Check if we need to swap fair probs to align with BotState's team order
        # Use shared normalize_team for consistent comparison - EXACT MATCH ONLY
        # CRITICAL: Strip " No" suffix before comparing - rugby 3-way markets have
        # team names like "Lions No" which need to match "Lions" from odds source
        source_t1 = normalize_team(team1.removesuffix(" No") if team1 else "")
        source_t2 = normalize_team(team2.removesuffix(" No") if team2 else "")
        bot_t1 = normalize_team(match.team1.removesuffix(" No") if match.team1 else "")
        bot_t2 = normalize_team(match.team2.removesuffix(" No") if match.team2 else "")
        
        # Auto-correct wrong team names (Over/Under/Yes/No) from source
        wrong_team_names = {"over", "under", "yes", "no"}
        if bot_t1 in wrong_team_names or bot_t2 in wrong_team_names:
            # BotState has wrong team names - update them from source
            match.team1 = team1
            match.team2 = team2
            bot_t1 = source_t1
            bot_t2 = source_t2
        
        # Determine if teams are swapped - EXACT MATCH REQUIRED
        # If source team1 matches BotState team2, we need to swap
        if bot_t1 and bot_t2:
            # Check if source order matches BotState order
            if source_t1 == bot_t1 and source_t2 == bot_t2:
                # Same order - no swap needed
                pass
            elif source_t1 == bot_t2 and source_t2 == bot_t1:
                # Opposite order - swap fair probs
                fair_prob1, fair_prob2 = fair_prob2, fair_prob1
            else:
                # Teams don't match at all
                # EXPECTED for rugby/cricket/football binary sub-markets:
                # bot has (Team, Team No) while source has (Team, Opponent).
                # These markets don't use bookmaker odds, so the mismatch is harmless.
                if not (match.team2 and match.team2.endswith(" No")):
                    import logging
                    logging.warning(f"Team mismatch in update_fair_probs_by_team: match_id={match_id} source=({team1}, {team2}) bot=({match.team1}, {match.team2})")
        
        # Now call the regular update - use match.match_id (not original match_id parameter)
        # because fallback lookup may have found a match with a different match_id
        return self.update_fair_probs(match.match_id, fair_prob1, fair_prob2)
    
    def update_bid(self, token_id: str, bid: float, ask: Optional[float] = None) -> Optional[MatchState]:
        """
        Update bid/ask from Book WebSocket.
        
        REACTIVE: Checks if bid changes affect order edges and emits events.
        """
        match_id = self._token_to_match.get(token_id)
        if not match_id:
            return None
        
        match = self._matches.get(match_id)
        if not match:
            return None
        
        # Capture old edges before update
        old_edges = {}
        if match.order1 and match.order1.is_open:
            old_edges[match.order1.order_id] = match.get_order_edge(match.order1)
        if match.order2 and match.order2.is_open:
            old_edges[match.order2.order_id] = match.get_order_edge(match.order2)
        
        old_bid1 = match.bid1
        old_bid2 = match.bid2
        
        # Update
        if token_id == match.token1:
            match.bid1 = bid
            if ask is not None:
                match.ask1 = ask
        elif token_id == match.token2:
            match.bid2 = bid
            if ask is not None:
                match.ask2 = ask
        
        match.touch()
        
        # Emit bid_updated event
        self._emit(StateEvent(
            event_type=StateEventType.BID_UPDATED,
            match_id=match_id,
            match_state=match,
            data={"token_id": token_id, "old_bid": old_bid1 if token_id == match.token1 else old_bid2, 
                  "new_bid": bid},
            token_id=token_id,
        ))
        
        # Check for edge changes (less common from bid updates, but possible)
        for order in [match.order1, match.order2]:
            if not order or not order.is_open:
                continue
            
            # Skip hedge orders - they're not subject to edge checks
            if match.is_order_hedge(order):
                continue
            
            old_edge = old_edges.get(order.order_id)
            new_edge = match.get_order_edge(order)
            
            if old_edge is None or new_edge is None:
                continue
            
            # Only emit if edge changed significantly (avoid noise)
            if abs(new_edge - old_edge) < 0.01:
                continue
            
            was_above = old_edge >= self.min_edge
            is_above = new_edge >= self.min_edge
            
            if was_above and not is_above:
                self._stats["edge_lost_events"] += 1
                self._emit(StateEvent(
                    event_type=StateEventType.EDGE_LOST,
                    match_id=match_id,
                    match_state=match,
                    data=order,
                    old_edge=old_edge,
                    new_edge=new_edge,
                    token_id=order.token_id,
                    order_id=order.order_id,
                ))
        
        return match
    
    # ===== Queries =====
    
    def get_match(self, match_id: str) -> Optional[MatchState]:
        """Get match state by match_id."""
        return self._matches.get(match_id)
    
    def get_match_by_token(self, token_id: str) -> Optional[MatchState]:
        """Get match state by token_id."""
        match_id = self._token_to_match.get(token_id)
        return self._matches.get(match_id) if match_id else None
    
    def get_match_by_order(self, order_id: str) -> Optional[MatchState]:
        """Get match state by order_id."""
        match_id = self._order_to_match.get(order_id)
        return self._matches.get(match_id) if match_id else None
    
    def get_match_by_condition(self, condition_id: str) -> Optional[MatchState]:
        """Get match state by Polymarket condition_id."""
        match_id = self._condition_to_match.get(condition_id)
        return self._matches.get(match_id) if match_id else None
    
    def has_exposure_for_match(self, match_id: str) -> bool:
        """
        Check if we have any exposure (order or position) on this match.
        
        Used to skip new opportunities on matches we're already in.
        Replaces OrderWatcher.has_position_for_match().
        """
        match = self._matches.get(match_id)
        if match:
            return match.has_coverage_on_side1 or match.has_coverage_on_side2
        return False
    
    def has_unhedged_position_for_match(self, match_id: str) -> bool:
        """
        Check if we have an unhedged POSITION (not just order) on this match.
        
        This is stricter than has_exposure_for_match - it only returns True
        if we have a filled position that still needs hedging.
        
        Used to prevent piling on when we're already overexposed, while
        still allowing orders on both sides of a match.
        """
        match = self._matches.get(match_id)
        if not match:
            return False
        
        # Check if we have a position on either side that needs hedging
        # Position = filled shares (not just an open order)
        has_position1 = match.position1 and match.position1.shares > 0
        has_position2 = match.position2 and match.position2.shares > 0
        
        if not has_position1 and not has_position2:
            return False  # No positions, just orders - allow more orders
        
        # We have a position. Check if it's fully hedged.
        # If we have positions on BOTH sides, it's hedged (or being hedged)
        if has_position1 and has_position2:
            return False  # Both sides have positions = hedged
        
        # Only one side has a position - it's unhedged
        return True
    
    def has_opposite_coverage(self, token_id: str) -> bool:
        """
        Check if we have coverage (order or position) on the opposite side.
        
        This is THE key check for natural arb detection.
        """
        match = self.get_match_by_token(token_id)
        if not match:
            return False
        return match.has_opposite_coverage(token_id)
    
    def get_matches_needing_hedge(self) -> List[MatchState]:
        """Get all matches that need hedge orders."""
        return [m for m in self._matches.values() if m.needs_hedge is not None]
    
    def get_positions_needing_hedge(self) -> Dict[str, dict]:
        """
        Get all positions that need hedging, in the format expected by HedgeSeeker.
        
        This is the AUTHORITATIVE source for hedge needs - derived directly from
        MatchState positions and orders. No separate tracking needed!
        
        IMPORTANT: When BOTH sides have filled positions, only the DIFFERENCE
        needs hedging. If side1 has 20 shares and side2 has 5 shares, then
        15 more shares are needed on side2 to complete the arb.
        
        Returns:
            Dict[key, hedge_info] where:
            - key: f"{condition_id}_{entry_team}"
            - hedge_info: {entry_team, hedge_team, shares (UNHEDGED), entry_price, 
                          entry_token_id, hedge_token_id, condition_id}
        """
        result: Dict[str, dict] = {}
        
        for match in self._matches.values():
            # Skip past-deadline matches — don't hedge positions for games already started
            if match.is_past_deadline:
                continue
            # Get position sizes on both sides (0 if no position)
            pos1_shares = match.position1.shares if match.position1 else 0.0
            pos2_shares = match.position2.shares if match.position2 else 0.0
            
            # Calculate hedge coverage from open orders (unfilled portion only)
            order1_coverage = 0.0
            order2_coverage = 0.0
            if match.order1 and match.order1.is_open:
                order1_coverage = match.order1.size - match.order1.filled
            if match.order2 and match.order2.is_open:
                order2_coverage = match.order2.size - match.order2.filled
            
            # Case 1: Side1 has larger position - needs more hedge on side2
            # Unhedged = (position1 shares) - (position2 shares) - (open order2 coverage)
            if pos1_shares > pos2_shares:
                unhedged = pos1_shares - pos2_shares - order2_coverage
                
                if unhedged >= 5.0 and match.position1:  # Only if at least 5 unhedged shares
                    # CRITICAL: Use position.team to preserve " No" suffix for NO token detection!
                    entry_team = match.position1.team
                    
                    # CRITICAL FIX: For NO tokens, hedge with SAME team's YES, not opponent
                    if entry_team.endswith(" No"):
                        base_team = entry_team.removesuffix(" No")
                        hedge_team = base_team
                        hedge_token_id = None
                        # Hedge fair value = 1 - NO fair (if NO is 25%, YES is 75%)
                        hedge_fair_value = 1.0 - match.fair_prob1 if match.fair_prob1 else None
                    else:
                        hedge_team = match.team2
                        hedge_token_id = match.token2
                        hedge_fair_value = match.fair_prob2
                    
                    # SAFETY: If hedge_token equals entry token, null it out.
                    # This happens when hydration sets token1 == token2 for single-team
                    # spread markets. HedgeSeeker will look up the correct opposite token.
                    if hedge_token_id and match.position1 and hedge_token_id == match.position1.token_id:
                        hedge_token_id = None
                    
                    key = f"{match.condition_id}_{match.team1}"
                    result[key] = {
                        "entry_team": entry_team,  # Use the team name from position (includes " No" suffix)!
                        "hedge_team": hedge_team,  # Fixed for NO tokens!
                        "shares": unhedged,  # UNHEDGED shares needed
                        "total_entry_shares": pos1_shares,  # TOTAL entry position size
                        "entry_price": match.position1.avg_price,
                        "entry_token_id": match.position1.token_id,  # FIXED: Use position's token, not match's token
                        "hedge_token_id": hedge_token_id,  # May be None for NO tokens
                        "hedge_fair_value": hedge_fair_value,  # May be None for NO tokens
                        "condition_id": match.condition_id,
                        "match_id": match.match_id,
                    }
            
            # Case 2: Side2 has larger position - needs more hedge on side1
            # Unhedged = (position2 shares) - (position1 shares) - (open order1 coverage)
            elif pos2_shares > pos1_shares:
                unhedged = pos2_shares - pos1_shares - order1_coverage
                
                if unhedged >= 5.0 and match.position2:  # Only if at least 5 unhedged shares
                    # CRITICAL: Use position.team to preserve " No" suffix for NO token detection!
                    entry_team = match.position2.team
                    
                    # CRITICAL FIX: For NO tokens, hedge with SAME team's YES, not opponent
                    # Example: "Ulster No" should hedge with "Ulster Yes", NOT "Scarlets Yes"
                    if entry_team.endswith(" No"):
                        # Strip " No" to get base team name
                        base_team = entry_team.removesuffix(" No")
                        hedge_team = base_team  # e.g., "Ulster" not "Scarlets"
                        hedge_token_id = None  # Will be looked up by HedgeSeeker
                        # Hedge fair value = 1 - NO fair (if NO is 25%, YES is 75%)
                        hedge_fair_value = 1.0 - match.fair_prob2 if match.fair_prob2 else None
                    else:
                        # Standard logic: hedge YES with opponent's YES
                        hedge_team = match.team1
                        hedge_token_id = match.token1
                        hedge_fair_value = match.fair_prob1
                    
                    # SAFETY: Same-token guard (mirrors Case 1 above)
                    if hedge_token_id and match.position2 and hedge_token_id == match.position2.token_id:
                        hedge_token_id = None
                    
                    key = f"{match.condition_id}_{match.team2}"
                    result[key] = {
                        "entry_team": entry_team,  # Use the team name from position (includes " No" suffix)!
                        "hedge_team": hedge_team,  # Fixed for NO tokens!
                        "shares": unhedged,  # UNHEDGED shares needed
                        "total_entry_shares": pos2_shares,  # TOTAL entry position size
                        "entry_price": match.position2.avg_price,
                        "entry_token_id": match.position2.token_id,  # FIXED: Use position's token, not match's token
                        "hedge_token_id": hedge_token_id,  # May be None for NO tokens
                        "hedge_fair_value": hedge_fair_value,  # May be None for NO tokens
                        "condition_id": match.condition_id,
                        "match_id": match.match_id,
                    }
            
            # Case 3: Equal positions (pos1 == pos2) - arb complete, no hedge needed
            # This includes the case where both are 0 (no positions at all)
        
        return result
    
    def get_natural_arbs(self) -> List[MatchState]:
        """Get all matches with natural arb pairs (both sides covered)."""
        return [m for m in self._matches.values() if m.has_natural_arb]
    
    def get_fully_hedged(self) -> List[MatchState]:
        """Get all fully hedged matches (positions on both sides)."""
        return [m for m in self._matches.values() if m.is_fully_hedged]
    
    def get_all_matches(self) -> List[MatchState]:
        """Get all tracked matches."""
        return list(self._matches.values())
    
    def cleanup_stale_matches(self) -> int:
        """Remove matches with no active exposure to prevent blocking token accumulation.
        
        A match is considered stale and can be removed if:
        1. No open orders on either side, AND
        2. Either:
           a. No positions at all (match was cancelled/never filled), OR
           b. Completed arb (equal positions on both sides - already hedged)
        
        Returns the number of matches removed.
        """
        to_remove = []
        
        for match_id, match in self._matches.items():
            # Check for open orders
            has_open_orders = (
                (match.order1 and match.order1.is_open) or
                (match.order2 and match.order2.is_open)
            )
            if has_open_orders:
                continue  # Keep - has pending exposure
            
            # Check positions
            pos1_shares = match.position1.shares if match.position1 else 0.0
            pos2_shares = match.position2.shares if match.position2 else 0.0
            
            # No positions at all - can remove
            if pos1_shares == 0 and pos2_shares == 0:
                to_remove.append(match_id)
                continue
            
            # Completed arb (both sides hedged) - can remove
            is_completed_arb = pos1_shares > 0 and pos2_shares > 0 and abs(pos1_shares - pos2_shares) < 5.0
            if is_completed_arb:
                to_remove.append(match_id)
                continue
            
            # Has unhedged position - keep for hedge seeking
        
        # Remove stale matches
        for match_id in to_remove:
            del self._matches[match_id]

        if to_remove:
            print(f"   🧹 Cleaned up {len(to_remove)} stale matches")

        # Also reclaim any reservations that outlived the happy-path window.
        # A reservation older than the threshold is almost certainly leaked
        # (executor crash mid-placement, raised exception that missed the
        # unreserve_token in _execute_opportunity's finally).
        freed = self.cleanup_stale_reservations()
        if freed:
            print(f"   🧹 Freed {freed} stale reserved tokens")

        return len(to_remove)
    

    def is_token_active(self, token_id: str) -> bool:
        """Check if we have any order, position, or reservation on this token.
        
        NOTE: Completed arbs (equal positions on both sides) do NOT block.
        This allows placing new orders if a new opportunity appears.
        """
        # Check collision tokens first - these should NEVER be re-used
        if token_id in self._collision_tokens:
            return True
        
        # Check reserved tokens (prevents race conditions during order placement)
        if token_id in self._reserved_tokens:
            return True
        
        # DIRECT CHECK: Scan ALL matches for any order or position on this token
        # This catches cases where match_id normalization differs between scanner and hydration
        for match in self._matches.values():
            # Check if this token is on either side of any match
            if match.token1 == token_id:
                # CRITICAL: Skip completed arbs (equal positions on both sides)
                # These should NOT block new orders - we want to place more if edge detected
                if match.is_fully_hedged:
                    pos1 = match.position1.shares if match.position1 else 0
                    pos2 = match.position2.shares if match.position2 else 0
                    if abs(pos1 - pos2) < 5.0:  # Nearly equal positions = completed arb (matches hedge threshold)
                        continue  # Don't block this token
                
                if match.has_coverage_on_side1:
                    return True
            elif match.token2 == token_id:
                # CRITICAL: Skip completed arbs (equal positions on both sides)
                if match.is_fully_hedged:
                    pos1 = match.position1.shares if match.position1 else 0
                    pos2 = match.position2.shares if match.position2 else 0
                    if abs(pos1 - pos2) < 5.0:  # Nearly equal positions = completed arb (matches hedge threshold)
                        continue  # Don't block this token
                
                if match.has_coverage_on_side2:
                    return True
            
            # Also check orders directly by token_id (in case token assignment is wrong)
            if match.order1 and match.order1.token_id == token_id and match.order1.is_open:
                return True
            if match.order2 and match.order2.token_id == token_id and match.order2.is_open:
                return True
        
        return False
    
    def reserve_token(self, token_id: str) -> bool:
        """
        Reserve a token before placing an order (race condition prevention).

        Returns True if reserved successfully, False if already active.
        Also records the reservation time so stale reservations (abandoned
        when the executor died mid-placement) can be garbage-collected by
        cleanup_stale_matches() instead of blocking the token forever.
        """
        if self.is_token_active(token_id):
            return False
        self._reserved_tokens.add(token_id)
        if not hasattr(self, "_reserved_token_timestamps"):
            self._reserved_token_timestamps = {}
        self._reserved_token_timestamps[token_id] = _time.monotonic()
        return True

    def unreserve_token(self, token_id: str):
        """Remove a token reservation and its timestamp."""
        self._reserved_tokens.discard(token_id)
        ts = getattr(self, "_reserved_token_timestamps", None)
        if ts is not None:
            ts.pop(token_id, None)

    def cleanup_stale_reservations(self, max_age_seconds: float = 120.0) -> int:
        """Garbage-collect reservations older than max_age_seconds.

        A reservation surviving longer than this is almost certainly leaked
        — the happy path reserves, places an order, and registers within a
        second or two. Returns the number of tokens freed.
        """
        ts = getattr(self, "_reserved_token_timestamps", None)
        if not ts:
            return 0
        now = _time.monotonic()
        stale = [tok for tok, t in ts.items() if now - t > max_age_seconds]
        for tok in stale:
            self._reserved_tokens.discard(tok)
            ts.pop(tok, None)
        return len(stale)

    def get_active_token_ids(self) -> Set[str]:
        """
        Get all token IDs with active orders or positions.
        
        Includes:
        - Tokens with open orders
        - Tokens with filled positions
        - Reserved tokens (pending order placement)
        - Collision tokens (blocked from re-use)
        
        IMPORTANT: Uses order/position token_id directly, not match.token1/token2,
        because those may be empty if team name matching failed during registration.
        """
        active = set(self._reserved_tokens) | set(self._collision_tokens)
        
        for match in self._matches.values():
            # Add tokens with open orders - use order's actual token_id
            if match.order1 and match.order1.is_open:
                active.add(match.order1.token_id)  # Use order's token, not match.token1
                if match.token1:  # Also add match.token1 if set
                    active.add(match.token1)
            if match.order2 and match.order2.is_open:
                active.add(match.order2.token_id)  # Use order's token, not match.token2
                if match.token2:  # Also add match.token2 if set
                    active.add(match.token2)
            
            # Add tokens with positions - use position's actual token_id
            if match.position1 and match.position1.shares > 0:
                active.add(match.position1.token_id)
                if match.token1:
                    active.add(match.token1)
            if match.position2 and match.position2.shares > 0:
                active.add(match.position2.token_id)
                if match.token2:
                    active.add(match.token2)
        
        return active
    
    def get_filled_token_ids(self) -> Set[str]:
        """Get all token IDs with filled positions (shares > 0).
        
        Uses position.token_id directly to handle cases where match.token1/token2
        may be empty due to team name matching issues during registration.
        """
        filled = set()
        
        for match in self._matches.values():
            if match.position1 and match.position1.shares > 0:
                filled.add(match.position1.token_id)
                if match.token1:
                    filled.add(match.token1)
            if match.position2 and match.position2.shares > 0:
                filled.add(match.position2.token_id)
                if match.token2:
                    filled.add(match.token2)
        
        return filled
    
    def get_tokens_blocking_new_orders(self) -> Set[str]:
        """Get tokens that should block new order placement.
        
        This is DIFFERENT from get_active_token_ids():
        - get_active_token_ids: All tokens we're involved with (for WebSocket subscriptions)
        - get_tokens_blocking_new_orders: Tokens where we should NOT place new orders
        
        EXCLUDES tokens from COMPLETED ARBS (equal positions on both sides)!
        This allows placing new orders with edge on completed arbs.
        
        Includes:
        - Reserved tokens (pending order placement)
        - Collision tokens (blocked from re-use)
        - Tokens with open orders (have pending exposure)
        - Tokens with UNHEDGED positions (to prevent piling on)
        
        Does NOT include:
        - Tokens from completed arbs (equal positions on both sides)
        
        RUGBY 3-WAY SPECIAL HANDLING:
        - If ANY token from a rugby match has exposure, block ALL tokens from that match
        - This prevents placing "Scarlets No" when we already have "Ulster No" position
        """
        blocking = set(self._reserved_tokens) | set(self._collision_tokens)
        
        # Track rugby matches that have exposure (block all tokens)
        rugby_matches_with_exposure = set()
        
        for match in self._matches.values():
            # Check if this is a completed arb (equal positions on both sides)
            pos1_shares = match.position1.shares if match.position1 else 0.0
            pos2_shares = match.position2.shares if match.position2 else 0.0
            is_completed_arb = pos1_shares > 0 and pos2_shares > 0 and abs(pos1_shares - pos2_shares) < 5.0
            
            # RUGBY 3-WAY: Track matches with exposure to block all related tokens
            # CRITICAL: Also check match.game and team names with " No" suffix
            # (hydrated positions have condition_id-based match_ids like "0x...")
            is_rugby = (
                match.match_id.startswith("rugby:") or
                match.game == "rugby" or
                (match.team1 and match.team1.endswith(" No")) or
                (match.team2 and match.team2.endswith(" No")) or
                (match.position1 and match.position1.team and match.position1.team.endswith(" No")) or
                (match.position2 and match.position2.team and match.position2.team.endswith(" No"))
            )
            has_exposure = (
                (match.order1 and match.order1.is_open) or
                (match.order2 and match.order2.is_open) or
                (pos1_shares > 0) or
                (pos2_shares > 0)
            )
            if is_rugby and has_exposure and not is_completed_arb:
                rugby_matches_with_exposure.add(match.match_id)
            
            # Add tokens with open orders - always block (pending exposure)
            if match.order1 and match.order1.is_open:
                blocking.add(match.order1.token_id)
                if match.token1:
                    blocking.add(match.token1)
            if match.order2 and match.order2.is_open:
                blocking.add(match.order2.token_id)
                if match.token2:
                    blocking.add(match.token2)
            
            # Add position tokens ONLY if not a completed arb
            if not is_completed_arb:
                if match.position1 and match.position1.shares > 0:
                    blocking.add(match.position1.token_id)
                    if match.token1:
                        blocking.add(match.token1)
                if match.position2 and match.position2.shares > 0:
                    blocking.add(match.position2.token_id)
                    if match.token2:
                        blocking.add(match.token2)
        
        # RUGBY 3-WAY: Block ALL tokens from matches with exposure
        # This prevents placing orders on "Scarlets No" when we have "Ulster No"
        if rugby_matches_with_exposure:
            for match in self._matches.values():
                if match.match_id in rugby_matches_with_exposure:
                    # Block all tokens from this match
                    if match.token1:
                        blocking.add(match.token1)
                    if match.token2:
                        blocking.add(match.token2)
                    if match.order1:
                        blocking.add(match.order1.token_id)
                    if match.order2:
                        blocking.add(match.order2.token_id)
                    if match.position1:
                        blocking.add(match.position1.token_id)
                    if match.position2:
                        blocking.add(match.position2.token_id)
        
        
        return blocking
    
    # Order query helpers inherited from OrderStateMixin:
    # get_order_info, get_all_open_order_infos, get_order_info_by_id
    # get_orders_below_min_edge, get_hedges_overpaying, get_open_orders, get_hedge_token_ids
    
    # ===== Reactive Callback System =====
    
    def on(self, event_type: StateEventType, callback: Callable[[StateEvent], None]):
        """Register a sync callback for a specific event type."""
        self._callbacks[event_type].append(callback)
    
    def on_async(self, event_type: StateEventType, callback: Callable[[StateEvent], Any]):
        """Register an async callback for a specific event type."""
        self._async_callbacks[event_type].append(callback)
    
    def on_change(self, callback: Callable[[str, MatchState, Any], None]):
        """Legacy: Register a callback for all state changes."""
        # Wrap legacy callback for all event types
        def wrapper(event: StateEvent):
            callback(event.event_type.value, event.match_state, event.data)
        
        for event_type in StateEventType:
            self._callbacks[event_type].append(wrapper)
    
    def _emit(self, event: StateEvent):
        """Emit an event to all registered callbacks."""
        event_type = event.event_type
        
        # Call sync callbacks immediately
        for callback in self._callbacks.get(event_type, []):
            try:
                callback(event)
            except Exception as e:
                print(f"⚠️ BotState sync callback error ({event_type.value}): {e}")
        
        # Queue async callbacks for processing
        if self._async_callbacks.get(event_type):
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                print(f"⚠️ BotState event queue full, dropping {event_type.value}")
    
    async def start_event_processor(self):
        """Start the async event processor."""
        if self._event_processor_task is None:
            self._event_processor_task = asyncio.create_task(self._process_events())
    
    async def stop_event_processor(self):
        """Stop the async event processor."""
        if self._event_processor_task:
            self._event_processor_task.cancel()
            try:
                await self._event_processor_task
            except asyncio.CancelledError:
                pass
            self._event_processor_task = None
    
    async def _process_events(self):
        """Process async callbacks from the event queue."""
        while True:
            try:
                event = await self._event_queue.get()
                
                for callback in self._async_callbacks.get(event.event_type, []):
                    try:
                        result = callback(event)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        print(f"⚠️ BotState async callback error ({event.event_type.value}): {e}")
                
                self._event_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"⚠️ BotState event processor error: {e}")
    
    def _notify(self, event_type: str, match: MatchState, data: Any = None):
        """Legacy notify - converts to new event system."""
        try:
            event_enum = StateEventType(event_type)
        except ValueError:
            # Unknown event type, create generic
            event_enum = StateEventType.ORDER_REGISTERED  # Fallback
        
        event = StateEvent(
            event_type=event_enum,
            match_id=match.match_id,
            match_state=match,
            data=data,
        )
        self._emit(event)
    
    # ===== Position Queries (replaces OrderWatcher._positions tracking) =====
    
    def get_open_match_positions(self) -> List[MatchState]:
        """
        Get matches with one side filled but not fully hedged.
        
        These are "open positions" - we have exposure that needs hedging.
        Replaces OrderWatcher.get_open_positions().
        """
        return [
            match for match in self._matches.values()
            if match.is_open  # Has position on one side, not both
        ]
    
    def get_hedged_match_positions(self) -> List[MatchState]:
        """
        Get matches with both sides filled (completed arbs).
        
        Replaces OrderWatcher.get_hedged_positions().
        """
        return [
            match for match in self._matches.values()
            if match.is_hedged  # Has positions on both sides
        ]
    
    def get_total_locked_profit(self) -> float:
        """
        Sum of locked profits from all hedged positions.
        
        Replaces: sum(p.locked_profit for p in watcher.get_hedged_positions())
        """
        return sum(match.locked_profit for match in self.get_hedged_match_positions())
    
    # ===== Stats =====
    
    def get_stats(self) -> dict:
        """Get state statistics."""
        natural_arbs = len(self.get_natural_arbs())
        hedged = len(self.get_fully_hedged())
        needs_hedge = len(self.get_matches_needing_hedge())
        open_orders = len(self.get_open_orders())
        orders_below_edge = len(self.get_orders_below_min_edge())
        hedges_overpaying = len(self.get_hedges_overpaying())
        
        return {
            "matches_tracked": len(self._matches),
            "tokens_mapped": len(self._token_to_match),
            "orders_tracked": len(self._order_to_match),
            "open_orders": open_orders,
            "natural_arbs": natural_arbs,
            "fully_hedged": hedged,
            "needs_hedge": needs_hedge,
            "orders_below_edge": orders_below_edge,
            "hedges_overpaying": hedges_overpaying,
            "event_queue_size": self._event_queue.qsize() if self._event_queue else 0,
            **self._stats,
        }
    
    def get_summary_stats(self) -> dict:
        """
        Get summary stats for Telegram pinned message.
        
        Updated during hydration with P&L, open orders, etc.
        Replaces OrderWatcher.get_summary_stats().
        """
        # Update live values that can change between hydrations
        open_orders = self.get_all_open_order_infos()
        open_orders_count = len(open_orders)
        open_orders_risk = sum(
            o.get("price", 0) * o.get("size", 0) 
            for o in open_orders.values()
        )
        
        # Return cached stats with live open order data
        return {
            **self._summary_stats,
            "open_orders_count": open_orders_count,
            "open_orders_risk": open_orders_risk,
        }
    
    def set_summary_stats(self, stats: dict):
        """
        Set summary stats (called during hydration).
        
        Stores computed P&L, position values, etc.
        """
        self._summary_stats.update(stats)
    
    def record_natural_arb_detected(self):
        """Record that a natural arb was detected (hedge skipped)."""
        self._stats["natural_arbs_detected"] += 1
        self._stats["hedges_skipped"] += 1
    
    # ===== Hydration Methods (inherited from OrderStateMixin) =====
    # register_hydrated_order, register_hydrated_position, clear_order
    
    def mark_match_live(self, token_id: str = "", match_id: str = "") -> Optional[MatchState]:
        """Mark a match as live (in-play)."""
        match = None
        if token_id:
            match = self.get_match_by_token(token_id)
        elif match_id:
            match = self.get_match(match_id)
        
        if match:
            match.is_live = True
            match.touch()
        return match
    
    def debug_dump(self) -> str:
        """Dump current state for debugging."""
        lines = ["=== BotState Debug ==="]
        lines.append(f"Matches: {len(self._matches)}")
        lines.append(f"Tokens mapped: {len(self._token_to_match)}")
        lines.append(f"Orders tracked: {len(self._order_to_match)}")
        lines.append("")
        
        for match in self._matches.values():
            lines.append(f"📊 {match.match_id}")
            lines.append(f"   Teams: {match.team1} vs {match.team2}")
            lines.append(f"   Token1: {match.token1[:20]}..." if match.token1 else "   Token1: None")
            lines.append(f"   Token2: {match.token2[:20]}..." if match.token2 else "   Token2: None")
            if match.order1:
                lines.append(f"   Order1: {match.order1.team} @ {match.order1.price:.2f} (open={match.order1.is_open})")
            if match.order2:
                lines.append(f"   Order2: {match.order2.team} @ {match.order2.price:.2f} (open={match.order2.is_open})")
            if match.position1:
                lines.append(f"   Pos1: {match.position1.shares:.1f} shares @ {match.position1.avg_price:.2f}")
            if match.position2:
                lines.append(f"   Pos2: {match.position2.shares:.1f} shares @ {match.position2.avg_price:.2f}")
            lines.append(f"   Natural Arb: {match.has_natural_arb}")
            lines.append("")
        
        return "\n".join(lines)


# Global singleton
_bot_state: Optional[BotState] = None


def get_bot_state() -> BotState:
    """Get or create global bot state instance."""
    global _bot_state
    if _bot_state is None:
        _bot_state = BotState()
    return _bot_state


def reset_bot_state():
    """Reset the global bot state (for testing)."""
    global _bot_state
    _bot_state = None
