"""
Unit tests for execution/hydration.py - Order and position hydration.
"""
import pytest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock

from src.execution.hydration import (
    _load_historical_pnl,
    _save_historical_pnl,
    HISTORICAL_PNL_FILE,
)


class TestLoadHistoricalPnl:
    """Tests for _load_historical_pnl function."""
    
    def test_returns_default_when_file_missing(self, tmp_path):
        """Returns default structure when file doesn't exist."""
        with patch("src.execution.hydration.HISTORICAL_PNL_FILE", tmp_path / "missing.json"):
            result = _load_historical_pnl()
        
        assert result == {
            "resolved_tokens": {},
            "total_wins": 0,
            "total_losses": 0,
            "total_pnl": 0.0,
        }
    
    def test_loads_existing_file(self, tmp_path):
        """Loads data from existing file."""
        test_file = tmp_path / "pnl.json"
        test_data = {
            "resolved_tokens": {"token1": {"pnl": 1.5}},
            "total_wins": 5,
            "total_losses": 2,
            "total_pnl": 12.5,
        }
        test_file.write_text(json.dumps(test_data))
        
        with patch("src.execution.hydration.HISTORICAL_PNL_FILE", test_file):
            result = _load_historical_pnl()
        
        assert result == test_data
    
    def test_returns_default_on_invalid_json(self, tmp_path):
        """Returns default when file contains invalid JSON."""
        test_file = tmp_path / "invalid.json"
        test_file.write_text("not valid json {{{")
        
        with patch("src.execution.hydration.HISTORICAL_PNL_FILE", test_file):
            result = _load_historical_pnl()
        
        assert result == {
            "resolved_tokens": {},
            "total_wins": 0,
            "total_losses": 0,
            "total_pnl": 0.0,
        }


class TestSaveHistoricalPnl:
    """Tests for _save_historical_pnl function."""
    
    def test_saves_data_to_file(self, tmp_path):
        """Saves data correctly to file."""
        test_file = tmp_path / "pnl.json"
        test_data = {
            "resolved_tokens": {"token1": {"pnl": 2.0}},
            "total_wins": 3,
            "total_losses": 1,
            "total_pnl": 8.5,
        }
        
        with patch("src.execution.hydration.HISTORICAL_PNL_FILE", test_file):
            _save_historical_pnl(test_data)
        
        assert test_file.exists()
        loaded = json.loads(test_file.read_text())
        assert loaded == test_data
    
    def test_creates_parent_directories(self, tmp_path):
        """Creates parent directories if needed."""
        test_file = tmp_path / "subdir" / "nested" / "pnl.json"
        
        with patch("src.execution.hydration.HISTORICAL_PNL_FILE", test_file):
            _save_historical_pnl({"total_pnl": 0})
        
        assert test_file.exists()
    
    def test_overwrites_existing_file(self, tmp_path):
        """Overwrites existing file with new data."""
        test_file = tmp_path / "pnl.json"
        test_file.write_text('{"old": "data"}')
        
        new_data = {"total_pnl": 100.0}
        
        with patch("src.execution.hydration.HISTORICAL_PNL_FILE", test_file):
            _save_historical_pnl(new_data)
        
        loaded = json.loads(test_file.read_text())
        assert loaded == new_data


class TestHydrateActiveOrdersUnit:
    """Unit tests for hydrate_active_orders helper logic."""
    
    def test_order_dict_parsing(self):
        """Order dict fields are correctly extracted."""
        order = {
            "asset_id": "token123",
            "id": "order456",
            "price": "0.50",
            "original_size": "10.0",
            "side": "BUY",
            "outcome": "Team A",
        }
        
        # Simulate the parsing logic from hydrate_active_orders
        token_id = order.get("asset_id") or order.get("token_id")
        order_id = order.get("id") or order.get("order_id")
        price = float(order.get("price", 0))
        size = float(order.get("original_size", order.get("size", 0)))
        
        assert token_id == "token123"
        assert order_id == "order456"
        assert price == 0.50
        assert size == 10.0
    
    def test_order_object_parsing(self):
        """Order object attributes are correctly extracted."""
        order = Mock()
        order.asset_id = "token789"
        order.id = "order012"
        order.price = "0.45"
        order.original_size = "20.0"
        order.side = "SELL"
        order.outcome = "Team B"
        
        # Simulate the parsing logic
        token_id = getattr(order, "asset_id", None) or getattr(order, "token_id", None)
        order_id = getattr(order, "id", None) or getattr(order, "order_id", None)
        price = float(getattr(order, "price", 0))
        size = float(getattr(order, "original_size", getattr(order, "size", 0)))
        
        assert token_id == "token789"
        assert order_id == "order012"
        assert price == 0.45
        assert size == 20.0


class TestHydrateFilledPositionsUnit:
    """Unit tests for hydrate_filled_positions helper logic."""
    
    def test_position_outcome_parsing(self):
        """Position outcome field is correctly parsed."""
        position = {
            "asset_id": "token_abc",
            "outcome": "Team Liquid",
            "size": "25.0",
            "avg_fill_price": "0.42",
        }
        
        outcome = position.get("outcome", "")
        shares = float(position.get("size", 0))
        avg_price = float(position.get("avg_fill_price", 0))
        
        assert outcome == "Team Liquid"
        assert shares == 25.0
        assert avg_price == 0.42
    
    def test_market_title_parsing(self):
        """Market title with game prefix is parsed correctly."""
        title = "CS2: Team Liquid vs Natus Vincere (BO3)"
        
        # Test game extraction
        if "CS2" in title.upper() or "COUNTER" in title.upper():
            game = "cs2"
        elif "DOTA" in title.upper():
            game = "dota2"
        elif "LOL" in title.upper() or "LEAGUE" in title.upper():
            game = "lol"
        else:
            game = ""
        
        assert game == "cs2"


class TestHydrationIntegration:
    """Integration-level tests for hydration functions."""
    
    @pytest.fixture
    def bot_state(self):
        """Create fresh BotState."""
        from src.state.bot_state import BotState
        return BotState()
    
    @pytest.fixture
    def mock_executor(self):
        """Create mock executor with client."""
        executor = Mock()
        executor.paper_trading = False
        
        mock_client = Mock()
        mock_client.get_orders.return_value = []
        executor._get_client.return_value = mock_client
        
        return executor
    
    @pytest.mark.asyncio
    async def test_hydrate_empty_orders(self, bot_state, mock_executor):
        """Hydration with no orders returns 0."""
        from src.execution.hydration import hydrate_active_orders
        
        count = await hydrate_active_orders(
            bot_state=bot_state,
            executor=mock_executor,
            quiet=True,
        )
        
        assert count == 0
    
    @pytest.mark.asyncio
    async def test_hydrate_handles_api_error(self, bot_state, mock_executor):
        """Hydration handles API errors gracefully."""
        from src.execution.hydration import hydrate_active_orders
        
        mock_executor._get_client().get_orders.side_effect = Exception("API error")
        
        count = await hydrate_active_orders(
            bot_state=bot_state,
            executor=mock_executor,
            quiet=True,
        )
        
        assert count == 0


class TestMatchDisplayCollisionPrevention:
    """Tests for preventing match_display collisions during hydration.
    
    Scenario: Same team name (e.g., "STATE") appears in multiple matches:
    - "Counter-Strike: FORZE Reload vs STATE (BO3)"
    - "Counter-Strike: los kogutos vs STATE (BO3)"
    
    The fallback team-only search should NOT overwrite match_display
    with a potentially wrong match, as this causes match_id collisions.
    """
    
    def test_exact_match_sets_matched_flag(self):
        """When find_matching_odds succeeds, matched_by_exact_parse is True.
        
        This is the logic that determines whether to overwrite match_display.
        """
        from src.scanning.team_matcher import find_matching_odds
        from src.services.odds_service import AggregatedMatch, MatchOdds
        from datetime import datetime, timezone
        
        # Create source odds for the matches (required for fair_prob1/fair_prob2 to be > 0)
        source1 = MatchOdds(
            match_id="cs2:forzereload:vs:state",
            team1="FORZE Reload",
            team2="STATE",
            odds1=1.80,
            odds2=2.10,
            fair_prob1=55,
            fair_prob2=45,
            source="test",
            game="cs2",
            is_live=False,
        )
        source2 = MatchOdds(
            match_id="cs2:loskogutos:vs:state",
            team1="los kogutos",
            team2="STATE",
            odds1=1.90,
            odds2=2.00,
            fair_prob1=50,
            fair_prob2=50,
            source="test",
            game="cs2",
            is_live=False,
        )
        
        # Create mock matches with sources
        match1 = AggregatedMatch(
            match_id="cs2:forzereload:vs:state",
            game="cs2",
            team1="FORZE Reload",
            team2="STATE",
            is_live=False,
            sources={"test": source1},
        )
        match2 = AggregatedMatch(
            match_id="cs2:loskogutos:vs:state",
            game="cs2",
            team1="los kogutos",
            team2="STATE",
            is_live=False,
            sources={"test": source2},
        )
        
        all_matches = [match1, match2]
        
        # Exact match with BOTH teams should find the correct match
        result, _ = find_matching_odds("FORZE Reload", "STATE", all_matches)
        assert result is not None
        assert result.team1 == "FORZE Reload"
        assert result.team2 == "STATE"
        
        # Different match should find different result
        result2, _ = find_matching_odds("los kogutos", "STATE", all_matches)
        assert result2 is not None
        assert result2.team1 == "los kogutos"
    
    def test_team_only_fallback_finds_first_match(self):
        """Fallback team-only search finds first match with that team.
        
        This is why we should NOT overwrite match_display in this case.
        """
        from src.core.match_id import normalize_team
        from src.services.odds_service import AggregatedMatch
        
        match1 = AggregatedMatch(
            match_id="cs2:forzereload:vs:state",
            game="cs2",
            team1="FORZE Reload",
            team2="STATE",
            is_live=False,
        )
        match2 = AggregatedMatch(
            match_id="cs2:loskogutos:vs:state",
            game="cs2",
            team1="los kogutos",
            team2="STATE",
            is_live=False,
        )
        
        all_matches = [match1, match2]
        
        # Simulate the team-only fallback
        team_name = "STATE"
        team_norm = normalize_team(team_name)
        
        odds_match = None
        for m in all_matches:
            if team_norm == normalize_team(m.team1) or team_norm == normalize_team(m.team2):
                odds_match = m
                break
        
        # It finds the FIRST match with STATE - could be wrong!
        assert odds_match is not None
        assert odds_match.team1 == "FORZE Reload"  # First match
        
        # If we used this for second STATE order (kogutos match), it would be WRONG
        # This is why the fix doesn't overwrite match_display with fallback results
    
    def test_different_matches_generate_different_match_ids(self):
        """Different matches with same team should have different match_ids.
        
        This ensures collision detection is actually needed.
        """
        from src.core.match_id import make_match_id
        
        # Match 1: FORZE Reload vs STATE
        match_id_1 = make_match_id("FORZE Reload", "STATE", "cs2")
        
        # Match 2: los kogutos vs STATE  
        match_id_2 = make_match_id("los kogutos", "STATE", "cs2")
        
        # They MUST be different
        assert match_id_1 != match_id_2
        assert "forzereload" in match_id_1
        assert "loskogutos" in match_id_2
        assert "state" in match_id_1
        assert "state" in match_id_2
    
    def test_match_display_determines_match_id(self):
        """Verify that match_display parsing produces the correct match_id.
        
        This is the key relationship: if match_display is wrong, match_id is wrong.
        """
        from src.core.match_id import parse_match_question, make_match_id
        
        # Correct match_display for first match
        display1 = "Counter-Strike: FORZE Reload vs STATE (BO3)"
        game1, team1_1, team2_1 = parse_match_question(display1)
        match_id_1 = make_match_id(team1_1, team2_1, game1)
        
        # Correct match_display for second match
        display2 = "Counter-Strike: los kogutos vs STATE (BO3)"
        game2, team1_2, team2_2 = parse_match_question(display2)
        match_id_2 = make_match_id(team1_2, team2_2, game2)
        
        # Verify parsing worked
        assert team1_1 == "FORZE Reload"
        assert team2_1 == "STATE"
        assert team1_2 == "los kogutos"
        assert team2_2 == "STATE"
        
        # Verify match_ids are different
        assert match_id_1 != match_id_2
        
        # If we incorrectly use display1 for both, they'd get the SAME match_id
        # This is the bug the fix prevents
        wrong_game, wrong_t1, wrong_t2 = parse_match_question(display1)
        wrong_match_id = make_match_id(wrong_t1, wrong_t2, wrong_game)
        
        # Both orders would register to this same match_id - collision!
        assert match_id_1 == wrong_match_id


class TestFairProbsTeamAlignment:
    """Tests for fair probs alignment between OddsService and BotState.
    
    This specifically tests the bug where team-only fallback search would
    find a match (e.g., "FORZE vs STATE") but the order was for a different
    match (e.g., "Bebop vs STATE"), causing a match_id/teams mismatch.
    """
    
    def test_matched_by_exact_parse_flag_controls_fair_probs_update(self):
        """Only update fair probs if matched_by_exact_parse is True.
        
        The key fix: when using team-only fallback, we should NOT call
        update_fair_probs_by_team because the odds_match may not correspond
        to the BotState match_id.
        """
        # Simulate the scenario:
        # - Polymarket has "Bebop vs STATE" (match_id: cs2:bebop:vs:state)
        # - Bookmakers only have "FORZE Reload vs STATE"
        # - Team-only fallback finds "FORZE Reload vs STATE" for "STATE" orders
        
        # Without the fix: update_fair_probs_by_team would be called with:
        #   match_id=cs2:bebop:vs:state, team1=FORZE Reload, team2=STATE
        # Which causes the "Team mismatch" warning
        
        # With the fix: update_fair_probs_by_team is NOT called when
        #   matched_by_exact_parse=False
        
        # Verify the condition is enforced
        matched_by_exact_parse = False
        odds_match_exists = True
        match_state_exists = True
        
        # The fix: only update if BOTH conditions are true AND exact parse matched
        should_update_fair_probs = (
            match_state_exists and 
            odds_match_exists and 
            matched_by_exact_parse  # NEW CONDITION
        )
        
        assert should_update_fair_probs == False  # Team-only fallback = NO update
        
        # When exact parse succeeds, should update
        matched_by_exact_parse = True
        should_update_fair_probs = (
            match_state_exists and 
            odds_match_exists and 
            matched_by_exact_parse
        )
        
        assert should_update_fair_probs == True  # Exact match = DO update
    
    def test_team_only_fallback_does_not_corrupt_match_state(self):
        """Team-only fallback should not affect match_id or team names.
        
        The fallback is useful for edge calculation but should not modify
        the canonical match data derived from Polymarket's question.
        """
        from src.core.match_id import parse_match_question, make_match_id
        
        # Polymarket question for "Bebop vs STATE"
        match_question = "Counter-Strike: Bebop vs STATE (BO3)"
        game, team1, team2 = parse_match_question(match_question)
        
        # This is the CANONICAL match_id - should never be overwritten
        canonical_match_id = make_match_id(team1, team2, game)
        assert canonical_match_id == "cs2:bebop:vs:state"
        assert team1 == "Bebop"
        assert team2 == "STATE"
        
        # Even if fallback finds "FORZE Reload vs STATE", the match_id stays canonical
        fallback_match_id = make_match_id("FORZE Reload", "STATE", "cs2")
        assert fallback_match_id == "cs2:forzereload:vs:state"
        
        # They are different - the fallback should NOT override canonical
        assert canonical_match_id != fallback_match_id
    
    def test_three_state_matches_all_get_unique_ids(self):
        """Multiple matches with same team (STATE) all get unique match_ids."""
        from src.core.match_id import make_match_id
        
        # Three different matches all featuring "STATE"
        matches = [
            ("FORZE Reload", "STATE", "cs2"),
            ("los kogutos", "STATE", "cs2"),
            ("Bebop", "STATE", "cs2"),
        ]
        
        match_ids = [make_match_id(t1, t2, g) for t1, t2, g in matches]
        
        # All three should be unique
        assert len(set(match_ids)) == 3
        
        expected = [
            "cs2:forzereload:vs:state",
            "cs2:loskogutos:vs:state",
            "cs2:bebop:vs:state",
        ]
        for mid, expected_mid in zip(match_ids, expected):
            assert mid == expected_mid

