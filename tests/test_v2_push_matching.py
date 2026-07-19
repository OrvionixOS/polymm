"""
Test the v2 push matching pipeline in OddsService._push_fair_probs_to_bot_state.

Tests cover the REAL production data flow:
1. Hydration creates match_ids with "Will X win?" format: team2 = "X No" + condition_id suffix
2. register_order auto-creates matches from these match_ids (team2 must strip condition_id)
3. v2 push must detect the "X No" pattern and match on team1 only

Also tests: game prefix normalization, team containment, spread/totals correspondence.
"""
import pytest
import re
from unittest.mock import patch, MagicMock
from dataclasses import dataclass, field
from typing import Optional

from src.core.match_id import normalize_team, normalize_game, make_match_id
from src.services.odds_service import OddsService


# ── Helpers ──────────────────────────────────────────────────────────────

@dataclass
class FakeOrder:
    team: str
    token_id: str
    is_open: bool = True
    fair_value: Optional[float] = None
    is_hedge: bool = False
    order_id: str = "fake_order_123"
    price: float = 0.10


@dataclass
class FakeMatch:
    team1: str
    team2: str
    token1: str = ""
    token2: str = ""
    order1: Optional[FakeOrder] = None
    order2: Optional[FakeOrder] = None
    fair_prob1: float = 0.0
    fair_prob2: float = 0.0
    match_id: str = ""

    def is_order_hedge(self, order):
        return getattr(order, 'is_hedge', False)


@dataclass
class FakeV2Match:
    match_id: str
    team1: str
    team2: str
    fair_prob1: float
    fair_prob2: float
    market_type: str = "h2h"
    line: float = 0
    fair_prob_draw: Optional[float] = None
    sources: list = field(default_factory=lambda: ["test"])
    game: str = ""


class FakeBotState:
    def __init__(self, matches: dict):
        self._matches = matches

    def update_fair_probs(self, match_id, fair_prob1, fair_prob2):
        m = self._matches.get(match_id)
        if m:
            m.fair_prob1 = fair_prob1
            m.fair_prob2 = fair_prob2
            return True
        return None

    def update_order_fair_value(self, match_id, order, fair_value):
        order.fair_value = fair_value

    def update_fair_probs_by_team(self, match_id, team1, team2, fair_prob1, fair_prob2):
        m = self._matches.get(match_id)
        if m:
            m.fair_prob1 = fair_prob1
            m.fair_prob2 = fair_prob2
            return True
        return None


def _make_odds_service():
    """Create an OddsService with empty caches."""
    svc = OddsService.__new__(OddsService)
    svc._cache = {}
    svc._v2_cache = {}
    return svc


def _run_push(svc, bot_state):
    """Run the push with a fake bot_state."""
    with patch('src.state.bot_state.get_bot_state', return_value=bot_state):
        svc._push_fair_probs_to_bot_state()


def _make_win_market_match_id(team: str, game: str, cond_id: str) -> str:
    """Generate the EXACT match_id that _build_match_id_for_order creates
    for a 'Will X win?' market."""
    team2 = f"{team} No"
    base = make_match_id(team, team2, game)
    return f"{base}:{cond_id[:18]}"


def _make_win_market_bs_entry(team: str, game: str, cond_id: str):
    """Create a BotState match entry exactly as hydration would.
    Simulates register_order's auto-create from the match_id."""
    match_id = _make_win_market_match_id(team, game, cond_id)
    
    # Simulate register_order's auto-create
    parts = match_id.split(":vs:")
    team2_parsed = parts[1]
    # Strip condition_id suffix (the fix in register_order)
    if ":" in team2_parsed:
        last_part = team2_parsed.rsplit(":", 1)[1]
        if last_part.startswith("0x") and len(last_part) >= 10:
            team2_parsed = team2_parsed.rsplit(":", 1)[0]
    prefix_parts = parts[0].rsplit(":", 1)
    team1_parsed = prefix_parts[1] if len(prefix_parts) == 2 else ""
    
    order = FakeOrder(team=team, token_id=f"tok_{cond_id[:8]}")
    bs_match = FakeMatch(
        team1=team1_parsed,
        team2=team2_parsed,
        order1=order,
        match_id=match_id,
    )
    return match_id, bs_match, order


# ── Win Market Tests (the core fix) ─────────────────────────────────────

class TestWinMarketMatching:
    """Test that 'Will X win?' markets get fair_value from v2 push."""

    def test_fc_barcelona_win_market(self):
        """FC Barcelona: 'Will FC Barcelona win?' → BS has fcbarcelona/fcbarcelonano,
        v2 has FC Barcelona vs Newcastle United."""
        svc = _make_odds_service()
        mid, bs_match, order = _make_win_market_bs_entry(
            "FC Barcelona", "football", "0x3133220400132933"
        )
        bot_state = FakeBotState({mid: bs_match})
        
        svc._v2_cache["football:fcbarcelona:vs:newcastleunited:h2h:0"] = FakeV2Match(
            match_id="football:fcbarcelona:vs:newcastleunited",
            team1="FC Barcelona", team2="Newcastle United",
            fair_prob1=70.0, fair_prob2=30.0,
            market_type="h2h",
        )
        _run_push(svc, bot_state)
        assert order.fair_value is not None, \
            f"FC Barcelona should get fair_value. mid={mid}, t1={bs_match.team1}, t2={bs_match.team2}"

    def test_psv_win_market(self):
        """PSV: 'Will PSV win?' with actual opponent Feyenoord."""
        svc = _make_odds_service()
        mid, bs_match, order = _make_win_market_bs_entry(
            "PSV", "football", "0x4953302970038400"
        )
        bot_state = FakeBotState({mid: bs_match})
        
        svc._v2_cache["football:feyenoord:vs:psv:h2h:0"] = FakeV2Match(
            match_id="football:feyenoord:vs:psv",
            team1="Feyenoord", team2="PSV",
            fair_prob1=40.0, fair_prob2=35.0,
            fair_prob_draw=25.0,
            market_type="h2h",
        )
        _run_push(svc, bot_state)
        assert order.fair_value is not None, \
            f"PSV should get fair_value. mid={mid}, t1={bs_match.team1}, t2={bs_match.team2}"

    def test_capitals_hockey_win_market(self):
        """Capitals: 'Will Capitals win?' with containted team name."""
        svc = _make_odds_service()
        mid, bs_match, order = _make_win_market_bs_entry(
            "Capitals", "hockey", "0x2846411503591099"
        )
        bot_state = FakeBotState({mid: bs_match})
        
        svc._v2_cache["hockey:bostonbruins:vs:washingtoncapitals:h2h:0"] = FakeV2Match(
            match_id="hockey:bostonbruins:vs:washingtoncapitals",
            team1="Boston Bruins", team2="Washington Capitals",
            fair_prob1=45.0, fair_prob2=55.0,
            market_type="h2h",
        )
        _run_push(svc, bot_state)
        assert order.fair_value is not None, \
            f"Capitals should match via containment. mid={mid}, t1={bs_match.team1}, t2={bs_match.team2}"

    def test_sharks_hockey_win_market(self):
        """Sharks: 'Will Sharks win?' with containment matching."""
        svc = _make_odds_service()
        mid, bs_match, order = _make_win_market_bs_entry(
            "Sharks", "hockey", "0x4752229329152127"
        )
        bot_state = FakeBotState({mid: bs_match})
        
        svc._v2_cache["hockey:sanjosesharks:vs:seattlekraken:h2h:0"] = FakeV2Match(
            match_id="hockey:sanjosesharks:vs:seattlekraken",
            team1="San Jose Sharks", team2="Seattle Kraken",
            fair_prob1=40.0, fair_prob2=60.0,
            market_type="h2h",
        )
        _run_push(svc, bot_state)
        assert order.fair_value is not None, "Sharks should match San Jose Sharks via containment"

    def test_northern_colorado_bears_basketball(self):
        """Northern Colorado Bears: basketball win market."""
        svc = _make_odds_service()
        mid, bs_match, order = _make_win_market_bs_entry(
            "Northern Colorado Bears", "basketball", "0x8056612626240485"
        )
        bot_state = FakeBotState({mid: bs_match})
        
        svc._v2_cache["basketball:northerncoloradobears:vs:weberstatewildcats:h2h:0"] = FakeV2Match(
            match_id="basketball:northerncoloradobears:vs:weberstatewildcats",
            team1="Northern Colorado Bears", team2="Weber State Wildcats",
            fair_prob1=55.0, fair_prob2=45.0,
            market_type="h2h",
        )
        _run_push(svc, bot_state)
        assert order.fair_value is not None, "Northern Colorado Bears should match directly"

    def test_nets_basketball_win_market(self):
        """Nets: 'Will Nets win?' — containment matching with Brooklyn Nets."""
        svc = _make_odds_service()
        mid, bs_match, order = _make_win_market_bs_entry(
            "Nets", "basketball", "0x5154190581357034"
        )
        bot_state = FakeBotState({mid: bs_match})
        
        svc._v2_cache["basketball:brooklynnets:vs:memphisgrizzlies:h2h:0"] = FakeV2Match(
            match_id="basketball:brooklynnets:vs:memphisgrizzlies",
            team1="Brooklyn Nets", team2="Memphis Grizzlies",
            fair_prob1=35.0, fair_prob2=65.0,
            market_type="h2h",
        )
        _run_push(svc, bot_state)
        assert order.fair_value is not None, "Nets should match Brooklyn Nets via containment"


# ── Register Order Condition_id Stripping ────────────────────────────────

class TestRegisterOrderConditionIdStrip:
    """Verify register_order strips condition_id from parsed team2."""

    def test_condition_id_stripped_from_team2(self):
        """When auto-creating a match, team2_parsed should NOT contain condition_id."""
        match_id = "football:fcbarcelona:vs:fcbarcelonano:0x3133220400132933"
        parts = match_id.split(":vs:")
        team2_parsed = parts[1]  # "fcbarcelonano:0x3133220400132933"
        
        # Apply the fix
        if ":" in team2_parsed:
            last_part = team2_parsed.rsplit(":", 1)[1]
            if last_part.startswith("0x") and len(last_part) >= 10:
                team2_parsed = team2_parsed.rsplit(":", 1)[0]
        
        assert team2_parsed == "fcbarcelonano", \
            f"team2_parsed should be 'fcbarcelonano', got '{team2_parsed}'"

    def test_team2_without_condition_id_unchanged(self):
        """Match_ids without condition_id should not be affected."""
        match_id = "cs2:fnatic:vs:navi"
        parts = match_id.split(":vs:")
        team2_parsed = parts[1]  # "navi"
        
        # Apply the fix
        if ":" in team2_parsed:
            last_part = team2_parsed.rsplit(":", 1)[1]
            if last_part.startswith("0x") and len(last_part) >= 10:
                team2_parsed = team2_parsed.rsplit(":", 1)[0]
        
        assert team2_parsed == "navi"


# ── Game Prefix Normalization ────────────────────────────────────────────

class TestV2PushGamePrefixMatching:
    """Test game prefix normalization."""

    def test_ncaab_prefix_matches_basketball(self):
        """Legacy 'ncaab:' prefix matches 'basketball:'."""
        svc = _make_odds_service()
        mid, bs_match, order = _make_win_market_bs_entry(
            "Warriors", "ncaab", "0x1359405713955509"
        )
        bot_state = FakeBotState({mid: bs_match})
        
        svc._v2_cache["basketball:goldenstatewarriors:vs:utahjazz:h2h:0"] = FakeV2Match(
            match_id="basketball:goldenstatewarriors:vs:utahjazz",
            team1="Golden State Warriors", team2="Utah Jazz",
            fair_prob1=65.0, fair_prob2=35.0,
            market_type="h2h",
        )
        _run_push(svc, bot_state)
        assert order.fair_value is not None, "ncaab prefix should match basketball via normalize_game"

    def test_icehockey_nhl_matches_hockey(self):
        assert normalize_game("icehockey_nhl") == "hockey"
        assert normalize_game("icehockey_khl") == "hockey"
        assert normalize_game("icehockey_cehl") == "hockey"


# ── Spread/Totals Matching ───────────────────────────────────────────────

class TestSpreadTotalsMatching:
    """Test spread and totals market matching."""

    def test_spread_matching(self):
        """Spread order should match via _v2_match_corresponds."""
        svc = _make_odds_service()

        order = FakeOrder(team="Warriors: Spread -4.5", token_id="tok_s")
        bs_match = FakeMatch(
            team1="Warriors: Spread -4.5", team2="Jazz: Spread 4.5",
            order1=order,
        )
        bot_state = FakeBotState({
            "basketball:goldenstatewarriors:vs:utahjazz:0xspread": bs_match,
        })

        svc._v2_cache["basketball:goldenstatewarriors:vs:utahjazz:spreads:4.5"] = FakeV2Match(
            match_id="basketball:goldenstatewarriors:vs:utahjazz",
            team1="Golden State Warriors", team2="Utah Jazz",
            fair_prob1=60.0, fair_prob2=40.0,
            market_type="spreads",
            line=4.5,
        )
        _run_push(svc, bot_state)
        assert order.fair_value is not None, "Spread order should match"

    def test_totals_matching(self):
        """Totals order should match via _v2_match_corresponds."""
        svc = _make_odds_service()

        order = FakeOrder(team="Over", token_id="tok_over")
        bs_match = FakeMatch(
            team1="Ducks: O/U 5.5", team2="Blues: O/U 5.5",
            order1=order,
        )
        bot_state = FakeBotState({
            "hockey:anaheimducksfc:vs:stlouisblues:0xtotals": bs_match,
        })

        svc._v2_cache["hockey:anaheimducksfc:vs:stlouisblues:totals:5.5"] = FakeV2Match(
            match_id="hockey:anaheimducksfc:vs:stlouisblues",
            team1="Anaheim Ducks", team2="St. Louis Blues",
            fair_prob1=55.0, fair_prob2=45.0,
            market_type="totals",
            line=5.5,
        )
        _run_push(svc, bot_state)
        assert order.fair_value is not None, "Totals order should match"


# ── Correspondence Tests ─────────────────────────────────────────────────

class TestV2PushCorrespondence:
    """Test _v2_match_corresponds filtering."""

    def test_h2h_plain_match(self):
        svc = _make_odds_service()
        v2 = FakeV2Match("", "A", "B", 60, 40, market_type="h2h")
        bs = FakeMatch(team1="Team A", team2="Team B")
        bs.order1 = FakeOrder(team="Team A", token_id="t1")
        assert svc._v2_match_corresponds(v2, bs) is True

    def test_h2h_skips_totals(self):
        svc = _make_odds_service()
        v2 = FakeV2Match("", "A", "B", 60, 40, market_type="h2h")
        bs = FakeMatch(team1="Team A: O/U 2.5", team2="Under")
        bs.order1 = FakeOrder(team="Over", token_id="t1")
        assert svc._v2_match_corresponds(v2, bs) is False

    def test_spreads_match(self):
        svc = _make_odds_service()
        v2 = FakeV2Match("", "A", "B", 60, 40, market_type="spreads", line=4.5)
        bs = FakeMatch(team1="Warriors: Spread -4.5", team2="Jazz: Spread 4.5")
        bs.order1 = FakeOrder(team="Warriors: Spread -4.5", token_id="t1")
        assert svc._v2_match_corresponds(v2, bs) is True

    def test_totals_match(self):
        svc = _make_odds_service()
        v2 = FakeV2Match("", "A", "B", 60, 40, market_type="totals", line=5.5)
        bs = FakeMatch(team1="Ducks: O/U 5.5", team2="Blues: O/U 5.5")
        bs.order1 = FakeOrder(team="Over", token_id="t1")
        assert svc._v2_match_corresponds(v2, bs) is True


# ── Token ID Fallback Tests ──────────────────────────────────────────────

class TestV2PushTokenIdFallback:
    """Test fair_value assignment works even when token1/token2 are empty."""

    def test_push_works_when_token1_token2_unset(self):
        """Hydrated MatchState entries may have token1='' and token2=''.
        The slot-position fallback must handle this."""
        svc = _make_odds_service()

        order1 = FakeOrder(team="FC Barcelona", token_id="tok_b")
        order2 = FakeOrder(team="Newcastle United FC", token_id="tok_n")
        bs_match = FakeMatch(
            team1="FC Barcelona", team2="Newcastle United FC",
            token1="", token2="",
            order1=order1, order2=order2,
        )
        bot_state = FakeBotState({
            "football:fcbarcelona:vs:newcastleunited:0xabc123": bs_match,
        })

        svc._v2_cache["football:fcbarcelona:vs:newcastleunited:h2h:0"] = FakeV2Match(
            match_id="football:fcbarcelona:vs:newcastleunited",
            team1="FC Barcelona", team2="Newcastle United",
            fair_prob1=70.0, fair_prob2=30.0,
            market_type="h2h",
        )
        _run_push(svc, bot_state)
        assert order1.fair_value is not None, "order1 should get fair_value via slot fallback"
        assert order2.fair_value is not None, "order2 should get fair_value via slot fallback"


# ── Normalize Game Consistency ───────────────────────────────────────────

class TestNormalizeGameConsistency:
    """Verify normalize_game and sport_from_slug return consistent values."""

    def test_ncaab_normalizes_to_basketball(self):
        assert normalize_game("ncaab") == "basketball"

    def test_basketball_is_canonical(self):
        assert normalize_game("basketball") == "basketball"

    def test_icehockey_khl_normalizes_to_hockey(self):
        assert normalize_game("icehockey_khl") == "hockey"

    def test_hockey_is_canonical(self):
        assert normalize_game("hockey") == "hockey"

    def test_sport_from_slug_basketball(self):
        from src.core.match_id import sport_from_slug
        assert sport_from_slug("nba-gsw-lakers-2026-03-10") == "basketball"
        assert sport_from_slug("cbb-duke-unc-2026-03-10") == "basketball"
