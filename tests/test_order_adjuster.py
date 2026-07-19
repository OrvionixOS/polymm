"""
Unit tests for execution/order_adjuster.py - Pending order adjustment queue.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch

from src.execution.order_adjuster import OrderAdjuster


class TestOrderAdjuster:
    """Tests for OrderAdjuster class."""
    
    @pytest.fixture
    def adjuster(self):
        """Create OrderAdjuster with mocked dependencies."""
        executor = Mock()
        hedge_finder = Mock()
        order_monitor = Mock()
        order_monitor.adjust_order_fast = AsyncMock()
        config = {"min_edge": 0.08}
        
        return OrderAdjuster(
            executor=executor,
            hedge_finder=hedge_finder,
            order_monitor=order_monitor,
            config=config,
        )
    
    def test_queue_adjustment(self, adjuster):
        """queue_adjustment adds to pending adjustments."""
        adjuster.queue_adjustment("token_123", 0.50)
        
        assert "token_123" in adjuster._pending_adjustments
        assert adjuster._pending_adjustments["token_123"] == 0.50
    
    def test_queue_adjustment_overwrites(self, adjuster):
        """Later adjustments overwrite earlier ones for same token."""
        adjuster.queue_adjustment("token_123", 0.50)
        adjuster.queue_adjustment("token_123", 0.52)
        
        assert adjuster._pending_adjustments["token_123"] == 0.52
    
    def test_queue_multiple_tokens(self, adjuster):
        """Can queue adjustments for multiple tokens."""
        adjuster.queue_adjustment("token_1", 0.50)
        adjuster.queue_adjustment("token_2", 0.45)
        adjuster.queue_adjustment("token_3", 0.55)
        
        assert len(adjuster._pending_adjustments) == 3
    
    def test_has_pending_adjustments_true(self, adjuster):
        """has_pending_adjustments returns True when queue non-empty."""
        adjuster.queue_adjustment("token_123", 0.50)
        
        assert adjuster.has_pending_adjustments() is True
    
    def test_has_pending_adjustments_false(self, adjuster):
        """has_pending_adjustments returns False when queue empty."""
        assert adjuster.has_pending_adjustments() is False
    
    @pytest.mark.asyncio
    async def test_process_pending_clears_queue(self, adjuster):
        """Processing clears the pending adjustments queue."""
        adjuster.queue_adjustment("token_123", 0.50)
        
        watcher = Mock()
        hedge_tokens = set()
        
        with patch("src.execution.order_adjuster.PolymarketEsportsClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=Mock())
            mock_client.return_value.__aexit__ = AsyncMock()
            
            await adjuster.process_pending_adjustments(watcher, hedge_tokens)
        
        assert adjuster.has_pending_adjustments() is False
    
    @pytest.mark.asyncio
    async def test_process_pending_calls_order_monitor(self, adjuster):
        """Processing calls order_monitor.adjust_order_fast for each token."""
        adjuster.queue_adjustment("token_1", 0.50)
        adjuster.queue_adjustment("token_2", 0.45)
        
        watcher = Mock()
        hedge_tokens = {"token_2"}
        
        with patch("src.execution.order_adjuster.PolymarketEsportsClient") as mock_client:
            mock_poly_instance = Mock()
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_poly_instance)
            mock_client.return_value.__aexit__ = AsyncMock()
            
            await adjuster.process_pending_adjustments(watcher, hedge_tokens)
        
        # Should have been called for both tokens
        assert adjuster.order_monitor.adjust_order_fast.call_count == 2
    
    @pytest.mark.asyncio
    async def test_process_pending_no_adjustments(self, adjuster):
        """Processing with empty queue does nothing."""
        watcher = Mock()
        hedge_tokens = set()
        
        # Should not raise or call anything
        await adjuster.process_pending_adjustments(watcher, hedge_tokens)
        
        assert adjuster.order_monitor.adjust_order_fast.call_count == 0
    
    @pytest.mark.asyncio
    async def test_process_pending_handles_errors(self, adjuster):
        """Processing continues even if one adjustment fails."""
        adjuster.queue_adjustment("token_1", 0.50)
        adjuster.queue_adjustment("token_2", 0.45)
        
        # First call fails, second succeeds
        adjuster.order_monitor.adjust_order_fast = AsyncMock(
            side_effect=[Exception("API error"), None]
        )
        
        watcher = Mock()
        hedge_tokens = set()
        
        with patch("src.execution.order_adjuster.PolymarketEsportsClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=Mock())
            mock_client.return_value.__aexit__ = AsyncMock()
            
            # Should not raise
            await adjuster.process_pending_adjustments(watcher, hedge_tokens)
        
        # Both should have been attempted
        assert adjuster.order_monitor.adjust_order_fast.call_count == 2
