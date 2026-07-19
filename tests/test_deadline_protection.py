"""
Tests for market deadline protection logic.

Tests the _is_past_deadline() helper and client-level filtering
for stocks, mentions, and weather markets.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from src.scanning.spread_scanner import SpreadOpportunity


# ===== Helper to create SpreadOpportunity with deadline fields =====

def make_opp(
    market_type: str = "weather",
    game_start_time=None,
    end_date=None,
    city: str = "",
) -> SpreadOpportunity:
    """Create a minimal SpreadOpportunity for deadline testing."""
    return SpreadOpportunity(
        event_id="test-event",
        event_title="Test Event",
        city=city,
        date_str="",
        market_id="test-market",
        condition_id="test-condition",
        question="Test?",
        bin_label="",
        token_id="token-yes",
        no_token_id="token-no",
        best_bid=0.40,
        best_ask=0.60,
        spread=0.20,
        spread_cents=20.0,
        mid_price=0.50,
        volume=1000.0,
        liquidity=500.0,
        market_type=market_type,
        game_start_time=game_start_time,
        end_date=end_date,
    )


# ===== _is_past_deadline tests =====

class TestIsPastDeadline:
    """Test the _is_past_deadline method on SpreadBot."""
    
    @pytest.fixture
    def bot(self):
        """Create a SpreadBot instance with mocked dependencies."""
        with patch("src.bots.spread_bot.SpreadBot.__init__", return_value=None):
            from src.bots.spread_bot import SpreadBot
            bot = SpreadBot()
            return bot
    
    # === Sports / Esports / Tennis / Basketball ===
    
    def test_sports_game_started(self, bot):
        """Game started 1 hour ago → past deadline."""
        opp = make_opp(
            market_type="sports",
            game_start_time=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert bot._is_past_deadline(opp) is True
    
    def test_sports_game_not_started(self, bot):
        """Game starts in 2 hours → NOT past deadline."""
        opp = make_opp(
            market_type="sports",
            game_start_time=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        assert bot._is_past_deadline(opp) is False
    
    def test_sports_no_game_start_time(self, bot):
        """No game_start_time → NOT past deadline."""
        opp = make_opp(market_type="sports")
        assert bot._is_past_deadline(opp) is False
    
    def test_sports_naive_datetime(self, bot):
        """Naive datetime (no tzinfo) should still work."""
        opp = make_opp(
            market_type="sports",
            game_start_time=datetime.utcnow() - timedelta(hours=1),
        )
        assert bot._is_past_deadline(opp) is True
    
    # === Mentions ===
    
    def test_mentions_event_started(self, bot):
        """Mentions event started (startTime = game_start_time) → past deadline."""
        opp = make_opp(
            market_type="mentions",
            game_start_time=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        assert bot._is_past_deadline(opp) is True
    
    def test_mentions_event_not_started(self, bot):
        """Mentions event not started yet → NOT past deadline."""
        opp = make_opp(
            market_type="mentions",
            game_start_time=datetime.now(timezone.utc) + timedelta(hours=3),
        )
        assert bot._is_past_deadline(opp) is False
    
    # === Stocks ===
    
    def test_stock_within_3h_of_close(self, bot):
        """Stock endDate 2h away → past deadline (within 3h threshold)."""
        opp = make_opp(
            market_type="stock",
            end_date=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        assert bot._is_past_deadline(opp) is True
    
    def test_stock_more_than_3h_from_close(self, bot):
        """Stock endDate 5h away → NOT past deadline."""
        opp = make_opp(
            market_type="stock",
            end_date=datetime.now(timezone.utc) + timedelta(hours=5),
        )
        assert bot._is_past_deadline(opp) is False
    
    def test_stock_exactly_3h_from_close(self, bot):
        """Stock endDate exactly 3h away → past deadline (≤ threshold)."""
        opp = make_opp(
            market_type="stock",
            end_date=datetime.now(timezone.utc) + timedelta(hours=3),
        )
        assert bot._is_past_deadline(opp) is True
    
    def test_stock_past_close(self, bot):
        """Stock endDate already passed → past deadline."""
        opp = make_opp(
            market_type="stock",
            end_date=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert bot._is_past_deadline(opp) is True
    
    def test_stock_no_end_date(self, bot):
        """Stock with no end_date → NOT past deadline."""
        opp = make_opp(market_type="stock")
        assert bot._is_past_deadline(opp) is False
    
    def test_stock_end_date_not_applied_to_weather(self, bot):
        """endDate check should NOT trigger for weather even if within 3h."""
        opp = make_opp(
            market_type="weather",
            end_date=datetime.now(timezone.utc) + timedelta(hours=2),
            city="nyc",
        )
        # Weather uses city cutoff, not end_date threshold
        # This should only trigger if city cutoff matches
        # Since we're testing isolation, check the stock-specific check doesn't fire
        # (city cutoff may or may not fire depending on current hour)
        opp_no_city = make_opp(
            market_type="weather",
            end_date=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        # No city → weather cutoff can't fire, and stock check won't fire for weather type
        assert bot._is_past_deadline(opp_no_city) is False
    
    # === Weather ===
    
    @patch("src.bots.spread_bot.SPREAD_CONFIG", {
        "deadline_stock_hours": 3,
        "weather_cutoff_utc": {"nyc": 18, "seattle": 21, "london": 13},
    })
    def test_weather_past_cutoff_on_event_day(self, bot):
        """Weather past city cutoff on event day → past deadline."""
        # Create opp for NYC (cutoff = 18 UTC)
        # end_date is noon next day, so event day = end_date - 1 day
        tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
        end_date = datetime.combine(tomorrow, datetime.min.time()).replace(
            hour=12, tzinfo=timezone.utc
        )
        
        opp = make_opp(
            market_type="weather",
            city="nyc",
            end_date=end_date,  # event day = today
        )
        
        # Mock current time to be 19:00 UTC (past NYC cutoff of 18)
        with patch("src.bots.spread_bot.datetime") as mock_dt:
            mock_now = datetime(
                datetime.now(timezone.utc).year,
                datetime.now(timezone.utc).month,
                datetime.now(timezone.utc).day,
                19, 0, tzinfo=timezone.utc,
            )
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = bot._is_past_deadline(opp)
        
        assert result is True
    
    @patch("src.bots.spread_bot.SPREAD_CONFIG", {
        "deadline_stock_hours": 3,
        "weather_cutoff_utc": {"nyc": 18, "seattle": 21, "london": 13},
    })
    def test_weather_before_cutoff_on_event_day(self, bot):
        """Weather before city cutoff on event day → NOT past deadline."""
        tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
        end_date = datetime.combine(tomorrow, datetime.min.time()).replace(
            hour=12, tzinfo=timezone.utc,
        )
        
        opp = make_opp(
            market_type="weather",
            city="nyc",
            end_date=end_date,
        )
        
        # Mock current time to be 15:00 UTC (before NYC cutoff of 18)
        with patch("src.bots.spread_bot.datetime") as mock_dt:
            mock_now = datetime(
                datetime.now(timezone.utc).year,
                datetime.now(timezone.utc).month,
                datetime.now(timezone.utc).day,
                15, 0, tzinfo=timezone.utc,
            )
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = bot._is_past_deadline(opp)
        
        assert result is False
    
    @patch("src.bots.spread_bot.SPREAD_CONFIG", {
        "deadline_stock_hours": 3,
        "weather_cutoff_utc": {"nyc": 18},
    })
    def test_weather_past_cutoff_day_before_event(self, bot):
        """Weather past city cutoff but day BEFORE event → NOT past deadline."""
        # end_date 2 days from now → event day is tomorrow, not today
        day_after_tomorrow = datetime.now(timezone.utc).date() + timedelta(days=2)
        end_date = datetime.combine(day_after_tomorrow, datetime.min.time()).replace(
            hour=12, tzinfo=timezone.utc,
        )
        
        opp = make_opp(
            market_type="weather",
            city="nyc",
            end_date=end_date,
        )
        
        # Even at 19 UTC (past cutoff), should NOT trigger because it's not event day
        with patch("src.bots.spread_bot.datetime") as mock_dt:
            mock_now = datetime(
                datetime.now(timezone.utc).year,
                datetime.now(timezone.utc).month,
                datetime.now(timezone.utc).day,
                19, 0, tzinfo=timezone.utc,
            )
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = bot._is_past_deadline(opp)
        
        assert result is False
    
    def test_weather_unknown_city(self, bot):
        """Weather with unknown city → NOT past deadline (no cutoff mapped)."""
        opp = make_opp(
            market_type="weather",
            city="mars",
            end_date=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert bot._is_past_deadline(opp) is False
    
    # === No fields set ===
    
    def test_no_deadline_fields(self, bot):
        """No deadline fields set → NOT past deadline."""
        opp = make_opp(market_type="weather")
        assert bot._is_past_deadline(opp) is False
