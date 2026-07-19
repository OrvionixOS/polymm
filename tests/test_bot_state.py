"""
Unit tests for state/bot_state.py - Unified state management.
"""
import pytest
from unittest.mock import Mock, patch

from src.state.bot_state import BotState
from src.state.match_state import MatchState, MatchOrder, MatchPosition
from src.state.state_events import StateEventType


class TestBotStateInit:
    """Tests for BotState initialization."""
    
    def test_default_min_edge(self):
        """Default min_edge is 0.05."""
        state = BotState()
        assert state.min_edge == 0.05
    
    def test_custom_min_edge(self):
        """Custom min_edge is respected."""
        state = BotState(min_edge=0.10)
        assert state.min_edge == 0.10
    
    def test_empty_initial_state(self):
        """Initial state has no matches."""
        state = BotState()
        assert len(state._matches) == 0
        assert len(state._token_to_match) == 0
        assert len(state._order_to_match) == 0


class TestRegisterMatch:
    """Tests for register_match method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create a fresh BotState."""
        return BotState()
    
    def test_register_new_match(self, bot_state):
        """Creates new match if not exists."""
        match = bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            game="cs2",
            team1="liquid",
            team2="navi",
        )
        
        assert match is not None
        assert match.match_id == "cs2:liquid:vs:navi"
        assert "cs2:liquid:vs:navi" in bot_state._matches
    
    def test_register_match_with_tokens(self, bot_state):
        """Tokens are registered in lookup."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
        )
        
        assert bot_state._token_to_match["token_liquid"] == "cs2:liquid:vs:navi"
        assert bot_state._token_to_match["token_navi"] == "cs2:liquid:vs:navi"
    
    def test_register_match_with_condition(self, bot_state):
        """Condition ID is registered in lookup."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            condition_id="0xabc123",
        )
        
        assert bot_state._condition_to_match["0xabc123"] == "cs2:liquid:vs:navi"
    
    def test_register_existing_match_updates(self, bot_state):
        """Registering existing match updates with new info."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            game="cs2",
        )
        
        # Second registration adds tokens
        match = bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
        )
        
        assert match.token1 == "token_liquid"
        assert match.token2 == "token_navi"
    
    def test_register_existing_match_no_overwrite(self, bot_state):
        """Existing info is not overwritten with empty."""
        bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
        )
        
        # Second registration with empty token1 should not overwrite
        match = bot_state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="",
        )
        
        assert match.token1 == "token_liquid"


class TestGetMatchQueries:
    """Tests for get_match_* query methods."""
    
    @pytest.fixture
    def bot_state(self):
        """Create BotState with a registered match."""
        state = BotState()
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            condition_id="cond123",
            token1="token_liquid",
            token2="token_navi",
        )
        return state
    
    def test_get_match_by_id(self, bot_state):
        """get_match returns match by ID."""
        match = bot_state.get_match("cs2:liquid:vs:navi")
        assert match is not None
        assert match.match_id == "cs2:liquid:vs:navi"
    
    def test_get_match_not_found(self, bot_state):
        """get_match returns None for unknown ID."""
        assert bot_state.get_match("unknown") is None
    
    def test_get_match_by_token(self, bot_state):
        """get_match_by_token returns correct match."""
        match = bot_state.get_match_by_token("token_liquid")
        assert match is not None
        assert match.match_id == "cs2:liquid:vs:navi"
    
    def test_get_match_by_order(self, bot_state):
        """get_match_by_order returns correct match."""
        # Register an order first
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        match = bot_state.get_match_by_order("order123")
        assert match is not None
        assert match.match_id == "cs2:liquid:vs:navi"
    
    def test_get_match_by_condition(self, bot_state):
        """get_match_by_condition returns correct match."""
        match = bot_state.get_match_by_condition("cond123")
        assert match is not None
        assert match.match_id == "cs2:liquid:vs:navi"


class TestHasExposureForMatch:
    """Tests for has_exposure_for_match method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create BotState with a match."""
        state = BotState()
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        return state
    
    def test_no_exposure_empty(self, bot_state):
        """No exposure when no orders or positions."""
        assert bot_state.has_exposure_for_match("cs2:liquid:vs:navi") is False
    
    def test_exposure_with_order(self, bot_state):
        """Exposure when order exists."""
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        assert bot_state.has_exposure_for_match("cs2:liquid:vs:navi") is True
    
    def test_no_exposure_unknown_match(self, bot_state):
        """No exposure for unknown match."""
        assert bot_state.has_exposure_for_match("unknown_match") is False


class TestHasUnhedgedPositionForMatch:
    """Tests for has_unhedged_position_for_match method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create BotState with a match."""
        state = BotState()
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        return state
    
    def test_no_unhedged_without_position(self, bot_state):
        """No unhedged when only orders (no filled positions)."""
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        assert bot_state.has_unhedged_position_for_match("cs2:liquid:vs:navi") is False
    
    def test_unhedged_with_single_position(self, bot_state):
        """Unhedged when position on one side only."""
        match = bot_state.get_match("cs2:liquid:vs:navi")
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="Team Liquid",
            shares=10.0,
            avg_price=0.45,
        )
        
        assert bot_state.has_unhedged_position_for_match("cs2:liquid:vs:navi") is True
    
    def test_not_unhedged_with_both_positions(self, bot_state):
        """Not unhedged when positions on both sides."""
        match = bot_state.get_match("cs2:liquid:vs:navi")
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="Team Liquid",
            shares=10.0,
            avg_price=0.45,
        )
        match.position2 = MatchPosition(
            token_id="token_navi",
            team="Natus Vincere",
            shares=10.0,
            avg_price=0.45,
        )
        
        assert bot_state.has_unhedged_position_for_match("cs2:liquid:vs:navi") is False


class TestHasOppositeCoverage:
    """Tests for has_opposite_coverage method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create BotState with a match."""
        state = BotState()
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        return state
    
    def test_no_opposite_coverage_empty(self, bot_state):
        """No opposite coverage when no orders."""
        assert bot_state.has_opposite_coverage("token_liquid") is False
    
    def test_no_opposite_coverage_same_side(self, bot_state):
        """No opposite coverage when order only on same side."""
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        # Checking token_liquid should show no opposite coverage (order is on same side)
        assert bot_state.has_opposite_coverage("token_liquid") is False
    
    def test_opposite_coverage_with_order(self, bot_state):
        """Opposite coverage when order on opposite side."""
        # Order on side 1
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="Team Liquid",
            price=0.50,
            size=10.0,
        )
        
        # Checking token_navi should show opposite coverage (order is on other side)
        assert bot_state.has_opposite_coverage("token_navi") is True


class TestReserveToken:
    """Tests for reserve_token method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create fresh BotState."""
        return BotState()
    
    def test_reserve_new_token(self, bot_state):
        """Can reserve a new token."""
        result = bot_state.reserve_token("new_token")
        assert result is True
        assert "new_token" in bot_state._reserved_tokens
    
    def test_cannot_reserve_twice(self, bot_state):
        """Cannot reserve an already reserved token."""
        bot_state.reserve_token("token")
        result = bot_state.reserve_token("token")
        assert result is False
    
    def test_unreserve_token(self, bot_state):
        """Can unreserve a token."""
        bot_state.reserve_token("token")
        bot_state.unreserve_token("token")
        assert "token" not in bot_state._reserved_tokens
    
    def test_cannot_reserve_active_token(self, bot_state):
        """Cannot reserve a token with active order."""
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
        
        result = bot_state.reserve_token("token_liquid")
        assert result is False


class TestIsTokenActive:
    """Tests for is_token_active method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create fresh BotState."""
        return BotState()
    
    def test_token_not_active_initially(self, bot_state):
        """Unknown token is not active."""
        assert bot_state.is_token_active("unknown") is False
    
    def test_reserved_token_is_active(self, bot_state):
        """Reserved token is active."""
        bot_state._reserved_tokens.add("token")
        assert bot_state.is_token_active("token") is True
    
    def test_collision_token_is_active(self, bot_state):
        """Collision token is active."""
        bot_state._collision_tokens.add("token")
        assert bot_state.is_token_active("token") is True
    
    def test_token_with_order_is_active(self, bot_state):
        """Token with open order is active."""
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
    
    def test_completed_arb_token_not_active(self, bot_state):
        """Token from completed arb (equal positions) is NOT active - allows new orders."""
        bot_state.register_match(
            match_id="lol:movistarkoi:vs:shifters",
            token1="token_movistar",
            token2="token_shifters",
        )
        # Create equal positions on both sides (completed arb)
        match = bot_state._matches["lol:movistarkoi:vs:shifters"]
        match.position1 = MatchPosition(
            token_id="token_movistar",
            team="Movistar KOI",
            shares=10.0,
            avg_price=0.35,
        )
        match.position2 = MatchPosition(
            token_id="token_shifters",
            team="Shifters",
            shares=10.0,  # Equal to position1 = completed arb
            avg_price=0.58,
        )
        
        # Completed arb tokens should NOT be active (allow new orders)
        assert bot_state.is_token_active("token_movistar") is False
        assert bot_state.is_token_active("token_shifters") is False
    
    def test_nearly_equal_positions_not_active(self, bot_state):
        """Token from nearly-equal positions (<5 share diff) is NOT active - allows new orders.
        
        Reproduces: 30 YTIGERES vs 32 3DMAXA = 2 share difference = treated as complete.
        """
        bot_state.register_match(
            match_id="cs2:ytigeres:vs:3dmax",
            token1="token_ytigeres",
            token2="token_3dmax",
        )
        match = bot_state._matches["cs2:ytigeres:vs:3dmax"]
        match.position1 = MatchPosition(
            token_id="token_ytigeres",
            team="Young TigeRES",
            shares=30.0,
            avg_price=0.60,
        )
        match.position2 = MatchPosition(
            token_id="token_3dmax",
            team="3DMAX Academy",
            shares=32.0,  # Difference of 2 shares (< 5.0) = nearly equal = completed
            avg_price=0.29,
        )
        
        # Nearly-equal positions should NOT be active (allow new orders)
        assert bot_state.is_token_active("token_ytigeres") is False
        assert bot_state.is_token_active("token_3dmax") is False
    
    def test_unequal_position_token_is_active(self, bot_state):
        """Token from unequal position (partial arb) IS active - blocks new orders."""
        bot_state.register_match(
            match_id="lol:movistarkoi:vs:shifters",
            token1="token_movistar",
            token2="token_shifters",
        )
        # Create UNEQUAL positions (partial arb, not complete)
        match = bot_state._matches["lol:movistarkoi:vs:shifters"]
        match.position1 = MatchPosition(
            token_id="token_movistar",
            team="Movistar KOI",
            shares=10.0,
            avg_price=0.35,
        )
        match.position2 = MatchPosition(
            token_id="token_shifters",
            team="Shifters",
            shares=3.0,  # Difference of 7 shares (> 5.0) = NOT completed
            avg_price=0.58,
        )
        
        # Unequal positions SHOULD be active (block new orders until hedge complete)
        assert bot_state.is_token_active("token_movistar") is True
        assert bot_state.is_token_active("token_shifters") is True
    
    def test_single_position_token_is_active(self, bot_state):
        """Token with position on one side only IS active - needs hedge."""
        bot_state.register_match(
            match_id="lol:movistarkoi:vs:shifters",
            token1="token_movistar",
            token2="token_shifters",
        )
        # Position only on side1
        match = bot_state._matches["lol:movistarkoi:vs:shifters"]
        match.position1 = MatchPosition(
            token_id="token_movistar",
            team="Movistar KOI",
            shares=10.0,
            avg_price=0.35,
        )
        # No position on side2
        
        # Single position tokens SHOULD be active (waiting for hedge)
        assert bot_state.is_token_active("token_movistar") is True
        # Side2 has no coverage, so not active
        assert bot_state.is_token_active("token_shifters") is False


class TestGetActiveTokenIds:
    """Tests for get_active_token_ids method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create BotState with various tokens."""
        state = BotState()
        state._reserved_tokens.add("reserved_token")
        state._collision_tokens.add("collision_token")
        
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
    
    def test_includes_reserved(self, bot_state):
        """Active tokens includes reserved."""
        tokens = bot_state.get_active_token_ids()
        assert "reserved_token" in tokens
    
    def test_includes_collision(self, bot_state):
        """Active tokens includes collision."""
        tokens = bot_state.get_active_token_ids()
        assert "collision_token" in tokens
    
    def test_includes_order_token(self, bot_state):
        """Active tokens includes order tokens."""
        tokens = bot_state.get_active_token_ids()
        assert "token_liquid" in tokens


class TestGetTokensBlockingNewOrders:
    """Tests for get_tokens_blocking_new_orders method.
    
    This is the critical fix allowing new orders on completed arbs.
    """
    
    @pytest.fixture
    def bot_state(self):
        """Create BotState with a match."""
        state = BotState()
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        return state
    
    def test_includes_open_orders(self, bot_state):
        """Open orders should block new orders on same token."""
        bot_state.register_order(
            match_id="cs2:liquid:vs:navi",
            order_id="order123",
            token_id="token_liquid",
            team="liquid",
            price=0.50,
            size=10.0,
        )
        
        blocking = bot_state.get_tokens_blocking_new_orders()
        assert "token_liquid" in blocking
    
    def test_includes_unhedged_position(self, bot_state):
        """Unhedged positions should block new orders."""
        match = bot_state.get_match("cs2:liquid:vs:navi")
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="liquid",
            shares=10.0,
            avg_price=0.50,
        )
        # No position on side2 = unhedged
        
        blocking = bot_state.get_tokens_blocking_new_orders()
        assert "token_liquid" in blocking
    
    def test_excludes_completed_arb_tokens(self, bot_state):
        """CRITICAL: Completed arbs should NOT block new orders.
        
        This allows placing new orders with edge on completed arbs.
        """
        match = bot_state.get_match("cs2:liquid:vs:navi")
        # Equal positions = completed arb
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="liquid",
            shares=10.0,
            avg_price=0.50,
        )
        match.position2 = MatchPosition(
            token_id="token_navi",
            team="navi",
            shares=10.0,
            avg_price=0.40,
        )
        
        blocking = bot_state.get_tokens_blocking_new_orders()
        
        # NEITHER token should be blocking!
        assert "token_liquid" not in blocking
        assert "token_navi" not in blocking
    
    def test_active_still_includes_completed_arb(self, bot_state):
        """get_active_token_ids should STILL include completed arb tokens.
        
        We need them for WebSocket subscriptions and display.
        """
        match = bot_state.get_match("cs2:liquid:vs:navi")
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="liquid",
            shares=10.0,
            avg_price=0.50,
        )
        match.position2 = MatchPosition(
            token_id="token_navi",
            team="navi",
            shares=10.0,
            avg_price=0.40,
        )
        
        active = bot_state.get_active_token_ids()
        
        # Active should include both
        assert "token_liquid" in active
        assert "token_navi" in active
    
    def test_unequal_positions_block_larger_side(self, bot_state):
        """Unequal positions: smaller side is hedged, larger is unhedged."""
        match = bot_state.get_match("cs2:liquid:vs:navi")
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="liquid",
            shares=20.0,  # Larger position
            avg_price=0.50,
        )
        match.position2 = MatchPosition(
            token_id="token_navi",
            team="navi",
            shares=3.0,  # Difference of 17 shares (> 5.0) = NOT completed
            avg_price=0.40,
        )
        
        blocking = bot_state.get_tokens_blocking_new_orders()
        
        # Both should be blocking when unequal (we need more hedge)
        assert "token_liquid" in blocking
        assert "token_navi" in blocking


class TestReactiveCallbacks:
    """Tests for reactive callback system."""
    
    @pytest.fixture
    def bot_state(self):
        """Create fresh BotState."""
        return BotState()
    
    def test_on_registers_callback(self, bot_state):
        """on() registers a callback."""
        callback = Mock()
        bot_state.on(StateEventType.ORDER_REGISTERED, callback)
        
        assert callback in bot_state._callbacks[StateEventType.ORDER_REGISTERED]
    
    def test_callback_called_on_order_register(self, bot_state):
        """Callback is called when order is registered."""
        callback = Mock()
        bot_state.on(StateEventType.ORDER_REGISTERED, callback)
        
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
        
        assert callback.called


class TestGetPositionsNeedingHedge:
    """Tests for get_positions_needing_hedge method."""
    
    @pytest.fixture
    def bot_state(self):
        """Create BotState with a match."""
        state = BotState()
        state.register_match(
            match_id="cs2:liquid:vs:navi",
            condition_id="cond123",
            token1="token_liquid",
            token2="token_navi",
            team1="liquid",
            team2="navi",
        )
        return state
    
    def test_empty_when_no_positions(self, bot_state):
        """Returns empty when no positions."""
        result = bot_state.get_positions_needing_hedge()
        assert len(result) == 0
    
    def test_empty_when_fully_hedged(self, bot_state):
        """Returns empty when positions on both sides."""
        match = bot_state.get_match("cs2:liquid:vs:navi")
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="liquid",
            shares=10.0,
            avg_price=0.45,
        )
        match.position2 = MatchPosition(
            token_id="token_navi",
            team="navi",
            shares=10.0,
            avg_price=0.45,
        )
        
        result = bot_state.get_positions_needing_hedge()
        assert len(result) == 0
    
    def test_returns_position_needing_hedge(self, bot_state):
        """Returns position that needs hedge."""
        match = bot_state.get_match("cs2:liquid:vs:navi")
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="liquid",
            shares=10.0,
            avg_price=0.45,
        )
        
        result = bot_state.get_positions_needing_hedge()
        assert len(result) == 1
        key = list(result.keys())[0]
        assert result[key]["entry_team"] == "liquid"
        assert result[key]["shares"] == 10.0
    
    def test_unequal_positions_need_hedge(self, bot_state):
        """When both sides have positions but unequal sizes, larger side needs more hedge.
        
        This is the critical bug fix: VIT 20 shares @ 0.61 vs SK 5 shares @ 0.27
        should show 15 shares still needing hedge, NOT be skipped as "complete".
        """
        match = bot_state.get_match("cs2:liquid:vs:navi")
        # Side 1: 20 shares (Team Vitality equivalent)
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="liquid",
            shares=20.0,
            avg_price=0.61,
        )
        # Side 2: 5 shares (SK Gaming equivalent) - partial hedge
        match.position2 = MatchPosition(
            token_id="token_navi",
            team="navi",
            shares=5.0,
            avg_price=0.27,
        )
        
        result = bot_state.get_positions_needing_hedge()
        
        # Should NOT be empty! We need 15 more shares on side2
        assert len(result) == 1
        key = list(result.keys())[0]
        info = result[key]
        
        # The LARGER position (side1=liquid) is the entry, needs hedge on side2
        assert info["entry_team"] == "liquid"
        assert info["hedge_team"] == "navi"
        assert info["shares"] == 15.0  # 20 - 5 = 15 unhedged
        assert info["total_entry_shares"] == 20.0
        assert info["hedge_token_id"] == "token_navi"
    
    def test_unequal_positions_other_side_larger(self, bot_state):
        """When side2 is larger, it needs hedge on side1."""
        match = bot_state.get_match("cs2:liquid:vs:navi")
        # Side 1: 5 shares
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="liquid",
            shares=5.0,
            avg_price=0.45,
        )
        # Side 2: 25 shares - larger position
        match.position2 = MatchPosition(
            token_id="token_navi",
            team="navi",
            shares=25.0,
            avg_price=0.45,
        )
        
        result = bot_state.get_positions_needing_hedge()
        
        assert len(result) == 1
        key = list(result.keys())[0]
        info = result[key]
        
        # The LARGER position (side2=navi) is the entry, needs hedge on side1
        assert info["entry_team"] == "navi"
        assert info["hedge_team"] == "liquid"
        assert info["shares"] == 20.0  # 25 - 5 = 20 unhedged
        assert info["hedge_token_id"] == "token_liquid"
    
    def test_unequal_positions_with_partial_order_coverage(self, bot_state):
        """Existing open order reduces unhedged amount."""
        match = bot_state.get_match("cs2:liquid:vs:navi")
        # Side 1: 20 shares
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="liquid",
            shares=20.0,
            avg_price=0.61,
        )
        # Side 2: 5 shares position
        match.position2 = MatchPosition(
            token_id="token_navi",
            team="navi",
            shares=5.0,
            avg_price=0.27,
        )
        # Plus an open order for 12 more shares on side2
        match.order2 = MatchOrder(
            order_id="order_hedge",
            token_id="token_navi",
            team="navi",
            price=0.30,
            size=12.0,
            filled=0.0,
            is_open=True,
        )
        
        result = bot_state.get_positions_needing_hedge()
        
        # Unhedged = 20 - 5 - 12 = 3 (below minimum threshold of 5)
        # So should return empty
        assert len(result) == 0
    
    def test_equal_positions_fully_hedged(self, bot_state):
        """Equal positions means arb is complete - no hedge needed."""
        match = bot_state.get_match("cs2:liquid:vs:navi")
        match.position1 = MatchPosition(
            token_id="token_liquid",
            team="liquid",
            shares=15.0,
            avg_price=0.45,
        )
        match.position2 = MatchPosition(
            token_id="token_navi",
            team="navi",
            shares=15.0,
            avg_price=0.45,
        )
        
        result = bot_state.get_positions_needing_hedge()
        assert len(result) == 0  # Equal = complete, no hedge needed


class TestHydratedTeamAlphabeticalSort:
    """Tests for fix: hydrated positions/orders must sort teams alphabetically.
    
    This ensures fair_prob1 from OddsService aligns with match.team1 in BotState.
    Bug: Washington was getting Phantom's 70% fair value instead of its own 30%.
    
    Root cause: match_id uses alphabetical order (phantom:vs:washington), 
    but team1/team2 were stored in Polymarket's order (Washington, Phantom).
    When OddsService pushed fair_prob1=70% (Phantom), it got assigned to
    match.team1 (Washington) because names didn't match.
    """
    
    @pytest.fixture
    def bot_state(self):
        """Create fresh BotState."""
        return BotState()
    
    def test_hydrated_position_sorts_teams_alphabetically(self, bot_state):
        """register_hydrated_position should sort teams to match match_id order.
        
        Polymarket question: "Washington vs Phantom" (Washington first)
        Match ID should be: "cs2:phantom:vs:washington" (sorted)
        After hydration: team1=Phantom, team2=Washington (sorted)
        """
        # Simulate hydrating a position for Washington
        # Question has Washington first, but alphabetically Phantom < Washington
        match = bot_state.register_hydrated_position(
            token_id="token_washington",
            team="Washington",
            shares=30.0,
            avg_price=0.47,
            condition_id="cond123",
            match_question="Counter-Strike: Washington vs Phantom (BO3)",
            opponent_team="Phantom",
            opponent_token="token_phantom",
        )
        
        # Match ID should be alphabetically sorted
        assert match.match_id == "cs2:phantom:vs:washington"
        
        # Team1 should be the alphabetically first team (Phantom)
        assert "phantom" in match.team1.lower()
        
        # Team2 should be Washington (alphabetically second)
        assert "washington" in match.team2.lower()
        
        # Washington's token should be token2 (since Washington is team2)
        assert match.token2 == "token_washington"
        
        # Phantom's token should be token1
        assert match.token1 == "token_phantom"

    
    def test_hydrated_order_sorts_teams_alphabetically(self, bot_state):
        """register_hydrated_order should sort teams to match match_id order."""
        # Simulate hydrating an order for Washington
        match = bot_state.register_hydrated_order(
            order_id="order123",
            token_id="token_washington",
            team="Washington",
            price=0.47,
            size=30.0,
            match_question="Counter-Strike: Washington vs Phantom (BO3)",
            condition_id="cond456",
        )
        
        # Match ID should be alphabetically sorted
        assert match.match_id == "cs2:phantom:vs:washington"
        
        # Team1 should be the alphabetically first team (Phantom)
        assert "phantom" in match.team1.lower()
        
        # Team2 should be Washington (alphabetically second)
        assert "washington" in match.team2.lower()
    
    def test_fair_value_alignment_after_hydration(self, bot_state):
        """Verify fair values are correctly aligned after hydration.
        
        This is the critical test for the bug fix:
        1. Hydrate Washington position
        2. Push fair probs from OddsService (Phantom=70%, Washington=30%)
        3. Verify Washington's token gets 30%, not 70%
        """
        # Step 1: Hydrate Washington position
        match = bot_state.register_hydrated_position(
            token_id="token_washington",
            team="Washington",
            shares=30.0,
            avg_price=0.47,
            match_question="Counter-Strike: Washington vs Phantom (BO3)",
            opponent_team="Phantom",
            opponent_token="token_phantom",
        )
        
        # Step 2: Simulate OddsService pushing fair probs
        # OddsService also sorts alphabetically: team1=Phantom (70%), team2=Washington (30%)
        bot_state.update_fair_probs_by_team(
            match_id=match.match_id,
            team1="Phantom",  # OddsService sends sorted
            team2="Washington",
            fair_prob1=0.70,  # Phantom's fair value
            fair_prob2=0.30,  # Washington's fair value
        )
        
        # Step 3: Verify fair values are correctly assigned
        # Washington's token should get Washington's fair value (30%)
        washington_fair = match.get_fair_for_token("token_washington")
        phantom_fair = match.get_fair_for_token("token_phantom")
        
        # THIS IS THE BUG FIX: Washington should get 30%, not 70%!
        assert washington_fair == 0.30, f"Washington should have 30% fair value, got {washington_fair}"
        assert phantom_fair == 0.70, f"Phantom should have 70% fair value, got {phantom_fair}"

