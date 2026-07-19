"""
Hedge Finder - Calculates and executes hedges for filled positions.

When an entry order fills, the HedgeFinder:
1. Calculates the optimal hedge price (at fair value for best fill chance)
2. Ensures minimum profit target is met (5%+)
3. Places the hedge limit order
"""
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class HedgeCalculation:
    """Result of hedge calculation."""
    can_hedge: bool
    hedge_price: float
    entry_cost: float
    hedge_cost: float
    total_cost: float
    payout: float
    profit: float
    profit_percent: float
    reason: str = ""


class HedgeFinder:
    """
    Calculates optimal hedge prices and validates profitability.
    
    Strategy:
    - Place hedge at opposite team's fair value (best fill chance)
    - Ensure total profit >= 5%
    - If fair value gives insufficient profit, don't hedge (wait or exit)
    """
    
    def __init__(self, min_profit_percent: float = 0.05):
        """
        Args:
            min_profit_percent: Minimum profit target (default 5%)
        """
        self.min_profit = min_profit_percent
    
    def calculate_hedge(
        self,
        entry_price: float,
        entry_shares: float,
        hedge_fair_value: float,
        current_best_bid: Optional[float] = None,
        requested_shares: Optional[float] = None,
    ) -> HedgeCalculation:
        """
        Calculate the optimal hedge and check profitability.

        Args:
            entry_price: Price we paid for entry shares
            entry_shares: Number of shares filled
            hedge_fair_value: Fair probability of the hedge team (from bookmakers)
            current_best_bid: Current best bid in the order book (optional)
            requested_shares: Original order size (if known). When provided,
                we assert entry_shares <= requested_shares and warn if fills
                arrive out of order (i.e. filled > requested).

        Returns:
            HedgeCalculation with details
        """
        if requested_shares is not None:
            # Partial-fill invariant: cumulative fills must never exceed order size.
            # Out-of-order fills (duplicate WS events, resync double-counting)
            # are the common failure mode — refuse to size the hedge against a
            # phantom amount, and loudly surface the inconsistency.
            if entry_shares > requested_shares + 1e-6:
                logger.warning(
                    "Hedge sizing invariant violated: filled=%.4f > requested=%.4f "
                    "(clamping hedge size to requested). Likely cause: duplicate "
                    "fill event or resync double-count.",
                    entry_shares, requested_shares,
                )
                entry_shares = requested_shares
            assert entry_shares <= requested_shares + 1e-6, (
                f"filled shares {entry_shares} exceed requested {requested_shares}"
            )

        payout = entry_shares * 1.0  # $1 per share payout
        entry_cost = entry_shares * entry_price
        
        # Calculate max hedge price for min profit
        min_profit_usd = payout * self.min_profit
        max_hedge_cost = payout - entry_cost - min_profit_usd
        max_hedge_price = max_hedge_cost / entry_shares
        
        # Proposed hedge at fair value
        hedge_price = hedge_fair_value
        hedge_cost = entry_shares * hedge_price
        total_cost = entry_cost + hedge_cost
        profit = payout - total_cost
        profit_percent = profit / total_cost if total_cost > 0 else 0
        
        # Check if profitable
        if hedge_price > max_hedge_price:
            return HedgeCalculation(
                can_hedge=False,
                hedge_price=hedge_price,
                entry_cost=entry_cost,
                hedge_cost=hedge_cost,
                total_cost=total_cost,
                payout=payout,
                profit=profit,
                profit_percent=profit_percent,
                reason=f"Fair value {hedge_price:.2f} > max {max_hedge_price:.2f} for {self.min_profit*100:.0f}% profit"
            )
        
        return HedgeCalculation(
            can_hedge=True,
            hedge_price=hedge_price,
            entry_cost=entry_cost,
            hedge_cost=hedge_cost,
            total_cost=total_cost,
            payout=payout,
            profit=profit,
            profit_percent=profit_percent,
            reason="OK"
        )
    
    def calculate_breakeven_hedge(
        self,
        entry_price: float,
        entry_shares: float,
    ) -> float:
        """
        Calculate breakeven hedge price (0% profit).
        
        Args:
            entry_price: Price we paid for entry shares
            entry_shares: Number of shares
            
        Returns:
            Maximum hedge price for breakeven
        """
        payout = entry_shares * 1.0
        entry_cost = entry_shares * entry_price
        max_hedge_cost = payout - entry_cost
        return max_hedge_cost / entry_shares
    
    def calculate_target_profit_hedge(
        self,
        entry_price: float,
        entry_shares: float,
        target_profit_percent: float,
    ) -> float:
        """
        Calculate hedge price for a specific profit target.
        
        Args:
            entry_price: Price we paid for entry shares
            entry_shares: Number of shares
            target_profit_percent: Target profit (e.g., 0.10 for 10%)
            
        Returns:
            Maximum hedge price for target profit
        """
        payout = entry_shares * 1.0
        entry_cost = entry_shares * entry_price
        target_profit_usd = payout * target_profit_percent
        max_hedge_cost = payout - entry_cost - target_profit_usd
        return max_hedge_cost / entry_shares
    
    def get_hedge_range(
        self,
        entry_price: float,
        entry_shares: float,
        hedge_fair_value: float,
        current_best_bid: float = 0.01,
    ) -> Tuple[float, float, bool]:
        """
        Get the valid bid range for hedge orders.
        
        Args:
            entry_price: Price we paid for entry shares
            entry_shares: Number of shares
            hedge_fair_value: Fair value of hedge team
            current_best_bid: Current best bid in order book
            
        Returns:
            (min_bid, max_bid, is_profitable)
            - min_bid: Minimum bid to be visible (above best bid)
            - max_bid: Maximum bid (fair value, capped at breakeven)
            - is_profitable: True if hedge at fair value gives 5%+ profit
        """
        breakeven_price = self.calculate_breakeven_hedge(entry_price, entry_shares)
        target_price = self.calculate_target_profit_hedge(entry_price, entry_shares, self.min_profit)
        
        min_bid = current_best_bid + 0.01  # 1¢ above best bid
        max_bid = min(hedge_fair_value, breakeven_price)  # Don't overpay
        
        is_profitable = hedge_fair_value <= target_price
        
        return (min_bid, max_bid, is_profitable)


def demo():
    """Demonstrate hedge calculations."""
    finder = HedgeFinder(min_profit_percent=0.05)
    
    print("=" * 60)
    print("🔍 HEDGE FINDER DEMO")
    print("=" * 60)
    
    # Scenario: Bought Team A at 26¢, fair value 50/50
    entry_price = 0.26
    entry_shares = 5
    hedge_fair = 0.50  # Team B fair value
    
    print(f"\n📊 Entry: {entry_shares} shares of Team A @ {entry_price:.2f}")
    print(f"   Team B fair value: {hedge_fair:.2f}")
    
    calc = finder.calculate_hedge(entry_price, entry_shares, hedge_fair)
    
    print(f"\n📈 Hedge Calculation:")
    print(f"   Can hedge: {calc.can_hedge}")
    print(f"   Hedge price: {calc.hedge_price:.2f}")
    print(f"   Entry cost: ${calc.entry_cost:.2f}")
    print(f"   Hedge cost: ${calc.hedge_cost:.2f}")
    print(f"   Total cost: ${calc.total_cost:.2f}")
    print(f"   Payout: ${calc.payout:.2f}")
    print(f"   Profit: ${calc.profit:.2f} ({calc.profit_percent*100:.1f}%)")
    
    # Show breakeven
    breakeven = finder.calculate_breakeven_hedge(entry_price, entry_shares)
    print(f"\n   Breakeven hedge price: {breakeven:.2f}")
    
    # Show range
    min_bid, max_bid, profitable = finder.get_hedge_range(entry_price, entry_shares, hedge_fair, 0.22)
    print(f"\n   Hedge bid range: {min_bid:.2f} - {max_bid:.2f}")
    print(f"   Profitable at fair: {profitable}")
    
    print("\n" + "=" * 60)


if __name__ == "__main__":
    demo()
