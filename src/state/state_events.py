"""
State Events - Event types for the reactive architecture.

These events are emitted by BotState when state changes occur,
allowing other modules to react appropriately.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any, TYPE_CHECKING
from enum import Enum

if TYPE_CHECKING:
    from .match_state import MatchState


class StateEventType(Enum):
    """Types of state change events that can trigger reactive actions."""
    # Order events
    ORDER_REGISTERED = "order_registered"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REPLACED = "order_replaced"
    
    # Market data events
    FAIR_PROBS_UPDATED = "fair_probs_updated"
    BID_UPDATED = "bid_updated"
    
    # Position events
    POSITION_CREATED = "position_created"
    POSITION_HYDRATED = "position_hydrated"
    
    # Match events
    MATCH_GONE_LIVE = "match_gone_live"
    NATURAL_ARB_DETECTED = "natural_arb_detected"
    
    # Edge events (computed from fair_prob and bid changes)
    EDGE_LOST = "edge_lost"  # Order edge dropped below threshold
    EDGE_GAINED = "edge_gained"  # New opportunity appeared
    STALE_ODDS = "stale_odds"  # Odds are too old to trust


@dataclass
class StateEvent:
    """A state change event with context for reactive handling."""
    event_type: StateEventType
    match_id: str
    match_state: 'MatchState'
    data: Any = None  # Event-specific data
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # For edge events
    old_edge: Optional[float] = None
    new_edge: Optional[float] = None
    token_id: Optional[str] = None
    order_id: Optional[str] = None
