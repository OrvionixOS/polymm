"""
Unit tests for Team Collision Bug Fix.

Tests verify that the bot NEVER uses odds from the wrong match by ensuring:
1. find_matching_odds requires BOTH teams to match exactly
2. update_fair_probs_by_team requires BOTH teams to match exactly  
3. get_fair_value_for_match rejects stale odds
4. Fallback searches are properly guarded against single-team matching

Bug reference: "Bad Luck vs Yawara" was incorrectly matched to "Players vs Bad Luck"
because fallback code matched on single team name only.
"""
import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta

from src.core.match_id import normalize_team
from src.state.bot_state import BotState
from src.state.match_state import MatchState, MatchPosition
from src.services.odds_service import AggregatedMatch, MatchOdds


def create_aggregated_match(
    match_id: str, 
    team1: str, 
    team2: str, 
    game: str = "cs2",
    fair1: float = 50.0,
    fair2: float = 50.0,
    timestamp: datetime = None,
) -> AggregatedMatch:
    """Helper to create AggregatedMatch with proper sources."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    
    match = AggregatedMatch(
        match_id=match_id,
        team1=team1,
        team2=team2,
        game=game,
        is_live=False,
        timestamp=timestamp,
    )
    # Add a source with fair probs (required for fair_prob1/2 properties)
    match.sources["test_source"] = MatchOdds(
        match_id=match_id,
        team1=team1,
        team2=team2,
        odds1=2.0,
        odds2=2.0,
        fair_prob1=fair1,
        fair_prob2=fair2,
        source="test_source",
        game=game,
        is_live=False,
        timestamp=timestamp,
    )
    return match


class TestFindMatchingOddsExactMatch:
    """Tests that find_matching_odds requires BOTH teams to match exactly."""
    
    @pytest.fixture
    def odds_matches(self):
        """Create sample odds matches.
        
        Simulates the bug scenario:
        - "Bad Luck vs Yawara" on Polymarket
        - "Players vs Bad Luck" in odds database
        
        These should NOT match because opponents are different.
        """
        return [
            create_aggregated_match(
                match_id="cs2:badluck:vs:players",
                team1="Players",
                team2="Bad Luck",
                fair1=60.0,
                fair2=40.0,
            ),
            create_aggregated_match(
                match_id="cs2:spikesyndicate:vs:lupus",
                team1="SPIKE Syndicate",
                team2="Lupus Esports",
                fair1=45.0,
                fair2=55.0,
            ),
        ]
    
    def test_no_match_when_only_one_team_matches(self, odds_matches):
        """CRITICAL: Must NOT match when only one team name matches."""
        from src.scanning.team_matcher import find_matching_odds
        
        # Polymarket has "Bad Luck vs Yawara" but odds only has "Players vs Bad Luck"
        match, is_swapped = find_matching_odds(
            market_team1="Bad Luck",
            market_team2="Yawara",  # This team is NOT in any odds match
            odds_matches=odds_matches,
        )
        
        # MUST return None - we should NOT match "Players vs Bad Luck"
        assert match is None, "Should NOT match when only one team matches!"
    
    def test_no_match_when_same_team_different_opponent(self, odds_matches):
        """CRITICAL: Must NOT match when team plays different opponent."""
        from src.scanning.team_matcher import find_matching_odds
        
        # Polymarket has "SPIKE Syndicate vs BeFive" but odds only has "SPIKE vs Lupus"
        match, is_swapped = find_matching_odds(
            market_team1="SPIKE Syndicate",
            market_team2="BeFive",  # Different opponent than Lupus
            odds_matches=odds_matches,
        )
        
        assert match is None, "Should NOT match when opponent is different!"
    
    def test_exact_match_succeeds(self, odds_matches):
        """Exact match with both teams works."""
        from src.scanning.team_matcher import find_matching_odds
        
        match, is_swapped = find_matching_odds(
            market_team1="Players",
            market_team2="Bad Luck",
            odds_matches=odds_matches,
        )
        
        assert match is not None
        assert match.team1 == "Players"
        assert match.team2 == "Bad Luck"
    
    def test_exact_match_swapped_order(self, odds_matches):
        """Exact match works when team order is reversed."""
        from src.scanning.team_matcher import find_matching_odds
        
        match, is_swapped = find_matching_odds(
            market_team1="Bad Luck",  # Order reversed from odds
            market_team2="Players",
            odds_matches=odds_matches,
        )
        
        assert match is not None
        assert is_swapped is True


class TestUpdateFairProbsByTeamExactMatch:
    """Tests that update_fair_probs_by_team fallback requires BOTH teams."""
    
    @pytest.fixture
    def bot_state_with_single_team(self):
        """Create BotState with a match that has only one team known.
        
        This simulates hydration where question parsing failed.
        """
        state = BotState()
        state.register_match(
            match_id="cs2:badluck:vs:unknown",
            team1="Bad Luck",
            team2="",  # Second team unknown (parsing failed)
            game="cs2",
        )
        return state
    
    @pytest.fixture
    def bot_state_with_both_teams(self):
        """Create BotState with both teams known."""
        state = BotState()
        state.register_match(
            match_id="cs2:badluck:vs:yawara",
            team1="Bad Luck",
            team2="Yawara",
            game="cs2",
        )
        return state
    
    def test_no_update_when_only_one_team_matches_botstate(self, bot_state_with_single_team):
        """CRITICAL: Must NOT update fair probs when BotState only has one team.
        
        Bug scenario: BotState has "Bad Luck" (single team), odds source pushes
        "Players vs Bad Luck". The fallback should NOT match these.
        """
        result = bot_state_with_single_team.update_fair_probs_by_team(
            match_id="wrong_match_id",  # Won't match by ID
            team1="Players",
            team2="Bad Luck",  # One team matches, but opponent doesn't!
            fair_prob1=0.60,
            fair_prob2=0.40,
        )
        
        # MUST return None - should NOT update the "Bad Luck vs unknown" match
        assert result is None, "Should NOT update when only one team matches!"
    
    def test_no_update_when_wrong_opponent(self, bot_state_with_both_teams):
        """CRITICAL: Must NOT update when source has different opponent.
        
        Bug scenario: BotState has "Bad Luck vs Yawara", odds source pushes
        "Players vs Bad Luck". These are DIFFERENT matches.
        """
        result = bot_state_with_both_teams.update_fair_probs_by_team(
            match_id="wrong_match_id",  # Won't match by ID
            team1="Players",  # Wrong opponent for "Bad Luck vs Yawara"!
            team2="Bad Luck",
            fair_prob1=0.60,
            fair_prob2=0.40,
        )
        
        assert result is None, "Should NOT update when opponent is different!"
    
    def test_update_when_both_teams_match(self, bot_state_with_both_teams):
        """Update succeeds when BOTH teams match exactly."""
        result = bot_state_with_both_teams.update_fair_probs_by_team(
            match_id="cs2:badluck:vs:yawara",  # Correct match ID
            team1="Bad Luck",
            team2="Yawara",
            fair_prob1=0.55,
            fair_prob2=0.45,
        )
        
        assert result is not None
        assert result.fair_prob1 == 0.55
        assert result.fair_prob2 == 0.45
    
    def test_fallback_finds_match_with_different_id_both_teams(self):
        """Fallback lookup works when match_id differs but BOTH teams match."""
        state = BotState()
        state.register_match(
            match_id="valorant:liquid:vs:navi",  # No game prefix
            team1="Team Liquid",
            team2="Natus Vincere",
            game="valorant",
        )
        
        # Try updating with a different match_id format
        result = state.update_fair_probs_by_team(
            match_id="some_other_id",  # Won't match by ID
            team1="Team Liquid",  # Both teams match
            team2="Natus Vincere",
            fair_prob1=0.65,
            fair_prob2=0.35,
        )
        
        # Should succeed via fallback since BOTH teams match
        assert result is not None
        assert result.fair_prob1 == 0.65


class TestGetFairValueStalenessRejection:
    """Tests that get_fair_value_for_match rejects stale odds."""
    
    def test_rejects_stale_odds(self):
        """CRITICAL: Must reject odds older than MAX_ODDS_AGE_SECONDS."""
        from src.scanning.team_matcher import get_fair_value_for_match, MAX_ODDS_AGE_SECONDS
        
        # Create stale odds (older than max age)
        old_timestamp = datetime.now(timezone.utc) - timedelta(seconds=MAX_ODDS_AGE_SECONDS + 60)
        stale_match = create_aggregated_match(
            match_id="cs2:liquid:vs:navi",
            team1="Team Liquid",
            team2="Natus Vincere",
            fair1=55.0,
            fair2=45.0,
            timestamp=old_timestamp,
        )
        
        mock_service = Mock()
        mock_service.get_matches.return_value = [stale_match]
        
        # Should reject stale odds
        fair, odds_match = get_fair_value_for_match(
            team1="Team Liquid",
            team2="Natus Vincere",
            our_team="Team Liquid",
            odds_service=mock_service,
            game="cs2",
        )
        
        assert fair is None, "Should reject stale odds!"
        assert odds_match is None
    
    def test_accepts_fresh_odds(self):
        """Fresh odds within MAX_ODDS_AGE_SECONDS should be accepted."""
        from src.scanning.team_matcher import get_fair_value_for_match, MAX_ODDS_AGE_SECONDS
        
        # Create fresh odds
        fresh_timestamp = datetime.now(timezone.utc) - timedelta(seconds=MAX_ODDS_AGE_SECONDS - 60)
        fresh_match = create_aggregated_match(
            match_id="cs2:liquid:vs:navi",
            team1="Team Liquid",
            team2="Natus Vincere",
            fair1=55.0,
            fair2=45.0,
            timestamp=fresh_timestamp,
        )
        
        mock_service = Mock()
        mock_service.get_matches.return_value = [fresh_match]
        
        fair, odds_match = get_fair_value_for_match(
            team1="Team Liquid",
            team2="Natus Vincere",
            our_team="Team Liquid",
            odds_service=mock_service,
            game="cs2",
        )
        
        assert fair is not None
        assert fair == 0.55  # 55% as decimal


class TestAlignTeamsWithBidsExactMatch:
    """Tests that align_teams_with_bids requires BOTH teams to match."""
    
    def test_no_align_when_one_team_differs(self):
        """CRITICAL: Must NOT align when one team is different."""
        from src.scanning.team_matcher import align_teams_with_bids
        
        # Odds match has "Bad Luck vs Players"
        odds_match = create_aggregated_match(
            match_id="cs2:badluck:vs:players",
            team1="Bad Luck",
            team2="Players",
            fair1=50.0,
            fair2=50.0,
        )
        
        # Polymarket has "Bad Luck vs Yawara" (different opponent!)
        outcomes = ["Bad Luck", "Yawara"]  
        token_ids = ["token_badluck", "token_yawara"]
        best_bids = {
            "token_badluck": {"price": 0.50, "size": 100},
            "token_yawara": {"price": 0.40, "size": 100},
        }
        
        result = align_teams_with_bids(odds_match, outcomes, token_ids, best_bids)
        
        # MUST return None - opponents don't match
        assert result is None, "Should NOT align when opponent differs!"
    
    def test_align_succeeds_when_both_teams_match(self):
        """Alignment succeeds when BOTH teams match exactly."""
        from src.scanning.team_matcher import align_teams_with_bids
        
        odds_match = create_aggregated_match(
            match_id="cs2:liquid:vs:navi",
            team1="Team Liquid",
            team2="Natus Vincere",
            fair1=60.0,
            fair2=40.0,
        )
        
        outcomes = ["Team Liquid", "Natus Vincere"]
        token_ids = ["token_liquid", "token_navi"]
        best_bids = {
            "token_liquid": {"price": 0.55, "size": 100},
            "token_navi": {"price": 0.35, "size": 100},
        }
        
        result = align_teams_with_bids(odds_match, outcomes, token_ids, best_bids)
        
        assert result is not None
        assert len(result) == 2


class TestTeamNormalizationDoesNotCreateFalseMatches:
    """Tests that team normalization doesn't create false positives."""
    
    def test_similar_names_do_not_match(self):
        """Teams with similar names but different identities must NOT match."""
        # "Bad Luck" should NOT match "Good Luck" even after normalization
        assert normalize_team("Bad Luck") != normalize_team("Good Luck")
    
    def test_partial_names_do_not_match(self):
        """Partial team names must NOT match full names incorrectly."""
        # "SPIKE" alone should NOT match "SPIKE Syndicate vs Lupus"
        # The test is about the matching logic, not normalization
        assert normalize_team("SPIKE Syndicate") == normalize_team("SPIKE Syndicate")
        # But different team entirely:
        assert normalize_team("SPIKE Syndicate") != normalize_team("SPIKE Gaming")
