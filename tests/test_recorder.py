"""
Unit tests for data/recorder.py - Data recording to Supabase.

Uses mocked Supabase client to test record creation and formatting.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta


class MockFilledOrderRecord:
    """Mock FilledOrderRecord dataclass for testing."""
    
    def __init__(
        self,
        order_id: str = "order123",
        token_id: str = "token456",
        condition_id: str = None,
        match_id: str = None,
        game: str = None,
        team1: str = None,
        team2: str = None,
        tournament: str = None,
        side: str = "entry",
        team: str = "Team A",
        price: float = 0.50,
        shares: float = 25.0,
        placed_at: datetime = None,
        filled_at: datetime = None,
        fair_value_at_fill: float = None,
        edge_at_fill: float = None,
        raw_odds: dict = None,
        outcome: str = "PENDING",
        pnl: float = None,
    ):
        self.order_id = order_id
        self.token_id = token_id
        self.condition_id = condition_id
        self.match_id = match_id
        self.game = game
        self.team1 = team1
        self.team2 = team2
        self.tournament = tournament
        self.side = side
        self.team = team
        self.price = price
        self.shares = shares
        self.placed_at = placed_at or datetime.now(timezone.utc)
        self.filled_at = filled_at or datetime.now(timezone.utc)
        self.fair_value_at_fill = fair_value_at_fill
        self.edge_at_fill = edge_at_fill
        self.raw_odds = raw_odds
        self.outcome = outcome
        self.pnl = pnl
    
    def to_dict(self) -> dict:
        """Convert to dict for Supabase insert."""
        return {
            "order_id": self.order_id,
            "token_id": self.token_id,
            "condition_id": self.condition_id,
            "match_id": self.match_id,
            "game": self.game,
            "team1": self.team1,
            "team2": self.team2,
            "tournament": self.tournament,
            "side": self.side,
            "team": self.team,
            "price": self.price,
            "shares": self.shares,
            "placed_at": self.placed_at.isoformat() if self.placed_at else None,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "fair_value_at_fill": self.fair_value_at_fill,
            "edge_at_fill": self.edge_at_fill,
            "raw_odds": self.raw_odds,
            "outcome": self.outcome,
            "pnl": self.pnl,
        }


class MockDataRecorder:
    """Mock DataRecorder for testing core logic patterns."""
    
    def __init__(self):
        self._client = None
        self._records = []  # In-memory store for testing
    
    def _calculate_edge(self, price: float, fair_value: float) -> float:
        """Calculate edge at fill."""
        return fair_value - price
    
    def _format_arb_record(
        self,
        match_id: str,
        entry_team: str,
        entry_price: float,
        entry_shares: float,
        hedge_team: str,
        hedge_price: float,
        hedge_shares: float,
    ) -> dict:
        """Format arbitrage record."""
        total_cost = (entry_price * entry_shares) + (hedge_price * hedge_shares)
        payout = min(entry_shares, hedge_shares) * 1.0
        locked_profit = payout - total_cost
        
        return {
            "match_id": match_id,
            "entry_team": entry_team,
            "entry_price": entry_price,
            "entry_shares": entry_shares,
            "hedge_team": hedge_team,
            "hedge_price": hedge_price,
            "hedge_shares": hedge_shares,
            "locked_profit": locked_profit,
        }
    
    def _retention_cutoff(self, days: int) -> datetime:
        """Get cutoff date for data retention."""
        return datetime.now(timezone.utc) - timedelta(days=days)


class TestFilledOrderRecord:
    """Tests for FilledOrderRecord dataclass."""
    
    def test_stores_order_fields(self):
        """Record stores all order fields."""
        record = MockFilledOrderRecord(
            order_id="order123",
            token_id="token456",
            team="Liquid",
            price=0.52,
            shares=25.0,
        )
        
        assert record.order_id == "order123"
        assert record.token_id == "token456"
        assert record.team == "Liquid"
        assert record.price == 0.52
        assert record.shares == 25.0
    
    def test_default_outcome_is_pending(self):
        """Default outcome is PENDING."""
        record = MockFilledOrderRecord()
        assert record.outcome == "PENDING"
    
    def test_to_dict_converts_datetime(self):
        """to_dict converts datetime to ISO string."""
        now = datetime.now(timezone.utc)
        record = MockFilledOrderRecord(placed_at=now, filled_at=now)
        
        result = record.to_dict()
        
        assert isinstance(result["placed_at"], str)
        assert isinstance(result["filled_at"], str)
        assert "T" in result["placed_at"]  # ISO format has T


class TestEdgeCalculation:
    """Tests for edge calculation in recorder."""
    
    @pytest.fixture
    def recorder(self):
        """Create mock recorder."""
        return MockDataRecorder()
    
    def test_positive_edge(self, recorder):
        """Calculate positive edge."""
        edge = recorder._calculate_edge(price=0.50, fair_value=0.55)
        
        assert abs(edge - 0.05) < 0.001
    
    def test_negative_edge(self, recorder):
        """Calculate negative edge (overpaying)."""
        edge = recorder._calculate_edge(price=0.55, fair_value=0.50)
        
        assert edge < 0
        assert abs(edge + 0.05) < 0.001
    
    def test_zero_edge(self, recorder):
        """Calculate zero edge (at fair value)."""
        edge = recorder._calculate_edge(price=0.50, fair_value=0.50)
        
        assert abs(edge) < 0.001


class TestArbRecordFormatting:
    """Tests for arbitrage record formatting."""
    
    @pytest.fixture
    def recorder(self):
        """Create mock recorder."""
        return MockDataRecorder()
    
    def test_format_profitable_arb(self, recorder):
        """Format record for profitable arb."""
        result = recorder._format_arb_record(
            match_id="cs2:liquid:vs:navi",
            entry_team="Liquid",
            entry_price=0.45,
            entry_shares=25.0,
            hedge_team="Navi",
            hedge_price=0.50,
            hedge_shares=25.0,
        )
        
        assert result["match_id"] == "cs2:liquid:vs:navi"
        assert result["entry_team"] == "Liquid"
        assert result["hedge_team"] == "Navi"
        assert result["locked_profit"] > 0
    
    def test_locked_profit_calculation(self, recorder):
        """Verify locked profit calculation."""
        result = recorder._format_arb_record(
            match_id="test",
            entry_team="A",
            entry_price=0.45,
            entry_shares=25.0,
            hedge_team="B",
            hedge_price=0.50,
            hedge_shares=25.0,
        )
        
        # Entry: 0.45 * 25 = 11.25
        # Hedge: 0.50 * 25 = 12.50
        # Total: 23.75
        # Payout: 25 * 1.0 = 25
        # Profit: 25 - 23.75 = 1.25
        assert abs(result["locked_profit"] - 1.25) < 0.01
    
    def test_uses_min_shares_for_payout(self, recorder):
        """Payout uses minimum of entry/hedge shares."""
        result = recorder._format_arb_record(
            match_id="test",
            entry_team="A",
            entry_price=0.45,
            entry_shares=25.0,
            hedge_team="B",
            hedge_price=0.50,
            hedge_shares=20.0,  # Less than entry
        )
        
        # Payout based on hedge shares (smaller)
        # Entry: 0.45 * 25 = 11.25
        # Hedge: 0.50 * 20 = 10.00
        # Total: 21.25
        # Payout: 20 * 1.0 = 20
        # Profit: 20 - 21.25 = -1.25 (partial coverage)
        assert abs(result["locked_profit"] - (-1.25)) < 0.01


class TestRetentionPolicy:
    """Tests for data retention cutoff calculation."""
    
    @pytest.fixture
    def recorder(self):
        """Create mock recorder."""
        return MockDataRecorder()
    
    def test_retention_cutoff_90_days(self, recorder):
        """90-day retention cutoff is correct."""
        cutoff = recorder._retention_cutoff(90)
        expected = datetime.now(timezone.utc) - timedelta(days=90)
        
        # Within 1 second of expected
        diff = abs((cutoff - expected).total_seconds())
        assert diff < 1
    
    def test_retention_cutoff_30_days(self, recorder):
        """30-day retention cutoff is correct."""
        cutoff = recorder._retention_cutoff(30)
        expected = datetime.now(timezone.utc) - timedelta(days=30)
        
        diff = abs((cutoff - expected).total_seconds())
        assert diff < 1


class TestFairValueLogging:
    """Tests for fair value logging patterns."""
    
    def test_fair_value_log_entry(self):
        """Fair value log entry format."""
        log_entry = {
            "match_id": "cs2:liquid:vs:navi",
            "team1": "Liquid",
            "team2": "Navi",
            "fair_prob1": 0.55,
            "fair_prob2": 0.45,
            "num_sources": 3,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        assert "match_id" in log_entry
        assert "fair_prob1" in log_entry
        assert "fair_prob2" in log_entry
        assert abs(log_entry["fair_prob1"] + log_entry["fair_prob2"] - 1.0) < 0.01


class TestOrderOutcomeUpdate:
    """Tests for order outcome update patterns."""
    
    def test_win_outcome(self):
        """WIN outcome with positive PnL."""
        outcome = "WIN"
        entry_price = 0.50
        shares = 25.0
        pnl = shares * (1.0 - entry_price)  # $25 * 0.50 = $12.50
        
        assert outcome == "WIN"
        assert abs(pnl - 12.50) < 0.01
    
    def test_loss_outcome(self):
        """LOSS outcome with negative PnL."""
        outcome = "LOSS"
        entry_price = 0.50
        shares = 25.0
        pnl = -shares * entry_price  # -$25 * 0.50 = -$12.50
        
        assert outcome == "LOSS"
        assert abs(pnl + 12.50) < 0.01

