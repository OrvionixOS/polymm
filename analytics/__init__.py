"""
polymm analytics

Scripts to evaluate a Polymarket trading account from its public wallet:
P&L, arb-vs-directional attribution, position breakdowns, and
favourite/underdog performance.
"""
# Only import base utilities at package level to avoid circular imports.
from .base import (
    get_date_range,
    format_percentage,
    format_currency,
    EMOJI,
)

__all__ = [
    "get_date_range",
    "format_percentage",
    "format_currency",
    "EMOJI",
]
