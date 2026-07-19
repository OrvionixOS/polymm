"""
Tests for the analytics helpers.

Covers the shared base utilities and the favourite/underdog analysis
(which runs on public Polymarket wallet data, no private odds needed).
"""
import pytest


# ============================================================================
# Base Module Tests
# ============================================================================

class TestBaseUtilities:
    """Test base analytics utilities."""

    def test_format_percentage_normal(self):
        from analytics.base import format_percentage
        assert format_percentage(0.05) == "5.0%"
        assert format_percentage(0.123, decimals=2) == "12.30%"

    def test_format_percentage_none(self):
        from analytics.base import format_percentage
        assert format_percentage(None) == "N/A"

    def test_format_currency_normal(self):
        from analytics.base import format_currency
        assert format_currency(100) == "$100.00"
        assert format_currency(1234.5, symbol="€") == "€1,234.50"

    def test_format_currency_none(self):
        from analytics.base import format_currency
        assert format_currency(None) == "N/A"

    def test_safe_divide_normal(self):
        from analytics.base import safe_divide
        assert safe_divide(10, 2) == 5.0
        assert safe_divide(1, 3) == pytest.approx(0.333, rel=0.01)

    def test_safe_divide_zero(self):
        from analytics.base import safe_divide
        assert safe_divide(10, 0) == 0.0
        assert safe_divide(10, 0, default=-1) == -1

    def test_get_date_range(self):
        from analytics.base import get_date_range
        start, end = get_date_range(days_back=7)

        assert end > start
        assert (end - start).days == 7

    def test_bucket_values(self):
        from analytics.base import bucket_values

        values = [1.2, 1.8, 2.1, 2.9, 3.0]
        buckets = bucket_values(values, bucket_size=1)

        assert 1 in buckets
        assert 2 in buckets
        assert 3 in buckets


# ============================================================================
# Underdog Analyzer Tests
# ============================================================================

class TestUnderdogAnalyzer:
    """Test underdog/favourite analyzer (runs on public wallet data)."""

    def test_analyze_empty_data(self):
        """Test analysis with no positions."""
        from analytics.underdog_analysis import analyze_underdog_performance

        results = analyze_underdog_performance({"closed_positions": []}, [])

        assert results.get("message") == "No resolved positions in window"

    def test_analyze_with_positions(self):
        """Test performance calculation with resolved positions."""
        from analytics.underdog_analysis import analyze_underdog_performance

        positions = [
            # Favourite win (fair_prob > 0.5)
            {"outcome": "Team A", "oppositeOutcome": "Team B", "realizedPnl": 5.0,
             "totalBought": 50, "avgPrice": 0.65, "curPrice": 1.0},
            # Favourite loss
            {"outcome": "Team C", "oppositeOutcome": "Team D", "realizedPnl": -10.0,
             "totalBought": 60, "avgPrice": 0.70, "curPrice": 0.0},
            # Underdog win
            {"outcome": "Team E", "oppositeOutcome": "Team F", "realizedPnl": 20.0,
             "totalBought": 30, "avgPrice": 0.35, "curPrice": 1.0},
        ]

        # No odds data - will use avgPrice as proxy for fair_prob
        results = analyze_underdog_performance({"closed_positions": positions}, [])

        assert results["favorites"]["count"] == 2  # avgPrice > 0.5
        assert results["underdogs"]["count"] == 1  # avgPrice < 0.5
        assert results["favorites"]["total_pnl"] == -5.0  # 5 - 10
        assert results["underdogs"]["total_pnl"] == 20.0


# Note: capital_analysis.py is a standalone async script, not class-based.
