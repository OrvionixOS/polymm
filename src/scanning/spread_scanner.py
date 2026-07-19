"""
Spread Scanner - Detects spread opportunities in weather markets.

Scans weather markets for bins with spread >= min_spread (default 15¢).
"""
import asyncio
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional
from dataclasses import dataclass

from src.polymarket.weather_client import WeatherMarketClient, WeatherEvent, WeatherMarket
from src.core.match_id import is_will_win_market


@dataclass
class SpreadOpportunity:
    """A detected spread opportunity."""
    event_id: str
    event_title: str
    city: str
    date_str: str
    market_id: str
    condition_id: str
    question: str
    bin_label: str  # e.g., "50-51°F"
    token_id: str
    no_token_id: str
    best_bid: float
    best_ask: float
    spread: float
    spread_cents: float
    mid_price: Optional[float]
    volume: float
    liquidity: float
    market_type: str = "weather"  # "weather", "stock", "sports", or "tennis"
    game_start_time: Optional[datetime] = None  # Actual game start (for live detection)
    end_date: Optional[datetime] = None  # Market close/resolution deadline
    
    @property
    def suggested_bid(self) -> float:
        """Suggested bid price (1¢ above best bid)."""
        return min(self.best_bid + 0.01, self.best_ask - 0.01)


class SpreadScanner:
    """Scans weather markets for spread opportunities."""
    
    def __init__(
        self,
        weather_client: WeatherMarketClient,
        min_spread: float = 0.15,  # 15 cents
        min_probability: float = 0.05,  # Skip bins < 5%
        max_probability: float = 0.95,  # Skip bins > 95%
    ):
        self.weather_client = weather_client
        self.min_spread = min_spread
        self.min_probability = min_probability
        self.max_probability = max_probability
        
        # Track active opportunities by token_id
        self._active_opportunities: Dict[str, SpreadOpportunity] = {}
    
    async def scan(self) -> List[SpreadOpportunity]:
        """Scan all active weather markets for spread opportunities."""
        opportunities = []
        
        # Get today's and tomorrow's events
        events = await self.weather_client.get_all_active_events()
        
        for event in events:
            event_opps = self._scan_event(event)
            opportunities.extend(event_opps)
        
        # Update internal tracking
        self._active_opportunities = {opp.token_id: opp for opp in opportunities}
        
        return opportunities
    
    def _scan_event(self, event: WeatherEvent) -> List[SpreadOpportunity]:
        """Scan a single event for opportunities."""
        opportunities = []
        
        for market in event.markets:
            # Skip inactive markets
            if not market.active or not market.accepting_orders:
                continue
            
            # Skip Draw markets for rugby/cricket/football events — only trade "Will X win?" markets
            if event.market_type in ("rugby", "cricket", "football"):
                q_lower = market.question.lower()
                if "draw" in q_lower:
                    continue
            
            # Skip near-certain outcomes (< 5% or > 95%)
            if market.mid_price:
                if market.mid_price < self.min_probability or market.mid_price > self.max_probability:
                    continue
            
            # Skip markets with invalid bid/ask data
            # Must have actual bid > 0 and ask < 1 to have a meaningful spread
            if market.best_bid <= 0 or market.best_ask >= 1:
                continue
            
            # Check spread threshold
            if market.spread >= self.min_spread:
                opp = SpreadOpportunity(
                    event_id=event.event_id,
                    event_title=event.title,
                    city=event.city,
                    date_str=event.date_str,
                    market_id=market.market_id,
                    condition_id=market.condition_id,
                    question=market.question,
                    bin_label=market.group_item,
                    token_id=market.token_id,
                    no_token_id=market.no_token_id,
                    best_bid=market.best_bid,
                    best_ask=market.best_ask,
                    spread=market.spread,
                    spread_cents=market.spread * 100,
                    mid_price=market.mid_price,
                    volume=market.volume,
                    liquidity=market.liquidity,
                    market_type=event.market_type,
                    game_start_time=event.game_start_time,
                    end_date=event.end_date,
                )
                opportunities.append(opp)
        
        return opportunities
    
    def should_cancel(self, token_id: str, current_spread: float) -> bool:
        """Check if an order should be cancelled due to spread collapsing."""
        return current_spread < self.min_spread
    
    def get_active_opportunity(self, token_id: str) -> Optional[SpreadOpportunity]:
        """Get active opportunity by token ID."""
        return self._active_opportunities.get(token_id)
    
    def is_opportunity_active(self, token_id: str) -> bool:
        """Check if a token still has an active opportunity."""
        return token_id in self._active_opportunities
    
    async def refresh_opportunity(self, token_id: str) -> Optional[SpreadOpportunity]:
        """Refresh data for a specific opportunity.
        
        If the token is not in the cache (e.g., hydrated from previous session),
        fetches market data directly from Gamma API and caches the result.
        """
        # Find the event containing this token
        opp = self._active_opportunities.get(token_id)
        if not opp:
            # Not in cache — fetch from Gamma API and cache
            return await self._fetch_and_cache_opportunity(token_id)
        
        # Re-fetch the event
        # For non-weather/stock markets (sports, rugby, mentions), the city/date-based
        # lookup won't work. Fall through to _fetch_and_cache_opportunity for these.
        if opp.market_type in ("weather", "stock"):
            event = await self.weather_client.get_event(opp.city, self._parse_date(opp.date_str))
            if event:
                # Find the updated market
                for market in event.markets:
                    if market.token_id == token_id:
                        updated_opp = SpreadOpportunity(
                            event_id=event.event_id,
                            event_title=event.title,
                            city=event.city,
                            date_str=event.date_str,
                            market_id=market.market_id,
                            condition_id=market.condition_id,
                            question=market.question,
                            bin_label=market.group_item,
                            token_id=market.token_id,
                            no_token_id=market.no_token_id,
                            best_bid=market.best_bid,
                            best_ask=market.best_ask,
                            spread=market.spread,
                            spread_cents=market.spread * 100,
                            mid_price=market.mid_price,
                            volume=market.volume,
                            liquidity=market.liquidity,
                        )
                        self._active_opportunities[token_id] = updated_opp
                        return updated_opp
                return None  # Token not found in event
            return None  # Event not found
        
        # Non-weather/stock: refresh via Gamma API by token ID
        # Preserve existing market_type and game_start_time for sports/tennis markets
        return await self._fetch_and_cache_opportunity(
            token_id,
            override_market_type=opp.market_type,
            override_game_start_time=opp.game_start_time,
        )
    
    async def _fetch_and_cache_opportunity(self, token_id: str, override_market_type: str = None, override_game_start_time: datetime = None) -> Optional[SpreadOpportunity]:
        """Fetch opportunity data from Gamma API for a token not in the scanner cache.
        
        This handles hydrated orders from previous sessions that were never
        discovered by the scanner's scan() method.
        
        Args:
            override_market_type: If set, use this market_type instead of detecting from question.
                                 Used when refreshing existing opportunities to preserve event-level type.
            override_game_start_time: If set, preserve the original game_start_time for live detection.
        """
        market_data = await self.weather_client.get_market_by_token(token_id)
        if not market_data:
            return None
        
        best_bid = float(market_data.get("bestBid", 0) or 0)
        best_ask = float(market_data.get("bestAsk", 1) or 1)
        spread = best_ask - best_bid if best_ask > best_bid else 0.0
        
        # Extract city from question (e.g., "Will the highest temperature in Chicago be...")
        question = market_data.get("question", "")
        city = ""
        q_lower = question.lower()
        city_match = re.search(r'temperature in (.+?)\s+be\s', question, re.IGNORECASE)
        if city_match:
            city = city_match.group(1).strip().lower()
        
        # Detect market type from question content (unless overridden)
        if override_market_type:
            market_type = override_market_type
        elif "temperature" in q_lower:
            market_type = "weather"
        elif "up or down" in q_lower:
            market_type = "stock"
        elif is_will_win_market(q_lower):
            market_type = "rugby"  # "Will X win?" binary sub-markets
        elif " vs " in question or " vs. " in question:
            market_type = "sports"
        elif "what will" in q_lower and ("say" in q_lower or "said" in q_lower or "mention" in q_lower or "name" in q_lower):
            market_type = "mentions"
        else:
            market_type = "weather"  # default fallback
        
        # Extract date from question (e.g., "...on February 7?")
        date_str = ""
        date_match = re.search(r'on (\w+ \d+)\??$', question)
        if date_match:
            date_str = date_match.group(1).lower().replace(" ", "-")
        
        tokens = market_data.get("clobTokenIds", "")
        token_list = [t.strip() for t in tokens.split(",")] if tokens else []
        no_token_id = ""
        if len(token_list) == 2:
            no_token_id = token_list[1] if token_list[0] == token_id else token_list[0]
        
        # Parse game_start_time: prefer override (from cached opp), fall back to API response
        game_start_time = override_game_start_time
        if not game_start_time:
            gst_str = market_data.get("gameStartTime")
            if gst_str:
                try:
                    game_start_time = datetime.fromisoformat(gst_str.replace("Z", "+00:00"))
                except Exception:
                    pass
        
        # Parse end_date (market close/resolution deadline)
        end_date = None
        end_str = market_data.get("endDate")
        if end_str:
            try:
                end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except Exception:
                pass
        
        opp = SpreadOpportunity(
            event_id=market_data.get("conditionId", ""),
            event_title=question,
            city=city,
            date_str=date_str,
            market_id=market_data.get("id", ""),
            condition_id=market_data.get("conditionId", ""),
            question=question,
            bin_label=market_data.get("groupItemTitle", ""),
            token_id=token_id,
            no_token_id=no_token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            spread_cents=spread * 100,
            mid_price=(best_bid + best_ask) / 2 if best_bid > 0 and best_ask < 1 else None,
            volume=float(market_data.get("volume", 0) or 0),
            liquidity=float(market_data.get("liquidity", 0) or 0),
            market_type=market_type,
            game_start_time=game_start_time,
            end_date=end_date,
        )
        
        # Cache for future refreshes
        self._active_opportunities[token_id] = opp
        return opp
    
    def _parse_date(self, date_str: str) -> datetime:
        """Parse date string like 'january-29' to datetime."""
        # Assume current year
        year = datetime.now(timezone.utc).year
        
        # Handle various formats
        date_str = date_str.lower().replace("-", " ")
        
        try:
            # Try full month name
            dt = datetime.strptime(f"{date_str} {year}", "%B %d %Y")
        except ValueError:
            try:
                # Try abbreviated month
                dt = datetime.strptime(f"{date_str} {year}", "%b %d %Y")
            except ValueError:
                # Default to today
                dt = datetime.now(timezone.utc)
        
        return dt.replace(tzinfo=timezone.utc)


# CLI for testing
async def main():
    from src.polymarket.weather_client import WeatherMarketClient
    
    print("🔍 Spread Scanner - Finding Opportunities")
    print("=" * 60)
    
    async with WeatherMarketClient() as client:
        scanner = SpreadScanner(client, min_spread=0.10)  # 10¢ for testing
        opportunities = await scanner.scan()
        
        print(f"Found {len(opportunities)} opportunities with spread >= 10¢\n")
        
        # Sort by spread descending
        opportunities.sort(key=lambda x: x.spread_cents, reverse=True)
        
        for opp in opportunities[:10]:  # Top 10
            print(f"🔥 {opp.city.title()} {opp.date_str} - {opp.bin_label}")
            print(f"   Spread: {opp.spread_cents:.1f}¢ (bid ${opp.best_bid:.3f} / ask ${opp.best_ask:.3f})")
            print(f"   Suggested bid: ${opp.suggested_bid:.3f}")
            print(f"   Volume: ${opp.volume:,.0f} | Liquidity: ${opp.liquidity:,.0f}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
