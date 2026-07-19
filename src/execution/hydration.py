"""
Hydration - Fetches existing orders and positions from Polymarket on startup.

This module handles:
- Active order hydration (prevent duplicate orders)
- Filled position hydration (detect arbs, calculate P&L)
- Historical P&L persistence
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, TYPE_CHECKING

from src.core.config import CONFIG
from src.core.match_id import is_individual_game_market, parse_teams_from_question, is_will_win_market, extract_will_win_team, normalize_team

if TYPE_CHECKING:
    from src.state.bot_state import BotState
    from src.services.odds_service import OddsService

# ============================================================================
# Trading Deadline Extraction
# ============================================================================

def _extract_deadline(market_info: dict) -> Optional[datetime]:
    """
    Extract trading deadline from market_info's Gamma API time fields.
    
    Priority: gameStartTime > endDate (gameStartTime is more accurate for esports/sports).
    Returns timezone-aware datetime or None.
    """
    for field in ("gameStartTime", "endDate"):
        raw = market_info.get(field)
        if not raw:
            continue
        try:
            # Gamma API returns various formats:
            #   "2026-02-27 07:30:00+00"  (gameStartTime)
            #   "2026-02-27T13:30:00Z"    (endDate ISO)
            #   "2026-02-27"              (endDateIso - date only)
            raw = raw.strip()
            # Try ISO format first (handles Z suffix and T separator)
            if "T" in raw or "Z" in raw:
                raw = raw.replace("Z", "+00:00")
                return datetime.fromisoformat(raw)
            # Try space-separated datetime
            if " " in raw:
                return datetime.fromisoformat(raw.replace(" ", "T"))
            # Date-only format (treat as end of day UTC)
            if len(raw) == 10:
                return datetime.fromisoformat(raw + "T23:59:59+00:00")
        except (ValueError, TypeError):
            continue
    return None


# ============================================================================
# Weather/Stock Market Helpers
# ============================================================================

def _extract_weather_team_name(
    question: str,
    group_item: str,
    outcome: str,
) -> Optional[str]:
    """
    Extract descriptive team name from weather/stock market questions.
    
    Must match SpreadBot's format: f"{city_lower}:{bin_label}"
    
    Weather: "Will the high temperature in Toronto be -11°C on February 7?"
      → "toronto:-11°C" (Yes) or "toronto:Not -11°C" (No)
    
    Stock: "Will TSLA close up or down on February 5?"
      → Uses group_item directly (e.g., "TSLA:Up 3-4%")
    
    Returns None if the question isn't a weather/stock market.
    """
    q_lower = question.lower()
    
    # City name normalization to match WeatherMarketClient conventions
    CITY_NORMALIZE = {
        "new york city": "new york",
        "nyc": "new york",
    }
    
    # Weather: "Will the high temperature in X be Y on Z?"
    if "temperature in " in q_lower and " be " in q_lower:
        try:
            # Extract city (lowercase) using case-insensitive split
            in_idx = q_lower.index("temperature in ") + len("temperature in ")
            be_idx = q_lower.index(" be ", in_idx)
            city = question[in_idx:be_idx].strip().lower()
            
            # Normalize city name
            city = CITY_NORMALIZE.get(city, city)
            
            # Use group_item for bin label (most reliable, has proper case)
            if group_item:
                bin_label = group_item
            else:
                # Fallback: extract from question, preserving original case
                after_be = question[be_idx + 4:]  # skip " be "
                on_idx = after_be.lower().find(" on ")
                if on_idx >= 0:
                    bin_label = after_be[:on_idx].strip().rstrip("?")
                else:
                    bin_label = after_be.strip().rstrip("?")
                
                # Strip "between " prefix (question says "be between 50-51°F")
                if bin_label.lower().startswith("between "):
                    bin_label = bin_label[8:]  # len("between ") == 8
            
            if outcome.lower() == "no":
                return f"{city}:Not {bin_label}"
            else:
                return f"{city}:{bin_label}"
        except (IndexError, ValueError):
            pass
    
    # Stock: "Will X close up or down on Y?" or similar
    if ("up or down" in q_lower or "close above" in q_lower) and group_item:
        # Extract ticker from question start: "Will TSLA close..."
        try:
            ticker = question.split("Will ")[1].split(" ")[0].strip().upper()
            if outcome.lower() == "no":
                return f"{ticker}:Not {group_item}"
            else:
                return f"{ticker}:{group_item}"
        except (IndexError, ValueError):
            pass
    
    return None


# ============================================================================
# Historical P&L Persistence
# ============================================================================

HISTORICAL_PNL_FILE = Path(__file__).parent.parent.parent / "data" / "historical_pnl.json"


def _load_historical_pnl() -> dict:
    """Load historical P&L from disk."""
    if not HISTORICAL_PNL_FILE.exists():
        return {"resolved_tokens": {}, "total_wins": 0, "total_losses": 0, "total_pnl": 0.0}
    try:
        with open(HISTORICAL_PNL_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"resolved_tokens": {}, "total_wins": 0, "total_losses": 0, "total_pnl": 0.0}


def _save_historical_pnl(data: dict):
    """Save historical P&L to disk."""
    HISTORICAL_PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORICAL_PNL_FILE, "w") as f:
        json.dump(data, f, indent=2)


async def hydrate_active_orders(
    bot_state: "BotState",
    executor,
    poly_client=None,
    odds_service: "OddsService" = None,
    get_position_order_ids_fn=None,
    quiet: bool = False,
    skip_individual_game_filter: bool = False,
) -> int:
    """
    Fetch open orders from Polymarket to prevent duplicate orders
    and enable monitoring for outbids.
    
    This is critical on restart to avoid placing orders on tokens
    where we already have active orders.
    
    Args:
        poly_client: Optional PolymarketEsportsClient for looking up market info
        odds_service: Optional OddsService for looking up fair values (for edge display)
        quiet: If True, suppress verbose per-order output (for periodic re-syncs)
    """
    try:
        if not quiet:
            print("🔄 Hydrating active orders from Polymarket...")
        client = executor._get_client()
        
        # Fetch all open orders
        # py-clob-client get_orders() returns OpenOrdersResponse which has list of orders
        orders = []
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = client.get_orders()
                # Handle different response formats
                if hasattr(response, '__iter__'):
                    orders = list(response)
                else:
                    orders = []
                break  # Success - exit retry loop
            except Exception as e:
                if attempt < max_retries:
                    await asyncio.sleep(2)
                else:
                    print(f"   ⚠️ get_orders() failed after {max_retries} attempts: {e}, proceeding with empty orders")
                    orders = []
        
        # First pass: collect all order info
        order_infos = []
        for o in orders:
            # Handle both dict and object formats
            if isinstance(o, dict):
                token_id = o.get("asset_id") or o.get("token_id")
                order_id = o.get("id") or o.get("order_id")
                status = o.get("status", "")
                price = float(o.get("price", 0))
                size = float(o.get("original_size", o.get("size", 0)))
                side = o.get("side", "")
                outcome_name = o.get("outcome", o.get("outcome_name"))
            else:
                token_id = getattr(o, "asset_id", None) or getattr(o, "token_id", None)
                order_id = getattr(o, "id", None) or getattr(o, "order_id", None)
                status = getattr(o, "status", "")
                price = float(getattr(o, "price", 0))
                size = float(getattr(o, "original_size", getattr(o, "size", 0)))
                side = getattr(o, "side", "")
                outcome_name = getattr(o, "outcome", getattr(o, "outcome_name", None))
            
            # Only track LIVE (open) orders
            if status.upper() in ["LIVE", "OPEN", "PENDING"] and token_id:
                order_infos.append({
                    "order_id": order_id,
                    "token_id": token_id,
                    "price": price,
                    "size": size,
                    "side": side,
                    "outcome_name": outcome_name,
                })
        
        # Second pass: batch lookup market info if poly_client provided
        market_info_cache = {}  # token_id -> market_info
        if poly_client and order_infos:
            # Batch lookup - limit concurrency
            token_ids = [o["token_id"] for o in order_infos]
            unique_tokens = list(set(token_ids))
            
            # Pre-warm L1 cache from Redis via MGET (single round-trip per 200 keys)
            hits = await poly_client.pre_warm_cache(unique_tokens)
            if hits > 0:
                print(f"   ⚡ Redis pre-warm: {hits}/{len(unique_tokens)} tokens cached")
            
            # Fetch remaining (cache misses) in batches of 30
            for i in range(0, len(unique_tokens), 30):
                batch = unique_tokens[i:i+30]
                tasks = [poly_client.get_market_by_token(tid) for tid in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for tid, result in zip(batch, results):
                    if result and not isinstance(result, Exception):
                        market_info_cache[tid] = result
        
        # Filter out orders we already cancelled locally (Polymarket API cache lag)
        # Without this, stale cancelled orders appear alongside new hedges and
        # the duplicate detection below incorrectly cancels the valid hedge order.
        cancelled_ids = bot_state.get_cancelled_order_ids()
        if cancelled_ids:
            pre_filter = len(order_infos)
            order_infos = [o for o in order_infos if o["order_id"] not in cancelled_ids]
            filtered = pre_filter - len(order_infos)
            if filtered > 0 and not quiet:
                print(f"   🔄 Filtered {filtered} stale cancelled order(s) from API response")
        
        # Third pass: group orders by token_id to detect duplicates
        orders_by_token: Dict[str, List[dict]] = {}
        for info in order_infos:
            token_id = info["token_id"]
            if token_id not in orders_by_token:
                orders_by_token[token_id] = []
            orders_by_token[token_id].append(info)
        
        # Fourth pass: for each token, keep best order, cancel duplicates
        count = 0
        total_value = 0.0
        duplicate_count = 0
        
        # Get order IDs from positions to avoid cancelling them as duplicates
        position_order_ids = get_position_order_ids_fn() if get_position_order_ids_fn else set()
        
        for token_id, token_orders in orders_by_token.items():
            # Token tracking handled by BotState.register_hydrated_order below
            
            # Filter out orders that are tracked as positions (those should not be touched)
            non_position_orders = [o for o in token_orders if o["order_id"] not in position_order_ids]
            position_orders = [o for o in token_orders if o["order_id"] in position_order_ids]
            
            # If there's a position order, it takes priority
            if position_orders:
                # Position order is the best, non-position orders are duplicates
                best_order = position_orders[0]  # Should only be one
                duplicates = non_position_orders  # All non-position orders are duplicates
            elif non_position_orders:
                # Sort by price descending (best bid first for BUY orders)
                non_position_orders.sort(key=lambda x: x["price"], reverse=True)
                best_order = non_position_orders[0]
                duplicates = non_position_orders[1:]
            else:
                continue  # No valid orders for this token
            
            # Cancel duplicate orders
            for dup in duplicates:
                try:
                    dup_market = market_info_cache.get(dup["token_id"], {})
                    dup_question = dup_market.get("question", "?")
                    print(f"   ⚠️ DUPLICATE ORDER: {dup['outcome_name']} @ {dup['price']:.2f} - cancelling... (dup_id={dup['order_id'][:12]}... kept={best_order['order_id'][:12]}... @ {best_order['price']:.2f}) | {dup_question}")
                    client.cancel(dup["order_id"])
                    # CRITICAL: Also update BotState so the cancelled order is removed
                    # from match state and added to _cancelled_order_ids.
                    # Without this, BotState still thinks the order is open, causing:
                    # 1. HedgeSeeker to see stale coverage and skip re-hedging
                    # 2. The stale order_id to be re-registered on next resync
                    bot_state.cancel_order(dup["order_id"])
                    duplicate_count += 1
                except Exception as e:
                    print(f"   ⚠️ Failed to cancel duplicate: {e}")
            
            info = best_order
            
            # Get market info for display
            market_info = market_info_cache.get(token_id, {})
            question = market_info.get("question", "")
            
            # Parse team name from outcome or market question
            team_name = info["outcome_name"]
            match_display = ""
            
            if question:
                # Extract match from question like "CS2: Team A vs Team B (BO1)"
                match_display = question
                
                # Skip individual game/round markets (e.g., "Game 1 Winner")
                # We don't have odds for these - cancel them
                # SpreadBot sets skip_individual_game_filter=True to keep toss markets etc.
                if not skip_individual_game_filter and is_individual_game_market(match_display):
                    try:
                        await executor.cancel_order(info["order_id"], force=True)
                        print(f"   🚫 Cancelled individual game market: {match_display[:50]}...")
                    except Exception as e:
                        print(f"   ⚠️ Failed to cancel individual game market: {e}")
                    continue
                
                # ===== EXTRACT TEAM NAME FROM BINARY MARKET QUESTIONS =====
                # Binary markets (Yes/No outcomes) need special handling to get descriptive team names.
                # Supported: rugby "Will X win?", weather "temperature in X be Y", stock "X up or down"
                # NCAAB sports: "Team A vs. Team B" (winner), "Spread: Team (-2.5)", "Over"/"Under"
                q_lower = question.lower()
                is_rugby_market = is_will_win_market(q_lower)
                is_weather_or_stock = False
                is_sports_market = False
                is_mentions_market = False
                is_tennis_market = False
                
                # Detect tennis sub-markets: Set Handicap, Set Winner, Total Sets O/U, Match O/U, Over/Under X.X
                import re as _re
                if (q_lower.startswith("set handicap:")
                    or _re.match(r'^set \d+ winner:', q_lower)
                    or _re.match(r'^(?:total sets|set \d+ games|match)\s+o/u\s+\d', q_lower)
                    or _re.match(r'^(?:over|under)\s+\d+\.?\d*$', q_lower)):
                    is_tennis_market = True
                    is_sports_market = True  # Prevent binary market cancellation
                    # Extract team name from outcomes for tennis sub-markets
                    token_outcome = info["outcome_name"]  # "Yes"/"No" or team name
                    if token_outcome.lower() not in ("yes", "no"):
                        team_name = token_outcome
                
                # Detect NCAAB/sports markets: has "vs" or "vs." in question, or Spread/O/U format
                if (" vs " in question or " vs. " in question 
                    or q_lower.startswith("spread:") 
                    or q_lower.strip() in ("over", "under")):
                    is_sports_market = True
                    # Try to use groupItemTitle for descriptive team name
                    group_item = market_info.get("groupItemTitle", "") if market_info else ""
                    if group_item and (group_item.startswith("Spread") or group_item.startswith("O/U")):
                        # Spread/O/U sub-market: construct team names
                        event_title_str = market_info.get("title", "") if market_info else ""
                        source_str = event_title_str or question
                        from src.core.match_id import parse_match_question
                        _g, et1, et2 = parse_match_question(source_str)
                        token_outcome = info["outcome_name"]  # "Yes"/"No" or "Over"/"Under" from API
                        
                        # SportsBot totals: outcomes are "Over"/"Under" — use directly
                        # SpreadBot: outcomes are "Yes"/"No" — construct team name
                        if token_outcome.lower() in ("over", "under"):
                            team_name = token_outcome  # Match check_multi_market_opportunity format
                        elif et1 and et2:
                            # Match SpreadBot: Yes = "Team1: O/U 149.5", No = "Team2"
                            if token_outcome.lower() == "no":
                                team_name = et2
                            else:
                                team_name = f"{et1}: {group_item}"
                        else:
                            # Fallback: no event title available
                            if token_outcome.lower() == "no":
                                team_name = f"Not {group_item}"
                            else:
                                team_name = group_item
                
                if is_rugby_market:
                    # Rugby/football "Will X win?" or "Will X win on DATE?" - extract team name
                    extracted_team = extract_will_win_team(question)
                    if extracted_team:
                        # CRITICAL FIX: Check OUTCOMES array, NOT token index!
                        # Polymarket does NOT guarantee token ordering.
                        # The outcomes array tells us what each token represents.
                        clob_tokens = market_info.get("clobTokenIds", [])
                        if isinstance(clob_tokens, str):
                            import json
                            try:
                                clob_tokens = json.loads(clob_tokens)
                            except:
                                clob_tokens = []
                        
                        outcomes = market_info.get("outcomes", [])
                        if isinstance(outcomes, str):
                            import json
                            try:
                                outcomes = json.loads(outcomes)
                            except:
                                outcomes = []
                        
                        # Find what outcome this token represents by checking outcomes array
                        is_no_token = False
                        try:
                            if token_id in clob_tokens:
                                token_index = clob_tokens.index(token_id)
                                if token_index < len(outcomes):
                                    token_outcome = outcomes[token_index]
                                    is_no_token = token_outcome.lower() == "no"
                        except (ValueError, IndexError):
                            pass
                        
                        # For NO tokens, append " No" suffix
                        # This ensures get_fair_value_for_team correctly inverts: 1 - yes_fair
                        if is_no_token:
                            team_name = f"{extracted_team} No"
                        else:
                            team_name = extracted_team
                else:
                    # Weather/stock/mentions: extract descriptive name from question
                    group_item = market_info.get("groupItemTitle", "") if market_info else ""
                    # Determine outcome for this token
                    token_outcome = info["outcome_name"]  # "Yes" or "No" from API
                    weather_name = _extract_weather_team_name(question, group_item, token_outcome)
                    if weather_name:
                        team_name = weather_name
                        is_weather_or_stock = True
                    else:
                        # Try mentions: "Will X say \"Y\" ...?"
                        import re
                        mentions_keywords = ("say ", "said ", "mention ", "name ")
                        if q_lower.startswith("will ") and any(kw in q_lower for kw in mentions_keywords):
                            label = None
                            # PRIORITY 1: Reuse existing BotState team name (registered by SpreadBot)
                            # groupItemTitle from Gamma API can vary per-token (e.g., "Hillary / Clinton"
                            # vs "Hillary") causing mismatches. BotState has the canonical name.
                            condition_id_check = market_info.get("condition_id", "") if market_info else ""
                            if condition_id_check:
                                existing_match_id = bot_state._condition_to_match.get(condition_id_check, "")
                                existing_match = bot_state._matches.get(existing_match_id) if existing_match_id else None
                                if existing_match:
                                    # Find which team this token belongs to
                                    if existing_match.token1 == token_id and existing_match.team1:
                                        team_name = existing_match.team1
                                        is_mentions_market = True
                                        label = "__matched__"  # Skip further extraction
                                    elif existing_match.token2 == token_id and existing_match.team2:
                                        team_name = existing_match.team2
                                        is_mentions_market = True
                                        label = "__matched__"
                            
                            # PRIORITY 2: Use groupItemTitle (matches SpreadBot's bin_label)
                            if not label and group_item:
                                label = group_item
                            
                            # PRIORITY 3: Extract quoted word from question
                            if not label:
                                quoted = re.search(r'["\u201c\u201d]([^"\u201c\u201d]+)["\u201c\u201d]', question)
                                label = quoted.group(1).strip() if quoted else None
                            
                            if label and label != "__matched__":
                                if token_outcome.lower() == "no":
                                    team_name = f"mentions:Not {label}"
                                else:
                                    team_name = f"mentions:{label}"
                                is_mentions_market = True
                
                
                # Skip Over/Under markets - we only trade on team vs team
                # EXCEPTION: Rugby, weather/stock, and NCAAB sports markets have binary outcomes but are valid
                outcomes = market_info.get("outcomes", [])
                if isinstance(outcomes, list) and len(outcomes) >= 2:
                    skip_outcomes = ("yes", "no", "over", "under")
                    is_binary_market = outcomes[0].lower() in skip_outcomes or outcomes[1].lower() in skip_outcomes
                    
                    if is_binary_market and not is_rugby_market and not is_weather_or_stock and not is_sports_market and not is_mentions_market and not is_tennis_market:
                        # True Over/Under or generic Yes/No - cancel
                        try:
                            await executor.cancel_order(info["order_id"], force=True)
                            print(f"   🚫 Cancelled Over/Under market: {match_display[:50]}...")
                        except Exception as e:
                            print(f"   ⚠️ Failed to cancel Over/Under market: {e}")
                        continue
                
                # If we have outcomes, try to find the team name (for non-rugby markets only)
                # Skip this if we already extracted team_name for rugby (team_name != "Yes")
                if isinstance(outcomes, list) and outcomes and not is_rugby_market:
                    # Find which outcome this token is
                    clob_tokens = market_info.get("clobTokenIds", [])
                    if isinstance(clob_tokens, str):
                        import json
                        try:
                            clob_tokens = json.loads(clob_tokens)
                        except:
                            clob_tokens = []
                    
                    for idx, t in enumerate(clob_tokens):
                        if t == token_id and idx < len(outcomes):
                            outcome_val = outcomes[idx] if isinstance(outcomes[idx], str) else outcomes[idx].get("name", outcomes[idx])
                            if outcome_val.lower() not in ("yes", "no"):
                                team_name = outcome_val
                            break
                
                # FIX: Resolve non-team-name outcomes to actual team names.
                # Esports binary markets use generic labels ("Match Winner", "Not Match Winner")
                # or full match titles ("Toronto KOI vs G2 Minnesota - CDL...") as outcomes
                # instead of actual team names. Resolve using groupItemTitle or question parsing.
                _is_bad_team = (
                    team_name in ("Match Winner", "Not Match Winner")
                    or (" vs " in team_name and not is_sports_market)  # Full match title as outcome
                )
                if _is_bad_team:
                    group_item = market_info.get("groupItemTitle", "") if market_info else ""
                    resolved_team = ""
                    
                    if group_item and group_item not in ("Match Winner", "Not Match Winner", "Moneyline") and " vs " not in group_item:
                        # groupItemTitle IS the team name for this binary market
                        resolved_team = group_item
                    else:
                        # Fallback: parse question for team names, use token position
                        from src.core.match_id import parse_match_question as _pmq
                        _g, t1, t2 = _pmq(question)
                        if t1 and t2:
                            # Determine which team by token position in clobTokenIds
                            clob_tokens_mw = market_info.get("clobTokenIds", []) if market_info else []
                            if isinstance(clob_tokens_mw, str):
                                import json
                                try:
                                    clob_tokens_mw = json.loads(clob_tokens_mw)
                                except:
                                    clob_tokens_mw = []
                            for idx, t in enumerate(clob_tokens_mw):
                                if t == token_id:
                                    # Token index 0 = YES = first team in question
                                    resolved_team = t1 if idx == 0 else t2
                                    break
                    
                    if resolved_team:
                        team_name = resolved_team
            
            if not team_name:
                team_name = f"Token-{token_id[:8]}"
            
            # Try to find odds match and calculate edge
            edge_pct = None
            fair_value = None
            odds_match = None
            
            if odds_service:
                try:
                    from src.scanning.team_matcher import find_matching_odds, get_fair_value_for_team
                    from src.core.match_id import parse_match_question, normalize_team, normalize_game
                    
                    # Use canonical parsing function (with odds_service for rugby team lookup)
                    game_parsed, team1_parsed, team2_parsed = parse_match_question(match_display, odds_service)
                    
                    # Try to find matching odds - MUST match BOTH teams
                    if team1_parsed and team2_parsed:
                        game_norm = normalize_game(game_parsed) if game_parsed else ""
                        all_matches = odds_service.get_matches(game=game_norm, fresh_only=True) if game_norm else odds_service.get_matches(fresh_only=True)
                        odds_match, _ = find_matching_odds(team1_parsed, team2_parsed, all_matches)
                    
                    # CRITICAL: Only use odds_match if BOTH teams matched exactly
                    # We NEVER use single-team matching - it can match the WRONG match!
                    # Example: "Bad Luck" could match "Players vs Bad Luck" instead of "Bad Luck vs Yawara"
                    if odds_match:
                        match_display = f"{odds_match.game}: {odds_match.team1} vs {odds_match.team2}"
                        fair_value = get_fair_value_for_team(team_name, odds_match)
                        if fair_value:
                            edge_pct = (fair_value - info["price"]) * 100
                except Exception:
                    pass
            
            # ===== REGISTER WITH BOTSTATE (single source of truth) =====
            if match_display:
                # DEBUG: Check if BotState already has a different order for this token
                existing_order_info = bot_state.get_order_info(token_id)
                if existing_order_info and existing_order_info.get("order_id") != info["order_id"]:
                    existing_oid = existing_order_info.get("order_id", "")[:12]
                    existing_price = existing_order_info.get("price", 0)
                    new_oid = info["order_id"][:12]
                    new_price = info["price"]
                
                condition_id = market_info.get("condition_id", "") if market_info else ""
                # Event title (from Gamma API) for sports spread/O/U two-team extraction
                event_title = market_info.get("title", "") if market_info else ""
                # Event slug (from Gamma API) for sport classification
                event_slug = market_info.get("event_slug", "") if market_info else ""
                # Trading deadline from Gamma API time fields
                trading_deadline = _extract_deadline(market_info) if market_info else None
                try:
                    match_state = bot_state.register_hydrated_order(
                        order_id=info["order_id"],
                        token_id=token_id,
                        team=team_name,
                        price=info["price"],
                        size=info["size"],
                        match_question=match_display,
                        condition_id=condition_id,
                        odds_service=odds_service,
                        event_title=event_title,
                        event_slug=event_slug,
                        trading_deadline=trading_deadline,
                        fair_value=fair_value,
                    )
                except Exception as e:
                    # CRITICAL: Don't let one bad market kill ALL hydration!
                    # Log the error and continue processing remaining orders.
                    print(f"   ⚠️ Failed to register order for '{match_display[:60]}...': {e}")
                    continue
                
                
                # Set fair probs from OddsService if we have a valid odds match
                # NOTE: odds_match is ONLY set when BOTH teams matched exactly
                # (we removed the dangerous single-team fallback)
                if match_state and odds_match:
                    bot_state.update_fair_probs_by_team(
                        match_id=match_state.match_id,
                        team1=odds_match.team1,
                        team2=odds_match.team2,
                        fair_prob1=odds_match.fair_prob1 / 100,  # Convert % to 0-1
                        fair_prob2=odds_match.fair_prob2 / 100,
                    )
                
                count += 1
                total_value += info["price"] * info["size"]
            else:
                # FALLBACK: Even without market info, register this order in BotState
                # so HedgeSeeker can see it exists and won't place a duplicate.
                # This is critical for orders whose get_market_by_token fails (404/429).
                try:
                    fallback_match_id = f"unknown:{token_id[:18]}"
                    bot_state.register_match(
                        match_id=fallback_match_id,
                        game="unknown",
                        team1=info["outcome_name"],
                        team2="",
                    )
                    bot_state.register_order(
                        match_id=fallback_match_id,
                        order_id=info["order_id"],
                        token_id=token_id,
                        team=info["outcome_name"],
                        price=info["price"],
                        size=info["size"],
                    )
                    count += 1
                    total_value += info["price"] * info["size"]
                except Exception:
                    pass  # Best-effort fallback
    except Exception as e:
        print(f"⚠️ Failed to hydrate orders: {e}")
        import traceback
        traceback.print_exc()
    
    return duplicate_count


def print_hydrated_orders(
    bot_state: "BotState",
    hedge_tokens: set = None,
    duplicate_count: int = 0,
    quiet: bool = False,
    live_only: bool = False,
    odds_service=None,
):
    """
    Print hydrated orders with hedge status.
    
    Call this AFTER positions are hydrated so we know which orders are hedges.
    Derives all data from BotState (single source of truth).
    
    Args:
        hedge_tokens: Set of token_ids that are identified as hedge orders
        quiet: If True, only print summary (skip per-order details)
        live_only: If True, only show orders for live events (for LiveBot)
    """
    if hedge_tokens is None:
        hedge_tokens = set()
    
    min_edge_pct = CONFIG.get("min_edge", 0.05) * 100  # Convert to percentage
    
    # Get all orders from BotState (single source of truth)
    all_orders = bot_state.get_all_open_order_infos()
    
    # In live_only mode, skip printing orders (LiveBot doesn't manage pre-match orders)
    if live_only:
        return
    
    # Compute summary stats
    order_count = len(all_orders)
    total_value = sum(info["price"] * info["size"] for info in all_orders.values())
    
    # Only print per-order details if not quiet
    if not quiet:
        # Pre-pass: resolve fair values via v2 cache fallback
        if odds_service and hasattr(odds_service, 'find_v2_fair_value'):
            import re
            for token_id, info in all_orders.items():
                team_name = info.get("team_name", "")
                # For spread/totals orders, ALWAYS re-lookup because
                # info["fair_value"] might be h2h moneyline (wrong market type)
                has_spread_totals = bool(
                    re.search(r'Spread\s*[-+]?\d', team_name) or
                    re.search(r'O/U\s*[\d.]', team_name)
                )
                if info.get("fair_value") and not has_spread_totals:
                    continue
                
                match_display = info.get("match", "")
                team_name = info.get("team_name", "")
                fb_t1, fb_t2, fb_market, fb_line = "", "", "", 0.0
                
                # Strip game prefix
                clean_display = re.sub(r'^(?:hockey|football|ncaab|counter-strike|khl|ufc|rugby|cricket):\s*', '', match_display, flags=re.IGNORECASE)
                
                # Get teams from BotState match
                cid = info.get("condition_id", "")
                bs_match = bot_state.get_match_by_condition(cid) if cid else None
                if not bs_match and token_id in bot_state._token_to_match:
                    bs_match = bot_state._matches.get(bot_state._token_to_match[token_id])
                if bs_match:
                    fb_t1 = re.sub(r'[:\s]*(?:O/U\s*[\d.]+|Spread\s*[-+]?[\d.]+|\bNo\b)$', '', bs_match.team1 or "").strip()
                    fb_t2 = re.sub(r'[:\s]*(?:O/U\s*[\d.]+|Spread\s*[-+]?[\d.]+|\bNo\b)$', '', bs_match.team2 or "").strip()
                    
                    # FIX: Yes/No markets have team1="X", team2="X No" which both
                    # clean to "X". find_v2_fair_value needs BOTH real teams to match
                    # v2 entries. Extract the real teams from the match_id instead.
                    # CRITICAL: find_v2_fair_value(outcome="No") inverts team1's prob,
                    # so fb_t1 MUST be the base team (the "Will X win?" subject).
                    if fb_t1 and fb_t2 and normalize_team(fb_t1) == normalize_team(fb_t2):
                        base_team_norm = normalize_team(fb_t1)  # The base team before collapse
                        mid_parts = bs_match.match_id.split(":vs:")
                        if len(mid_parts) == 2:
                            # match_id format: "game:teamA:vs:teamB:0xcondition"
                            mid_t1 = mid_parts[0].split(":", 1)[-1]  # strip game prefix
                            mid_t2 = mid_parts[1].split(":")[0]       # strip condition suffix
                            if mid_t1 and mid_t2:
                                # Put base team first — find_v2_fair_value inverts fb_t1's prob
                                mid_t1_norm = normalize_team(mid_t1)
                                if mid_t1_norm == base_team_norm or base_team_norm in mid_t1_norm or mid_t1_norm in base_team_norm:
                                    fb_t1, fb_t2 = mid_t1, mid_t2
                                else:
                                    fb_t1, fb_t2 = mid_t2, mid_t1
                
                # Detect market type
                ou_m = re.search(r'O/U\s*([\d.]+)', clean_display)
                sp_m = re.search(r'Spread.*?\(([-+]?[\d.]+)\)', clean_display)
                will_m = re.search(r'Will (.+?) win', clean_display)
                if ou_m:
                    fb_market = "totals"
                    fb_line = float(ou_m.group(1))
                    vs_m = re.match(r'^(.+?):\s*O/U\s*[\d.]+\s+vs\.?\s+(.+?)$', clean_display)
                    if vs_m and not fb_t1:
                        fb_t1, fb_t2 = vs_m.group(1).strip(), vs_m.group(2).strip()
                    if not vs_m and not fb_t1:
                        vs_m2 = re.match(r'^(.+?)\s+vs\.?\s+(.+?):\s*O/U', clean_display)
                        if vs_m2:
                            fb_t1, fb_t2 = vs_m2.group(1).strip(), vs_m2.group(2).strip()
                elif sp_m:
                    fb_market = "spreads"
                    fb_line = abs(float(sp_m.group(1)))
                elif will_m:
                    fb_market = "h2h"
                    if not fb_t1:
                        fb_t1 = will_m.group(1).strip()
                
                if not fb_t1:
                    vs_m = re.match(r'^(.+?)\s+vs\.?\s+(.+?)$', clean_display)
                    if vs_m:
                        fb_t1, fb_t2 = vs_m.group(1).strip(), vs_m.group(2).strip()
                
                # If no market type detected but we have two teams, it's h2h
                # CRITICAL: without this, find_v2_fair_value matches ANY market type
                # and may return a spread/totals fair value for an h2h order
                if not fb_market and fb_t1 and fb_t2:
                    fb_market = "h2h"
                
                # ALSO check team_name for spread/totals info that match_display doesn't contain
                # e.g., team_name="Jazz: Spread -10.5" but match_display="Bucks vs Jazz"
                tn_sp = re.search(r'Spread\s*([-+]?\d+\.?\d*)', team_name)
                tn_ou = re.search(r'O/U\s*([\d.]+)', team_name)
                if tn_sp:
                    fb_market = "spreads"
                    fb_line = abs(float(tn_sp.group(1)))
                elif tn_ou:
                    fb_market = "totals"
                    fb_line = float(tn_ou.group(1))
                
                # For outcome lookup: strip spread/totals suffix from team_name
                # "Jazz: Spread -10.5" → "Jazz", "Over" stays "Over"
                lookup_outcome = team_name
                if tn_sp:
                    lookup_outcome = re.sub(r'[:\s]*Spread\s*[-+]?\d+\.?\d*$', '', team_name).strip()
                elif tn_ou:
                    lookup_outcome = re.sub(r'[:\s]*O/U\s*[\d.]+$', '', team_name).strip()
                
                # Strip " No" suffix for proper binary adjustment in find_v2_fair_value
                # "NEC No" → "No" so it hits the outcome == "No" branch (1 - fair)
                if lookup_outcome.endswith(" No"):
                    lookup_outcome = "No"
                
                if fb_t1:
                    fv = odds_service.find_v2_fair_value(
                        team1=fb_t1, team2=fb_t2,
                        outcome=lookup_outcome,
                        market_type=fb_market, line=fb_line,
                    )
                    if fv:
                        info["fair_value"] = fv
                        # Write to actual MatchOrder in BotState so reactive handler can evaluate edge
                        # BUT only if OddsService hasn't already pushed fair probs for this match
                        # (OddsService push is more reliable — correct market-type matching)
                        match_obj = bot_state.get_match_by_token(token_id)
                        if match_obj:
                            # Skip if match already has fair probs from OddsService
                            has_fair_probs = (match_obj.fair_prob1 is not None or match_obj.fair_prob2 is not None)
                            if not has_fair_probs:
                                for order in [match_obj.order1, match_obj.order2]:
                                    if order and order.token_id == token_id and order.fair_value is None:
                                        order.fair_value = fv
        
        # Sort: (1) orders with fair values first, (2) good-edge before low-edge (⚠️), (3) by size desc
        def _sort_key(item):
            _, info = item
            fv = info.get("fair_value")
            if not fv:
                return (2, 0, -info.get("size", 0))  # No fair value → last
            edge = (fv - info.get("price", 0)) * 100
            below_threshold = 1 if edge < min_edge_pct else 0
            return (0, below_threshold, -info.get("size", 0))
        sorted_orders = sorted(all_orders.items(), key=_sort_key)
        for token_id, info in sorted_orders:
            fair_value = info.get("fair_value")
            match_display = info.get("match", "")
            team_name = info.get("team_name", "")
            price = info.get("price", 0)
            size = info.get("size", 0)
            
            # Compute edge at print time
            edge_pct = (fair_value - price) * 100 if fair_value else None
            
            fair_str = f" (fair: {fair_value*100:.0f}%)" if fair_value else ""
            
            # Determine order status indicators
            is_hedge = token_id in hedge_tokens
            hedge_str = " 🔒" if is_hedge else ""
            
            # Warning for low edge (applies to both entry and hedge orders)
            if edge_pct is not None:
                warn = " ⚠️" if edge_pct < min_edge_pct else ""
                edge_str = f" | Edge: {edge_pct:+.1f}%{warn}{hedge_str}"
            elif fair_value is None:
                # No fair value available - this is concerning, can't evaluate edge
                edge_str = f"{hedge_str}" if hedge_str else ""
            else:
                edge_str = hedge_str if hedge_str else ""
            
            if match_display:
                print(f"   📋 BUY {size} @ {price:.2f}{fair_str} | {team_name} | {match_display}{edge_str}")
            else:
                print(f"   📋 BUY {size} @ {price:.2f}{fair_str} | {team_name}{edge_str}")
    
    # Print summary (always)
    if order_count > 0:
        dup_msg = f" (cancelled {duplicate_count} duplicates)" if duplicate_count > 0 else ""
        print(f"✅ Hydrated {order_count} active orders, ~${total_value:.2f} at risk{dup_msg}")
    else:
        print(f"✅ No existing open orders found")
    
    print(f"   Active tokens: {len(bot_state._token_to_match)}")
    print(f"   📊 _order_to_match entries: {len(bot_state._order_to_match)}")


async def hydrate_filled_positions(
    bot_state: "BotState",
    executor,
    poly_client=None,
    odds_service: "OddsService" = None,
    quiet: bool = False,
    live_only: bool = False,
):
    """
    Fetch actual positions (shares we own) from Polymarket Data API.
    
    This prevents placing duplicate orders on tokens where we already
    have filled positions (shares we own).
    
    Also detects completed arbitrages (both sides of a market have shares)
    to allow re-entering those markets.
    
    Args:
        poly_client: Optional PolymarketEsportsClient for looking up market info
        odds_service: Optional OddsService for looking up current fair values (for EV calc)
        live_only: If True, only process positions for live events (for LiveBot)
        quiet: If True, suppress verbose per-position output (for periodic re-syncs)
    """
    import aiohttp
    import asyncio
    
    try:
        if not quiet:
            print("🔄 Checking for filled positions...")
        
        # Get user address from executor
        # For proxy wallets (sig_type=2), positions are stored under the funder address
        client = executor._get_client()
        # Use executor's funder address if set (supports separate accounts for LiveBot/SportsBot)
        funder = executor._funder_address or os.getenv("POLYMARKET_FUNDER_ADDRESS")
        if funder:
            user_address = funder  # Use proxy wallet address for positions
        else:
            user_address = client.get_address()  # Fall back to EOA
        
        # Fetch positions from Polymarket Data API with pagination (with retry)
        positions = []
        page_size = 1000
        offset = 0
        max_retries = 3
        
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60, connect=20)
        ) as session:
            while True:
                url = f"https://data-api.polymarket.com/positions?user={user_address}&limit={page_size}&offset={offset}"
                
                page = None
                for attempt in range(max_retries):
                    try:
                        async with session.get(url) as resp:
                            if resp.status != 200:
                                print(f"   ⚠️ Failed to fetch positions: HTTP {resp.status}")
                                return
                            page = await resp.json()
                            break  # Success
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        if attempt < max_retries - 1:
                            delay = 2 * (attempt + 1)
                            await asyncio.sleep(delay)
                        else:
                            raise  # Let outer handler catch it
                
                if not page:
                    break
                positions.extend(page)
                if len(page) < page_size:
                    break  # Last page
                offset += page_size
        
        if not positions:
            print("   ✅ No filled positions found")
            return
        
        # Track market -> tokens and position info for arbitrage detection
        market_tokens: dict = {}  # conditionId -> list of position info
        market_info_cache: dict = {}  # conditionId -> market info
        
        # Get live event tokens to mark live positions
        live_token_ids = await poly_client.get_live_token_ids() if poly_client else set()
        
        # First pass: collect all positions
        position_infos = []
        for pos in positions:
            token_id = pos.get("asset", "")
            size = float(pos.get("size", 0))
            outcome = pos.get("outcome", "Unknown")
            condition_id = pos.get("conditionId", "")
            avg_price = float(pos.get("avgPrice", 0))
            cur_price = float(pos.get("curPrice", 0))  # Current market price
            
            if size <= 0.01:  # Skip negligible positions
                continue
            
            is_live = token_id in live_token_ids
            
            # LIVE_ONLY FILTER: Skip non-live positions early (before arb/hedge detection)
            if live_only and not is_live:
                continue
            
            position_infos.append({
                "token_id": token_id,
                "size": size,
                "outcome": outcome,
                "condition_id": condition_id,
                "avg_price": avg_price,
                "cur_price": cur_price,
                "is_live": is_live,
            })
            
            # Track for arbitrage detection
            if condition_id:
                if condition_id not in market_tokens:
                    market_tokens[condition_id] = []
                market_tokens[condition_id].append({
                    "token_id": token_id,
                    "outcome": outcome,
                    "size": size,
                })
        
        # Second pass: batch lookup market info for opposing team names
        if poly_client and position_infos:
            unique_tokens = list(set(p["token_id"] for p in position_infos))
            
            # Pre-warm L1 cache from Redis via MGET (single round-trip per 200 keys)
            hits = await poly_client.pre_warm_cache(unique_tokens)
            if hits > 0:
                print(f"   ⚡ Redis pre-warm: {hits}/{len(unique_tokens)} tokens cached")
            
            # Fetch remaining (cache misses) in batches of 30
            for i in range(0, len(unique_tokens), 30):
                batch = unique_tokens[i:i+30]
                tasks = [poly_client.get_market_by_token(tid) for tid in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for tid, result in zip(batch, results):
                    if result and not isinstance(result, Exception):
                        cond_id = result.get("condition_id", "")
                        if cond_id:
                            market_info_cache[cond_id] = result
        
        # Third pass: categorize positions into completed, live/ongoing, and upcoming
        # Completed: cur_price >= 0.95 or cur_price <= 0.05 (match decided)
        # The rest are shown individually
        
        completed_positions = []  # (size, avg_price, cur_price, outcome, is_win)
        active_positions = []  # Everything else
        
        for pos in position_infos:
            token_id = pos["token_id"]
            size = pos["size"]
            outcome = pos["outcome"]
            condition_id = pos["condition_id"]
            avg_price = pos["avg_price"]
            cur_price = pos["cur_price"]
            is_live = pos.get("is_live", False)
            
            # NOTE: live_only filtering is done in first pass (before market_tokens is built)
            
            # Get market question for filtering
            market_question = market_info_cache.get(condition_id, {}).get("question", "")
            
            # Skip positions where market info couldn't be fetched (404/429 = resolved/expired market)
            if not market_question:
                print(f"   ⏭️ Skipping resolved/expired position: {outcome} ({size:.1f} shares) - no market info")
                continue
            
            
            # RUGBY FIX: Verify and correct position outcome using market info
            # The positions API can sometimes return stale/incorrect outcome for a token.
            # The market info (from CLOB) is authoritative for what a token represents.
            if market_question and "win?" in market_question.lower():
                market_info = market_info_cache.get(condition_id, {})
                clob_tokens = market_info.get("clobTokenIds", [])
                if isinstance(clob_tokens, str):
                    import json
                    try:
                        clob_tokens = json.loads(clob_tokens)
                    except:
                        clob_tokens = []
                outcomes_list = market_info.get("outcomes", [])
                if isinstance(outcomes_list, str):
                    import json
                    try:
                        outcomes_list = json.loads(outcomes_list)
                    except:
                        outcomes_list = []
                
                # Check which outcome this token ACTUALLY represents
                if token_id in clob_tokens:
                    idx = clob_tokens.index(token_id)
                    market_outcome = outcomes_list[idx] if idx < len(outcomes_list) else None
                    # CRITICAL: If there's a mismatch, trust the market info!
                    if market_outcome and market_outcome.lower() != outcome.lower():
                        print(f"      ⚠️ MISMATCH! Positions API says '{outcome}' but token is actually '{market_outcome}'")
                        print(f"      🔧 FIXING: Using market outcome '{market_outcome}' instead of positions API '{outcome}'")
                        outcome = market_outcome  # FIX: Use the correct outcome
                else:
                    print(f"      ❌ Token NOT FOUND in market's clobTokenIds!")
                    print(f"         token_id: {token_id[:20]}...")
                    print(f"         clob_tokens: {[t[:16] + '...' for t in clob_tokens]}")
            
            # ===== EXTRACT TEAM NAME FROM BINARY MARKET QUESTIONS =====
            # Binary markets (Yes/No) need descriptive team names for BotState tracking.
            # Supported: rugby "Will X win?", weather "temperature in X", stock "X up or down"
            original_outcome = outcome  # Save for debug
            if market_question:
                q_lower = market_question.lower()
                if is_will_win_market(q_lower):
                    # Rugby/football: "Will Harlequins win?" or "Will SE Palmeiras win on DATE?"
                    team_name = extract_will_win_team(market_question)
                    if team_name:
                        if outcome.lower() == "no":
                            outcome = f"{team_name} No"
                        else:
                            outcome = team_name
                elif (" vs " in market_question or " vs. " in market_question
                      or q_lower.startswith("spread:")
                      or q_lower.strip() in ("over", "under")):
                    # NCAAB/sports: use groupItemTitle + event title for descriptive team names
                    group_item = market_info_cache.get(condition_id, {}).get("groupItemTitle", "")
                    if group_item and (group_item.startswith("Spread") or group_item.startswith("O/U")):
                        # Spread/O/U: construct team names matching SpreadBot format
                        # SpreadBot uses: yes_team = f"{team1}: {bin_label}", no_team = f"{team2}"
                        event_title_str = market_info_cache.get(condition_id, {}).get("title", "")
                        source_str = event_title_str or market_question
                        from src.core.match_id import parse_match_question as _pmq
                        _g, et1, et2 = _pmq(source_str)
                        if et1 and et2:
                            if outcome.lower() in ("no", "under"):
                                outcome = et2
                            else:
                                outcome = f"{et1}: {group_item}"
                        else:
                            outcome = group_item if outcome.lower() in ("yes", "over") else f"Not {group_item}"
                    elif group_item:
                        # Non-Spread/O/U groupItemTitle (e.g., team name) — use as-is
                        outcome = group_item if outcome.lower() in ("yes", "over") else f"Not {group_item}"
                    # If no groupItemTitle, outcome stays as-is (team name from winner market)
                else:
                    # Weather/stock/mentions: extract descriptive name from question
                    group_item = market_info_cache.get(condition_id, {}).get("groupItemTitle", "")
                    weather_name = _extract_weather_team_name(market_question, group_item, outcome)
                    if weather_name:
                        outcome = weather_name
                    else:
                        # Try mentions: "Will X say \"Y\" ...?"
                        import re
                        mentions_keywords = ("say ", "said ", "mention ", "name ")
                        if q_lower.startswith("will ") and any(kw in q_lower for kw in mentions_keywords):
                            quoted = re.search(r'["\u201c\u201d]([^"\u201c\u201d]+)["\u201c\u201d]', market_question)
                            if quoted:
                                word = quoted.group(1).strip()
                                if outcome.lower() == "no":
                                    outcome = f"mentions:Not {word}"
                                else:
                                    outcome = f"mentions:{word}"
            
            # FIX: Resolve non-team-name outcomes to actual team names.
            # Esports binary markets use generic labels ("Match Winner", "Not Match Winner")
            # or full match titles ("Not Toronto KOI vs G2 Minnesota - CDL...") as outcomes.
            _is_bad_outcome = (
                outcome in ("Match Winner", "Not Match Winner")
                or (" vs " in outcome)  # Full match title as outcome
            )
            if _is_bad_outcome:
                mi = market_info_cache.get(condition_id, {}) if condition_id else {}
                group_item = mi.get("groupItemTitle", "")
                resolved_team = ""
                
                if group_item and group_item not in ("Match Winner", "Not Match Winner", "Moneyline") and " vs " not in group_item:
                    resolved_team = group_item
                elif market_question:
                    from src.core.match_id import parse_match_question as _pmq
                    _g, t1, t2 = _pmq(market_question)
                    if t1 and t2:
                        clob_tokens_mw = mi.get("clobTokenIds", [])
                        if isinstance(clob_tokens_mw, str):
                            import json
                            try:
                                clob_tokens_mw = json.loads(clob_tokens_mw)
                            except:
                                clob_tokens_mw = []
                        for idx, t in enumerate(clob_tokens_mw):
                            if t == token_id:
                                resolved_team = t1 if idx == 0 else t2
                                break
                
                if resolved_team:
                    outcome = resolved_team
            
            # Skip individual game/round markets (e.g., "Game 1 Winner")
            # We don't track these in BotState as we can't hedge them
            if market_question and is_individual_game_market(market_question):
                # Still show them in the position list, but don't register with BotState
                print(f"   ⚠️ Skipping 'Game X' position: {outcome} @ {avg_price:.2f} ({size:.1f} shares) [YOU HAVE GAME/MAP X MARKET POSITION!]")
                continue
            
            # ===== REGISTER POSITION WITH BOTSTATE =====
            # Token tracking is handled by register_hydrated_position below
            # Find opponent token from market info
            opponent_token = ""
            all_tokens = market_info_cache.get(condition_id, {}).get("clobTokenIds", [])
            if isinstance(all_tokens, str):
                import json
                try:
                    all_tokens = json.loads(all_tokens)
                except:
                    all_tokens = []
            for t in all_tokens:
                if t != token_id:
                    opponent_token = t
                    break
            
            # Event title (from Gamma API) for sports spread/O/U two-team extraction
            event_title = market_info_cache.get(condition_id, {}).get("title", "")
            # Event slug (from Gamma API) for sport classification
            event_slug = market_info_cache.get(condition_id, {}).get("event_slug", "")
            
            bot_state.register_hydrated_position(
                token_id=token_id,
                team=outcome,
                shares=size,
                avg_price=avg_price,
                condition_id=condition_id,
                match_question=market_question,
                opponent_team="",  # Will be filled later
                opponent_token=opponent_token,
                odds_service=odds_service,
                event_title=event_title,
                event_slug=event_slug,
            )
            
            # Get market info for opponent team
            market_info = market_info_cache.get(condition_id, {})
            question = market_info.get("question", "")
            
            # Find opponent from market tokens
            opponent = ""
            for tok_info in market_tokens.get(condition_id, []):
                if tok_info["token_id"] != token_id:
                    opponent = tok_info["outcome"]
                    break
            
            # If opponent not found from our positions, try market info
            if not opponent and market_info:
                outcomes = market_info.get("outcomes", [])
                if isinstance(outcomes, list):
                    for o in outcomes:
                        o_name = o if isinstance(o, str) else o.get("name", "")
                        if o_name and o_name.lower() != outcome.lower():
                            opponent = o_name
                            break
            
            # RUGBY FIX: For "Will X win?" markets, opponent is just "Yes"/"No" from outcomes.
            # Use parse_match_question to look up the actual opponent team from odds.
            q_lower_rb = market_question.lower() if market_question else ""
            if is_will_win_market(q_lower_rb) and odds_service:
                from src.core.match_id import parse_match_question, normalize_team
                game_rb, team1_rb, team2_rb = parse_match_question(market_question, odds_service=odds_service)
                if team1_rb and team2_rb:
                    # Use the actual team names from odds lookup
                    # Strip " No" suffix from outcome for display if present
                    display_team = outcome.removesuffix(" No") if outcome.endswith(" No") else outcome
                    other_team = team2_rb if normalize_team(display_team) == normalize_team(team1_rb) else team1_rb
                    opponent = other_team
            
            # Build display string
            match_display = ""
            if opponent:
                match_display = f"{outcome} vs {opponent}"
            elif question:
                match_display = question
            else:
                match_display = outcome
            
            # Check if match is completed (price near 1.00 or 0.00)
            is_completed = cur_price >= 0.95 or cur_price <= 0.05
            
            if is_completed:
                is_win = cur_price >= 0.95
                completed_positions.append({
                    "size": size,
                    "avg_price": avg_price,
                    "cur_price": cur_price,
                    "outcome": outcome,
                    "match": match_display,
                    "is_win": is_win,
                    "token_id": token_id,
                })
            else:
                # Defer fair value lookup — we'll fill it in a second pass
                # after all positions are registered and fair probs are pushed to BotState
                active_positions.append({
                    "token_id": token_id,
                    "condition_id": condition_id,
                    "size": size,
                    "avg_price": avg_price,
                    "cur_price": cur_price,
                    "fair_value": None,  # Filled in second pass below
                    "is_live": is_live,
                    "outcome": outcome,
                    "match": match_display,
                })
        
        # ===== SECOND PASS: Push fair probs to BotState and fill in fair values =====
        # This MUST happen AFTER all positions are registered in BotState,
        # because _push_fair_probs_to_bot_state needs the matches to exist.
        if odds_service and hasattr(odds_service, '_push_fair_probs_to_bot_state'):
            odds_service._push_fair_probs_to_bot_state()
        
        from src.state.bot_state import get_bot_state
        from src.core.match_id import normalize_team
        bot_state = get_bot_state()
        
        for pos in active_positions:
            if pos["fair_value"] is not None:
                continue
            
            cid = pos.get("condition_id")
            outcome = pos["outcome"]
            
            # PRIMARY: Use BotState fair values (just pushed from both caches)
            # Match by token_id — the most reliable method, avoids all team name format issues
            token_id = pos["token_id"]
            bs_match = bot_state.get_match_by_condition(cid) if cid else None
            
            # Fallback: find match by token_id if condition_id lookup failed
            if not bs_match and token_id in bot_state._token_to_match:
                bs_match_id = bot_state._token_to_match[token_id]
                bs_match = bot_state._matches.get(bs_match_id)
            if bs_match and bs_match.fair_prob1 and bs_match.fair_prob2:
                if token_id == bs_match.token1:
                    pos["fair_value"] = bs_match.fair_prob1
                elif token_id == bs_match.token2:
                    pos["fair_value"] = bs_match.fair_prob2
                else:
                    # Token might not be token1/token2 if hydrated differently.
                    # Fall back to checking if position is on order1 or order2 side.
                    if bs_match.position1 and bs_match.position1.token_id == token_id:
                        pos["fair_value"] = bs_match.fair_prob1
                    elif bs_match.position2 and bs_match.position2.token_id == token_id:
                        pos["fair_value"] = bs_match.fair_prob2
                
                # BINARY ADJUSTMENT for "No" tokens on 3-way sports (football/hockey/rugby).
                # BotState may store raw team probabilities that don't sum to 1.0
                # (e.g., PSV=70%, NEC=14%, draw=16%). For a "Will PSV win?" No token,
                # the fair value should be 1 - PSV_win_prob = 30%, NOT PSV's raw prob.
                # Detect this by checking if fair_prob1+fair_prob2 doesn't sum to ~1.0.
                if (pos["fair_value"] is not None
                        and outcome.endswith(" No")
                        and abs(bs_match.fair_prob1 + bs_match.fair_prob2 - 1.0) > 0.05):
                    # Raw 3-way probs: the value assigned is the team's WIN probability.
                    # For No token, fair = 1 - team_win_prob
                    pos["fair_value"] = 1.0 - pos["fair_value"]
            
            # FALLBACK: Direct OddsService lookup (catches cases BotState missed)
            if pos["fair_value"] is None and odds_service:
                from src.scanning.team_matcher import get_fair_value_for_match
                from src.core.match_id import parse_match_question
                
                match_display = pos["match"]
                game, poly_t1, poly_t2 = parse_teams_from_question(match_display)
                
                if poly_t1 and poly_t2:
                    fv, _ = get_fair_value_for_match(
                        poly_t1, poly_t2, outcome, odds_service, game=game
                    )
                    pos["fair_value"] = fv
                
                if pos["fair_value"] is None and is_will_win_market(match_display.lower()):
                    game_fb, t1_fb, t2_fb = parse_match_question(match_display, odds_service=odds_service)
                    if game_fb and t1_fb and t2_fb:
                        fv, _ = get_fair_value_for_match(
                            t1_fb, t2_fb, outcome, odds_service, game=game_fb
                        )
                        pos["fair_value"] = fv
            
            # FINAL FALLBACK: Direct v2 cache search
            # For positions where text parsing failed or BotState match_id
            # was non-standard (single-team spread/O/U format).
            if pos["fair_value"] is None and odds_service and hasattr(odds_service, 'find_v2_fair_value'):
                import re
                match_display = pos["match"]
                
                # Try to get teams from BotState match first (most reliable)
                fb_t1, fb_t2, fb_market, fb_line = "", "", "", 0.0
                if bs_match:
                    # Strip O/U / Spread suffixes from BotState team names
                    fb_t1 = re.sub(r'[:\s]*(?:O/U\s*[\d.]+|Spread\s*[-+]?[\d.]+|\bNo\b)$', '', bs_match.team1 or "").strip()
                    fb_t2 = re.sub(r'[:\s]*(?:O/U\s*[\d.]+|Spread\s*[-+]?[\d.]+|\bNo\b)$', '', bs_match.team2 or "").strip()
                
                # Detect market type and line from display text
                ou_m = re.search(r'O/U\s*([\d.]+)', match_display)
                sp_m = re.search(r'Spread.*?\(([-+]?[\d.]+)\)', match_display)
                will_m = re.search(r'Will (.+?) win', match_display)
                
                if ou_m:
                    fb_market = "totals"
                    fb_line = float(ou_m.group(1))
                    # Extract teams from "Team1 vs. Team2: O/U X.X" format
                    vs_m = re.match(r'^(.+?)\s+vs\.?\s+(.+?):\s*O/U', match_display)
                    if vs_m and not fb_t1:
                        fb_t1 = vs_m.group(1).strip()
                        fb_t2 = vs_m.group(2).strip()
                elif sp_m:
                    fb_market = "spreads"
                    fb_line = abs(float(sp_m.group(1)))
                elif will_m:
                    fb_market = "h2h"
                    if not fb_t1:
                        fb_t1 = will_m.group(1).strip()
                
                # Parse teams from "Team1 vs. Team2" format (with period)
                if not fb_t1:
                    vs_m = re.match(r'^(.+?)\s+vs\.?\s+(.+?)$', match_display)
                    if vs_m:
                        fb_t1 = vs_m.group(1).strip()
                        fb_t2 = vs_m.group(2).strip()
                
                if fb_t1:
                    fv = odds_service.find_v2_fair_value(
                        team1=fb_t1, team2=fb_t2,
                        outcome=outcome,
                        market_type=fb_market, line=fb_line,
                    )
                    if fv:
                        pos["fair_value"] = fv
        
        # Display completed positions summary (only if not quiet)
        if completed_positions and not quiet:
            wins = [p for p in completed_positions if p["is_win"]]
            losses = [p for p in completed_positions if not p["is_win"]]
            
            total_win_cost = sum(p["size"] * p["avg_price"] for p in wins)
            total_win_return = sum(p["size"] * 1.0 for p in wins)  # $1 per share if won
            total_loss_cost = sum(p["size"] * p["avg_price"] for p in losses)
            
            win_profit = total_win_return - total_win_cost
            total_pnl = win_profit - total_loss_cost
            
            print(f"   💰 Completed matches: {len(wins)} wins, {len(losses)} losses | P&L: ${total_pnl:+.2f}")
            if wins:
                print(f"      ✅ Wins: ${total_win_cost:.2f} → ${total_win_return:.2f} (+${win_profit:.2f})")
            if losses:
                print(f"      ❌ Losses: -${total_loss_cost:.2f}")
        
        # ===== FIRST: Identify arbitrages and calculate hedged shares =====
        # This MUST happen before position display so we can show only net exposure
        completed_arb_count = 0
        total_arb_profit = 0.0
        partial_arbs = []  # Track positions needing hedge
        arbed_shares = {}  # token_id -> shares that are arbed (hedged)
        arb_partners = {}  # token_id -> partner token_id (for display grouping)
        
        # Build set of completed token_ids to exclude from arb counting
        completed_token_ids = set()
        for pos in position_infos:
            if pos["cur_price"] >= 0.95 or pos["cur_price"] <= 0.05:
                completed_token_ids.add(pos["token_id"])
        
        # Identify arbitrages
        for condition_id, tokens in market_tokens.items():
            if len(tokens) >= 2:
                # Both tokens have shares - at least partial arbitrage!
                teams = [t["outcome"] for t in tokens]
                
                # Get position details for each side
                token_details = []
                for tok in tokens:
                    for pos in position_infos:
                        if pos["token_id"] == tok["token_id"]:
                            token_details.append({
                                "token_id": tok["token_id"],
                                "outcome": tok["outcome"],
                                "size": pos["size"],
                                "avg_price": pos["avg_price"],
                                "cur_price": pos["cur_price"],
                            })
                            break
                
                if len(token_details) >= 2:
                    # Check if BOTH sides are completed - skip (already in completed_pnl)
                    all_completed = all(t["token_id"] in completed_token_ids for t in token_details)
                    if all_completed:
                        continue
                    
                    # Calculate locked arb: min shares on both sides
                    sizes = [t["size"] for t in token_details]
                    arb_shares = min(sizes)  # Locked arb = min of both sides
                    
                    # Calculate cost for the locked portion
                    arb_cost = 0.0
                    for t in token_details:
                        arb_cost += arb_shares * t["avg_price"]
                    
                    # Profit = shares won (at $1) - total cost of both sides
                    arb_profit = arb_shares * 1.0 - arb_cost
                    arb_profit_pct = (arb_profit / arb_cost * 100) if arb_cost > 0 else 0.0
                    total_arb_profit += arb_profit
                    completed_arb_count += 1
                    
                    # Track arbed shares and partner tokens
                    for t in token_details:
                        arbed_shares[t["token_id"]] = arb_shares
                        partner = [x["token_id"] for x in token_details if x["token_id"] != t["token_id"]]
                        if partner:
                            arb_partners[t["token_id"]] = partner[0]
                    
                    # Check for remaining shares needing hedge
                    for t in token_details:
                        remaining = t["size"] - arb_shares
                        if remaining > 0.5:  # More than 0.5 shares remaining
                            opponent = [x["outcome"] for x in token_details if x["outcome"] != t["outcome"]]
                            opponent_name = opponent[0] if opponent else "opponent"
                            is_live = False
                            for pi in position_infos:
                                if pi["outcome"] == t["outcome"]:
                                    is_live = pi.get("is_live", False)
                                    break
                            partial_arbs.append({
                                "outcome": t["outcome"],
                                "opponent": opponent_name,
                                "remaining": remaining,
                                "avg_price": t["avg_price"],
                                "cur_price": t["cur_price"],
                                "is_live": is_live,
                            })
        
        # ===== NOW: Display active positions (only unhedged exposure) =====
        # Sort: fair value positions first, then live, then no-odds at the end
        active_positions.sort(key=lambda p: (
            0 if p.get("is_live") else (0 if p.get("fair_value") is not None and p.get("fair_value") > 0 else 1),
            -p["size"],  # Within each group, largest positions first
        ))
        active_count = 0
        for pos in active_positions:
            size = pos["size"]
            token_id = pos["token_id"]
            avg_price = pos["avg_price"]
            cur_price = pos["cur_price"]
            fair_value = pos.get("fair_value")
            match_display = pos["match"]
            is_live = pos.get("is_live", False)
            
            # Calculate NET exposure (subtract hedged/arbed shares)
            hedged_amount = arbed_shares.get(token_id, 0)
            net_size = size - hedged_amount
            
            # Skip if fully hedged (no exposure)
            if net_size < 0.5:
                continue
            
            # For live matches, just show LIVE status (no edge/EV - odds are stale)
            if is_live:
                if not quiet:
                    print(f"   📦 {net_size:.1f} shares @ {avg_price:.2f} (now: {cur_price:.2f}) | {match_display} | 🔴 LIVE")
            # Calculate EV using bookmaker fair probability (not Polymarket price)
            elif fair_value is not None and fair_value > 0:
                ev_pct = (fair_value - avg_price) * 100
                # Show: entry price, fair probability, and edge
                if not quiet:
                    print(f"   📦 {net_size:.1f} shares @ {avg_price:.2f} (fair: {fair_value:.0%}) | {match_display} | Edge: {ev_pct:+.1f}%")
            else:
                # No fair value available - show market price but mark it clearly
                ev_pct = (cur_price - avg_price) * 100 if cur_price > 0 else 0
                if not quiet:
                    print(f"   📦 {net_size:.1f} shares @ {avg_price:.2f} (mkt: {cur_price:.2f}) | {match_display} | EV: {ev_pct:+.1f}% ⚠️ no odds")
            active_count += 1
        
        # ===== Display completed arbs =====
        for condition_id, tokens in market_tokens.items():
            if len(tokens) >= 2:
                teams = [t["outcome"] for t in tokens]
                token_details = []
                for tok in tokens:
                    for pos in position_infos:
                        if pos["token_id"] == tok["token_id"]:
                            token_details.append({
                                "token_id": tok["token_id"],
                                "outcome": tok["outcome"],
                                "size": pos["size"],
                                "avg_price": pos["avg_price"],
                            })
                            break
                
                if len(token_details) >= 2:
                    all_completed = all(t["token_id"] in completed_token_ids for t in token_details)
                    if all_completed:
                        continue
                    
                    sizes = [t["size"] for t in token_details]
                    arb_shares = min(sizes)
                    arb_cost = sum(arb_shares * t["avg_price"] for t in token_details)
                    arb_profit = arb_shares * 1.0 - arb_cost
                    arb_profit_pct = (arb_profit / arb_cost * 100) if arb_cost > 0 else 0.0
                    
                    if not quiet:
                        print(f"   ✅ Completed arb: {teams[0]} vs {teams[1]} ({arb_shares:.0f} shares) | Profit: ${arb_profit:+.2f} ({arb_profit_pct:+.1f}%)")
        
        # Also check for SINGLE-SIDED positions (not partially arbed) that need hedges
        # These are positions where we have shares on ONE side only
        for condition_id, tokens in market_tokens.items():
            if len(tokens) == 1:
                # Only one side of the market - needs a full hedge
                tok = tokens[0]
                for pos in position_infos:
                    if pos["token_id"] == tok["token_id"]:
                        # Skip completed matches (price near 0 or 1) and live matches
                        if pos["cur_price"] >= 0.98 or pos["cur_price"] <= 0.02:
                            continue
                        if pos.get("is_live", False):
                            continue
                        
                        # Get market info for hedge token lookup
                        market_info = market_info_cache.get(condition_id, {})
                        
                        # clobTokenIds might be a JSON string or already parsed
                        all_tokens = market_info.get("clobTokenIds", [])
                        if isinstance(all_tokens, str):
                            import json
                            try:
                                all_tokens = json.loads(all_tokens)
                            except:
                                all_tokens = []
                        
                        outcomes = market_info.get("outcomes", [])
                        if isinstance(outcomes, str):
                            import json
                            try:
                                outcomes = json.loads(outcomes)
                            except:
                                outcomes = []
                        
                        # Debug: log if we can't find market info
                        if not market_info:
                            print(f"   ⚠️ No market info for {tok['outcome']} (condition: {condition_id[:12]}...)")
                        elif len(all_tokens) != 2 or len(outcomes) != 2:
                            print(f"   ⚠️ Incomplete market info for {tok['outcome']}: tokens={len(all_tokens)}, outcomes={len(outcomes)}")
                        
                        # Find the hedge token (the other side)
                        hedge_token = None
                        hedge_team = None
                        if len(all_tokens) == 2 and len(outcomes) == 2:
                            for i, t in enumerate(all_tokens):
                                if t != pos["token_id"]:
                                    hedge_token = t
                                    hedge_team = outcomes[i]
                                    break
                        
                        if hedge_token and hedge_team:
                            partial_arbs.append({
                                "outcome": pos["outcome"],  # Use position's extracted team name (handles "Will X win?" markets)
                                "opponent": hedge_team,
                                "remaining": pos["size"],
                                "avg_price": pos["avg_price"],
                                "cur_price": pos["cur_price"],
                                "is_live": pos.get("is_live", False),
                                "entry_token_id": pos["token_id"],
                                "hedge_token_id": hedge_token,
                                "condition_id": condition_id,
                            })
                        break
        
        # Display remaining shares needing hedging
        # BotState.get_positions_needing_hedge() is the source of truth
        for p in partial_arbs:
            cur_price = p['cur_price']
            is_finished = cur_price <= 0.02 or cur_price >= 0.98
            is_live = p.get('is_live', False)
            
            # Skip finished matches - no point hedging
            if is_finished:
                continue
            
            # Skip live matches - wait for match to end
            if is_live:
                continue
            
            # Skip positions < 5 shares - not worth hedging (Polymarket min order)
            if p['remaining'] < 5.0:
                continue
            
            if not quiet:
                print(f"   🎯 Needs hedge: {p['remaining']:.1f} {p['outcome']} @ {p['avg_price']:.2f} (now {cur_price:.2f}) → need {p['opponent']}")
        
        # Log positions needing hedge from BotState (single source of truth)
        positions_needing_hedge = bot_state.get_positions_needing_hedge()
        if positions_needing_hedge and not quiet:
            print(f"   📋 BotState tracking {len(positions_needing_hedge)} positions for hedge placement")
        
        # Update summary stats
        # Load historical P&L (positions that have been fully resolved/redeemed)
        historical = _load_historical_pnl()
        historical_wins = historical.get("total_wins", 0)
        historical_losses = historical.get("total_losses", 0)
        historical_pnl = historical.get("total_pnl", 0.0)
        historical_tokens = set(historical.get("resolved_tokens", {}).keys())
        
        # Completed: positions at 0/1 price (match ended) - EXCLUDING already-recorded historical
        # Only count NEW completions (not yet in historical file)
        new_completed = [p for p in completed_positions if p.get("token_id", "") not in historical_tokens]
        new_wins = [p for p in new_completed if p["is_win"]]
        new_losses = [p for p in new_completed if not p["is_win"]]
        new_win_cost = sum(p["size"] * p["avg_price"] for p in new_wins)
        new_win_return = sum(p["size"] * 1.0 for p in new_wins)
        new_loss_cost = sum(p["size"] * p["avg_price"] for p in new_losses)
        new_completed_pnl = (new_win_return - new_win_cost) - new_loss_cost
        
        # Total = historical + new session completions
        total_wins = historical_wins + len(new_wins)
        total_losses = historical_losses + len(new_losses)
        total_completed_pnl = historical_pnl + new_completed_pnl
        
        # Active: positions with cur_price not at 0/1
        # Calculate expected profit based on fair value (from bookmaker odds), not market price
        # EXCLUDE shares that are already counted as arbed (locked profit, not expected)
        active_value = sum(p["size"] * p["avg_price"] for p in active_positions)
        active_expected_profit = 0.0
        for p in active_positions:
            fair_value = p.get("fair_value")
            if fair_value and fair_value > 0:
                # Subtract arbed shares - their profit is locked in arb_pnl
                token_id = p.get("token_id", "")
                non_arbed_shares = p["size"] - arbed_shares.get(token_id, 0)
                if non_arbed_shares > 0:
                    # Expected profit = (fair_prob - entry_price) * non-arbed shares
                    active_expected_profit += non_arbed_shares * (fair_value - p["avg_price"])
            # If no fair value, don't include in expected (unknown)
        
        # Store summary stats in BotState (single source of truth)
        # Open orders count/risk are computed live by BotState.get_summary_stats()
        bot_state.set_summary_stats({
            "completed_wins": total_wins,
            "completed_losses": total_losses,
            "completed_pnl": total_completed_pnl,
            "arb_count": completed_arb_count,
            "arb_pnl": total_arb_profit,
            "active_positions": active_count,
            "active_value": active_value,
            "active_expected_profit": active_expected_profit,
        })
        
        total_positions = len(completed_positions) + active_count
        if total_positions > 0:
            print(f"✅ Found {total_positions} filled positions ({active_count} active, {len(completed_positions)} completed)")
            if completed_arb_count > 0:
                print(f"   ({completed_arb_count} completed arbs, total profit: ${total_arb_profit:+.2f})")
            if historical_wins + historical_losses > 0:
                print(f"   📊 Historical: {historical_wins}W / {historical_losses}L → ${historical_pnl:+.2f}")
        else:
            print(f"   ✅ No positions found")
            if historical_wins + historical_losses > 0:
                print(f"   📊 Historical: {historical_wins}W / {historical_losses}L → ${historical_pnl:+.2f}")
            
    except Exception as e:
        print(f"⚠️ Failed to hydrate filled positions: {e}")
        import traceback
        traceback.print_exc()
