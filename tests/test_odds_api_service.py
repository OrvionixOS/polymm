"""Tests for src/services/odds_api_service.py — API fetch, parse, and fair value computation."""
import pytest
import statistics
from unittest.mock import AsyncMock, MagicMock, patch
from src.services.odds_api_service import (
    OddsApiService, OddsRecord, SPORT_CONFIGS, SPORT_KEY_TO_GAME,
)


# ── Sample API response fixtures ───────────────────────────────────────

def _make_event(home="Man City", away="Arsenal", event_id="abc123",
                bookmakers=None, commence_time="2026-03-05T15:00:00Z"):
    return {
        "id": event_id,
        "home_team": home,
        "away_team": away,
        "commence_time": commence_time,
        "bookmakers": bookmakers or [],
    }


def _make_bookmaker(key="source_a", markets=None):
    return {
        "key": key,
        "title": key.title(),
        "markets": markets or [],
    }


def _make_market(key="h2h", outcomes=None):
    return {
        "key": key,
        "outcomes": outcomes or [],
    }


def _make_outcome(name="Man City", price=1.50, point=None):
    o = {"name": name, "price": price}
    if point is not None:
        o["point"] = point
    return o


# ── Config tests ─────────────────────────────────────────────────────────

class TestSportConfigs:
    """Verify sport key → game mappings are consistent."""

    def test_all_sport_keys_have_game_mapping(self):
        for key in SPORT_CONFIGS:
            assert key in SPORT_KEY_TO_GAME, f"Missing game mapping for {key}"

    def test_football_keys_map_to_football(self):
        for key in SPORT_CONFIGS:
            if key.startswith("soccer_"):
                assert SPORT_KEY_TO_GAME[key] == "football"

    def test_basketball_keys_map_correctly(self):
        """ALL basketball keys → basketball (matches sport_from_slug)."""
        for key in SPORT_CONFIGS:
            if key.startswith("basketball_"):
                assert SPORT_KEY_TO_GAME[key] == "basketball", f"{key} should map to basketball"

    def test_hockey_keys_map_to_hockey(self):
        for key in SPORT_CONFIGS:
            if key.startswith("icehockey_"):
                assert SPORT_KEY_TO_GAME[key] == "hockey"


# ── Parsing tests ────────────────────────────────────────────────────────

class TestParseEvent:
    """Test _parse_event with various market types."""

    def _make_service(self):
        with patch.dict("os.environ", {"THE_ODDS_API_KEY": "test"}):
            return OddsApiService()

    def test_parse_h2h_2way(self):
        """Basketball — 2-way h2h (no draw)."""
        svc = self._make_service()
        event = _make_event(
            home="Duke", away="UNC",
            bookmakers=[
                _make_bookmaker("source_a", [
                    _make_market("h2h", [
                        _make_outcome("Duke", 1.50),
                        _make_outcome("UNC", 2.60),
                    ]),
                ]),
                _make_bookmaker("source_k", [
                    _make_market("h2h", [
                        _make_outcome("Duke", 1.52),
                        _make_outcome("UNC", 2.55),
                    ]),
                ]),
            ],
        )
        records = svc._parse_event(event, "basketball_ncaab", "ncaab")
        assert len(records) == 1
        r = records[0]
        assert r.market_type == "h2h"
        assert r.outcome1_name == "Duke"
        assert r.outcome2_name == "UNC"
        assert r.outcome_draw_name is None
        assert r.odds_draw is None
        assert r.fair_prob1 > 0
        assert r.fair_prob2 > 0
        assert abs(r.fair_prob1 + r.fair_prob2 - 100) < 0.5
        assert r.bookmaker_count == 2

    def test_parse_h2h_3way(self):
        """Football — 3-way h2h (with draw)."""
        svc = self._make_service()
        event = _make_event(
            home="Man City", away="Arsenal",
            bookmakers=[
                _make_bookmaker("source_a", [
                    _make_market("h2h", [
                        _make_outcome("Man City", 1.70),
                        _make_outcome("Draw", 3.80),
                        _make_outcome("Arsenal", 4.50),
                    ]),
                ]),
            ],
        )
        records = svc._parse_event(event, "soccer_epl", "football")
        h2h = [r for r in records if r.market_type == "h2h"]
        assert len(h2h) == 1
        r = h2h[0]
        assert r.outcome_draw_name == "Draw"
        assert r.odds_draw is not None
        assert r.fair_prob_draw is not None
        assert abs(r.fair_prob1 + r.fair_prob2 + r.fair_prob_draw - 100) < 0.5

    def test_parse_spreads(self):
        """Spreads with point values."""
        svc = self._make_service()
        event = _make_event(
            home="Man City", away="Arsenal",
            bookmakers=[
                _make_bookmaker("source_a", [
                    _make_market("spreads", [
                        _make_outcome("Man City", 1.90, point=-1.5),
                        _make_outcome("Arsenal", 1.95, point=1.5),
                    ]),
                ]),
            ],
        )
        records = svc._parse_event(event, "soccer_epl", "football")
        spreads = [r for r in records if r.market_type == "spreads"]
        assert len(spreads) == 1
        r = spreads[0]
        assert r.line == 1.5  # abs value — both sides grouped
        assert r.outcome1_name == "Man City"
        assert r.outcome2_name == "Arsenal"
        assert r.fair_prob1 > 0
        assert r.fair_prob2 > 0

    def test_parse_totals(self):
        """Totals with O/U."""
        svc = self._make_service()
        event = _make_event(
            home="Man City", away="Arsenal",
            bookmakers=[
                _make_bookmaker("source_a", [
                    _make_market("totals", [
                        _make_outcome("Over", 1.85, point=2.5),
                        _make_outcome("Under", 2.00, point=2.5),
                    ]),
                ]),
            ],
        )
        records = svc._parse_event(event, "soccer_epl", "football")
        totals = [r for r in records if r.market_type == "totals"]
        assert len(totals) == 1
        r = totals[0]
        assert r.line == 2.5
        assert r.outcome1_name == "Over"
        assert r.outcome2_name == "Under"

    def test_parse_btts(self):
        """BTTS with Yes/No."""
        svc = self._make_service()
        event = _make_event(
            home="Man City", away="Arsenal",
            bookmakers=[
                _make_bookmaker("source_a", [
                    _make_market("btts", [
                        _make_outcome("Yes", 1.70),
                        _make_outcome("No", 2.10),
                    ]),
                ]),
            ],
        )
        records = svc._parse_event(event, "soccer_epl", "football")
        btts = [r for r in records if r.market_type == "btts"]
        assert len(btts) == 1
        r = btts[0]
        assert r.outcome1_name == "Yes"
        assert r.outcome2_name == "No"
        assert r.line == 0

    def test_parse_multiple_bookmakers_median(self):
        """Median odds from multiple bookmakers."""
        svc = self._make_service()
        event = _make_event(
            home="Duke", away="UNC",
            bookmakers=[
                _make_bookmaker("bm1", [_make_market("h2h", [
                    _make_outcome("Duke", 1.40), _make_outcome("UNC", 3.00),
                ])]),
                _make_bookmaker("bm2", [_make_market("h2h", [
                    _make_outcome("Duke", 1.50), _make_outcome("UNC", 2.60),
                ])]),
                _make_bookmaker("bm3", [_make_market("h2h", [
                    _make_outcome("Duke", 1.60), _make_outcome("UNC", 2.40),
                ])]),
            ],
        )
        records = svc._parse_event(event, "basketball_ncaab", "ncaab")
        r = records[0]
        # Median of [1.40, 1.50, 1.60] = 1.50
        assert r.odds1 == 1.50
        # Median of [3.00, 2.60, 2.40] = 2.60
        assert r.odds2 == 2.60
        assert r.bookmaker_count == 3

    def test_parse_h2h_h1_3way(self):
        """Football — 1st half 3-way moneyline (h2h_h1)."""
        svc = self._make_service()
        event = _make_event(
            home="Man City", away="Arsenal",
            bookmakers=[
                _make_bookmaker("source_a", [
                    _make_market("h2h_h1", [
                        _make_outcome("Man City", 2.80),
                        _make_outcome("Draw", 2.30),
                        _make_outcome("Arsenal", 3.60),
                    ]),
                ]),
            ],
        )
        records = svc._parse_event(event, "soccer_epl", "football")
        h1 = [r for r in records if r.market_type == "h2h_h1"]
        assert len(h1) == 1
        r = h1[0]
        assert r.market_type == "h2h_h1"
        assert r.outcome_draw_name == "Draw"
        assert r.odds_draw is not None
        assert r.fair_prob_draw is not None
        assert abs(r.fair_prob1 + r.fair_prob2 + r.fair_prob_draw - 100) < 0.5
        assert r.line == 0

    def test_h2h_and_h2h_h1_are_separate(self):
        """h2h and h2h_h1 from same event produce distinct records."""
        svc = self._make_service()
        event = _make_event(
            home="Man City", away="Arsenal",
            bookmakers=[
                _make_bookmaker("source_a", [
                    _make_market("h2h", [
                        _make_outcome("Man City", 1.70),
                        _make_outcome("Draw", 3.80),
                        _make_outcome("Arsenal", 4.50),
                    ]),
                    _make_market("h2h_h1", [
                        _make_outcome("Man City", 2.80),
                        _make_outcome("Draw", 2.30),
                        _make_outcome("Arsenal", 3.60),
                    ]),
                ]),
            ],
        )
        records = svc._parse_event(event, "soccer_epl", "football")
        types = [r.market_type for r in records]
        assert "h2h" in types
        assert "h2h_h1" in types
        assert types.count("h2h") == 1
        assert types.count("h2h_h1") == 1

    def test_parse_multiple_market_types_single_event(self):
        """One event can produce h2h + spreads + totals + btts + h2h_h1 records."""
        svc = self._make_service()
        event = _make_event(
            home="Man City", away="Arsenal",
            bookmakers=[
                _make_bookmaker("source_a", [
                    _make_market("h2h", [
                        _make_outcome("Man City", 1.70),
                        _make_outcome("Draw", 3.80),
                        _make_outcome("Arsenal", 4.50),
                    ]),
                    _make_market("spreads", [
                        _make_outcome("Man City", 1.90, point=-1.5),
                        _make_outcome("Arsenal", 1.95, point=1.5),
                    ]),
                    _make_market("totals", [
                        _make_outcome("Over", 1.85, point=2.5),
                        _make_outcome("Under", 2.00, point=2.5),
                    ]),
                    _make_market("btts", [
                        _make_outcome("Yes", 1.70),
                        _make_outcome("No", 2.10),
                    ]),
                    _make_market("h2h_h1", [
                        _make_outcome("Man City", 2.80),
                        _make_outcome("Draw", 2.30),
                        _make_outcome("Arsenal", 3.60),
                    ]),
                ]),
            ],
        )
        records = svc._parse_event(event, "soccer_epl", "football")
        types = {r.market_type for r in records}
        assert "h2h" in types
        assert "spreads" in types
        assert "totals" in types
        assert "btts" in types
        assert "h2h_h1" in types

    def test_parse_multiple_spread_lines(self):
        """Multiple spread lines (e.g., -0.5, -1.5) create separate records."""
        svc = self._make_service()
        event = _make_event(
            home="Man City", away="Arsenal",
            bookmakers=[
                _make_bookmaker("source_a", [
                    _make_market("spreads", [
                        _make_outcome("Man City", 1.50, point=-0.5),
                        _make_outcome("Arsenal", 2.60, point=0.5),
                        _make_outcome("Man City", 1.90, point=-1.5),
                        _make_outcome("Arsenal", 1.95, point=1.5),
                    ]),
                ]),
            ],
        )
        records = svc._parse_event(event, "soccer_epl", "football")
        spreads = [r for r in records if r.market_type == "spreads"]
        assert len(spreads) == 2
        lines = {r.line for r in spreads}
        assert -0.5 in lines or 0.5 in lines  # depends on which side gets picked
        assert -1.5 in lines or 1.5 in lines

    def test_skip_invalid_odds(self):
        """Odds <= 1 are skipped."""
        svc = self._make_service()
        event = _make_event(
            home="Duke", away="UNC",
            bookmakers=[
                _make_bookmaker("bm1", [_make_market("h2h", [
                    _make_outcome("Duke", 0.95),  # Invalid
                    _make_outcome("UNC", 2.60),
                ])]),
            ],
        )
        records = svc._parse_event(event, "basketball_ncaab", "ncaab")
        assert len(records) == 0  # Can't build record without both sides


# ── OddsRecord tests ─────────────────────────────────────────────────────

class TestOddsRecord:
    """Test OddsRecord serialization."""

    def test_to_dict_basic(self):
        r = OddsRecord(
            match_id="football:arsenal:vs:man city",
            source="the-odds-api",
            sport="soccer_epl",
            team1="arsenal", team2="man city",
            market_type="h2h", line=0,
            outcome1_name="Arsenal", outcome2_name="Man City",
            outcome_draw_name="Draw",
            odds1=4.5, odds2=1.7, odds_draw=3.8,
            fair_prob1=20.0, fair_prob2=55.0, fair_prob_draw=25.0,
            event_id="abc123", commence_time="2026-03-05T15:00:00Z",
            bookmaker_count=5,
        )
        d = r.to_dict()
        assert d["match_id"] == "football:arsenal:vs:man city"
        assert d["market_type"] == "h2h"
        assert d["outcome_draw_name"] == "Draw"
        assert d["odds_draw"] == 3.8
        assert d["fair_prob_draw"] == 25.0

    def test_to_dict_no_draw(self):
        r = OddsRecord(
            match_id="ncaab:duke:vs:unc",
            source="the-odds-api",
            sport="basketball_ncaab",
            team1="duke", team2="unc",
            market_type="h2h", line=0,
            outcome1_name="Duke", outcome2_name="UNC",
            outcome_draw_name=None,
            odds1=1.5, odds2=2.6, odds_draw=None,
            fair_prob1=63.0, fair_prob2=37.0, fair_prob_draw=None,
            event_id="xyz", commence_time=None,
            bookmaker_count=3,
        )
        d = r.to_dict()
        assert "outcome_draw_name" not in d
        assert "odds_draw" not in d
        assert "fair_prob_draw" not in d
        assert "commence_time" not in d

    def test_to_dict_with_line(self):
        r = OddsRecord(
            match_id="football:arsenal:vs:man city",
            source="the-odds-api",
            sport="soccer_epl",
            team1="arsenal", team2="man city",
            market_type="spreads", line=-1.5,
            outcome1_name="Arsenal", outcome2_name="Man City",
            outcome_draw_name=None,
            odds1=1.9, odds2=1.95, odds_draw=None,
            fair_prob1=49.0, fair_prob2=51.0, fair_prob_draw=None,
            event_id="abc123", commence_time="2026-03-05T15:00:00Z",
            bookmaker_count=5,
        )
        d = r.to_dict()
        assert d["line"] == -1.5
        assert d["market_type"] == "spreads"


# ── Fair value tests ─────────────────────────────────────────────────────

class TestFairValues:
    """Verify fair value computation logic."""

    def _make_service(self):
        with patch.dict("os.environ", {"THE_ODDS_API_KEY": "test"}):
            return OddsApiService()

    def test_2way_probs_sum_to_100(self):
        """2-way fair probs should sum to ~100."""
        svc = self._make_service()
        event = _make_event(
            home="Duke", away="UNC",
            bookmakers=[
                _make_bookmaker("bm1", [_make_market("h2h", [
                    _make_outcome("Duke", 1.50), _make_outcome("UNC", 2.60),
                ])]),
            ],
        )
        records = svc._parse_event(event, "basketball_ncaab", "ncaab")
        r = records[0]
        assert abs(r.fair_prob1 + r.fair_prob2 - 100) < 1.0

    def test_3way_probs_sum_to_100(self):
        """3-way fair probs should sum to ~100."""
        svc = self._make_service()
        event = _make_event(
            home="Man City", away="Arsenal",
            bookmakers=[
                _make_bookmaker("bm1", [_make_market("h2h", [
                    _make_outcome("Man City", 1.70),
                    _make_outcome("Draw", 3.80),
                    _make_outcome("Arsenal", 4.50),
                ])]),
            ],
        )
        records = svc._parse_event(event, "soccer_epl", "football")
        r = records[0]
        assert abs(r.fair_prob1 + r.fair_prob2 + r.fair_prob_draw - 100) < 1.0

    def test_favorite_gets_higher_prob(self):
        """Lower odds = higher probability."""
        svc = self._make_service()
        event = _make_event(
            home="Duke", away="UNC",
            bookmakers=[
                _make_bookmaker("bm1", [_make_market("h2h", [
                    _make_outcome("Duke", 1.30),  # Heavy favorite
                    _make_outcome("UNC", 3.50),
                ])]),
            ],
        )
        records = svc._parse_event(event, "basketball_ncaab", "ncaab")
        r = records[0]
        assert r.fair_prob1 > r.fair_prob2
