"""
Supabase client for storing and retrieving esports odds.
"""
import os
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, asdict
from supabase import create_client, Client


@dataclass
class EsportsOddsRecord:
    """A single odds record for storage."""
    match_id: str
    source: str  # 'crossbet' or 'egamersworld'
    game: str  # 'cs2' or 'dota2'
    team1: str
    team2: str
    odds1: float
    odds2: float
    fair_prob1: float
    fair_prob2: float
    fair_odds1: float
    fair_odds2: float
    avg_vig: float
    tournament: str
    format: str = ""
    is_live: bool = False
    match_time: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for Supabase insert."""
        return {
            "match_id": self.match_id,
            "source": self.source,
            "game": self.game,
            "team1": self.team1,
            "team2": self.team2,
            "odds1": self.odds1,
            "odds2": self.odds2,
            "fair_prob1": self.fair_prob1,
            "fair_prob2": self.fair_prob2,
            "fair_odds1": self.fair_odds1,
            "fair_odds2": self.fair_odds2,
            "avg_vig": self.avg_vig,
            "tournament": self.tournament,
            "format": self.format,
            "is_live": self.is_live,
            "match_time": self.match_time,
        }


class SupabaseClient:
    """Client for Supabase operations."""
    
    def __init__(
        self,
        url: Optional[str] = None,
        key: Optional[str] = None,
    ):
        """
        Initialize Supabase client.
        
        Args:
            url: Supabase URL (or from SUPABASE_URL env var)
            key: Supabase service key (or from SUPABASE_SERVICE_KEY env var)
        """
        self.url = url or os.getenv("SUPABASE_URL")
        self.key = key or os.getenv("SUPABASE_SERVICE_KEY")
        
        if not self.url or not self.key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        
        self.client: Client = create_client(self.url, self.key)
    
    def insert_odds(self, records: List[EsportsOddsRecord]) -> int:
        """
        Insert multiple odds records.
        
        Args:
            records: List of EsportsOddsRecord objects
            
        Returns:
            Number of records inserted
        """
        if not records:
            return 0
        
        data = [r.to_dict() for r in records]
        result = self.client.table("esports_odds").insert(data).execute()
        return len(result.data) if result.data else 0
    
    def insert_odds_batch(self, matches: List[Dict[str, Any]], source: str) -> int:
        """
        Insert odds from a batch of match dictionaries (from scrapers).
        
        Args:
            matches: List of match dicts from scrapers
            source: 'crossbet' or 'egamersworld'
            
        Returns:
            Number of records inserted
        """
        records = []
        for m in matches:
            # Generate a match ID from team names
            team1 = m.get("team1", "")
            team2 = m.get("team2", "")
            match_id = f"{team1.lower()[:10]}_{team2.lower()[:10]}"
            
            record = EsportsOddsRecord(
                match_id=match_id,
                source=source,
                game=m.get("game", ""),
                team1=team1,
                team2=team2,
                odds1=float(m.get("best_odds1") or m.get("odds1") or 0),
                odds2=float(m.get("best_odds2") or m.get("odds2") or 0),
                fair_prob1=float(m.get("avg_fair_prob1") or m.get("fair_prob1") or 0),
                fair_prob2=float(m.get("avg_fair_prob2") or m.get("fair_prob2") or 0),
                fair_odds1=float(m.get("avg_fair_odds1") or m.get("fair_odds1") or 0),
                fair_odds2=float(m.get("avg_fair_odds2") or m.get("fair_odds2") or 0),
                avg_vig=float(m.get("vig_percent") or m.get("vig") or 0),
                tournament=m.get("tournament", ""),
                format=m.get("format", ""),
                is_live=m.get("isLive", False),
                match_time=m.get("match_time") or m.get("date", ""),
            )
            records.append(record)
        
        return self.insert_odds(records)
    
    def get_latest_odds(
        self,
        game: Optional[str] = None,
        live_only: bool = False,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Get the most recent odds records.
        
        Handles Supabase's 1000 record limit by paginating if needed.
        
        Args:
            game: Filter by game ('cs2' or 'dota2')
            live_only: Only return live matches
            limit: Maximum number of records (can exceed 1000)
            
        Returns:
            List of odds records
        """
        all_records = []
        page_size = min(limit, 1000)  # Supabase max per query
        offset = 0
        
        while len(all_records) < limit:
            query = self.client.table("esports_odds").select("*")
            
            if game:
                query = query.eq("game", game)
            if live_only:
                query = query.eq("is_live", True)
            
            remaining = limit - len(all_records)
            fetch_count = min(page_size, remaining)
            
            result = query.order("scraped_at", desc=True).range(offset, offset + fetch_count - 1).execute()
            
            if not result.data:
                break  # No more records
            
            all_records.extend(result.data)
            
            if len(result.data) < fetch_count:
                break  # Got fewer than requested, no more pages
            
            offset += fetch_count
        
        return all_records
    
    def get_latest_sports_odds(
        self,
        sport: Optional[str] = None,
        live_only: bool = False,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """
        Get the most recent 3-way sports odds records from sports_odds table.
        
        Args:
            sport: Filter by sport ('rugby', 'soccer')
            live_only: Only return live matches
            limit: Maximum number of records
            
        Returns:
            List of sports odds records with 3-way odds
        """
        all_records = []
        page_size = min(limit, 1000)
        offset = 0
        
        while len(all_records) < limit:
            query = self.client.table("sports_odds").select("*")
            
            if sport:
                query = query.eq("sport", sport)
            if live_only:
                query = query.eq("is_live", True)
            
            remaining = limit - len(all_records)
            fetch_count = min(page_size, remaining)
            
            result = query.order("scraped_at", desc=True).range(offset, offset + fetch_count - 1).execute()
            
            if not result.data:
                break
            
            all_records.extend(result.data)
            
            if len(result.data) < fetch_count:
                break
            
            offset += fetch_count
        
        return all_records
    
    def get_latest_sports_odds_v2(
        self,
        market_types: Optional[List[str]] = None,
        limit: int = 100000,
    ) -> List[Dict[str, Any]]:
        """
        Get multi-market odds from sports_odds_v2 table (the-odds-api data).
        
        Args:
            market_types: Filter by market types (e.g., ["spreads", "totals"]).
                         None = all types.
            limit: Maximum number of records
            
        Returns:
            List of odds records with market_type, line, fair probs, etc.
        """
        all_records = []
        page_size = min(limit, 1000)
        offset = 0
        
        while len(all_records) < limit:
            query = self.client.table("sports_odds_v2").select("*")
            
            if market_types:
                query = query.in_("market_type", market_types)
            
            remaining = limit - len(all_records)
            fetch_count = min(page_size, remaining)
            
            result = query.order("scraped_at", desc=True).range(offset, offset + fetch_count - 1).execute()
            
            if not result.data:
                break
            
            all_records.extend(result.data)
            
            if len(result.data) < fetch_count:
                break
            
            offset += fetch_count
        
        return all_records
    
    def get_latest_by_match(
        self,
        match_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get the most recent odds for a specific match.
        
        Args:
            match_id: The match identifier
            
        Returns:
            Most recent odds record or None
        """
        result = (
            self.client.table("esports_odds")
            .select("*")
            .eq("match_id", match_id)
            .order("scraped_at", desc=True)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    
    def get_odds_history(
        self,
        match_id: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Get historical odds for a match (for analysis).
        
        Args:
            match_id: The match identifier
            limit: Maximum number of records
            
        Returns:
            List of odds records ordered by time
        """
        result = (
            self.client.table("esports_odds")
            .select("*")
            .eq("match_id", match_id)
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []

    # =========================================================================
    # LIVE ODDS METHODS
    # =========================================================================
    
    def upsert_live_odds(self, records: List[Dict[str, Any]]) -> int:
        """
        Upsert live odds records (update if exists, insert if not).
        
        Uses match_id + source as unique key.
        
        Args:
            records: List of live odds dicts
            
        Returns:
            Number of records upserted
        """
        if not records:
            return 0
        
        # Ensure required fields
        for r in records:
            if 'scraped_at' not in r:
                r['scraped_at'] = datetime.utcnow().isoformat()
        
        try:
            result = (
                self.client.table("esports_odds_live")
                .upsert(records, on_conflict="match_id,source")
                .execute()
            )
            return len(result.data) if result.data else 0
        except Exception as e:
            print(f"⚠️ Live odds upsert error: {e}")
            return 0
    
    def get_live_odds(
        self,
        game: Optional[str] = None,
        max_age_seconds: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        Get fresh live odds (within max_age_seconds).
        
        Args:
            game: Filter by game (cs2, dota2, lol)
            max_age_seconds: Max age of odds to consider fresh
            
        Returns:
            List of live odds records
        """
        from datetime import timedelta
        
        cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
        
        query = (
            self.client.table("esports_odds_live")
            .select("*")
            .gte("scraped_at", cutoff.isoformat())
        )
        
        if game:
            query = query.eq("game", game)
        
        result = query.order("scraped_at", desc=True).execute()
        return result.data or []
    
    def delete_stale_live_odds(self, max_age_seconds: int = 300) -> int:
        """
        Delete live odds older than max_age_seconds.
        
        Keeps the table clean after matches end.
        
        Args:
            max_age_seconds: Delete records older than this
            
        Returns:
            Number of records deleted
        """
        from datetime import timedelta
        
        cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
        
        try:
            result = (
                self.client.table("esports_odds_live")
                .delete()
                .lt("scraped_at", cutoff.isoformat())
                .execute()
            )
            return len(result.data) if result.data else 0
        except Exception as e:
            print(f"⚠️ Delete stale odds error: {e}")
            return 0
    
    def get_all_live_match_ids(self) -> List[str]:
        """
        Get all unique match_ids from esports_odds_live table.
        
        No freshness filter - if a match is in this table, it's considered live.
        This is the ground truth for live match detection.
        
        Returns:
            List of match_id strings
        """
        try:
            result = (
                self.client.table("esports_odds_live")
                .select("match_id")
                .execute()
            )
            # Extract unique match_ids
            if result.data:
                return list({r['match_id'] for r in result.data if r.get('match_id')})
            return []
        except Exception as e:
            print(f"⚠️ Get live match IDs error: {e}")
            return []
    
    # =========================================================================
    # POSITION TRACKING METHODS
    # =========================================================================
    
    def upsert_position(self, position: Dict[str, Any]) -> bool:
        """
        Upsert a position record.
        
        Args:
            position: Position dict with position_id
            
        Returns:
            True if successful
        """
        try:
            position['updated_at'] = datetime.utcnow().isoformat()
            result = (
                self.client.table("positions")
                .upsert(position, on_conflict="position_id")
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            print(f"⚠️ Position upsert error: {e}")
            return False
    
    def get_open_positions(self, bot_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all open positions (not settled/cancelled).
        
        Args:
            bot_type: Filter by 'prematch' or 'live'
            
        Returns:
            List of position records
        """
        query = (
            self.client.table("positions")
            .select("*")
            .not_.in_("state", ["settled", "cancelled"])
        )
        
        if bot_type:
            query = query.eq("bot_type", bot_type)
        
        result = query.order("created_at", desc=True).execute()
        return result.data or []
    
    def get_position_by_token(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        Get position by entry token ID.
        
        Args:
            token_id: Polymarket token ID
            
        Returns:
            Position record or None
        """
        result = (
            self.client.table("positions")
            .select("*")
            .eq("entry_token_id", token_id)
            .not_.in_("state", ["settled", "cancelled"])
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    
    def update_position_state(
        self,
        position_id: str,
        state: str,
        **kwargs
    ) -> bool:
        """
        Update position state and optional fields.
        
        Args:
            position_id: Position ID
            state: New state
            **kwargs: Additional fields to update
            
        Returns:
            True if successful
        """
        try:
            data = {"state": state, "updated_at": datetime.utcnow().isoformat()}
            data.update(kwargs)
            
            result = (
                self.client.table("positions")
                .update(data)
                .eq("position_id", position_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            print(f"⚠️ Position update error: {e}")
            return False


# Singleton instance
_client: Optional[SupabaseClient] = None


def get_supabase_client() -> SupabaseClient:
    """Get or create the global Supabase client."""
    global _client
    if _client is None:
        _client = SupabaseClient()
    return _client
