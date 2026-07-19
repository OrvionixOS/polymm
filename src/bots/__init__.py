"""
Bot Strategy Package

Contains base bot infrastructure and strategy implementations:
- BaseBot: Abstract base class with shared infrastructure
- SportsBot: External odds → fair probability → edge detection (pre-match)
- LiveBot: Live match trading with faster intervals
- SpreadBot: Weather temperature markets based on spread opportunities
"""
from src.bots.base_bot import BaseBot
from src.bots.sports_bot import SportsBot
from src.bots.live_bot import LiveBot
from src.bots.spread_bot import SpreadBot

__all__ = ["BaseBot", "SportsBot", "LiveBot", "SpreadBot"]
