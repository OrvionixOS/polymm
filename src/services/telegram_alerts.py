"""
Telegram Alerts - Send notifications for trading events.

Alerts on:
- Order fills
- Positions hedged
- Errors
- Daily P&L summary
"""
import asyncio
import os
from datetime import datetime
from typing import Optional
import aiohttp
from dotenv import load_dotenv

load_dotenv()


class TelegramAlerts:
    """Send trading alerts via Telegram bot."""
    
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        """
        Args:
            bot_token: Telegram bot token (from @BotFather)
            chat_id: Your Telegram chat/user ID
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self._enabled = bool(self.bot_token and self.chat_id)
        
        # Summary message (sent + edited, not pinned)
        self._summary_message_id: Optional[int] = None
        self._last_summary_update: Optional[datetime] = None
        
        if not self._enabled:
            print("⚠️ Telegram alerts disabled (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)")
    
    async def send(self, message: str, parse_mode: str = "HTML", retries: int = 2) -> Optional[int]:
        """Send a message via Telegram. Returns message_id if successful."""
        if not self._enabled:
            return None
        
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
        }
        
        timeout = aiohttp.ClientTimeout(total=10)  # 10 second timeout
        
        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data.get("result", {}).get("message_id")
                        else:
                            print(f"⚠️ Telegram error: {await resp.text()}")
                            return None
            except asyncio.TimeoutError:
                if attempt < retries:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                print(f"⚠️ Telegram send timeout after {retries + 1} attempts")
                return None
            except Exception as e:
                if attempt < retries:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                print(f"⚠️ Telegram send error: {e}")
                return None
        
        return None
    
    async def edit_message(self, message_id: int, text: str, parse_mode: str = "HTML", retries: int = 2) -> bool:
        """Edit an existing message. Returns True if successful."""
        if not self._enabled:
            return False
        
        url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        
        timeout = aiohttp.ClientTimeout(total=10)  # 10 second timeout
        
        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            return True
                        else:
                            # Telegram returns error if message content is same - ignore
                            error_text = await resp.text()
                            if "message is not modified" not in error_text.lower():
                                print(f"⚠️ Telegram edit error: {error_text}")
                            return False
            except asyncio.TimeoutError:
                if attempt < retries:
                    await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff
                    continue
                # Silently fail on final timeout - not critical
                return False
            except Exception as e:
                if attempt < retries:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                # Only log on final failure
                print(f"⚠️ Telegram edit error: {e}")
                return False
        
        return False
    

    
    async def on_entry_placed(
        self,
        match: str,
        team: str,
        price: float,
        shares: int,
        edge: float,
    ):
        """Alert when entry order is placed."""
        await self.send(
            f"📝 <b>Entry Order Placed</b>\n\n"
            f"🎮 {match}\n"
            f"📊 {team} @ {price:.2f} ({shares} shares)\n"
            f"📈 Edge: {edge*100:.1f}%"
        )
    
    async def on_entry_fill(
        self,
        match: str,
        team: str,
        price: float,
        shares: float,
        edge: float = 0.0,
        fair_value: float = 0.0,
        is_live: bool = False,
        is_weather: bool = False,
        odds_age_seconds: float = None,
        spread_at_fill: float = None,
    ):
        """Alert when entry order fills."""
        prefix = "☁️ WEATHER " if is_weather else ("🔴 LIVE " if is_live else "")
        edge_str = f"\n📈 Edge: {edge*100:.1f}%" if edge > 0 else ""
        fair_str = f" (fair: {fair_value:.0f}%)" if fair_value > 0 else ""
        
        # Format odds freshness
        freshness_str = ""
        if odds_age_seconds is not None:
            if odds_age_seconds < 60:
                freshness_str = f" [{int(odds_age_seconds)}s old]"
            else:
                freshness_str = f" [{int(odds_age_seconds/60)}m old]"
        
        # Format spread
        spread_str = ""
        if spread_at_fill is not None:
            spread_cents = spread_at_fill * 100
            spread_str = f"\n📏 Spread: {spread_cents:.1f}c"
        
        await self.send(
            f"✅ <b>{prefix}Entry Filled!</b>\n\n"
            f"🎮 {match}\n"
            f"📊 {team} @ {price:.2f}{fair_str}{freshness_str} ({shares} shares)\n"
            f"💵 Cost: ${price * shares:.2f}{edge_str}{spread_str}"
        )
    
    async def on_position_hedged(
        self,
        match: str,
        entry_team: str,
        hedge_team: str,
        total_cost: float,
        profit: float,
        profit_pct: float,
        is_live: bool = False,
        is_weather: bool = False,
    ):
        """Alert when position is fully hedged."""
        prefix = "☁️ WEATHER " if is_weather else ("🔴 LIVE " if is_live else "")
        await self.send(
            f"🎉 <b>{prefix}Position Hedged!</b>\n\n"
            f"🎮 {match}\n"
            f"📊 {entry_team} + {hedge_team}\n"
            f"💵 Total Cost: ${total_cost:.2f}\n"
            f"💰 <b>Locked Profit: ${profit:.2f} ({profit_pct:.1f}%)</b>"
        )
    
    async def on_error(self, error: str, context: str = ""):
        """Alert on error."""
        await self.send(
            f"❌ <b>Error</b>\n\n"
            f"📍 {context}\n"
            f"⚠️ {error}"
        )
    
    async def on_low_balance(self, balance: float, threshold: float):
        """Alert when balance is low."""
        await self.send(
            f"⚠️ <b>Low Balance Warning</b>\n\n"
            f"💵 Current: ${balance:.2f}\n"
            f"📉 Threshold: ${threshold:.2f}\n\n"
            f"Bot will pause new orders until balance is topped up."
        )
    
    async def on_hydrated_fill(
        self,
        match: str,
        team: str,
        price: float,
        shares: float,
        edge: float = None,
        fair_value: float = None,
        is_hedge: bool = False,
        opposite_team: str = None,
        opposite_price: float = None,
        arb_profit_pct: float = None,
        is_live: bool = False,
        is_weather: bool = False,
        odds_age_seconds: float = None,
        spread_at_fill: float = None,
    ):
        """Alert when a hydrated (pre-existing) order fills."""
        prefix = "☁️ WEATHER " if is_weather else ("🔴 LIVE " if is_live else "")
        # edge is a decimal (0.15 for 15%)
        if edge is not None:
            edge_pct = edge * 100
            edge_dollars = edge * shares
            edge_str = f"\n📈 Edge: {edge_pct:+.1f}% (${edge_dollars:+.2f})"
        else:
            edge_str = ""
        fair_str = f" (fair: {fair_value:.0f}%)" if fair_value else ""
        
        # Format odds freshness (only for non-hedge fills)
        freshness_str = ""
        if not is_hedge and odds_age_seconds is not None:
            if odds_age_seconds < 60:
                freshness_str = f" [{int(odds_age_seconds)}s old]"
            else:
                freshness_str = f" [{int(odds_age_seconds/60)}m old]"
        
        # Format spread
        spread_str = ""
        if spread_at_fill is not None:
            spread_cents = spread_at_fill * 100
            spread_str = f"\n📏 Spread: {spread_cents:.1f}c"
        
        # If this is a hedge that completed an arb, show arb info instead of edge
        if is_hedge and opposite_team and opposite_price is not None:
            total_cost = price + opposite_price
            profit_dollars = (1.0 - total_cost) * shares
            arb_str = (
                f"\n\n🎯 <b>ARBITRAGE COMPLETE!</b>\n"
                f"📦 {opposite_team} @ {opposite_price:.2f} + {team} @ {price:.2f}\n"
                f"💰 Total cost: ${total_cost:.2f} → $1.00 payout\n"
                f"📈 Profit: {arb_profit_pct:+.1f}% (${profit_dollars:+.2f})"
            )
            # Don't show edge for hedge fills - arb profit is what matters
            edge_str = ""
        else:
            arb_str = ""
        
        fill_type = f"{prefix}Hedge Filled" if is_hedge else f"{prefix}Order Filled (Hydrated)"
        await self.send(
            f"✅ <b>{fill_type}</b>\n\n"
            f"🎮 {match}\n"
            f"📊 {team} @ {price:.2f}{fair_str}{freshness_str} ({shares:.2f} shares)\n"
            f"💵 Cost: ${price * shares:.2f}{edge_str}{spread_str}{arb_str}\n"
        )
    
    async def on_websocket_reconnect(self, ws_type: str):
        """Alert when WebSocket reconnects. Disabled - too noisy."""
        pass  # Disabled
    
    async def daily_summary(
        self,
        total_positions: int,
        hedged_positions: int,
        total_profit: float,
        balance: float,
    ):
        """Send daily P&L summary."""
        await self.send(
            f"📊 <b>Daily Summary</b>\n\n"
            f"📈 Positions: {hedged_positions}/{total_positions} hedged\n"
            f"💰 Today's Profit: ${total_profit:.2f}\n"
            f"💵 Balance: ${balance:.2f}"
        )
    
    def _format_summary(
        self,
        open_orders_count: int = 0,
        open_orders_risk: float = 0.0,
        completed_wins: int = 0,
        completed_losses: int = 0,
        completed_pnl: float = 0.0,
        arb_count: int = 0,
        arb_pnl: float = 0.0,
        active_positions: int = 0,
        active_value: float = 0.0,
        active_expected_profit: float = 0.0,
        balance: float = 0.0,
    ) -> str:
        """Format the pinned summary message."""
        now = datetime.now().strftime("%H:%M:%S")
        
        # Total realized P&L
        total_realized = completed_pnl + arb_pnl
        
        # PnL emoji
        pnl_emoji = "🟢" if total_realized >= 0 else "🔴"
        expected_emoji = "🟢" if active_expected_profit >= 0 else "🔴"
        
        # Completed win rate
        total_completed = completed_wins + completed_losses
        win_rate = (completed_wins / total_completed * 100) if total_completed > 0 else 0
        
        lines = [
            f"📊 <b>POLYMM STATUS</b>",
            f"<i>Updated: {now}</i>",
            f"",
            f"<b>💰 P&L</b>",
            f"  {pnl_emoji} Realized: <b>${total_realized:+.2f}</b>",
            f"  {expected_emoji} Expected: ${active_expected_profit:+.2f}",
            f"",
            f"<b>📋 ORDERS</b>",
            f"  Open: {open_orders_count} (${open_orders_risk:.2f} at risk)",
            f"",
            f"<b>📦 POSITIONS</b>",
            f"  Completed: {completed_wins}W / {completed_losses}L ({win_rate:.0f}%) → ${completed_pnl:+.2f}",
            f"  Arbitrages: {arb_count} → ${arb_pnl:+.2f}",
            f"  Active: {active_positions} (${active_value:.2f})",
            f"",
            f"<b>💵 Balance: ${balance:.2f}</b>",
        ]
        
        return "\n".join(lines)
    
    async def init_pinned_summary(
        self,
        open_orders_count: int = 0,
        open_orders_risk: float = 0.0,
        completed_wins: int = 0,
        completed_losses: int = 0,
        completed_pnl: float = 0.0,
        arb_count: int = 0,
        arb_pnl: float = 0.0,
        active_positions: int = 0,
        active_value: float = 0.0,
        active_expected_profit: float = 0.0,
        balance: float = 0.0,
    ):
        """Create the initial summary message."""
        text = self._format_summary(
            open_orders_count=open_orders_count,
            open_orders_risk=open_orders_risk,
            completed_wins=completed_wins,
            completed_losses=completed_losses,
            completed_pnl=completed_pnl,
            arb_count=arb_count,
            arb_pnl=arb_pnl,
            active_positions=active_positions,
            active_value=active_value,
            active_expected_profit=active_expected_profit,
            balance=balance,
        )
        
        message_id = await self.send(text)
        if message_id:
            self._summary_message_id = message_id
            self._last_summary_update = datetime.now()
            print(f"📊 Summary message created (id: {message_id})")
    
    async def update_pinned_summary(
        self,
        open_orders_count: int = 0,
        open_orders_risk: float = 0.0,
        completed_wins: int = 0,
        completed_losses: int = 0,
        completed_pnl: float = 0.0,
        arb_count: int = 0,
        arb_pnl: float = 0.0,
        active_positions: int = 0,
        active_value: float = 0.0,
        active_expected_profit: float = 0.0,
        balance: float = 0.0,
    ):
        """Update the pinned summary message with current stats."""
        if not self._summary_message_id:
            # No pinned message yet - create one
            await self.init_pinned_summary(
                open_orders_count=open_orders_count,
                open_orders_risk=open_orders_risk,
                completed_wins=completed_wins,
                completed_losses=completed_losses,
                completed_pnl=completed_pnl,
                arb_count=arb_count,
                arb_pnl=arb_pnl,
                active_positions=active_positions,
                active_value=active_value,
                active_expected_profit=active_expected_profit,
                balance=balance,
            )
            return
        
        text = self._format_summary(
            open_orders_count=open_orders_count,
            open_orders_risk=open_orders_risk,
            completed_wins=completed_wins,
            completed_losses=completed_losses,
            completed_pnl=completed_pnl,
            arb_count=arb_count,
            arb_pnl=arb_pnl,
            active_positions=active_positions,
            active_value=active_value,
            active_expected_profit=active_expected_profit,
            balance=balance,
        )
        
        if await self.edit_message(self._summary_message_id, text):
            self._last_summary_update = datetime.now()


# Singleton instance
_alerts: Optional[TelegramAlerts] = None


def get_alerts() -> TelegramAlerts:
    """Get or create the global alerts instance."""
    global _alerts
    if _alerts is None:
        _alerts = TelegramAlerts()
    return _alerts


async def demo():
    """Test Telegram alerts."""
    alerts = TelegramAlerts()
    
    if not alerts._enabled:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to test")
        return
    
    await asyncio.sleep(1)
    
    await alerts.on_entry_placed(
        match="Team Alpha vs Team Beta",
        team="Team Alpha",
        price=0.26,
        shares=5,
        edge=0.24,
    )
    await asyncio.sleep(1)
    
    await alerts.on_entry_fill(
        match="Team Alpha vs Team Beta",
        team="Team Alpha",
        price=0.26,
        shares=5,
    )
    
    print("✅ Test alerts sent!")


if __name__ == "__main__":
    asyncio.run(demo())
