"""
Market State Cache - In-memory cache with change detection.

Prevents redundant processing when market state hasn't changed.
Key optimization for reducing API calls and computation.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Set, List, Tuple
from collections import defaultdict


@dataclass
class OrderBookState:
    """State of an order book for a single token."""
    token_id: str
    best_bid: float = 0.0
    best_bid_size: float = 0.0
    best_ask: float = 0.0
    best_ask_size: float = 0.0
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def has_changed(self, new_bid: float, new_ask: float, threshold: float = 0.01) -> bool:
        """Check if BID changed significantly (default 1¢ threshold).
        
        NOTE: We only track bid changes because:
        1. Outbid detection only cares about bid changes
        2. Edge calculation uses bid (entry_price = best_bid + 0.01)
        3. Ask values differ between code paths, causing false "changes"
        """
        bid_changed = abs(self.best_bid - new_bid) >= threshold
        return bid_changed
    
    def update(self, bid: float, bid_size: float, ask: float, ask_size: float) -> bool:
        """Update state. Returns True if changed significantly."""
        changed = self.has_changed(bid, ask)
        self.best_bid = bid
        self.best_bid_size = bid_size
        self.best_ask = ask
        self.best_ask_size = ask_size
        self.last_updated = datetime.now(timezone.utc)
        return changed


@dataclass  
class FairValueState:
    """Fair value state for a team in a match."""
    match_id: str
    team: str
    fair_prob: float = 0.0  # 0.0 to 1.0
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def has_changed(self, new_prob: float, threshold: float = 0.02) -> bool:
        """Check if fair value changed significantly (default 2% threshold)."""
        return abs(self.fair_prob - new_prob) >= threshold
    
    def update(self, prob: float) -> bool:
        """Update fair value. Returns True if changed significantly."""
        changed = self.has_changed(prob)
        self.fair_prob = prob
        self.last_updated = datetime.now(timezone.utc)
        return changed


@dataclass
class MarketState:
    """Combined state for a tradeable market (token + fair value)."""
    token_id: str
    match_id: str
    team: str
    
    # Order book state
    best_bid: float = 0.0
    best_bid_size: float = 0.0
    best_ask: float = 0.0
    best_ask_size: float = 0.0
    
    # Fair value from bookmakers
    fair_prob: float = 0.0
    
    # Computed edge
    @property
    def entry_price(self) -> float:
        """Entry price = best_bid + 0.01 to jump queue."""
        return self.best_bid + 0.01 if self.best_bid > 0 else 0
    
    @property
    def edge(self) -> float:
        """Edge = fair_prob - entry_price."""
        return self.fair_prob - self.entry_price
    
    @property
    def has_edge(self) -> bool:
        """Check if edge is within tradeable range."""
        return 0.10 <= self.edge <= 0.25  # 10-25% edge


class MarketCache:
    """
    In-memory cache for market state with change detection.
    
    Key features:
    - Tracks order book state per token
    - Tracks fair values per match/team
    - Detects changes and triggers events
    - Prevents redundant processing
    """
    
    def __init__(self):
        # Order book state by token_id
        self._order_books: Dict[str, OrderBookState] = {}
        
        # Fair value state by "match_id:team" key
        self._fair_values: Dict[str, FairValueState] = {}
        
        # Token to match mapping for edge calculation
        self._token_to_match: Dict[str, Tuple[str, str]] = {}  # token_id -> (match_id, team)
        
        # Changed tokens since last clear
        self._changed_tokens: Set[str] = set()
        
        # Async event for change notification
        self._change_event = asyncio.Event()
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
    
    async def update_order_book(
        self, 
        token_id: str, 
        best_bid: float, 
        best_bid_size: float,
        best_ask: float,
        best_ask_size: float,
    ) -> bool:
        """
        Update order book state for a token.
        Returns True if state changed significantly.
        """
        async with self._lock:
            if token_id not in self._order_books:
                self._order_books[token_id] = OrderBookState(token_id=token_id)
            
            state = self._order_books[token_id]
            changed = state.update(best_bid, best_bid_size, best_ask, best_ask_size)
            
            if changed:
                self._changed_tokens.add(token_id)
                self._change_event.set()
            
            return changed
    
    async def update_fair_value(
        self,
        match_id: str,
        team: str,
        fair_prob: float,
        token_id: Optional[str] = None,
    ) -> bool:
        """
        Update fair value for a team in a match.
        Returns True if value changed significantly.
        """
        key = f"{match_id}:{team.lower()}"
        
        async with self._lock:
            if key not in self._fair_values:
                self._fair_values[key] = FairValueState(match_id=match_id, team=team)
            
            state = self._fair_values[key]
            changed = state.update(fair_prob)
            
            # Also track token mapping if provided
            if token_id:
                self._token_to_match[token_id] = (match_id, team)
                if changed:
                    self._changed_tokens.add(token_id)
                    self._change_event.set()
            
            return changed
    
    async def get_market_state(self, token_id: str) -> Optional[MarketState]:
        """Get combined market state for a token."""
        async with self._lock:
            order_book = self._order_books.get(token_id)
            if not order_book:
                return None
            
            match_info = self._token_to_match.get(token_id)
            fair_prob = 0.0
            match_id = ""
            team = ""
            
            if match_info:
                match_id, team = match_info
                fair_key = f"{match_id}:{team.lower()}"
                fair_state = self._fair_values.get(fair_key)
                if fair_state:
                    fair_prob = fair_state.fair_prob
            
            return MarketState(
                token_id=token_id,
                match_id=match_id,
                team=team,
                best_bid=order_book.best_bid,
                best_bid_size=order_book.best_bid_size,
                best_ask=order_book.best_ask,
                best_ask_size=order_book.best_ask_size,
                fair_prob=fair_prob,
            )
    
    async def get_changed_tokens(self) -> Set[str]:
        """Get tokens that changed since last call and clear the set."""
        async with self._lock:
            changed = self._changed_tokens.copy()
            self._changed_tokens.clear()
            self._change_event.clear()
            return changed
    
    async def wait_for_changes(self, timeout: float = 5.0) -> Set[str]:
        """Wait for state changes with timeout. Returns changed token IDs."""
        try:
            await asyncio.wait_for(self._change_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return await self.get_changed_tokens()
    
    def get_order_book_sync(self, token_id: str) -> Optional[OrderBookState]:
        """Synchronous access to order book state (for quick checks)."""
        return self._order_books.get(token_id)
    
    def get_cached_bid(self, token_id: str, max_age_seconds: float = 30.0) -> Optional[float]:
        """
        Get cached best bid if it's fresh enough.
        
        Returns the bid price if we have data within max_age_seconds, None otherwise.
        Useful for skipping API calls when WebSocket data is recent.
        """
        state = self._order_books.get(token_id)
        if not state or state.best_bid <= 0:
            return None
        
        age = (datetime.now(timezone.utc) - state.last_updated).total_seconds()
        if age > max_age_seconds:
            return None
        
        return state.best_bid
    
    def get_cached_bids_batch(self, token_ids: list, max_age_seconds: float = 30.0) -> Dict[str, Optional[float]]:
        """Get cached bids for multiple tokens. Returns None for stale/missing entries."""
        result = {}
        for token_id in token_ids:
            result[token_id] = self.get_cached_bid(token_id, max_age_seconds)
        return result
    
    def has_order_book_changed(self, token_id: str, new_bid: float, new_ask: float) -> bool:
        """Quick check if order book changed without updating."""
        state = self._order_books.get(token_id)
        if not state:
            return True  # New market = changed
        return state.has_changed(new_bid, new_ask)
    
    def get_stats(self) -> dict:
        """Get cache statistics."""
        return {
            "order_books": len(self._order_books),
            "fair_values": len(self._fair_values),
            "token_mappings": len(self._token_to_match),
            "pending_changes": len(self._changed_tokens),
        }


# Global cache instance
_market_cache: Optional[MarketCache] = None


def get_market_cache() -> MarketCache:
    """Get or create global market cache instance."""
    global _market_cache
    if _market_cache is None:
        _market_cache = MarketCache()
    return _market_cache
