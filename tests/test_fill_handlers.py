"""
Unit tests for handlers/fill_handlers.py - Order fill event handling.

Note: FillHandler has complex async dependencies. These tests use mock-based
approaches and test the core logic patterns.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch


class MockPosition:
    """Mock Position for testing fill handlers."""
    
    def __init__(
        self,
        position_id: str = "pos123",
        entry_team: str = "Team A",
        entry_price: float = 0.50,
        entry_size: float = 25.0,
        entry_token_id: str = "token_a",
        hedge_team: str = "Team B",
        hedge_token_id: str = "token_b",
        is_hydrated: bool = False,
    ):
        self.position_id = position_id
        self.entry_team = entry_team
        self.entry_price = entry_price
        self.entry_size = entry_size
        self.entry_token_id = entry_token_id
        self.hedge_team = hedge_team
        self.hedge_token_id = hedge_token_id
        self.is_hydrated = is_hydrated
        self.entry_order = Mock()
        self.entry_order.fair_value = 0.55
        self.entry_filled_shares = 0
        self.hedge_filled_shares = 0
        self.condition_id = "cond123"
        self.match_id = "match123"


class MockFillHandler:
    """Mock FillHandler for testing core logic patterns."""
    
    def __init__(self, odds_service: Mock, alerts: Mock, hedge_finder: Mock):
        self.odds_service = odds_service
        self.alerts = alerts
        self.hedge_finder = hedge_finder
        self.bot_state = Mock()
        self._logged_arbs = set()
    
    def _should_place_hedge(self, position: MockPosition) -> tuple:
        """Determine if hedge should be placed or if natural arb exists."""
        # Check for natural arb via BotState
        match = self.bot_state.get_match_by_token(position.entry_token_id)
        
        if not match:
            return (True, None)  # No match found, place hedge normally
        
        # Check if opposite side has coverage (natural arb)
        if match.token1 == position.entry_token_id:
            has_coverage = match.order2 or match.position2
            opposite_team = match.team2
        else:
            has_coverage = match.order1 or match.position1
            opposite_team = match.team1
        
        if has_coverage:
            return (False, opposite_team)  # Natural arb, skip hedge
        
        return (True, None)  # No natural arb, place hedge
    
    def _calculate_profit(
        self,
        entry_price: float,
        hedge_price: float,
        shares: float,
    ) -> dict:
        """Calculate profit for a hedged position."""
        entry_cost = entry_price * shares
        hedge_cost = hedge_price * shares
        total_cost = entry_cost + hedge_cost
        payout = shares * 1.0
        profit = payout - total_cost
        profit_pct = (profit / total_cost) * 100 if total_cost > 0 else 0
        
        return {
            "entry_cost": entry_cost,
            "hedge_cost": hedge_cost,
            "total_cost": total_cost,
            "profit": profit,
            "profit_pct": profit_pct,
        }


class TestFillHandlerInit:
    """Tests for FillHandler initialization."""
    
    def test_init_stores_dependencies(self):
        """Handler stores injected dependencies."""
        odds_service = Mock()
        alerts = Mock()
        hedge_finder = Mock()
        
        handler = MockFillHandler(odds_service, alerts, hedge_finder)
        
        assert handler.odds_service is odds_service
        assert handler.alerts is alerts
        assert handler.hedge_finder is hedge_finder


class TestNaturalArbDetection:
    """Tests for natural arb detection in on_fill."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockFillHandler(Mock(), Mock(), Mock())
    
    def test_no_match_places_hedge(self, handler):
        """When no match found, hedge should be placed."""
        handler.bot_state.get_match_by_token.return_value = None
        position = MockPosition()
        
        should_hedge, opposite_team = handler._should_place_hedge(position)
        
        assert should_hedge is True
        assert opposite_team is None
    
    def test_natural_arb_with_opposite_order(self, handler):
        """Natural arb detected when opposite side has order."""
        match = Mock()
        match.token1 = "token_a"
        match.order2 = Mock()  # Opposite side has order
        match.position2 = None
        match.team2 = "Team B"
        handler.bot_state.get_match_by_token.return_value = match
        
        position = MockPosition(entry_token_id="token_a")
        
        should_hedge, opposite_team = handler._should_place_hedge(position)
        
        assert should_hedge is False
        assert opposite_team == "Team B"
    
    def test_natural_arb_with_opposite_position(self, handler):
        """Natural arb detected when opposite side has position."""
        match = Mock()
        match.token1 = "token_a"
        match.order2 = None
        match.position2 = Mock()  # Opposite side has position
        match.team2 = "Team B"
        handler.bot_state.get_match_by_token.return_value = match
        
        position = MockPosition(entry_token_id="token_a")
        
        should_hedge, opposite_team = handler._should_place_hedge(position)
        
        assert should_hedge is False
        assert opposite_team == "Team B"
    
    def test_no_natural_arb_when_opposite_empty(self, handler):
        """No natural arb when opposite side is empty."""
        match = Mock()
        match.token1 = "token_a"
        match.order2 = None
        match.position2 = None
        match.team2 = "Team B"
        handler.bot_state.get_match_by_token.return_value = match
        
        position = MockPosition(entry_token_id="token_a")
        
        should_hedge, opposite_team = handler._should_place_hedge(position)
        
        assert should_hedge is True
        assert opposite_team is None
    
    def test_side2_entry_checks_side1(self, handler):
        """Entry on side2 checks side1 for natural arb."""
        match = Mock()
        match.token1 = "token_a"
        match.token2 = "token_b"
        match.order1 = Mock()  # Side1 has order
        match.position1 = None
        match.team1 = "Team A"
        handler.bot_state.get_match_by_token.return_value = match
        
        position = MockPosition(entry_token_id="token_b")  # Entry on side2
        
        should_hedge, opposite_team = handler._should_place_hedge(position)
        
        assert should_hedge is False
        assert opposite_team == "Team A"


class TestProfitCalculation:
    """Tests for profit calculation in on_hedge_fill."""
    
    @pytest.fixture
    def handler(self):
        """Create mock handler."""
        return MockFillHandler(Mock(), Mock(), Mock())
    
    def test_profitable_hedge(self, handler):
        """Calculate profit for profitable hedge."""
        result = handler._calculate_profit(
            entry_price=0.45,
            hedge_price=0.50,
            shares=25.0,
        )
        
        assert abs(result["entry_cost"] - 11.25) < 0.01
        assert abs(result["hedge_cost"] - 12.50) < 0.01
        assert abs(result["total_cost"] - 23.75) < 0.01
        assert abs(result["profit"] - 1.25) < 0.01
        assert abs(result["profit_pct"] - 5.26) < 0.1
    
    def test_breakeven_hedge(self, handler):
        """Calculate profit for breakeven hedge."""
        result = handler._calculate_profit(
            entry_price=0.50,
            hedge_price=0.50,
            shares=25.0,
        )
        
        assert abs(result["profit"]) < 0.01
        assert abs(result["profit_pct"]) < 0.1
    
    def test_losing_hedge(self, handler):
        """Calculate profit (loss) for unprofitable hedge."""
        result = handler._calculate_profit(
            entry_price=0.55,
            hedge_price=0.50,
            shares=25.0,
        )
        
        assert result["profit"] < 0
        assert result["profit_pct"] < 0


class TestHydratedFillHandling:
    """Tests for hydrated (previous session) fill handling."""
    
    def test_hydrated_position_flag(self):
        """Hydrated positions are identified correctly."""
        position = MockPosition(is_hydrated=True)
        assert position.is_hydrated is True
    
    def test_regular_position_flag(self):
        """Regular positions are identified correctly."""
        position = MockPosition(is_hydrated=False)
        assert position.is_hydrated is False


class TestHydratedFillRecording:
    """Tests for hydrated fill database recording."""
    
    @pytest.fixture
    def mock_recorder(self):
        """Create mock recorder."""
        recorder = Mock()
        recorder.record_filled_order = AsyncMock()
        recorder.record_order_final_status = AsyncMock()
        recorder.record_arbitrage = AsyncMock()  # Add arbitrage recording mock
        return recorder
    
    @pytest.fixture
    def hydrated_position(self):
        """Create a hydrated position with order_id."""
        pos = MockPosition(is_hydrated=True)
        pos.order_id = "order_hydrated_123"
        pos.entry_team = "Test Team"
        pos.entry_price = 0.55
        pos.entry_token_id = "token_abc"
        pos.team1 = "Test Team"
        pos.team2 = "Opponent Team"
        pos.match_id = "match_123"
        pos.game = "cs2"
        pos.condition_id = "cond_456"
        pos.tournament = "Test Cup"
        pos.placed_at = None
        pos.fair_value = 0.60
        return pos
    
    @pytest.mark.asyncio
    async def test_hydrated_fill_records_to_database(self, mock_recorder, hydrated_position):
        """Hydrated fills should call record_filled_order."""
        from src.handlers.fill_handlers import FillHandler
        
        # Create handler with mock recorder
        handler = FillHandler(
            odds_service=Mock(),
            alerts=Mock(on_hydrated_fill=AsyncMock()),
            hedge_finder=Mock(),
            recorder=mock_recorder,
        )
        
        # Mock bot_state
        with patch('src.handlers.fill_handlers.get_bot_state') as mock_get_state:
            mock_bot_state = Mock()
            mock_bot_state.get_match_by_token.return_value = None
            mock_bot_state.get_match.return_value = None
            mock_get_state.return_value = mock_bot_state
            
            # Mock _get_raw_odds_snapshot
            handler._get_raw_odds_snapshot = Mock(return_value=None)
            handler._lookup_fair_value = Mock(return_value=0.60)
            
            await handler._handle_hydrated_fill(hydrated_position, filled_shares=10.0)
        
        # Verify record_filled_order was called
        mock_recorder.record_filled_order.assert_called_once()
        call_kwargs = mock_recorder.record_filled_order.call_args[1]
        
        assert call_kwargs["order_id"] == "order_hydrated_123"
        assert call_kwargs["token_id"] == "token_abc"
        assert call_kwargs["team"] == "Test Team"
        assert call_kwargs["price"] == 0.55
        assert call_kwargs["shares"] == 10.0
        assert call_kwargs["side"] == "entry"  # Not a hedge
    
    @pytest.mark.asyncio
    async def test_hydrated_fill_records_order_status(self, mock_recorder, hydrated_position):
        """Hydrated fills should record order status as FILLED."""
        from src.handlers.fill_handlers import FillHandler
        
        handler = FillHandler(
            odds_service=Mock(),
            alerts=Mock(on_hydrated_fill=AsyncMock()),
            hedge_finder=Mock(),
            recorder=mock_recorder,
        )
        
        with patch('src.handlers.fill_handlers.get_bot_state') as mock_get_state:
            mock_bot_state = Mock()
            mock_bot_state.get_match_by_token.return_value = None
            mock_bot_state.get_match.return_value = None
            mock_get_state.return_value = mock_bot_state
            
            handler._get_raw_odds_snapshot = Mock(return_value=None)
            handler._lookup_fair_value = Mock(return_value=0.60)
            
            await handler._handle_hydrated_fill(hydrated_position, filled_shares=10.0)
        
        # Verify record_order_final_status was called with FILLED
        mock_recorder.record_order_final_status.assert_called_once_with(
            order_id="order_hydrated_123",
            status="FILLED",
        )
    
    @pytest.mark.asyncio
    async def test_hydrated_fill_without_order_id_skips_recording(self, mock_recorder):
        """Hydrated fills without order_id should skip recording."""
        from src.handlers.fill_handlers import FillHandler
        
        # Position without order_id
        position = MockPosition(is_hydrated=True)
        position.entry_team = "Test Team"
        position.entry_price = 0.55
        # No order_id set!
        
        handler = FillHandler(
            odds_service=Mock(),
            alerts=Mock(on_hydrated_fill=AsyncMock()),
            hedge_finder=Mock(),
            recorder=mock_recorder,
        )
        
        with patch('src.handlers.fill_handlers.get_bot_state') as mock_get_state:
            mock_bot_state = Mock()
            mock_bot_state.get_match_by_token.return_value = None
            mock_bot_state.get_match.return_value = None
            mock_get_state.return_value = mock_bot_state
            
            handler._lookup_fair_value = Mock(return_value=0.60)
            
            await handler._handle_hydrated_fill(position, filled_shares=10.0)
        
        # Verify record_filled_order was NOT called
        mock_recorder.record_filled_order.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_hydrated_hedge_fill_records_as_hedge(self, mock_recorder, hydrated_position):
        """Hydrated fills on hedge side record side='hedge'."""
        from src.handlers.fill_handlers import FillHandler
        
        handler = FillHandler(
            odds_service=Mock(),
            alerts=Mock(on_hydrated_fill=AsyncMock()),
            hedge_finder=Mock(),
            recorder=mock_recorder,
        )
        
        with patch('src.handlers.fill_handlers.get_bot_state') as mock_get_state:
            mock_bot_state = Mock()
            mock_bot_state.get_match_by_token.return_value = None
            
            # Set up match with opposite position (makes this a hedge)
            mock_match = Mock()
            mock_match.token1 = hydrated_position.entry_token_id
            mock_match.token2 = "other_token"
            mock_match.position2 = Mock(team="Opponent", avg_price=0.40)
            mock_bot_state.get_match.return_value = mock_match
            mock_get_state.return_value = mock_bot_state
            
            handler._get_raw_odds_snapshot = Mock(return_value=None)
            handler._lookup_fair_value = Mock(return_value=0.60)
            
            await handler._handle_hydrated_fill(hydrated_position, filled_shares=10.0)
        
        # Verify side is recorded as "hedge" when opposite position exists
        call_kwargs = mock_recorder.record_filled_order.call_args[1]
        assert call_kwargs["side"] == "hedge"
    
    @pytest.mark.asyncio
    async def test_hydrated_hedge_fill_records_arbitrage(self, mock_recorder, hydrated_position):
        """Hydrated hedge fills should record completed arbitrage."""
        from src.handlers.fill_handlers import FillHandler
        
        handler = FillHandler(
            odds_service=Mock(),
            alerts=Mock(on_hydrated_fill=AsyncMock()),
            hedge_finder=Mock(),
            recorder=mock_recorder,
        )
        
        with patch('src.handlers.fill_handlers.get_bot_state') as mock_get_state:
            mock_bot_state = Mock()
            mock_bot_state.get_match_by_token.return_value = None
            
            # Set up match with opposite position (makes this a hedge)
            mock_match = Mock()
            mock_match.token1 = hydrated_position.entry_token_id
            mock_match.token2 = "other_token"
            mock_match.team1 = "Test Team"
            mock_match.team2 = "Opponent Team"
            mock_match.position2 = Mock(team="Opponent", avg_price=0.40)
            mock_bot_state.get_match.return_value = mock_match
            mock_get_state.return_value = mock_bot_state
            
            handler._get_raw_odds_snapshot = Mock(return_value=None)
            handler._lookup_fair_value = Mock(return_value=0.60)
            
            await handler._handle_hydrated_fill(hydrated_position, filled_shares=10.0)
        
        # Verify arbitrage was recorded
        mock_recorder.record_arbitrage.assert_called_once()
        arb_kwargs = mock_recorder.record_arbitrage.call_args[1]
        
        # entry_team/hedge_team should come from match state (not raw fill names)
        assert arb_kwargs["entry_team"] == "Opponent Team"
        assert arb_kwargs["entry_price"] == 0.40
        assert arb_kwargs["hedge_team"] == "Test Team"
        assert arb_kwargs["hedge_price"] == 0.55
        assert arb_kwargs["hedge_order_id"] == "order_hydrated_123"


class TestEdgeCalculation:
    """Tests for edge calculation used in fill logging."""
    
    def test_edge_from_fair_value(self):
        """Edge = fair_value - fill_price."""
        fair_value = 0.55
        fill_price = 0.50
        edge = fair_value - fill_price
        
        assert abs(edge - 0.05) < 0.001
    
    def test_edge_percentage_format(self):
        """Edge as percentage for display."""
        edge = 0.05
        edge_pct = edge * 100
        
        assert edge_pct == 5.0
    
    def test_negative_edge(self):
        """Negative edge when overpaying."""
        fair_value = 0.48
        fill_price = 0.50
        edge = fair_value - fill_price
        
        assert edge < 0

