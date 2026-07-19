"""
Unit tests for execution/hedge_finder.py - Hedge calculation and profitability.
"""
import pytest

from src.execution.hedge_finder import HedgeFinder, HedgeCalculation


class TestHedgeCalculation:
    """Tests for HedgeCalculation dataclass."""
    
    def test_dataclass_fields(self):
        """Verify all expected fields exist."""
        calc = HedgeCalculation(
            can_hedge=True,
            hedge_price=0.45,
            entry_cost=5.0,
            hedge_cost=4.5,
            total_cost=9.5,
            payout=10.0,
            profit=0.5,
            profit_percent=0.0526,
            reason="OK"
        )
        assert calc.can_hedge is True
        assert calc.hedge_price == 0.45


class TestHedgeFinder:
    """Tests for HedgeFinder class."""
    
    def test_calculate_hedge_profitable(self):
        """Can hedge when fair value allows 5%+ profit."""
        finder = HedgeFinder(min_profit_percent=0.05)
        
        # Entry at 45¢, hedge fair value at 45¢ = 90¢ total
        # Profit = $1 - $0.90 = $0.10 = 11.1%
        calc = finder.calculate_hedge(
            entry_price=0.45,
            entry_shares=10.0,
            hedge_fair_value=0.45,
        )
        
        assert calc.can_hedge is True
        assert calc.profit_percent > 0.05  # More than 5% profit
        assert abs(calc.profit - 1.0) < 0.01  # ~$1 profit on 10 shares
    
    def test_calculate_hedge_unprofitable(self):
        """Cannot hedge when fair value too high."""
        finder = HedgeFinder(min_profit_percent=0.05)
        
        # Entry at 45¢, hedge fair value at 55¢ = 100¢ total = 0% profit
        calc = finder.calculate_hedge(
            entry_price=0.45,
            entry_shares=10.0,
            hedge_fair_value=0.55,
        )
        
        assert calc.can_hedge is False
        assert "max" in calc.reason.lower() or "profit" in calc.reason.lower()
    
    def test_calculate_hedge_edge_case_exactly_5_percent(self):
        """Edge case: exactly at 5% profit threshold."""
        finder = HedgeFinder(min_profit_percent=0.05)
        
        # For 5% profit: total_cost = 1.0 / 1.05 = 0.952
        # With entry at 0.40, hedge needs to be at 0.552
        calc = finder.calculate_hedge(
            entry_price=0.40,
            entry_shares=10.0,
            hedge_fair_value=0.552,
        )
        
        # Should be right at the threshold
        assert calc.profit_percent >= 0.048  # Close to 5%
    
    def test_breakeven_hedge_price(self):
        """Breakeven = 1 - entry_price."""
        finder = HedgeFinder()
        
        # Entry at 45¢ → breakeven hedge at 55¢
        breakeven = finder.calculate_breakeven_hedge(
            entry_price=0.45,
            entry_shares=10.0,
        )
        
        assert abs(breakeven - 0.55) < 0.001
    
    def test_target_profit_hedge(self):
        """Calculate max hedge for specific profit target."""
        finder = HedgeFinder()
        
        # Entry at 40¢, want 10% profit
        # payout = 10 shares * $1 = $10
        # entry_cost = 10 * 0.40 = $4
        # target_profit = $10 * 0.10 = $1
        # max_hedge_cost = $10 - $4 - $1 = $5
        # max_hedge_price = $5 / 10 = $0.50
        
        max_hedge = finder.calculate_target_profit_hedge(
            entry_price=0.40,
            entry_shares=10.0,
            target_profit_percent=0.10,
        )
        
        assert abs(max_hedge - 0.50) < 0.001
    
    def test_hedge_range(self):
        """Returns (min_bid, max_bid, is_profitable) tuple."""
        finder = HedgeFinder(min_profit_percent=0.05)
        
        min_bid, max_bid, is_profitable = finder.get_hedge_range(
            entry_price=0.40,
            entry_shares=10.0,
            hedge_fair_value=0.45,
            current_best_bid=0.30,
        )
        
        # min_bid = best_bid + 0.01 = 0.31
        assert abs(min_bid - 0.31) < 0.001
        
        # max_bid = min(fair_value, breakeven) = min(0.45, 0.60) = 0.45
        assert abs(max_bid - 0.45) < 0.001
        
        # At 40¢ entry + 45¢ hedge = 85¢ → 15% profit, is_profitable = True
        assert is_profitable is True
    
    def test_hedge_range_not_profitable(self):
        """Returns is_profitable=False when hedge at fair doesn't meet target."""
        finder = HedgeFinder(min_profit_percent=0.10)  # 10% min profit
        
        # Entry at 50¢, hedge fair at 48¢ = 98¢ → only 2% profit
        _, _, is_profitable = finder.get_hedge_range(
            entry_price=0.50,
            entry_shares=10.0,
            hedge_fair_value=0.48,
            current_best_bid=0.30,
        )
        
        assert is_profitable is False
    
    def test_profit_percent_calculation(self):
        """Verify profit = (payout - total_cost) / total_cost."""
        finder = HedgeFinder()
        
        calc = finder.calculate_hedge(
            entry_price=0.30,
            entry_shares=10.0,
            hedge_fair_value=0.40,
        )
        
        # Entry: 10 * 0.30 = $3
        # Hedge: 10 * 0.40 = $4
        # Total: $7
        # Payout: $10
        # Profit: $3 = 42.9%
        
        assert abs(calc.entry_cost - 3.0) < 0.01
        assert abs(calc.hedge_cost - 4.0) < 0.01
        assert abs(calc.total_cost - 7.0) < 0.01
        assert abs(calc.payout - 10.0) < 0.01
        assert abs(calc.profit - 3.0) < 0.01
        assert abs(calc.profit_percent - 0.429) < 0.01


class TestHedgeFinderMinProfit:
    """Tests for custom min_profit_percent."""
    
    def test_custom_min_profit(self):
        """Finder respects custom min_profit_percent."""
        # 10% minimum
        finder = HedgeFinder(min_profit_percent=0.10)
        
        # Entry at 45¢, hedge at 46¢ = 91¢ → 9.9% profit < 10%
        calc = finder.calculate_hedge(
            entry_price=0.45,
            entry_shares=10.0,
            hedge_fair_value=0.46,
        )
        
        assert calc.can_hedge is False
    
    def test_default_min_profit_is_5_percent(self):
        """Default min_profit is 5%."""
        finder = HedgeFinder()
        assert finder.min_profit == 0.05
