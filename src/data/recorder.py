"""
DataRecorder - Records bot activity to Supabase for analysis.

Tables:
- filled_orders: All filled orders with context
- order_history: Order lifecycle tracking
- arbitrages: Completed arb records
- fair_value_log: Odds history snapshots
"""
import os
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()


@dataclass
class FilledOrderRecord:
    """Record of a filled order."""
    order_id: str
    token_id: str
    condition_id: Optional[str]
    
    # Match context
    match_id: Optional[str]
    game: Optional[str]
    team1: Optional[str]
    team2: Optional[str]
    market_type: Optional[str]  # esports, esports_live, weather, stock, rugby
    
    # Order details
    side: str  # 'entry' or 'hedge'
    team: str  # which team we bet on
    price: float
    shares: float
    
    # Timing
    placed_at: Optional[datetime]
    filled_at: datetime
    time_to_fill_seconds: Optional[int]
    
    # Fair value context
    fair_value_at_fill: Optional[float]
    edge_at_fill: Optional[float]
    raw_odds: Optional[Dict[str, Any]]  # per-bookie odds snapshot
    

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for Supabase insert."""
        data = {
            "order_id": self.order_id,
            "token_id": self.token_id,
            "condition_id": self.condition_id,
            "match_id": self.match_id,
            "game": self.game,
            "team1": self.team1,
            "team2": self.team2,
            "market_type": self.market_type,
            "side": self.side,
            "team": self.team,
            "price": self.price,
            "shares": self.shares,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "time_to_fill_seconds": self.time_to_fill_seconds,
            "fair_value_at_fill": self.fair_value_at_fill,
            "edge_at_fill": self.edge_at_fill,
            "raw_odds": json.dumps(self.raw_odds) if self.raw_odds else None,
        }
        if self.placed_at:
            data["placed_at"] = self.placed_at.isoformat()
        return data


class DataRecorder:
    """
    Records bot activity to Supabase for analysis.
    
    Usage:
        recorder = DataRecorder()
        await recorder.record_filled_order(...)
    """
    
    def __init__(
        self,
        url: Optional[str] = None,
        key: Optional[str] = None,
    ):
        self.url = url or os.getenv("SUPABASE_URL")
        self.key = key or os.getenv("SUPABASE_SERVICE_KEY")
        
        self._client: Optional[Client] = None
        self._enabled = bool(self.url and self.key)
        
        if not self._enabled:
            print("⚠️ DataRecorder disabled (missing SUPABASE_URL or SUPABASE_SERVICE_KEY)")
    
    @property
    def client(self) -> Client:
        """Lazy init Supabase client."""
        if self._client is None and self._enabled:
            self._client = create_client(self.url, self.key)
        return self._client
    
    # =========================================================================
    # FILLED ORDERS
    # =========================================================================
    
    async def record_filled_order(
        self,
        order_id: str,
        token_id: str,
        side: str,  # 'entry' or 'hedge'
        team: str,
        price: float,
        shares: float,
        condition_id: Optional[str] = None,
        match_id: Optional[str] = None,
        game: Optional[str] = None,
        team1: Optional[str] = None,
        team2: Optional[str] = None,
        market_type: Optional[str] = None,
        placed_at: Optional[datetime] = None,
        fair_value: Optional[float] = None,
        raw_odds: Optional[Dict[str, Any]] = None,
        arb_id: Optional[str] = None,
        spread_at_fill: Optional[float] = None,
        trading_deadline: Optional[datetime] = None,
    ) -> Optional[str]:
        """
        Record a filled order.
        
        Args:
            order_id: Polymarket order ID
            token_id: Token ID
            side: 'entry' or 'hedge'
            team: Team name we bet on
            price: Fill price
            shares: Number of shares
            condition_id: Polymarket condition ID
            match_id: Our match identifier
            game: Game type (cs2, dota2, lol, etc.)
            team1, team2: Match teams
            market_type: Market type (esports, esports_live, weather, stock, rugby)
            placed_at: When order was placed (for time_to_fill calc)
            fair_value: Fair probability from bookmakers at fill time
            raw_odds: Dict of per-bookie odds snapshot
            arb_id: Link to arbitrage record if part of arb
            
        Returns:
            Record ID if successful, None otherwise
        """
        if not self._enabled:
            return None
        
        try:
            filled_at = datetime.now(timezone.utc)
            
            # If placed_at not provided, try to look it up from order_history
            if not placed_at and order_id:
                try:
                    result = (
                        self.client.table("order_history")
                        .select("placed_at")
                        .eq("order_id", order_id)
                        .limit(1)
                        .execute()
                    )
                    if result.data and result.data[0].get("placed_at"):
                        placed_at = datetime.fromisoformat(
                            result.data[0]["placed_at"].replace("Z", "+00:00")
                        )
                except Exception:
                    pass  # Silently ignore - placed_at remains None
            
            # Calculate time to fill
            time_to_fill = None
            if placed_at:
                time_to_fill = int((filled_at - placed_at).total_seconds())
            
            # Calculate edge
            edge = None
            if fair_value is not None:
                edge = fair_value - price
            
            record = {
                "order_id": order_id,
                "token_id": token_id,
                "condition_id": condition_id,
                "match_id": match_id,
                "game": game,
                "team1": team1,
                "team2": team2,
                "market_type": market_type,
                "side": side,
                "team": team,
                "price": price,
                "shares": shares,
                "filled_at": filled_at.isoformat(),
                "time_to_fill_seconds": time_to_fill,
                "fair_value_at_fill": fair_value,
                "edge_at_fill": edge,
                "raw_odds": raw_odds,
            }
            
            if placed_at:
                record["placed_at"] = placed_at.isoformat()
            if arb_id:
                record["arb_id"] = arb_id
            if spread_at_fill is not None:
                record["spread_at_fill"] = spread_at_fill
            if trading_deadline is not None:
                dl = trading_deadline if trading_deadline.tzinfo else trading_deadline.replace(tzinfo=timezone.utc)
                record["trading_deadline"] = dl.isoformat()
            
            result = (
                self.client.table("filled_orders")
                .upsert(record, on_conflict="order_id")
                .execute()
            )
            
            if result.data:
                return result.data[0].get("id")
            return None
            
        except Exception as e:
            print(f"⚠️ Failed to record filled order: {e}")
            return None
    
    # =========================================================================
    # ARBITRAGES
    # =========================================================================
    
    async def record_arbitrage(
        self,
        match_id: Optional[str],
        condition_id: Optional[str],
        game: Optional[str],
        team1: str,
        team2: str,
        entry_order_id: str,
        entry_team: str,
        entry_price: float,
        entry_shares: float,
        entry_filled_at: Optional[datetime],
        hedge_order_id: str,
        hedge_team: str,
        hedge_price: float,
        hedge_shares: float,
        hedge_filled_at: Optional[datetime],
    ) -> Optional[str]:
        """
        Record a completed arbitrage (both legs filled).
        
        Returns:
            Arb record ID if successful
        """
        if not self._enabled:
            return None
        
        try:
            entry_cost = entry_price * entry_shares
            hedge_cost = hedge_price * hedge_shares
            total_cost = entry_cost + hedge_cost
            
            # Calculate locked profit
            # Guaranteed return is min(entry_shares, hedge_shares) * $1
            arb_shares = min(entry_shares, hedge_shares)
            locked_profit = arb_shares - total_cost
            locked_profit_pct = (locked_profit / total_cost) * 100 if total_cost > 0 else 0
            
            record = {
                "match_id": match_id,
                "condition_id": condition_id,
                "game": game,
                "team1": team1,
                "team2": team2,
                "entry_order_id": entry_order_id,
                "entry_team": entry_team,
                "entry_price": entry_price,
                "entry_shares": entry_shares,
                "entry_cost": entry_cost,
                "entry_filled_at": entry_filled_at.isoformat() if entry_filled_at else None,
                "hedge_order_id": hedge_order_id,
                "hedge_team": hedge_team,
                "hedge_price": hedge_price,
                "hedge_shares": hedge_shares,
                "hedge_cost": hedge_cost,
                "hedge_filled_at": hedge_filled_at.isoformat() if hedge_filled_at else None,
                "total_cost": total_cost,
                "locked_profit": locked_profit,
                "locked_profit_pct": locked_profit_pct,
            }
            
            result = (
                self.client.table("arbitrages")
                .insert(record)
                .execute()
            )
            
            if result.data:
                arb_id = result.data[0].get("id")
                
                # Update the filled_orders with arb_id link
                self.client.table("filled_orders").update({"arb_id": arb_id}).eq("order_id", entry_order_id).execute()
                self.client.table("filled_orders").update({"arb_id": arb_id}).eq("order_id", hedge_order_id).execute()
                
                return arb_id
            return None
            
        except Exception as e:
            print(f"⚠️ Failed to record arbitrage: {e}")
            return None
    
    # =========================================================================
    # ORDER HISTORY (Lifecycle tracking)
    # =========================================================================
    
    async def record_order_placed(
        self,
        order_id: str,
        token_id: str,
        condition_id: Optional[str],
        match_id: Optional[str],
        team: str,
        price: float,
        fair_value: Optional[float] = None,
    ) -> Optional[str]:
        """Record when an order is placed."""
        if not self._enabled:
            return None
        
        try:
            edge = (fair_value - price) if fair_value else None
            
            record = {
                "order_id": order_id,
                "token_id": token_id,
                "condition_id": condition_id,
                "match_id": match_id,
                "team": team,
                "initial_price": price,
                "final_price": price,
                "adjustment_count": 0,
                "price_history": [{"price": price, "at": datetime.now(timezone.utc).isoformat(), "reason": "placed"}],
                "fair_value_at_place": fair_value,
                "edge_at_place": edge,
                "final_status": "OPEN",
            }
            
            result = (
                self.client.table("order_history")
                .insert(record)
                .execute()
            )
            
            return result.data[0].get("id") if result.data else None
            
        except Exception as e:
            print(f"⚠️ Failed to record order placed: {e}")
            return None
    
    async def record_order_adjustment(
        self,
        order_id: str,
        new_price: float,
        reason: str = "outbid",
    ) -> bool:
        """Record a price adjustment (retries once on transient errors)."""
        if not self._enabled:
            return False
        
        import asyncio
        
        for attempt in range(2):
            try:
                # Get current record - don't use single() which throws on 0 rows
                result = (
                    self.client.table("order_history")
                    .select("*")
                    .eq("order_id", order_id)
                    .execute()
                )
                
                if not result.data or len(result.data) == 0:
                    # Order not in history - might be a hydrated order from previous session
                    # Just skip recording the adjustment silently
                    return False
                
                current_data = result.data[0]
                history = current_data.get("price_history", []) or []
                history.append({
                    "price": new_price,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "reason": reason,
                })
                # Cap history to last 50 entries to avoid payload bloat
                if len(history) > 50:
                    history = history[-50:]
                
                update_result = (
                    self.client.table("order_history")
                    .update({
                        "final_price": new_price,
                        "adjustment_count": current_data.get("adjustment_count", 0) + 1,
                        "price_history": history,
                    })
                    .eq("order_id", order_id)
                    .execute()
                )
                
                return bool(update_result.data)
                
            except Exception as e:
                if attempt == 0:
                    await asyncio.sleep(2)  # Retry after 2s for transient Cloudflare/Supabase errors
                    continue
                print(f"⚠️ Failed to record order adjustment: {e}")
                return False
    
    async def record_order_final_status(
        self,
        order_id: str,
        status: str,  # FILLED, CANCELLED, EXPIRED
        cancel_reason: Optional[str] = None,
        best_bid_at_cancel: Optional[float] = None,
        fair_value_at_cancel: Optional[float] = None,
    ) -> bool:
        """Record final order status."""
        if not self._enabled:
            return False
        
        try:
            now = datetime.now(timezone.utc).isoformat()
            
            update = {
                "final_status": status,
            }
            
            if status == "FILLED":
                update["filled_at"] = now
            elif status == "CANCELLED":
                update["cancelled_at"] = now
                update["cancel_reason"] = cancel_reason
                update["best_bid_at_cancel"] = best_bid_at_cancel
                update["fair_value_at_cancel"] = fair_value_at_cancel
            
            result = (
                self.client.table("order_history")
                .update(update)
                .eq("order_id", order_id)
                .execute()
            )
            
            return bool(result.data)
            
        except Exception as e:
            print(f"⚠️ Failed to record order final status: {e}")
            return False
    
    # =========================================================================
    # FAIR VALUE LOG
    # =========================================================================
    
    async def log_fair_values(
        self,
        matches: List[Dict[str, Any]],
    ) -> int:
        """
        Log current fair values for analysis.
        
        Args:
            matches: List of match dicts from OddsService
            
        Returns:
            Number of records inserted
        """
        if not self._enabled:
            return 0
        
        try:
            records = []
            for m in matches:
                record = {
                    "match_id": m.get("match_id"),
                    "team1": m.get("team1"),
                    "team2": m.get("team2"),
                    "game": m.get("game"),
                    "fair_prob1": m.get("fair_prob1"),
                    "fair_prob2": m.get("fair_prob2"),
                    "source_count": len(m.get("sources", [])),
                    # recorded_at is auto-set by DEFAULT NOW() in DB
                }

                records.append(record)
            
            if records:
                result = (
                    self.client.table("fair_value_log")
                    .insert(records)
                    .execute()
                )
                return len(result.data) if result.data else 0
            
            return 0
            
        except Exception as e:
            print(f"⚠️ Failed to log fair values: {e}")
            return 0
    
    # =========================================================================
    # RETENTION & CLEANUP
    # =========================================================================
    
    async def cleanup_old_data(
        self,
        order_history_days: int = 90,
        fair_value_log_days: int = 30,
    ) -> Dict[str, int]:
        """
        Delete old records based on retention policies.
        
        Retention policies:
        - filled_orders: Forever (no cleanup)
        - arbitrages: Forever (no cleanup)
        - order_history: 90 days (configurable)
        - fair_value_log: 30 days (configurable)
        
        Returns:
            Dict with count of deleted records per table
        """
        if not self._enabled:
            return {}
        
        deleted = {}
        
        # Calculate cutoff dates
        now = datetime.now(timezone.utc)
        order_history_cutoff = (now - timedelta(days=order_history_days)).isoformat()
        fair_value_cutoff = (now - timedelta(days=fair_value_log_days)).isoformat()
        
        # Clean order_history (90 days)
        try:
            result = (
                self.client.table("order_history")
                .delete()
                .lt("placed_at", order_history_cutoff)
                .execute()
            )
            deleted["order_history"] = len(result.data) if result.data else 0
        except Exception as e:
            print(f"⚠️ Failed to cleanup order_history: {e}")
            deleted["order_history"] = 0
        
        # Clean fair_value_log (30 days) - uses recorded_at column
        try:
            result = (
                self.client.table("fair_value_log")
                .delete()
                .lt("recorded_at", fair_value_cutoff)
                .execute()
            )
            deleted["fair_value_log"] = len(result.data) if result.data else 0
        except Exception as e:
            print(f"⚠️ Failed to cleanup fair_value_log: {e}")
            deleted["fair_value_log"] = 0
        
        total = sum(deleted.values())
        if total > 0:
            print(f"🧹 Cleanup: Deleted {deleted.get('order_history', 0)} order_history, "
                  f"{deleted.get('fair_value_log', 0)} fair_value_log records")
        
        return deleted
    
    async def get_storage_stats(self) -> Dict[str, Any]:
        """
        Get storage statistics for monitoring.
        
        Returns:
            Dict with record counts and date ranges per table
        """
        if not self._enabled:
            return {}
        
        stats = {}
        
        tables = ["filled_orders", "order_history", "fair_value_log", "arbitrages"]
        
        for table in tables:
            try:
                # Get count
                result = (
                    self.client.table(table)
                    .select("id", count="exact")
                    .limit(1)
                    .execute()
                )
                count = result.count if hasattr(result, 'count') else len(result.data) if result.data else 0
                
                stats[table] = {"count": count}
            except Exception as e:
                stats[table] = {"count": 0, "error": str(e)}
        
        return stats


# Singleton instance
_recorder: Optional[DataRecorder] = None


def get_recorder() -> DataRecorder:
    """Get or create the global DataRecorder instance."""
    global _recorder
    if _recorder is None:
        _recorder = DataRecorder()
    return _recorder
