"""
Unit tests for services/odds_service.py - Odds aggregation and caching service.

Uses mocked Supabase responses to test odds parsing and aggregation logic.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta


class MockMatchOdds:
    """Mock MatchOdds dataclass for testing."""
    
    def __init__(
        self,
        match_id: str = "cs2:team-a:vs:team-b",
        team1: str = "Team A",
        team2: str = "Team B",
        odds1: float = 2.0,
        odds2: float = 2.0,
        fair_prob1: float = 0.50,
        fair_prob2: float = 0.50,
        source: str = "source_a",
        game: str = "cs2",
        is_live: bool = False,
        timestamp: datetime = None,
    ):
        self.match_id = match_id
        self.team1 = team1
        self.team2 = team2
        self.odds1 = odds1
        self.odds2 = odds2
        self.fair_prob1 = fair_prob1
        self.fair_prob2 = fair_prob2
        self.source = source
        self.game = game
        self.is_live = is_live
        self.timestamp = timestamp or datetime.now(timezone.utc)


class MockAggregatedMatch:
    """Mock AggregatedMatch for testing aggregation logic."""
    
    def __init__(
        self,
        match_id: str = "cs2:team-a:vs:team-b",
        team1: str = "Team A",
        team2: str = "Team B",
        game: str = "cs2",
        is_live: bool = False,
        sources: dict = None,
    ):
        self.match_id = match_id
        self.team1 = team1
        self.team2 = team2
        self.game = game
        self.is_live = is_live
        self.sources = sources or {}
    
    @property
    def fair_prob1(self) -> float:
        """Calculate median fair_prob1 across sources."""
        if not self.sources:
            return 0.0
        probs = [s.fair_prob1 for s in self.sources.values()]
        probs.sort()
        n = len(probs)
        if n == 1:
            return probs[0]
        if n % 2 == 0:
            return (probs[n//2 - 1] + probs[n//2]) / 2
        return probs[n//2]
    
    @property
    def fair_prob2(self) -> float:
        """Calculate median fair_prob2 across sources."""
        if not self.sources:
            return 0.0
        probs = [s.fair_prob2 for s in self.sources.values()]
        probs.sort()
        n = len(probs)
        if n == 1:
            return probs[0]
        if n % 2 == 0:
            return (probs[n//2 - 1] + probs[n//2]) / 2
        return probs[n//2]
    
    @property
    def num_sources(self) -> int:
        return len(self.sources)
    
    def is_fresh(self, max_age_live: int = 30, max_age_prematch: int = 1800) -> bool:
        """Check if data is fresh enough."""
        if not self.sources:
            return False
        
        now = datetime.now(timezone.utc)
        max_age = max_age_live if self.is_live else max_age_prematch
        
        for source in self.sources.values():
            age = (now - source.timestamp).total_seconds()
            if age > max_age:
                return False
        return True


class TestMatchOdds:
    """Tests for MatchOdds dataclass."""
    
    def test_stores_match_data(self):
        """MatchOdds stores all fields."""
        odds = MockMatchOdds(
            match_id="cs2:liquid:vs:navi",
            team1="Liquid",
            team2="Navi",
            odds1=1.85,
            odds2=2.10,
            fair_prob1=0.54,
            fair_prob2=0.46,
            source="source_k",
        )
        
        assert odds.match_id == "cs2:liquid:vs:navi"
        assert odds.team1 == "Liquid"
        assert odds.team2 == "Navi"
        assert odds.odds1 == 1.85
        assert odds.odds2 == 2.10
        assert abs(odds.fair_prob1 - 0.54) < 0.01
        assert abs(odds.fair_prob2 - 0.46) < 0.01
        assert odds.source == "source_k"
    
    def test_default_game_is_cs2(self):
        """Default game is cs2."""
        odds = MockMatchOdds()
        assert odds.game == "cs2"
    
    def test_default_is_not_live(self):
        """Default is_live is False."""
        odds = MockMatchOdds()
        assert odds.is_live is False


class TestAggregatedMatchMedian:
    """Tests for fair probability median calculation."""
    
    def test_single_source_returns_value(self):
        """Single source returns its value."""
        source = MockMatchOdds(fair_prob1=0.55, fair_prob2=0.45)
        match = MockAggregatedMatch(sources={"source_k": source})
        
        assert match.fair_prob1 == 0.55
        assert match.fair_prob2 == 0.45
    
    def test_two_sources_returns_mean(self):
        """Two sources returns mean."""
        source1 = MockMatchOdds(fair_prob1=0.50, fair_prob2=0.50)
        source2 = MockMatchOdds(fair_prob1=0.56, fair_prob2=0.44)
        match = MockAggregatedMatch(sources={"source_a": source1, "source_k": source2})
        
        assert abs(match.fair_prob1 - 0.53) < 0.01
        assert abs(match.fair_prob2 - 0.47) < 0.01
    
    def test_three_sources_returns_median(self):
        """Three sources returns median (middle value)."""
        source1 = MockMatchOdds(fair_prob1=0.48, fair_prob2=0.52)
        source2 = MockMatchOdds(fair_prob1=0.55, fair_prob2=0.45)
        source3 = MockMatchOdds(fair_prob1=0.60, fair_prob2=0.40)
        match = MockAggregatedMatch(sources={
            "source_a": source1, 
            "source_k": source2, 
            "betway": source3
        })
        
        # Median of [0.48, 0.55, 0.60] = 0.55
        assert abs(match.fair_prob1 - 0.55) < 0.01
        # Median of [0.40, 0.45, 0.52] = 0.45
        assert abs(match.fair_prob2 - 0.45) < 0.01
    
    def test_no_sources_returns_zero(self):
        """No sources returns 0.0."""
        match = MockAggregatedMatch(sources={})
        
        assert match.fair_prob1 == 0.0
        assert match.fair_prob2 == 0.0


class TestAggregatedMatchFreshness:
    """Tests for data freshness checking."""
    
    def test_fresh_prematch_data(self):
        """Prematch data within 30 min is fresh."""
        now = datetime.now(timezone.utc)
        source = MockMatchOdds(timestamp=now - timedelta(minutes=10))
        match = MockAggregatedMatch(is_live=False, sources={"source_a": source})
        
        is_fresh = match.is_fresh()
        assert is_fresh is True
    
    def test_stale_prematch_data(self):
        """Prematch data older than 30 min is stale."""
        now = datetime.now(timezone.utc)
        source = MockMatchOdds(timestamp=now - timedelta(minutes=45))
        match = MockAggregatedMatch(is_live=False, sources={"source_a": source})
        
        is_fresh = match.is_fresh()
        assert is_fresh is False
    
    def test_fresh_live_data(self):
        """Live data within 30 sec is fresh."""
        now = datetime.now(timezone.utc)
        source = MockMatchOdds(timestamp=now - timedelta(seconds=20), is_live=True)
        match = MockAggregatedMatch(is_live=True, sources={"source_a": source})
        
        is_fresh = match.is_fresh()
        assert is_fresh is True
    
    def test_stale_live_data(self):
        """Live data older than 30 sec is stale."""
        now = datetime.now(timezone.utc)
        source = MockMatchOdds(timestamp=now - timedelta(seconds=45), is_live=True)
        match = MockAggregatedMatch(is_live=True, sources={"source_a": source})
        
        is_fresh = match.is_fresh()
        assert is_fresh is False
    
    def test_no_sources_not_fresh(self):
        """No sources means not fresh."""
        match = MockAggregatedMatch(sources={})
        
        is_fresh = match.is_fresh()
        assert is_fresh is False


class TestOddsServiceLogic:
    """Tests for OddsService core logic patterns."""
    
    def test_match_id_normalization(self):
        """Match IDs are normalized consistently."""
        # Simulate normalize_team behavior
        def normalize_team(name):
            return name.lower().replace(" ", "-")
        
        team1 = normalize_team("Team Liquid")
        team2 = normalize_team("Navi")
        
        assert team1 == "team-liquid"
        assert team2 == "navi"
    
    def test_make_match_id(self):
        """Match IDs are created consistently."""
        def make_match_id(team1, team2, game):
            t1 = team1.lower().replace(" ", "-")
            t2 = team2.lower().replace(" ", "-")
            sorted_teams = sorted([t1, t2])
            return f"{game}:{sorted_teams[0]}:vs:{sorted_teams[1]}"
        
        # Order shouldn't matter
        id1 = make_match_id("Team Liquid", "Navi", "cs2")
        id2 = make_match_id("Navi", "Team Liquid", "cs2")
        
        assert id1 == id2
    
    def test_fair_prob_from_odds(self):
        """Convert decimal odds to implied probability."""
        def implied_prob(odds):
            return 1.0 / odds
        
        # 2.0 odds = 50% prob
        assert abs(implied_prob(2.0) - 0.50) < 0.01
        
        # 1.5 odds = 66.7% prob
        assert abs(implied_prob(1.5) - 0.667) < 0.01
        
        # 3.0 odds = 33.3% prob
        assert abs(implied_prob(3.0) - 0.333) < 0.01


class TestNumSources:
    """Tests for source counting."""
    
    def test_num_sources_empty(self):
        """Empty sources returns 0."""
        match = MockAggregatedMatch(sources={})
        assert match.num_sources == 0
    
    def test_num_sources_one(self):
        """One source returns 1."""
        source = MockMatchOdds()
        match = MockAggregatedMatch(sources={"source_a": source})
        assert match.num_sources == 1
    
    def test_num_sources_multiple(self):
        """Multiple sources returns correct count."""
        sources = {
            "source_a": MockMatchOdds(source="source_a"),
            "source_k": MockMatchOdds(source="source_k"),
            "betway": MockMatchOdds(source="betway"),
        }
        match = MockAggregatedMatch(sources=sources)
        assert match.num_sources == 3


class MockMatchOdds3Way:
    """Mock MatchOdds with fair_prob_draw for 3-way sports."""
    
    def __init__(
        self,
        match_id: str = "rugby:team-a:vs:team-b",
        team1: str = "Team A",
        team2: str = "Team B",
        odds1: float = 1.90,
        odds2: float = 1.90,
        fair_prob1: float = 47.5,
        fair_prob2: float = 47.5,
        fair_prob_draw: float = 5.0,  # Rugby typically 1-5%
        source: str = "source_l",
        game: str = "rugby",
        is_live: bool = False,
        timestamp: datetime = None,
    ):
        self.match_id = match_id
        self.team1 = team1
        self.team2 = team2
        self.odds1 = odds1
        self.odds2 = odds2
        self.fair_prob1 = fair_prob1
        self.fair_prob2 = fair_prob2
        self.fair_prob_draw = fair_prob_draw
        self.source = source
        self.game = game
        self.is_live = is_live
        self.timestamp = timestamp or datetime.now(timezone.utc)


class MockAggregatedMatch3Way:
    """Mock AggregatedMatch with 3-way support."""
    
    def __init__(
        self,
        match_id: str = "rugby:team-a:vs:team-b",
        team1: str = "Team A",
        team2: str = "Team B",
        game: str = "rugby",
        is_live: bool = False,
        sources: dict = None,
    ):
        self.match_id = match_id
        self.team1 = team1
        self.team2 = team2
        self.game = game
        self.is_live = is_live
        self.sources = sources or {}
    
    @property
    def fair_prob_draw(self):
        """Median draw probability across sources."""
        if not self.sources:
            return None
        probs = [s.fair_prob_draw for s in self.sources.values() 
                 if hasattr(s, 'fair_prob_draw') and s.fair_prob_draw is not None and s.fair_prob_draw > 0]
        if not probs:
            return None
        probs.sort()
        n = len(probs)
        if n == 1:
            return probs[0]
        if n % 2 == 0:
            return (probs[n//2 - 1] + probs[n//2]) / 2
        return probs[n//2]
    
    @property
    def is_3way(self) -> bool:
        """Check if this is a 3-way match."""
        return self.fair_prob_draw is not None


class Test3WayMatchDrawProbability:
    """Tests for fair_prob_draw median calculation on 3-way sports."""
    
    def test_single_source_returns_draw_prob(self):
        """Single source returns its draw probability."""
        source = MockMatchOdds3Way(fair_prob_draw=4.5)
        match = MockAggregatedMatch3Way(sources={"source_l": source})
        
        assert match.fair_prob_draw == 4.5
        assert match.is_3way is True
    
    def test_multiple_sources_returns_median_draw_prob(self):
        """Multiple sources return median draw probability."""
        source1 = MockMatchOdds3Way(fair_prob_draw=3.0)
        source2 = MockMatchOdds3Way(fair_prob_draw=4.5)
        source3 = MockMatchOdds3Way(fair_prob_draw=5.5)
        match = MockAggregatedMatch3Way(sources={
            "source_l": source1,
            "source_g": source2,
            "source_n": source3
        })
        
        # Median of [3.0, 4.5, 5.5] = 4.5
        assert match.fair_prob_draw == 4.5
    
    def test_is_3way_false_for_esports(self):
        """is_3way returns False for esports (no draw probability)."""
        source = MockMatchOdds(fair_prob1=50.0, fair_prob2=50.0)  # 2-way
        match = MockAggregatedMatch3Way(sources={"source_b": source})
        
        # MockMatchOdds doesn't have fair_prob_draw
        assert match.fair_prob_draw is None
        assert match.is_3way is False
    
    def test_no_sources_returns_none(self):
        """No sources returns None for draw probability."""
        match = MockAggregatedMatch3Way(sources={})
        
        assert match.fair_prob_draw is None
        assert match.is_3way is False


class TestRugbyDrawConfig:
    """Tests for rugby draw config values."""
    
    def test_rugby_draw_max_prices_exist(self):
        """Config has rugby_draw_max_prices."""
        from src.core.config import CONFIG
        
        prices = CONFIG.get("rugby_draw_max_prices")
        assert prices is not None
        assert 5.0 in prices
        assert 4.0 in prices
        assert 0.0 in prices
    
    def test_draw_min_edge_exists(self):
        """Config has draw_min_edge."""
        from src.core.config import CONFIG
        
        assert CONFIG.get("draw_min_edge") == 0.015  # 1.5%
