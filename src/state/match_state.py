"""
Match State - Data models for tracking match-level state.

This module contains the key abstraction for tracking BOTH sides of a market together,
enabling natural arb detection and proper hedge coordination.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Tuple
from enum import Enum


class MatchSide(Enum):
    """Which side of a match the order is on (not to be confused with OrderSide in order_executor.py)."""
    TEAM1 = "TEAM1"
    TEAM2 = "TEAM2"


@dataclass
class MatchOrder:
    """An order on one side of a match."""
    order_id: str
    token_id: str
    team: str
    price: float
    size: float
    filled: float = 0.0
    is_open: bool = True
    is_entry: bool = True  # vs hedge
    placed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Per-order fair value — set at placement time, isolates spread/totals from moneyline
    fair_value: Optional[float] = None
    
    @property
    def is_filled(self) -> bool:
        return self.filled >= self.size - 0.01
    
    @property
    def is_partial(self) -> bool:
        return 0 < self.filled < self.size - 0.01


@dataclass
class MatchPosition:
    """A filled position on one side of a match."""
    token_id: str
    team: str
    shares: float
    avg_price: float
    filled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MatchState:
    """
    Complete state for a single match (both sides).
    
    This is the key abstraction - we track BOTH tokens of a match together,
    so we can detect natural arb pairs (orders on both sides).
    """
    match_id: str
    condition_id: str = ""  # Polymarket condition ID
    game: str = ""
    team1: str = ""
    team2: str = ""
    token1: str = ""  # Team 1's token
    token2: str = ""  # Team 2's token
    
    # Market data (updated by Book WS)
    fair_prob1: Optional[float] = None  # From OddsService (0-1)
    fair_prob2: Optional[float] = None
    bid1: Optional[float] = None  # From Book WS
    bid2: Optional[float] = None
    ask1: Optional[float] = None
    ask2: Optional[float] = None
    
    # Our orders (by token)
    order1: Optional[MatchOrder] = None  # Our order on team1
    order2: Optional[MatchOrder] = None  # Our order on team2
    
    # Our filled positions
    position1: Optional[MatchPosition] = None
    position2: Optional[MatchPosition] = None
    
    # Is this match live (in-play)?
    is_live: bool = False
    
    # Odds freshness tracking
    odds_updated_at: Optional[datetime] = None  # Last time fair probs were updated
    
    # Trading deadline — absolute UTC timestamp when we stop trading this market
    # Sports/esports/mentions: game_start_time; Stocks: end_date - N hours; Weather: city cutoff
    trading_deadline: Optional[datetime] = None
    
    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def is_past_deadline(self) -> bool:
        """Check if this match is past its trading deadline."""
        if not self.trading_deadline:
            return False
        deadline = self.trading_deadline
        if not deadline.tzinfo:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= deadline
    
    # ===== Staleness Helpers =====
    
    def get_odds_age_seconds(self) -> Optional[float]:
        """Get how old the odds are in seconds."""
        if self.odds_updated_at is None:
            return None
        now = datetime.now(timezone.utc)
        return (now - self.odds_updated_at).total_seconds()
    
    def is_odds_stale(self, stale_threshold: float) -> bool:
        """Check if odds are stale (older than threshold)."""
        age = self.get_odds_age_seconds()
        if age is None:
            return False  # No odds yet, not stale (just unknown)
        return age > stale_threshold
    
    # ===== Key Properties =====
    
    @property
    def has_order_on_side1(self) -> bool:
        """Do we have an open order on team1?"""
        return self.order1 is not None and self.order1.is_open
    
    @property
    def has_order_on_side2(self) -> bool:
        """Do we have an open order on team2?"""
        return self.order2 is not None and self.order2.is_open
    
    @property
    def has_position_on_side1(self) -> bool:
        """Do we have a filled position on team1?"""
        return self.position1 is not None and self.position1.shares > 0
    
    @property
    def has_position_on_side2(self) -> bool:
        """Do we have a filled position on team2?"""
        return self.position2 is not None and self.position2.shares > 0
    
    @property
    def has_coverage_on_side1(self) -> bool:
        """Do we have an order OR position on team1?"""
        return self.has_order_on_side1 or self.has_position_on_side1
    
    @property
    def has_coverage_on_side2(self) -> bool:
        """Do we have an order OR position on team2?"""
        return self.has_order_on_side2 or self.has_position_on_side2
    
    @property
    def has_natural_arb(self) -> bool:
        """
        Do we have both sides covered (orders or positions)?
        
        This is the KEY check - if True, no hedge order is needed
        because the other side will naturally complete the arb.
        """
        return self.has_coverage_on_side1 and self.has_coverage_on_side2
    
    @property
    def is_fully_hedged(self) -> bool:
        """Do we have filled positions on BOTH sides?"""
        return self.has_position_on_side1 and self.has_position_on_side2
    
    @property
    def unhedged_shares_side1(self) -> float:
        """How many side1 shares are NOT covered by side2 positions/orders?"""
        if not self.position1:
            return 0.0
        entry_shares = self.position1.shares
        hedge_shares = 0.0
        if self.position2:
            hedge_shares += self.position2.shares
        if self.order2 and self.order2.is_open:
            hedge_shares += (self.order2.size - self.order2.filled)
        return max(0.0, entry_shares - hedge_shares)
    
    @property
    def unhedged_shares_side2(self) -> float:
        """How many side2 shares are NOT covered by side1 positions/orders?"""
        if not self.position2:
            return 0.0
        entry_shares = self.position2.shares
        hedge_shares = 0.0
        if self.position1:
            hedge_shares += self.position1.shares
        if self.order1 and self.order1.is_open:
            hedge_shares += (self.order1.size - self.order1.filled)
        return max(0.0, entry_shares - hedge_shares)
    
    @property
    def needs_hedge(self) -> Optional[str]:
        """
        Which token needs a hedge? Returns token_id or None.
        
        Now handles PARTIAL hedges:
        - 20 VIT + 5 SK = needs 15 more SK (returns SK token)
        - Returns token if >= 5 unhedged shares
        """
        if self.unhedged_shares_side1 >= 5.0:
            return self.token2  # Need more hedge on side2
        if self.unhedged_shares_side2 >= 5.0:
            return self.token1  # Need more hedge on side1
        return None
    
    @property
    def edge1(self) -> Optional[float]:
        """Edge on team1 = fair_prob - bid - 0.01."""
        if self.fair_prob1 is not None and self.bid1 is not None:
            entry_price = self.bid1 + 0.01
            return self.fair_prob1 - entry_price
        return None
    
    @property
    def edge2(self) -> Optional[float]:
        """Edge on team2 = fair_prob - bid - 0.01."""
        if self.fair_prob2 is not None and self.bid2 is not None:
            entry_price = self.bid2 + 0.01
            return self.fair_prob2 - entry_price
        return None
    
    # ===== Edge Evaluation Methods =====
    
    def is_order_hedge(self, order: 'MatchOrder') -> bool:
        """
        Check if an order is a hedge order.
        
        An order is a hedge if there's a position on the OPPOSITE side.
        Entry orders have no position on the opposite side.
        """
        if order.token_id == self.token1 and self.has_position_on_side2:
            return True  # Order on side1, position on side2 -> hedge
        if order.token_id == self.token2 and self.has_position_on_side1:
            return True  # Order on side2, position on side1 -> hedge
        return False
    
    def get_fair_value_for_order(self, order: 'MatchOrder') -> Optional[float]:
        """
        Get the fair value for an order. Single canonical lookup path.
        
        ALL fair value lookups for orders should go through this method.
        
        PRIORITY:
        1. order.fair_value — per-order, market-type-specific (set by OddsService v2 push)
        2. Match-level fair_prob by team name match
        3. Match-level fair_prob by token position (fallback)
        
        For NO tokens (team ends with " No"), inverts the fair prob.
        """
        # PRIORITY 1: Per-order fair value (most reliable for spreads/totals)
        if order.fair_value is not None:
            return order.fair_value
        
        # SAFETY NET: For isolated sub-markets (spread/totals/Yes-No with condition_id
        # suffix in match_id), DO NOT fall through to match-level fair_prob.
        # These entries get their fair_prob from the v2 push path, but h2h pushes
        # via update_fair_probs_by_team can contaminate match-level fair_prob1/prob2.
        # Example: HOFF spread -1.5 gets match.fair_prob1=0.65 from h2h push → wrong!
        # Only trust order.fair_value (PRIORITY 1) for these markets.
        if self.match_id and ":" in self.match_id:
            parts = self.match_id.rsplit(":", 1)
            if len(parts) == 2 and len(parts[1]) >= 10 and parts[1].startswith("0x"):
                return None  # Isolated sub-market without per-order fair — refuse to guess
        
        # PRIORITY 2-3: Fall back to match-level fair_prob
        from src.core.match_id import normalize_team
        
        fair_value = None
        
        # Get base team name (strip " No" suffix for comparison)
        order_team = order.team or ""
        order_team_base = order_team.removesuffix(" No")
        order_team_norm = normalize_team(order_team_base)
        
        # PRIORITY 2: Look up by TEAM NAME
        team1_norm = normalize_team(self.team1) if self.team1 else ""
        team2_norm = normalize_team(self.team2) if self.team2 else ""
        
        if order_team_norm == team1_norm and self.fair_prob1 is not None:
            fair_value = self.fair_prob1
        elif order_team_norm == team2_norm and self.fair_prob2 is not None:
            fair_value = self.fair_prob2
        else:
            # PRIORITY 3: Fallback to token position
            if order.token_id == self.token1 and self.fair_prob1 is not None:
                fair_value = self.fair_prob1
            elif order.token_id == self.token2 and self.fair_prob2 is not None:
                fair_value = self.fair_prob2
            else:
                return None
        
        # For NO tokens, invert the fair value
        if order.team and order.team.endswith(" No"):
            fair_value = 1 - fair_value
        
        return fair_value
    
    def get_order_edge(self, order: 'MatchOrder') -> Optional[float]:
        """
        Calculate current edge for an order.
        
        Edge = fair_value - order_price
        
        Delegates to get_fair_value_for_order for the canonical fair value lookup.
        """
        fair_value = self.get_fair_value_for_order(order)
        if fair_value is None:
            return None
        return fair_value - order.price
    
    def get_all_order_edges(self) -> List[Tuple['MatchOrder', float]]:
        """Get edges for all open orders on this match."""
        edges = []
        if self.order1 and self.order1.is_open:
            edge = self.get_order_edge(self.order1)
            if edge is not None:
                edges.append((self.order1, edge))
        if self.order2 and self.order2.is_open:
            edge = self.get_order_edge(self.order2)
            if edge is not None:
                edges.append((self.order2, edge))
        return edges
    
    def get_arb_profit_percent(self) -> Optional[float]:
        """
        Calculate arb profit if we have both sides covered.
        
        Profit = 1.0 - (entry_price + hedge_price)
        
        Returns None if we don't have both sides.
        """
        # Check positions
        if self.position1 and self.position2:
            return 1.0 - (self.position1.avg_price + self.position2.avg_price)
        
        # Check position + order
        if self.position1 and self.order2 and self.order2.is_open:
            return 1.0 - (self.position1.avg_price + self.order2.price)
        if self.position2 and self.order1 and self.order1.is_open:
            return 1.0 - (self.position2.avg_price + self.order1.price)
        
        # Check two orders (natural arb)
        if self.order1 and self.order1.is_open and self.order2 and self.order2.is_open:
            return 1.0 - (self.order1.price + self.order2.price)
        
        return None
    
    @property
    def is_hedged(self) -> bool:
        """True if both sides have filled positions (arb complete)."""
        return (self.has_position_on_side1 and self.has_position_on_side2)
    
    @property
    def is_open(self) -> bool:
        """True if we have a position on one side but not fully hedged (arb incomplete)."""
        has_any_position = self.has_position_on_side1 or self.has_position_on_side2
        return has_any_position and not self.is_hedged
    
    @property
    def hedged_shares(self) -> float:
        """Number of shares that are fully hedged (min of both sides)."""
        if not self.is_hedged:
            return 0.0
        return min(self.position1.shares, self.position2.shares)
    
    @property
    def locked_profit(self) -> float:
        """
        Locked dollar profit for a completed arb.
        
        = hedged_shares * $1 - (entry_cost + hedge_cost)
        """
        if not self.is_hedged:
            return 0.0
        
        hedged = self.hedged_shares
        entry_cost = hedged * self.position1.avg_price
        hedge_cost = hedged * self.position2.avg_price
        return hedged * 1.0 - entry_cost - hedge_cost
    
    @property
    def locked_profit_percent(self) -> float:
        """Locked profit as percentage of total cost."""
        if not self.is_hedged:
            return 0.0
        
        hedged = self.hedged_shares
        total_cost = hedged * (self.position1.avg_price + self.position2.avg_price)
        if total_cost == 0:
            return 0.0
        return self.locked_profit / total_cost * 100
    
    def is_hedge_overpaying(self, hedge_order: 'MatchOrder', min_arb_profit: float = 0.05) -> bool:
        """
        Check if a hedge order is overpaying (would result in < min_arb_profit).
        
        Returns True if the hedge price is too high and should be adjusted.
        """
        # Find the entry position this hedge is covering
        if hedge_order.token_id == self.token1:
            entry_pos = self.position2
        else:
            entry_pos = self.position1
        
        if not entry_pos:
            return False
        
        # Calculate potential profit
        total_cost = entry_pos.avg_price + hedge_order.price
        profit = 1.0 - total_cost
        
        return profit < min_arb_profit
    
    def get_fair_for_token(self, token_id: str) -> Optional[float]:
        """
        Get fair probability for a token.
        
        For NO tokens (team ends with " No"), inverts the fair prob.
        E.g., if Montauban YES = 4%, then Montauban NO = 96%.
        """
        fair_value = None
        team_name = None
        order_team = None  # Check order's team name for " No" suffix
        
        if token_id == self.token1:
            fair_value = self.fair_prob1
            team_name = self.team1
            # Check if there's an order for this token with a " No" suffix
            if self.order1 and self.order1.token_id == token_id:
                order_team = self.order1.team
            elif self.position1 and self.position1.token_id == token_id:
                order_team = self.position1.team
        elif token_id == self.token2:
            fair_value = self.fair_prob2
            team_name = self.team2
            # Check if there's an order for this token with a " No" suffix
            if self.order2 and self.order2.token_id == token_id:
                order_team = self.order2.team
            elif self.position2 and self.position2.token_id == token_id:
                order_team = self.position2.team
        
        if fair_value is None:
            return None
        
        # CRITICAL FIX: For NO tokens, invert the fair value
        # Check BOTH match.team AND order.team for " No" suffix
        # Order.team is more reliable for rugby 3-way markets where match.team = "Lions" but order.team = "Lions No"
        is_no_token = (
            (team_name and team_name.endswith(" No")) or
            (order_team and order_team.endswith(" No"))
        )
        if is_no_token:
            return 1 - fair_value
        
        return fair_value
    
    # ===== Helper Methods =====
    
    def get_order_for_token(self, token_id: str) -> Optional[MatchOrder]:
        """Get our order for a specific token.
        
        Checks both the token mapping (self.token1/token2) AND the actual order's token_id.
        This handles cases where the token wasn't properly registered in the match slots
        but the order itself has the correct token_id.
        """
        # First check token slot mapping
        if token_id == self.token1:
            return self.order1
        elif token_id == self.token2:
            return self.order2
        
        # FALLBACK: Check if any order has this token_id directly
        # This handles cases where order was assigned but token slot wasn't updated
        if self.order1 and self.order1.token_id == token_id:
            return self.order1
        elif self.order2 and self.order2.token_id == token_id:
            return self.order2
        
        return None
    
    def get_opposite_token(self, token_id: str) -> Optional[str]:
        """Get the token on the opposite side of this match."""
        if token_id == self.token1:
            return self.token2
        elif token_id == self.token2:
            return self.token1
        return None
    
    def get_opposite_order(self, token_id: str) -> Optional[MatchOrder]:
        """Get the order on the opposite side of this match."""
        opposite_token = self.get_opposite_token(token_id)
        if opposite_token:
            return self.get_order_for_token(opposite_token)
        return None
    
    def has_opposite_coverage(self, token_id: str) -> bool:
        """Check if we have coverage (order or position) on the opposite side."""
        if token_id == self.token1:
            return self.has_coverage_on_side2
        elif token_id == self.token2:
            return self.has_coverage_on_side1
        return False
    
    def touch(self):
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now(timezone.utc)
