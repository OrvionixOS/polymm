"""
Tests for fair value resolution across all lookup paths.

Ensures correct fair values for:
- H2H (moneyline) markets
- Totals (Over/Under) markets
- Spread markets
- No tokens (inverted fair value)

Tests the canonical get_fair_value_for_order method and its integration
with get_order_edge, get_order_info, and get_fair_value_for_team.
"""
import pytest
from datetime import datetime, timezone

from src.state.match_state import MatchState, MatchOrder, MatchPosition
from src.scanning.team_matcher import get_fair_value_for_team
from src.services.odds_service import AggregatedMatch, MatchOdds


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def totals_match():
    """A totals match: Man City O/U 4.5.
    
    team1 = 'Manchester City FC: O/U 4.5' (Over side)
    team2 = 'Real Madrid CF' (Under side)
    fair_prob1 = 0.21 (Over), fair_prob2 = 0.79 (Under)
    """
    return MatchState(
        match_id="football:manchestercityfc:o/u4.5:vs:realmadridcf:0xabc123def456789a",
        condition_id="0xabc123def456789a",
        game="football",
        team1="Manchester City FC: O/U 4.5",
        team2="Real Madrid CF",
        token1="token_over",
        token2="token_under",
        fair_prob1=0.21,  # Over probability
        fair_prob2=0.79,  # Under probability
    )


@pytest.fixture
def h2h_match():
    """A standard h2h match: FURIA vs Heroic."""
    return MatchState(
        match_id="cs2:furia:vs:heroic",
        condition_id="0xdef456",
        game="cs2",
        team1="Furia Esports",
        team2="Heroic",
        token1="token_furia",
        token2="token_heroic",
        fair_prob1=0.64,
        fair_prob2=0.36,
    )


@pytest.fixture
def spread_match():
    """A spread match: Bournemouth (-1.5)."""
    return MatchState(
        match_id="football:afcbournemouth:vs:manchesterunitedfc:0x789def012345678a",
        condition_id="0x789def012345678a",
        game="football",
        team1="AFC Bournemouth Spread (-1.5)",
        team2="Manchester United FC",
        token1="token_bournemouth_spread",
        token2="token_manutd_spread",
        fair_prob1=0.45,
        fair_prob2=0.55,
    )


@pytest.fixture
def no_token_match():
    """A rugby match with a No token."""
    return MatchState(
        match_id="rugby:bathrugby:vs:bathrugbyno:0x111aaa222bbb333c",
        condition_id="0x111aaa222bbb333c",
        game="rugby",
        team1="Bath Rugby",
        team2="Bath Rugby No",
        token1="token_bath_yes",
        token2="token_bath_no",
        fair_prob1=0.75,
        fair_prob2=0.25,
    )


@pytest.fixture
def totals_odds_match():
    """AggregatedMatch for totals (from OddsService v2 cache)."""
    match = AggregatedMatch(
        match_id="football:manchestercityfc:vs:realmadridcf",
        team1="Manchester City FC",
        team2="Real Madrid CF",
        game="football",
        is_live=False,
        market_type="totals",
        line=4.5,
        outcome1_name="Over",
        outcome2_name="Under",
    )
    # Add a source so fair_prob1/fair_prob2 work
    match.sources["test"] = MatchOdds(
        match_id=match.match_id,
        team1="Manchester City FC",
        team2="Real Madrid CF",
        odds1=4.76,   # Over odds
        odds2=1.27,   # Under odds
        fair_prob1=21.0,  # Over probability (%)
        fair_prob2=79.0,  # Under probability (%)
        source="test",
        game="football",
        is_live=False,
        market_type="totals",
        line=4.5,
    )
    return match


@pytest.fixture
def h2h_odds_match():
    """AggregatedMatch for h2h (from OddsService cache)."""
    match = AggregatedMatch(
        match_id="cs2:furia:vs:heroic",
        team1="Furia Esports",
        team2="Heroic",
        game="cs2",
        is_live=False,
        market_type="h2h",
    )
    match.sources["test"] = MatchOdds(
        match_id=match.match_id,
        team1="Furia Esports",
        team2="Heroic",
        odds1=1.56,
        odds2=2.50,
        fair_prob1=64.0,
        fair_prob2=36.0,
        source="test",
        game="cs2",
        is_live=False,
    )
    return match


# ============================================================================
# Tests: get_fair_value_for_order (canonical method)
# ============================================================================

class TestGetFairValueForOrder:
    """Tests for MatchState.get_fair_value_for_order — the canonical method."""
    
    def test_uses_per_order_fair_value_when_set(self, totals_match):
        """Per-order fair_value is highest priority and should be returned directly."""
        order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.08, size=10.0, fair_value=0.21,
        )
        totals_match.order1 = order
        
        fair = totals_match.get_fair_value_for_order(order)
        assert fair == 0.21
    
    def test_falls_back_to_team_name_match(self, h2h_match):
        """When order.fair_value is None, matches by team name to get match-level fair_prob."""
        order = MatchOrder(
            order_id="o1", token_id="token_furia", team="Furia Esports",
            price=0.55, size=100.0,
        )
        h2h_match.order1 = order
        
        fair = h2h_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.64)
    
    def test_falls_back_to_token_position(self, totals_match):
        """When team name doesn't match, falls back to token position.
        
        NOTE: For isolated sub-markets (condition_id suffix), orders MUST
        have per-order fair_value set. This test verifies that per-order
        fair_value works even when team name ("Over") doesn't match
        match team1 ("Manchester City FC: O/U 4.5").
        """
        order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.08, size=10.0, fair_value=0.21,  # Set by OddsService v2 push
        )
        totals_match.order1 = order
        
        fair = totals_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.21)
    
    def test_no_token_inversion(self, no_token_match):
        """NO tokens get inverted fair value (1 - fair_prob).
        
        For isolated sub-markets, per-order fair_value must be set.
        For No tokens, the fair_value is pre-inverted at placement time.
        """
        order = MatchOrder(
            order_id="o1", token_id="token_bath_no", team="Bath Rugby No",
            price=0.20, size=10.0, fair_value=0.25,  # 1 - 0.75 = 0.25
        )
        no_token_match.order2 = order
        
        fair = no_token_match.get_fair_value_for_order(order)
        # Bath Rugby fair = 0.75, so Bath Rugby No = 1 - 0.75 = 0.25
        assert fair == pytest.approx(0.25)
    
    def test_returns_none_when_no_fair_probs(self, totals_match):
        """Returns None if no fair probabilities are set on the match."""
        totals_match.fair_prob1 = None
        totals_match.fair_prob2 = None
        
        order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.08, size=10.0,
        )
        totals_match.order1 = order
        
        fair = totals_match.get_fair_value_for_order(order)
        assert fair is None


# ============================================================================
# Tests: get_order_edge delegates correctly
# ============================================================================

class TestGetOrderEdge:
    """Tests that get_order_edge uses get_fair_value_for_order internally."""
    
    def test_edge_with_per_order_fair_value(self, totals_match):
        """Edge uses order.fair_value when set."""
        order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.08, size=10.0, fair_value=0.21,
        )
        totals_match.order1 = order
        
        edge = totals_match.get_order_edge(order)
        assert edge == pytest.approx(0.13)  # 0.21 - 0.08
    
    def test_edge_with_match_level_fair(self, h2h_match):
        """Edge uses match-level fair_prob for h2h orders."""
        order = MatchOrder(
            order_id="o1", token_id="token_heroic", team="Heroic",
            price=0.30, size=100.0,
        )
        h2h_match.order2 = order
        
        edge = h2h_match.get_order_edge(order)
        assert edge == pytest.approx(0.06)  # 0.36 - 0.30
    
    def test_totals_over_order_edge_correct(self, totals_match):
        """Over order on totals market gets correct edge (not swapped)."""
        # This is the EXACT bug scenario:
        # Over fair = 21%, price = 0.08 → edge should be +13%, NOT +71%
        order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.08, size=10.0, fair_value=0.21,  # Set by OddsService v2 push
        )
        totals_match.order1 = order
        
        edge = totals_match.get_order_edge(order)
        assert edge == pytest.approx(0.13)  # Correct: 0.21 - 0.08
        assert edge < 0.50  # Definitely NOT using Under's 79%
    
    def test_totals_under_order_edge_correct(self, totals_match):
        """Under order on totals market gets correct edge."""
        order = MatchOrder(
            order_id="o1", token_id="token_under", team="Under",
            price=0.60, size=10.0, fair_value=0.79,  # Set by OddsService v2 push
        )
        totals_match.order2 = order
        
        edge = totals_match.get_order_edge(order)
        assert edge == pytest.approx(0.19)  # 0.79 - 0.60
    
    def test_returns_none_for_missing_fair_probs(self, totals_match):
        """Returns None when fair value cannot be determined."""
        totals_match.fair_prob1 = None
        totals_match.fair_prob2 = None
        
        order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.08, size=10.0,
        )
        totals_match.order1 = order
        
        edge = totals_match.get_order_edge(order)
        assert edge is None


# ============================================================================
# Tests: get_fair_value_for_team (team_matcher.py)
# ============================================================================

class TestGetFairValueForTeamTotals:
    """Tests for get_fair_value_for_team with totals markets."""
    
    def test_over_returns_fair_prob1(self, totals_odds_match):
        """'Over' on a totals match returns fair_prob1 (Over probability)."""
        fair = get_fair_value_for_team("Over", totals_odds_match)
        assert fair == pytest.approx(0.21)
    
    def test_under_returns_fair_prob2(self, totals_odds_match):
        """'Under' on a totals match returns fair_prob2 (Under probability)."""
        fair = get_fair_value_for_team("Under", totals_odds_match)
        assert fair == pytest.approx(0.79)
    
    def test_1h_over_returns_correct_value(self):
        """'1H Over' on a totals_1h match returns fair_prob1."""
        match = AggregatedMatch(
            match_id="football:teama:vs:teamb",
            team1="Team A", team2="Team B",
            game="football", is_live=False,
            market_type="totals_1h", line=1.5,
        )
        match.sources["test"] = MatchOdds(
            match_id=match.match_id,
            team1="Team A", team2="Team B",
            odds1=2.0, odds2=1.8,
            fair_prob1=47.0, fair_prob2=53.0,
            source="test", game="football", is_live=False,
            market_type="totals_1h", line=1.5,
        )
        fair = get_fair_value_for_team("1H Over", match)
        assert fair == pytest.approx(0.47)
    
    def test_1h_under_returns_correct_value(self):
        """'1H Under' on a totals_1h match returns fair_prob2."""
        match = AggregatedMatch(
            match_id="football:teama:vs:teamb",
            team1="Team A", team2="Team B",
            game="football", is_live=False,
            market_type="totals_1h", line=1.5,
        )
        match.sources["test"] = MatchOdds(
            match_id=match.match_id,
            team1="Team A", team2="Team B",
            odds1=2.0, odds2=1.8,
            fair_prob1=47.0, fair_prob2=53.0,
            source="test", game="football", is_live=False,
            market_type="totals_1h", line=1.5,
        )
        fair = get_fair_value_for_team("1H Under", match)
        assert fair == pytest.approx(0.53)
    
    def test_over_on_h2h_does_not_match(self, h2h_odds_match):
        """'Over' on an h2h match should NOT match (returns None)."""
        fair = get_fair_value_for_team("Over", h2h_odds_match)
        assert fair is None
    
    def test_team_name_still_works_on_h2h(self, h2h_odds_match):
        """Regular team name matching still works for h2h markets."""
        fair = get_fair_value_for_team("Heroic", h2h_odds_match)
        assert fair == pytest.approx(0.36)
    
    def test_no_token_still_works(self, h2h_odds_match):
        """'Team No' inversion still works correctly."""
        fair = get_fair_value_for_team("Heroic No", h2h_odds_match)
        # Heroic fair = 0.36, Heroic No = 1 - 0.36 = 0.64
        assert fair == pytest.approx(0.64)


# ============================================================================
# Tests: Integration — Over/Under edge calculation end-to-end
# ============================================================================

class TestTotalsEdgeEndToEnd:
    """End-to-end tests for the bug scenario: Over/Under fair values must not swap."""
    
    def test_over_order_with_per_order_fair_has_correct_edge(self, totals_match):
        """
        THE BUG SCENARIO:
        Over 4.5 order at 21¢ with fair=21% should show edge ~0%, NOT +58%.
        
        Before fix: get_fair_for_token returned 79% (Under's fair)
        After fix: get_fair_value_for_order returns 21% (order.fair_value)
        """
        order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.21, size=6.0, fair_value=0.21,
        )
        totals_match.order1 = order
        
        edge = totals_match.get_order_edge(order)
        assert edge == pytest.approx(0.0)  # 0.21 - 0.21 = 0% edge
        assert edge < 0.50  # Must NOT be the swapped +58%
    
    def test_entry_at_08_should_have_13_edge(self, totals_match):
        """Entry at 8¢ with Over fair=21% → edge = +13%."""
        order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.08, size=10.0, fair_value=0.21,
        )
        totals_match.order1 = order
        
        edge = totals_match.get_order_edge(order)
        assert edge == pytest.approx(0.13)
    
    def test_spread_order_uses_per_order_fair(self, spread_match):
        """Spread order uses order.fair_value, not match-level fair_prob."""
        order = MatchOrder(
            order_id="o1", token_id="token_bournemouth_spread",
            team="AFC Bournemouth Spread (-1.5)",
            price=0.35, size=100.0, fair_value=0.45,
        )
        spread_match.order1 = order
        
        edge = spread_match.get_order_edge(order)
        assert edge == pytest.approx(0.10)  # 0.45 - 0.35


# ============================================================================
# Fixtures: Additional market types for reactive scenarios
# ============================================================================

@pytest.fixture
def yesno_match():
    """A football 3-way Yes/No market (Will PSV win?)."""
    return MatchState(
        match_id="football:psv:vs:psvno:0xaaa123bbb456ccc7",
        condition_id="0xaaa123bbb456ccc7",
        game="football",
        team1="PSV",
        team2="PSV No",
        token1="token_psv_yes",
        token2="token_psv_no",
        fair_prob1=0.55,   # PSV win prob
        fair_prob2=0.45,   # Includes draw + opponent
    )


@pytest.fixture
def h2h_1h_match():
    """A 1st-half moneyline market."""
    return MatchState(
        match_id="football:barcelona:vs:sevilla:0xbbb456ccc789ddd0",
        condition_id="0xbbb456ccc789ddd0",
        game="football",
        team1="FC Barcelona",
        team2="Sevilla",
        token1="token_barca_1h",
        token2="token_sevilla_1h",
        fair_prob1=0.52,
        fair_prob2=0.48,
    )


@pytest.fixture
def totals_1h_match():
    """A 1st-half totals (O/U) market."""
    return MatchState(
        match_id="football:barca:1ho/u1.5:vs:sevilla:0xccc789ddd012eee3",
        condition_id="0xccc789ddd012eee3",
        game="football",
        team1="FC Barcelona: 1H O/U 1.5",
        team2="Sevilla",
        token1="token_1h_over",
        token2="token_1h_under",
        fair_prob1=0.38,  # 1H Over
        fair_prob2=0.62,  # 1H Under
    )


@pytest.fixture
def spread_1h_match():
    """A 1st-half spread market."""
    return MatchState(
        match_id="football:barcelona:1hspread:vs:sevilla:0xddd012eee345fff6",
        condition_id="0xddd012eee345fff6",
        game="football",
        team1="FC Barcelona: 1H Spread (-0.5)",
        team2="Sevilla",
        token1="token_barca_1h_spread",
        token2="token_sevilla_1h_spread",
        fair_prob1=0.58,  # Barca covers -0.5 in 1H
        fair_prob2=0.42,
    )


@pytest.fixture
def player_prop_match():
    """An NBA player prop market (Points O/U)."""
    return MatchState(
        match_id="nba:jamal_murray_points:vs:jamal_murray_points_under:0xeee456fff789aaa0",
        condition_id="0xeee456fff789aaa0",
        game="nba",
        team1="Jamal Murray: Points O/U 29.5",
        team2="Not Jamal Murray: Points O/U 29.5",
        token1="token_murray_over",
        token2="token_murray_under",
        fair_prob1=0.44,  # Over 29.5 prob
        fair_prob2=0.56,  # Under 29.5 prob
    )


# ============================================================================
# Tests: Outbid edge detection per market type
# ============================================================================

class TestOutbidEdgeDetection:
    """Tests that outbid handler computes correct edge for each market type.
    
    The reactive handler reads fair_value from get_order_info → 
    get_fair_value_for_order. For outbid decisions, the edge must be
    correct for the SPECIFIC market type, not contaminated by h2h.
    
    Simulates: someone outbids us → handler checks if new_price still has edge.
    """
    
    def test_h2h_outbid_uses_match_level_fair(self, h2h_match):
        """H2H outbid uses match-level fair_prob (no per-order fair needed)."""
        order = MatchOrder(
            order_id="o1", token_id="token_heroic", team="Heroic",
            price=0.30, size=100.0,  # No fair_value set
        )
        h2h_match.order2 = order
        
        # Before outbid: edge = 0.36 - 0.30 = 0.06
        edge = h2h_match.get_order_edge(order)
        assert edge == pytest.approx(0.06)
        
        # After outbid to 0.32: new_edge = 0.36 - 0.33 = 0.03 (below min_edge)
        order.price = 0.33
        new_edge = h2h_match.get_order_edge(order)
        assert new_edge == pytest.approx(0.03)
        assert new_edge < 0.05  # Below typical min_edge
    
    def test_spread_outbid_uses_per_order_fair(self, spread_match):
        """Spread outbid MUST use per-order fair_value, not match-level."""
        order = MatchOrder(
            order_id="o1", token_id="token_bournemouth_spread",
            team="AFC Bournemouth Spread (-1.5)",
            price=0.35, size=100.0, fair_value=0.45,
        )
        spread_match.order1 = order
        
        # Before outbid: edge = 0.45 - 0.35 = 0.10
        edge = spread_match.get_order_edge(order)
        assert edge == pytest.approx(0.10)
        
        # After outbid to 0.41: new_edge = 0.45 - 0.41 = 0.04
        order.price = 0.41
        new_edge = spread_match.get_order_edge(order)
        assert new_edge == pytest.approx(0.04)
        assert new_edge < 0.05  # Below min_edge → should NOT outbid further
    
    def test_totals_outbid_uses_per_order_fair(self, totals_match):
        """Totals Over/Under outbid MUST use per-order fair_value."""
        over_order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.08, size=10.0, fair_value=0.21,
        )
        totals_match.order1 = over_order
        
        # Before outbid: edge = 0.21 - 0.08 = 0.13
        edge = totals_match.get_order_edge(over_order)
        assert edge == pytest.approx(0.13)
        
        # After outbid to 0.17: new_edge = 0.21 - 0.17 = 0.04
        over_order.price = 0.17
        new_edge = totals_match.get_order_edge(over_order)
        assert new_edge == pytest.approx(0.04)
        
        # CRITICAL: Must NOT use Under's 79% fair value!
        assert new_edge < 0.50, "Edge is using Under's fair value — O/U swap bug!"
    
    def test_yesno_outbid_uses_inverted_fair_for_no(self, yesno_match):
        """Yes/No outbid: No token must get correct fair value."""
        no_order = MatchOrder(
            order_id="o1", token_id="token_psv_no", team="PSV No",
            price=0.30, size=10.0, fair_value=0.45,  # 1 - 0.55 = 0.45
        )
        yesno_match.order2 = no_order
        
        # No fair = 0.45 (pre-inverted at placement)
        # Edge = 0.45 - 0.30 = 0.15
        edge = yesno_match.get_order_edge(no_order)
        assert edge == pytest.approx(0.15)
        
        # Outbid to 0.42: edge = 0.45 - 0.42 = 0.03
        no_order.price = 0.42
        new_edge = yesno_match.get_order_edge(no_order)
        assert new_edge == pytest.approx(0.03)
    
    def test_1h_totals_outbid(self, totals_1h_match):
        """1H totals outbid uses per-order fair_value."""
        order = MatchOrder(
            order_id="o1", token_id="token_1h_over", team="1H Over",
            price=0.20, size=10.0, fair_value=0.38,
        )
        totals_1h_match.order1 = order
        
        edge = totals_1h_match.get_order_edge(order)
        assert edge == pytest.approx(0.18)  # 0.38 - 0.20
        
        # After outbid
        order.price = 0.34
        assert totals_1h_match.get_order_edge(order) == pytest.approx(0.04)
    
    def test_1h_moneyline_outbid(self, h2h_1h_match):
        """1H moneyline outbid uses per-order fair_value."""
        order = MatchOrder(
            order_id="o1", token_id="token_barca_1h", team="FC Barcelona",
            price=0.40, size=10.0, fair_value=0.52,
        )
        h2h_1h_match.order1 = order
        
        edge = h2h_1h_match.get_order_edge(order)
        assert edge == pytest.approx(0.12)  # 0.52 - 0.40
        
        # After outbid to 0.49: edge = 0.52 - 0.49 = 0.03 (below min_edge)
        order.price = 0.49
        assert h2h_1h_match.get_order_edge(order) == pytest.approx(0.03)
    
    def test_1h_spread_outbid(self, spread_1h_match):
        """1H spread outbid uses per-order fair_value."""
        order = MatchOrder(
            order_id="o1", token_id="token_barca_1h_spread",
            team="FC Barcelona: 1H Spread (-0.5)",
            price=0.42, size=10.0, fair_value=0.58,
        )
        spread_1h_match.order1 = order
        
        edge = spread_1h_match.get_order_edge(order)
        assert edge == pytest.approx(0.16)  # 0.58 - 0.42
        
        # After outbid
        order.price = 0.55
        assert spread_1h_match.get_order_edge(order) == pytest.approx(0.03)
    
    def test_player_prop_outbid(self, player_prop_match):
        """Player prop outbid uses per-order fair_value."""
        order = MatchOrder(
            order_id="o1", token_id="token_murray_over",
            team="Over",
            price=0.30, size=10.0, fair_value=0.44,
        )
        player_prop_match.order1 = order
        
        edge = player_prop_match.get_order_edge(order)
        assert edge == pytest.approx(0.14)  # 0.44 - 0.30
        
        # After outbid: must NOT use Under's 56%
        order.price = 0.41
        new_edge = player_prop_match.get_order_edge(order)
        assert new_edge == pytest.approx(0.03)
        assert new_edge < 0.20, "Edge is using Under's fair value — prop O/U swap!"


# ============================================================================
# Tests: Price improvement edge detection per market type
# ============================================================================

class TestPriceImprovementEdge:
    """Tests that price improvement computes correct edge.
    
    Price improvement = we're best bid with a gap to 2nd.
    Handler checks: current_edge = our_fair - our_price.
    If edge > min_edge, it can lower the bid to save spread.
    The fair value MUST be market-type-specific.
    """
    
    def test_h2h_improvement_edge(self, h2h_match):
        """H2H price improvement correctly uses match-level fair."""
        order = MatchOrder(
            order_id="o1", token_id="token_furia", team="Furia Esports",
            price=0.45, size=100.0,
        )
        h2h_match.order1 = order
        
        fair = h2h_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.64)
        
        current_edge = fair - order.price
        assert current_edge == pytest.approx(0.19)
        
        # Can improve down to 2nd_bid + 0.01
        # New price = 0.40, new_edge = 0.64 - 0.40 = 0.24 → still good
        new_price = 0.40
        assert fair - new_price == pytest.approx(0.24)
    
    def test_spread_improvement_uses_per_order_fair(self, spread_match):
        """Spread price improvement MUST use per-order fair, not match-level."""
        order = MatchOrder(
            order_id="o1", token_id="token_bournemouth_spread",
            team="AFC Bournemouth Spread (-1.5)",
            price=0.35, size=100.0, fair_value=0.45,
        )
        spread_match.order1 = order
        
        fair = spread_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.45)  # Per-order, not match-level
        
        current_edge = fair - order.price
        assert current_edge == pytest.approx(0.10)
    
    def test_totals_improvement_never_swaps(self, totals_match):
        """Totals improvement must use correct Over/Under fair (no swap)."""
        over_order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.10, size=10.0, fair_value=0.21,
        )
        totals_match.order1 = over_order
        
        fair = totals_match.get_fair_value_for_order(over_order)
        assert fair == pytest.approx(0.21)  # NOT 0.79 (Under)
        
        current_edge = fair - over_order.price
        assert current_edge == pytest.approx(0.11)
        assert current_edge < 0.50  # No swap!
    
    def test_1h_moneyline_improvement(self, h2h_1h_match):
        """1H moneyline improvement uses per-order fair."""
        order = MatchOrder(
            order_id="o1", token_id="token_barca_1h", team="FC Barcelona",
            price=0.35, size=10.0, fair_value=0.52,
        )
        h2h_1h_match.order1 = order
        
        fair = h2h_1h_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.52)
        assert fair - order.price == pytest.approx(0.17)
    
    def test_1h_spread_improvement(self, spread_1h_match):
        """1H spread improvement uses per-order fair."""
        order = MatchOrder(
            order_id="o1", token_id="token_barca_1h_spread",
            team="FC Barcelona: 1H Spread (-0.5)",
            price=0.42, size=10.0, fair_value=0.58,
        )
        spread_1h_match.order1 = order
        
        fair = spread_1h_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.58)
        assert fair - order.price == pytest.approx(0.16)
    
    def test_1h_totals_improvement(self, totals_1h_match):
        """1H totals improvement uses per-order fair (no O/U swap)."""
        order = MatchOrder(
            order_id="o1", token_id="token_1h_over", team="1H Over",
            price=0.20, size=10.0, fair_value=0.38,
        )
        totals_1h_match.order1 = order
        
        fair = totals_1h_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.38)  # NOT 0.62 (1H Under)
        assert fair - order.price == pytest.approx(0.18)
    
    def test_player_prop_improvement(self, player_prop_match):
        """Player prop improvement uses per-order fair (no O/U swap)."""
        order = MatchOrder(
            order_id="o1", token_id="token_murray_over",
            team="Over",
            price=0.30, size=10.0, fair_value=0.44,
        )
        player_prop_match.order1 = order
        
        fair = player_prop_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.44)  # NOT 0.56 (Under)
        assert fair - order.price == pytest.approx(0.14)


# ============================================================================
# Tests: Hedging fair value resolution
# ============================================================================

class TestHedgeFairValue:
    """Tests that hedge fair values are correctly resolved from BotState.
    
    After an entry fill, the bot looks up hedge_fair_value from 
    get_positions_needing_hedge(). This must return the correct
    market-type-specific fair value for the OPPOSITE side.
    """
    
    def test_h2h_hedge_fair_is_opponent_prob(self, h2h_match):
        """H2H: if Furia filled, hedge fair = Heroic fair_prob."""
        h2h_match.position1 = MatchPosition(
            token_id="token_furia", team="Furia Esports",
            shares=100.0, avg_price=0.55,
        )
        
        # Check fair values
        assert h2h_match.fair_prob2 == pytest.approx(0.36)
        
        # Verify via get_fair_for_token
        hedge_fair = h2h_match.get_fair_for_token("token_heroic")
        assert hedge_fair == pytest.approx(0.36)
    
    def test_no_token_hedge_fair_is_inverted(self, yesno_match):
        """Yes/No: if No filled, hedge must find Yes fair via canonical method."""
        yesno_match.position2 = MatchPosition(
            token_id="token_psv_no", team="PSV No",
            shares=10.0, avg_price=0.30,
        )
        
        # PSV Yes fair = 0.55 directly 
        yes_fair = yesno_match.get_fair_for_token("token_psv_yes")
        assert yes_fair == pytest.approx(0.55)
        
        # For the canonical order path, test via get_fair_value_for_order:
        # PSV No order should get inverted fair = 1 - 0.55 = 0.45
        no_order = MatchOrder(
            order_id="o1", token_id="token_psv_no", team="PSV No",
            price=0.30, size=10.0, fair_value=0.45,
        )
        yesno_match.order2 = no_order
        no_fair = yesno_match.get_fair_value_for_order(no_order)
        assert no_fair == pytest.approx(0.45)
    
    def test_totals_hedge_fair_correct_side(self, totals_match):
        """Totals: if Over filled, hedge fair should be Under's fair."""
        totals_match.position1 = MatchPosition(
            token_id="token_over", team="Over",
            shares=10.0, avg_price=0.08,
        )
        
        under_fair = totals_match.get_fair_for_token("token_under")
        assert under_fair == pytest.approx(0.79)
        
        # The Over fair should be 0.21 (NOT 0.79)
        over_fair = totals_match.get_fair_for_token("token_over")
        assert over_fair == pytest.approx(0.21)
    
    def test_spread_hedge_fair_correct_side(self, spread_match):
        """Spread: if team1 filled, hedge fair = team2 fair_prob."""
        spread_match.position1 = MatchPosition(
            token_id="token_bournemouth_spread",
            team="AFC Bournemouth Spread (-1.5)",
            shares=100.0, avg_price=0.35,
        )
        
        hedge_fair = spread_match.get_fair_for_token("token_manutd_spread")
        assert hedge_fair == pytest.approx(0.55)
    
    def test_1h_moneyline_hedge_fair(self, h2h_1h_match):
        """1H moneyline: hedge fair uses match-level fair_prob of opponent."""
        h2h_1h_match.position1 = MatchPosition(
            token_id="token_barca_1h", team="FC Barcelona",
            shares=10.0, avg_price=0.40,
        )
        hedge_fair = h2h_1h_match.get_fair_for_token("token_sevilla_1h")
        assert hedge_fair == pytest.approx(0.48)
    
    def test_1h_spread_hedge_fair(self, spread_1h_match):
        """1H spread: hedge fair uses match-level fair_prob of opponent."""
        spread_1h_match.position1 = MatchPosition(
            token_id="token_barca_1h_spread",
            team="FC Barcelona: 1H Spread (-0.5)",
            shares=10.0, avg_price=0.42,
        )
        hedge_fair = spread_1h_match.get_fair_for_token("token_sevilla_1h_spread")
        assert hedge_fair == pytest.approx(0.42)
    
    def test_1h_totals_hedge_fair(self, totals_1h_match):
        """1H totals: if Over filled, hedge fair = Under fair."""
        totals_1h_match.position1 = MatchPosition(
            token_id="token_1h_over", team="1H Over",
            shares=10.0, avg_price=0.20,
        )
        under_fair = totals_1h_match.get_fair_for_token("token_1h_under")
        assert under_fair == pytest.approx(0.62)
    
    def test_player_prop_hedge_fair(self, player_prop_match):
        """Player prop: if Over filled, hedge fair = Under fair."""
        player_prop_match.position1 = MatchPosition(
            token_id="token_murray_over", team="Over",
            shares=10.0, avg_price=0.30,
        )
        under_fair = player_prop_match.get_fair_for_token("token_murray_under")
        assert under_fair == pytest.approx(0.56)


# ============================================================================
# Tests: Cross-market contamination guard (Fix B)
# ============================================================================

class TestCrossMarketContaminationGuard:
    """Tests that isolated sub-markets (spread/totals/Yes-No with condition_id
    suffix) refuse to use contaminated match-level fair_prob.
    
    Regression test for the Hoffenheim -1.5 bug:
    H2H fair pushed to match.fair_prob1 → hydrated spread order with
    order.fair_value=None reads contaminated match-level fair → wrong edge.
    """
    
    def test_spread_hydrated_order_refuses_match_level_fair(self, spread_match):
        """Hydrated spread order (fair_value=None) should return None, not match fair."""
        order = MatchOrder(
            order_id="o1", token_id="token_bournemouth_spread",
            team="AFC Bournemouth Spread (-1.5)",
            price=0.35, size=100.0,
            fair_value=None,  # Hydrated — no per-order fair
        )
        spread_match.order1 = order
        
        # Spread match has condition_id suffix → isolated sub-market
        # get_fair_value_for_order should return None (refuse to guess)
        fair = spread_match.get_fair_value_for_order(order)
        assert fair is None, (
            f"Expected None for hydrated spread order, got {fair} — "
            f"likely reading contaminated match-level fair_prob"
        )
    
    def test_totals_hydrated_order_refuses_match_level_fair(self, totals_match):
        """Hydrated totals order (fair_value=None) should return None."""
        order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.08, size=10.0,
            fair_value=None,  # Hydrated
        )
        totals_match.order1 = order
        
        fair = totals_match.get_fair_value_for_order(order)
        assert fair is None, (
            f"Expected None for hydrated totals order, got {fair}"
        )
    
    def test_yesno_hydrated_order_refuses_match_level_fair(self, yesno_match):
        """Hydrated Yes/No order (fair_value=None) should return None."""
        order = MatchOrder(
            order_id="o1", token_id="token_psv_no", team="PSV No",
            price=0.30, size=10.0,
            fair_value=None,  # Hydrated
        )
        yesno_match.order2 = order
        
        fair = yesno_match.get_fair_value_for_order(order)
        assert fair is None, (
            f"Expected None for hydrated Yes/No order, got {fair}"
        )
    
    def test_h2h_no_suffix_allows_match_level_fallback(self, h2h_match):
        """H2H (no condition_id suffix) CAN use match-level fair_prob."""
        order = MatchOrder(
            order_id="o1", token_id="token_heroic", team="Heroic",
            price=0.30, size=100.0,
            fair_value=None,  # Hydrated
        )
        h2h_match.order2 = order
        
        # H2H match_id has no 0x... suffix → allowed to use match-level fair
        fair = h2h_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.36), (
            f"H2H should allow match-level fallback, got {fair}"
        )
    
    def test_1h_spread_hydrated_refuses_match_level(self, spread_1h_match):
        """Hydrated 1H spread order should return None."""
        order = MatchOrder(
            order_id="o1", token_id="token_barca_1h_spread",
            team="FC Barcelona: 1H Spread (-0.5)",
            price=0.42, size=10.0,
            fair_value=None,
        )
        spread_1h_match.order1 = order
        fair = spread_1h_match.get_fair_value_for_order(order)
        assert fair is None
    
    def test_player_prop_hydrated_refuses_match_level(self, player_prop_match):
        """Hydrated player prop order should return None."""
        order = MatchOrder(
            order_id="o1", token_id="token_murray_over",
            team="Over",
            price=0.30, size=10.0,
            fair_value=None,
        )
        player_prop_match.order1 = order
        fair = player_prop_match.get_fair_value_for_order(order)
        assert fair is None
    
    def test_spread_with_per_order_fair_still_works(self, spread_match):
        """Spread with per-order fair set should use it (not blocked by guard)."""
        order = MatchOrder(
            order_id="o1", token_id="token_bournemouth_spread",
            team="AFC Bournemouth Spread (-1.5)",
            price=0.35, size=100.0, fair_value=0.45,
        )
        spread_match.order1 = order
        
        # Per-order fair is set → should use it regardless of guard
        fair = spread_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.45)
    
    def test_contaminated_match_level_not_leaking(self, spread_match):
        """Even if match fair_prob is contaminated with h2h, per-order wins."""
        # Simulate contamination: someone pushed h2h fair (0.65) to match level
        spread_match.fair_prob1 = 0.65  # Contaminated!
        spread_match.fair_prob2 = 0.17  # Also contaminated!
        
        order = MatchOrder(
            order_id="o1", token_id="token_bournemouth_spread",
            team="AFC Bournemouth Spread (-1.5)",
            price=0.35, size=100.0, fair_value=0.45,  # Correct spread fair
        )
        spread_match.order1 = order
        
        # Per-order fair takes priority → 0.45 (not contaminated 0.65)
        fair = spread_match.get_fair_value_for_order(order)
        assert fair == pytest.approx(0.45)
        assert fair != pytest.approx(0.65), "Reading contaminated h2h fair!"
    
    def test_contaminated_match_level_blocked_for_hydrated(self, spread_match):
        """Hydrated spread order with contaminated match fair gets None."""
        # Simulate contamination
        spread_match.fair_prob1 = 0.65
        spread_match.fair_prob2 = 0.17
        
        order = MatchOrder(
            order_id="o1", token_id="token_bournemouth_spread",
            team="AFC Bournemouth Spread (-1.5)",
            price=0.35, size=100.0, fair_value=None,  # Hydrated
        )
        spread_match.order1 = order
        
        fair = spread_match.get_fair_value_for_order(order)
        # Guard should block: returns None rather than contaminated 0.65
        assert fair is None, (
            f"Expected None for hydrated spread, got {fair} — "
            f"contaminated h2h fair is leaking through!"
        )


# ============================================================================
# Tests: BotState get_order_info integration
# ============================================================================

class TestGetOrderInfoFairValue:
    """Tests that BotState.get_order_info returns correct fair_value for
    each market type — this is the exact path the reactive handler reads.
    
    get_order_info → match.get_fair_value_for_order(order) → fair_value.
    """
    
    def test_h2h_get_order_info_returns_correct_fair(self, h2h_match):
        """get_order_info returns correct fair for h2h."""
        from src.state.bot_state import BotState
        
        bs = BotState()
        bs._matches[h2h_match.match_id] = h2h_match
        
        order = MatchOrder(
            order_id="o1", token_id="token_furia", team="Furia Esports",
            price=0.55, size=100.0,
        )
        h2h_match.order1 = order
        bs._token_to_match["token_furia"] = h2h_match.match_id
        bs._order_to_match["o1"] = h2h_match.match_id
        
        info = bs.get_order_info("token_furia")
        assert info is not None
        assert info["fair_value"] == pytest.approx(0.64)
    
    def test_spread_get_order_info_returns_per_order_fair(self, spread_match):
        """get_order_info returns per-order fair for spread."""
        from src.state.bot_state import BotState
        
        bs = BotState()
        bs._matches[spread_match.match_id] = spread_match
        
        order = MatchOrder(
            order_id="o1", token_id="token_bournemouth_spread",
            team="AFC Bournemouth Spread (-1.5)",
            price=0.35, size=100.0, fair_value=0.45,
        )
        spread_match.order1 = order
        bs._token_to_match["token_bournemouth_spread"] = spread_match.match_id
        bs._order_to_match["o1"] = spread_match.match_id
        
        info = bs.get_order_info("token_bournemouth_spread")
        assert info is not None
        assert info["fair_value"] == pytest.approx(0.45)
    
    def test_totals_get_order_info_over_correct(self, totals_match):
        """get_order_info returns Over fair (not Under) for Over order."""
        from src.state.bot_state import BotState
        
        bs = BotState()
        bs._matches[totals_match.match_id] = totals_match
        
        order = MatchOrder(
            order_id="o1", token_id="token_over", team="Over",
            price=0.08, size=10.0, fair_value=0.21,
        )
        totals_match.order1 = order
        bs._token_to_match["token_over"] = totals_match.match_id
        bs._order_to_match["o1"] = totals_match.match_id
        
        info = bs.get_order_info("token_over")
        assert info is not None
        assert info["fair_value"] == pytest.approx(0.21)
        assert info["fair_value"] < 0.50  # NOT Under's 79%
    
    def test_yesno_get_order_info_no_token_inverted(self, yesno_match):
        """get_order_info returns inverted fair for No token."""
        from src.state.bot_state import BotState
        
        bs = BotState()
        bs._matches[yesno_match.match_id] = yesno_match
        
        no_order = MatchOrder(
            order_id="o1", token_id="token_psv_no", team="PSV No",
            price=0.30, size=10.0, fair_value=0.45,  # 1 - 0.55
        )
        yesno_match.order2 = no_order
        bs._token_to_match["token_psv_no"] = yesno_match.match_id
        bs._order_to_match["o1"] = yesno_match.match_id
        
        info = bs.get_order_info("token_psv_no")
        assert info is not None
        assert info["fair_value"] == pytest.approx(0.45)
    
    def test_hydrated_spread_get_order_info_returns_none(self, spread_match):
        """Hydrated spread order: get_order_info returns None fair (safety)."""
        from src.state.bot_state import BotState
        
        bs = BotState()
        bs._matches[spread_match.match_id] = spread_match
        
        order = MatchOrder(
            order_id="o1", token_id="token_bournemouth_spread",
            team="AFC Bournemouth Spread (-1.5)",
            price=0.35, size=100.0, fair_value=None,  # Hydrated
        )
        spread_match.order1 = order
        bs._token_to_match["token_bournemouth_spread"] = spread_match.match_id
        bs._order_to_match["o1"] = spread_match.match_id
        
        info = bs.get_order_info("token_bournemouth_spread")
        assert info is not None
        assert info["fair_value"] is None, (
            f"Hydrated spread should have None fair, got {info['fair_value']}"
        )
    
    def test_1h_moneyline_get_order_info(self, h2h_1h_match):
        """get_order_info returns correct fair for 1H moneyline."""
        from src.state.bot_state import BotState
        
        bs = BotState()
        bs._matches[h2h_1h_match.match_id] = h2h_1h_match
        
        order = MatchOrder(
            order_id="o1", token_id="token_barca_1h", team="FC Barcelona",
            price=0.40, size=10.0, fair_value=0.52,
        )
        h2h_1h_match.order1 = order
        bs._token_to_match["token_barca_1h"] = h2h_1h_match.match_id
        bs._order_to_match["o1"] = h2h_1h_match.match_id
        
        info = bs.get_order_info("token_barca_1h")
        assert info is not None
        assert info["fair_value"] == pytest.approx(0.52)
    
    def test_player_prop_get_order_info(self, player_prop_match):
        """get_order_info returns correct fair for player prop Over."""
        from src.state.bot_state import BotState
        
        bs = BotState()
        bs._matches[player_prop_match.match_id] = player_prop_match
        
        order = MatchOrder(
            order_id="o1", token_id="token_murray_over",
            team="Over",
            price=0.30, size=10.0, fair_value=0.44,
        )
        player_prop_match.order1 = order
        bs._token_to_match["token_murray_over"] = player_prop_match.match_id
        bs._order_to_match["o1"] = player_prop_match.match_id
        
        info = bs.get_order_info("token_murray_over")
        assert info is not None
        assert info["fair_value"] == pytest.approx(0.44)
        assert info["fair_value"] < 0.50  # NOT Under's 56%
