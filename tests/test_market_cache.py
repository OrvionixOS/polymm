"""
Unit tests for state/market_cache.py - Market state caching with change detection.
"""
import pytest
from datetime import datetime, timezone, timedelta

from src.state.market_cache import (
    OrderBookState,
    FairValueState,
    MarketState,
    MarketCache,
)


class TestOrderBookState:
    """Tests for OrderBookState dataclass."""
    
    def test_has_changed_bid(self):
        """Detects significant bid changes."""
        state = OrderBookState(token_id="t1", best_bid=0.50, best_ask=0.55)
        
        # 1¢ change should trigger
        assert state.has_changed(0.51, 0.55) is True
        assert state.has_changed(0.49, 0.55) is True
        
        # Less than 1¢ should not trigger
        assert state.has_changed(0.505, 0.55) is False
    
    def test_has_changed_ignores_ask(self):
        """Only tracks bid changes, not ask."""
        state = OrderBookState(token_id="t1", best_bid=0.50, best_ask=0.55)
        
        # Ask changed but bid didn't = no change
        assert state.has_changed(0.50, 0.60) is False
    
    def test_update_returns_changed(self):
        """Update returns True when changed."""
        state = OrderBookState(token_id="t1", best_bid=0.50, best_ask=0.55)
        
        # No significant change
        changed = state.update(0.50, 100.0, 0.55, 100.0)
        assert changed is False
        
        # Significant change
        changed = state.update(0.52, 100.0, 0.55, 100.0)
        assert changed is True
    
    def test_update_modifies_values(self):
        """Update modifies state values."""
        state = OrderBookState(token_id="t1", best_bid=0.50, best_ask=0.55)
        state.update(0.52, 200.0, 0.58, 150.0)
        
        assert state.best_bid == 0.52
        assert state.best_bid_size == 200.0
        assert state.best_ask == 0.58
        assert state.best_ask_size == 150.0


class TestFairValueState:
    """Tests for FairValueState dataclass."""
    
    def test_has_changed(self):
        """Detects significant fair value changes (2% threshold)."""
        state = FairValueState(match_id="m1", team="Team A", fair_prob=0.50)
        
        # 2% change should trigger
        assert state.has_changed(0.52) is True
        assert state.has_changed(0.48) is True
        
        # Less than 2% should not trigger
        assert state.has_changed(0.51) is False
    
    def test_update(self):
        """Update modifies fair value."""
        state = FairValueState(match_id="m1", team="Team A", fair_prob=0.50)
        changed = state.update(0.55)
        
        assert changed is True
        assert state.fair_prob == 0.55


class TestMarketState:
    """Tests for MarketState dataclass."""
    
    def create_market_state(
        self,
        best_bid: float = 0.45,
        fair_prob: float = 0.55,
    ) -> MarketState:
        """Helper to create MarketState."""
        return MarketState(
            token_id="token1",
            match_id="cs2:a:vs:b",
            team="Team A",
            best_bid=best_bid,
            best_bid_size=100.0,
            best_ask=best_bid + 0.05,
            best_ask_size=100.0,
            fair_prob=fair_prob,
        )
    
    def test_entry_price(self):
        """Entry price = best_bid + 0.01."""
        state = self.create_market_state(best_bid=0.45)
        assert abs(state.entry_price - 0.46) < 0.001
    
    def test_entry_price_zero_bid(self):
        """Entry price = 0 when no bid."""
        state = self.create_market_state(best_bid=0.0)
        assert state.entry_price == 0
    
    def test_edge(self):
        """Edge = fair_prob - entry_price."""
        state = self.create_market_state(best_bid=0.45, fair_prob=0.55)
        # edge = 0.55 - 0.46 = 0.09
        assert abs(state.edge - 0.09) < 0.001
    
    def test_has_edge_true(self):
        """has_edge = True when 10% <= edge <= 25%."""
        # 10% edge
        state = self.create_market_state(best_bid=0.44, fair_prob=0.55)
        # edge = 0.55 - 0.45 = 0.10
        assert state.has_edge is True
        
        # 15% edge
        state = self.create_market_state(best_bid=0.39, fair_prob=0.55)
        # edge = 0.55 - 0.40 = 0.15
        assert state.has_edge is True
    
    def test_has_edge_false_too_low(self):
        """has_edge = False when edge < 10%."""
        state = self.create_market_state(best_bid=0.46, fair_prob=0.55)
        # edge = 0.55 - 0.47 = 0.08 < 10%
        assert state.has_edge is False
    
    def test_has_edge_false_too_high(self):
        """has_edge = False when edge > 25%."""
        state = self.create_market_state(best_bid=0.28, fair_prob=0.55)
        # edge = 0.55 - 0.29 = 0.26 > 25%
        assert state.has_edge is False


class TestMarketCache:
    """Tests for MarketCache class."""
    
    @pytest.fixture
    def cache(self):
        """Create a fresh cache for each test."""
        return MarketCache()
    
    @pytest.mark.asyncio
    async def test_update_order_book_new_token(self, cache):
        """First update for a token creates state and reports change (from 0.0 default)."""
        changed = await cache.update_order_book("t1", 0.50, 100.0, 0.55, 100.0)
        # First update compares against default 0.0 → IS a change
        assert changed is True
    
    @pytest.mark.asyncio
    async def test_update_order_book_significant_change(self, cache):
        """Significant price change returns True."""
        await cache.update_order_book("t1", 0.50, 100.0, 0.55, 100.0)
        changed = await cache.update_order_book("t1", 0.52, 100.0, 0.55, 100.0)
        assert changed is True
    
    @pytest.mark.asyncio
    async def test_update_order_book_no_change(self, cache):
        """No significant change returns False."""
        await cache.update_order_book("t1", 0.50, 100.0, 0.55, 100.0)
        changed = await cache.update_order_book("t1", 0.50, 100.0, 0.55, 100.0)
        assert changed is False
    
    @pytest.mark.asyncio
    async def test_update_fair_value(self, cache):
        """Fair value updates work correctly."""
        # First update compares against default 0.0 → IS a change
        changed = await cache.update_fair_value("m1", "Team A", 0.55)
        assert changed is True
        
        # Insignificant change (< 2% threshold)
        changed = await cache.update_fair_value("m1", "Team A", 0.56)
        assert changed is False
        
        # Significant change (>= 2% threshold)
        changed = await cache.update_fair_value("m1", "Team A", 0.60)
        assert changed is True
    
    @pytest.mark.asyncio
    async def test_get_changed_tokens(self, cache):
        """get_changed_tokens returns and clears changed tokens."""
        await cache.update_order_book("t1", 0.50, 100.0, 0.55, 100.0)
        await cache.update_order_book("t1", 0.52, 100.0, 0.55, 100.0)  # Change
        
        changed = await cache.get_changed_tokens()
        assert "t1" in changed
        
        # Second call returns empty (cleared)
        changed2 = await cache.get_changed_tokens()
        assert len(changed2) == 0
    
    def test_get_cached_bid(self, cache):
        """get_cached_bid returns fresh bids."""
        # Manually add order book state
        cache._order_books["t1"] = OrderBookState(
            token_id="t1",
            best_bid=0.50,
            best_ask=0.55,
            last_updated=datetime.now(timezone.utc)
        )
        
        bid = cache.get_cached_bid("t1", max_age_seconds=30.0)
        assert bid == 0.50
    
    def test_get_cached_bid_stale(self, cache):
        """get_cached_bid returns None for stale data."""
        # Add stale order book state
        cache._order_books["t1"] = OrderBookState(
            token_id="t1",
            best_bid=0.50,
            best_ask=0.55,
            last_updated=datetime.now(timezone.utc) - timedelta(seconds=60)
        )
        
        bid = cache.get_cached_bid("t1", max_age_seconds=30.0)
        assert bid is None
    
    def test_get_cached_bid_missing(self, cache):
        """get_cached_bid returns None for unknown tokens."""
        bid = cache.get_cached_bid("unknown_token")
        assert bid is None
    
    def test_has_order_book_changed_new_token(self, cache):
        """has_order_book_changed returns True for new tokens."""
        changed = cache.has_order_book_changed("new_token", 0.50, 0.55)
        assert changed is True
    
    def test_get_stats(self, cache):
        """get_stats returns correct counts."""
        stats = cache.get_stats()
        assert stats["order_books"] == 0
        assert stats["fair_values"] == 0
        assert stats["token_mappings"] == 0
        assert stats["pending_changes"] == 0
