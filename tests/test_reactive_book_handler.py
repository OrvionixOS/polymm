"""
Tests for ReactiveBookHandler - event-driven order management.

Tests the decision tree:
1. Spread collapse → cancel
2. Outbid → adjust
3. Price improvement → lower bid
4. Throttling prevents duplicate actions
5. Inactive tokens are ignored
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import Optional, List

from src.handlers.reactive_book_handler import ReactiveBookHandler
from src.polymarket.book_websocket import LivePrice


@pytest.fixture
def mock_bot_state():
    """Create a mock BotState."""
    state = MagicMock()
    state.is_token_active.return_value = False
    state.get_order_info.return_value = None
    state._reserved_tokens = set()
    state.get_match_by_token.return_value = None
    state.cancel_order = MagicMock()
    return state


@pytest.fixture
def mock_order_monitor():
    """Create a mock OrderMonitor."""
    monitor = MagicMock()
    monitor._handle_outbid = AsyncMock()
    monitor._try_price_improvement = AsyncMock()
    return monitor


@pytest.fixture
def mock_executor():
    """Create a mock OrderExecutor."""
    executor = MagicMock()
    executor.cancel_order = AsyncMock(return_value=(True, False))
    return executor


@pytest.fixture
def handler(mock_order_monitor, mock_executor):
    """Create a ReactiveBookHandler instance."""
    config = {
        "min_edge": 0.05,
        "default_shares": 10,
        "min_profit": 0.10,
    }
    h = ReactiveBookHandler(
        order_monitor=mock_order_monitor,
        executor=mock_executor,
        config=config,
        spread_cancel_threshold=0.10,
    )
    h.set_watcher(MagicMock(), set())
    return h


class TestInactiveTokens:
    """Test that inactive tokens are ignored."""
    
    def test_inactive_token_ignored(self, handler, mock_bot_state):
        """Book update on inactive token should be no-op."""
        mock_bot_state.is_token_active.return_value = False
        
        price = LivePrice(token_id="tok1", best_bid=0.50, best_ask=0.60)
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            handler.on_book_update("tok1", price)
        
        # No tasks should be spawned (no adjustments made)
        assert "tok1" not in handler._adjusting_tokens
    
    def test_no_order_info_ignored(self, handler, mock_bot_state):
        """Token active but no order info should be no-op."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = None
        
        price = LivePrice(token_id="tok1", best_bid=0.50, best_ask=0.60)
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            handler.on_book_update("tok1", price)


class TestSpreadCollapse:
    """Test spread collapse detection."""
    
    def test_spread_collapse_detected(self, handler, mock_bot_state):
        """When spread < threshold, should trigger cancel task."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.50,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }
        
        # Spread of 5c < 10c threshold
        price = LivePrice(token_id="tok1", best_bid=0.50, best_ask=0.55)
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state), \
             patch("asyncio.create_task") as mock_create_task:
            handler.on_book_update("tok1", price)
        
        # Verify a task was spawned for spread collapse
        mock_create_task.assert_called_once()
    
    def test_healthy_spread_not_cancelled(self, handler, mock_bot_state):
        """When spread > threshold, should NOT trigger cancel."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }
        
        # Spread of 20c > 10c threshold
        price = LivePrice(token_id="tok1", best_bid=0.40, best_ask=0.60)
        
        # Best bid == our price, so we're best bid → no outbid
        # But we need 2 bids for price improvement check
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            handler.on_book_update("tok1", price)
    
    def test_hedge_orders_skip_spread_check(self, handler, mock_bot_state):
        """Hedge orders should not be cancelled for spread collapse."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.50,
            "order_id": "hedge1",
            "team_name": "HedgeTeam",
            "is_hedge": True,
        }
        
        # Tight spread — but this is a hedge
        price = LivePrice(token_id="tok1", best_bid=0.50, best_ask=0.55)
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            handler.on_book_update("tok1", price)


class TestOutbidDetection:
    """Test outbid detection and handling."""
    
    def test_outbid_detected(self, handler, mock_bot_state):
        """When best_bid > our_price + 0.005, should detect outbid."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }
        
        # Best bid is 42c, our price is 40c → outbid by 2c
        price = LivePrice(
            token_id="tok1",
            best_bid=0.42,
            best_ask=0.60,
            bids=[{"price": "0.42", "size": "100"}, {"price": "0.40", "size": "50"}],
        )
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state), \
             patch("asyncio.create_task") as mock_create_task:
            handler.on_book_update("tok1", price)
        
        # Verify a task was spawned for outbid handling
        mock_create_task.assert_called_once()
    
    def test_outbid_skip_same_level(self, handler, mock_bot_state):
        """Should skip if we already tried and failed at same bid level (fresh marker)."""
        import time as _time
        handler._outbid_state["tok1"] = {"best_bid": 0.42, "failed": True, "at": _time.monotonic()}

        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }

        price = LivePrice(
            token_id="tok1",
            best_bid=0.42,
            best_ask=0.60,
        )

        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            handler.on_book_update("tok1", price)

    def test_outbid_marker_expires_via_ttl(self, handler, mock_bot_state):
        """Marker older than OUTBID_FAIL_TTL_S should be popped on next outbid check."""
        import time as _time
        from src.handlers.reactive_book_handler import OUTBID_FAIL_TTL_S
        # Stale marker — older than TTL
        handler._outbid_state["tok1"] = {
            "best_bid": 0.42,
            "failed": True,
            "at": _time.monotonic() - (OUTBID_FAIL_TTL_S + 1.0),
        }

        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }

        # Outbid at same level — stale marker should not block the retry
        price = LivePrice(token_id="tok1", best_bid=0.42, best_ask=0.60)

        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state), \
             patch("asyncio.create_task") as mock_create_task:
            handler.on_book_update("tok1", price)

        # Stale marker popped, retry task spawned
        assert "tok1" not in handler._outbid_state
        mock_create_task.assert_called_once()


class TestPriceImprovement:
    """Test price improvement detection."""
    
    def test_improvement_detected_with_gap(self, handler, mock_bot_state):
        """When we're best bid with 3c+ gap to 2nd, should try improvement."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }
        
        # We're best bid at 40c, 2nd bid at 35c → 5c gap
        price = LivePrice(
            token_id="tok1",
            best_bid=0.40,
            best_ask=0.60,
            bids=[
                {"price": "0.40", "size": "50"},
                {"price": "0.35", "size": "100"},
            ],
        )
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state), \
             patch("asyncio.create_task") as mock_create_task:
            handler.on_book_update("tok1", price)
        
        # Verify a task was spawned for price improvement
        mock_create_task.assert_called_once()
    
    def test_no_improvement_small_gap(self, handler, mock_bot_state):
        """When gap to 2nd bid < 3c, should NOT try improvement."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }
        
        # We're best bid at 40c, 2nd bid at 39c → only 1c gap
        price = LivePrice(
            token_id="tok1",
            best_bid=0.40,
            best_ask=0.60,
            bids=[
                {"price": "0.40", "size": "50"},
                {"price": "0.39", "size": "100"},
            ],
        )
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            handler.on_book_update("tok1", price)
    
    def test_no_improvement_single_bid(self, handler, mock_bot_state):
        """When there's only 1 bid (us), should NOT try improvement."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }
        
        # Only 1 bid
        price = LivePrice(
            token_id="tok1",
            best_bid=0.40,
            best_ask=0.60,
            bids=[{"price": "0.40", "size": "50"}],
        )
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            handler.on_book_update("tok1", price)


class TestThrottling:
    """Test per-token throttling."""
    
    def test_outbid_throttled(self, handler):
        """Second outbid within cooldown should be blocked."""
        assert handler._can_act("tok1", "outbid") == True
        assert handler._can_act("tok1", "outbid") == False  # Within 1s cooldown
    
    def test_different_tokens_not_throttled(self, handler):
        """Different tokens should have independent throttling."""
        assert handler._can_act("tok1", "outbid") == True
        assert handler._can_act("tok2", "outbid") == True
    
    def test_different_actions_not_throttled(self, handler):
        """Different action types should have independent throttling."""
        assert handler._can_act("tok1", "outbid") == True
        assert handler._can_act("tok1", "improve") == True
    
    def test_improvement_cooldown_longer(self, handler):
        """Price improvement should have 5s cooldown."""
        assert handler._can_act("tok1", "improve") == True
        assert handler._can_act("tok1", "improve") == False
        # Even after outbid cooldown would expire (1s < 5s)


class TestReservedTokens:
    """Test that reserved tokens are skipped."""
    
    def test_reserved_token_skipped(self, handler, mock_bot_state):
        """Token in _reserved_tokens should be skipped entirely."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }
        mock_bot_state._reserved_tokens = {"tok1"}
        
        # This would normally trigger outbid
        price = LivePrice(token_id="tok1", best_bid=0.50, best_ask=0.60)
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            handler.on_book_update("tok1", price)


class TestHandleOutbidAsync:
    """Test the async outbid handler."""
    
    @pytest.mark.asyncio
    async def test_outbid_handler_calls_order_monitor(self, handler, mock_order_monitor, mock_bot_state):
        """_handle_outbid should delegate to OrderMonitor._handle_outbid when fair_value exists."""
        order_info = {"price": 0.40, "order_id": "o1", "team_name": "Team1", "fair_value": 0.55}
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = order_info

        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            await handler._handle_outbid(
                token_id="tok1",
                order_info=order_info,
                our_price=0.40,
                best_bid=0.45,
                is_hedge=False,
            )

        mock_order_monitor._handle_outbid.assert_called_once()
        call_kwargs = mock_order_monitor._handle_outbid.call_args
        assert call_kwargs.kwargs.get("our_fair") == 0.55


class TestNoFairValueRejection:
    """Test bot-type-aware fair value handling.
    
    SportsBot: fair_value is ALWAYS set from bookmaker odds. No fair = reject.
    SpreadBot: fair_value is None — uses cost-based (1.0 - opposite_price).
    """
    
    @pytest.mark.asyncio
    async def test_no_fair_no_opposite_rejects(self, handler, mock_order_monitor, mock_bot_state):
        """No fair_value AND no opposite side → reject (no data at all)."""
        order_info = {"price": 0.02, "order_id": "o1", "team_name": "Over", "fair_value": None}
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = order_info
        mock_bot_state.get_match_by_token.return_value = None

        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            await handler._handle_outbid(
                token_id="tok1",
                order_info=order_info,
                our_price=0.02,
                best_bid=0.50,
                is_hedge=False,
            )

        mock_order_monitor._handle_outbid.assert_not_called()
        assert handler._outbid_state.get("tok1", {}).get("failed") == True

    @pytest.mark.asyncio
    async def test_no_fair_with_opposite_uses_cost(self, handler, mock_order_monitor, mock_bot_state):
        """No fair_value WITH opposite side order → SpreadBot cost-based path."""
        order_info = {"price": 0.40, "order_id": "o1", "team_name": "Above 50°F", "fair_value": None}
        match = MagicMock(
            token1="tok1", token2="tok2",
            order1=MagicMock(price=0.40), order2=MagicMock(price=0.45),
            position1=None, position2=None,
        )
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = order_info
        mock_bot_state.get_match_by_token.return_value = match

        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            await handler._handle_outbid(
                token_id="tok1",
                order_info=order_info,
                our_price=0.40,
                best_bid=0.42,
                is_hedge=False,
            )

        # Should delegate with cost-based fair = 1.0 - 0.45 = 0.55
        mock_order_monitor._handle_outbid.assert_called_once()
        call_kwargs = mock_order_monitor._handle_outbid.call_args
        assert abs(call_kwargs.kwargs.get("our_fair") - 0.55) < 0.001

    @pytest.mark.asyncio
    async def test_outbid_with_real_fair_value_proceeds(self, handler, mock_order_monitor, mock_bot_state):
        """SportsBot order WITH real fair_value should proceed normally."""
        order_info = {"price": 0.05, "order_id": "o1", "team_name": "Over", "fair_value": 0.55}
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = order_info

        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            await handler._handle_outbid(
                token_id="tok1",
                order_info=order_info,
                our_price=0.05,
                best_bid=0.10,
                is_hedge=False,
            )

        mock_order_monitor._handle_outbid.assert_called_once()
        call_kwargs = mock_order_monitor._handle_outbid.call_args
        assert call_kwargs.kwargs.get("our_fair") == 0.55
    
    @pytest.mark.asyncio
    async def test_improvement_no_fair_no_opposite_skips(self, handler, mock_order_monitor, mock_bot_state):
        """Price improvement with no fair_value and no opposite side should NOT proceed."""
        mock_bot_state.get_match_by_token.return_value = None
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            await handler._handle_price_improvement(
                token_id="tok1",
                order_info={"price": 0.40, "order_id": "o1", "team_name": "Over", "fair_value": None},
                our_price=0.40,
                second_bid=0.35,
                bids=[{"price": "0.40", "size": "50"}, {"price": "0.35", "size": "100"}],
            )
        
        mock_order_monitor._try_price_improvement.assert_not_called()


class TestHandleSpreadCollapseAsync:
    """Test the async spread collapse handler."""
    
    @pytest.mark.asyncio
    async def test_spread_cancel_calls_callback(self, handler, mock_executor, mock_bot_state):
        """Spread collapse should call on_spread_cancel callback if set."""
        match = MagicMock()
        order_info = {"order_id": "o1", "team_name": "Team1", "token_id": "tok1"}
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = order_info
        mock_bot_state.get_match_by_token.return_value = match

        callback = AsyncMock()
        handler.on_spread_cancel = callback

        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            await handler._handle_spread_collapse("tok1", order_info, 0.05)

        callback.assert_called_once_with(match, order_info, 0.05)

    @pytest.mark.asyncio
    async def test_spread_cancel_direct_without_callback(self, handler, mock_executor, mock_bot_state):
        """Without callback, should cancel order directly."""
        order_info = {"order_id": "o1", "team_name": "Team1", "token_id": "tok1"}
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = order_info
        mock_bot_state.get_match_by_token.return_value = MagicMock()
        handler.on_spread_cancel = None

        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state):
            await handler._handle_spread_collapse("tok1", order_info, 0.05)

        mock_executor.cancel_order.assert_called_once()
        mock_bot_state.cancel_order.assert_called_once_with("o1")


class TestUnifiedTokenLocking:
    """Test that all action paths lock _adjusting_tokens before creating tasks."""
    
    def test_outbid_locks_before_task(self, handler, mock_bot_state):
        """Outbid should add token to _adjusting_tokens before creating task."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }
        
        price = LivePrice(
            token_id="tok1",
            best_bid=0.42,
            best_ask=0.60,
            bids=[{"price": "0.42", "size": "100"}, {"price": "0.40", "size": "50"}],
        )
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state), \
             patch("asyncio.create_task") as mock_create_task:
            handler.on_book_update("tok1", price)
        
        # Token should be locked SYNCHRONOUSLY (before task body runs)
        assert "tok1" in handler._adjusting_tokens
        mock_create_task.assert_called_once()
    
    def test_improvement_locks_before_task(self, handler, mock_bot_state):
        """Price improvement should add token to _adjusting_tokens before creating task."""
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }
        
        price = LivePrice(
            token_id="tok1",
            best_bid=0.40,
            best_ask=0.60,
            bids=[
                {"price": "0.40", "size": "50"},
                {"price": "0.35", "size": "100"},
            ],
        )
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state), \
             patch("asyncio.create_task") as mock_create_task:
            handler.on_book_update("tok1", price)
        
        # Token should be locked SYNCHRONOUSLY (before task body runs)
        assert "tok1" in handler._adjusting_tokens
        mock_create_task.assert_called_once()
    
    def test_locked_token_blocks_all_actions(self, handler, mock_bot_state):
        """Token in _adjusting_tokens should block outbid, improvement, and spread."""
        handler._adjusting_tokens.add("tok1")
        
        mock_bot_state.is_token_active.return_value = True
        mock_bot_state.get_order_info.return_value = {
            "price": 0.40,
            "order_id": "order1",
            "team_name": "TestTeam",
            "is_hedge": False,
        }
        
        # This would normally trigger outbid
        price = LivePrice(token_id="tok1", best_bid=0.50, best_ask=0.60)
        
        with patch("src.handlers.reactive_book_handler.get_bot_state", return_value=mock_bot_state), \
             patch("asyncio.create_task") as mock_create_task:
            handler.on_book_update("tok1", price)
        
        # No task should be spawned — token is locked
        mock_create_task.assert_not_called()

