"""
Position - Data models for order tracking and position management.

Contains the core dataclasses used by OrderWatcher:
- OrderType: ENTRY or HEDGE
- PositionState: Lifecycle states
- WatchedOrder: An order being monitored
- Position: A trading position (entry + optional hedge)
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum

from .order_executor import Order


class OrderType(Enum):
    """Type of order in our system."""
    ENTRY = "ENTRY"   # Initial limit buy
    HEDGE = "HEDGE"   # Hedge order after fill


class PositionState(Enum):
    """State of a position."""
    ENTRY_PENDING = "ENTRY_PENDING"      # Entry order placed, waiting for fill
    ENTRY_PARTIAL = "ENTRY_PARTIAL"      # Entry partially filled
    POSITION_OPEN = "POSITION_OPEN"      # Entry filled, no hedge yet
    HEDGE_PENDING = "HEDGE_PENDING"      # Hedge order placed
    HEDGE_PARTIAL = "HEDGE_PARTIAL"      # Hedge partially filled
    HEDGED = "HEDGED"                    # Fully hedged
    CANCELLED = "CANCELLED"              # Entry cancelled (edge lost, etc.)


@dataclass
class WatchedOrder:
    """An order being monitored."""
    order: Order
    order_type: OrderType
    token_id: str
    team_name: str
    fair_value: float           # Fair probability from bookmakers
    placed_at: datetime = field(default_factory=datetime.utcnow)
    last_checked: datetime = field(default_factory=datetime.utcnow)
    filled_shares: float = 0.0
    
    @property
    def is_entry(self) -> bool:
        return self.order_type == OrderType.ENTRY
    
    @property
    def is_hedge(self) -> bool:
        return self.order_type == OrderType.HEDGE


@dataclass
class Position:
    """A trading position (entry + optional hedge)."""
    position_id: str
    match_id: str
    game: str
    team1: str
    team2: str
    
    # Entry side
    entry_team: str
    entry_token_id: str
    
    # Fields with defaults must come after required fields
    condition_id: str = ""  # Polymarket condition ID for cross-referencing
    entry_order: Optional[WatchedOrder] = None
    entry_filled_shares: float = 0.0
    entry_avg_price: float = 0.0
    
    # Hedge side
    hedge_team: str = ""
    hedge_token_id: str = ""
    hedge_order: Optional[WatchedOrder] = None
    hedge_filled_shares: float = 0.0
    hedge_avg_price: float = 0.0
    
    # State
    state: PositionState = PositionState.ENTRY_PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def entry_cost(self) -> float:
        """Total cost of entry position."""
        return self.entry_filled_shares * self.entry_avg_price
    
    @property
    def hedge_cost(self) -> float:
        """Total cost of hedge position."""
        return self.hedge_filled_shares * self.hedge_avg_price
    
    @property
    def total_cost(self) -> float:
        """Total cost of both positions."""
        return self.entry_cost + self.hedge_cost
    
    @property
    def potential_payout(self) -> float:
        """Payout if fully hedged (either team wins = $1 per share)."""
        return min(self.entry_filled_shares, self.hedge_filled_shares) * 1.0
    
    @property
    def locked_profit(self) -> float:
        """Profit if fully hedged."""
        hedged_shares = min(self.entry_filled_shares, self.hedge_filled_shares)
        if hedged_shares == 0:
            return 0.0
        entry_cost = hedged_shares * self.entry_avg_price
        hedge_cost = hedged_shares * self.hedge_avg_price
        return hedged_shares * 1.0 - entry_cost - hedge_cost
    
    @property
    def profit_percent(self) -> float:
        """Profit as percentage of total cost."""
        if self.total_cost == 0:
            return 0.0
        return self.locked_profit / self.total_cost * 100
