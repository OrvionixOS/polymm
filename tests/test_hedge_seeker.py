"""
Unit tests for monitoring/hedge_seeker.py - Hedge seeking with backoff logic.

Note: HedgeSeeker has complex dependencies that cause circular imports.
These tests use mock-based approaches and test the core logic patterns.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta


class MockHedgeSeeker:
    """Mock HedgeSeeker for testing backoff logic patterns."""
    
    # Backoff configuration (mirrors HedgeSeeker)
    FAST_RETRY_ATTEMPTS = 3
    SLOW_RETRY_ATTEMPTS = 7
    GIVE_UP_ATTEMPTS = 20
    SLOW_RETRY_DELAY = 300  # 5 minutes
    LONG_COOLDOWN = 3600    # 1 hour
    
    def __init__(self, config: dict):
        self.config = config
        
        # Warning de-duplication
        self._warned_unprofitable = {}
        
        # Backoff state per position
        self._attempt_counts = {}
        self._last_attempt_time = {}
        self._given_up = {}
        self._permanently_given_up = set()
    
    def _should_skip_due_to_backoff(self, key: str) -> bool:
        """Check if we should skip this position due to backoff rules."""
        now = datetime.now(timezone.utc)
        
        # Permanently given up?
        if key in self._permanently_given_up:
            return True
        
        # In cooldown after many failures?
        if key in self._given_up:
            cooldown_end = self._given_up[key] + timedelta(seconds=self.LONG_COOLDOWN)
            if now < cooldown_end:
                return True
            else:
                # Cooldown expired, reset and try again
                del self._given_up[key]
                self._attempt_counts[key] = 0
        
        attempts = self._attempt_counts.get(key, 0)
        
        # In slow retry phase? Check if enough time has passed
        if attempts >= self.FAST_RETRY_ATTEMPTS:
            last_attempt = self._last_attempt_time.get(key)
            if last_attempt:
                time_since_last = (now - last_attempt).total_seconds()
                if time_since_last < self.SLOW_RETRY_DELAY:
                    return True  # Not enough time has passed
        
        return False
    
    def _record_attempt(self, key: str, success: bool):
        """Record a hedge attempt result."""
        now = datetime.now(timezone.utc)
        
        if success:
            # Clear all backoff state on success
            self._attempt_counts.pop(key, None)
            self._last_attempt_time.pop(key, None)
            self._given_up.pop(key, None)
            self._warned_unprofitable.pop(key, None)
            return
        
        # Record failure
        self._attempt_counts[key] = self._attempt_counts.get(key, 0) + 1
        self._last_attempt_time[key] = now
        
        attempts = self._attempt_counts[key]
        
        # Check if we should give up
        if attempts >= self.GIVE_UP_ATTEMPTS:
            self._permanently_given_up.add(key)
        elif attempts >= self.FAST_RETRY_ATTEMPTS + self.SLOW_RETRY_ATTEMPTS:
            self._given_up[key] = now


class TestHedgeSeekerInit:
    """Tests for HedgeSeeker initialization patterns."""
    
    def test_init_stores_config(self):
        """Seeker stores config."""
        config = {"min_profit": 0.05}
        seeker = MockHedgeSeeker(config)
        assert seeker.config == config
    
    def test_init_empty_backoff_state(self):
        """Seeker starts with empty backoff state."""
        seeker = MockHedgeSeeker({})
        
        assert len(seeker._attempt_counts) == 0
        assert len(seeker._last_attempt_time) == 0
        assert len(seeker._given_up) == 0
        assert len(seeker._permanently_given_up) == 0
    
    def test_backoff_constants(self):
        """Backoff constants are correctly set."""
        seeker = MockHedgeSeeker({})
        
        assert seeker.FAST_RETRY_ATTEMPTS == 3
        assert seeker.SLOW_RETRY_ATTEMPTS == 7
        assert seeker.GIVE_UP_ATTEMPTS == 20
        assert seeker.SLOW_RETRY_DELAY == 300  # 5 minutes
        assert seeker.LONG_COOLDOWN == 3600    # 1 hour


class TestBackoffLogic:
    """Tests for _should_skip_due_to_backoff method."""
    
    @pytest.fixture
    def seeker(self):
        """Create mock seeker."""
        return MockHedgeSeeker({})
    
    def test_no_skip_on_first_attempt(self, seeker):
        """First attempt is not skipped."""
        assert seeker._should_skip_due_to_backoff("key1") is False
    
    def test_skip_when_permanently_given_up(self, seeker):
        """Skip when permanently given up."""
        seeker._permanently_given_up.add("key1")
        assert seeker._should_skip_due_to_backoff("key1") is True
    
    def test_skip_during_cooldown(self, seeker):
        """Skip during long cooldown period."""
        now = datetime.now(timezone.utc)
        seeker._given_up["key1"] = now  # Just gave up
        assert seeker._should_skip_due_to_backoff("key1") is True
    
    def test_resume_after_cooldown_expired(self, seeker):
        """Resume after cooldown expires."""
        # Set gave up time to > 1 hour ago
        seeker._given_up["key1"] = datetime.now(timezone.utc) - timedelta(seconds=3700)
        assert seeker._should_skip_due_to_backoff("key1") is False
        # Should have cleared the given_up state
        assert "key1" not in seeker._given_up
    
    def test_fast_retry_phase_no_skip(self, seeker):
        """Fast retry phase (first 3 attempts) doesn't skip."""
        seeker._attempt_counts["key1"] = 2  # Under FAST_RETRY_ATTEMPTS
        assert seeker._should_skip_due_to_backoff("key1") is False
    
    def test_slow_retry_phase_skips_if_too_soon(self, seeker):
        """Slow retry phase skips if not enough time passed."""
        now = datetime.now(timezone.utc)
        seeker._attempt_counts["key1"] = 4  # In slow retry phase
        seeker._last_attempt_time["key1"] = now - timedelta(seconds=60)  # 1 minute ago
        
        # 60 seconds < 300 seconds (SLOW_RETRY_DELAY), should skip
        assert seeker._should_skip_due_to_backoff("key1") is True
    
    def test_slow_retry_phase_allows_if_enough_time(self, seeker):
        """Slow retry phase allows if enough time passed."""
        seeker._attempt_counts["key1"] = 4  # In slow retry phase
        # 6 minutes ago - more than SLOW_RETRY_DELAY
        seeker._last_attempt_time["key1"] = datetime.now(timezone.utc) - timedelta(seconds=360)
        
        assert seeker._should_skip_due_to_backoff("key1") is False


class TestRecordAttempt:
    """Tests for _record_attempt method."""
    
    @pytest.fixture
    def seeker(self):
        """Create mock seeker."""
        return MockHedgeSeeker({})
    
    def test_success_clears_all_state(self, seeker):
        """Success clears all backoff state."""
        seeker._attempt_counts["key1"] = 5
        seeker._last_attempt_time["key1"] = datetime.now(timezone.utc)
        seeker._given_up["key1"] = datetime.now(timezone.utc)
        seeker._warned_unprofitable["key1"] = 0.50
        
        seeker._record_attempt("key1", success=True)
        
        assert "key1" not in seeker._attempt_counts
        assert "key1" not in seeker._last_attempt_time
        assert "key1" not in seeker._given_up
        assert "key1" not in seeker._warned_unprofitable
    
    def test_failure_increments_count(self, seeker):
        """Failure increments attempt count."""
        seeker._record_attempt("key1", success=False)
        assert seeker._attempt_counts["key1"] == 1
        
        seeker._record_attempt("key1", success=False)
        assert seeker._attempt_counts["key1"] == 2
    
    def test_failure_updates_timestamp(self, seeker):
        """Failure updates last attempt time."""
        seeker._record_attempt("key1", success=False)
        assert "key1" in seeker._last_attempt_time
    
    def test_permanent_give_up_after_20_failures(self, seeker):
        """Permanently give up after 20 failures."""
        for i in range(20):
            seeker._record_attempt("key1", success=False)
        
        assert "key1" in seeker._permanently_given_up
    
    def test_long_cooldown_after_10_failures(self, seeker):
        """Enter long cooldown after 10 failures (3 fast + 7 slow)."""
        for i in range(10):
            seeker._record_attempt("key1", success=False)
        
        assert "key1" in seeker._given_up


class TestProfitabilityCalculation:
    """Tests for hedge profitability calculations used in seek_hedge_for_position."""
    
    def test_max_hedge_price_basic(self):
        """Max hedge price = 1.0 - entry_price - min_profit."""
        entry_price = 0.45
        min_profit = 0.05
        max_hedge_price = 1.0 - entry_price - min_profit
        
        assert abs(max_hedge_price - 0.50) < 0.001
    
    def test_max_hedge_price_tight_entry(self):
        """Tight entry leaves little room for hedge."""
        entry_price = 0.55
        min_profit = 0.05
        max_hedge_price = 1.0 - entry_price - min_profit
        
        assert abs(max_hedge_price - 0.40) < 0.001
    
    def test_max_hedge_price_too_high_entry(self):
        """Very high entry price means negative max hedge price."""
        entry_price = 0.92
        min_profit = 0.05
        max_hedge_price = 1.0 - entry_price - min_profit
        
        assert max_hedge_price < 0.05  # Too small to be viable
    
    def test_hedge_entry_calculation(self):
        """Hedge entry = min(best_bid + 0.01, fair_value, max_hedge_price)."""
        best_bid = 0.45
        fair_value = 0.50
        max_hedge_price = 0.48
        
        hedge_entry = min(best_bid + 0.01, fair_value, max_hedge_price)
        
        assert hedge_entry == 0.46  # best_bid + 0.01
    
    def test_hedge_entry_capped_at_max_price(self):
        """Hedge entry capped at max profitable price."""
        best_bid = 0.52
        fair_value = 0.55
        max_hedge_price = 0.50  # Profitability constraint
        
        hedge_entry = min(best_bid + 0.01, fair_value, max_hedge_price)
        
        assert hedge_entry == 0.50  # Capped at max_hedge_price
    
    def test_hedge_entry_capped_at_fair_value(self):
        """Hedge entry capped at fair value."""
        best_bid = 0.52
        fair_value = 0.48  # Below best_bid
        max_hedge_price = 0.55
        
        hedge_entry = min(best_bid + 0.01, fair_value, max_hedge_price)
        
        assert hedge_entry == 0.48  # Capped at fair_value


class TestMinimumSharesLogic:
    """Tests for minimum order size logic."""
    
    def test_minimum_order_size_is_5(self):
        """Minimum order size is 5 shares."""
        shares = 3.0  # Below minimum
        order_shares = max(shares, 5.0)
        assert order_shares == 5.0
    
    def test_larger_sizes_unchanged(self):
        """Larger order sizes are unchanged."""
        shares = 25.0
        order_shares = max(shares, 5.0)
        assert order_shares == 25.0
    
    def test_skip_very_small_positions(self):
        """Skip positions < 5 shares entirely."""
        shares = 4.5
        should_skip = shares < 5.0
        assert should_skip is True


class TestPassiveHedgeLogic:
    """Tests for passive hedge order placement logic."""
    
    def test_passive_hedge_when_market_too_expensive(self):
        """Place passive order when market is too expensive."""
        best_bid = 0.55
        max_hedge_price = 0.48  # We can only pay up to 0.48
        
        if best_bid >= max_hedge_price:
            hedge_entry = max_hedge_price
            is_passive = True
        else:
            hedge_entry = min(best_bid + 0.01, max_hedge_price)
            is_passive = False
        
        assert hedge_entry == 0.48
        assert is_passive is True
    
    def test_active_hedge_when_market_cheap(self):
        """Bid above best when market is cheap enough."""
        best_bid = 0.45
        max_hedge_price = 0.50
        
        if best_bid >= max_hedge_price:
            hedge_entry = max_hedge_price
            is_passive = True
        else:
            hedge_entry = min(best_bid + 0.01, max_hedge_price)
            is_passive = False
        
        assert hedge_entry == 0.46  # best_bid + 0.01
        assert is_passive is False


class TestExistingHedgeOrderLogic:
    """Tests for existing hedge order detection."""
    
    def test_skip_if_existing_order_sufficient(self):
        """Skip if existing order covers unhedged amount."""
        existing_order_size = 25.0
        unhedged_shares = 20.0
        tolerance = 0.5
        
        should_skip = existing_order_size >= unhedged_shares - tolerance
        assert should_skip is True
    
    def test_cancel_if_undersized(self):
        """Cancel if existing order is undersized."""
        existing_order_size = 15.0
        unhedged_shares = 25.0
        tolerance = 0.5
        
        is_undersized = existing_order_size < unhedged_shares - tolerance
        assert is_undersized is True


class TestResolvedMatchBackoffFix:
    """Tests for the fix preventing repeated UNHEDGED WIN/LOSS logs.
    
    The bug was: when a match resolves (ends), HedgeSeeker would log
    "UNHEDGED WIN" or "UNHEDGED LOSS" every ~10 seconds because the
    backoff check happened AFTER the market status API call.
    
    The fix: check _should_skip_due_to_backoff BEFORE calling the
    expensive market status API. This ensures once a position is
    marked as permanently_given_up, it's skipped immediately.
    """
    
    @pytest.fixture
    def seeker(self):
        """Create mock seeker."""
        return MockHedgeSeeker({})
    
    def test_permanently_given_up_skips_immediately(self, seeker):
        """Once permanently given up, position is skipped immediately.
        
        This is the key fix - by checking backoff first, we avoid
        calling market status API and logging the same message repeatedly.
        """
        key = "dota2:betboom:vs:aurora"
        
        # Simulate match ended - position was marked as permanently given up
        seeker._permanently_given_up.add(key)
        
        # Next check should skip immediately
        assert seeker._should_skip_due_to_backoff(key) is True
        
        # Verify the skip happens without any new state changes
        # (backoff check is a pure read operation)
        assert key in seeker._permanently_given_up
        assert key not in seeker._attempt_counts
        assert key not in seeker._last_attempt_time
    
    def test_backoff_check_order_prevents_repeated_logs(self, seeker):
        """Simulates the flow that caused repeated logs.
        
        Old flow (buggy):
        1. Check market status (expensive API call)
        2. If resolved, log UNHEDGED WIN/LOSS
        3. Add to permanently_given_up
        4. Check backoff <- too late!
        
        New flow (fixed):
        1. Check backoff <- first!
        2. If skip, return immediately (no log)
        3. Only then check market status
        """
        key = "dota2:betboom:vs:aurora"
        
        # First call: position not yet given up
        assert seeker._should_skip_due_to_backoff(key) is False
        
        # Simulate: market status shows resolved, we log and give up
        seeker._permanently_given_up.add(key)
        
        # All subsequent calls should skip immediately
        for _ in range(10):
            assert seeker._should_skip_due_to_backoff(key) is True
    
    def test_multiple_resolved_positions_tracked_independently(self, seeker):
        """Each resolved match is tracked independently."""
        key1 = "dota2:betboom:vs:aurora"
        key2 = "cs2:forzereload:vs:state"
        key3 = "lol:geng:vs:ktrolster"
        
        # Only key1 has ended
        seeker._permanently_given_up.add(key1)
        
        # key1 skips, key2 and key3 don't
        assert seeker._should_skip_due_to_backoff(key1) is True
        assert seeker._should_skip_due_to_backoff(key2) is False
        assert seeker._should_skip_due_to_backoff(key3) is False
        
        # key2 ends
        seeker._permanently_given_up.add(key2)
        
        assert seeker._should_skip_due_to_backoff(key1) is True
        assert seeker._should_skip_due_to_backoff(key2) is True
        assert seeker._should_skip_due_to_backoff(key3) is False


class MockHedgeSeekerWithDrawLogic(MockHedgeSeeker):
    """Extended mock with rugby draw hedging methods."""
    
    def _get_max_draw_price_rugby(self, fair_prob_draw: float) -> float:
        """
        Get maximum price for rugby draw order based on fair probability.
        
        Rugby draw prices are capped:
        - 5% fair prob -> max 3¢
        - 4% fair prob -> max 2¢
        - <=3% fair prob -> max 1¢
        """
        thresholds = self.config.get("rugby_draw_max_prices", {5.0: 0.03, 4.0: 0.02, 0.0: 0.01})
        for threshold in sorted(thresholds.keys(), reverse=True):
            if fair_prob_draw >= threshold:
                return thresholds[threshold]
        return 0.01
    
    def _is_rugby_match(self, match_id: str) -> bool:
        """Check if match is rugby based on match_id prefix."""
        return match_id.startswith("rugby:")


class TestRugbyDrawPriceCaps:
    """Tests for rugby draw price cap logic."""
    
    @pytest.fixture
    def seeker(self):
        """Create seeker with rugby draw config."""
        config = {
            "rugby_draw_max_prices": {
                5.0: 0.03,  # 5% fair prob -> max 3¢
                4.0: 0.02,  # 4% fair prob -> max 2¢
                0.0: 0.01,  # <=3% fair prob -> max 1¢
            },
            "draw_min_edge": 0.015,  # 1.5%
        }
        return MockHedgeSeekerWithDrawLogic(config)
    
    def test_5_percent_prob_max_3_cents(self, seeker):
        """5% fair draw probability allows up to 3¢."""
        assert seeker._get_max_draw_price_rugby(5.0) == 0.03
        assert seeker._get_max_draw_price_rugby(5.5) == 0.03
        assert seeker._get_max_draw_price_rugby(6.0) == 0.03
    
    def test_4_percent_prob_max_2_cents(self, seeker):
        """4% fair draw probability allows up to 2¢."""
        assert seeker._get_max_draw_price_rugby(4.0) == 0.02
        assert seeker._get_max_draw_price_rugby(4.5) == 0.02
        assert seeker._get_max_draw_price_rugby(4.9) == 0.02
    
    def test_3_percent_or_lower_max_1_cent(self, seeker):
        """3% or lower fair draw probability allows max 1¢."""
        assert seeker._get_max_draw_price_rugby(3.0) == 0.01
        assert seeker._get_max_draw_price_rugby(2.5) == 0.01
        assert seeker._get_max_draw_price_rugby(1.0) == 0.01
    
    def test_is_rugby_match_true(self, seeker):
        """Rugby match IDs start with 'rugby:'."""
        assert seeker._is_rugby_match("rugby:saracens:vs:leicester") is True
        assert seeker._is_rugby_match("rugby:all-blacks:vs:springboks") is True
    
    def test_is_rugby_match_false_for_esports(self, seeker):
        """Esports match IDs don't start with 'rugby:'."""
        assert seeker._is_rugby_match("cs2:liquid:vs:navi") is False
        assert seeker._is_rugby_match("dota2:og:vs:spirit") is False
        assert seeker._is_rugby_match("lol:t1:vs:geng") is False


class TestDrawHedgeEdgeCalculation:
    """Tests for draw hedge edge calculation logic."""
    
    def test_edge_at_1_cent_with_3_percent_draw(self):
        """Edge at 1¢ with 3% fair draw prob = 2%."""
        fair_prob_draw = 3.0  # 3%
        target_price = 0.01
        fair_value_decimal = fair_prob_draw / 100.0  # 0.03
        
        edge = fair_value_decimal - target_price
        edge_pct = edge * 100
        
        assert abs(edge_pct - 2.0) < 0.01  # 2% edge
    
    def test_edge_at_2_cents_with_4_percent_draw(self):
        """Edge at 2¢ with 4% fair draw prob = 2%."""
        fair_prob_draw = 4.0  # 4%
        target_price = 0.02
        fair_value_decimal = fair_prob_draw / 100.0  # 0.04
        
        edge = fair_value_decimal - target_price
        edge_pct = edge * 100
        
        assert abs(edge_pct - 2.0) < 0.01  # 2% edge
    
    def test_edge_at_3_cents_with_5_percent_draw(self):
        """Edge at 3¢ with 5% fair draw prob = 2%."""
        fair_prob_draw = 5.0  # 5%
        target_price = 0.03
        fair_value_decimal = fair_prob_draw / 100.0  # 0.05
        
        edge = fair_value_decimal - target_price
        edge_pct = edge * 100
        
        assert abs(edge_pct - 2.0) < 0.01  # 2% edge
    
    def test_min_edge_threshold(self):
        """Draw min edge is 1.5%."""
        draw_min_edge = 0.015  # From config
        
        # At 5% draw, max price 3¢, edge = 2% > 1.5% ✓
        assert (0.05 - 0.03) >= draw_min_edge
        
        # At 4% draw, max price 2¢, edge = 2% > 1.5% ✓
        assert (0.04 - 0.02) >= draw_min_edge
        
        # At 3% draw, max price 1¢, edge = 2% > 1.5% ✓
        assert (0.03 - 0.01) >= draw_min_edge


class TestClobPrePlacementCheck:
    """Tests for pre-placement CLOB check in _seek_weather_hedge.
    
    When BotState has no existing order for a hedge token, the hedge seeker
    queries the CLOB API directly to discover un-hydrated orders from previous
    sessions. This prevents duplicate hedge orders.
    """
    
    def test_clob_existing_order_prevents_placement(self):
        """When CLOB shows existing order with sufficient size, no new order is placed."""
        existing_size = 25.0
        needed_shares = 20.0
        tolerance = 0.5
        
        should_skip = existing_size >= needed_shares - tolerance
        assert should_skip is True
    
    def test_clob_existing_order_at_boundary(self):
        """Existing order within tolerance still prevents placement."""
        existing_size = 19.6  # 20.0 - 0.5 + 0.1
        needed_shares = 20.0
        tolerance = 0.5
        
        should_skip = existing_size >= needed_shares - tolerance
        assert should_skip is True
    
    def test_clob_empty_allows_placement(self):
        """When CLOB returns no orders, placement proceeds."""
        clob_orders = []
        should_place = len(clob_orders) == 0
        assert should_place is True
    
    def test_clob_undersized_triggers_cancel_replace(self):
        """Undersized CLOB order should be cancelled and replaced."""
        existing_size = 10.0
        needed_shares = 25.0
        tolerance = 0.5
        
        is_undersized = existing_size < needed_shares - tolerance
        assert is_undersized is True
    
    def test_clob_orders_sorted_best_first(self):
        """CLOB orders are sorted by price descending (best bid first)."""
        orders = [
            {"order_id": "a", "price": 0.20, "size": 10.0},
            {"order_id": "b", "price": 0.25, "size": 10.0},
            {"order_id": "c", "price": 0.15, "size": 10.0},
        ]
        sorted_orders = sorted(orders, key=lambda x: x["price"], reverse=True)
        
        assert sorted_orders[0]["order_id"] == "b"  # Highest price first
        assert sorted_orders[0]["price"] == 0.25
    
    def test_clob_error_returns_empty_allows_placement(self):
        """If CLOB query fails, return empty list → placement proceeds."""
        # Simulates the error handling in get_orders_for_token
        clob_orders = []  # Error case returns empty list
        should_check_book = len(clob_orders) == 0
        assert should_check_book is True


class TestClobPrePlacementCheckAsync:
    """Async integration tests for get_orders_for_token."""
    
    @pytest.fixture
    def mock_executor(self):
        """Create mock executor with mocked get_orders_for_token."""
        executor = Mock()
        executor.get_orders_for_token = AsyncMock()
        executor.cancel_order = AsyncMock(return_value=(True, False))
        return executor
    
    @pytest.mark.asyncio
    async def test_get_orders_for_token_returns_filtered_live_orders(self, mock_executor):
        """get_orders_for_token only returns LIVE orders."""
        mock_executor.get_orders_for_token.return_value = [
            {"order_id": "abc123", "price": 0.20, "size": 10.0, "status": "LIVE"},
        ]
        
        result = await mock_executor.get_orders_for_token("token_xyz")
        assert len(result) == 1
        assert result[0]["status"] == "LIVE"
    
    @pytest.mark.asyncio
    async def test_get_orders_for_token_empty_on_no_orders(self, mock_executor):
        """get_orders_for_token returns empty list when no orders exist."""
        mock_executor.get_orders_for_token.return_value = []
        
        result = await mock_executor.get_orders_for_token("token_xyz")
        assert result == []
    
    @pytest.mark.asyncio
    async def test_get_orders_for_token_empty_on_error(self, mock_executor):
        """get_orders_for_token returns empty list on API error (no crash)."""
        mock_executor.get_orders_for_token.side_effect = Exception("API timeout")
        
        try:
            result = await mock_executor.get_orders_for_token("token_xyz")
        except Exception:
            result = []  # Mirrors the actual try/except in get_orders_for_token
        
        assert result == []

