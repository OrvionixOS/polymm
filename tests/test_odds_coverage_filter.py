"""
Tests for OddsCoverageFilter — verifies that the filter correctly identifies
which markets are covered by external odds and which are not.
"""
import pytest
from unittest.mock import patch, MagicMock

from src.services.odds_coverage_filter import (
    OddsCoverageFilter,
    _team_pair_key,
    parse_spread_opportunity_market,
)


# ── parse_spread_opportunity_market tests ──────────────────────────────────


class TestParseSpreadOpportunityMarket:
    """Test extraction of odds market type + line from SpreadBot opportunity data."""
    
    def test_winner_market(self):
        """No bin_label prefix → h2h."""
        mt, line = parse_spread_opportunity_market("", "Who will win?")
        assert mt == "h2h"
        assert line is None
    
    def test_winner_with_team_name(self):
        """Empty bin_label, question about winner → h2h."""
        mt, line = parse_spread_opportunity_market("", "Butler Bulldogs vs Creighton Bluejays")
        assert mt == "h2h"
        assert line is None
    
    def test_ou_market(self):
        """bin_label = 'O/U 145.5' → totals with line 145.5."""
        mt, line = parse_spread_opportunity_market("O/U 145.5", "Over/Under 145.5?")
        assert mt == "totals"
        assert line == 145.5
    
    def test_ou_soccer(self):
        """bin_label = 'O/U 2.5' → totals with line 2.5."""
        mt, line = parse_spread_opportunity_market("O/U 2.5", "")
        assert mt == "totals"
        assert line == 2.5
    
    def test_spread_market(self):
        """bin_label = 'Spread: Butler (-3.5)' → spreads with line 3.5."""
        mt, line = parse_spread_opportunity_market("Spread: Butler (-3.5)", "")
        assert mt == "spreads"
        assert line == 3.5
    
    def test_spread_positive(self):
        """bin_label = 'Spread: Creighton (+3.5)' → spreads with line 3.5."""
        mt, line = parse_spread_opportunity_market("Spread: Creighton (+3.5)", "")
        assert mt == "spreads"
        assert line == 3.5
    
    def test_spread_in_question(self):
        """Question contains 'spread' → spreads."""
        mt, line = parse_spread_opportunity_market("", "Spread: Butler (-4.5)")
        assert mt == "spreads"
        assert line == 4.5
    
    def test_will_win_rugby(self):
        """'Will X win?' question → h2h."""
        mt, line = parse_spread_opportunity_market("", "Will Harlequins win?")
        assert mt == "h2h"
        assert line is None


# ── _team_pair_key tests ────────────────────────────────────────────────


class TestTeamPairKey:
    """Test canonical team pair key generation."""
    
    def test_sorted_order(self):
        """Keys should be sorted alphabetically."""
        k1 = _team_pair_key("Zenit", "Arsenal")
        k2 = _team_pair_key("Arsenal", "Zenit")
        assert k1 == k2
    
    def test_normalization(self):
        """Team names should be normalized."""
        k1 = _team_pair_key("Butler Bulldogs", "Creighton Bluejays")
        k2 = _team_pair_key("butler bulldogs", "creighton bluejays")
        assert k1 == k2


# ── OddsCoverageFilter.is_covered tests ──────────────────────────────────


class TestOddsCoverageFilter:
    """Test the is_covered method with a pre-populated lookup."""
    
    def _make_filter(self, records):
        """Create a filter with mock data (no DB call)."""
        filt = OddsCoverageFilter()
        
        # Manually build lookup from records
        from collections import defaultdict
        lookup = defaultdict(lambda: defaultdict(set))
        for r in records:
            key = _team_pair_key(r["team1"], r["team2"])
            mt = r["market_type"]
            line = r.get("line")
            if line is not None:
                line = float(line)
            lookup[key][mt].add(line)
        filt._lookup = dict(lookup)
        filt._record_count = len(records)
        return filt
    
    def test_h2h_covered(self):
        """Match with h2h odds → covered for h2h."""
        filt = self._make_filter([
            {"team1": "Arsenal", "team2": "Chelsea", "market_type": "h2h", "line": None},
        ])
        assert filt.is_covered("Arsenal", "Chelsea", "h2h") is True
    
    def test_h2h_not_in_db(self):
        """Match not in DB → not covered."""
        filt = self._make_filter([
            {"team1": "Arsenal", "team2": "Chelsea", "market_type": "h2h", "line": None},
        ])
        assert filt.is_covered("Liverpool", "Man City", "h2h") is False
    
    def test_totals_specific_line_covered(self):
        """Match with O/U 2.5 in DB → covered for totals 2.5."""
        filt = self._make_filter([
            {"team1": "Arsenal", "team2": "Chelsea", "market_type": "h2h", "line": None},
            {"team1": "Arsenal", "team2": "Chelsea", "market_type": "totals", "line": 2.5},
            {"team1": "Arsenal", "team2": "Chelsea", "market_type": "totals", "line": 3.5},
        ])
        assert filt.is_covered("Arsenal", "Chelsea", "totals", 2.5) is True
        assert filt.is_covered("Arsenal", "Chelsea", "totals", 3.5) is True
    
    def test_totals_uncovered_line(self):
        """Match with O/U 2.5 and 3.5 → NOT covered for O/U 1.5."""
        filt = self._make_filter([
            {"team1": "Arsenal", "team2": "Chelsea", "market_type": "totals", "line": 2.5},
            {"team1": "Arsenal", "team2": "Chelsea", "market_type": "totals", "line": 3.5},
        ])
        assert filt.is_covered("Arsenal", "Chelsea", "totals", 1.5) is False
        assert filt.is_covered("Arsenal", "Chelsea", "totals", 4.5) is False
    
    def test_spreads_covered(self):
        """Match with Spread 3.5 → covered."""
        filt = self._make_filter([
            {"team1": "Butler Bulldogs", "team2": "Creighton Bluejays", "market_type": "spreads", "line": 3.5},
        ])
        assert filt.is_covered("Butler Bulldogs", "Creighton Bluejays", "spreads", 3.5) is True
        # Negative line should match absolute value
        assert filt.is_covered("Butler Bulldogs", "Creighton Bluejays", "spreads", -3.5) is True
    
    def test_spreads_uncovered_line(self):
        """Match with Spread 3.5 → NOT covered for Spread 5.5."""
        filt = self._make_filter([
            {"team1": "Butler Bulldogs", "team2": "Creighton Bluejays", "market_type": "spreads", "line": 3.5},
        ])
        assert filt.is_covered("Butler Bulldogs", "Creighton Bluejays", "spreads", 5.5) is False
    
    def test_market_type_not_present(self):
        """Match with h2h only → NOT covered for spreads."""
        filt = self._make_filter([
            {"team1": "Arsenal", "team2": "Chelsea", "market_type": "h2h", "line": None},
        ])
        assert filt.is_covered("Arsenal", "Chelsea", "spreads", 1.5) is False
    
    def test_containment_matching(self):
        """Fuzzy containment: 'arsenalfc' contains 'arsenal'."""
        filt = self._make_filter([
            {"team1": "arsenalfc", "team2": "chelseafc", "market_type": "h2h", "line": None},
        ])
        # Short names should match via containment
        assert filt.is_covered("Arsenal", "Chelsea", "h2h") is True
    
    def test_reversed_team_order(self):
        """Team order shouldn't matter — keys are sorted."""
        filt = self._make_filter([
            {"team1": "Chelsea", "team2": "Arsenal", "market_type": "h2h", "line": None},
        ])
        assert filt.is_covered("Arsenal", "Chelsea", "h2h") is True
    
    def test_basketball_totals(self):
        """Basketball O/U lines (high values like 145.5)."""
        filt = self._make_filter([
            {"team1": "Butler Bulldogs", "team2": "Creighton Bluejays", "market_type": "totals", "line": 131.5},
            {"team1": "Butler Bulldogs", "team2": "Creighton Bluejays", "market_type": "totals", "line": 145.5},
        ])
        assert filt.is_covered("Butler Bulldogs", "Creighton Bluejays", "totals", 145.5) is True
        assert filt.is_covered("Butler Bulldogs", "Creighton Bluejays", "totals", 131.5) is True
        assert filt.is_covered("Butler Bulldogs", "Creighton Bluejays", "totals", 150.5) is False
    
    def test_empty_filter(self):
        """Empty filter → nothing is covered."""
        filt = self._make_filter([])
        assert filt.is_covered("Arsenal", "Chelsea", "h2h") is False


class TestParseOrderTeamNames:
    """Test parsing of order.team names (hydrated order format)."""
    
    def test_team_ou_suffix(self):
        """order.team = 'Gil Vicente FC: O/U 3.5' → totals 3.5."""
        mt, line = parse_spread_opportunity_market("Gil Vicente FC: O/U 3.5", "")
        assert mt == "totals"
        assert line == 3.5
    
    def test_team_spread_format(self):
        """order.team = 'Spread: Butler (-3.5)' → spreads 3.5."""
        mt, line = parse_spread_opportunity_market("Spread: Butler (-3.5)", "")
        assert mt == "spreads"
        assert line == 3.5
    
    def test_plain_team_name(self):
        """order.team = 'Arsenal' → h2h."""
        mt, line = parse_spread_opportunity_market("Arsenal", "")
        assert mt == "h2h"
        assert line is None
    
    def test_bare_dot_no_crash(self):
        """Bare '.' shouldn't crash (the bug that caused 'could not convert string to float')."""
        mt, line = parse_spread_opportunity_market(".", "")
        assert mt == "h2h"
        assert line is None
    
    def test_over_under_team(self):
        """order.team = 'Over' → h2h (no line data in team name alone)."""
        mt, line = parse_spread_opportunity_market("Over", "")
        assert mt == "h2h"
        assert line is None
