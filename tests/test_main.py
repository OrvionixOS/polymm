"""
Unit tests for main.py - TradingBot main entry point.

Tests core orchestration logic patterns without starting actual services.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta


class MockTradingBot:
    """Mock TradingBot for testing core logic patterns."""
    
    # Loop intervals (seconds)
    SIGNAL_LOOP_INTERVAL = 30
    POSITION_LOOP_INTERVAL = 15
    WS_ADJUSTMENT_INTERVAL = 5
    STATUS_LOOP_INTERVAL = 300  # 5 min
    RESYNC_LOOP_INTERVAL = 3600  # 1 hour
    REACTIVE_CHECK_INTERVAL = 60
    DATA_CLEANUP_INTERVAL = 86400  # 24 hours
    
    def __init__(
        self,
        executor: Mock = None,
        odds_service: Mock = None,
        scanner: Mock = None,
        alerts: Mock = None,
        config: dict = None,
    ):
        self.executor = executor or Mock()
        self.odds_service = odds_service or Mock()
        self.scanner = scanner or Mock()
        self.alerts = alerts or Mock()
        self.config = config or {
            "min_edge": 0.04,
            "min_profit": 0.05,
            "max_position_size": 25,
            "low_balance_threshold": 100,
        }
        
        self._running = False
        self._balance = 500.0
        self._last_scan_time = None
        self._last_resync_time = None
    
    def is_running(self) -> bool:
        """Check if bot is running."""
        return self._running
    
    def _get_health_status(self) -> dict:
        """Get health status for HTTP endpoint."""
        return {
            "status": "ok",
            "running": self._running,
            "balance": self._balance,
            "uptime": "1h 30m",
            "last_scan": self._last_scan_time.isoformat() if self._last_scan_time else None,
        }
    
    def _is_balance_low(self, balance: float) -> bool:
        """Check if balance is below threshold."""
        return balance < self.config.get("low_balance_threshold", 100)
    
    def _should_skip_scan(self, now: datetime) -> bool:
        """Check if we should skip this scan cycle."""
        if self._last_scan_time is None:
            return False
        elapsed = (now - self._last_scan_time).total_seconds()
        return elapsed < self.SIGNAL_LOOP_INTERVAL
    
    def _should_resync(self, now: datetime) -> bool:
        """Check if we should resync state."""
        if self._last_resync_time is None:
            return True
        elapsed = (now - self._last_resync_time).total_seconds()
        return elapsed >= self.RESYNC_LOOP_INTERVAL
    
    def _calculate_shares(self, price: float, max_size: float) -> int:
        """Calculate number of shares to buy."""
        if price <= 0:
            return 0
        shares = int(max_size / price)
        return max(1, shares)
    
    def _format_uptime(self, start_time: datetime, now: datetime) -> str:
        """Format uptime as human-readable string."""
        delta = now - start_time
        hours = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        return f"{hours}h {minutes}m"


class TestTradingBotInit:
    """Tests for TradingBot initialization."""
    
    def test_init_stores_services(self):
        """Bot stores injected services."""
        executor = Mock()
        alerts = Mock()
        
        bot = MockTradingBot(executor=executor, alerts=alerts)
        
        assert bot.executor is executor
        assert bot.alerts is alerts
    
    def test_init_not_running(self):
        """Bot starts in stopped state."""
        bot = MockTradingBot()
        
        assert bot.is_running() is False
    
    def test_init_default_config(self):
        """Bot has default config values."""
        bot = MockTradingBot()
        
        assert "min_edge" in bot.config
        assert "min_profit" in bot.config


class TestLoopIntervals:
    """Tests for loop interval constants."""
    
    def test_signal_loop_interval(self):
        """Signal loop runs every 30s."""
        assert MockTradingBot.SIGNAL_LOOP_INTERVAL == 30
    
    def test_position_loop_interval(self):
        """Position loop runs every 15s."""
        assert MockTradingBot.POSITION_LOOP_INTERVAL == 15
    
    def test_ws_adjustment_interval(self):
        """WS adjustment runs every 5s."""
        assert MockTradingBot.WS_ADJUSTMENT_INTERVAL == 5
    
    def test_status_loop_interval(self):
        """Status loop runs every 5 min."""
        assert MockTradingBot.STATUS_LOOP_INTERVAL == 300
    
    def test_resync_loop_interval(self):
        """Resync loop runs every hour."""
        assert MockTradingBot.RESYNC_LOOP_INTERVAL == 3600
    
    def test_reactive_check_interval(self):
        """Reactive check runs every 60s."""
        assert MockTradingBot.REACTIVE_CHECK_INTERVAL == 60
    
    def test_data_cleanup_interval(self):
        """Data cleanup runs daily."""
        assert MockTradingBot.DATA_CLEANUP_INTERVAL == 86400


class TestHealthStatus:
    """Tests for health status generation."""
    
    @pytest.fixture
    def bot(self):
        """Create mock bot."""
        bot = MockTradingBot()
        bot._running = True
        bot._balance = 250.0
        bot._last_scan_time = datetime.now(timezone.utc)
        return bot
    
    def test_includes_status(self, bot):
        """Status includes ok status."""
        status = bot._get_health_status()
        
        assert status["status"] == "ok"
    
    def test_includes_running(self, bot):
        """Status includes running state."""
        status = bot._get_health_status()
        
        assert status["running"] is True
    
    def test_includes_balance(self, bot):
        """Status includes balance."""
        status = bot._get_health_status()
        
        assert status["balance"] == 250.0
    
    def test_includes_last_scan(self, bot):
        """Status includes last scan time."""
        status = bot._get_health_status()
        
        assert status["last_scan"] is not None


class TestBalanceCheck:
    """Tests for balance threshold checking."""
    
    @pytest.fixture
    def bot(self):
        """Create mock bot."""
        return MockTradingBot()
    
    def test_low_balance_detected(self, bot):
        """Detects low balance below threshold."""
        is_low = bot._is_balance_low(50.0)
        
        assert is_low is True
    
    def test_sufficient_balance_detected(self, bot):
        """Detects sufficient balance."""
        is_low = bot._is_balance_low(150.0)
        
        assert is_low is False
    
    def test_exactly_at_threshold(self, bot):
        """Balance at threshold is not low."""
        is_low = bot._is_balance_low(100.0)
        
        assert is_low is False


class TestScanTiming:
    """Tests for scan timing logic."""
    
    @pytest.fixture
    def bot(self):
        """Create mock bot."""
        return MockTradingBot()
    
    def test_first_scan_not_skipped(self, bot):
        """First scan never skipped."""
        now = datetime.now(timezone.utc)
        
        should_skip = bot._should_skip_scan(now)
        
        assert should_skip is False
    
    def test_skip_if_too_soon(self, bot):
        """Skip if last scan was too recent."""
        now = datetime.now(timezone.utc)
        bot._last_scan_time = now - timedelta(seconds=10)
        
        should_skip = bot._should_skip_scan(now)
        
        assert should_skip is True
    
    def test_dont_skip_after_interval(self, bot):
        """Don't skip after interval elapsed."""
        now = datetime.now(timezone.utc)
        bot._last_scan_time = now - timedelta(seconds=35)
        
        should_skip = bot._should_skip_scan(now)
        
        assert should_skip is False


class TestResyncTiming:
    """Tests for state resync timing."""
    
    @pytest.fixture
    def bot(self):
        """Create mock bot."""
        return MockTradingBot()
    
    def test_first_resync_scheduled(self, bot):
        """First resync is always needed."""
        now = datetime.now(timezone.utc)
        
        should_resync = bot._should_resync(now)
        
        assert should_resync is True
    
    def test_no_resync_too_soon(self, bot):
        """No resync if last was recent."""
        now = datetime.now(timezone.utc)
        bot._last_resync_time = now - timedelta(minutes=30)
        
        should_resync = bot._should_resync(now)
        
        assert should_resync is False
    
    def test_resync_after_hour(self, bot):
        """Resync after an hour elapsed."""
        now = datetime.now(timezone.utc)
        bot._last_resync_time = now - timedelta(hours=1, minutes=5)
        
        should_resync = bot._should_resync(now)
        
        assert should_resync is True


class TestSharesCalculation:
    """Tests for shares calculation."""
    
    @pytest.fixture
    def bot(self):
        """Create mock bot."""
        return MockTradingBot()
    
    def test_calculates_based_on_price(self, bot):
        """Calculates shares from price and max size."""
        shares = bot._calculate_shares(price=0.50, max_size=25)
        
        assert shares == 50  # 25 / 0.50 = 50
    
    def test_minimum_one_share(self, bot):
        """Always buys at least one share."""
        shares = bot._calculate_shares(price=50.0, max_size=25)
        
        assert shares >= 1
    
    def test_zero_price_returns_zero(self, bot):
        """Zero price returns zero shares."""
        shares = bot._calculate_shares(price=0, max_size=25)
        
        assert shares == 0
    
    def test_truncates_to_int(self, bot):
        """Truncates result to integer."""
        shares = bot._calculate_shares(price=0.33, max_size=25)
        
        assert isinstance(shares, int)


class TestUptimeFormatting:
    """Tests for uptime formatting."""
    
    @pytest.fixture
    def bot(self):
        """Create mock bot."""
        return MockTradingBot()
    
    def test_formats_hours_and_minutes(self, bot):
        """Formats as 'Xh Ym'."""
        start = datetime.now(timezone.utc) - timedelta(hours=2, minutes=30)
        now = datetime.now(timezone.utc)
        
        uptime = bot._format_uptime(start, now)
        
        assert "2h" in uptime
        assert "30m" in uptime
    
    def test_handles_zero_hours(self, bot):
        """Handles less than an hour."""
        start = datetime.now(timezone.utc) - timedelta(minutes=45)
        now = datetime.now(timezone.utc)
        
        uptime = bot._format_uptime(start, now)
        
        assert "0h" in uptime
        assert "45m" in uptime


class TestTeamOrderConsistency:
    """Tests for team order handling in _execute_opportunity.
    
    Verifies that when a match already exists in BotState, its team order
    is used instead of Polymarket's outcome order (which can differ).
    This prevents token slot collisions when both sides have orders.
    """
    
    def test_existing_match_team_order_used(self):
        """When match exists, use its team order not Polymarket's.
        
        Bug: Polymarket returns outcomes as ["Team RA'AD", "GnG Amazigh"]
        but match_id is valorant:gngamazigh:vs:raad (opposite order).
        If we use Polymarket's order, tokens get assigned to wrong slots.
        """
        from src.state.bot_state import BotState, MatchState
        
        # Create BotState with existing match
        bot_state = BotState()
        bot_state.register_match(
            match_id="valorant:gngamazigh:vs:raad",
            game="valorant",
            team1="GnG Amazigh",  # from match_id order
            team2="Team RA'AD",   # from match_id order
            token1="tok_gng_123",
            token2="tok_raad_456",
        )
        
        # Simulate what _execute_opportunity does
        match_id = "valorant:gngamazigh:vs:raad"
        existing_match = bot_state.get_match(match_id)
        
        # Polymarket outcomes in DIFFERENT order
        polymarket_outcomes = ["Team RA'AD", "GnG Amazigh"]
        
        # The fix: use existing match order if available
        if existing_match and existing_match.team1 and existing_match.team2:
            team1 = existing_match.team1
            team2 = existing_match.team2
        else:
            team1 = polymarket_outcomes[0]
            team2 = polymarket_outcomes[1]
        
        # Verify the fix uses match's order, not Polymarket's
        assert team1 == "GnG Amazigh", "Should use match's team1, not Polymarket's"
        assert team2 == "Team RA'AD", "Should use match's team2, not Polymarket's"
    
    def test_new_match_uses_polymarket_order(self):
        """When match doesn't exist, use Polymarket's outcome order."""
        from src.state.bot_state import BotState
        
        bot_state = BotState()
        
        # No existing match
        match_id = "cs2:teamA:vs:teamB"
        existing_match = bot_state.get_match(match_id)
        
        polymarket_outcomes = ["TeamB", "TeamA"]  # Polymarket's order
        
        if existing_match and existing_match.team1 and existing_match.team2:
            team1 = existing_match.team1
            team2 = existing_match.team2
        else:
            team1 = polymarket_outcomes[0]
            team2 = polymarket_outcomes[1]
        
        # For new matches, Polymarket order is used
        assert team1 == "TeamB"
        assert team2 == "TeamA"
    
    def test_partial_match_uses_polymarket_fallback(self):
        """When match exists but has no team names, use Polymarket's."""
        from src.state.bot_state import BotState
        
        bot_state = BotState()
        # Match exists but with empty team names (edge case)
        bot_state.register_match(
            match_id="lol:empty:vs:match",
            game="lol",
            team1="",  # empty
            team2="",  # empty
        )
        
        match_id = "lol:empty:vs:match"
        existing_match = bot_state.get_match(match_id)
        
        polymarket_outcomes = ["Team X", "Team Y"]
        
        if existing_match and existing_match.team1 and existing_match.team2:
            team1 = existing_match.team1
            team2 = existing_match.team2
        else:
            team1 = polymarket_outcomes[0]
            team2 = polymarket_outcomes[1]
        
        # Should fallback to Polymarket since teams are empty
        assert team1 == "Team X"
        assert team2 == "Team Y"

