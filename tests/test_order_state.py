"""
Unit tests for state/order_state.py - Order CRUD operations for BotState.
"""
import pytest
from unittest.mock import Mock, patch

from src.state.match_state import MatchState, MatchOrder, MatchPosition
from src.state.order_state import OrderStateMixin


class MockBotState(OrderStateMixin):
    """Mock BotState that includes OrderStateMixin."""
    
    def __init__(self):
        self._matches: dict = {}
        self._order_to_match: dict = {}
        self._token_to_match: dict = {}
        self._condition_to_match: dict = {}
        self._reserved_tokens: set = set()
        self._collision_tokens: set = set()
        self._cancelled_order_ids: set = set()
        self._replacing_order_ids: set = set()
        self._callbacks: dict = {}
        self._rugby_no_orders: dict = {}  # For rugby No token tracking
    
    @staticmethod
    def _normalize_team(name: str) -> str:
        """Normalize team name (simplified)."""
        return name.lower().strip()
    
    @staticmethod
    def _make_match_id(team1: str, team2: str, game: str) -> str:
        """Create canonical match ID."""
        t1 = team1.lower().strip()
        t2 = team2.lower().strip()
        if t1 > t2:
            t1, t2 = t2, t1
        return f"{game}:{t1}:vs:{t2}" if game else f"{t1}:vs:{t2}"
    
    def get_match_by_condition(self, condition_id: str):
        """Get match by condition ID."""
        match_id = self._condition_to_match.get(condition_id)
        return self._matches.get(match_id) if match_id else None
    
    def register_match(
        self,
        match_id: str,
        condition_id: str = "",
        game: str = "",
        team1: str = "",
        team2: str = "",
        token1: str = "",
        token2: str = "",
        trading_deadline=None,
    ) -> MatchState:
        """Register a match."""
        if match_id in self._matches:
            # Return existing match
            return self._matches[match_id]
        
        match = MatchState(
            match_id=match_id,
            condition_id=condition_id,
            game=game,
            team1=team1,
            team2=team2,
            token1=token1,
            token2=token2,
            trading_deadline=trading_deadline,
        )
        self._matches[match_id] = match
        if token1:
            self._token_to_match[token1] = match_id
        if token2:
            self._token_to_match[token2] = match_id
        if condition_id:
            self._condition_to_match[condition_id] = match_id
        return match
    
    def _notify(self, event_type: str, match: MatchState, *args):
        """Notify callbacks (mock)."""
        pass
    
    def get_order_info(self, token_id: str) -> dict | None:
        """Get order info by token ID."""
        match_id = self._token_to_match.get(token_id)
        if not match_id:
            return None
        match = self._matches.get(match_id)
        if not match:
            return None
        
        if match.order1 and match.order1.token_id == token_id:
            return {
                "order_id": match.order1.order_id,
                "price": match.order1.price,
                "size": match.order1.size,
                "team": match.order1.team,
            }
        if match.order2 and match.order2.token_id == token_id:
            return {
                "order_id": match.order2.order_id,
                "price": match.order2.price,
                "size": match.order2.size,
                "team": match.order2.team,
            }
        return None
    
    def is_token_active(self, token_id: str) -> bool:
        """Check if token has an active order or reservation."""
        # Reserved
        if token_id in self._reserved_tokens:
            return True
        # Collision
        if token_id in self._collision_tokens:
            return True
        # Has order
        match_id = self._token_to_match.get(token_id)
        if match_id:
            match = self._matches.get(match_id)
            if match:
                if match.order1 and match.order1.token_id == token_id:
                    return True
                if match.order2 and match.order2.token_id == token_id:
                    return True
        return False


class TestRegisterOrder:
    """Tests for register_order method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create a fresh mock bot state."""
        return MockBotState()
    
    def test_register_order_creates_match_if_missing(self, bot_state):
        """Auto-creates match if it doesn't exist."""
        result = bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token456",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        assert result is not None
        assert "cs2:liquid:vs:navi" in bot_state._matches
    
    def test_register_order_assigns_to_token1_slot(self, bot_state):
        """Order with matching token1 goes to order1 slot."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order1 is not None
        assert match.order1.order_id == "order123"
        assert match.order2 is None
    
    def test_register_order_assigns_to_token2_slot(self, bot_state):
        """Order with matching token2 goes to order2 slot."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_navi",
            team="Natus Vincere",
            price=0.45,
            size=10.0,
        )
        
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order2 is not None
        assert match.order2.order_id == "order123"
        assert match.order1 is None
    
    def test_register_order_both_sides(self, bot_state):
        """Can register orders on both sides (natural arb)."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order1",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order2",
            token_id="token_navi",
            team="Natus Vincere",
            price=0.45,
            size=10.0,
        )
        
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order1 is not None
        assert match.order2 is not None
        assert match.order1.order_id == "order1"
        assert match.order2.order_id == "order2"
    
    def test_register_order_updates_lookups(self, bot_state):
        """Order registration updates lookup dictionaries."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
        )
        
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        assert bot_state._order_to_match["order123"] == "cs2:liquid:vs:navi"
        assert bot_state._token_to_match["token_liquid"] == "cs2:liquid:vs:navi"
    
    def test_register_order_clears_reservation(self, bot_state):
        """Registration clears token reservation."""
        bot_state._reserved_tokens.add("token_liquid")
        
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
        )
        
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        assert "token_liquid" not in bot_state._reserved_tokens


class TestRegisterOrderCollisions:
    """Tests for token collision handling in register_order."""
    
    @pytest.fixture
    def bot_state(self):
        """Create a fresh mock bot state."""
        return MockBotState()
    
    def test_collision_same_team_different_token(self, bot_state):
        """When same team has different token and order2 is empty, fills order2 (arb setup)."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid_old",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        
        # First order registers normally
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order1",
            token_id="token_liquid_old",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        # Second order with DIFFERENT token for same team
        # Since order2 is empty, this fills order2 as complementary token
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order2",
            token_id="token_liquid_new",
            team="Team Liquid",
            price=0.52,
            size=10.0,
        )
        
        match = bot_state._matches["cs2:liquid:vs:navi"]
        # Original order should still be in place
        assert match.order1.order_id == "order1"
        assert match.order1.token_id == "token_liquid_old"
        # New order went to order2 slot as complementary (arb setup)
        assert match.order2.order_id == "order2"
        assert match.order2.token_id == "token_liquid_new"
    
    def test_same_token_different_order_id_preserves_open_order(self, bot_state):
        """Additive-only: open order with different ID is PRESERVED (not overwritten).
        
        This is the core fix for duplicate hedge orders. When rehydration
        tries to re-register with a different order_id, the existing open
        order is kept.
        """
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order1",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        # Same token, different order ID, but order1 is OPEN → should be preserved
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order2",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.52,
            size=10.0,
        )
        
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order1.order_id == "order1"  # PRESERVED, not overwritten
        assert match.order1.price == 0.50
    
    def test_same_token_same_order_id_updates(self, bot_state):
        """Same order_id is allowed to update (metadata refresh)."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            team1="liquid",
        )
        
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order1",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        # Same order_id, updated price → should update
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order1",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.55,
            size=10.0,
        )
        
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order1.order_id == "order1"
        assert match.order1.price == 0.55  # Updated
    
    def test_same_token_overwrites_closed_order(self, bot_state):
        """Closed/filled orders can be overwritten by new orders."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            team1="liquid",
        )
        
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order1",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        # Close the order
        match = bot_state._matches["cs2:liquid:vs:navi"]
        match.order1.is_open = False
        
        # New order for same token → should overwrite (order is closed)
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order2",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.52,
            size=10.0,
        )
        
        assert match.order1.order_id == "order2"  # Overwritten
        assert match.order1.price == 0.52
    
    def test_additive_only_for_token2_slot(self, bot_state):
        """Additive-only guard also works for order2 slot."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="hedge_order",
            token_id="token_navi",
            team="Navi",
            price=0.45,
            size=10.0,
        )
        
        # Rehydration tries to overwrite with different order → preserved
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="hydrated_order",
            token_id="token_navi",
            team="Navi",
            price=0.40,
            size=10.0,
        )
        
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order2.order_id == "hedge_order"  # PRESERVED


class TestReplaceOrder:
    """Tests for replace_order method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create bot state with an existing order."""
        state = MockBotState()
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="old_order",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        return state
    
    def test_replace_order_updates_order_id(self, bot_state):
        """replace_order updates the order ID."""
        result = bot_state.replace_order("old_order", "new_order", 0.52)
        
        assert result is not None
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order1.order_id == "new_order"
    
    def test_replace_order_updates_price(self, bot_state):
        """replace_order updates the price."""
        bot_state.replace_order("old_order", "new_order", 0.52)
        
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order1.price == 0.52
    
    def test_replace_order_updates_lookup(self, bot_state):
        """replace_order updates order_to_match lookup."""
        bot_state.replace_order("old_order", "new_order", 0.52)
        
        assert "old_order" not in bot_state._order_to_match
        assert bot_state._order_to_match["new_order"] == "cs2:liquid:vs:navi"
    
    def test_replace_order_not_found(self, bot_state):
        """replace_order returns None if order not found."""
        result = bot_state.replace_order("nonexistent", "new_order", 0.52)
        assert result is None


class TestClearOrder:
    """Tests for clear_order method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create bot state with an existing order."""
        state = MockBotState()
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        return state
    
    def test_clear_order_removes_order(self, bot_state):
        """clear_order removes the order from the match."""
        bot_state.clear_order("order123")
        
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order1 is None
    
    def test_clear_order_removes_lookup(self, bot_state):
        """clear_order removes order from lookup."""
        bot_state.clear_order("order123")
        
        assert "order123" not in bot_state._order_to_match
    
    def test_clear_order_not_found(self, bot_state):
        """clear_order handles missing order gracefully."""
        result = bot_state.clear_order("nonexistent")
        assert result is None


class TestGetOrderInfo:
    """Tests for get_order_info method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create bot state with orders."""
        state = MockBotState()
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        return state
    
    def test_get_order_info_by_token(self, bot_state):
        """get_order_info returns order info for token."""
        info = bot_state.get_order_info("token_liquid")
        
        assert info is not None
        assert info["order_id"] == "order123"
        assert info["price"] == 0.50
        assert info["team"] == "Team Liquid"
    
    def test_get_order_info_not_found(self, bot_state):
        """get_order_info returns None for unknown token."""
        info = bot_state.get_order_info("unknown_token")
        assert info is None


class TestIsTokenActive:
    """Tests for is_token_active method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create bot state."""
        return MockBotState()
    
    def test_token_active_with_order(self, bot_state):
        """Token with order is active."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
        )
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        assert bot_state.is_token_active("token_liquid") is True
    
    def test_token_active_with_reservation(self, bot_state):
        """Reserved token is active."""
        bot_state._reserved_tokens.add("reserved_token")
        assert bot_state.is_token_active("reserved_token") is True
    
    def test_token_active_in_collision_set(self, bot_state):
        """Collision token is active."""
        bot_state._collision_tokens.add("collision_token")
        assert bot_state.is_token_active("collision_token") is True
    
    def test_token_not_active(self, bot_state):
        """Unknown token is not active."""
        assert bot_state.is_token_active("unknown_token") is False


class TestRegisterHydratedPosition:
    """Tests for register_hydrated_position method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create bot state with an existing open order."""
        state = MockBotState()
        # Use a condition_id so hydration can find this match
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            condition_id="cond123",
            token1="token_liquid",
            token2="token_navi",
            team1="Team Liquid",
            team2="Navi",
        )
        state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        return state
    
    def test_hydrated_position_marks_order_as_closed(self, bot_state):
        """When a position is hydrated, the corresponding order should be marked closed.
        
        This prevents ReactiveHandler from trying to cancel already-filled orders,
        which would result in "matched orders can't be canceled" errors.
        """
        # Verify order is open before hydration
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order1 is not None
        assert match.order1.is_open is True
        
        # Hydrate a position for the same token - use same condition_id to find match
        bot_state.register_hydrated_position(
            token_id="token_liquid",
            team="Team Liquid",
            shares=10.0,
            avg_price=0.50,
            condition_id="cond123",  # This makes it find the existing match
            match_question="CS2: Team Liquid vs Navi (BO3)",
        )
        
        # CORRECTED BEHAVIOR: Order should STAY OPEN!
        # Having a position doesn't mean the order is closed - hedge orders
        # can be partially filled (5 shares filled, 10 more still open).
        # The order status comes from Polymarket API during hydration, not inferred.
        assert match.order1.is_open is True  # Order stays open!
        
        # Position should be created
        assert match.position1 is not None
        assert match.position1.shares == 10.0
    
    def test_hydrated_position_side2_creates_position(self, bot_state):
        """Position hydration creates position without affecting order status."""
        # Add an order on side2
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order456",
            token_id="token_navi",
            team="Navi",
            price=0.45,
            size=10.0,
        )
        
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order2 is not None
        assert match.order2.is_open is True
        
        # Hydrate position for side2
        bot_state.register_hydrated_position(
            token_id="token_navi",
            team="Navi",
            shares=10.0,
            avg_price=0.45,
            condition_id="cond123",  # This makes it find the existing match
            match_question="CS2: Team Liquid vs Navi (BO3)",
        )
        
        # CORRECTED BEHAVIOR: Order should STAY OPEN!
        assert match.order2.is_open is True  # Order stays open!
        assert match.position2 is not None


class TestCancelledOrderTracking:
    """Tests for cancelled order ID tracking to prevent re-registration."""
    
    @pytest.fixture
    def bot_state(self):
        """Create bot state with an order."""
        state = MockBotState()
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            condition_id="cond123",
            token1="token_liquid",
            token2="token_navi",
            team1="Team Liquid",
            team2="Navi",
        )
        state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        return state
    
    def test_cancel_order_tracks_order_id(self, bot_state):
        """Cancelled orders are added to _cancelled_order_ids set."""
        # Initially no cancelled orders
        assert len(bot_state._cancelled_order_ids) == 0
        
        # Cancel the order
        bot_state.cancel_order("order123")
        
        # Order ID should now be tracked
        assert "order123" in bot_state._cancelled_order_ids
    
    def test_hydrated_order_skips_cancelled(self, bot_state):
        """Re-registering a cancelled order is skipped during hydration.
        
        This prevents the 'order can't be found' warning loop when
        Polymarket API has cache lag after order cancellation.
        """
        # Cancel the order first
        bot_state.cancel_order("order123")
        
        # Verify order was removed
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order1 is None
        
        # Try to re-register the same order (simulating API cache lag)
        result = bot_state.register_hydrated_order(
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
            condition_id="cond123",
            match_question="CS2: Team Liquid vs Navi (BO3)",
        )
        
        # Should return None (skipped)
        assert result is None
        
        # Order should NOT be re-registered
        assert match.order1 is None
    
    def test_replace_order_tracks_old_order_id(self, bot_state):
        """When an order is replaced, the OLD order_id is tracked.
        
        This prevents Polymarket API cache lag from overwriting
        new orders with stale old order data during resync.
        """
        # Initially no cancelled orders
        assert len(bot_state._cancelled_order_ids) == 0
        
        # Replace the order (simulating price adjustment)
        bot_state.replace_order("order123", "new_order456", 0.55)
        
        # OLD order ID should now be tracked
        assert "order123" in bot_state._cancelled_order_ids
        
        # Attempt to re-register the old order (simulating API cache lag)
        result = bot_state.register_hydrated_order(
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
            condition_id="cond123",
            match_question="CS2: Team Liquid vs Navi (BO3)",
        )
        
        # Should be skipped
        assert result is None
        
        # New order should still be in place
        match = bot_state._matches["cs2:liquid:vs:navi"]
        assert match.order1 is not None
        assert match.order1.order_id == "new_order456"
        assert match.order1.price == 0.55


class TestMultiTeamCollisionHandling:
    """Tests for handling same team name in multiple matches.
    
    Scenario: Team "STATE" plays in multiple CS2 matches:
    - FORZE Reload vs STATE
    - los kogutos vs STATE
    - Bebop vs STATE
    
    Each match should have a unique match_id and orders should be
    correctly assigned without collisions.
    """
    
    @pytest.fixture
    def bot_state(self):
        """Create mock bot state."""
        return MockBotState()
    
    def test_different_matches_get_different_match_ids(self, bot_state):
        """State playing multiple opponents should create different matches."""
        # Register first match: FORZE Reload vs STATE
        match1 = bot_state.register_match(
            match_id="cs2:forzereload:vs:state",
            game="cs2",
            team1="FORZE Reload",
            team2="STATE",
        )
        
        # Register second match: los kogutos vs STATE
        match2 = bot_state.register_match(
            match_id="cs2:loskogutos:vs:state",
            game="cs2",
            team1="los kogutos",
            team2="STATE",
        )
        
        # Register third match: Bebop vs STATE
        match3 = bot_state.register_match(
            match_id="cs2:bebop:vs:state",
            game="cs2",
            team1="Bebop",
            team2="STATE",
        )
        
        # All should be distinct matches
        assert len(bot_state._matches) == 3
        assert "cs2:forzereload:vs:state" in bot_state._matches
        assert "cs2:loskogutos:vs:state" in bot_state._matches
        assert "cs2:bebop:vs:state" in bot_state._matches
        
        # Each should have correct teams
        assert match1.team1 == "FORZE Reload"
        assert match2.team1 == "los kogutos"
        assert match3.team1 == "Bebop"
    
    def test_orders_register_to_correct_match(self, bot_state):
        """Orders for STATE should go to correct match based on match_id."""
        # Setup: Create all three matches
        bot_state.register_match(
            match_id="cs2:forzereload:vs:state",
            game="cs2",
            team1="FORZE Reload",
            team2="STATE",
            token1="tok_forze",
            token2="tok_state_forze",
        )
        bot_state.register_match(
            match_id="cs2:bebop:vs:state",
            game="cs2",
            team1="Bebop",
            team2="STATE",
            token1="tok_bebop",
            token2="tok_state_bebop",
        )
        
        # Register STATE order for FORZE match
        bot_state.register_order(
            match_id="cs2:forzereload:vs:state",
            order_id="order_state_forze",
            token_id="tok_state_forze",
            team="STATE",
            price=0.36,
            size=10.0,
        )
        
        # Register STATE order for Bebop match  
        bot_state.register_order(
            match_id="cs2:bebop:vs:state",
            order_id="order_state_bebop",
            token_id="tok_state_bebop",
            team="STATE",
            price=0.40,
            size=10.0,
        )
        
        # Each order should be in its correct match
        forze_match = bot_state._matches["cs2:forzereload:vs:state"]
        bebop_match = bot_state._matches["cs2:bebop:vs:state"]
        
        assert forze_match.order2.order_id == "order_state_forze"
        assert forze_match.order2.price == 0.36
        
        assert bebop_match.order2.order_id == "order_state_bebop"
        assert bebop_match.order2.price == 0.40
    
    def test_hydrated_order_preserves_match_integrity(self, bot_state):
        """Hydrating orders should not corrupt existing match team names."""
        # Create match with proper team names
        bot_state.register_match(
            match_id="cs2:forzereload:vs:state",
            game="cs2",
            team1="FORZE Reload",
            team2="STATE",
        )
        
        # Hydrate an order for this match
        bot_state.register_hydrated_order(
            order_id="order123",
            token_id="tok_forze",
            team="FORZE Reload",
            price=0.64,
            size=10.0,
            match_question="Counter-Strike: FORZE Reload vs STATE (BO3)",
        )
        
        # Match team names should remain correct
        match = bot_state._matches["cs2:forzereload:vs:state"]
        assert match.team1 == "FORZE Reload"
        assert match.team2 == "STATE"
        
        # Hydrate STATE order for same match
        bot_state.register_hydrated_order(
            order_id="order456",
            token_id="tok_state",
            team="STATE",
            price=0.36,
            size=10.0,
            match_question="Counter-Strike: FORZE Reload vs STATE (BO3)",
        )
        
        # Teams should still be correct
        assert match.team1 == "FORZE Reload"
        assert match.team2 == "STATE"
    
    def test_team_order_consistency_with_sorted_match_id(self, bot_state):
        """Match ID uses alphabetical sorting but team1/team2 preserve original order."""
        from src.core.match_id import make_match_id, normalize_team
        
        # "bebop" < "state" alphabetically, so match_id = cs2:bebop:vs:state
        match_id = make_match_id("Bebop", "STATE", "cs2")
        assert match_id == "cs2:bebop:vs:state"
        
        # But if we register with STATE first, team order depends on how we call register_match
        bot_state.register_match(
            match_id=match_id,
            game="cs2",
            team1="Bebop",  # First arg = team1
            team2="STATE",  # Second arg = team2
        )
        
        match = bot_state._matches[match_id]
        assert match.team1 == "Bebop"
        assert match.team2 == "STATE"

