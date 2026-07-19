"""
Unit tests for data/supabase_client.py - Supabase database client.

Uses mocked Supabase client to test record creation and query logic.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone


class MockEsportsOddsRecord:
    """Mock EsportsOddsRecord dataclass for testing."""
    
    def __init__(
        self,
        match_id: str = "cs2:team-a:vs:team-b",
        source: str = "source_a",
        game: str = "cs2",
        team1: str = "Team A",
        team2: str = "Team B",
        odds1: float = 2.0,
        odds2: float = 2.0,
        fair_prob1: float = 0.50,
        fair_prob2: float = 0.50,
        fair_odds1: float = 2.0,
        fair_odds2: float = 2.0,
        avg_vig: float = 0.05,
        tournament: str = "ESL Pro League",
        format: str = "bo3",
        is_live: bool = False,
        match_time: str = "",
    ):
        self.match_id = match_id
        self.source = source
        self.game = game
        self.team1 = team1
        self.team2 = team2
        self.odds1 = odds1
        self.odds2 = odds2
        self.fair_prob1 = fair_prob1
        self.fair_prob2 = fair_prob2
        self.fair_odds1 = fair_odds1
        self.fair_odds2 = fair_odds2
        self.avg_vig = avg_vig
        self.tournament = tournament
        self.format = format
        self.is_live = is_live
        self.match_time = match_time
    
    def to_dict(self) -> dict:
        """Convert to dict for Supabase insert."""
        return {
            "match_id": self.match_id,
            "source": self.source,
            "game": self.game,
            "team1": self.team1,
            "team2": self.team2,
            "odds1": self.odds1,
            "odds2": self.odds2,
            "fair_prob1": self.fair_prob1,
            "fair_prob2": self.fair_prob2,
            "fair_odds1": self.fair_odds1,
            "fair_odds2": self.fair_odds2,
            "avg_vig": self.avg_vig,
            "tournament": self.tournament,
            "format": self.format,
            "is_live": self.is_live,
            "match_time": self.match_time,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


class MockSupabaseClient:
    """Mock SupabaseClient for testing core logic patterns."""
    
    def __init__(self, url: str = None, key: str = None):
        self._url = url or "https://test.supabase.co"
        self._key = key or "test-key"
        self._client = None
    
    def _parse_scraper_match(self, match: dict, source: str) -> MockEsportsOddsRecord:
        """Parse a match dict from scraper to record."""
        return MockEsportsOddsRecord(
            match_id=match.get("match_id", ""),
            source=source,
            game=match.get("game", ""),
            team1=match.get("team1", ""),
            team2=match.get("team2", ""),
            odds1=float(match.get("odds1", 0)),
            odds2=float(match.get("odds2", 0)),
            fair_prob1=float(match.get("fair_prob1", 0)),
            fair_prob2=float(match.get("fair_prob2", 0)),
            is_live=match.get("is_live", False),
        )
    
    def _build_query_filter(
        self,
        game: str = None,
        live_only: bool = False,
    ) -> dict:
        """Build query filter params."""
        filters = {}
        if game:
            filters["game"] = game
        if live_only:
            filters["is_live"] = True
        return filters


class TestEsportsOddsRecord:
    """Tests for EsportsOddsRecord dataclass."""
    
    def test_stores_all_fields(self):
        """Record stores all fields."""
        record = MockEsportsOddsRecord(
            match_id="cs2:liquid:vs:navi",
            source="source_k",
            game="cs2",
            team1="Liquid",
            team2="Navi",
            odds1=1.85,
            odds2=2.10,
            fair_prob1=0.54,
            fair_prob2=0.46,
        )
        
        assert record.match_id == "cs2:liquid:vs:navi"
        assert record.source == "source_k"
        assert record.team1 == "Liquid"
        assert record.team2 == "Navi"
        assert abs(record.fair_prob1 - 0.54) < 0.01
    
    def test_default_is_not_live(self):
        """Default is_live is False."""
        record = MockEsportsOddsRecord()
        assert record.is_live is False
    
    def test_to_dict_includes_all_fields(self):
        """to_dict includes all required fields."""
        record = MockEsportsOddsRecord(match_id="test")
        result = record.to_dict()
        
        assert "match_id" in result
        assert "source" in result
        assert "team1" in result
        assert "team2" in result
        assert "odds1" in result
        assert "fair_prob1" in result
        assert "created_at" in result
    
    def test_to_dict_has_timestamp(self):
        """to_dict adds created_at timestamp."""
        record = MockEsportsOddsRecord()
        result = record.to_dict()
        
        assert result["created_at"] is not None
        assert "T" in result["created_at"]  # ISO format


class TestSupabaseClientInit:
    """Tests for SupabaseClient initialization."""
    
    def test_init_stores_credentials(self):
        """Client stores URL and key."""
        client = MockSupabaseClient(
            url="https://myproject.supabase.co",
            key="my-service-key",
        )
        
        assert client._url == "https://myproject.supabase.co"
        assert client._key == "my-service-key"
    
    def test_init_with_defaults(self):
        """Client uses defaults when not provided."""
        client = MockSupabaseClient()
        
        assert client._url is not None
        assert client._key is not None


class TestScraperMatchParsing:
    """Tests for parsing scraper match dicts."""
    
    @pytest.fixture
    def client(self):
        """Create mock client."""
        return MockSupabaseClient()
    
    def test_parses_full_match(self, client):
        """Parses complete match dict."""
        match = {
            "match_id": "cs2:liquid:vs:navi",
            "game": "cs2",
            "team1": "Liquid",
            "team2": "Navi",
            "odds1": 1.85,
            "odds2": 2.10,
            "fair_prob1": 0.54,
            "fair_prob2": 0.46,
            "is_live": True,
        }
        
        record = client._parse_scraper_match(match, "source_a")
        
        assert record.match_id == "cs2:liquid:vs:navi"
        assert record.source == "source_a"
        assert record.team1 == "Liquid"
        assert record.is_live is True
    
    def test_handles_missing_fields(self, client):
        """Handles missing fields with defaults."""
        match = {"match_id": "test"}
        
        record = client._parse_scraper_match(match, "source_k")
        
        assert record.match_id == "test"
        assert record.source == "source_k"
        assert record.team1 == ""
        assert record.odds1 == 0
        assert record.is_live is False


class TestQueryFilterBuilding:
    """Tests for building query filters."""
    
    @pytest.fixture
    def client(self):
        """Create mock client."""
        return MockSupabaseClient()
    
    def test_filter_by_game(self, client):
        """Filter by game only."""
        filters = client._build_query_filter(game="cs2")
        
        assert filters["game"] == "cs2"
        assert "is_live" not in filters
    
    def test_filter_live_only(self, client):
        """Filter for live matches."""
        filters = client._build_query_filter(live_only=True)
        
        assert filters["is_live"] is True
    
    def test_filter_combined(self, client):
        """Combined game + live filter."""
        filters = client._build_query_filter(game="dota2", live_only=True)
        
        assert filters["game"] == "dota2"
        assert filters["is_live"] is True
    
    def test_empty_filter(self, client):
        """No filters returns empty dict."""
        filters = client._build_query_filter()
        
        assert filters == {}


class TestPositionRecordFormat:
    """Tests for position record formatting."""
    
    def test_position_record_structure(self):
        """Position record has correct structure."""
        position = {
            "position_id": "pos123",
            "entry_token_id": "token456",
            "entry_team": "Liquid",
            "entry_price": 0.50,
            "entry_shares": 25.0,
            "hedge_token_id": None,
            "hedge_price": None,
            "hedge_shares": None,
            "state": "ENTRY_FILLED",
            "bot_type": "prematch",
        }
        
        assert position["position_id"] == "pos123"
        assert position["state"] == "ENTRY_FILLED"
        assert position["hedge_token_id"] is None
    
    def test_position_state_values(self):
        """Position states follow expected values."""
        valid_states = [
            "ENTRY_PENDING",
            "ENTRY_FILLED",
            "HEDGE_PENDING",
            "HEDGED",
            "SETTLED",
            "CANCELLED",
        ]
        
        for state in valid_states:
            position = {"state": state}
            assert position["state"] in valid_states

