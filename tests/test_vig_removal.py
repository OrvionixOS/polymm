"""
Unit tests for core/vig_removal.py - Vig removal methods for sports betting odds.
"""
import pytest
from src.core.vig_removal import (
    raw_probabilities,
    proportional_probabilities,
    calculate_fair_odds,
    calculate_fair_odds_3way,
)


class TestRawProbabilities:
    """Tests for raw_probabilities function."""

    def test_sum_over_100_percent(self):
        """Raw probabilities should sum to >100% (include vig)."""
        prob1, prob2 = raw_probabilities(1.50, 2.60)
        total = prob1 + prob2
        assert total > 1.0, f"Expected sum > 1.0, got {total}"

    def test_individual_probabilities(self):
        """Raw probabilities are just 1/odds."""
        prob1, prob2 = raw_probabilities(2.00, 2.00)
        assert abs(prob1 - 0.50) < 0.001
        assert abs(prob2 - 0.50) < 0.001

    def test_invalid_odds_return_zeros(self):
        """Odds <= 1 should return (0.0, 0.0)."""
        assert raw_probabilities(1.0, 2.0) == (0.0, 0.0)
        assert raw_probabilities(2.0, 1.0) == (0.0, 0.0)
        assert raw_probabilities(0.5, 2.0) == (0.0, 0.0)


class TestProportionalProbabilities:
    """Tests for proportional_probabilities function."""

    def test_sum_to_100_percent(self):
        """Proportional probabilities should sum to exactly 100%."""
        prob1, prob2 = proportional_probabilities(1.50, 2.60)
        total = prob1 + prob2
        assert abs(total - 1.0) < 0.001, f"Expected sum = 1.0, got {total}"

    def test_preserves_probability_ratio(self):
        """Proportional scaling preserves the ratio between probabilities."""
        prob1, prob2 = proportional_probabilities(1.50, 3.00)
        # 1/1.50 = 0.667, 1/3.00 = 0.333, ratio = 2:1
        assert abs(prob1 / prob2 - 2.0) < 0.001

    def test_invalid_odds_return_zeros(self):
        """Odds <= 1 should return (0.0, 0.0)."""
        assert proportional_probabilities(1.0, 2.0) == (0.0, 0.0)


class TestCalculateFairOdds:
    """Tests for calculate_fair_odds — proportional method only."""

    def test_returns_complete_dict(self):
        """Result should contain every expected key."""
        result = calculate_fair_odds(1.50, 2.60)
        assert "fair_prob1" in result
        assert "fair_prob2" in result
        assert "fair_odds1" in result
        assert "fair_odds2" in result
        assert "vig" in result

    def test_vig_calculation(self):
        """Vig should be calculated as overround - 100."""
        result = calculate_fair_odds(1.50, 2.60)
        # 1/1.50 + 1/2.60 = 0.667 + 0.385 = 1.052 → ~5% vig
        assert 4.5 < result["vig"] < 6.0

    def test_fair_probs_as_percentages(self):
        """Fair probs should be returned as percentages (0-100)."""
        result = calculate_fair_odds(1.50, 2.60)
        assert result["fair_prob1"] > 1
        assert result["fair_prob1"] < 100
        assert result["fair_prob2"] > 1
        assert result["fair_prob2"] < 100

    def test_invalid_odds_return_empty(self):
        """Invalid odds should return empty dict."""
        assert calculate_fair_odds(1.0, 2.0) == {}
        assert calculate_fair_odds(2.0, 0.5) == {}

    def test_known_reference_value(self):
        """Result must match the known Python reference output."""
        # calculate_fair_odds(1.85, 2.00) →
        # {'fair_prob1': 51.9, 'fair_prob2': 48.1, 'fair_odds1': 1.92,
        #  'fair_odds2': 2.08, 'vig': 4.1}
        result = calculate_fair_odds(1.85, 2.00)
        assert result["fair_prob1"] == 51.9
        assert result["fair_prob2"] == 48.1
        assert result["fair_odds1"] == 1.92
        assert result["fair_odds2"] == 2.08
        assert result["vig"] == 4.1


# =============================================================================
# 3-WAY MARKET TESTS (Rugby, Soccer)
# =============================================================================


class TestCalculateFairOdds3Way:
    """Tests for calculate_fair_odds_3way — proportional method only."""

    def test_returns_all_fields(self):
        """Should return all expected fields."""
        result = calculate_fair_odds_3way(2.20, 3.40, 3.00)
        assert "fair_prob1" in result
        assert "fair_prob_draw" in result
        assert "fair_prob2" in result
        assert "fair_prob1_no_draw" in result
        assert "fair_prob2_no_draw" in result
        assert "vig" in result

    def test_3way_probs_sum_to_100(self):
        """3-way probabilities should sum to 100%."""
        result = calculate_fair_odds_3way(2.20, 3.40, 3.00)
        total = result["fair_prob1"] + result["fair_prob_draw"] + result["fair_prob2"]
        assert abs(total - 100.0) < 0.5, f"Expected sum = 100, got {total}"

    def test_no_draw_probs_sum_to_100(self):
        """No-draw proxy probabilities should sum to 100%."""
        result = calculate_fair_odds_3way(2.20, 3.40, 3.00)
        total = result["fair_prob1_no_draw"] + result["fair_prob2_no_draw"]
        assert abs(total - 100.0) < 0.5, f"Expected sum = 100, got {total}"

    def test_no_draw_proxy_excludes_draw(self):
        """No-draw proxy should be rescaled to exclude draw probability."""
        result = calculate_fair_odds_3way(2.20, 3.40, 3.00)
        assert result["fair_prob1_no_draw"] > result["fair_prob1"]
        assert result["fair_prob2_no_draw"] > result["fair_prob2"]

    def test_vig_calculation(self):
        """Vig should be calculated correctly for 3-way market."""
        result = calculate_fair_odds_3way(2.20, 3.40, 3.00)
        # 1/2.20 + 1/3.40 + 1/3.00 = 0.455 + 0.294 + 0.333 = 1.082 → ~8% vig
        assert 6.0 < result["vig"] < 10.0

    def test_invalid_odds_return_empty(self):
        """Invalid odds should return empty dict."""
        assert calculate_fair_odds_3way(1.0, 3.0, 3.0) == {}
        assert calculate_fair_odds_3way(2.0, 0.5, 3.0) == {}

    def test_high_vig_market(self):
        """Test with high vig market (common in lower leagues)."""
        result = calculate_fair_odds_3way(1.80, 3.00, 4.50)
        assert result["vig"] > 10.0
        total = result["fair_prob1"] + result["fair_prob_draw"] + result["fair_prob2"]
        assert abs(total - 100.0) < 0.5
