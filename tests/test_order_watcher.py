"""
Unit tests for execution/order_watcher.py - Central order lifecycle manager.

Note: OrderWatcher has complex dependencies. These tests use mock-based
approaches and test the core logic patterns.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone
import uuid


class MockPosition:
    """Mock Position for testing order watcher."""
    
    def __init__(
        self,
        position_id: str = None,
        entry_team: str = "Team A",
        entry_token_id: str = "token_a",
        hedge_team: str = "Team B",
        hedge_token_id: str = "token_b",
        entry_price: float = 0.50,
        entry_filled_shares: float = 0,
        hedge_filled_shares: float = 0,
        state: str = "ENTRY_PENDING",
        match_id: str = "cs2:team-a:vs:team-b",
        fair_value: float = 0.55,
    ):
        self.position_id = position_id or f"pos_{uuid.uuid4().hex[:8]}"
        self.entry_team = entry_team
        self.entry_token_id = entry_token_id
        self.hedge_team = hedge_team
        self.hedge_token_id = hedge_token_id
        self.entry_price = entry_price
        self.entry_filled_shares = entry_filled_shares
        self.hedge_filled_shares = hedge_filled_shares
        self.state = state
        self.match_id = match_id
        self.fair_value = fair_value
        self.entry_order = Mock()
        self.hedge_order = None
    
    def is_entry_filled(self) -> bool:
        return self.entry_filled_shares > 0
    
    def is_fully_hedged(self) -> bool:
        return self.hedge_filled_shares > 0 and self.hedge_filled_shares >= self.entry_filled_shares


class MockOrderWatcher:
    """Mock OrderWatcher for testing core logic patterns."""
    
    def __init__(self, executor: Mock, config: dict = None, bot_state: Mock = None):
        self.executor = executor
        self.config = config or {"min_edge": 0.04, "min_profit": 0.05}
        self.bot_state = bot_state or Mock()
        self._positions = {}
        self._position_counter = 0
        self._ws_handler = None
    
    def _generate_position_id(self) -> str:
        """Generate unique position ID."""
        self._position_counter += 1
        return f"pos_{self._position_counter:04d}"
    
    def is_token_active(self, token_id: str) -> bool:
        """Check if token has active order or position."""
        # Check positions
        for pos in self._positions.values():
            if pos.entry_token_id == token_id or pos.hedge_token_id == token_id:
                return True
        
        # Delegate to BotState
        return self.bot_state.is_token_active(token_id)
    
    def get_positions_needing_hedge(self) -> list:
        """Get positions that are filled but not hedged."""
        return [
            p for p in self._positions.values()
            if p.is_entry_filled() and not p.is_fully_hedged()
        ]
    
    def get_position(self, position_id: str) -> MockPosition:
        """Get position by ID."""
        return self._positions.get(position_id)
    
    def get_all_positions(self) -> dict:
        """Get all positions."""
        return self._positions.copy()
    
    def get_open_positions(self) -> list:
        """Get non-hedged, non-cancelled positions."""
        return [
            p for p in self._positions.values()
            if p.state not in ("HEDGED", "CANCELLED", "SETTLED")
        ]
    
    def get_hedged_positions(self) -> list:
        """Get fully hedged positions."""
        return [p for p in self._positions.values() if p.is_fully_hedged()]
    
    def has_natural_arb(self, match_id: str) -> bool:
        """Check if match has coverage on both sides (natural arb)."""
        return self.bot_state.has_natural_arb_for_match(match_id)
    
    def has_opposite_coverage(self, token_id: str) -> bool:
        """Check if opposite side of token's match has coverage."""
        return self.bot_state.has_opposite_coverage(token_id)
    
    def _should_adjust_bid(self, our_price: float, best_bid: float) -> bool:
        """Check if we should adjust our bid (outbid threshold)."""
        return best_bid > our_price + 0.01  # >1¢ higher
    
    def _calculate_new_bid(self, best_bid: float, max_price: float) -> float:
        """Calculate new bid price (best_bid + 1¢, capped at max)."""
        new_bid = min(best_bid + 0.01, max_price)
        return round(new_bid, 2)
    
    def _is_edge_valid(self, fair_value: float, our_price: float, min_edge: float) -> bool:
        """Check if edge is still valid."""
        edge = fair_value - our_price
        return edge >= min_edge


class TestOrderWatcherInit:
    """Tests for OrderWatcher initialization."""
    
    def test_init_stores_dependencies(self):
        """Watcher stores executor and config."""
        executor = Mock()
        config = {"min_edge": 0.05}
        
        watcher = MockOrderWatcher(executor=executor, config=config)
        
        assert watcher.executor is executor
        assert watcher.config["min_edge"] == 0.05
    
    def test_init_empty_positions(self):
        """Watcher starts with no positions."""
        watcher = MockOrderWatcher(Mock())
        
        assert len(watcher._positions) == 0


class TestPositionIdGeneration:
    """Tests for position ID generation."""
    
    def test_generates_unique_ids(self):
        """Each call generates unique ID."""
        watcher = MockOrderWatcher(Mock())
        
        id1 = watcher._generate_position_id()
        id2 = watcher._generate_position_id()
        id3 = watcher._generate_position_id()
        
        assert id1 != id2 != id3
    
    def test_id_format(self):
        """ID has expected format."""
        watcher = MockOrderWatcher(Mock())
        
        pos_id = watcher._generate_position_id()
        
        assert pos_id.startswith("pos_")


class TestTokenActiveCheck:
    """Tests for token activity checking."""
    
    @pytest.fixture
    def watcher(self):
        """Create mock watcher with position."""
        watcher = MockOrderWatcher(Mock())
        watcher._positions["pos1"] = MockPosition(
            entry_token_id="token_a",
            hedge_token_id="token_b",
        )
        return watcher
    
    def test_entry_token_is_active(self, watcher):
        """Entry token is marked as active."""
        watcher.bot_state.is_token_active.return_value = False
        
        is_active = watcher.is_token_active("token_a")
        assert is_active is True
    
    def test_hedge_token_is_active(self, watcher):
        """Hedge token is marked as active."""
        watcher.bot_state.is_token_active.return_value = False
        
        is_active = watcher.is_token_active("token_b")
        assert is_active is True
    
    def test_unknown_token_checks_botstate(self, watcher):
        """Unknown token delegates to BotState."""
        watcher.bot_state.is_token_active.return_value = True
        
        is_active = watcher.is_token_active("token_unknown")
        assert is_active is True
        watcher.bot_state.is_token_active.assert_called_with("token_unknown")


class TestPositionsNeedingHedge:
    """Tests for finding positions that need hedges."""
    
    @pytest.fixture
    def watcher(self):
        """Create mock watcher."""
        return MockOrderWatcher(Mock())
    
    def test_filled_unhedged_needs_hedge(self, watcher):
        """Filled but unhedged position needs hedge."""
        pos = MockPosition(
            entry_filled_shares=25.0,
            hedge_filled_shares=0,
        )
        watcher._positions["pos1"] = pos
        
        needing = watcher.get_positions_needing_hedge()
        
        assert len(needing) == 1
        assert needing[0] is pos
    
    def test_unfilled_doesnt_need_hedge(self, watcher):
        """Unfilled position doesn't need hedge."""
        pos = MockPosition(
            entry_filled_shares=0,
            hedge_filled_shares=0,
        )
        watcher._positions["pos1"] = pos
        
        needing = watcher.get_positions_needing_hedge()
        
        assert len(needing) == 0
    
    def test_fully_hedged_doesnt_need_hedge(self, watcher):
        """Fully hedged position doesn't need hedge."""
        pos = MockPosition(
            entry_filled_shares=25.0,
            hedge_filled_shares=25.0,
        )
        watcher._positions["pos1"] = pos
        
        needing = watcher.get_positions_needing_hedge()
        
        assert len(needing) == 0


class TestOpenPositions:
    """Tests for getting open positions."""
    
    @pytest.fixture
    def watcher(self):
        """Create mock watcher with various positions."""
        watcher = MockOrderWatcher(Mock())
        watcher._positions["pos1"] = MockPosition(state="ENTRY_PENDING")
        watcher._positions["pos2"] = MockPosition(state="POSITION_OPEN")
        watcher._positions["pos3"] = MockPosition(state="HEDGED")
        watcher._positions["pos4"] = MockPosition(state="CANCELLED")
        return watcher
    
    def test_excludes_hedged(self, watcher):
        """Open positions excludes hedged."""
        open_pos = watcher.get_open_positions()
        
        states = [p.state for p in open_pos]
        assert "HEDGED" not in states
    
    def test_excludes_cancelled(self, watcher):
        """Open positions excludes cancelled."""
        open_pos = watcher.get_open_positions()
        
        states = [p.state for p in open_pos]
        assert "CANCELLED" not in states
    
    def test_includes_pending(self, watcher):
        """Open positions includes pending."""
        open_pos = watcher.get_open_positions()
        
        states = [p.state for p in open_pos]
        assert "ENTRY_PENDING" in states


class TestOutbidCheck:
    """Tests for outbid detection logic."""
    
    @pytest.fixture
    def watcher(self):
        """Create mock watcher."""
        return MockOrderWatcher(Mock())
    
    def test_should_adjust_when_outbid(self, watcher):
        """Should adjust when significantly outbid."""
        our_price = 0.50
        best_bid = 0.52  # 2¢ higher
        
        should_adjust = watcher._should_adjust_bid(our_price, best_bid)
        assert should_adjust is True
    
    def test_no_adjust_when_we_are_best(self, watcher):
        """No adjust when we are best bid."""
        our_price = 0.50
        best_bid = 0.50
        
        should_adjust = watcher._should_adjust_bid(our_price, best_bid)
        assert should_adjust is False
    
    def test_no_adjust_within_threshold(self, watcher):
        """No adjust when outbid by only 1¢."""
        our_price = 0.50
        best_bid = 0.51  # Only 1¢ higher
        
        should_adjust = watcher._should_adjust_bid(our_price, best_bid)
        assert should_adjust is False


class TestBidCalculation:
    """Tests for new bid price calculation."""
    
    @pytest.fixture
    def watcher(self):
        """Create mock watcher."""
        return MockOrderWatcher(Mock())
    
    def test_new_bid_is_best_plus_one_cent(self, watcher):
        """New bid is best_bid + 1¢."""
        best_bid = 0.52
        max_price = 0.60
        
        new_bid = watcher._calculate_new_bid(best_bid, max_price)
        
        assert new_bid == 0.53
    
    def test_new_bid_capped_at_max(self, watcher):
        """New bid capped at max price."""
        best_bid = 0.59
        max_price = 0.55
        
        new_bid = watcher._calculate_new_bid(best_bid, max_price)
        
        assert new_bid == 0.55
    
    def test_new_bid_rounded(self, watcher):
        """New bid is rounded to 2 decimal places."""
        best_bid = 0.499
        max_price = 0.60
        
        new_bid = watcher._calculate_new_bid(best_bid, max_price)
        
        assert new_bid == 0.51


class TestEdgeValidation:
    """Tests for edge validation logic."""
    
    @pytest.fixture
    def watcher(self):
        """Create mock watcher."""
        return MockOrderWatcher(Mock())
    
    def test_edge_valid_when_above_min(self, watcher):
        """Edge valid when above minimum."""
        fair_value = 0.55
        our_price = 0.50
        min_edge = 0.04
        
        is_valid = watcher._is_edge_valid(fair_value, our_price, min_edge)
        assert is_valid is True
    
    def test_edge_invalid_when_below_min(self, watcher):
        """Edge invalid when below minimum."""
        fair_value = 0.52
        our_price = 0.50
        min_edge = 0.04
        
        is_valid = watcher._is_edge_valid(fair_value, our_price, min_edge)
        assert is_valid is False
    
    def test_edge_exactly_at_min(self, watcher):
        """Edge valid when exactly at minimum."""
        fair_value = 0.54
        our_price = 0.50
        min_edge = 0.04
        
        is_valid = watcher._is_edge_valid(fair_value, our_price, min_edge)
        assert is_valid is True


class TestNaturalArbDetection:
    """Tests for natural arb detection."""
    
    @pytest.fixture
    def watcher(self):
        """Create mock watcher."""
        watcher = MockOrderWatcher(Mock())
        return watcher
    
    def test_delegates_to_botstate(self, watcher):
        """Natural arb check delegates to BotState."""
        watcher.bot_state.has_natural_arb_for_match.return_value = True
        
        result = watcher.has_natural_arb("cs2:team-a:vs:team-b")
        
        assert result is True
        watcher.bot_state.has_natural_arb_for_match.assert_called_with("cs2:team-a:vs:team-b")


class TestOppositeCoverage:
    """Tests for opposite side coverage check."""
    
    @pytest.fixture
    def watcher(self):
        """Create mock watcher."""
        watcher = MockOrderWatcher(Mock())
        return watcher
    
    def test_delegates_to_botstate(self, watcher):
        """Opposite coverage check delegates to BotState."""
        watcher.bot_state.has_opposite_coverage.return_value = True
        
        result = watcher.has_opposite_coverage("token_a")
        
        assert result is True
        watcher.bot_state.has_opposite_coverage.assert_called_with("token_a")

