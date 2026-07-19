#!/usr/bin/env python3
"""
Capital Utilization & Portfolio Analysis

Fetches data directly from Polymarket API for accurate metrics:
- Open positions and exposure
- Closed/resolved positions with P&L
- Arbitrage detection
- Expected value calculations

Usage:
    python -m analytics.capital_analysis
"""
import asyncio
import datetime as _dt
import os
from collections import defaultdict
from typing import Dict, Any, Optional
import aiohttp

from dotenv import load_dotenv
load_dotenv()

from analytics.base import (
    print_header,
    print_subheader,
    print_stat,
    format_currency,
    EMOJI,
)

# Polymarket's data-api now returns HTTP 403 for requests without a
# User-Agent header. Send one on every data-api / lb-api call.
DATA_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; polymm-analytics/1.0; +https://kacho.io)"
}


async def fetch_portfolio_from_api() -> Dict[str, Any]:
    """
    Fetch portfolio data from Polymarket Data API.
    
    Uses two endpoints:
    - /positions - current open positions
    - /v1/closed-positions - historical resolved positions
    Also fetches USDC balance from the exchange.
    """
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    
    if not funder and not private_key:
        return {"error": "No wallet configured"}
    
    if funder:
        user_address = funder
    else:
        from eth_account import Account
        account = Account.from_key(private_key)
        user_address = account.address
    
    async with aiohttp.ClientSession(headers=DATA_API_HEADERS) as session:
        # Fetch current positions with pagination.
        # IMPORTANT: break only on an EMPTY page, never on a short one. data-api
        # /positions returns short pages MID-stream (observed ~offset 600), so a
        # `len(page) < page_size` break silently truncated the list at ~600 and
        # dropped ~435 resolved LOSING legs (curPrice~0) — inflating computed
        # P&L by ~$945 vs the leaderboard truth. Paging until empty recovers the
        # full ~1049 positions and the computed total matches the leaderboard.
        positions = []
        page_size = 500
        offset = 0

        while True:
            positions_url = f"https://data-api.polymarket.com/positions?user={user_address}&limit={page_size}&offset={offset}"
            async with session.get(positions_url) as resp:
                if resp.status != 200:
                    return {"error": f"Positions API error: HTTP {resp.status}"}
                page = await resp.json()
                if not page:
                    break
                positions.extend(page)
                offset += page_size
                if offset > 20000:  # safety cap; data-api errors past its window
                    break
        
        # Fetch ALL closed/resolved positions with pagination.
        # Break only on an EMPTY page (data-api can return short pages
        # mid-stream — same trap that truncated /positions). A truncated closed
        # list would drop realized winners and skew sub-window P&L.
        closed_positions = []
        offset = 0
        page_size = 50  # API may cap at 50

        while True:
            closed_url = f"https://data-api.polymarket.com/v1/closed-positions?user={user_address}&limit={page_size}&offset={offset}"
            async with session.get(closed_url) as resp:
                if resp.status != 200:
                    break  # Stop on error

                page = await resp.json()
                if not page:
                    break  # No more results

                closed_positions.extend(page)
                offset += page_size
                if offset > 20000:  # safety cap
                    break

        # Fetch the ACTIVITY ledger (trades + redemptions), newest first.
        #
        # Why we need it. When a position wins it gets redeemed, which removes
        # it from /positions, and /v1/closed-positions only returns recent
        # winners sparsely. The LOSING legs, by contrast, sit in /positions at
        # ~$0 forever (nobody redeems worthless shares). So for recent weeks we
        # see the losses but not the offsetting wins, and the window looks
        # hugely negative. analyze_portfolio uses this ledger to rebuild
        # realized P&L symmetrically (redeemed + sold − bought), per asset.
        activity = []
        offset = 0
        page_size = 500
        while True:
            activity_url = (
                f"https://data-api.polymarket.com/activity?user={user_address}"
                f"&limit={page_size}&offset={offset}"
                f"&sortBy=TIMESTAMP&sortDirection=DESC"
            )
            try:
                async with session.get(activity_url) as resp:
                    if resp.status != 200:
                        break
                    page = await resp.json()
            except Exception:
                break
            if not page:
                break
            activity.extend(page)
            # Break only on an EMPTY page — a short page mid-stream must NOT stop
            # us, or the ledger truncates and the redeemed-winner reconciliation
            # silently fails (recent windows then collapse to losses-only).
            offset += page_size
            if offset > 20000:  # safety cap; data-api errors past its window anyway
                break

    # Fetch the AUTHORITATIVE all-time P&L from Polymarket's leaderboard API.
    #
    # Why this is needed. Polymarket's /v1/closed-positions silently
    # truncates older history — for the SPREAD wallet the earliest visible
    # endDate is ~7 weeks after the wallet was funded, so ~$1.4k of older
    # realized P&L is invisible to us. The methodology in analyze_portfolio
    # is correct in principle (Σ realizedPnl + open-as-loss + open-as-win)
    # but lands on a wrong total when the underlying data is incomplete.
    #
    # lb-api.polymarket.com/profit is computed from Polymarket's internal
    # complete database and matches the Profit/Loss number shown on the UI
    # to the cent. Use it as the source of truth for the headline P&L;
    # keep /v1/closed-positions for per-game/per-day attribution breakdowns
    # (those breakdowns are inherently "recent activity only" anyway).
    leaderboard_pnl_all: Optional[float] = None
    try:
        async with aiohttp.ClientSession(headers=DATA_API_HEADERS) as session:
            url = f"https://lb-api.polymarket.com/profit?window=all&address={user_address}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    if isinstance(payload, list) and payload:
                        leaderboard_pnl_all = float(payload[0].get("amount", 0) or 0)
    except Exception as e:
        # Non-fatal — leaderboard fetch failing falls back to computed total.
        print(f"⚠️ Leaderboard P&L fetch failed: {e}")

    # Fetch fair probs from odds_esports table
    fair_probs = await fetch_fair_probs_from_odds()
    
    # Fetch USDC balance from exchange
    usdc_balance = 0.0
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
        
        host = "https://clob.polymarket.com"
        chain_id = 137
        sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        
        client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=sig_type,
            funder=funder
        )
        
        # Must set API credentials for L2 auth
        if sig_type == 0:
            # EOA mode - derive credentials
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
        else:
            # Proxy mode - try website API credentials first
            api_key = os.getenv("POLYMARKET_API_KEY")
            api_secret = os.getenv("POLYMARKET_API_SECRET")
            api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
            
            if api_key and api_secret and api_passphrase:
                creds = ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=api_passphrase,
                )
                client.set_api_creds(creds)
            else:
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)
        
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=sig_type
        )
        result = client.get_balance_allowance(params)
        usdc_balance = float(result.get("balance", 0)) / 1e6
    except Exception as e:
        import traceback
        print(f"⚠️ Balance fetch failed: {e}")
        traceback.print_exc()
    
    return {
        "positions": positions or [],
        "closed_positions": closed_positions,
        "activity": activity,
        "fair_probs": fair_probs,
        "user_address": user_address,
        "usdc_balance": usdc_balance,
        "leaderboard_pnl_all": leaderboard_pnl_all,
    }


async def fetch_fair_probs_from_odds() -> Dict[str, Dict[str, float]]:
    """fetch_fair_probs_from_odds: stubbed in the open-source build."""
    # Requires a private odds / fair-value source not shipped in this open-source build; the wallet-based analysis runs without it.
    return []


def find_fair_prob_for_position(odds_matches: list, outcome: str, opposite_outcome: str) -> float | None:
    """
    Find fair probability for a position using EXACT normalized matching.
    
    Both teams must match exactly (after normalization).
    Returns fair prob (0-1) for the 'outcome' team, or None if no match.
    """
    from src.core.match_id import normalize_team
    
    if not odds_matches or not outcome or not opposite_outcome:
        return None
    
    outcome_norm = normalize_team(outcome)
    opposite_norm = normalize_team(opposite_outcome)
    
    for odds in odds_matches:
        t1_norm = odds.get("team1_norm") or normalize_team(odds["team1"])
        t2_norm = odds.get("team2_norm") or normalize_team(odds["team2"])
        
        # Check direct order: outcome = team1, opposite = team2
        if outcome_norm == t1_norm and opposite_norm == t2_norm:
            return odds["fair_prob1"]
        
        # Check swapped order: outcome = team2, opposite = team1
        if outcome_norm == t2_norm and opposite_norm == t1_norm:
            return odds["fair_prob2"]
    
    return None


def extract_game_from_title(title: str) -> str:
    """
    Extract game type from Polymarket position title.
    
    Examples:
    - "CS2: Team Vitality vs Virtus.pro" -> "CS2"
    - "Dota 2: Team Spirit vs Gaimin" -> "Dota 2"
    - "United Rugby Championship: Leinster vs Ulster" -> "Rugby"
    - "Premiership Rugby: Bath vs Bristol" -> "Rugby"
    - "NHL: Rangers vs Bruins" -> "Hockey"
    - "IPL: Mumbai Indians vs Chennai Super Kings" -> "Cricket"
    """
    if not title:
        return "Unknown"
    
    # Common game prefixes
    game_patterns = {
        "cs2:": "CS2",
        "counter-strike:": "CS2",
        "dota 2:": "Dota 2",
        "dota2:": "Dota 2",
        "lol:": "LoL",
        "league of legends:": "LoL",
        "valorant:": "Valorant",
        "cod:": "CoD",
        "call of duty:": "CoD",
        "mlbb:": "MLBB",
        "mobile legends:": "MLBB",
        "mobile legends bang bang:": "MLBB",
        "hok:": "HoK",
        "honor of kings:": "HoK",
        "r6:": "R6",
        "rainbow six:": "R6",
        "sc2:": "SC2",
        "starcraft:": "SC2",
        # Rugby tournaments
        "united rugby championship:": "Rugby",
        "urc:": "Rugby",
        "premiership rugby:": "Rugby",
        "rugby premiership:": "Rugby",
        "top 14:": "Rugby",
        "six nations:": "Rugby",
        # Ice hockey
        "nhl:": "Hockey",
        "khl:": "Hockey",
        "ahl:": "Hockey",
        "shl:": "Hockey",
        "czech extraliga:": "Hockey",
        "del:": "Hockey",
        "swiss national league:": "Hockey",
        # Cricket
        "ipl:": "Cricket",
        "cricket:": "Cricket",
        "odi:": "Cricket",
        "t20:": "Cricket",
        "big bash:": "Cricket",
        "test match:": "Cricket",
        "sheffield shield:": "Cricket",
        "lanka premier league:": "Cricket",
        # UFC / MMA
        "ufc:": "UFC",
        "ufc ": "UFC",
        "bellator:": "UFC",
        # Football (Soccer)
        "epl:": "Football",
        "premier league:": "Football",
        "la liga:": "Football",
        "serie a:": "Football",
        "bundesliga:": "Football",
        "ligue 1:": "Football",
        "champions league:": "Football",
        "europa league:": "Football",
        "conference league:": "Football",
        "mls:": "Football",
        "eredivisie:": "Football",
        "liga mx:": "Football",
        "copa libertadores:": "Football",
        "copa sudamericana:": "Football",
        "fa cup:": "Football",
        "dfb-pokal:": "Football",
        "copa del rey:": "Football",
        "coupe de france:": "Football",
        "saudi pro league:": "Football",
        "a-league:": "Football",
        "j-league:": "Football",
        "k-league:": "Football",
        "concacaf:": "Football",
        "conmebol:": "Football",
        "africa cup:": "Football",
    }
    
    title_lower = title.lower()
    for pattern, game in game_patterns.items():
        if title_lower.startswith(pattern):
            return game
    
    # Fallback: Rugby/Cricket binary markets use "Will X win?" format
    if title_lower.startswith("will ") and " win?" in title_lower:
        return "Rugby"
    
    return "Other"



def analyze_prediction_accuracy(closed_positions: list = None) -> Dict[str, Any]:
    """analyze_prediction_accuracy: stubbed in the open-source build."""
    # Requires a private odds / fair-value source not shipped in this open-source build; the wallet-based analysis runs without it.
    return {"message": "Prediction accuracy (Brier calibration) needs the private fair-value log; omitted from the open-source build."}


def analyze_portfolio(data: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze Polymarket portfolio data."""
    
    if data.get("error"):
        return {"error": data["error"]}
    
    positions = data.get("positions", [])
    closed_positions = data.get("closed_positions", [])
    fair_probs = data.get("fair_probs", {})  # Dict of normalized_team_name -> fair_prob
    
    if not positions and not closed_positions:
        return {"total_positions": 0, "message": "No positions found"}
    
    # ========== ANALYZE CLOSED POSITIONS ==========
    closed_wins = []
    closed_losses = []
    cancelled_positions = []  # Cancelled matches refunded at 50¢
    total_closed_pnl = 0.0
    total_cancelled_pnl = 0.0
    
    # Group closed positions by conditionId to detect arbs
    closed_by_condition = defaultdict(list)

    # ===== Activity-ledger realized P&L (recent window) =====
    # /v1/closed-positions only lists positions exited by SELLING. Winners
    # redeemed at $1 are NOT there and have already left /positions, while their
    # losing legs sit in /positions at ~$0. So the position-based path below sees
    # recent losses but not the matching wins, and recent windows look wildly
    # negative. The activity ledger is the only place redemptions live (keyed by
    # conditionId; REDEEM rows carry no asset/outcome). For every market the
    # ledger fully covers we recompute realized straight from cash
    #     realized = redeemed + sold - bought
    # and let the position-based path keep handling everything OLDER than the
    # ledger window (it reaches back only ~7 weeks, so Jan/early-Feb stay as-is).
    activity = data.get("activity", []) or []
    act_cond: Dict[str, Dict[str, Any]] = {}
    for ev in activity:
        cid = ev.get("conditionId")
        if not cid:
            continue
        a = act_cond.get(cid)
        if a is None:
            a = act_cond[cid] = {
                "buys": 0.0, "sells": 0.0, "redeems": 0.0,
                "out_shares": defaultdict(float),  # outcome asset -> bought shares
                "out_cost": defaultdict(float),    # outcome asset -> bought cost
                "title": ev.get("title", ""),
                "event_slug": ev.get("eventSlug", ""),
                "ts": 0,
            }
        usdc = float(ev.get("usdcSize") or 0)
        etype = ev.get("type")
        if etype == "TRADE":
            asset = ev.get("asset", "")
            if ev.get("side") == "BUY":
                a["buys"] += usdc
                a["out_shares"][asset] += float(ev.get("size") or 0)
                a["out_cost"][asset] += usdc
            elif ev.get("side") == "SELL":
                a["sells"] += usdc
        elif etype == "REDEEM":
            a["redeems"] += usdc
        ts = int(ev.get("timestamp") or 0)
        if ts >= a["ts"]:
            a["ts"] = ts
            if ev.get("title"):
                a["title"] = ev.get("title")
            if ev.get("eventSlug"):
                a["event_slug"] = ev.get("eventSlug")

    # Per-conditionId state from /positions: shares still OPEN (mid-price), and
    # whether there's an unclaimed winner (resolved at ~$1 but not yet redeemed).
    open_shares_by_cond: Dict[str, float] = defaultdict(float)
    has_unclaimed_win: Dict[str, bool] = defaultdict(bool)
    for _p in positions:
        _cp = float(_p.get("curPrice", 0))
        _sz = float(_p.get("size", 0))
        if _sz <= 0.01:
            continue
        _cidp = _p.get("conditionId", "")
        if 0.01 < _cp < 0.99 and not _p.get("redeemable"):
            open_shares_by_cond[_cidp] += _sz
        elif _cp >= 0.99:
            has_unclaimed_win[_cidp] = True

    # A ledger-covered market is handled here only when it is FULLY realized:
    #   - we have its buy cost in the ledger (buys > 0), so no phantom gains off a
    #     truncated cost basis;
    #   - nothing left open (no mid-price legs); and
    #   - no unclaimed winner pending redemption (those stay on the position path,
    #     which marks the win, until the redeem shows up in the ledger).
    # The asymmetry is specifically about REDEEMED winners: /v1/closed-positions
    # books realized P&L from SELLS only, so a position redeemed at $1 shows up
    # there as ~0 (the +$0.58 mystery), while its losing arb leg sits in
    # /positions and DOES get counted. So we take over exactly the markets that
    # have a redemption in the ledger - with their full buy cost present - and
    # rebuild them from cash. Everything else (naked losers, sold-out positions,
    # and all of Jan/early-Feb which predates the ledger) keeps its
    # position-based number, so older months are untouched.
    # Realized P&L already booked by /v1/closed-positions, per conditionId. The
    # decisive signal: for a REDEEMED market this is ~0 only when the endpoint
    # failed to book that redemption (the recent-weeks breakage we're fixing).
    # For older, settled months it already holds the correct figure (verified:
    # Feb/Mar/Apr match the ledger to within rounding), so we leave those alone -
    # which also sidesteps the ledger's own buy-truncation at its far edge.
    closed_realized_by_cond: Dict[str, float] = defaultdict(float)
    for _cp2 in closed_positions:
        closed_realized_by_cond[_cp2.get("conditionId", "")] += float(
            _cp2.get("realizedPnl", 0) or 0
        )

    act_resolved_conds = {
        cid for cid, a in act_cond.items()
        if a["redeems"] > 0
        and a["buys"] > 0
        and abs(closed_realized_by_cond.get(cid, 0.0)) < 0.01
        and open_shares_by_cond.get(cid, 0.0) <= 0.01
        and not has_unclaimed_win.get(cid, False)
    }

    for pos in closed_positions:
        realized_pnl = float(pos.get("realizedPnl", 0))
        total_bought = float(pos.get("totalBought", 0))
        avg_price = float(pos.get("avgPrice", 0))
        outcome = pos.get("outcome", "Unknown")
        title = pos.get("title", "")
        condition_id = pos.get("conditionId", "")
        event_slug = pos.get("eventSlug", "")
        end_date = pos.get("endDate", "")[:10] if pos.get("endDate") else ""

        # Ledger-covered markets are realized from the activity ledger below;
        # skip them here so they aren't double-counted.
        if condition_id in act_resolved_conds:
            continue

        cost = total_bought * avg_price
        
        # Detect cancelled matches: payout of 50¢ per share
        # For a cancelled match: realized_pnl = (0.50 - avg_price) * shares
        # So: payout_per_share = (realized_pnl / shares) + avg_price
        if total_bought > 0:
            payout_per_share = (realized_pnl / total_bought) + avg_price
        else:
            payout_per_share = 0
        
        # Check if this is a cancelled match refund (~50¢ payout per share)
        is_cancelled = 0.49 <= payout_per_share <= 0.51 and total_bought > 0
        
        if is_cancelled:
            # Cancelled match - refunded at 50¢ per share
            refund_info = {
                "outcome": outcome,
                "title": title,
                "shares": total_bought,
                "cost": cost,
                "avg_price": avg_price,
                "refund_value": total_bought * 0.50,
                "pnl": realized_pnl,
                "is_cancelled": True,
                "end_date": end_date,
                "condition_id": condition_id,
                "event_slug": event_slug,
            }
            cancelled_positions.append(refund_info)
            total_cancelled_pnl += realized_pnl
            total_closed_pnl += realized_pnl
            if condition_id:
                closed_by_condition[condition_id].append(refund_info)
        else:
            # Normal win/loss
            is_winner = realized_pnl >= 0
            
            closed_info = {
                "outcome": outcome,
                "title": title,
                "shares": total_bought,
                "cost": cost,
                "avg_price": avg_price,
                "pnl": realized_pnl,
                "is_winner": is_winner,
                "end_date": end_date,
                "condition_id": condition_id,
                "event_slug": event_slug,
            }
            
            if is_winner:
                closed_wins.append(closed_info)
            else:
                closed_losses.append(closed_info)
            
            total_closed_pnl += realized_pnl
            
            # Group by condition for arb detection
            if condition_id:
                closed_by_condition[condition_id].append(closed_info)
    
    # ========== ANALYZE OPEN POSITIONS ==========
    # Separate into: active, resolved-losses (curPrice ~= 0), and track for arbs
    active = []
    resolved_from_open = []  # Losses from open positions (curPrice near 0)
    market_positions = defaultdict(list)
    
    for pos in positions:
        size = float(pos.get("size", 0))
        if size <= 0.01:
            continue
        
        token_id = pos.get("asset", "")
        condition_id = pos.get("conditionId", "")

        # Ledger-covered (recent, fully-resolved) markets are realized from the
        # activity ledger below; skip here to avoid double-counting.
        if condition_id in act_resolved_conds:
            continue

        outcome = pos.get("outcome", "Unknown")
        avg_price = float(pos.get("avgPrice", 0))
        cur_price = float(pos.get("curPrice", 0))
        cash_pnl = float(pos.get("cashPnl", 0))
        redeemable = pos.get("redeemable", False)
        event_slug = pos.get("eventSlug", "")
        title = pos.get("title", "")
        
        pos_info = {
            "token_id": token_id,
            "condition_id": condition_id,
            "outcome": outcome,
            "opposite_outcome": pos.get("oppositeOutcome", ""),  # For fair prob lookup
            "title": title,
            "size": size,
            "avg_price": avg_price,
            "cur_price": cur_price,
            "cost": size * avg_price,
            "current_value": size * cur_price,
            "cash_pnl": cash_pnl,
            "redeemable": redeemable,
        }
        
        if condition_id:
            market_positions[condition_id].append(pos_info)
        
        # Check if this is a resolved loss (curPrice near 0, market resolved)
        if cur_price <= 0.01:
            # This is a LOSS - market resolved against us
            loss_info = {
                "outcome": outcome,
                "title": title,
                "shares": size,
                "cost": size * avg_price,
                "avg_price": avg_price,
                "pnl": -(size * avg_price),  # Lost entire cost
                "is_winner": False,
                "end_date": str(pos.get("endDate", ""))[:10] if pos.get("endDate") else "",
                "condition_id": condition_id,  # Needed for arb detection
                "event_slug": event_slug,
            }
            closed_losses.append(loss_info)
            total_closed_pnl += loss_info["pnl"]
            # Add to closed_by_condition for arb detection
            if condition_id:
                closed_by_condition[condition_id].append(loss_info)
        # Check if this is a resolved WIN not yet claimed (curPrice near 1)
        elif cur_price >= 0.99:
            # This is a WIN - market resolved in our favor (not yet redeemed)
            win_info = {
                "outcome": outcome,
                "title": title,
                "shares": size,
                "cost": size * avg_price,
                "avg_price": avg_price,
                "pnl": size * 1.0 - size * avg_price,  # Won $1 per share minus cost
                "is_winner": True,
                "end_date": str(pos.get("endDate", ""))[:10] if pos.get("endDate") else "",
                "condition_id": condition_id,  # Needed for arb detection
                "event_slug": event_slug,
            }
            closed_wins.append(win_info)
            total_closed_pnl += win_info["pnl"]
            # Add to closed_by_condition for arb detection
            if condition_id:
                closed_by_condition[condition_id].append(win_info)
        # Otherwise it's an active position
        else:
            active.append(pos_info)

    # ========== CALCULATE DAILY P&L WITH ARB BREAKDOWN ==========
    # First, detect arbs from closed positions (both sides filled on same condition)
    closed_arb_conditions = set()
    closed_arb_info = {}  # condition_id -> {arb_shares, arb_profit, date, sides: [...]}
    
    for condition_id, positions in closed_by_condition.items():
        if len(positions) >= 2:
            # Both sides filled - this is an arb
            closed_arb_conditions.add(condition_id)
            
            # Calculate arb: min shares is the arbed portion
            arb_shares = min(p["shares"] for p in positions)
            total_arb_cost = sum(p["avg_price"] * arb_shares for p in positions)
            arb_profit = arb_shares * 1.0 - total_arb_cost  # $1 payout per share
            
            # Use the latest end_date from either side
            dates = [p.get("end_date", "") for p in positions if p.get("end_date")]
            end_date = max(dates) if dates else "unknown"
            
            closed_arb_info[condition_id] = {
                "arb_shares": arb_shares,
                "arb_profit": arb_profit,
                "end_date": end_date,
                "positions": positions,
            }
    
    # Now calculate daily P&L with arb/spec breakdown
    daily_pnl = defaultdict(lambda: {
        "pnl": 0.0, "wins": 0, "losses": 0, "cost": 0.0,
        "arb_count": 0, "arb_pnl": 0.0, "spec_pnl": 0.0
    })
    
    # Games P&L breakdown (same structure as daily)
    games_pnl = defaultdict(lambda: {
        "pnl": 0.0, "wins": 0, "losses": 0, "cost": 0.0,
        "arb_count": 0, "arb_pnl": 0.0, "spec_pnl": 0.0
    })
    
    # Track arb conditions per game for arb_count
    games_arb_conditions = defaultdict(set)
    
    # Debug: collect position details per day
    daily_pnl_debug = defaultdict(lambda: {"total_spec_pnl": 0.0, "positions": []})
    
    # Track which arbs we've already counted (to avoid double-counting across sides)
    arb_pnl_counted = set()
    
    # Process each closed position
    all_closed = closed_wins + closed_losses
    for pos in all_closed:
        date = pos.get("end_date", "unknown")
        condition_id = pos.get("condition_id", "")
        game = extract_game_from_title(pos.get("title", ""))
        
        if pos["is_winner"]:
            daily_pnl[date]["wins"] += 1
            games_pnl[game]["wins"] += 1
        else:
            daily_pnl[date]["losses"] += 1
            games_pnl[game]["losses"] += 1
        daily_pnl[date]["cost"] += pos["cost"]
        games_pnl[game]["cost"] += pos["cost"]
        
        # Add total P&L
        daily_pnl[date]["pnl"] += pos["pnl"]
        games_pnl[game]["pnl"] += pos["pnl"]
        
        # Check if this position is part of an arb
        is_arb = condition_id in closed_arb_conditions
        if is_arb:
            arb_info = closed_arb_info[condition_id]
            games_arb_conditions[game].add(condition_id)
            
            # Only count arb P&L once per condition (not per side)
            if condition_id not in arb_pnl_counted:
                arb_pnl_counted.add(condition_id)
                
                # Get the TOTAL P&L for this arb (sum of both sides)
                total_arb_pnl = sum(p["pnl"] for p in arb_info["positions"])
                
                # Arb profit is the pre-calculated guaranteed profit from the arb
                arb_profit = arb_info["arb_profit"]
                
                # Spec P&L is whatever is left (from unbalanced positions)
                spec_pnl = total_arb_pnl - arb_profit
                
                daily_pnl[date]["arb_pnl"] += arb_profit
                daily_pnl[date]["spec_pnl"] += spec_pnl
                
                games_pnl[game]["arb_pnl"] += arb_profit
                games_pnl[game]["spec_pnl"] += spec_pnl
                
                # Debug
                daily_pnl_debug[date]["total_spec_pnl"] += spec_pnl
                daily_pnl_debug[date]["positions"].append({
                    "outcome": f"ARB: {condition_id[:16]}",
                    "pnl": total_arb_pnl,
                    "arb_pnl": arb_profit,
                    "spec_pnl": spec_pnl,
                    "shares": arb_info["arb_shares"],
                    "is_arb": True,
                    "condition_id": condition_id[:16] if condition_id else "",
                })
        else:
            # Pure one-sided position - all spec
            daily_pnl[date]["spec_pnl"] += pos["pnl"]
            games_pnl[game]["spec_pnl"] += pos["pnl"]
            
            # Debug
            daily_pnl_debug[date]["total_spec_pnl"] += pos["pnl"]
            daily_pnl_debug[date]["positions"].append({
                "outcome": pos.get("outcome", "?"),
                "pnl": pos["pnl"],
                "spec_pnl": pos["pnl"],
                "shares": pos["shares"],
                "is_arb": False,
                "condition_id": "",
            })
    
    # Add arb counts per day
    for condition_id, arb_info in closed_arb_info.items():
        date = arb_info["end_date"]
        daily_pnl[date]["arb_count"] += 1
    
    # Add arb counts per game
    for game, conditions in games_arb_conditions.items():
        games_pnl[game]["arb_count"] = len(conditions)

    # ===== Fold in the ledger-realized markets (recent window) =====
    # These conditionIds were skipped in BOTH position-based passes above, so
    # there's no double-counting. We book realized straight from cash
    # (redeemed + sold - bought), split arb vs spec from the per-outcome buys
    # (the hedged min-shares portion is the locked arb), and attribute to the
    # latest ledger event's date. Markets older than the ledger window keep
    # their position-based numbers untouched.
    for cid in act_resolved_conds:
        a = act_cond[cid]
        realized = a["redeems"] + a["sells"] - a["buys"]
        cost = a["buys"]
        date = (
            _dt.datetime.fromtimestamp(a["ts"], _dt.timezone.utc).strftime("%Y-%m-%d")
            if a["ts"] else "unknown"
        )
        game = extract_game_from_title(a["title"])
        legs = [
            (sh, a["out_cost"][asset])
            for asset, sh in a["out_shares"].items() if sh > 0.01
        ]
        if len(legs) >= 2:
            arb_shares = min(sh for sh, _ in legs)
            arb_cost = sum((c / sh) * arb_shares for sh, c in legs)
            arb_profit = arb_shares * 1.0 - arb_cost
            is_arb = True
        else:
            arb_profit = 0.0
            is_arb = False
        spec_pnl = realized - arb_profit
        won = a["redeems"] > 0

        daily_pnl[date]["pnl"] += realized
        daily_pnl[date]["cost"] += cost
        daily_pnl[date]["arb_pnl"] += arb_profit
        daily_pnl[date]["spec_pnl"] += spec_pnl
        games_pnl[game]["pnl"] += realized
        games_pnl[game]["cost"] += cost
        games_pnl[game]["arb_pnl"] += arb_profit
        games_pnl[game]["spec_pnl"] += spec_pnl
        if won:
            daily_pnl[date]["wins"] += 1
            games_pnl[game]["wins"] += 1
        else:
            daily_pnl[date]["losses"] += 1
            games_pnl[game]["losses"] += 1
        if is_arb:
            daily_pnl[date]["arb_count"] += 1
            games_pnl[game]["arb_count"] += 1

        entry = {
            "outcome": "",
            "title": a["title"],
            "shares": sum(sh for sh, _ in legs),
            "cost": cost,
            "avg_price": 0.0,
            "pnl": realized,
            "is_winner": realized >= 0,
            "end_date": date,
            "condition_id": cid,
            "event_slug": a["event_slug"],
            "from_activity": True,
        }
        total_closed_pnl += realized
        if realized >= 0:
            closed_wins.append(entry)
        else:
            closed_losses.append(entry)

    # Sort by date (most recent first)
    daily_pnl_sorted = sorted(daily_pnl.items(), key=lambda x: x[0], reverse=True)
    
    # Sort games by volume (highest first)
    games_pnl_sorted = sorted(games_pnl.items(), key=lambda x: x[1]["cost"], reverse=True)
    
    # ========== DETECT ARBITRAGES ==========
    arbs = []
    arb_token_ids = set()
    
    for condition_id, market_pos in market_positions.items():
        if len(market_pos) >= 2:
            # Has both outcomes - this is an arb
            arb_shares = min(p["size"] for p in market_pos)
            total_cost = sum(p["avg_price"] * arb_shares for p in market_pos)
            locked_profit = arb_shares * 1.0 - total_cost
            locked_profit_pct = (locked_profit / total_cost * 100) if total_cost > 0 else 0
            
            arbs.append({
                "condition_id": condition_id,
                "title": market_pos[0].get("title", ""),
                "outcomes": [p["outcome"] for p in market_pos],
                "arb_shares": arb_shares,
                "total_cost": total_cost,
                "locked_profit": locked_profit,
                "locked_profit_pct": locked_profit_pct,
            })
            
            for p in market_pos:
                arb_token_ids.add(p["token_id"])
    
    # ========== CALCULATE UNHEDGED EXPOSURE ==========
    unhedged = [p for p in active if p["token_id"] not in arb_token_ids]
    
    unhedged_cost = sum(p["cost"] for p in unhedged)
    unhedged_value = sum(p["current_value"] for p in unhedged)
    
    # Calculate Expected Value using fair probs from bookmakers
    # Use find_fair_prob_for_position with both teams for accurate match
    odds_matches = fair_probs  # This is now a list of odds matches
    unhedged_expected = 0.0
    matched_count = 0
    
    # Add fair_prob to each unhedged position
    for p in unhedged:
        outcome = p.get("outcome", "")
        opposite_outcome = p.get("opposite_outcome", "")
        
        # Look up fair prob using both team names
        fair_prob = find_fair_prob_for_position(odds_matches, outcome, opposite_outcome)
        p["fair_prob"] = fair_prob  # Store for display
        
        if fair_prob is not None:
            p["expected_value"] = p["size"] * fair_prob
            p["expected_profit"] = p["expected_value"] - p["cost"]
            unhedged_expected += p["expected_value"]
            matched_count += 1
        else:
            # Fallback to market price if no fair prob available
            p["expected_value"] = p["current_value"]
            p["expected_profit"] = p["current_value"] - p["cost"]
            unhedged_expected += p["current_value"]
    
    # ========== SUMMARY METRICS ==========
    total_open_cost = sum(p["cost"] for p in active)
    total_open_value = sum(p["current_value"] for p in active)
    total_arb_profit = sum(a["locked_profit"] for a in arbs)
    
    # Redeemable positions (resolved but not claimed)
    redeemable = [p for p in positions if p.get("redeemable") and float(p.get("size", 0)) > 0.01]
    redeemable_value = sum(
        float(p.get("size", 0)) * (1.0 if float(p.get("curPrice", 0)) >= 0.99 else 0)
        for p in redeemable
    )
    
    # Calculate totals for closed trades
    total_wins_pnl = sum(w["pnl"] for w in closed_wins)
    total_losses_pnl = sum(l["pnl"] for l in closed_losses)  # Already negative
    total_wins_cost = sum(w["cost"] for w in closed_wins)
    total_losses_cost = sum(l["cost"] for l in closed_losses)
    total_bets_cost = total_wins_cost + total_losses_cost
    
    return {
        "total_open_positions": len(active),
        "total_closed_positions": len(closed_positions),
        
        # USDC Balance
        "usdc_balance": data.get("usdc_balance", 0.0),
        
        # Open positions
        "open_cost": total_open_cost,
        "open_value": total_open_value,
        
        # Closed/resolved trades. `closed_pnl` is the HEADLINE all-time number
        # — prefer Polymarket's authoritative leaderboard figure when we have
        # it, falling back to the per-position sum (which can be off because
        # /v1/closed-positions silently truncates older history; see
        # fetch_portfolio_from_api for the full rationale).
        # `closed_pnl_computed` keeps the raw analyzer total for debugging
        # the gap, and `leaderboard_pnl_all` keeps the leaderboard value
        # explicitly addressable.
        "closed_wins_count": len(closed_wins),
        "closed_losses_count": len(closed_losses),
        "closed_wins_pnl": total_wins_pnl,
        "closed_losses_pnl": total_losses_pnl,
        "closed_pnl": (
            data.get("leaderboard_pnl_all")
            if data.get("leaderboard_pnl_all") is not None
            else total_closed_pnl
        ),
        "closed_pnl_computed": total_closed_pnl,
        "leaderboard_pnl_all": data.get("leaderboard_pnl_all"),
        "total_bets_count": len(closed_wins) + len(closed_losses),
        "total_bets_cost": total_bets_cost,
        "closed_win_details": closed_wins,
        "closed_loss_details": closed_losses,
        
        # Cancelled/refunded matches (50/50 split)
        "cancelled_count": len(cancelled_positions),
        "cancelled_pnl": total_cancelled_pnl,
        "cancelled_cost": sum(p["cost"] for p in cancelled_positions),
        "cancelled_refund": sum(p["refund_value"] for p in cancelled_positions),
        "cancelled_positions": cancelled_positions,
        "daily_pnl": daily_pnl_sorted,  # Day-by-day breakdown
        "games_pnl": games_pnl_sorted,  # Per-game breakdown
        
        # Redeemable (resolved, awaiting claim)
        "redeemable_count": len(redeemable),
        "redeemable_value": redeemable_value,
        
        # Arbitrages
        "arb_count": len(arbs),
        "arb_locked_profit": total_arb_profit,
        "arbs": arbs,
        
        # Unhedged
        "unhedged_count": len(unhedged),
        "unhedged_cost": unhedged_cost,
        "unhedged_value": unhedged_value,
        "unhedged_expected": unhedged_expected,
        "unhedged_positions": unhedged,
        
        # Prediction calibration
        "prediction_accuracy": analyze_prediction_accuracy(closed_positions),
        
        # Debug
        "daily_pnl_debug": dict(daily_pnl_debug),
    }


def print_report(results: Dict[str, Any]) -> None:
    """Print capital analysis report."""
    
    print_header("Capital & Portfolio Analysis (Live)")
    
    if results.get("error"):
        print(f"\n❌ {results['error']}")
        return
    
    if results.get("message"):
        print(f"\n{results['message']}")
        return
    
    # === Portfolio Summary ===
    print_subheader(f"{EMOJI['money']} Portfolio Summary")
    print_stat("Open Positions", str(results["total_open_positions"]))
    print_stat("Closed Positions", str(results["total_closed_positions"]))
    print_stat("Open Cost Basis", f"{format_currency(results['open_cost'])} (total $ spent on open positions)")
    print_stat("Open Current Value", f"{format_currency(results['open_value'])} (market value at current prices)")
    
    # === Closed Trades (Historical) ===
    print_subheader(f"{EMOJI['chart']} Closed Trades (Historical)")
    total_bets = results["total_bets_count"]
    total_bets_cost = results["total_bets_cost"]
    print_stat("Total Bets", f"{total_bets} ({format_currency(total_bets_cost)})")
    
    wins_count = results["closed_wins_count"]
    wins_pnl = results["closed_wins_pnl"]
    print_stat("  Wins", f"{wins_count} ({format_currency(wins_pnl)})")
    
    losses_count = results["closed_losses_count"]
    losses_pnl = results["closed_losses_pnl"]
    print_stat("  Losses", f"{losses_count} ({format_currency(losses_pnl)})")
    
    pnl = results["closed_pnl"]
    pnl_pct = (pnl / total_bets_cost * 100) if total_bets_cost > 0 else 0
    emoji = EMOJI["up"] if pnl >= 0 else EMOJI["down"]
    
    # Calculate total arb/spec breakdown from daily data
    daily_pnl = results.get("daily_pnl", [])
    total_arb_pnl = sum(stats.get("arb_pnl", 0) for _, stats in daily_pnl)
    total_spec_pnl = sum(stats.get("spec_pnl", 0) for _, stats in daily_pnl)
    total_arb_count = sum(stats.get("arb_count", 0) for _, stats in daily_pnl)
    
    # Show arb/spec breakdown on Realized P&L line
    arb_str = f"🔒{total_arb_count} +${total_arb_pnl:.2f}" if total_arb_count > 0 else ""
    spec_emoji = EMOJI["up"] if total_spec_pnl >= 0 else EMOJI["down"]
    spec_str = f"{spec_emoji}${total_spec_pnl:.2f}"
    breakdown = f" | arb: {arb_str} | spec: {spec_str}" if total_arb_count > 0 else ""
    
    print_stat("Realized P&L", f"{emoji} {format_currency(pnl)} ({pnl_pct:.1f}% of bets){breakdown}")
    
    # Win rate
    if total_bets > 0:
        win_rate = wins_count / total_bets * 100
        print_stat("Win Rate", f"{win_rate:.1f}%")
    
    # === Daily P&L with Arb breakdown ===
    daily_pnl = results.get("daily_pnl", [])
    if daily_pnl:
        print("\n   Daily breakdown:")
        for date, stats in daily_pnl[:7]:  # Last 7 days
            if date == "unknown" or not date:
                continue
            pnl = stats["pnl"]
            cost = stats.get("cost", 0)
            wins = stats["wins"]
            losses = stats["losses"]
            arb_count = stats.get("arb_count", 0)
            arb_pnl = stats.get("arb_pnl", 0)
            spec_pnl = stats.get("spec_pnl", 0)
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0
            
            emoji = EMOJI["up"] if pnl >= 0 else EMOJI["down"]
            arb_str = f"🔒{arb_count} +${arb_pnl:.2f}" if arb_count > 0 else ""
            spec_emoji = EMOJI["up"] if spec_pnl >= 0 else EMOJI["down"]
            spec_str = f"{spec_emoji}${spec_pnl:.2f}"
            
            breakdown = f" | arb: {arb_str} | spec: {spec_str}" if arb_count > 0 else f" | spec: {spec_str}"
            print(f"   {emoji} {date}: {format_currency(pnl)} ({pnl_pct:+.1f}%) on ${cost:.0f} | {wins}W/{losses}L{breakdown}")
    
    # === Games P&L with Arb breakdown ===
    games_pnl = results.get("games_pnl", [])
    if games_pnl:
        print("\n   Games breakdown:")
        for game, stats in games_pnl:
            if game == "Unknown" or not game:
                continue
            pnl = stats["pnl"]
            cost = stats.get("cost", 0)
            wins = stats["wins"]
            losses = stats["losses"]
            arb_count = stats.get("arb_count", 0)
            arb_pnl = stats.get("arb_pnl", 0)
            spec_pnl = stats.get("spec_pnl", 0)
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0
            
            emoji = EMOJI["up"] if pnl >= 0 else EMOJI["down"]
            arb_str = f"🔒{arb_count} +${arb_pnl:.2f}" if arb_count > 0 else ""
            spec_emoji = EMOJI["up"] if spec_pnl >= 0 else EMOJI["down"]
            spec_str = f"{spec_emoji}${spec_pnl:.2f}"
            
            breakdown = f" | arb: {arb_str} | spec: {spec_str}" if arb_count > 0 else f" | spec: {spec_str}"
            print(f"   {emoji} {game:10s}: {format_currency(pnl)} ({pnl_pct:+.1f}%) on ${cost:.0f} | {wins}W/{losses}L{breakdown}")
    
    # === Redeemable ===
    if results["redeemable_count"] > 0:
        print_subheader(f"{EMOJI['target']} Awaiting Redemption")
        print_stat("Positions to Claim", str(results["redeemable_count"]))
        print_stat("Claimable Value", format_currency(results["redeemable_value"]))
    
    # === Arbitrages ===
    print_subheader(f"{EMOJI['target']} Locked Arbitrages")
    print_stat("Active Arbs", str(results["arb_count"]))
    if results["arb_count"] > 0:
        print_stat("Locked Profit", format_currency(results["arb_locked_profit"]))
        print("\n   Details:")
        for arb in results["arbs"][:15]:  # Limit display
            outcomes = " vs ".join(arb["outcomes"])
            print(f"   🔒 {arb['arb_shares']:.1f} shares | {outcomes}")
            print(f"      Cost: {format_currency(arb['total_cost'])} → Profit: {format_currency(arb['locked_profit'])} ({arb['locked_profit_pct']:.1f}%)")
        if len(results["arbs"]) > 15:
            print(f"   ... and {len(results['arbs']) - 15} more")
    
    # === Unhedged Exposure ===
    print_subheader(f"{EMOJI['warning']} Unhedged Exposure")
    print_stat("Unhedged Positions", str(results["unhedged_count"]))
    if results["unhedged_count"] > 0:
        print_stat("Cost Basis", format_currency(results["unhedged_cost"]))
        print_stat("Current Value", f"{format_currency(results['unhedged_value'])} (at market prices)")
        print_stat("Expected Value", f"{format_currency(results['unhedged_expected'])} (at fair probs)")
        
        unrealized = results["unhedged_value"] - results["unhedged_cost"]
        unrealized_ev = results["unhedged_expected"] - results["unhedged_cost"]
        emoji = EMOJI["up"] if unrealized >= 0 else EMOJI["down"]
        emoji_ev = EMOJI["up"] if unrealized_ev >= 0 else EMOJI["down"]
        print_stat("Unrealized P&L", f"{emoji} {format_currency(unrealized)} market / {emoji_ev} {format_currency(unrealized_ev)} EV")
        
        print("\n   Top positions:")
        sorted_positions = sorted(results["unhedged_positions"], key=lambda x: x["cost"], reverse=True)
        for pos in sorted_positions[:10]:
            unrealized = pos["current_value"] - pos["cost"]
            emoji = EMOJI["up"] if unrealized >= 0 else EMOJI["down"]
            
            fair_prob = pos.get("fair_prob")
            expected_profit = pos.get("expected_profit", 0)
            
            print(f"   {emoji} {pos['size']:.1f}x {pos['outcome']}")
            if fair_prob is not None:
                ev_emoji = EMOJI["up"] if expected_profit >= 0 else EMOJI["down"]
                edge = (fair_prob - pos['avg_price']) * 100
                print(f"      Entry: {pos['avg_price']:.2f} | Fair: {fair_prob*100:.0f}% | Edge: {edge:+.1f}% | EV: {ev_emoji}{format_currency(expected_profit)}")
            else:
                print(f"      Entry: {pos['avg_price']:.2f} → Now: {pos['cur_price']:.2f} ({format_currency(unrealized)}) ⚠️ no odds")
        
        if len(results["unhedged_positions"]) > 10:
            print(f"   ... and {len(results['unhedged_positions']) - 10} more")
    
    # === Cancelled Matches (50/50 Refunds) ===
    cancelled_count = results.get("cancelled_count", 0)
    if cancelled_count > 0:
        print_subheader("⚖️ Cancelled Matches (50/50 Refunds)")
        cancelled_pnl = results.get("cancelled_pnl", 0)
        cancelled_cost = results.get("cancelled_cost", 0)
        cancelled_refund = results.get("cancelled_refund", 0)
        
        print_stat("Cancelled Positions", str(cancelled_count))
        print_stat("Total Cost", format_currency(cancelled_cost))
        print_stat("Refund (50¢/share)", format_currency(cancelled_refund))
        
        pnl_emoji = EMOJI["up"] if cancelled_pnl >= 0 else EMOJI["down"]
        print_stat("P&L from Cancellations", f"{pnl_emoji} {format_currency(cancelled_pnl)}")
        
        # Show top cancelled positions
        cancelled_positions = results.get("cancelled_positions", [])
        if cancelled_positions:
            print("\n   Details:")
            for pos in cancelled_positions[:10]:
                pnl = pos.get("pnl", 0)
                emoji = EMOJI["up"] if pnl >= 0 else EMOJI["down"]
                print(f"   {emoji} {pos['outcome'][:35]}")
                print(f"      Entry: ${pos['avg_price']:.2f} | {pos['shares']:.1f} shares | Cost: {format_currency(pos['cost'])} → Refund: {format_currency(pos['refund_value'])} | P&L: {format_currency(pnl)}")
            if len(cancelled_positions) > 10:
                print(f"   ... and {len(cancelled_positions) - 10} more")
    
    # === Prediction Accuracy ===
    prediction_acc = results.get("prediction_accuracy", {})
    if prediction_acc and not prediction_acc.get("message"):
        print_subheader(f"{EMOJI['target']} Prediction Calibration")
        
        brier = prediction_acc.get("brier_score", 0)
        total = prediction_acc.get("total_predictions", 0)
        
        # Brier score interpretation
        if brier < 0.15:
            quality = "🥇 Excellent"
        elif brier < 0.20:
            quality = "🥈 Good"
        elif brier < 0.25:
            quality = "🥉 Fair"
        else:
            quality = "⚠️ Poor"
        
        print_stat("Brier Score", f"{brier:.3f} ({quality})")
        print_stat("Total Predictions", str(total))
        print("   (Brier: 0 = perfect, 0.25 = random guessing)")
        
        # Calibration by bucket
        calibration = prediction_acc.get("calibration", {})
        if calibration:
            print("\n   Calibration (predicted → actual win rate):")
            for bucket in sorted(calibration.keys()):
                data = calibration[bucket]
                expected = data["expected"] * 100
                actual = data["actual"] * 100
                count = data["count"]
                diff = data["diff"] * 100
                
                # Indicator for calibration quality
                if abs(diff) < 5:
                    indicator = "✅"
                elif abs(diff) < 10:
                    indicator = "〰️"
                else:
                    indicator = "⚠️"
                
                print(f"   {indicator} {bucket:2d}-{bucket+10:2d}% predicted → {actual:.0f}% actual ({count} bets) [{diff:+.0f}% diff]")
        
        # By side
        by_side = prediction_acc.get("by_side", {})
        if by_side:
            print(f"\n   By order type:")
            for side, brier_val in sorted(by_side.items(), key=lambda x: x[1]):
                print(f"      {side}: Brier={brier_val:.3f}")
    
    print("")


async def main():
    """Run capital analysis using Polymarket API."""
    import sys
    debug_mode = "--debug" in sys.argv
    
    data = await fetch_portfolio_from_api()
    results = analyze_portfolio(data)
    
    # Add prediction accuracy analysis (pass closed positions for outcome lookup)
    closed_positions = data.get("closed_positions", [])
    prediction_acc = analyze_prediction_accuracy(closed_positions)
    results["prediction_accuracy"] = prediction_acc
    
    print_report(results)
    
    # Debug: Show spec loss breakdown for most recent day with issues
    if debug_mode:
        print("\n" + "=" * 60)
        print("DEBUG: Spec P&L Attribution Details")
        print("=" * 60)
        
        daily_details = results.get("daily_pnl_debug", {})
        for date, details in sorted(daily_details.items(), reverse=True)[:2]:
            print(f"\n📅 {date}:")
            print(f"   Total spec P&L: ${details['total_spec_pnl']:.2f}")
            print(f"   Positions contributing to spec P&L:")
            for pos in details["positions"][:20]:
                arb_flag = "ARB" if pos["is_arb"] else "SPEC"
                print(f"   [{arb_flag}] {pos['outcome'][:30]:30} | P&L: ${pos['pnl']:+.2f} | spec: ${pos['spec_pnl']:+.2f} | shares: {pos['shares']:.1f}")


if __name__ == "__main__":
    asyncio.run(main())

