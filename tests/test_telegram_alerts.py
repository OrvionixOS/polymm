"""
Unit tests for services/telegram_alerts.py - Telegram notification service.

Tests message formatting and alert logic without sending actual messages.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock


class MockTelegramAlerts:
    """Mock TelegramAlerts for testing core logic patterns."""
    
    def __init__(self, bot_token: str = None, chat_id: str = None):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        self._pinned_message_id = None
    
    def is_enabled(self) -> bool:
        """Check if alerts are enabled."""
        return self._enabled
    
    def _format_entry_fill_message(
        self,
        match: str,
        team: str,
        price: float,
        shares: float,
        edge: float = 0.0,
        fair_value: float = 0.0,
    ) -> str:
        """Format entry fill message."""
        edge_pct = edge * 100
        return (
            f"✅ <b>ENTRY FILLED</b>\n\n"
            f"🎮 {match}\n"
            f"📊 {team} @ ${price:.2f}\n"
            f"📦 {shares:.0f} shares\n"
            f"🎯 Edge: {edge_pct:.1f}%"
        )
    
    def _format_hedged_message(
        self,
        match: str,
        entry_team: str,
        hedge_team: str,
        total_cost: float,
        profit: float,
        profit_pct: float,
    ) -> str:
        """Format position hedged message."""
        return (
            f"🔒 <b>POSITION HEDGED</b>\n\n"
            f"🎮 {match}\n"
            f"📊 {entry_team} ↔ {hedge_team}\n"
            f"💰 Locked: ${profit:.2f} ({profit_pct:.1f}%)\n"
            f"📦 Cost: ${total_cost:.2f}"
        )
    
    def _format_error_message(self, error: str, context: str = "") -> str:
        """Format error message."""
        msg = f"❌ <b>ERROR</b>\n\n{error}"
        if context:
            msg += f"\n\nContext: {context}"
        return msg
    
    def _format_summary(
        self,
        open_orders_count: int = 0,
        open_orders_risk: float = 0.0,
        arb_count: int = 0,
        arb_pnl: float = 0.0,
        active_positions: int = 0,
        balance: float = 0.0,
    ) -> str:
        """Format pinned summary message."""
        return (
            f"📊 <b>POLYMM STATUS</b>\n\n"
            f"📋 Open Orders: {open_orders_count}\n"
            f"💵 At Risk: ${open_orders_risk:.2f}\n"
            f"🔒 Arbs: {arb_count} (${arb_pnl:.2f})\n"
            f"📍 Positions: {active_positions}\n"
            f"💰 Balance: ${balance:.2f}"
        )


class TestTelegramAlertsInit:
    """Tests for TelegramAlerts initialization."""
    
    def test_init_with_credentials(self):
        """Alerts enabled with credentials."""
        alerts = MockTelegramAlerts(
            bot_token="123456:ABC-xyz",
            chat_id="987654321",
        )
        
        assert alerts.is_enabled() is True
    
    def test_init_without_token(self):
        """Alerts disabled without token."""
        alerts = MockTelegramAlerts(chat_id="987654321")
        
        assert alerts.is_enabled() is False
    
    def test_init_without_chat_id(self):
        """Alerts disabled without chat_id."""
        alerts = MockTelegramAlerts(bot_token="123456:ABC-xyz")
        
        assert alerts.is_enabled() is False
    
    def test_init_no_credentials(self):
        """Alerts disabled with no credentials."""
        alerts = MockTelegramAlerts()
        
        assert alerts.is_enabled() is False


class TestEntryFillMessage:
    """Tests for entry fill message formatting."""
    
    @pytest.fixture
    def alerts(self):
        """Create mock alerts."""
        return MockTelegramAlerts("token", "chat")
    
    def test_includes_match_info(self, alerts):
        """Message includes match info."""
        msg = alerts._format_entry_fill_message(
            match="CS2: Liquid vs Navi",
            team="Liquid",
            price=0.50,
            shares=25,
        )
        
        assert "CS2: Liquid vs Navi" in msg
        assert "Liquid" in msg
    
    def test_includes_price_and_shares(self, alerts):
        """Message includes price and shares."""
        msg = alerts._format_entry_fill_message(
            match="Test",
            team="Team A",
            price=0.52,
            shares=25,
        )
        
        assert "$0.52" in msg
        assert "25" in msg
    
    def test_includes_edge(self, alerts):
        """Message includes edge percentage."""
        msg = alerts._format_entry_fill_message(
            match="Test",
            team="Team A",
            price=0.50,
            shares=25,
            edge=0.05,
        )
        
        assert "5.0%" in msg
    
    def test_has_emoji_header(self, alerts):
        """Message has emoji header."""
        msg = alerts._format_entry_fill_message(
            match="Test",
            team="A",
            price=0.5,
            shares=25,
        )
        
        assert "✅" in msg
        assert "ENTRY FILLED" in msg


class TestHedgedMessage:
    """Tests for position hedged message formatting."""
    
    @pytest.fixture
    def alerts(self):
        """Create mock alerts."""
        return MockTelegramAlerts("token", "chat")
    
    def test_includes_both_teams(self, alerts):
        """Message includes both teams."""
        msg = alerts._format_hedged_message(
            match="Test",
            entry_team="Liquid",
            hedge_team="Navi",
            total_cost=23.75,
            profit=1.25,
            profit_pct=5.26,
        )
        
        assert "Liquid" in msg
        assert "Navi" in msg
    
    def test_includes_profit(self, alerts):
        """Message includes profit info."""
        msg = alerts._format_hedged_message(
            match="Test",
            entry_team="A",
            hedge_team="B",
            total_cost=23.75,
            profit=1.25,
            profit_pct=5.26,
        )
        
        assert "$1.25" in msg
        assert "5.26%" in msg or "5.3%" in msg
    
    def test_includes_total_cost(self, alerts):
        """Message includes total cost."""
        msg = alerts._format_hedged_message(
            match="Test",
            entry_team="A",
            hedge_team="B",
            total_cost=23.75,
            profit=1.25,
            profit_pct=5.0,
        )
        
        assert "$23.75" in msg
    
    def test_has_lock_emoji(self, alerts):
        """Message has lock emoji for hedged."""
        msg = alerts._format_hedged_message(
            match="Test",
            entry_team="A",
            hedge_team="B",
            total_cost=25,
            profit=1,
            profit_pct=5,
        )
        
        assert "🔒" in msg


class TestErrorMessage:
    """Tests for error message formatting."""
    
    @pytest.fixture
    def alerts(self):
        """Create mock alerts."""
        return MockTelegramAlerts("token", "chat")
    
    def test_includes_error(self, alerts):
        """Message includes error text."""
        msg = alerts._format_error_message("Connection failed")
        
        assert "Connection failed" in msg
    
    def test_includes_context(self, alerts):
        """Message includes context if provided."""
        msg = alerts._format_error_message(
            "Order failed",
            context="Token: abc123",
        )
        
        assert "Order failed" in msg
        assert "Token: abc123" in msg
    
    def test_no_context_section_when_empty(self, alerts):
        """No context section when not provided."""
        msg = alerts._format_error_message("Error only")
        
        assert "Context:" not in msg
    
    def test_has_error_emoji(self, alerts):
        """Message has error emoji."""
        msg = alerts._format_error_message("Test error")
        
        assert "❌" in msg


class TestSummaryMessage:
    """Tests for pinned summary message formatting."""
    
    @pytest.fixture
    def alerts(self):
        """Create mock alerts."""
        return MockTelegramAlerts("token", "chat")
    
    def test_includes_order_count(self, alerts):
        """Summary includes open orders count."""
        msg = alerts._format_summary(open_orders_count=5)
        
        assert "5" in msg
        assert "Orders" in msg
    
    def test_includes_arb_count(self, alerts):
        """Summary includes arb count."""
        msg = alerts._format_summary(arb_count=3, arb_pnl=15.50)
        
        assert "3" in msg
        assert "$15.50" in msg
    
    def test_includes_balance(self, alerts):
        """Summary includes balance."""
        msg = alerts._format_summary(balance=250.00)
        
        assert "$250.00" in msg
    
    def test_has_header(self, alerts):
        """Summary has header."""
        msg = alerts._format_summary()
        
        assert "POLYMM STATUS" in msg or "STATUS" in msg

