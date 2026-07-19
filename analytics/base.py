"""
Base utilities for the analytics scripts.

Shared helpers used across the analytics modules:
- Date range helpers
- Output formatting
- Common numeric helpers
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from collections import defaultdict

from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()


def get_date_range(days_back: int = 7) -> Tuple[datetime, datetime]:
    """
    Get start and end datetime for a date range.

    Args:
        days_back: Number of days to look back (default 7)

    Returns:
        Tuple of (start_datetime, end_datetime)
    """
    end = datetime.now()
    start = end - timedelta(days=days_back)
    return start, end


def format_percentage(value: Optional[float], decimals: int = 1) -> str:
    """Format a decimal as percentage string."""
    if value is None:
        return "N/A"
    return f"{float(value) * 100:.{decimals}f}%"


def format_currency(value: Optional[float], symbol: str = "$") -> str:
    """Format a number as currency."""
    if value is None:
        return "N/A"
    return f"{symbol}{float(value):,.2f}"


def format_number(value: Optional[float], decimals: int = 2) -> str:
    """Format a number with specified decimals."""
    if value is None:
        return "N/A"
    return f"{float(value):,.{decimals}f}"


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Safe division that returns default on zero denominator."""
    if denominator == 0:
        return default
    return numerator / denominator


def print_header(title: str, char: str = "=", width: int = 60):
    """Print a formatted header."""
    print(f"\n{char * width}")
    print(title.center(width))
    print(f"{char * width}")


def print_subheader(title: str, char: str = "-", width: int = 60):
    """Print a formatted subheader."""
    print(f"\n{char * width}")
    print(title)
    print(f"{char * width}")


def print_stat(label: str, value: str, emoji: str = ""):
    """Print a labeled statistic."""
    prefix = f"{emoji} " if emoji else "   "
    print(f"{prefix}{label}: {value}")


def calculate_roi(pnl: float, cost: float) -> Optional[float]:
    """Calculate ROI as a decimal (0.05 = 5% ROI)."""
    if cost == 0:
        return None
    return pnl / cost


def bucket_values(values: List[float], bucket_size: int = 1) -> Dict[int, List[float]]:
    """
    Group values into buckets.

    Args:
        values: List of values to bucket
        bucket_size: Size of each bucket (e.g., 1 for integer buckets)

    Returns:
        Dict mapping bucket key to list of values in that bucket
    """
    buckets = defaultdict(list)
    for v in values:
        bucket_key = int(v // bucket_size) * bucket_size
        buckets[bucket_key].append(v)
    return dict(buckets)


# Emoji constants for consistent output
EMOJI = {
    "money": "💰",
    "chart": "📊",
    "win": "✅",
    "loss": "❌",
    "warning": "⚠️",
    "fire": "🔥",
    "trophy": "🏆",
    "target": "🎯",
    "time": "⏱️",
    "game": "🎮",
    "up": "📈",
    "down": "📉",
    "star": "⭐",
    "neutral": "➖",
}
