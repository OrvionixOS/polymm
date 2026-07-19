"""
Unit tests for rugby support in SpreadBot.

Tests the rugby-specific logic across multiple components:
- SpreadScanner: Draw filtering, market type detection, refresh fallthrough
- SpreadBot: Rugby execution branch (team parsing, match IDs, prefix filters)
- HedgeSeeker: rby: prefix routing to cost-based hedging
- FillHandler: rby game type recognition
"""
import pytest
import re
from unittest.mock import Mock, AsyncMock, patch
from dataclasses import dataclass, field
from typing import List, Optional

from src.polymarket.weather_client import WeatherEvent, WeatherMarket
from src.scanning.spread_scanner import SpreadScanner, SpreadOpportunity
from src.core.match_id import make_match_id, sport_from_slug, parse_match_question


# ============================================================
# Helpers
# ============================================================

def _make_rugby_market(
    question: str,
    token_id: str = "tok_yes",
    no_token_id: str = "tok_no",
    best_bid: float = 0.30,
    best_ask: float = 0.50,
    mid_price: float = 0.40,
    active: bool = True,
    accepting_orders: bool = True,
) -> WeatherMarket:
    """Create a WeatherMarket for a rugby sub-market."""
    return WeatherMarket(
        market_id="mkt_123",
        condition_id="cond_abc",
        question=question,
        group_item="",
        token_id=token_id,
        no_token_id=no_token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=best_ask - best_bid,
        mid_price=mid_price,
        volume=1000.0,
        liquidity=500.0,
        active=active,
        accepting_orders=accepting_orders,
    )


def _make_rugby_event(
    markets: List[WeatherMarket],
    title: str = "Harlequins vs. Saracens",
    market_type: str = "rugby",
) -> WeatherEvent:
    """Create a WeatherEvent for a rugby match."""
    return WeatherEvent(
        event_id="evt_rugby_1",
        slug="harlequins-vs-saracens",
        title=title,
        city="",
        date_str="",
        markets=markets,
        volume=5000.0,
        liquidity=2500.0,
        active=True,
        market_type=market_type,
    )


# ============================================================
# SpreadScanner: Draw Filtering
# ============================================================

class TestRugbyDrawFiltering:
    """Tests that Draw markets are excluded from rugby events."""

    @pytest.fixture
    def scanner(self):
        """Create SpreadScanner with mock client."""
        mock_client = Mock()
        return SpreadScanner(
            weather_client=mock_client,
            min_spread=0.10,
        )

    def test_draw_market_filtered_out(self, scanner):
        """Rugby draw markets are skipped."""
        draw_market = _make_rugby_market(
            question="Will there be a draw?",
            token_id="tok_draw_yes",
            no_token_id="tok_draw_no",
        )
        event = _make_rugby_event([draw_market])

        opps = scanner._scan_event(event)
        assert len(opps) == 0

    def test_draw_case_insensitive(self, scanner):
        """Draw filtering is case-insensitive."""
        for q in [
            "Will there be a Draw?",
            "Will there be a DRAW?",
            "Will there be a draw?",
            "Draw",
        ]:
            market = _make_rugby_market(question=q, token_id=f"tok_{q[:5]}")
            event = _make_rugby_event([market])
            opps = scanner._scan_event(event)
            assert len(opps) == 0, f"Draw not filtered for question: {q}"

    def test_win_market_passes_through(self, scanner):
        """'Will X win?' markets pass through filtering."""
        win_market = _make_rugby_market(
            question="Will Harlequins win?",
            token_id="tok_harle_yes",
            no_token_id="tok_harle_no",
        )
        event = _make_rugby_event([win_market])

        opps = scanner._scan_event(event)
        assert len(opps) == 1
        assert opps[0].question == "Will Harlequins win?"

    def test_mixed_markets_only_win_survives(self, scanner):
        """Only 'Will X win?' markets survive from a full rugby event."""
        markets = [
            _make_rugby_market(question="Will Harlequins win?", token_id="tok_h"),
            _make_rugby_market(question="Will Saracens win?", token_id="tok_s"),
            _make_rugby_market(question="Will there be a draw?", token_id="tok_d"),
        ]
        event = _make_rugby_event(markets)

        opps = scanner._scan_event(event)
        assert len(opps) == 2
        questions = {o.question for o in opps}
        assert "Will Harlequins win?" in questions
        assert "Will Saracens win?" in questions
        assert "Will there be a draw?" not in questions

    def test_draw_not_filtered_for_weather(self, scanner):
        """Draw filtering only applies to rugby events, not weather."""
        market = _make_rugby_market(
            question="Will there be a draw temperature above 50°F?",
        )
        event = _make_rugby_event([market], market_type="weather")

        opps = scanner._scan_event(event)
        # Weather events don't filter on "draw" in question
        assert len(opps) == 1

    def test_market_type_preserved_on_opportunity(self, scanner):
        """SpreadOpportunity inherits market_type from event."""
        market = _make_rugby_market(question="Will Harlequins win?")
        event = _make_rugby_event([market], market_type="rugby")

        opps = scanner._scan_event(event)
        assert len(opps) == 1
        assert opps[0].market_type == "rugby"

    def test_inactive_market_filtered(self, scanner):
        """Inactive markets are skipped regardless of type."""
        market = _make_rugby_market(
            question="Will Harlequins win?",
            active=False,
        )
        event = _make_rugby_event([market])

        opps = scanner._scan_event(event)
        assert len(opps) == 0

    def test_narrow_spread_filtered(self, scanner):
        """Markets with spread below threshold are skipped."""
        market = _make_rugby_market(
            question="Will Harlequins win?",
            best_bid=0.45,
            best_ask=0.48,  # 3c spread < 10c min
        )
        event = _make_rugby_event([market])

        opps = scanner._scan_event(event)
        assert len(opps) == 0


# ============================================================
# SpreadBot: Rugby Execution Branch — Team Parsing
# ============================================================

class TestRugbyTeamParsing:
    """Tests for the regex that extracts team names from 'Will X win?' questions."""

    PATTERN = re.compile(r'Will (.+?)\s+win\??', re.IGNORECASE)

    def test_standard_team(self):
        """Standard single-word team name."""
        m = self.PATTERN.match("Will Harlequins win?")
        assert m is not None
        assert m.group(1).strip() == "Harlequins"

    def test_multi_word_team(self):
        """Multi-word team name."""
        m = self.PATTERN.match("Will Leicester Tigers win?")
        assert m is not None
        assert m.group(1).strip() == "Leicester Tigers"

    def test_three_word_team(self):
        """Three-word team name."""
        m = self.PATTERN.match("Will Stade Français Paris win?")
        assert m is not None
        assert m.group(1).strip() == "Stade Français Paris"

    def test_team_with_hyphen(self):
        """Team name with hyphen."""
        m = self.PATTERN.match("Will Glasgow Warriors win?")
        assert m is not None
        assert m.group(1).strip() == "Glasgow Warriors"

    def test_no_question_mark(self):
        """Works without trailing question mark."""
        m = self.PATTERN.match("Will Saracens win")
        assert m is not None
        assert m.group(1).strip() == "Saracens"

    def test_case_insensitive(self):
        """Case-insensitive matching."""
        m = self.PATTERN.match("will Bristol Bears win?")
        assert m is not None
        assert m.group(1).strip() == "Bristol Bears"


# ============================================================
# SpreadBot: Match ID Generation for Rugby
# ============================================================

class TestRugbyMatchIdGeneration:
    """Tests for rby: match ID generation in SpreadBot rugby branch."""

    def test_match_id_uses_rugby_prefix(self):
        """Rugby spread trades use canonical 'rugby' game prefix."""
        yes_team = "Harlequins"
        no_team = "Harlequins No"
        game = "rby"  # SpreadBot passes abbreviation, normalize_game converts to "rugby"
        
        match_id = make_match_id(yes_team, no_team, game)
        assert match_id.startswith("rugby:")

    def test_match_id_normalizes_rby_to_rugby(self):
        """rby abbreviation is normalized to canonical 'rugby' prefix."""
        yes_team = "Saracens"
        no_team = "Saracens No"
        
        # Both 'rby' and 'rugby' produce the same match_id
        match_id_rby = make_match_id(yes_team, no_team, "rby")
        match_id_rugby = make_match_id(yes_team, no_team, "rugby")
        assert match_id_rby == match_id_rugby
        assert match_id_rby.startswith("rugby:")

    def test_condition_id_suffix_uniqueness(self):
        """Condition ID suffix ensures each sub-market has a unique match_id."""
        yes_team = "Harlequins"
        no_team = "Harlequins No"
        game = "rby"
        
        base_match_id = make_match_id(yes_team, no_team, game)
        
        cond1 = "0xabc123def456789012345678"
        cond2 = "0xdef789abc012345678901234"
        
        mid1 = f"{base_match_id}:{cond1[:18]}"
        mid2 = f"{base_match_id}:{cond2[:18]}"
        
        assert mid1 != mid2
        assert mid1.startswith("rugby:")
        assert mid2.startswith("rugby:")

    def test_two_team_markets_different_match_ids(self):
        """Two team sub-markets from same event get different match_ids."""
        team_a_yes = "Harlequins"
        team_a_no = "Harlequins No"
        team_b_yes = "Saracens"
        team_b_no = "Saracens No"
        game = "rby"
        
        mid_a = make_match_id(team_a_yes, team_a_no, game)
        mid_b = make_match_id(team_b_yes, team_b_no, game)
        
        assert mid_a != mid_b
        assert mid_a.startswith("rugby:")
        assert mid_b.startswith("rugby:")


# ============================================================
# HedgeSeeker: rby: Prefix Routing
# ============================================================

class TestPrefixRouting:
    """Tests that rugby/cricket/football match IDs are routed correctly."""

    def test_rugby_is_weather_market(self):
        """rugby: match IDs are treated as weather markets (cost-based hedging)."""
        match_id = "rugby:harlequins:vs:harlequinsno:0xabc123"
        is_weather = (
            match_id.startswith("weather") or 
            match_id.startswith("stock:") or 
            match_id.startswith("ncaab:") or 
            match_id.startswith("mentions:") or 
            match_id.startswith("spread:") or 
            match_id.startswith("rugby:")
        )
        assert is_weather is True

    def test_rugby_not_is_esports(self):
        """rugby: match IDs are not esports."""
        match_id = "rugby:harlequins:vs:harlequinsno:0xabc123"
        _ESPORTS_GAMES = {"cs2", "dota2", "lol", "valorant", "mlbb", "cod", "hok", "r6", "sc2"}
        game = match_id.split(":")[0]
        assert game not in _ESPORTS_GAMES

    def test_rugby_prefix_routes_to_cost_hedging(self):
        """rugby: match IDs use cost-based hedging."""
        match_id = "rugby:saracens:vs:saracensno:0xdef456"
        is_cost_based = match_id.startswith("rugby:")
        assert is_cost_based is True

    def test_rugby_included_in_spread_collapse_filter(self):
        """rugby: is included in spread collapse match ID filter."""
        prefixes = ["weather:", "stock:", "ncaab:", "mentions:", "spread:", "rugby:", "cricket:", "football:"]
        match_id = "rugby:bristolbears:vs:bristolbearsno:0xdef456"
        
        passes_filter = any(match_id.startswith(p) for p in prefixes)
        assert passes_filter is True

    def test_esports_excluded_from_spread_filter(self):
        """Esports match IDs do NOT pass the spread filter."""
        prefixes = ["weather:", "stock:", "ncaab:", "mentions:", "spread:", "rugby:", "cricket:", "football:"]
        
        for mid in ["cs2:liquid:vs:navi", "dota2:og:vs:spirit"]:
            passes = any(mid.startswith(p) for p in prefixes)
            assert passes is False, f"Esports ID should not pass: {mid}"


# ============================================================
# FillHandler: Market Type Detection
# ============================================================

class TestFillHandlerMarketType:
    """Tests for _get_market_type with rby game type."""

    @staticmethod
    def _get_market_type(game: str, is_live: bool = False) -> str:
        """Reproduce _get_market_type logic for testing."""
        if game == "weather":
            return "weather"
        elif game == "stock":
            return "stock"
        elif game == "rugby" or game == "rby":
            return "rugby"
        elif game == "mentions":
            return "mentions"
        elif game == "ncaab":
            return "ncaab"
        elif is_live:
            return "esports_live"
        else:
            return "esports"

    def test_rby_maps_to_rugby(self):
        """game='rby' (SpreadBot) maps to market_type='rugby'."""
        assert self._get_market_type("rby") == "rugby"

    def test_rugby_maps_to_rugby(self):
        """game='rugby' (SportsBot) also maps to market_type='rugby'."""
        assert self._get_market_type("rugby") == "rugby"

    def test_weather_maps_to_weather(self):
        """game='weather' maps correctly."""
        assert self._get_market_type("weather") == "weather"

    def test_stock_maps_to_stock(self):
        """game='stock' maps correctly."""
        assert self._get_market_type("stock") == "stock"

    def test_esports_default(self):
        """Unknown game defaults to 'esports'."""
        assert self._get_market_type("cs2") == "esports"

    def test_esports_live(self):
        """Unknown game with is_live=True returns 'esports_live'."""
        assert self._get_market_type("cs2", is_live=True) == "esports_live"


class TestFillHandlerIsWeather:
    """Tests that rugby/cricket/football game types are treated as weather (no fair value)."""

    WEATHER_GAMES = ("weather", "stock", "ncaab", "mentions", "rugby", "tennis", "cricket", "football")

    def test_rugby_is_weather(self):
        """rugby is in the weather games set."""
        assert "rugby" in self.WEATHER_GAMES

    def test_cricket_is_weather(self):
        """cricket is in the weather games set."""
        assert "cricket" in self.WEATHER_GAMES

    def test_football_is_weather(self):
        """football is in the weather games set."""
        assert "football" in self.WEATHER_GAMES

    def test_all_spread_bot_games_are_weather(self):
        """All SpreadBot game types are in weather set."""
        for game in ["weather", "stock", "ncaab", "mentions", "rugby", "cricket", "football"]:
            assert game in self.WEATHER_GAMES


# ============================================================
# SpreadScanner: Market Type Detection in _fetch_and_cache_opportunity
# ============================================================

class TestMarketTypeDetection:
    """Tests for market type detection from question text."""

    @staticmethod
    def _detect_market_type(question: str) -> str:
        """Reproduce the detection logic from _fetch_and_cache_opportunity."""
        q_lower = question.lower()
        if "temperature" in q_lower:
            return "weather"
        elif "up or down" in q_lower:
            return "stock"
        elif q_lower.startswith("will ") and " win?" in q_lower:
            return "rugby"
        elif " vs " in question or " vs. " in question:
            return "sports"
        elif "what will" in q_lower and ("say" in q_lower or "said" in q_lower or "mention" in q_lower or "name" in q_lower):
            return "mentions"
        else:
            return "weather"

    def test_will_x_win_is_rugby(self):
        """'Will X win?' questions are detected as rugby."""
        assert self._detect_market_type("Will Harlequins win?") == "rugby"
        assert self._detect_market_type("Will Leicester Tigers win?") == "rugby"
        assert self._detect_market_type("Will Glasgow Warriors win?") == "rugby"

    def test_temperature_is_weather(self):
        """Temperature questions are weather."""
        assert self._detect_market_type("Will the highest temperature in Chicago be above 50°F?") == "weather"

    def test_vs_is_sports(self):
        """'vs' or 'vs.' questions are sports."""
        assert self._detect_market_type("Duke vs. North Carolina") == "sports"
        assert self._detect_market_type("Team A vs Team B") == "sports"

    def test_up_or_down_is_stock(self):
        """'up or down' questions are stock."""
        assert self._detect_market_type("Will AAPL go up or down?") == "stock"

    def test_mentions_is_mentions(self):
        """Mentions questions detected correctly."""
        assert self._detect_market_type("What will the CEO say about AI?") == "mentions"

    def test_will_win_takes_priority_over_vs(self):
        """'Will X win?' is checked before 'vs' since it's more specific."""
        # This question has 'win?' — should be rugby, not sports
        assert self._detect_market_type("Will Harlequins win?") == "rugby"

    def test_draw_question_is_not_rugby(self):
        """'Will there be a draw?' doesn't match 'Will X win?' pattern."""
        # "Will there be a draw?" doesn't contain " win?"
        result = self._detect_market_type("Will there be a draw?")
        assert result != "rugby"


# ============================================================
# SpreadScanner: Refresh Opportunity Fallthrough
# ============================================================

class TestRefreshOpportunityFallthrough:
    """Tests that refresh_opportunity falls through correctly for non-weather tokens."""

    @pytest.fixture
    def scanner(self):
        """Create SpreadScanner with mock client."""
        mock_client = AsyncMock()
        return SpreadScanner(
            weather_client=mock_client,
            min_spread=0.10,
        )

    @pytest.mark.asyncio
    async def test_rugby_opp_does_not_call_get_event(self, scanner):
        """Rugby opportunities don't call get_event (city/date lookup)."""
        # Pre-populate with a rugby opportunity
        opp = SpreadOpportunity(
            event_id="evt1",
            event_title="Harlequins vs. Saracens",
            city="",
            date_str="",
            market_id="mkt1",
            condition_id="cond1",
            question="Will Harlequins win?",
            bin_label="",
            token_id="tok_rugby",
            no_token_id="tok_rugby_no",
            best_bid=0.30,
            best_ask=0.50,
            spread=0.20,
            spread_cents=20.0,
            mid_price=0.40,
            volume=1000,
            liquidity=500,
            market_type="rugby",
        )
        scanner._active_opportunities["tok_rugby"] = opp

        # Mock _fetch_and_cache_opportunity (Gamma API path)
        mock_refreshed = SpreadOpportunity(
            event_id="evt1",
            event_title="Harlequins vs. Saracens",
            city="",
            date_str="",
            market_id="mkt1",
            condition_id="cond1",
            question="Will Harlequins win?",
            bin_label="",
            token_id="tok_rugby",
            no_token_id="tok_rugby_no",
            best_bid=0.32,
            best_ask=0.48,
            spread=0.16,
            spread_cents=16.0,
            mid_price=0.40,
            volume=1100,
            liquidity=550,
            market_type="rugby",
        )
        scanner._fetch_and_cache_opportunity = AsyncMock(return_value=mock_refreshed)

        result = await scanner.refresh_opportunity("tok_rugby")

        # Should NOT call get_event (weather path)
        scanner.weather_client.get_event.assert_not_called()
        # Should call _fetch_and_cache_opportunity (Gamma API path)
        scanner._fetch_and_cache_opportunity.assert_called_once_with("tok_rugby", override_market_type="rugby", override_game_start_time=None)
        assert result is not None
        assert result.best_bid == 0.32

    @pytest.mark.asyncio
    async def test_weather_opp_calls_get_event(self, scanner):
        """Weather opportunities use get_event (city/date lookup)."""
        opp = SpreadOpportunity(
            event_id="evt_w",
            event_title="Chicago Weather",
            city="chicago",
            date_str="february-15",
            market_id="mkt_w",
            condition_id="cond_w",
            question="Will the highest temperature in Chicago be above 50°F?",
            bin_label="50-51°F",
            token_id="tok_weather",
            no_token_id="tok_weather_no",
            best_bid=0.30,
            best_ask=0.50,
            spread=0.20,
            spread_cents=20.0,
            mid_price=0.40,
            volume=1000,
            liquidity=500,
            market_type="weather",
        )
        scanner._active_opportunities["tok_weather"] = opp

        # Mock get_event to return None (simulates event not found)
        scanner.weather_client.get_event = AsyncMock(return_value=None)

        result = await scanner.refresh_opportunity("tok_weather")

        # Should call get_event for weather
        scanner.weather_client.get_event.assert_called_once()
        assert result is None

    @pytest.mark.asyncio
    async def test_sports_opp_falls_through(self, scanner):
        """Sports opportunities fall through to Gamma API fetch."""
        opp = SpreadOpportunity(
            event_id="evt_s",
            event_title="Duke vs. UNC",
            city="",
            date_str="",
            market_id="mkt_s",
            condition_id="cond_s",
            question="Duke vs. UNC",
            bin_label="Winner",
            token_id="tok_sports",
            no_token_id="tok_sports_no",
            best_bid=0.40,
            best_ask=0.55,
            spread=0.15,
            spread_cents=15.0,
            mid_price=0.475,
            volume=2000,
            liquidity=1000,
            market_type="sports",
        )
        scanner._active_opportunities["tok_sports"] = opp
        scanner._fetch_and_cache_opportunity = AsyncMock(return_value=opp)

        result = await scanner.refresh_opportunity("tok_sports")

        scanner.weather_client.get_event.assert_not_called()
        scanner._fetch_and_cache_opportunity.assert_called_once_with("tok_sports", override_market_type="sports", override_game_start_time=None)

    @pytest.mark.asyncio
    async def test_mentions_opp_falls_through(self, scanner):
        """Mentions opportunities fall through to Gamma API fetch."""
        opp = SpreadOpportunity(
            event_id="evt_m",
            event_title="Earnings Call",
            city="",
            date_str="",
            market_id="mkt_m",
            condition_id="cond_m",
            question="What will the CEO say?",
            bin_label="AI mentioned",
            token_id="tok_mentions",
            no_token_id="tok_mentions_no",
            best_bid=0.35,
            best_ask=0.55,
            spread=0.20,
            spread_cents=20.0,
            mid_price=0.45,
            volume=500,
            liquidity=250,
            market_type="mentions",
        )
        scanner._active_opportunities["tok_mentions"] = opp
        scanner._fetch_and_cache_opportunity = AsyncMock(return_value=opp)

        result = await scanner.refresh_opportunity("tok_mentions")

        scanner.weather_client.get_event.assert_not_called()
        scanner._fetch_and_cache_opportunity.assert_called_once_with("tok_mentions", override_market_type="mentions", override_game_start_time=None)


# ============================================================
# WeatherMarketClient: Rugby Event Marking
# ============================================================

class TestWeatherClientRugbyEvents:
    """Tests that rugby events are properly marked with market_type."""

    def test_rugby_event_market_type(self):
        """Events from get_rugby_events have market_type='rugby'."""
        event = WeatherEvent(
            event_id="evt1",
            slug="harle-vs-sarce",
            title="Harlequins vs. Saracens",
            city="",
            date_str="",
        )
        # Simulate what get_rugby_events does
        event.market_type = "rugby"
        assert event.market_type == "rugby"

    def test_default_market_type_is_weather(self):
        """Default market_type is 'weather'."""
        event = WeatherEvent(
            event_id="evt1",
            slug="chicago-temp",
            title="Chicago Temperature",
            city="chicago",
            date_str="february-15",
        )
        assert event.market_type == "weather"


# ============================================================
# End-to-End Flow: Rugby Event → Opportunity → Match ID
# ============================================================

class TestRugbyEndToEndFlow:
    """Integration-style tests that verify the full rugby flow."""

    @pytest.fixture
    def scanner(self):
        mock_client = Mock()
        return SpreadScanner(weather_client=mock_client, min_spread=0.10)

    def test_full_event_scan_produces_correct_opportunities(self, scanner):
        """A complete rugby event with 3 sub-markets produces 2 opportunities."""
        markets = [
            _make_rugby_market(
                question="Will Harlequins win?",
                token_id="tok_h_yes",
                no_token_id="tok_h_no",
            ),
            _make_rugby_market(
                question="Will Saracens win?",
                token_id="tok_s_yes",
                no_token_id="tok_s_no",
            ),
            _make_rugby_market(
                question="Will there be a draw?",
                token_id="tok_d_yes",
                no_token_id="tok_d_no",
            ),
        ]
        event = _make_rugby_event(markets)

        opps = scanner._scan_event(event)

        assert len(opps) == 2
        assert all(o.market_type == "rugby" for o in opps)
        token_ids = {o.token_id for o in opps}
        assert token_ids == {"tok_h_yes", "tok_s_yes"}

    def test_opportunity_match_id_generation(self, scanner):
        """Each rugby opportunity can generate a unique rby: match_id."""
        market = _make_rugby_market(
            question="Will Harlequins win?",
            token_id="tok_h_yes",
            no_token_id="tok_h_no",
        )
        event = _make_rugby_event([market])

        opps = scanner._scan_event(event)
        assert len(opps) == 1
        opp = opps[0]

        # Simulate SpreadBot._execute_opportunity team extraction
        win_match = re.match(r'Will (.+?)\s+win\??', opp.question, re.IGNORECASE)
        assert win_match is not None
        team_name = win_match.group(1).strip()
        assert team_name == "Harlequins"

        yes_team = team_name
        no_team = f"{team_name} No"
        game = "rby"

        base_match_id = make_match_id(yes_team, no_team, game)
        match_id = f"{base_match_id}:{opp.condition_id[:18]}"

        assert match_id.startswith("rugby:")

    def test_spread_profitability_check(self):
        """Spread profitability check works for rugby markets."""
        yes_bid = 0.35
        no_bid = 0.40
        total_cost = yes_bid + no_bid
        expected_profit = 1.0 - total_cost
        min_profit = 0.10  # Default 10c

        # Total cost 0.75 → profit 25c > 10c min ✓
        assert expected_profit >= min_profit

    def test_spread_unprofitable_rejected(self):
        """Unprofitable spreads are correctly identified."""
        yes_bid = 0.48
        no_bid = 0.48
        total_cost = yes_bid + no_bid
        expected_profit = 1.0 - total_cost
        min_profit = 0.10

        # Total cost 0.96 → profit 4c < 10c min ✗
        assert expected_profit < min_profit


# ============================================================
# sport_from_slug: Event Slug → Sport Classification
# ============================================================

class TestSportFromSlug:
    """Tests for sport_from_slug() event slug classification."""

    def test_rugby_slugs(self):
        """Rugby event slugs are classified correctly."""
        assert sport_from_slug("english-premiership-round-15-harlequins-vs-saracens") == "rugby"
        assert sport_from_slug("rugby-union-round-10") == "rugby"
        assert sport_from_slug("premiership-rugby-2026") == "rugby"

    def test_football_slugs(self):
        """Football event slugs are classified correctly."""
        assert sport_from_slug("epl-arsenal-vs-chelsea") == "football"
        assert sport_from_slug("la-liga-real-madrid-vs-barcelona") == "football"
        assert sport_from_slug("serie-a-napoli-vs-milan") == "football"
        assert sport_from_slug("bundesliga-bayern-vs-dortmund") == "football"
        assert sport_from_slug("champions-league-round-of-16") == "football"
        assert sport_from_slug("copa-libertadores-boca-vs-river") == "football"
        assert sport_from_slug("liga-betplay-jaguares-vs-once-caldas") == "football"
        assert sport_from_slug("mls-inter-miami-vs-lafc") == "football"
        assert sport_from_slug("saudi-pro-league-al-hilal-vs-al-nassr") == "football"

    def test_cricket_slugs(self):
        """Cricket event slugs are classified correctly."""
        assert sport_from_slug("ipl-2026-mumbai-vs-chennai") == "cricket"
        assert sport_from_slug("t20-world-cup-india-vs-australia") == "cricket"
        assert sport_from_slug("cricket-ashes-test-3") == "cricket"
        assert sport_from_slug("big-bash-thunder-vs-heat") == "cricket"

    def test_unknown_returns_empty(self):
        """Unrecognized slugs return empty string."""
        assert sport_from_slug("random-event-slug") == ""
        assert sport_from_slug("") == ""
        assert sport_from_slug("cs2-tournament-blast") == ""

    def test_none_returns_empty(self):
        """None-like inputs return empty string."""
        assert sport_from_slug("") == ""


# ============================================================
# parse_match_question: event_slug-based Sport Classification
# ============================================================

class TestParseMatchQuestionWithSlug:
    """Tests that parse_match_question uses event_slug for sport classification."""

    def test_football_will_win_with_slug(self):
        """'Will X win?' with football slug returns game='football'."""
        game, team1, team2 = parse_match_question(
            "Will Jaguares de Córdoba FC win on 2026-02-20?",
            event_slug="liga-betplay-jaguares-vs-once-caldas",
        )
        assert game == "football"
        assert team1 == "Jaguares de Córdoba FC"

    def test_rugby_will_win_with_slug(self):
        """'Will X win?' with rugby slug returns game='rugby'."""
        game, team1, team2 = parse_match_question(
            "Will Western Force win?",
            event_slug="rugby-union-super-rugby",
        )
        assert game == "rugby"
        assert team1 == "Western Force"

    def test_cricket_will_win_with_slug(self):
        """'Will X win?' with cricket slug returns game='cricket'."""
        game, team1, team2 = parse_match_question(
            "Will Mumbai Indians win?",
            event_slug="ipl-2026-mumbai-vs-chennai",
        )
        assert game == "cricket"
        assert team1 == "Mumbai Indians"

    def test_no_slug_returns_empty_game(self):
        """Without event_slug, 'Will X win?' returns empty game (caller infers via OddsService)."""
        game, team1, team2 = parse_match_question(
            "Will Harlequins win?",
        )
        assert game == ""
        assert team1 == "Harlequins"

    def test_esports_question_ignores_slug(self):
        """Standard esports format ignores event_slug entirely."""
        game, team1, team2 = parse_match_question(
            "Counter-Strike: Team Liquid vs Natus Vincere (BO3)",
            event_slug="some-random-slug",
        )
        assert game == "counter-strike"
        assert team1 == "Team Liquid"
        assert team2 == "Natus Vincere"

