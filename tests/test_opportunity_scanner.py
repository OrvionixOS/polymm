"""
Unit tests for scanning/opportunity_scanner.py - Opportunity discovery logic.
"""
import pytest
from dataclasses import dataclass
from typing import Optional, List


# Mock data classes to avoid heavy imports
@dataclass
class MockAggregatedMatch:
    """Mock for AggregatedMatch."""
    match_id: str
    team1: str
    team2: str
    game: str
    fair_prob1: float = 0.5
    fair_prob2: float = 0.5


@dataclass
class MockPolymarketEvent:
    """Mock for PolymarketEsportsEvent."""
    title: str
    team1: str = ""
    team2: str = ""
    is_live: bool = False
    is_finished: bool = False
    markets: Optional[List] = None


# Import the functions under test
from src.scanning.opportunity_scanner import find_poly_event



class TestFindPolyEvent:
    """Tests for find_poly_event function."""
    
    def test_exact_team_match_cs2(self):
        """Finds correct event with exact team names for CS2."""
        odds_match = MockAggregatedMatch(
            match_id="cs2:liquid:vs:navi",
            team1="Team Liquid",
            team2="Natus Vincere",
            game="cs2",
        )
        
        poly_events = [
            MockPolymarketEvent(title="CS2: Team Liquid vs Natus Vincere"),
            MockPolymarketEvent(title="Dota 2: Team Liquid vs Evil Geniuses"),
        ]
        
        result = find_poly_event(odds_match, poly_events)
        
        assert result is not None
        assert "CS2" in result.title
        assert "Liquid" in result.title
    
    def test_game_filtering_prevents_cross_game_collision(self):
        """CS2 match should NOT match Dota2 event, even with same team name."""
        odds_match = MockAggregatedMatch(
            match_id="cs2:liquid:vs:nip",
            team1="Team Liquid",
            team2="NiP",
            game="cs2",
        )
        
        poly_events = [
            # Only Dota2 event with Liquid, no CS2 event
            MockPolymarketEvent(title="Dota 2: Team Liquid vs Tundra"),
        ]
        
        result = find_poly_event(odds_match, poly_events)
        
        # Should NOT match - wrong game type
        assert result is None
    
    def test_team_prefix_matching(self):
        """Matches teams using 6-character prefix."""
        odds_match = MockAggregatedMatch(
            match_id="cs2:vitality:vs:g2",
            team1="Team Vitality",
            team2="G2 Esports",
            game="cs2",
        )
        
        poly_events = [
            MockPolymarketEvent(title="CS2: Vitality vs G2", team1="Vitality", team2="G2"),
        ]
        
        result = find_poly_event(odds_match, poly_events)
        
        assert result is not None
        assert "Vitality" in result.title
    
    def test_strips_common_suffixes(self):
        """Strips 'Esports', 'Gaming', 'Team' from match."""
        odds_match = MockAggregatedMatch(
            match_id="lol:fnatic:vs:g2",
            team1="Fnatic Esports",
            team2="G2 Esports",
            game="lol",
        )
        
        poly_events = [
            MockPolymarketEvent(title="LoL: Fnatic vs G2", team1="Fnatic", team2="G2"),
        ]
        
        result = find_poly_event(odds_match, poly_events)
        
        assert result is not None
    
    def test_no_match_when_teams_missing(self):
        """Returns None when teams not in any event."""
        odds_match = MockAggregatedMatch(
            match_id="cs2:faze:vs:mouz",
            team1="FaZe Clan",
            team2="MOUZ",
            game="cs2",
        )
        
        poly_events = [
            MockPolymarketEvent(title="CS2: Team Liquid vs Natus Vincere"),
        ]
        
        result = find_poly_event(odds_match, poly_events)
        
        assert result is None
    
    def test_best_match_scoring(self):
        """Selects best match when multiple events match partially."""
        odds_match = MockAggregatedMatch(
            match_id="cs2:liquid:vs:navi",
            team1="Team Liquid",
            team2="Natus Vincere",
            game="cs2",
        )
        
        poly_events = [
            # Shorter team names
            MockPolymarketEvent(title="CS2: Liquid vs Natus"),
            # Full team names (should score higher)
            MockPolymarketEvent(title="CS2: Team Liquid vs Natus Vincere (BO3)"),
        ]
        
        result = find_poly_event(odds_match, poly_events)
        
        assert result is not None
        # Should prefer the more complete match
        assert "Team Liquid" in result.title or "Vincere" in result.title
    
    def test_game_patterns_dota2(self):
        """Matches Dota2 game patterns correctly."""
        odds_match = MockAggregatedMatch(
            match_id="dota2:og:vs:tundra",
            team1="OG",
            team2="Tundra",
            game="dota2",
        )
        
        poly_events = [
            MockPolymarketEvent(title="Dota 2: OG vs Tundra"),
        ]
        
        result = find_poly_event(odds_match, poly_events)
        
        assert result is not None
        assert "Dota" in result.title
    
    def test_game_patterns_lol(self):
        """Matches LoL game patterns correctly."""
        odds_match = MockAggregatedMatch(
            match_id="lol:t1:vs:geng",
            team1="T1",
            team2="Gen.G",
            game="lol",
        )
        
        poly_events = [
            MockPolymarketEvent(title="League of Legends: T1 vs Gen.G"),
        ]
        
        result = find_poly_event(odds_match, poly_events)
        
        assert result is not None
    
    def test_unknown_game_matches_without_game_filter(self):
        """When game is unknown, matches on team names only."""
        odds_match = MockAggregatedMatch(
            match_id="unknown:teama:vs:teamb",
            team1="Team Alpha",
            team2="Team Beta",
            game="",  # Unknown game
        )
        
        poly_events = [
            MockPolymarketEvent(title="Esports: Team Alpha vs Team Beta"),
        ]
        
        result = find_poly_event(odds_match, poly_events)
        
        # Should match since game is unknown
        assert result is not None
    
    def test_empty_events_list(self):
        """Returns None for empty events list."""
        odds_match = MockAggregatedMatch(
            match_id="cs2:a:vs:b",
            team1="Team A",
            team2="Team B",
            game="cs2",
        )
        
        result = find_poly_event(odds_match, [])
        
        assert result is None
    
    def test_case_insensitive_matching(self):
        """Matching is case insensitive."""
        odds_match = MockAggregatedMatch(
            match_id="cs2:navi:vs:faze",
            team1="NATUS VINCERE",
            team2="FAZE CLAN",
            game="cs2",
        )
        
        poly_events = [
            MockPolymarketEvent(title="cs2: natus vincere vs faze clan"),
        ]
        
        result = find_poly_event(odds_match, poly_events)
        
        assert result is not None
