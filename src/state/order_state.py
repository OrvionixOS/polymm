"""
Order State Mixin - Order and position CRUD operations for BotState.

This module contains order/position management methods extracted from BotState
for better code organization. Uses mixin pattern to avoid logic duplication.

Provides:
- Order CRUD: register_order, update_order_fill, cancel_order, replace_order
- Hydration: register_hydrated_order, register_hydrated_position
- Query helpers: get_order_info, get_all_open_order_infos, etc.
"""
import logging
from typing import Optional, Dict, List, Any, Set, Tuple

from src.core.match_id import normalize_team, normalize_game, parse_match_question, sport_from_slug
from src.state.match_state import MatchOrder, MatchPosition, MatchState
import re


def _clean_team_name(name: str) -> str:
    """Strip Polymarket metadata suffixes from team names.
    
    Removes:
    - " - More Markets" suffix (from sub-event titles)
    - ": Spread -X.X" suffix (from groupItemTitle)
    - ": O/U X.X" suffix (from groupItemTitle)
    - "(-X.X)" trailing spread value (from position team names)
    """
    if not name:
        return name
    # Strip " - More Markets" suffix
    name = re.sub(r'\s*-\s*More Markets$', '', name)
    # Strip ": Spread -X.X" or ": Spread +X.X" suffix  
    name = re.sub(r':\s*Spread\s*[-+]?\d+\.?\d*$', '', name)
    # Strip ": O/U X.X" suffix
    name = re.sub(r':\s*O/U\s*\d+\.?\d*$', '', name)
    # Strip ": 1H Moneyline" suffix (halftime result markets)
    name = re.sub(r':\s*1H Moneyline$', '', name)
    # Strip trailing "(-X.X)" spread value (e.g., "Not Team (-2.5)" → "Not Team")
    name = re.sub(r'\s*\([-+]?\d+\.?\d*\)\s*$', '', name)
    return name.strip()


class OrderStateMixin:
    """
    Mixin providing order/position CRUD operations for BotState.
    
    This class is designed to be inherited by BotState. It uses self._matches,
    self._order_to_match, self._token_to_match, etc. from BotState.
    """
    
    # ===== Order Management =====
    
    def register_order(
        self,
        match_id: str,
        order_id: str,
        token_id: str,
        team: str,
        price: float,
        size: float,
        is_entry: bool = True,
        fair_value: Optional[float] = None,
    ) -> Optional[MatchState]:
        """
        Register an order on a match.
        
        Returns the updated MatchState, or None if match not found.
        """
        match = self._matches.get(match_id)
        if not match:
            # Auto-create match - parse team names from match_id
            # Format: "{game}:{team1}:vs:{team2}" where team1 < team2 alphabetically
            team1_parsed = ""
            team2_parsed = ""
            game_parsed = ""
            parts = match_id.split(":vs:")
            if len(parts) == 2:
                team2_parsed = parts[1]
                # Strip condition_id suffix (":0x..." at end) so team2 is clean
                # e.g., "fcbarcelonano:0x3133220400132933" → "fcbarcelonano"
                if ":" in team2_parsed:
                    last_part = team2_parsed.rsplit(":", 1)[1]
                    if last_part.startswith("0x") and len(last_part) >= 10:
                        team2_parsed = team2_parsed.rsplit(":", 1)[0]
                prefix_parts = parts[0].rsplit(":", 1)
                if len(prefix_parts) == 2:
                    game_parsed = prefix_parts[0]
                    team1_parsed = prefix_parts[1]
            
            match = self.register_match(
                match_id=match_id,
                game=game_parsed,
                team1=team1_parsed,
                team2=team2_parsed,
            )
        
        # Preserve fair_value from existing order if not explicitly provided.
        # This prevents _state_resync_loop from wiping fair_value set by OddsService push:
        # resync calls register_order with same order_id but fair_value=None,
        # creating a new MatchOrder that replaces the old one (which had fair_value set).
        if fair_value is None:
            existing_order = None
            if match.order1 and match.order1.order_id == order_id:
                existing_order = match.order1
            elif match.order2 and match.order2.order_id == order_id:
                existing_order = match.order2
            if existing_order and existing_order.fair_value is not None:
                fair_value = existing_order.fair_value
        
        # Create order
        order = MatchOrder(
            order_id=order_id,
            token_id=token_id,
            team=team,
            price=price,
            size=size,
            is_entry=is_entry,
            fair_value=fair_value,
        )
        
        # Normalize team names for comparison
        # For rugby "No" tokens (e.g., "Pau No"), strip " No" for matching against team1/team2
        # CRITICAL FIX: Strip " No" from BOTH the incoming team AND the match teams
        # This allows "Cardiff Rugby" to match "Cardiff Rugby No" (same underlying team)
        team_base = team[:-3] if team.endswith(" No") else team
        team1_base = match.team1[:-3] if match.team1 and match.team1.endswith(" No") else (match.team1 or "")
        team2_base = match.team2[:-3] if match.team2 and match.team2.endswith(" No") else (match.team2 or "")
        team_lower = normalize_team(team_base)
        team1_lower = normalize_team(team1_base)
        team2_lower = normalize_team(team2_base)
        

        # WEATHER/MENTIONS MARKET FIX: For weather/mentions markets, "Yes" -> team1, "No" -> team2
        # Polymarket uses "Yes"/"No" outcomes, but our match has bin labels as team names
        if match.game in ("weather", "mentions") and team_lower in ("yes", "no"):
            if team_lower == "yes":
                team_lower = team1_lower  # Map "yes" to team1
            else:
                team_lower = team2_lower  # Map "no" to team2
        
        # STOCK MARKET FIX: Handle "Up" / "Down" orders
        # Stock match teams are "TICKER:Up" / "TICKER:Down" but orders come as just "Up" / "Down"
        if match.game == "stock" and team_lower in ("up", "down"):
            # Map "up" to whichever team ends with ":Up", and "down" to ":Down"
            if team_lower == "up":
                if team1_lower.endswith(":up"):
                    team_lower = team1_lower
                elif team2_lower.endswith(":up"):
                    team_lower = team2_lower
            else:  # "down"
                if team1_lower.endswith(":down"):
                    team_lower = team1_lower
                elif team2_lower.endswith(":down"):
                    team_lower = team2_lower

        # Assign to correct side based on token_id or team name matching
        # EXACT MATCH ONLY - no substring matching
        # CRITICAL: Don't overwrite existing orders with DIFFERENT tokens!
        if token_id == match.token1:
            # ADDITIVE: Don't overwrite an existing OPEN order with a different ID.
            # Rehydration creates new WatchedOrder objects for the same CLOB order;
            # overwriting would destroy metadata set by the hedge seeker / CLOB check.
            if (not match.order1
                    or match.order1.order_id == order_id
                    or not match.order1.is_open):
                match.order1 = order
            if not match.team1:
                match.team1 = team
        elif token_id == match.token2:
            # ADDITIVE: Same guard as token1 above.
            if (not match.order2
                    or match.order2.order_id == order_id
                    or not match.order2.is_open):
                match.order2 = order
            if not match.team2:
                match.team2 = team
        elif team_lower == team1_lower:
            # Team matches side 1 (exact match)
            # CRITICAL: If position1 exists with a DIFFERENT token, this order is a HEDGE!
            # (e.g., position "Bayonne No" on NO token, order "Bayonne" on YES token)
            if match.position1 and match.token1 and token_id != match.token1:
                # This is a hedge order - put it on order2 (opposite side)!
                if not match.order2 or match.order2.token_id == token_id:
                    match.order2 = order
                    if not match.team2:
                        match.team2 = team
                    if not match.token2:
                        match.token2 = token_id
                        self._token_to_match[token_id] = match_id
                else:
                    # order2 exists with different token - collision
                    self._token_to_match[token_id] = match_id
                    if token_id not in self._collision_tokens:
                        logging.warning(f"Hedge collision: {team} (token {token_id[:16]}...) - order2 already exists")
                        self._collision_tokens.add(token_id)
                    return match
            elif match.order1 and match.order1.token_id != token_id:
                # CRITICAL: Don't overwrite if order1 exists with a DIFFERENT token!
                # Check if this is actually the COMPLEMENTARY token (order on the OTHER side)
                # This happens when we have orders on BOTH sides of the same market (arb!)
                # Case 1: token_id matches token2 explicitly
                # Case 2: order2 slot is empty - this IS the complementary token!
                # Case 3: DEGENERATE STATE - token1 == token2 (both slots have same token)
                is_degenerate = match.token1 and match.token2 and match.token1 == match.token2
                
                if token_id == match.token2 or not match.order2 or is_degenerate:
                    # This is the complementary token - assign to order2!
                    match.order2 = order
                    if not match.team2:
                        match.team2 = team
                    # Also fix token2 if it was degenerate or empty
                    if not match.token2 or is_degenerate:
                        match.token2 = token_id
                        self._token_to_match[token_id] = match_id
                else:
                    # True collision: different market with same team name
                    # CRITICAL: Add to collision tokens - these should NEVER be discarded
                    self._token_to_match[token_id] = match_id
                    
                    # Only log warning the FIRST time we see this collision
                    if token_id not in self._collision_tokens:
                        logging.warning(f"Skipping duplicate team order: {team} (token {token_id[:16]}...) - order1 already has token {match.order1.token_id[:16]}...")
                        logging.warning(f"  match_id: {match_id} | team1: {match.team1} | team2: {match.team2}")
                        logging.warning(f"  new_token: {token_id} | existing_token: {match.order1.token_id}")
                        self._collision_tokens.add(token_id)
                        logging.info(f"Token {token_id[:16]}... added to _collision_tokens (size: {len(self._collision_tokens)})")
                    
                    return match  # Return without registering this order
            else:
                match.order1 = order
                if not match.token1:
                    match.token1 = token_id
                    self._token_to_match[token_id] = match_id
        elif team_lower == team2_lower:
            # Team matches side 2 (exact match)
            # CRITICAL: If position2 exists with a DIFFERENT token, this order is a HEDGE!
            if match.position2 and match.token2 and token_id != match.token2:
                # This is a hedge order - put it on order1 (opposite side)!
                if not match.order1 or match.order1.token_id == token_id:
                    match.order1 = order
                    if not match.team1:
                        match.team1 = team
                    if not match.token1:
                        match.token1 = token_id
                        self._token_to_match[token_id] = match_id
                else:
                    # order1 exists with different token - collision
                    self._token_to_match[token_id] = match_id
                    if token_id not in self._collision_tokens:
                        logging.warning(f"Hedge collision: {team} (token {token_id[:16]}...) - order1 already exists")
                        self._collision_tokens.add(token_id)
                    return match
            elif match.order2 and match.order2.token_id != token_id:
                # CRITICAL: Don't overwrite if order2 exists with a DIFFERENT token!
                # Check if this is actually the COMPLEMENTARY token (order on the OTHER side)
                # Case 1: token_id matches token1 explicitly
                # Case 2: order1 slot is empty - this IS the complementary token!
                
                if token_id == match.token1 or not match.order1:
                    # This is the complementary token - assign to order1!
                    match.order1 = order
                    if not match.team1:
                        match.team1 = team
                    if not match.token1:
                        match.token1 = token_id
                        self._token_to_match[token_id] = match_id
                else:
                    # True collision: different market with same team name
                    # CRITICAL: Add to collision tokens - these should NEVER be discarded
                    self._token_to_match[token_id] = match_id
                    
                    # Only log warning the FIRST time we see this collision
                    if token_id not in self._collision_tokens:
                        logging.warning(f"Skipping duplicate team order: {team} (token {token_id[:16]}...) - order2 already has token {match.order2.token_id[:16]}...")
                        self._collision_tokens.add(token_id)
                    
                    return match  # Return without registering this order
            else:
                match.order2 = order
                if not match.token2:
                    match.token2 = token_id
                    self._token_to_match[token_id] = match_id
        else:
            # Can't determine side by team name.
            # Common cases:
            #   - Spread single-team: team2 is empty, incoming team is the opponent
            #   - O/U variant: team is "Under"/"Over", match teams are descriptive
            #
            # Strategy: fill empty slots first, then warn on true mismatches.
            
            # If team2 is empty, this IS the complementary team — assign silently
            if not match.team2 and not match.order2:
                match.team2 = team
                match.order2 = order
                if not match.token2:
                    match.token2 = token_id
                    self._token_to_match[token_id] = match_id
            elif not match.team1 and not match.order1:
                match.team1 = team
                match.order1 = order
                if not match.token1:
                    match.token1 = token_id
                    self._token_to_match[token_id] = match_id
            elif match.order2 and match.order2.token_id != token_id:
                # order2 exists with different token — check complementary
                if token_id == match.token1 or not match.order1:
                    match.order1 = order
                    if not match.team1:
                        match.team1 = team
                    if not match.token1:
                        match.token1 = token_id
                        self._token_to_match[token_id] = match_id
                elif match.order1 and match.order1.order_id == order_id:
                    # Re-registration of existing order1 (resync) — allow silently
                    match.order1 = order
                elif match.order2 and match.order2.order_id == order_id:
                    # Re-registration of existing order2 (resync) — allow silently
                    match.order2 = order
                else:
                    # True collision
                    self._token_to_match[token_id] = match_id
                    if token_id not in self._collision_tokens:
                        logging.warning(f"Cannot assign order to match {match_id}: {team} - order2 already exists with different token")
                        self._collision_tokens.add(token_id)
                    return match
            else:
                # O/U "Under"/"Over" or other variant — assign to order2
                team_lower = team.lower()
                is_ou_variant = team_lower in ("under", "over", "yes", "no") or team_lower.startswith("over ") or team_lower.startswith("under ")
                
                # Tennis suffix match: "Vidmanova" should match "Darja Vidmanova"
                team_norm = normalize_team(team)
                t1_norm = normalize_team(match.team1) if match.team1 else ""
                t2_norm = normalize_team(match.team2) if match.team2 else ""
                is_suffix_match = len(team_norm) >= 2 and (
                    t1_norm.endswith(team_norm) or t2_norm.endswith(team_norm)
                )
                
                # O/U suffix match: "FC Metz: O/U 3.5" -> base "FC Metz" matches team1/team2
                # Also handles "Not FC Metz: O/U 3.5" -> strip "Not " prefix
                import re
                is_ou_suffix_match = False
                ou_base_matches_t1 = False
                ou_sfx = re.search(r'[:\s]+(?:Set \d+ (?:Games )?)?O/U\s+[\d.]+$', team)
                if ou_sfx:
                    ou_base_raw = team[:ou_sfx.start()].strip()
                    # Strip "Not " prefix for NO token O/U markets
                    if ou_base_raw.startswith("Not "):
                        ou_base_raw = ou_base_raw[4:]
                    ou_base = normalize_team(ou_base_raw)
                    if ou_base == t1_norm:
                        is_ou_suffix_match = True
                        ou_base_matches_t1 = True
                    elif ou_base == t2_norm:
                        is_ou_suffix_match = True
                    # CONTAINMENT: "mavericks" in "dallasmavericks" (Polymarket nickname -> full name)
                    elif len(ou_base) >= 3 and t1_norm.endswith(ou_base):
                        is_ou_suffix_match = True
                        ou_base_matches_t1 = True
                    elif len(ou_base) >= 3 and t2_norm.endswith(ou_base):
                        is_ou_suffix_match = True
                
                # Spread suffix match: "Kings: Spread -1.5" -> base "Kings" matches team1/team2
                # Also handles "Not Kings: Spread -1.5" -> strip "Not " prefix
                is_spread_suffix_match = False
                spread_base_matches_t1 = False
                spread_sfx = re.search(r'[:\s]+Spread\s+[+-]?[\d.]+$', team)
                if spread_sfx:
                    spread_base_raw = team[:spread_sfx.start()].strip()
                    if spread_base_raw.startswith("Not "):
                        spread_base_raw = spread_base_raw[4:]
                    spread_base = normalize_team(spread_base_raw)
                    if spread_base == t1_norm:
                        is_spread_suffix_match = True
                        spread_base_matches_t1 = True
                    elif spread_base == t2_norm:
                        is_spread_suffix_match = True
                    # CONTAINMENT: "mavericks" in "dallasmavericks" (Polymarket nickname -> full name)
                    elif len(spread_base) >= 3 and t1_norm.endswith(spread_base):
                        is_spread_suffix_match = True
                        spread_base_matches_t1 = True
                    elif len(spread_base) >= 3 and t2_norm.endswith(spread_base):
                        is_spread_suffix_match = True
                
                # Spread prefix match: "Spread: Stade de Reims (-1.5)" -> "Stade de Reims"
                # Also handles "Not Spread: Stade de Reims (-1.5)"
                is_spread_prefix_match = False
                spread_prefix_matches_t1 = False
                spread_pfx = re.match(r'(?:Not\s+)?Spread:\s+(.+?)(?:\s+\([+-]?[\d.]+\))?\s*$', team)
                if spread_pfx:
                    spread_p_base = normalize_team(spread_pfx.group(1))
                    if spread_p_base == t1_norm:
                        is_spread_prefix_match = True
                        spread_prefix_matches_t1 = True
                    elif spread_p_base == t2_norm:
                        is_spread_prefix_match = True
                    # CONTAINMENT: "mavericks" in "dallasmavericks" (Polymarket nickname -> full name)
                    elif len(spread_p_base) >= 3 and t1_norm.endswith(spread_p_base):
                        is_spread_prefix_match = True
                        spread_prefix_matches_t1 = True
                    elif len(spread_p_base) >= 3 and t2_norm.endswith(spread_p_base):
                        is_spread_prefix_match = True
                
                # Bare "Not" prefix match: "Not CSyD Macará" -> "CSyD Macará" matches team1/team2
                is_not_prefix_match = False
                not_prefix_matches_t1 = False
                if team.startswith("Not ") and not is_ou_suffix_match and not is_spread_suffix_match and not is_spread_prefix_match:
                    not_base = normalize_team(team[4:])
                    if not_base == t1_norm:
                        is_not_prefix_match = True
                        not_prefix_matches_t1 = True
                    elif not_base == t2_norm:
                        is_not_prefix_match = True
                    # Suffix match: "Not Up" → "up" matches "hoodfeb27:up" (stock markets)
                    elif len(not_base) >= 2 and t1_norm.endswith(not_base):
                        is_not_prefix_match = True
                        not_prefix_matches_t1 = True
                    elif len(not_base) >= 2 and t2_norm.endswith(not_base):
                        is_not_prefix_match = True
                
                if is_ou_variant:
                    logging.debug(f"Assigning O/U variant '{team}' to match {match_id}")
                elif is_suffix_match:
                    logging.debug(f"Suffix match: '{team}' matches team in {match_id}")
                elif is_ou_suffix_match:
                    logging.debug(f"O/U suffix match: '{team}' base matches team in {match_id}")
                elif is_spread_suffix_match:
                    logging.debug(f"Spread suffix match: '{team}' base matches team in {match_id}")
                elif is_spread_prefix_match:
                    logging.debug(f"Spread prefix match: '{team}' base matches team in {match_id}")
                elif is_not_prefix_match:
                    logging.debug(f"Not prefix match: '{team}' base matches team in {match_id}")
                else:
                    logging.warning(f"Could not match team '{team}' to match {match_id} (team1={match.team1}, team2={match.team2})")
                
                # Assign to correct slot based on suffix/O/U/Spread match
                assign_to_order1 = (
                    (is_suffix_match and t1_norm.endswith(team_norm) and not match.order1) or
                    (is_ou_suffix_match and ou_base_matches_t1 and not match.order1) or
                    (is_spread_suffix_match and spread_base_matches_t1 and not match.order1) or
                    (is_spread_prefix_match and spread_prefix_matches_t1 and not match.order1) or
                    (is_not_prefix_match and not_prefix_matches_t1 and not match.order1)
                )
                if assign_to_order1:
                    match.order1 = order
                    if not match.token1:
                        match.token1 = token_id
                        self._token_to_match[token_id] = match_id
                else:
                    match.order2 = order
                    if not match.token2:
                        match.token2 = token_id
                        self._token_to_match[token_id] = match_id
        
        # Register order lookup
        self._order_to_match[order_id] = match_id
        self._token_to_match[token_id] = match_id
        
        # Clear any reservation - token is now properly tracked
        self._reserved_tokens.discard(token_id)
        
        match.touch()
        self._notify("order_registered", match, order)
        
        return match
    
    def update_order_fill(
        self,
        order_id: str,
        filled: float,
        price: Optional[float] = None,
    ) -> Optional[MatchState]:
        """
        Update an order's fill status.
        
        If fully filled, converts to a position.
        """
        match_id = self._order_to_match.get(order_id)
        match = self._matches.get(match_id) if match_id else None
        
        # Find the order (with fallback search if not in _order_to_match)
        order = None
        is_side1 = False
        
        if match:
            if match.order1 and match.order1.order_id == order_id:
                order = match.order1
                is_side1 = True
            elif match.order2 and match.order2.order_id == order_id:
                order = match.order2
                is_side1 = False
        
        # FALLBACK: Search all matches if order not found via _order_to_match
        # This handles hydrated orders that weren't properly added to _order_to_match
        if not order:
            for mid, m in self._matches.items():
                if m.order1 and m.order1.order_id == order_id:
                    order = m.order1
                    match = m
                    is_side1 = True
                    break
                if m.order2 and m.order2.order_id == order_id:
                    order = m.order2
                    match = m
                    is_side1 = False
                    break
        
        if not order:
            return None
        
        # Update fill
        order.filled = filled
        if price is not None:
            order.price = price
        
        # If fully filled, create/update position
        if order.is_filled:
            order.is_open = False
            
            position = MatchPosition(
                token_id=order.token_id,
                team=order.team,
                shares=order.filled,
                avg_price=order.price,
            )
            
            if is_side1:
                # Merge with existing position if any
                if match.position1:
                    total_shares = match.position1.shares + position.shares
                    total_cost = (match.position1.shares * match.position1.avg_price +
                                  position.shares * position.avg_price)
                    position.shares = total_shares
                    position.avg_price = total_cost / total_shares if total_shares > 0 else 0
                match.position1 = position
            else:
                if match.position2:
                    total_shares = match.position2.shares + position.shares
                    total_cost = (match.position2.shares * match.position2.avg_price +
                                  position.shares * position.avg_price)
                    position.shares = total_shares
                    position.avg_price = total_cost / total_shares if total_shares > 0 else 0
                match.position2 = position
            
            self._notify("order_filled", match, order)
        
        match.touch()
        return match
    
    def cancel_order(self, order_id: str) -> Optional[MatchState]:
        """
        Mark an order as cancelled - CLEARS from match state entirely.
        
        This prevents the scanner from re-placing orders for recently-cancelled
        tokens by adding the token to _reserved_tokens.
        
        Also tracks the order_id in _cancelled_order_ids to prevent re-registration
        during resync when Polymarket API has cache lag.
        """
        # RACE GUARD: If this order is being replaced (cancel + place new),
        # the WS handler may fire CANCELED before replace_order runs.
        # Skip cancelling to preserve _order_to_match and match slot integrity.
        if order_id in self._replacing_order_ids:
            return None
        
        match_id = self._order_to_match.get(order_id)
        if not match_id:
            return None
        
        match = self._matches.get(match_id)
        if not match:
            return None
        
        token_id = None
        if match.order1 and match.order1.order_id == order_id:
            token_id = match.order1.token_id
            match.order1 = None  # Clear entirely, not just is_open=False
        elif match.order2 and match.order2.order_id == order_id:
            token_id = match.order2.token_id
            match.order2 = None  # Clear entirely
        
        # Remove from order lookup
        if order_id in self._order_to_match:
            del self._order_to_match[order_id]
        
        # CRITICAL: Track cancelled order_id to prevent re-registration during resync
        # Polymarket API has cache lag - cancelled orders may still appear in API response
        self._cancelled_order_ids.add(order_id)
        
        # CRITICAL: Add token to reserved to prevent scanner from immediately re-placing
        # This blocks the token until next resync or explicit unreserve
        if token_id:
            self._reserved_tokens.add(token_id)
            logging.info(f"Order cancelled: token {token_id[:16]}... added to _reserved_tokens to prevent re-placing")
        
        match.touch()
        return match
    
    def get_cancelled_order_ids(self) -> Set[str]:
        """Get set of recently cancelled order IDs.
        
        Used by hydrate_active_orders to filter stale orders that Polymarket API
        still returns due to cache lag, preventing false duplicate detection.
        """
        return self._cancelled_order_ids.copy()
    
    def replace_order(
        self,
        old_order_id: str,
        new_order_id: str,
        new_price: float,
        token_id: str = "",
    ) -> Optional[MatchState]:
        """
        Replace an order with a new one (after cancel+replace).
        
        Tracks the old order_id in _cancelled_order_ids to prevent stale
        re-registration during hydration (Polymarket API has cache lag).
        
        Args:
            token_id: Optional fallback — if old_order_id is missing from
                      _order_to_match (cleared by a concurrent path), use
                      _token_to_match to find the match. Without this, the
                      new order becomes a ghost on the CLOB.
        """
        match_id = self._order_to_match.get(old_order_id)
        
        # FALLBACK: If old_order_id mapping is gone (concurrent clear_order),
        # use token_id to find the match. This prevents ghost orders.
        if not match_id and token_id:
            match_id = self._token_to_match.get(token_id)
            if match_id:
                logging.warning(
                    f"replace_order: old_order_id {old_order_id[:12]}... not in "
                    f"_order_to_match, fell back to token_id lookup → {match_id}"
                )
        
        if not match_id:
            logging.warning(
                f"replace_order: FAILED — no match found for order {old_order_id[:12]}... "
                f"(token_id={token_id[:12]}...). New order {new_order_id[:12]}... is a ghost!"
            )
            return None
        
        match = self._matches.get(match_id)
        if not match:
            return None
        
        # Find and update the order — check by old_order_id first, then by token_id
        updated = False
        if match.order1 and match.order1.order_id == old_order_id:
            match.order1.order_id = new_order_id
            match.order1.price = new_price
            match.order1.is_open = True
            self._order_to_match[new_order_id] = match_id
            updated = True
        elif match.order2 and match.order2.order_id == old_order_id:
            match.order2.order_id = new_order_id
            match.order2.price = new_price
            match.order2.is_open = True
            self._order_to_match[new_order_id] = match_id
            updated = True
        elif token_id:
            # old_order_id was already cleared — find the slot by token_id
            if match.order1 and match.order1.token_id == token_id:
                match.order1.order_id = new_order_id
                match.order1.price = new_price
                match.order1.is_open = True
                self._order_to_match[new_order_id] = match_id
                updated = True
            elif match.order2 and match.order2.token_id == token_id:
                match.order2.order_id = new_order_id
                match.order2.price = new_price
                match.order2.is_open = True
                self._order_to_match[new_order_id] = match_id
                updated = True
            elif not match.order1 and match.token1 == token_id:
                # Slot was None'd by clear_order — re-create it
                from src.state.match_state import MatchOrder
                match.order1 = MatchOrder(
                    order_id=new_order_id, token_id=token_id,
                    team=match.team1 or "", price=new_price, size=10.0, is_open=True,
                )
                self._order_to_match[new_order_id] = match_id
                updated = True
            elif not match.order2 and match.token2 == token_id:
                from src.state.match_state import MatchOrder
                match.order2 = MatchOrder(
                    order_id=new_order_id, token_id=token_id,
                    team=match.team2 or "", price=new_price, size=10.0, is_open=True,
                )
                self._order_to_match[new_order_id] = match_id
                updated = True
        
        if not updated:
            logging.warning(
                f"replace_order: match found ({match_id}) but could not update "
                f"any order slot for {old_order_id[:12]}... / token {token_id[:12]}..."
            )
        
        # Remove old mapping
        if old_order_id in self._order_to_match:
            del self._order_to_match[old_order_id]
        
        # CRITICAL: Track old order_id to prevent stale re-registration during hydration
        # Polymarket API has cache lag - old cancelled orders may still appear in API response
        self._cancelled_order_ids.add(old_order_id)
        
        match.touch()
        return match
    
    def clear_order(self, order_id: str) -> Optional[MatchState]:
        """
        Clear an order completely (remove from match state).
        
        Use this when an order is cancelled/invalid and should be forgotten.
        """
        # RACE GUARD: If this order is being replaced (cancel + place new),
        # the WS handler may fire CANCELED before replace_order runs.
        # Skip clearing to preserve _order_to_match and match slot integrity.
        if order_id in self._replacing_order_ids:
            return None
        
        match_id = self._order_to_match.get(order_id)
        if not match_id:
            # FALLBACK: Search through all matches for orders not in _order_to_match
            # This can happen when orders are placed directly and WebSocket fills come in
            # before the order is fully registered in _order_to_match
            for mid, match in self._matches.items():
                if match.order1 and match.order1.order_id == order_id:
                    match.order1 = None
                    match.touch()
                    return match
                if match.order2 and match.order2.order_id == order_id:
                    match.order2 = None
                    match.touch()
                    return match
            return None
        
        match = self._matches.get(match_id)
        if not match:
            return None
        
        # Clear the order from the match
        if match.order1 and match.order1.order_id == order_id:
            match.order1 = None
        elif match.order2 and match.order2.order_id == order_id:
            match.order2 = None
        
        # Remove from lookup
        if order_id in self._order_to_match:
            del self._order_to_match[order_id]
        
        match.touch()
        return match
    
    # ===== Hydration Methods =====
    
    def register_hydrated_order(
        self,
        order_id: str,
        token_id: str,
        team: str,
        price: float,
        size: float,
        match_question: str = "",
        condition_id: str = "",
        is_hedge: bool = False,
        odds_service=None,  # For rugby team lookup
        event_title: str = "",  # Event title ("Team A vs. Team B") for spread/O/U
        event_slug: str = "",  # Polymarket event slug for sport classification
        trading_deadline: "Optional[datetime]" = None,  # From Gamma API gameStartTime/endDate
        fair_value: "Optional[float]" = None,  # Fair value from odds lookup
    ) -> Optional[MatchState]:
        """
        Register a hydrated order (from previous session).
        
        This creates/updates the match state and registers the order.
        Used during startup to populate BotState with existing orders.
        
        Skips orders that were recently cancelled (to handle Polymarket API cache lag).
        """
        # CRITICAL: Skip orders that were recently cancelled
        # Polymarket API has cache lag - cancelled orders may still appear in API response
        if order_id in self._cancelled_order_ids:
            logging.info(f"Skipping re-registration of recently cancelled order {order_id[:16]}...")
            return None
        
        # CRITICAL FIX: Look up existing match by condition_id FIRST.
        # SpreadBot creates matches with a specific match_id format. If we regenerate
        # match_id from question parsing, we may get a DIFFERENT format (e.g., different
        # game prefix like 'rby' vs 'rugby', or single-team vs two-team format).
        # This causes _order_to_match desync: the order gets registered on a NEW match
        # while the original match still holds a stale reference, causing duplicate
        # hedge orders and false hedge coverage.
        existing_match = None
        if condition_id:
            existing_match = self._matches.get(
                self._condition_to_match.get(condition_id, ""), None
            )
        
        # Parse match info from question using shared function (with odds_service for rugby)
        game, team1, team2 = parse_match_question(match_question, odds_service, event_slug=event_slug)
        # Clean team names — strip "- More Markets", ": Spread", ": O/U" suffixes
        team1 = _clean_team_name(team1)
        team2 = _clean_team_name(team2)
        
        # SPORTS DETECTION: Questions with "vs." (period) and no game prefix are sports
        # Esports always have game prefix like "CS2: Team A vs Team B"
        # Sports use "Team A vs. Team B" (with period, no prefix)
        if not game and team1 and team2 and " vs. " in match_question:
            game = sport_from_slug(event_slug) or "unknown"
        
        # REDIS GAME LOOKUP: If game is unreliable, check Redis for persisted
        # condition_id → game mapping (written during order placement).
        # This is the primary mechanism for correct sport classification on restart.
        _RELIABLE_ESPORTS = {"cs2", "dota2", "lol", "valorant", "mlbb", "cod", "hok", "r6", "sc2"}
        game_norm = normalize_game(game) if game else ""
        if condition_id and game_norm not in _RELIABLE_ESPORTS:
            try:
                from src.infra.redis_cache import get_condition_game
                redis_game = get_condition_game(condition_id)
                if redis_game:
                    game = redis_game
            except Exception:
                pass
        
        # FIX #3: For single-team markets (spread/O/U), the individual question only has
        # one team. But the EVENT TITLE has both (e.g., "Portland Pilots vs. Seattle Redhawks").
        # Parse it to get both teams — this produces the same match_id as SpreadBot.
        if team1 and not team2 and event_title:
            cleaned_title = _clean_team_name(event_title)
            et_game, et_team1, et_team2 = parse_match_question(cleaned_title)
            et_team1 = _clean_team_name(et_team1)
            et_team2 = _clean_team_name(et_team2)
            if et_team1 and et_team2:
                # Use event title teams, keep the game from the question parse
                if not game and et_game:
                    game = et_game
                elif not game and " vs. " in event_title:
                    game = sport_from_slug(event_slug) or "unknown"  # Sports event with period
                team1 = et_team1
                team2 = et_team2
        
        # FIX #4: For O/U markets, detect the line number so we can include it
        # in team1 AFTER match_id is generated (to not corrupt the match_id).
        _ou_line = None
        if team1 and team2 and team.lower() in ("over", "under"):
            ou_line_match = re.search(r'(?:O/U|Over|Under)\s+([\d.]+)', match_question)
            if ou_line_match:
                _ou_line = ou_line_match.group(1)
        
        # If we found an existing match by condition_id, reuse its match_id
        # This ensures we always use the SAME match_id as the original creator
        if existing_match:
            match_id = existing_match.match_id
            # Still need sorted team names for register_order
            if team1 and team2:
                t1_norm = self._normalize_team(team1)
                t2_norm = self._normalize_team(team2)
                if t1_norm > t2_norm:
                    team1, team2 = team2, team1
            elif not team1:
                team1 = team or ""
            # Use existing match's game if we couldn't parse one
            if not game and existing_match.game:
                game = existing_match.game
        # Generate CANONICAL match_id (sorted, normalized - MUST match OddsService)
        elif team1 and team2:
            base_match_id = self._make_match_id(team1, team2, game)
            
            # NON-ESPORTS FIX: Include condition_id to prevent different Polymarket 
            # markets from colliding. Sports have multiple sub-markets per match
            # (winner, toss, spread, top batter, O/U, etc.) that share team names
            # but have different tokens. Without condition_id, they all collide.
            # Esports games only have one market per match, so they don't need this.
            _ESPORTS_GAMES = {"cs2", "dota2", "lol", "valorant", "mlbb", "cod", "hok", "r6", "sc2"}
            game_norm = normalize_game(game) if game else ""
            if condition_id and game_norm not in _ESPORTS_GAMES:
                # Use first 18 chars of condition_id for uniqueness
                match_id = f"{base_match_id}:{condition_id[:18]}"
            else:
                match_id = base_match_id
            
            # CRITICAL FIX: Sort team1/team2 to match the alphabetical order in match_id
            # This ensures fair_prob1 from OddsService aligns with match.team1
            t1_norm = self._normalize_team(team1)
            t2_norm = self._normalize_team(team2)
            if t1_norm > t2_norm:
                # Swap to alphabetical order
                team1, team2 = team2, team1
        elif team1 and game in ("rugby", "cricket", "football", "basketball", "hockey", "ufc"):
            # RUGBY/CRICKET/FOOTBALL/NCAAB "Will X win?" MARKET:
            # SpreadBot generates: make_match_id(team_name, team_name+" No", game):{cond[:18]}
            # We MUST match that format. Generate the "No" team name.
            team2 = f"{team1} No"
            base_match_id = self._make_match_id(team1, team2, game)
            if condition_id:
                match_id = f"{base_match_id}:{condition_id[:18]}"
            else:
                match_id = base_match_id
        elif team1 and game in ("basketball", "hockey", "ufc"):
            # SPORTS SINGLE-TEAM MARKET: "Spread: Team Name (-2.5)" format
            # Only one team parsed — use condition_id for unique match_id
            if condition_id:
                match_id = f"{game}:{condition_id[:18]}_{self._normalize_team(team1)}"
            else:
                match_id = f"{game}:{self._normalize_team(team1)}"
            team2 = ""
        elif team1 and game == "tennis":
            # TENNIS SINGLE-TEAM MARKET: "Set Handicap: Baena (-1.5)" format
            # Only one team parsed — use condition_id for unique match_id
            if condition_id:
                match_id = f"tennis:{condition_id[:18]}_{self._normalize_team(team1)}"
            else:
                match_id = f"tennis:{self._normalize_team(team1)}"
            team2 = ""
        elif game in ("basketball", "hockey", "ufc") and not team1 and not team2:
            # SPORTS O/U MARKET: question is just "Over" or "Under" — no team info
            # Use condition_id-only match_id
            if condition_id:
                match_id = f"{game}:ou:{condition_id[:18]}"
                team1 = condition_id[:18]  # placeholder for BotState
                team2 = ""
            else:
                logging.warning(f"{game} O/U market with no condition_id, skipping: '{match_question}'")
                return None
        elif game == "tennis" and not team1 and not team2:
            # TENNIS O/U MARKET: "Match O/U 23.5", "Over 2.5", "Under 23.5", etc.
            # Use condition_id-only match_id
            if condition_id:
                match_id = f"tennis:ou:{condition_id[:18]}"
                team1 = condition_id[:18]  # placeholder for BotState
                team2 = ""
            else:
                logging.warning(f"Tennis O/U market with no condition_id, skipping: '{match_question}'")
                return None
        else:
            # GENERIC BINARY MARKET FALLBACK: Some spread-bot markets have unparseable
            # questions (e.g., "Will Trump's meeting with Netanyahu not air?").
            # Use condition_id + team param for unique identification.
            if condition_id and team:
                match_id = f"spread:{condition_id[:18]}"
                team1 = team
                team2 = ""
                game = "mentions"  # Treat as mentions-like for hedging purposes
                logging.info(f"Generic binary market, using fallback match_id: {match_id} (team={team})")
            else:
                raise RuntimeError(
                    f"Failed to parse teams from match question during order hydration: '{match_question}'. "
                    f"Parsed: game={game}, team1={team1}, team2={team2}. "
                    f"This likely means a missing team alias in match_id.py"
                )
        
        # FIX #4 (continued): Apply O/U line suffix to team1 for OddsService alignment.
        # This MUST happen after match_id is generated (so the O/U suffix doesn't
        # corrupt the base match_id that OddsService searches by prefix).
        if _ou_line and ": O/U" not in team1:
            team1 = f"{team1}: O/U {_ou_line}"
        
        # Register match
        match = self.register_match(
            match_id=match_id,
            condition_id=condition_id,
            game=game,
            team1=team1,
            team2=team2,
            trading_deadline=trading_deadline,
        )
        
        # Register order
        return self.register_order(
            match_id=match_id,
            order_id=order_id,
            token_id=token_id,
            team=team,
            price=price,
            size=size,
            is_entry=not is_hedge,
            fair_value=fair_value,
        )
    
    def register_hydrated_position(
        self,
        token_id: str,
        team: str,
        shares: float,
        avg_price: float,
        condition_id: str = "",
        match_question: str = "",
        opponent_team: str = "",
        opponent_token: str = "",
        odds_service=None,  # For rugby team lookup
        event_title: str = "",  # Event title ("Team A vs. Team B") for spread/O/U
        event_slug: str = "",  # Polymarket event slug for sport classification
    ) -> Optional[MatchState]:
        """
        Register a hydrated position (filled shares from previous session).
        
        Used during startup to populate BotState with existing positions.
        """
        # CRITICAL FIX: Look up existing match by condition_id FIRST.
        # Same rationale as register_hydrated_order — prevent match_id mismatches
        # between SpreadBot's format and hydration's question-parsed format.
        existing_match = None
        if condition_id:
            existing_match = self._matches.get(
                self._condition_to_match.get(condition_id, ""), None
            )
        
        # Parse match info using shared function (with odds_service for rugby)
        game, team1, team2 = parse_match_question(match_question, odds_service, event_slug=event_slug)
        # Clean team names — strip "- More Markets", ": Spread", ": O/U" suffixes
        team1 = _clean_team_name(team1)
        team2 = _clean_team_name(team2)
        
        # SPORTS DETECTION: Questions with "vs." (period) and no game prefix are sports
        if not game and team1 and team2 and " vs. " in match_question:
            game = sport_from_slug(event_slug) or "unknown"
        
        # FIX #3: For single-team markets (spread/O/U), parse event title for both teams
        if team1 and not team2 and event_title:
            cleaned_title = _clean_team_name(event_title)
            et_game, et_team1, et_team2 = parse_match_question(cleaned_title)
            et_team1 = _clean_team_name(et_team1)
            et_team2 = _clean_team_name(et_team2)
            if et_team1 and et_team2:
                if not game and et_game:
                    game = et_game
                elif not game and " vs. " in event_title:
                    game = sport_from_slug(event_slug) or "unknown"
                team1 = et_team1
                team2 = et_team2
        
        # RUGBY NO TOKEN FIX: If team is just "No" (hydration extraction failed),
        # reconstruct the correct team name using the parsed match info
        if team == "No" and team1 and match_question:
            # This is a rugby NO token - team1 contains the actual team name
            team = f"{team1} No"
        
        if not team1 and not team2 and opponent_team:
            # Use team + opponent to construct match
            team1 = team
            team2 = opponent_team
        
        # If we found an existing match by condition_id, reuse its match_id
        if existing_match:
            match_id = existing_match.match_id
            if team1 and team2:
                t1_norm = self._normalize_team(team1)
                t2_norm = self._normalize_team(team2)
                if t1_norm > t2_norm:
                    team1, team2 = team2, team1
            elif not team1:
                team1 = team or ""
            if not game and existing_match.game:
                game = existing_match.game
        # Generate CANONICAL match_id (sorted, normalized - MUST match OddsService)
        elif team1 and team2:
            # CRITICAL FIX: Sort team1/team2 to match the alphabetical order in match_id
            # This ensures fair_prob1 from OddsService aligns with match.team1
            t1_norm = self._normalize_team(team1)
            t2_norm = self._normalize_team(team2)
            if t1_norm > t2_norm:
                # Swap to alphabetical order
                team1, team2 = team2, team1
            
            if game:
                base_match_id = self._make_match_id(team1, team2, game)
                # NON-ESPORTS FIX: Include condition_id to prevent collisions.
                # Sports have multiple sub-markets per match with same team names.
                _ESPORTS_GAMES = {"cs2", "dota2", "lol", "valorant", "mlbb", "cod", "hok", "r6", "sc2"}
                game_norm = normalize_game(game) if game else ""
                if condition_id and game_norm not in _ESPORTS_GAMES:
                    match_id = f"{base_match_id}:{condition_id[:18]}"
                else:
                    match_id = base_match_id
            else:
                # For no-game case, still sort for consistency
                match_id = f"match:{t1_norm}:vs:{t2_norm}" if t1_norm < t2_norm else f"match:{t2_norm}:vs:{t1_norm}"
        elif team1 and game in ("rugby", "cricket", "football", "basketball", "hockey", "ufc"):
            # RUGBY/CRICKET/FOOTBALL/NCAAB "Will X win?" MARKET:
            # Match SpreadBot format: make_match_id(team_name, team_name+" No", game):{cond[:18]}
            team2 = f"{team1} No"
            base_match_id = self._make_match_id(team1, team2, game)
            if condition_id:
                match_id = f"{base_match_id}:{condition_id[:18]}"
            else:
                match_id = base_match_id
        elif team1 and game in ("basketball", "hockey", "ufc"):
            # SPORTS SINGLE-TEAM MARKET: "Spread: Team Name (-2.5)" format
            if condition_id:
                match_id = f"{game}:{condition_id[:18]}_{self._normalize_team(team1)}"
            else:
                match_id = f"{game}:{self._normalize_team(team1)}"
            team2 = ""
        elif game in ("basketball", "hockey", "ufc") and not team1 and not team2:
            # SPORTS O/U MARKET: question is just "Over" or "Under"
            if condition_id:
                match_id = f"{game}:ou:{condition_id[:18]}"
                team1 = condition_id[:18]
                team2 = ""
            else:
                logging.warning(f"{game} O/U position with no condition_id, skipping: '{match_question}'")
                return None
        else:
            # Can't parse teams — expected for non-team markets (mentions, UFC rounds, etc.)
            logging.debug(
                f"Skipping non-team position question: '{match_question}'. "
                f"Parsed: game={game}, team1={team1}, team2={team2}."
            )
            return None
        
        # Look up by condition_id first
        if condition_id:
            existing = self.get_match_by_condition(condition_id)
            if existing:
                match_id = existing.match_id
        
        # Register/get match - DON'T pass token yet, we need to check match's team assignments
        match = self.register_match(
            match_id=match_id,
            condition_id=condition_id,
            game=game,
            team1=team1,
            team2=team2,
            token1="",  # Set correctly below
            token2="",  # Set correctly below
        )
        
        
        # CRITICAL FIX: Compare position team against MATCH's team assignments, NOT parsed names!
        # The match may have team1/team2 in different order than parse_match_question returned.
        # Use normalized comparison to handle "Dplus KIA Challengers" vs "Dplus Challengers" etc.
        # RUGBY FIX: Strip " No" suffix before comparison (rugby NO tokens have " No" appended)
        team_for_comparison = team.removesuffix(" No") if team else team
        team_for_comparison = _clean_team_name(team_for_comparison)
        team_normalized = self._normalize_team(team_for_comparison)
        match_team1_clean = _clean_team_name(match.team1) if match.team1 else ""
        match_team2_clean = _clean_team_name(match.team2) if match.team2 else ""
        match_team1_normalized = self._normalize_team(match_team1_clean)
        match_team2_normalized = self._normalize_team(match_team2_clean)
        
        # WEATHER MARKET FIX: Handle weather position team formats
        # Position team can come in as:
        #   - "Yes" / "No" (raw Polymarket outcome)
        #   - "city:temp" / "city:temp No" (from hydration transformation)
        #   - "city be:temp No" (incorrectly parsed, with "be" captured as part of city)
        # Match teams may be: "city:temp" or just "temp" (without city prefix)
        #
        # STOCK MARKET FIX: Handle stock position team formats
        # Position team from hydration: "Up" / "Down"
        # Match teams are: "TICKER:Up" / "TICKER:Down"
        weather_matched = False
        stock_matched = False
        
        # Detect stock market from team name pattern (TICKER:Up / TICKER:Down)
        is_stock_market = game == "stock" or (
            match.team1 and match.team2 and 
            (match.team1.endswith(":Up") or match.team1.endswith(":Down")) and
            (match.team2.endswith(":Up") or match.team2.endswith(":Down"))
        )
        
        if is_stock_market:
            # Stock positions come as "Up" or "Down" but match teams are "TICKER:Up" / "TICKER:Down"
            if team_normalized in ("up", "down"):
                # Extract ticker prefix from existing match team (e.g., "CL-feb6" from "CL-feb6:Down")
                ticker_prefix = ""
                if match.team1 and ":" in match.team1:
                    ticker_prefix = match.team1.rsplit(":", 1)[0]
                elif match.team2 and ":" in match.team2 and match.team2 not in ("Not ", ""):
                    ticker_prefix = match.team2.rsplit(":", 1)[0]
                
                # FIX: Handle incomplete stock matches where team2 is garbage (e.g., "Not ")
                # This happens when only one side was hydrated first
                if ticker_prefix:
                    expected_team = f"{ticker_prefix}:{team_normalized.capitalize()}"
                    
                    # Fix incomplete match by setting missing team
                    if team_normalized == "up":
                        if not match.team1 or not match.team1.endswith(":Up"):
                            # Check if team2 has :Up
                            if match.team2 and match.team2.endswith(":Up"):
                                if not match.token2:
                                    match.token2 = token_id
                                    self._token_to_match[token_id] = match_id
                                stock_matched = True
                            else:
                                # Need to set team1 to the Up side
                                if not match.team1 or match.team1.endswith(":Down"):
                                    # team1 is Down, team2 should be Up
                                    match.team2 = expected_team
                                    if not match.token2:
                                        match.token2 = token_id
                                        self._token_to_match[token_id] = match_id
                                    stock_matched = True
                                else:
                                    match.team1 = expected_team
                                    if not match.token1:
                                        match.token1 = token_id
                                        self._token_to_match[token_id] = match_id
                                    stock_matched = True
                        else:
                            if not match.token1:
                                match.token1 = token_id
                                self._token_to_match[token_id] = match_id
                            stock_matched = True
                    elif team_normalized == "down":
                        if match.team1 and match.team1.endswith(":Down"):
                            if not match.token1:
                                match.token1 = token_id
                                self._token_to_match[token_id] = match_id
                            stock_matched = True
                        elif match.team2 and match.team2.endswith(":Down"):
                            if not match.token2:
                                match.token2 = token_id
                                self._token_to_match[token_id] = match_id
                            stock_matched = True
                        else:
                            # Fix incomplete match
                            if match.team1 and match.team1.endswith(":Up"):
                                match.team2 = expected_team
                                if not match.token2:
                                    match.token2 = token_id
                                    self._token_to_match[token_id] = match_id
                                stock_matched = True
        
        if game == "mentions":
            # MENTIONS: Use direct normalized name matching, NOT weather heuristics.
            # Mentions teams use "Not " prefix ("mentions:Not Witkoff") instead of " No" suffix,
            # so the weather " No" checks don't work. Direct matching handles alphabetical
            # team sorting correctly regardless of which side is Yes/No.
            if team_normalized == match_team1_normalized:
                if not match.token1:
                    match.token1 = token_id
                    self._token_to_match[token_id] = match_id
                weather_matched = True
            elif team_normalized == match_team2_normalized:
                if not match.token2:
                    match.token2 = token_id
                    self._token_to_match[token_id] = match_id
                weather_matched = True
        elif game == "weather":
            # Check for raw "Yes"/"No" outcomes
            if team_normalized in ("yes", "no"):
                if team_normalized == "yes":
                    if not match.token1:
                        match.token1 = token_id
                        self._token_to_match[token_id] = match_id
                    weather_matched = True
                else:  # "no"
                    if not match.token2:
                        match.token2 = token_id
                        self._token_to_match[token_id] = match_id
                    weather_matched = True
            # Check for position with " No" suffix -> maps to team2 (NO token)
            elif team and " No" in team:
                # Position ends with " No", this is the NO token
                if not match.token2:
                    match.token2 = token_id
                    self._token_to_match[token_id] = match_id
                weather_matched = True
            # Check if position team contains match.team1 (YES token) - handles city:temp format
            elif team and match.team1 and match.team1 in team and "Not " not in team:
                # Position contains the YES team temperature bin
                if not match.token1:
                    match.token1 = token_id
                    self._token_to_match[token_id] = match_id
                weather_matched = True
            # Fallback: position with ":" but no "No"/"Not" is probably YES token
            elif team and ":" in team and " No" not in team and "Not " not in team:
                # Position is "city:temp", which is the YES token
                if not match.token1:
                    match.token1 = token_id
                    self._token_to_match[token_id] = match_id
                weather_matched = True
        
        if weather_matched or stock_matched:
            pass  # Already handled above
        elif team_normalized == match_team1_normalized:
            # Position team matches match.team1 -> this token is token1
            if not match.token1:
                match.token1 = token_id
                self._token_to_match[token_id] = match_id
        elif team_normalized == match_team2_normalized:
            # Position team matches match.team2 -> this token is token2
            if not match.token2:
                match.token2 = token_id
                self._token_to_match[token_id] = match_id
        else:
            # Team doesn't match either side by normalized name.
            # Handle known cases before warning:
            handled = False
            
            # Case 0: Position team has O/U suffix — e.g. "Newcastle United FC: O/U 3.5"
            # Strip ": O/U X.5" (or similar) and match the base team name.
            # This happens when the position outcome includes the market label.
            import re
            ou_suffix_match = re.search(r'[:\s]+(?:Set \d+ (?:Games )?)?O/U\s+[\d.]+$', team)
            if not handled and ou_suffix_match:
                base_team = team[:ou_suffix_match.start()].strip()
                base_normalized = self._normalize_team(base_team)
                if base_normalized == match_team1_normalized:
                    if not match.token1:
                        match.token1 = token_id
                        self._token_to_match[token_id] = match_id
                    handled = True
                elif base_normalized == match_team2_normalized:
                    if not match.token2:
                        match.token2 = token_id
                        self._token_to_match[token_id] = match_id
                    handled = True
            
            # Case 1: O/U positions — "Over"/"Under"/"Over 2.5" etc. for NCAAB and tennis
            # Tennis O/U teams have format "Jorge: Set 1 Games O/U 10.5" (O/U not right after colon)
            is_over = team_normalized.startswith("over")
            is_under = team_normalized.startswith("under")
            if is_over or is_under:
                # Find which team has "O/U" in its name — that's the O/U market
                ou_on_team1 = match.team1 and "O/U" in match.team1
                ou_on_team2 = match.team2 and "O/U" in match.team2
                if ou_on_team1:
                    # O/U is on team1 side. "Over" -> token1, "Under" -> token2
                    if is_over:
                        if not match.token1:
                            match.token1 = token_id
                            self._token_to_match[token_id] = match_id
                    else:
                        if not match.token2:
                            match.token2 = token_id
                            self._token_to_match[token_id] = match_id
                    handled = True
                elif ou_on_team2:
                    if is_over:
                        if not match.token2:
                            match.token2 = token_id
                            self._token_to_match[token_id] = match_id
                    else:
                        if not match.token1:
                            match.token1 = token_id
                            self._token_to_match[token_id] = match_id
                    handled = True
            
            # Case 2: Tennis last-name outcomes — position "Lee" should match "Gabriela Lee"
            # Suffix match: if the position team is a suffix of a match team name
            if not handled and len(team_normalized) >= 3:
                if match_team1_normalized.endswith(team_normalized):
                    if not match.token1:
                        match.token1 = token_id
                        self._token_to_match[token_id] = match_id
                    handled = True
                elif match_team2_normalized.endswith(team_normalized):
                    if not match.token2:
                        match.token2 = token_id
                        self._token_to_match[token_id] = match_id
                    handled = True
            
            # Case 4a: Halftime/1H "Not" tokens — "Not 1H Moneyline", "Not 1H Spread", etc.
            # These are generic No-side outcomes that don't contain a team name.
            # Assign directly to token2 (the No side).
            if not handled and team.startswith("Not ") and re.match(
                r'^Not\s+1H\s+', team, re.IGNORECASE
            ):
                if not match.token2:
                    match.token2 = token_id
                    self._token_to_match[token_id] = match_id
                handled = True
            
            # Case 4: Spread "Not" tokens — "Not Antalyaspor (-2.5)" → base "Antalyaspor"
            # Strip "Not " prefix and spread suffix "(-X.5)" to match base team
            if not handled and team.startswith("Not "):
                not_base = re.sub(r'\s*\([-+]?\d+\.?\d*\)\s*$', '', team[4:]).strip()
                not_base_norm = self._normalize_team(not_base)
                if not_base_norm == match_team1_normalized:
                    if not match.token2:
                        match.token2 = token_id
                        self._token_to_match[token_id] = match_id
                    handled = True
                elif not_base_norm == match_team2_normalized:
                    if not match.token1:
                        match.token1 = token_id
                        self._token_to_match[token_id] = match_id
                    handled = True
            
            # Case 5: Bare "Yes"/"No" for mentions markets — assign based on match team prefixes
            if not handled and team_normalized in ("yes", "no"):
                is_yes = team_normalized == "yes"
                # For mentions, team1 is usually the positive side (mentions:X)
                # and team2 is negative (mentions:Not X)
                t1_is_not = match.team1 and match.team1.lower().startswith("mentions:not ")
                if is_yes:
                    # "Yes" → positive side (whichever doesn't have "Not")
                    target_slot = "token2" if t1_is_not else "token1"
                else:
                    # "No" → negative side (whichever has "Not")
                    target_slot = "token1" if t1_is_not else "token2"
                if target_slot == "token1" and not match.token1:
                    match.token1 = token_id
                    self._token_to_match[token_id] = match_id
                elif target_slot == "token2" and not match.token2:
                    match.token2 = token_id
                    self._token_to_match[token_id] = match_id
                handled = True
            
            # Case 3: Empty team slot — incoming team is the complementary side (e.g. Spread)
            if not handled:
                if not match.team2:
                    match.team2 = team
                    if not match.token2:
                        match.token2 = token_id
                        self._token_to_match[token_id] = match_id
                    handled = True
                elif not match.team1:
                    match.team1 = team
                    if not match.token1:
                        match.token1 = token_id
                        self._token_to_match[token_id] = match_id
                    handled = True
            
            if not handled:
                # True mismatch — warn once per team+match combo
                warn_key = f"{team}:{match_id}"
                if not hasattr(self, '_position_warn_cache'):
                    self._position_warn_cache = set()
                if warn_key not in self._position_warn_cache:
                    print(f"   ⚠️ Position team '{team}' doesn't match match teams: '{match.team1}' or '{match.team2}'")
                    self._position_warn_cache.add(warn_key)


        # Also register opponent token if provided
        # FIX: Use NORMALIZED team names for comparison, not exact string match
        # "Dplus KIA Challengers" != "Dplus Challengers" with exact match, but they're the same team!
        if opponent_token:
            team_normalized = self._normalize_team(team)
            team1_normalized = self._normalize_team(match.team1) if match.team1 else ""
            team2_normalized = self._normalize_team(match.team2) if match.team2 else ""
            
            if team_normalized == team1_normalized:
                # Position is for team1, so opponent goes to token2
                if not match.token2:
                    match.token2 = opponent_token
            elif team_normalized == team2_normalized:
                # Position is for team2, so opponent goes to token1
                if not match.token1:
                    match.token1 = opponent_token
            # If neither matches, DON'T assign blindly - that's what caused the bug!
            
            self._token_to_match[opponent_token] = match_id
            

        
        # Create and assign position
        position = MatchPosition(
            token_id=token_id,
            team=team,
            shares=shares,
            avg_price=avg_price,
        )
        
        # Determine which side
        # NOTE: For hydration, we REPLACE the position (Polymarket API returns TOTAL position)
        # Do NOT merge/add - that would double-count on every re-sync!
        is_side1 = token_id == match.token1 or team.lower() == match.team1.lower()
        
        # COLLISION GUARD: Two DIFFERENT events with the same teams (e.g.,
        # "LoL: HMBLE vs CG (BO5) - LIT Playoffs" and "LoL: CG vs HMBLE (BO5) - LIT Playoffs")
        # can produce the same match_id (lol:colossal:vs:hmble) but have different
        # condition_ids and token_ids. If the match already has a DIFFERENT token on this
        # side, the incoming position is from a collision — skip it to avoid overwriting
        # the real position (e.g., 341 shares) with a smaller one (e.g., 32 shares).
        if is_side1:
            if match.token1 and match.token1 != token_id:
                # Different token on same side = collision from a different event
                return match
        else:
            if match.token2 and match.token2 != token_id:
                return match
        
        if is_side1:
            match.position1 = position
            if not match.token1:
                match.token1 = token_id
                self._token_to_match[token_id] = match_id
        else:
            match.position2 = position
            if not match.token2:
                match.token2 = token_id
                self._token_to_match[token_id] = match_id
        

        match.touch()
        self._notify("position_hydrated", match, position)
        return match
    
    # ===== Order Query Helpers =====
    
    def get_order_info(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        Get order info for a token in the format previously used by OrderWatcher._hydrated_orders.
        
        Returns dict with: order_id, token_id, price, size, team_name, match, fair_value, side
        Returns None if no open order exists for this token.
        """
        match = self.get_match_by_token(token_id)
        if not match:
            return None
        
        # Find the order for this token
        order = match.get_order_for_token(token_id)
        

        
        if not order or not order.is_open:
            return None
        
        # Determine fair value using canonical lookup (handles spreads/totals correctly)
        fair_value = match.get_fair_value_for_order(order)
        

        # Build display string
        match_display = f"{match.game}: {match.team1} vs {match.team2}" if match.game else f"{match.team1} vs {match.team2}"
        
        return {
            "order_id": order.order_id,
            "token_id": token_id,
            "condition_id": match.condition_id,  # For filled_orders recording
            "placed_at": order.placed_at,  # For time_to_fill calculation
            "price": order.price,
            "size": order.size,
            "filled": order.filled,
            "team_name": order.team,
            "match": match_display,
            "match_id": match.match_id,
            "game": match.game,  # For filled_orders recording
            "team1": match.team1,
            "team2": match.team2,
            "fair_value": fair_value,
            "side": "BUY",  # We only place BUY orders
            "is_hedge": match.is_order_hedge(order),
        }
    
    def get_all_open_order_infos(self) -> Dict[str, Dict[str, Any]]:
        """
        Get info for all open orders, keyed by token_id.
        
        This replaces OrderWatcher._hydrated_orders.
        """
        result = {}
        for match in self._matches.values():
            for order in [match.order1, match.order2]:
                if order and order.is_open:
                    token_id = order.token_id
                    # Use canonical lookup (handles spreads/totals correctly)
                    fair_value = match.get_fair_value_for_order(order)
                    match_display = f"{match.game}: {match.team1} vs {match.team2}" if match.game else f"{match.team1} vs {match.team2}"
                    
                    result[token_id] = {
                        "order_id": order.order_id,
                        "token_id": token_id,
                        "condition_id": match.condition_id,  # For filled_orders recording
                        "placed_at": order.placed_at,  # For time_to_fill calculation
                        "price": order.price,
                        "size": order.size,
                        "filled": order.filled,
                        "team_name": order.team,
                        "match": match_display,
                        "match_id": match.match_id,
                        "game": match.game,
                        "team1": match.team1,
                        "team2": match.team2,
                        "fair_value": fair_value,
                        "side": "BUY",
                        "is_hedge": match.is_order_hedge(order),
                    }
        

        return result
    
    def get_order_info_by_id(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Get order info by order_id.
        
        This replaces OrderWatcher._hydrated_order_id_map.
        """
        match = self.get_match_by_order(order_id)
        if not match:
            return None
        
        # Find the order
        order = None
        if match.order1 and match.order1.order_id == order_id:
            order = match.order1
        elif match.order2 and match.order2.order_id == order_id:
            order = match.order2
        
        if not order:
            return None
        
        return self.get_order_info(order.token_id)
    
    def get_orders_below_min_edge(self, include_hedges: bool = False, include_no_fair: bool = True) -> List[Tuple[MatchState, MatchOrder, float]]:
        """
        Get all open orders that are below minimum edge threshold.
        
        Args:
            include_hedges: If True, also check hedge orders (default: False for entries only)
            include_no_fair: If True, also include orders where we can't calculate edge (default: True)
        
        Returns: List of (match, order, current_edge) tuples
                 Edge is -999 for orders without fair value (to indicate cancellation needed)
        """
        results = []
        
        for match in self._matches.values():
            for order in [match.order1, match.order2]:
                if not order or not order.is_open:
                    continue
                
                # Skip hedges unless requested
                if match.is_order_hedge(order) and not include_hedges:
                    continue
                
                edge = match.get_order_edge(order)
                
                # If we can't calculate edge (no fair probs), flag for cancellation
                if edge is None:
                    if include_no_fair:
                        results.append((match, order, -999.0))  # Special value: no fair probs
                    continue
                
                if edge < self.min_edge:
                    results.append((match, order, edge))
        

        return results
    
    def get_stale_order_matches(self, stale_threshold: float) -> List[Tuple[MatchState, MatchOrder, float]]:
        """
        Get all open entry orders where odds are stale (older than threshold).
        
        Args:
            stale_threshold: Maximum age in seconds for odds to be considered fresh
        
        Returns: List of (match, order, odds_age_seconds) tuples
        """
        results = []
        
        for match in self._matches.values():
            # Skip if no odds timestamp (match just created, no odds yet)
            if match.odds_updated_at is None:
                continue
            
            # Check if odds are stale
            if not match.is_odds_stale(stale_threshold):
                continue
            
            odds_age = match.get_odds_age_seconds()
            
            for order in [match.order1, match.order2]:
                if not order or not order.is_open:
                    continue
                
                # Only cancel entry orders - hedges still need to close positions
                if match.is_order_hedge(order):
                    continue
                
                results.append((match, order, odds_age))
        
        return results
    
    def get_hedges_overpaying(self) -> List[Tuple[MatchState, MatchOrder, float]]:
        """
        Get all hedge orders that are overpaying (would result in poor arb profit).
        
        Returns: List of (match, order, potential_profit) tuples
        """
        results = []
        
        for match in self._matches.values():
            for order in [match.order1, match.order2]:
                if not order or not order.is_open:
                    continue
                
                # Skip non-hedge orders
                if not match.is_order_hedge(order):
                    continue
                
                if match.is_hedge_overpaying(order, self.min_arb_profit):
                    profit = match.get_arb_profit_percent()
                    if profit is not None:
                        results.append((match, order, profit))
        
        return results
    
    def get_open_orders(self) -> List[Tuple[MatchState, MatchOrder]]:
        """Get all open orders across all matches."""
        results = []
        for match in self._matches.values():
            if match.order1 and match.order1.is_open:
                results.append((match, match.order1))
            if match.order2 and match.order2.is_open:
                results.append((match, match.order2))
        

        return results
    
    def get_hedge_token_ids(self) -> set:
        """
        Get all token IDs of orders that are hedges (covering a filled position).
        
        A hedge order is an open order on the opposite side of an UNHEDGED position.
        To determine if an order is a hedge, we check if there's unhedged exposure on
        the OPPOSITE side WITHOUT counting the current order.
        
        CRITICAL: An order is ONLY a hedge if:
        1. There's a filled position on the opposite side
        2. That position has >= 5.0 UNHEDGED shares (not covered by existing positions/orders)
        3. The order size is close to the unhedged amount (within 50%)
        """
        hedge_tokens = set()
        
        for match in self._matches.values():
            # Check if order2 is a hedge for side1 positions
            # Calculate side1 unhedged shares WITHOUT including order2 itself
            if match.order2 and match.order2.is_open and match.position1:
                side1_shares = match.position1.shares
                side1_coverage = 0.0
                if match.position2:
                    side1_coverage += match.position2.shares
                # Don't include order2 itself (the order we're checking)
                # Don't include order1 either (it's on same side as position1, not a hedge)
                
                unhedged_side1 = max(0.0, side1_shares - side1_coverage)
                
                # Only mark as hedge if:
                # 1. Significant unhedged exposure (>= 5.0 shares)
                # 2. Order size is reasonable for hedging (at least 30% of unhedged amount)
                if unhedged_side1 >= 5.0:
                    # Check if order size is appropriate for a hedge
                    # (prevents standalone entry orders from being marked as hedges)
                    hedge_ratio = match.order2.size / unhedged_side1
                    if hedge_ratio >= 0.3:  # Order is at least 30% of unhedged amount
                        hedge_tokens.add(match.order2.token_id)
            
            # Check if order1 is a hedge for side2 positions
            # Calculate side2 unhedged shares WITHOUT including order1 itself
            if match.order1 and match.order1.is_open and match.position2:
                side2_shares = match.position2.shares
                side2_coverage = 0.0
                if match.position1:
                    side2_coverage += match.position1.shares
                # Don't include order1 itself (the order we're checking)
                # Don't include order2 either (it's on same side as position2, not a hedge)
                
                unhedged_side2 = max(0.0, side2_shares - side2_coverage)
                
                # Only mark as hedge if:
                # 1. Significant unhedged exposure (>= 5.0 shares)
                # 2. Order size is reasonable for hedging (at least 30% of unhedged amount)
                if unhedged_side2 >= 5.0:
                    # Check if order size is appropriate for a hedge
                    hedge_ratio = match.order1.size / unhedged_side2
                    if hedge_ratio >= 0.3:  # Order is at least 30% of unhedged amount
                        hedge_tokens.add(match.order1.token_id)
        
        return hedge_tokens
