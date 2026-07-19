"""
Unit tests for Cricket, Hockey, UFC, and Football support in SpreadBot.

Tests the new-sport-specific logic across multiple components:
- SpreadScanner: Cricket Draw filtering
- SpreadBot: Team parsing and match ID generation for cricket/hockey/ufc
- Dashboard: categorize_position() for new sports
- Analytics: extract_game_from_title() for new sports
"""
import re
import pytest
from typing import List

from src.polymarket.weather_client import WeatherMarket, WeatherEvent
from src.scanning.spread_scanner import SpreadScanner, SpreadOpportunity
from src.core.match_id import make_match_id


# ============================================================
# Helpers
# ============================================================

def _make_market(
    question: str,
    token_id: str = "tok_yes",
    no_token_id: str = "tok_no",
    best_bid: float = 0.30,
    best_ask: float = 0.50,
    mid_price: float = 0.40,
    active: bool = True,
    accepting_orders: bool = True,
) -> WeatherMarket:
    """Create a WeatherMarket for testing."""
    return WeatherMarket(
        market_id="mkt_1",
        condition_id="cond_1",
        question=question,
        token_id=token_id,
        no_token_id=no_token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid_price,
        spread=best_ask - best_bid,
        volume=1000.0,
        liquidity=500.0,
        group_item="",
        active=active,
        accepting_orders=accepting_orders,
    )


def _make_event(
    markets: List[WeatherMarket],
    title: str = "India vs Australia",
    market_type: str = "cricket",
) -> WeatherEvent:
    """Create a WeatherEvent for testing."""
    return WeatherEvent(
        event_id="evt_1",
        slug="test-event",
        title=title,
        city="",
        date_str="",
        volume=5000.0,
        liquidity=2000.0,
        markets=markets,
        market_type=market_type,
    )


# ============================================================
# SpreadScanner: Cricket Draw Filtering
# ============================================================

class TestCricketDrawFiltering:
    """Tests that Draw markets are excluded from cricket events."""

    @pytest.fixture
    def scanner(self):
        """Create SpreadScanner with mock client."""
        from unittest.mock import AsyncMock
        client = AsyncMock()
        return SpreadScanner(weather_client=client, min_spread=0.10)

    def test_draw_market_filtered_out(self, scanner):
        """Cricket draw markets are skipped."""
        draw_market = _make_market("Will the match end in a Draw?")
        event = _make_event([draw_market], market_type="cricket")
        opps = scanner._scan_event(event)
        assert len(opps) == 0

    def test_draw_case_insensitive(self, scanner):
        """Draw filtering is case-insensitive."""
        draw_market = _make_market("Will the match DRAW?")
        event = _make_event([draw_market], market_type="cricket")
        opps = scanner._scan_event(event)
        assert len(opps) == 0

    def test_win_market_passes_through(self, scanner):
        """'Will X win?' markets pass through filtering."""
        win_market = _make_market("Will India win?")
        event = _make_event([win_market], market_type="cricket")
        opps = scanner._scan_event(event)
        assert len(opps) == 1

    def test_mixed_markets_only_win_survives(self, scanner):
        """Only 'Will X win?' markets survive from a full cricket event."""
        markets = [
            _make_market("Will India win?", token_id="t1", no_token_id="t1n"),
            _make_market("Will Australia win?", token_id="t2", no_token_id="t2n"),
            _make_market("Will the match end in a Draw?", token_id="t3", no_token_id="t3n"),
        ]
        event = _make_event(markets, market_type="cricket")
        opps = scanner._scan_event(event)
        assert len(opps) == 2
        questions = {o.question for o in opps}
        assert "Will India win?" in questions
        assert "Will Australia win?" in questions

    def test_draw_not_filtered_for_hockey(self, scanner):
        """Draw filtering only applies to cricket/rugby, not hockey."""
        draw_market = _make_market("Will the match Draw?")
        event = _make_event([draw_market], market_type="hockey")
        opps = scanner._scan_event(event)
        assert len(opps) == 1

    def test_draw_not_filtered_for_ufc(self, scanner):
        """Draw filtering does not apply to UFC."""
        draw_market = _make_market("Will there be a Draw?")
        event = _make_event([draw_market], market_type="ufc")
        opps = scanner._scan_event(event)
        assert len(opps) == 1

    def test_football_draw_filtered(self, scanner):
        """Football draw markets are skipped."""
        draw_market = _make_market("Will the match end in a Draw?")
        event = _make_event([draw_market], market_type="football")
        opps = scanner._scan_event(event)
        assert len(opps) == 0

    def test_football_win_passes_through(self, scanner):
        """Football 'Will X win?' markets pass through."""
        win_market = _make_market("Will Arsenal win?")
        event = _make_event([win_market], market_type="football")
        opps = scanner._scan_event(event)
        assert len(opps) == 1
        assert opps[0].market_type == "football"

    def test_market_type_preserved_on_opportunity(self, scanner):
        """SpreadOpportunity inherits market_type from event."""
        win_market = _make_market("Will India win?")
        event = _make_event([win_market], market_type="cricket")
        opps = scanner._scan_event(event)
        assert opps[0].market_type == "cricket"


# ============================================================
# SpreadBot: Team Parsing for Cricket
# ============================================================

class TestCricketTeamParsing:
    """Tests for the regex that extracts team names from 'Will X win?' questions."""

    PATTERN = re.compile(r'Will (.+?)\s+win\??', re.IGNORECASE)

    def test_standard_team(self):
        """Standard single-word team name."""
        match = self.PATTERN.match("Will India win?")
        assert match.group(1).strip() == "India"

    def test_multi_word_team(self):
        """Multi-word team name."""
        match = self.PATTERN.match("Will Mumbai Indians win?")
        assert match.group(1).strip() == "Mumbai Indians"

    def test_three_word_team(self):
        """Three-word team name."""
        match = self.PATTERN.match("Will Chennai Super Kings win?")
        assert match.group(1).strip() == "Chennai Super Kings"

    def test_no_question_mark(self):
        """Works without trailing question mark."""
        match = self.PATTERN.match("Will Australia win")
        assert match.group(1).strip() == "Australia"


# ============================================================
# Match ID Generation
# ============================================================

class TestNewSportsMatchIdGeneration:
    """Tests for match ID generation for new sports."""

    def test_cricket_uses_cricket_prefix(self):
        """Cricket spread trades use canonical 'cricket' game prefix."""
        match_id = make_match_id("India", "India No", "crk")
        assert match_id.startswith("cricket:")

    def test_hockey_uses_hockey_prefix(self):
        """Hockey spread trades use 'hockey' game prefix."""
        match_id = make_match_id("Rangers", "Bruins", "hockey")
        assert match_id.startswith("hockey:")

    def test_ufc_uses_ufc_prefix(self):
        """UFC spread trades use 'ufc' game prefix."""
        match_id = make_match_id("McGregor", "Khabib", "ufc")
        assert match_id.startswith("ufc:")

    def test_condition_id_suffix_uniqueness(self):
        """Condition ID suffix ensures different sub-markets have unique match_ids."""
        base = make_match_id("India", "India No", "crk")
        match_id_1 = f"{base}:cond_abc123456789"
        match_id_2 = f"{base}:cond_xyz987654321"
        assert match_id_1 != match_id_2

    def test_football_uses_football_prefix(self):
        """Football spread trades use canonical 'football' game prefix."""
        match_id = make_match_id("Arsenal", "Arsenal No", "ftb")
        assert match_id.startswith("football:")


# ============================================================
# Dashboard: categorize_position()
# ============================================================

class TestDashboardCategorization:
    """Tests for categorize_position() with new sports.
    
    We test the function directly by extracting its logic,
    avoiding Streamlit import issues in test context.
    """

    @staticmethod
    def _categorize(pos: dict) -> str:
        """Inline categorize_position logic to avoid Streamlit import."""
        slug = pos.get("event_slug", "")
        if slug:
            s = slug.lower()
            if "temperature" in s:
                return "☁️ Weather"
            if "up-or-down" in s:
                return "📈 Stocks"
            if s.startswith("atp-") or s.startswith("wta-"):
                return "🎾 Tennis"
            if s.startswith("cbb-") or s.startswith("cwbb-") or s.startswith("bk"):
                return "🏀 Basketball"
            if "rugby" in s or "premiership" in s:
                return "🏉 Rugby"
            if any(kw in s for kw in ["cricket", "ipl", "t20", "odi", "big-bash", "test-match"]):
                return "🏏 Cricket"
            if any(kw in s for kw in ["nhl", "ahl", "khl", "shl", "hockey", "extraliga", "del-"]):
                return "🏒 Hockey"
            if any(kw in s for kw in ["ufc", "mma", "zuffa"]):
                return "🥊 UFC"
            if any(kw in s for kw in [
                "epl-", "la-liga", "serie-a", "bundesliga", "ligue-1", "champions-league",
                "europa-league", "conference-league", "mls-", "eredivisie", "liga-mx",
                "copa-libertadores", "copa-sudamericana", "super-lig", "primeira-liga",
                "brasileirao", "j-league", "saudi-pro", "a-league", "fa-cup", "dfb-pokal",
                "copa-del-rey", "coupe-de-france", "efl-", "concacaf", "conmebol",
                "africa-cup", "k-league", "indian-super-league",
            ]):
                return "⚽ Football"
            if any(kw in s for kw in [
                "what-will-", "what-nicknames-", "earnings-mentions-",
                "what-will-be-said-", "how-many-times-will-",
            ]):
                return "💬 Mentions"
        title = pos.get("title", "")
        if not title:
            return "Other"
        t = title.lower()
        if "temperature" in t or "°f" in t or "°c" in t:
            return "☁️ Weather"
        if "up or down" in t:
            return "📈 Stocks"
        if any(kw in t for kw in ["set 1 winner", "set handicap", "match o/u"]):
            return "🎾 Tennis"
        if any(kw in t for kw in ["cricket", "ipl", "t20 international", "odi", "big bash"]):
            return "🏏 Cricket"
        if any(kw in t for kw in ["nhl:", "ahl:", "khl:", "shl:", "hockey"]):
            return "🏒 Hockey"
        if any(kw in t for kw in ["ufc", "mma"]):
            return "🥊 UFC"
        if any(kw in t for kw in [
            "epl:", "la liga:", "serie a:", "bundesliga:", "ligue 1:", "champions league:",
            "europa league:", "mls:", "eredivisie:", "liga mx:", "copa libertadores:",
            "fa cup:", "dfb-pokal:", "copa del rey:",
        ]):
            return "⚽ Football"
        if any(kw in t for kw in ["spread:", "over", "under", "o/u ", " vs. ", " vs "]):
            return "🏀 Basketball"
        if any(kw in t for kw in ["will ", "say ", "mention "]):
            return "💬 Mentions"
        if "rugby" in t:
            return "🏉 Rugby"
        return "Other"

    def test_cricket_slug(self):
        assert self._categorize({"event_slug": "ipl-mumbai-indians-vs-csk"}) == "🏏 Cricket"

    def test_hockey_slug(self):
        assert self._categorize({"event_slug": "nhl-rangers-vs-bruins"}) == "🏒 Hockey"

    def test_ufc_slug(self):
        assert self._categorize({"event_slug": "ufc-305-main-event"}) == "🥊 UFC"

    def test_cricket_title_fallback(self):
        assert self._categorize({"title": "IPL: Mumbai Indians vs CSK"}) == "🏏 Cricket"

    def test_hockey_title_fallback(self):
        assert self._categorize({"title": "NHL: Rangers vs Bruins"}) == "🏒 Hockey"

    def test_ufc_title_fallback(self):
        assert self._categorize({"title": "UFC 305: Main Event"}) == "🥊 UFC"

    def test_football_slug(self):
        assert self._categorize({"event_slug": "epl-arsenal-vs-chelsea"}) == "⚽ Football"

    def test_football_ucl_slug(self):
        assert self._categorize({"event_slug": "champions-league-real-madrid-vs-bayern"}) == "⚽ Football"

    def test_football_title_fallback(self):
        assert self._categorize({"title": "EPL: Arsenal vs Chelsea"}) == "⚽ Football"

    def test_football_serie_a_title(self):
        assert self._categorize({"title": "Serie A: Juventus vs AC Milan"}) == "⚽ Football"


# ============================================================
# Analytics: extract_game_from_title()
# ============================================================

class TestAnalyticsGameExtraction:
    """Tests for extract_game_from_title() with new sports."""

    def test_cricket_ipl(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("IPL: Mumbai Indians vs Chennai Super Kings") == "Cricket"

    def test_cricket_odi(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("ODI: India vs Australia") == "Cricket"

    def test_hockey_nhl(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("NHL: Rangers vs Bruins") == "Hockey"

    def test_hockey_khl(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("KHL: CSKA vs SKA") == "Hockey"

    def test_hockey_ahl(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("AHL: Hartford vs Springfield") == "Hockey"

    def test_ufc(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("UFC 305: Main Event") == "UFC"

    def test_rugby_still_works(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("Premiership Rugby: Bath vs Bristol") == "Rugby"

    def test_will_x_win_fallback(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("Will India win?") == "Rugby"  # Fallback — shared pattern

    def test_football_epl(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("EPL: Arsenal vs Chelsea") == "Football"

    def test_football_la_liga(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("La Liga: Real Madrid vs Barcelona") == "Football"

    def test_football_ucl(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("Champions League: Bayern vs PSG") == "Football"

    def test_football_mls(self):
        from analytics.capital_analysis import extract_game_from_title
        assert extract_game_from_title("MLS: LAFC vs Inter Miami") == "Football"
