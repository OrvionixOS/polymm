"""
OddsApiService - Fetches odds from the-odds-api.com and writes to Supabase.

Standalone service that runs on a 5-minute polling interval.
Fetches h2h, spreads, totals, btts, and h2h_h1 (1st half) markets for all configured sports.
Computes fair values (vig-removed) using median odds across bookmakers.
Upserts results to the sports_odds_v2 table.

Usage:
    # Run standalone (polls every 5 minutes):
    proxychains4 -q python -m src.services.odds_api_service

    # Single fetch (no loop):
    proxychains4 -q python -m src.services.odds_api_service --once
"""
import asyncio
import math
import os
import statistics
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp
from dotenv import load_dotenv

from src.core.match_id import normalize_team, make_match_id
from src.core.vig_removal import proportional_probabilities

load_dotenv()

logger = logging.getLogger(__name__)


# ── Sport key → markets configuration ──────────────────────────────────────

# the-odds-api sport keys we poll, grouped by Polymarket coverage
SPORT_CONFIGS: Dict[str, List[str]] = {
    # Football — h2h, spreads, totals, btts, h2h_h1 (1st half 3-way)
    "soccer_epl": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_spain_la_liga": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_germany_bundesliga": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_france_ligue_one": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_italy_serie_a": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_uefa_champs_league": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_uefa_europa_league": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_usa_mls": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_efl_champ": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_netherlands_eredivisie": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_mexico_ligamx": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_argentina_primera_division": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_conmebol_copa_libertadores": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_conmebol_copa_sudamericana": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_turkey_super_league": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_portugal_primeira_liga": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_brazil_campeonato": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_japan_j_league": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_australia_aleague": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_korea_kleague1": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_uefa_europa_conference_league": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_fa_cup": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_denmark_superliga": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_norway_eliteserien": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_russia_premier_league": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_africa_cup_of_nations": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_brazil_serie_b": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_china_superleague": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_chile_campeonato": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_france_coupe_de_france": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_spain_copa_del_rey": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_germany_dfb_pokal": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_saudi_arabia_pro_league": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_england_efl_cup": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_france_ligue_two": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_germany_bundesliga2": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_spl": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_italy_serie_b": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_italy_coppa_italia": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    "soccer_fifa_world_cup_qualifiers_europe": ["h2h", "spreads", "totals", "btts", "h2h_h1"],
    # Basketball — h2h, spreads, totals
    "basketball_ncaab": ["h2h", "spreads", "totals"],
    "basketball_nba": ["h2h", "spreads", "totals"],
    "basketball_nbl": ["h2h", "spreads", "totals"],
    "basketball_euroleague": ["h2h", "spreads", "totals"],
    # Hockey — h2h, spreads, totals
    "icehockey_nhl": ["h2h", "spreads", "totals"],
    "icehockey_sweden_hockey_league": ["h2h", "spreads", "totals"],
    "icehockey_ahl": ["h2h", "spreads", "totals"],
    # MMA — h2h, totals (rounds)
    "mma_mixed_martial_arts": ["h2h", "totals"],
    # Cricket — h2h only (exotic markets not on odds-api)
    "cricket_ipl": ["h2h"],
    "cricket_odi": ["h2h"],
    "cricket_international_t20": ["h2h"],
    "cricket_big_bash": ["h2h"],
    "cricket_test_match": ["h2h"],
    # Rugby — h2h only
    "rugbyunion_six_nations": ["h2h"],
}

# Map odds-api sport keys to our canonical sport names (for match_id)
# CRITICAL: These MUST match what sport_from_slug() returns for the same sport,
# since sport_from_slug determines BotState match_ids during hydration.
# sport_from_slug returns "basketball" for ALL basketball (NBA, NBL, Euroleague, etc.)
SPORT_KEY_TO_GAME: Dict[str, str] = {}
for key in SPORT_CONFIGS:
    if key.startswith("soccer_"):
        SPORT_KEY_TO_GAME[key] = "football"
    elif key.startswith("basketball_"):
        SPORT_KEY_TO_GAME[key] = "basketball"  # ALL basketball → "basketball" (matches sport_from_slug)
    elif key.startswith("icehockey_"):
        SPORT_KEY_TO_GAME[key] = "hockey"
    elif key.startswith("mma_"):
        SPORT_KEY_TO_GAME[key] = "ufc"
    elif key.startswith("cricket_"):
        SPORT_KEY_TO_GAME[key] = "cricket"
    elif key.startswith("rugby"):
        SPORT_KEY_TO_GAME[key] = "rugby"

# Direct mappings for scrapers that use non-standard sport keys
SPORT_KEY_TO_GAME["nba"] = "basketball"  # NBA also maps to "basketball" (matches sport_from_slug)


def _fair_probabilities(
    odds1: float,
    odds2: float,
    odds_draw: Optional[float] = None,
) -> Optional[Tuple[float, float, Optional[float]]]:
    """Fair probabilities as percentages, or None when they cannot be derived.

    Returns (prob1, prob2, prob_draw) with prob_draw None for two-way markets.

    None rather than a placeholder is the whole point. Nothing downstream
    re-derives probabilities from odds1/odds2 — `odds_service` reads
    fair_prob* as written — so a substituted value is consumed as an
    observation and priced against. A record we cannot price is strictly
    better absent: the caller skips it, and the previous good row for that
    unique key survives instead of being overwritten.

    Decimal odds <= 1 are rejected here rather than passed to
    `proportional_probabilities`, which returns (0.0, 0.0) for them — zeros
    that read downstream as "no fair value" only after the row has already
    replaced a usable one.
    """
    prices = [odds1, odds2] + ([odds_draw] if odds_draw is not None else [])
    for price in prices:
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            return None
        if not math.isfinite(price) or price <= 1:
            return None

    if odds_draw is not None:
        total = 1 / odds1 + 1 / odds_draw + 1 / odds2
        return (
            round((1 / odds1) / total * 100, 2),
            round((1 / odds2) / total * 100, 2),
            round((1 / odds_draw) / total * 100, 2),
        )

    fp1, fp2 = proportional_probabilities(odds1, odds2)
    return round(fp1 * 100, 2), round(fp2 * 100, 2), None


@dataclass
class OddsRecord:
    """A single parsed odds record ready for Supabase upsert."""
    match_id: str
    source: str
    sport: str
    team1: str
    team2: str
    market_type: str
    line: float  # 0 for h2h/btts, actual value for spreads/totals
    outcome1_name: str
    outcome2_name: str
    outcome_draw_name: Optional[str]
    odds1: float
    odds2: float
    odds_draw: Optional[float]
    fair_prob1: float
    fair_prob2: float
    fair_prob_draw: Optional[float]
    event_id: str
    commence_time: Optional[str]
    bookmaker_count: int
    is_live: bool = False
    team1_spread_point: Optional[float] = None  # Signed spread for team1 (e.g., +1.5 or -1.5)

    def to_dict(self) -> dict:
        from datetime import datetime, timezone
        d = {
            "match_id": self.match_id,
            "source": self.source,
            "sport": self.sport,
            "team1": self.team1,
            "team2": self.team2,
            "market_type": self.market_type,
            "line": self.line,
            "outcome1_name": self.outcome1_name,
            "outcome2_name": self.outcome2_name,
            "odds1": self.odds1,
            "odds2": self.odds2,
            "fair_prob1": self.fair_prob1,
            "fair_prob2": self.fair_prob2,
            "event_id": self.event_id,
            "bookmaker_count": self.bookmaker_count,
            "is_live": self.is_live,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        # Nullable fields
        if self.outcome_draw_name:
            d["outcome_draw_name"] = self.outcome_draw_name
        if self.odds_draw is not None:
            d["odds_draw"] = self.odds_draw
        if self.fair_prob_draw is not None:
            d["fair_prob_draw"] = self.fair_prob_draw
        if self.commence_time:
            d["commence_time"] = self.commence_time
        if self.team1_spread_point is not None:
            d["team1_spread_point"] = self.team1_spread_point
        return d


class OddsApiService:
    """Fetches odds from the-odds-api.com and writes to Supabase.

    Polls all configured sport keys on a fixed interval. For each event,
    collects odds from all bookmakers, computes median odds, removes vig
    to get fair probabilities, and upserts to sports_odds_v2.
    """

    BASE_URL = "https://api.the-odds-api.com/v4"
    POLL_INTERVAL = 300  # 5 minutes
    REGIONS = "eu,uk"

    def __init__(self, poll_interval: float = 300):
        self.api_key = os.getenv("THE_ODDS_API_KEY", "")
        if not self.api_key:
            raise ValueError("THE_ODDS_API_KEY not set in environment")
        logger.info(f"   API key: {self.api_key[:8]}...")
        self.poll_interval = poll_interval
        self._session: Optional[aiohttp.ClientSession] = None
        self._supabase = None
        self._running = False
        self.last_quota_remaining: Optional[int] = None
        self.last_quota_used: Optional[int] = None
        self.total_records_upserted = 0
        self.total_polls = 0

    def _get_supabase(self):
        if self._supabase is None:
            from src.data.supabase_client import SupabaseClient
            self._supabase = SupabaseClient()
        return self._supabase

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Main loop ────────────────────────────────────────────────────────

    async def run(self):
        """Main polling loop — runs forever on POLL_INTERVAL."""
        self._running = True
        logger.info(f"🏈 OddsApiService starting (poll every {self.poll_interval}s)")
        logger.info(f"   {len(SPORT_CONFIGS)} sport keys configured")

        while self._running:
            try:
                await self.poll_all()
                self.total_polls += 1
                logger.info(
                    f"✅ Poll #{self.total_polls} complete. "
                    f"Total records: {self.total_records_upserted}. "
                    f"Quota remaining: {self.last_quota_remaining}"
                )
            except Exception as e:
                logger.error(f"❌ Poll failed: {e}", exc_info=True)

            await asyncio.sleep(self.poll_interval)

    def stop(self):
        self._running = False

    # ── Core fetch + parse + upsert ──────────────────────────────────────

    async def poll_all(self) -> int:
        """Fetch odds for all configured sports and upsert to Supabase.

        Returns total number of records upserted.
        """
        all_records: List[OddsRecord] = []

        for i, (sport_key, markets) in enumerate(SPORT_CONFIGS.items()):
            # Throttle: 1s between requests to avoid rate limiting
            if i > 0:
                await asyncio.sleep(1.0)
            try:
                records = await self._fetch_sport(sport_key, markets)
                all_records.extend(records)
            except Exception as e:
                logger.warning(f"⚠️ Failed to fetch {sport_key}: {e}")

        if all_records:
            # Deduplicate: keep last record per unique key to avoid
            # "ON CONFLICT DO UPDATE cannot affect row a second time"
            seen = {}
            dupes = []
            for r in all_records:
                key = (r.match_id, r.market_type, r.line, r.source, r.sport)
                if key in seen:
                    dupes.append(r)
                seen[key] = r
            deduped = list(seen.values())
            if dupes:
                logger.info(f"   🔄 Deduped {len(all_records)} → {len(deduped)} records. Duplicates:")
                for r in dupes:
                    logger.info(f"      dup: {r.outcome1_name} vs {r.outcome2_name} [{r.sport}] {r.market_type} line={r.line} (event={r.event_id})")
            count = await self._upsert_to_supabase(deduped)
            self.total_records_upserted += count
            return count
        return 0

    # Markets supported by the bulk /odds endpoint
    BULK_MARKETS = {"h2h", "spreads", "totals"}

    async def _fetch_sport(self, sport_key: str, markets: List[str]) -> List[OddsRecord]:
        """Fetch odds for one sport key with all specified markets.

        The bulk /odds endpoint only supports h2h, spreads, totals.
        Extended markets (btts, h2h_h1, etc.) require the per-event endpoint.
        We split requests accordingly:
        1. Fetch standard markets via bulk /odds endpoint
        2. If there are extended markets, use event IDs from step 1
           to fetch them via /events/{eventId}/odds
        """
        bulk = [m for m in markets if m in self.BULK_MARKETS]
        extended = [m for m in markets if m not in self.BULK_MARKETS]

        session = await self._get_session()
        records: List[OddsRecord] = []
        events = []

        # Step 1: Fetch standard markets via bulk endpoint
        if bulk:
            bulk_str = ",".join(bulk)
            url = f"{self.BASE_URL}/sports/{sport_key}/odds"
            params = {
                "apiKey": self.api_key,
                "regions": self.REGIONS,
                "markets": bulk_str,
                "oddsFormat": "decimal",
            }

            async with session.get(url, params=params) as resp:
                self.last_quota_remaining = _safe_int(resp.headers.get("x-requests-remaining"))
                self.last_quota_used = _safe_int(resp.headers.get("x-requests-used"))

                if resp.status == 401:
                    raise ValueError("Invalid API key")
                if resp.status == 429:
                    logger.warning("⚠️ API rate limit hit!")
                    return []
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"⚠️ {sport_key}: HTTP {resp.status}: {text[:200]}")
                    return []

                events = await resp.json()

            game = SPORT_KEY_TO_GAME.get(sport_key, sport_key)
            for event in events:
                try:
                    event_records = self._parse_event(event, sport_key, game)
                    records.extend(event_records)
                except Exception as e:
                    logger.debug(f"Error parsing event {event.get('id', '?')}: {e}")

            logger.info(f"   📥 {sport_key}: {len(events)} events, {len(records)} records (bulk)")

        # Step 2: Fetch extended markets via per-event endpoint
        if extended and events:
            ext_str = ",".join(extended)
            game = SPORT_KEY_TO_GAME.get(sport_key, sport_key)
            ext_count = 0

            for event in events:
                event_id = event.get("id", "")
                if not event_id:
                    continue

                await asyncio.sleep(0.3)  # Throttle per-event requests

                url = f"{self.BASE_URL}/sports/{sport_key}/events/{event_id}/odds"
                params = {
                    "apiKey": self.api_key,
                    "regions": self.REGIONS,
                    "markets": ext_str,
                    "oddsFormat": "decimal",
                }

                try:
                    async with session.get(url, params=params) as resp:
                        self.last_quota_remaining = _safe_int(resp.headers.get("x-requests-remaining"))
                        self.last_quota_used = _safe_int(resp.headers.get("x-requests-used"))

                        if resp.status != 200:
                            continue

                        event_data = await resp.json()

                    try:
                        event_records = self._parse_event(event_data, sport_key, game)
                        records.extend(event_records)
                        ext_count += len(event_records)
                    except Exception as e:
                        logger.debug(f"Error parsing extended event {event_id}: {e}")
                except Exception as e:
                    logger.debug(f"Error fetching extended markets for {event_id}: {e}")

            if ext_count:
                logger.info(f"   📥 {sport_key}: +{ext_count} extended records ({ext_str})")

        return records

    # ── Event parsing ────────────────────────────────────────────────────

    def _parse_event(self, event: dict, sport_key: str, game: str) -> List[OddsRecord]:
        """Parse a single the-odds-api event into OddsRecord(s).

        For each market type (h2h, spreads, totals, btts), collects odds from
        all bookmakers, takes median, removes vig.
        """
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        event_id = event.get("id", "")
        commence_time = event.get("commence_time", "")

        match_id = make_match_id(home, away, game)
        team1 = normalize_team(home)
        team2 = normalize_team(away)

        # Collect odds by market type + line
        # Key: (market_type, line) -> { outcome_name: [decimal_odds_list] }
        from collections import defaultdict
        market_odds: Dict[Tuple, Dict[str, List[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        
        # Track home team's signed spread point per line
        # Key: abs_line -> home team's actual signed point (e.g., +1.5 or -1.5)
        home_spread_points: Dict[float, float] = {}

        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                mkey = market.get("key", "")
                for outcome in market.get("outcomes", []):
                    name = outcome.get("name", "")
                    price = outcome.get("price", 0)
                    point = outcome.get("point")  # None for h2h/btts

                    if price <= 1:
                        continue  # Invalid odds

                    if mkey in ("h2h", "btts", "h2h_h1"):
                        mkt_key = (mkey, None)
                    elif mkey == "spreads":
                        # Spreads: each team gets its own point (+1.5 / -1.5)
                        # Group by absolute value so both sides merge
                        abs_point = abs(point) if point is not None else None
                        mkt_key = (mkey, abs_point)
                        # Track home team's signed point
                        if point is not None and name == home and abs_point is not None:
                            home_spread_points[abs_point] = point
                    elif mkey == "totals":
                        mkt_key = (mkey, point)
                    else:
                        continue  # Skip unsupported market types

                    market_odds[mkt_key][name].append(price)

        records = []
        for (mtype, line), outcomes in market_odds.items():
            rec = self._build_record(
                mtype, line, outcomes, match_id, team1, team2,
                home, away, sport_key, game, event_id, commence_time,
                home_spread_point=home_spread_points.get(line) if mtype == "spreads" else None,
            )
            if rec:
                records.append(rec)

        return records

    def _build_record(
        self, mtype: str, line: Optional[float],
        outcomes: Dict[str, List[float]],
        match_id: str, team1: str, team2: str,
        home: str, away: str,
        sport_key: str, game: str,
        event_id: str, commence_time: str,
        home_spread_point: Optional[float] = None,
    ) -> Optional[OddsRecord]:
        """Build an OddsRecord from aggregated bookmaker odds.

        Takes median odds across bookmakers, removes vig.
        """
        if mtype == "h2h":
            return self._build_h2h_record(
                outcomes, match_id, team1, team2, home, away,
                sport_key, game, event_id, commence_time,
            )
        elif mtype == "h2h_h1":
            return self._build_h2h_record(
                outcomes, match_id, team1, team2, home, away,
                sport_key, game, event_id, commence_time,
                market_type_override="h2h_h1",
            )
        elif mtype == "spreads":
            return self._build_spread_record(
                line, outcomes, match_id, team1, team2, home, away,
                sport_key, game, event_id, commence_time,
                home_spread_point=home_spread_point,
            )
        elif mtype == "totals":
            return self._build_totals_record(
                line, outcomes, match_id, team1, team2,
                sport_key, game, event_id, commence_time,
            )
        elif mtype == "btts":
            return self._build_btts_record(
                outcomes, match_id, team1, team2,
                sport_key, game, event_id, commence_time,
            )
        return None

    def _build_h2h_record(
        self, outcomes: Dict[str, List[float]],
        match_id: str, team1: str, team2: str,
        home: str, away: str,
        sport_key: str, game: str,
        event_id: str, commence_time: str,
        market_type_override: Optional[str] = None,
    ) -> Optional[OddsRecord]:
        """Build h2h (moneyline) record — supports 2-way and 3-way.

        market_type_override allows reuse for h2h_h1 (1st half moneyline).
        """
        home_odds_list = outcomes.get(home, [])
        away_odds_list = outcomes.get(away, [])
        draw_odds_list = outcomes.get("Draw", [])

        if not home_odds_list or not away_odds_list:
            return None

        odds1 = statistics.median(home_odds_list)
        odds2 = statistics.median(away_odds_list)

        if draw_odds_list:
            # 3-way: football, rugby, hockey
            odds_draw = statistics.median(draw_odds_list)
            probs = _fair_probabilities(odds1, odds2, odds_draw)
            if probs is None:
                logger.debug(
                    "Skipping 3-way h2h for %s: odds %s/%s/%s do not support a "
                    "fair value", match_id, odds1, odds_draw, odds2,
                )
                return None
            fair_prob1, fair_prob2, fair_prob_draw = probs

            return OddsRecord(
                match_id=match_id, source="the-odds-api", sport=sport_key,
                team1=team1, team2=team2,
                market_type=market_type_override or "h2h", line=0,
                outcome1_name=home, outcome2_name=away,
                outcome_draw_name="Draw",
                odds1=round(odds1, 3), odds2=round(odds2, 3),
                odds_draw=round(odds_draw, 3),
                fair_prob1=fair_prob1, fair_prob2=fair_prob2,
                fair_prob_draw=fair_prob_draw,
                event_id=event_id, commence_time=commence_time,
                bookmaker_count=len(home_odds_list),
            )
        else:
            # 2-way: basketball, MMA, tennis, cricket
            probs = _fair_probabilities(odds1, odds2)
            if probs is None:
                logger.debug(
                    "Skipping 2-way h2h for %s: odds %s/%s do not support a "
                    "fair value", match_id, odds1, odds2,
                )
                return None
            fair_prob1, fair_prob2, _ = probs

            return OddsRecord(
                match_id=match_id, source="the-odds-api", sport=sport_key,
                team1=team1, team2=team2,
                market_type=market_type_override or "h2h", line=0,
                outcome1_name=home, outcome2_name=away,
                outcome_draw_name=None,
                odds1=round(odds1, 3), odds2=round(odds2, 3),
                odds_draw=None,
                fair_prob1=fair_prob1, fair_prob2=fair_prob2,
                fair_prob_draw=None,
                event_id=event_id, commence_time=commence_time,
                bookmaker_count=len(home_odds_list),
            )

    def _build_spread_record(
        self, line: Optional[float], outcomes: Dict[str, List[float]],
        match_id: str, team1: str, team2: str,
        home: str, away: str,
        sport_key: str, game: str,
        event_id: str, commence_time: str,
        home_spread_point: Optional[float] = None,
    ) -> Optional[OddsRecord]:
        """Build spread (handicap) record.
        
        team1 = home team. fair_prob1 = probability of team1 covering their spread.
        team1_spread_point stores team1's actual signed point (e.g., +1.5 or -1.5)
        so the scanner knows which direction team1 is on.
        """
        home_odds = outcomes.get(home, [])
        away_odds = outcomes.get(away, [])

        if not home_odds or not away_odds:
            return None

        odds1 = statistics.median(home_odds)
        odds2 = statistics.median(away_odds)

        probs = _fair_probabilities(odds1, odds2)
        if probs is None:
            logger.debug(
                "Skipping spreads for %s: odds %s/%s do not support a fair "
                "value", match_id, odds1, odds2,
            )
            return None
        fair_prob1, fair_prob2, _ = probs

        return OddsRecord(
            match_id=match_id, source="the-odds-api", sport=sport_key,
            team1=team1, team2=team2,
            market_type="spreads", line=line,
            outcome1_name=home, outcome2_name=away,
            outcome_draw_name=None,
            odds1=round(odds1, 3), odds2=round(odds2, 3),
            odds_draw=None,
            fair_prob1=fair_prob1, fair_prob2=fair_prob2,
            fair_prob_draw=None,
            event_id=event_id, commence_time=commence_time,
            bookmaker_count=len(home_odds),
            team1_spread_point=home_spread_point,
        )

    def _build_totals_record(
        self, line: Optional[float], outcomes: Dict[str, List[float]],
        match_id: str, team1: str, team2: str,
        sport_key: str, game: str,
        event_id: str, commence_time: str,
    ) -> Optional[OddsRecord]:
        """Build totals (O/U) record."""
        over_odds = outcomes.get("Over", [])
        under_odds = outcomes.get("Under", [])

        if not over_odds or not under_odds:
            return None

        odds1 = statistics.median(over_odds)
        odds2 = statistics.median(under_odds)

        probs = _fair_probabilities(odds1, odds2)
        if probs is None:
            logger.debug(
                "Skipping totals for %s: odds %s/%s do not support a fair "
                "value", match_id, odds1, odds2,
            )
            return None
        fair_prob1, fair_prob2, _ = probs

        return OddsRecord(
            match_id=match_id, source="the-odds-api", sport=sport_key,
            team1=team1, team2=team2,
            market_type="totals", line=line,
            outcome1_name="Over", outcome2_name="Under",
            outcome_draw_name=None,
            odds1=round(odds1, 3), odds2=round(odds2, 3),
            odds_draw=None,
            fair_prob1=fair_prob1, fair_prob2=fair_prob2,
            fair_prob_draw=None,
            event_id=event_id, commence_time=commence_time,
            bookmaker_count=len(over_odds),
        )

    def _build_btts_record(
        self, outcomes: Dict[str, List[float]],
        match_id: str, team1: str, team2: str,
        sport_key: str, game: str,
        event_id: str, commence_time: str,
    ) -> Optional[OddsRecord]:
        """Build BTTS (both teams to score) record."""
        yes_odds = outcomes.get("Yes", [])
        no_odds = outcomes.get("No", [])

        if not yes_odds or not no_odds:
            return None

        odds1 = statistics.median(yes_odds)
        odds2 = statistics.median(no_odds)

        probs = _fair_probabilities(odds1, odds2)
        if probs is None:
            logger.debug(
                "Skipping btts for %s: odds %s/%s do not support a fair "
                "value", match_id, odds1, odds2,
            )
            return None
        fair_prob1, fair_prob2, _ = probs

        return OddsRecord(
            match_id=match_id, source="the-odds-api", sport=sport_key,
            team1=team1, team2=team2,
            market_type="btts", line=0,
            outcome1_name="Yes", outcome2_name="No",
            outcome_draw_name=None,
            odds1=round(odds1, 3), odds2=round(odds2, 3),
            odds_draw=None,
            fair_prob1=fair_prob1, fair_prob2=fair_prob2,
            fair_prob_draw=None,
            event_id=event_id, commence_time=commence_time,
            bookmaker_count=len(yes_odds),
        )

    # ── Supabase upsert ──────────────────────────────────────────────────

    async def _upsert_to_supabase(self, records: List[OddsRecord]) -> int:
        """Upsert records to sports_odds_v2 table.

        Uses match_id + market_type + line + source as unique key.
        """
        if not records:
            return 0

        client = self._get_supabase()
        rows = [r.to_dict() for r in records]

        try:
            result = (
                client.client.table("sports_odds_v2")
                .upsert(rows, on_conflict="match_id,market_type,line,source,sport")
                .execute()
            )
            count = len(result.data) if result.data else 0
            logger.info(f"   💾 Upserted {count} records to sports_odds_v2")
            return count
        except Exception as e:
            logger.error(f"❌ Supabase upsert error: {e}")
            # Try smaller batches on failure
            count = 0
            batch_size = 50
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                try:
                    result = (
                        client.client.table("sports_odds_v2")
                        .upsert(batch, on_conflict="match_id,market_type,line,source,sport")
                        .execute()
                    )
                    count += len(result.data) if result.data else 0
                except Exception as e2:
                    logger.error(f"❌ Batch upsert error (rows {i}-{i+len(batch)}): {e2}")
            return count

    # ── Status reporting ─────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "polls": self.total_polls,
            "records_upserted": self.total_records_upserted,
            "quota_remaining": self.last_quota_remaining,
            "quota_used": self.last_quota_used,
            "sport_keys": len(SPORT_CONFIGS),
        }


def _safe_int(val) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


# ── CLI entry point ──────────────────────────────────────────────────────

async def _main():
    import argparse
    parser = argparse.ArgumentParser(description="Odds API Service")
    parser.add_argument("--once", action="store_true", help="Single fetch, no loop")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval (seconds)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    service = OddsApiService(poll_interval=args.interval)

    try:
        if args.once:
            count = await service.poll_all()
            logger.info(f"✅ Fetched {count} records. Status: {service.status()}")
        else:
            await service.run()
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(_main())
