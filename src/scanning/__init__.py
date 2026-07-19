"""
Scanning module - opportunity detection and team matching.
"""
from src.scanning.team_matcher import (
    align_teams_with_bids,
    normalize_team_name,
    find_matching_odds,
    get_fair_value_for_team,
    get_fair_value_for_match,
)
from src.scanning.opportunity_scanner import OpportunityScanner, find_poly_event

__all__ = [
    "align_teams_with_bids",
    "normalize_team_name",
    "find_matching_odds",
    "get_fair_value_for_team",
    "get_fair_value_for_match",
    "OpportunityScanner",
    "find_poly_event",
]
