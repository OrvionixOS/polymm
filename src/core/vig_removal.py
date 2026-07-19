"""
Vig Removal Methods for Sports Betting Odds.

This module provides methods to remove the bookmaker's margin (vig)
from implied probabilities to derive true fair probabilities.

Methods implemented:
- Raw: No vig removal (includes vig, conservative)
- Proportional: Scales probabilities proportionally (simple, default)

History note (2026-06-12): an unimplemented "shin" option used to be
accepted as a `method=` parameter on `calculate_fair_odds`. It silently
fell through to proportional — every scraper that thought it was using
the Shin method has actually been using proportional all along. The
dead parameter was removed; all callers now use proportional explicitly.
"""
from typing import Tuple


def raw_probabilities(odds1: float, odds2: float) -> Tuple[float, float]:
    """
    Return raw implied probabilities without any vig removal.

    This is conservative - probabilities will sum to >100%.
    Use when you want to be cautious about edge estimates.
    """
    if odds1 <= 1 or odds2 <= 1:
        return 0.0, 0.0

    implied1 = 1 / odds1
    implied2 = 1 / odds2
    return implied1, implied2


def proportional_probabilities(odds1: float, odds2: float) -> Tuple[float, float]:
    """
    Remove vig proportionally (basic normalization).

    Each probability is scaled by the same factor.
    Simple but tends to over-correct favorites.

    Formula: p_true = p_implied / sum(p_implied)
    """
    if odds1 <= 1 or odds2 <= 1:
        return 0.0, 0.0

    implied1 = 1 / odds1
    implied2 = 1 / odds2
    total = implied1 + implied2

    return implied1 / total, implied2 / total


def calculate_fair_odds(odds1: float, odds2: float) -> dict:
    """
    Calculate fair probabilities from 2-way decimal odds via proportional
    vig removal.

    Returns:
        Dictionary with fair_prob1, fair_prob2 (as percentages),
        fair_odds1, fair_odds2, and vig. Returns {} for invalid odds
        (odds <= 1 on either side).
    """
    if odds1 <= 1 or odds2 <= 1:
        return {}

    implied1 = 1 / odds1
    implied2 = 1 / odds2
    overround = implied1 + implied2
    vig = (overround - 1) * 100

    fair1, fair2 = proportional_probabilities(odds1, odds2)

    # Calculate fair odds (inverse of fair probability)
    fair_odds1 = 1 / fair1 if fair1 > 0 else 0
    fair_odds2 = 1 / fair2 if fair2 > 0 else 0

    return {
        'fair_prob1': round(fair1 * 100, 1),
        'fair_prob2': round(fair2 * 100, 1),
        'fair_odds1': round(fair_odds1, 2),
        'fair_odds2': round(fair_odds2, 2),
        'vig': round(vig, 1)
    }


def calculate_fair_odds_3way(
    odds1: float,
    odds_draw: float,
    odds2: float,
) -> dict:
    """
    Calculate fair probabilities from 3-way decimal odds (home/draw/away)
    via proportional vig removal.

    Returns:
        Dictionary with:
        - fair_prob1, fair_prob_draw, fair_prob2 (as percentages, sum to 100)
        - fair_prob1_no_draw, fair_prob2_no_draw (2-way proxy for Polymarket)
        - vig
        Returns {} for invalid odds (any odds <= 1).
    """
    if odds1 <= 1 or odds_draw <= 1 or odds2 <= 1:
        return {}

    implied1 = 1 / odds1
    implied_draw = 1 / odds_draw
    implied2 = 1 / odds2
    overround = implied1 + implied_draw + implied2
    vig = (overround - 1) * 100

    # Get fair probabilities (proportional normalization)
    total = overround
    fair1, fair_draw, fair2 = implied1 / total, implied_draw / total, implied2 / total

    # Calculate 2-way proxy (excluding draw) for Polymarket matching
    # P(home | no draw) = P(home) / (P(home) + P(away))
    win_total = fair1 + fair2
    fair1_no_draw = fair1 / win_total if win_total > 0 else 0.5
    fair2_no_draw = fair2 / win_total if win_total > 0 else 0.5

    return {
        'fair_prob1': round(fair1 * 100, 1),
        'fair_prob_draw': round(fair_draw * 100, 1),
        'fair_prob2': round(fair2 * 100, 1),
        'fair_prob1_no_draw': round(fair1_no_draw * 100, 1),
        'fair_prob2_no_draw': round(fair2_no_draw * 100, 1),
        'vig': round(vig, 1)
    }


# Example usage and verification
if __name__ == "__main__":
    # Test with some sample odds
    test_cases = [
        (1.50, 2.60),   # Favorite vs underdog
        (1.85, 2.00),   # Fairly even
        (1.10, 8.00),   # Heavy favorite
        (1.30, 3.50),   # Moderate favorite
    ]

    print("=" * 80)
    print("VIG REMOVAL — PROPORTIONAL METHOD")
    print("=" * 80)

    for odds1, odds2 in test_cases:
        implied1 = 1 / odds1 * 100
        implied2 = 1 / odds2 * 100
        total = implied1 + implied2
        vig = total - 100

        print(f"\nOdds: {odds1:.2f} / {odds2:.2f}")
        print(f"Implied: {implied1:.1f}% / {implied2:.1f}% = {total:.1f}% (vig: {vig:.1f}%)")
        print("-" * 60)

        result = calculate_fair_odds(odds1, odds2)
        print(f"  proportional: {result['fair_prob1']:.1f}% / {result['fair_prob2']:.1f}% "
              f"(sum: {result['fair_prob1'] + result['fair_prob2']:.1f}%)")
