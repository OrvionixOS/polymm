#!/usr/bin/env python3
"""
Underdog vs Favorite Performance Analysis

Fetches resolved positions from Polymarket API to compare performance:
- Distribution of bets (favorite vs underdog)
- Win rate by category
- ROI comparison
- Edge comparison

Usage:
    python -m analytics.underdog_analysis          # All-time
    python -m analytics.underdog_analysis --days 30  # Last 30 days
"""
import asyncio
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
import aiohttp
import argparse

from dotenv import load_dotenv
load_dotenv()

from analytics.base import (
    print_header,
    print_subheader,
    print_stat,
    format_percentage,
    format_currency,
    safe_divide,
    EMOJI,
)


# Polymarket's data-api returns HTTP 403 for requests without a User-Agent.
DATA_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; polymm-analytics/1.0; +https://kacho.io)"
}


async def fetch_positions_from_api(days_back: Optional[int] = None) -> Dict[str, Any]:
    """Fetch positions + closed-positions + the activity ledger.

    All three are required for the symmetric, ledger-aware P&L that
    analyze_underdog_performance now delegates to capital_analysis. `/closed-
    positions` alone is winner-biased (it books realized P&L from SELLS only),
    so we also need /positions (resolved losers sit there at ~0) and /activity
    (redeems+sells-buys reconciliation). `days_back` is kept for backward-compat
    but date filtering now happens in analyze (via the start/end window).
    """
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")

    if not funder and not private_key:
        return {"error": "No wallet configured"}

    if funder:
        user_address = funder
    else:
        from eth_account import Account
        user_address = Account.from_key(private_key).address

    async with aiohttp.ClientSession(headers=DATA_API_HEADERS) as session:
        async def paginate(url_base: str, page_size: int, extra: str = "") -> list:
            # IMPORTANT: break only on an EMPTY page, never on a short one.
            # data-api /positions returns short pages MID-stream (seen ~offset
            # 600), so the old `len(page) < page_size` break silently truncated
            # the list — dropping ~435 resolved losing legs and inflating P&L by
            # ~$945. Paging until empty recovers the full ~1049 positions.
            out, offset = [], 0
            while True:
                url = f"{url_base}&limit={page_size}&offset={offset}{extra}"
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            break
                        page = await resp.json()
                except Exception:
                    break
                if not isinstance(page, list) or not page:
                    break
                out.extend(page)
                offset += page_size
                if offset > 20000:  # data-api errors past its window anyway
                    break
            return out

        base = "https://data-api.polymarket.com"
        positions = await paginate(f"{base}/positions?user={user_address}", 500)
        closed_positions = await paginate(f"{base}/v1/closed-positions?user={user_address}", 50)
        activity = await paginate(
            f"{base}/activity?user={user_address}", 500,
            "&sortBy=TIMESTAMP&sortDirection=DESC",
        )

    return {
        "positions": positions,
        "closed_positions": closed_positions,
        "activity": activity,
    }


def get_fair_probs_from_db() -> List[Dict]:
    """get_fair_probs_from_db: stubbed in the open-source build."""
    # Requires a private odds / fair-value source not shipped in this open-source build; the wallet-based analysis runs without it.
    return []


def find_fair_prob(odds_matches: List, outcome: str, opposite_outcome: str) -> Optional[float]:
    """Find fair probability for a position using EXACT normalized matching."""
    from src.core.match_id import normalize_team
    
    if not odds_matches or not outcome or not opposite_outcome:
        return None
    
    outcome_norm = normalize_team(outcome)
    opposite_norm = normalize_team(opposite_outcome)
    
    for odds in odds_matches:
        t1_norm = normalize_team(odds["team1"])
        t2_norm = normalize_team(odds["team2"])
        
        # Check direct order: outcome = team1, opposite = team2
        if outcome_norm == t1_norm and opposite_norm == t2_norm:
            return odds["fair_prob1"]
        
        # Check swapped order: outcome = team2, opposite = team1
        if outcome_norm == t2_norm and opposite_norm == t1_norm:
            return odds["fair_prob2"]
    
    return None


def _parse_end_date(s) -> Optional[datetime]:
    """Parse a position end_date (YYYY-MM-DD or ISO) to an aware UTC datetime."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def analyze_underdog_performance(
    data: Dict[str, Any],
    odds_matches: List,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Favorite vs underdog performance, with CORRECT (symmetric) P&L.

    `/v1/closed-positions` only books realized P&L from SELLS, so it contains
    almost only winners — the losing legs sit in `/positions` at ~0 or live only
    in the activity ledger. Summing closed-positions alone over-states P&L ~3x
    (measured: +$17k vs the true ~$5k). So we DELEGATE the economics to
    capital_analysis.analyze_portfolio — the ledger-aware path that reconciles
    `redeems + sells - buys` per market and recovers losers from /positions —
    and classify its per-leg win/loss details into favorite/underdog buckets.

    Ledger-reconciled markets are booked net per market (no per-outcome entry
    price), so they land in `unknowns`; for any window older than ~7 weeks
    (e.g. Jan-May) there are none and every leg is classified.

    `start`/`end` (aware UTC) optionally restrict to positions whose endDate is
    in [start, end).
    """
    if data.get("error"):
        return {"error": data["error"]}

    from analytics import capital_analysis
    capres = capital_analysis.analyze_portfolio(data)
    if capres.get("error"):
        return {"error": capres["error"]}

    legs = list(capres.get("closed_win_details", [])) + list(capres.get("closed_loss_details", []))

    def _in_window(end_date_str) -> bool:
        if start is None and end is None:
            return True
        d = _parse_end_date(end_date_str)
        if d is None:
            return False
        if start is not None and d < start:
            return False
        if end is not None and d >= end:
            return False
        return True

    legs = [l for l in legs if _in_window(l.get("end_date", ""))]
    if not legs:
        return {"total": 0, "message": "No resolved positions in window"}

    cancelled_count = sum(
        1 for c in capres.get("cancelled_positions", [])
        if _in_window(c.get("end_date", ""))
    )

    # Hedged = both sides of a conditionId present in the (now symmetric) leg set.
    by_condition = defaultdict(list)
    for l in legs:
        cid = l.get("condition_id", "")
        if cid:
            by_condition[cid].append(l)
    hedged_conditions = {cid for cid, v in by_condition.items() if len(v) >= 2}

    favorites = []
    underdogs = []
    unknowns = []
    hedged_positions = []
    unhedged_positions = []
    price_buckets = defaultdict(lambda: {"hedged": [], "unhedged": []})

    for l in legs:
        outcome = l.get("outcome", "")
        avg_price = float(l.get("avg_price", 0) or 0)
        pnl = float(l.get("pnl", 0) or 0)
        cost = float(l.get("cost", 0) or 0)
        is_win = bool(l.get("is_winner"))
        is_hedged = l.get("condition_id", "") in hedged_conditions

        # Favorite/underdog by implied prob: DB fair prob when available, else
        # the entry price (avg_price) as the market-implied probability.
        fair_prob = find_fair_prob(odds_matches, outcome, "")
        if fair_prob is None:
            fair_prob = avg_price if avg_price > 0 else None

        pos_info = {
            "outcome": outcome,
            "pnl": pnl,
            "cost": cost,
            "avg_price": avg_price,
            "is_win": is_win,
            "fair_prob": fair_prob,
            "is_hedged": is_hedged,
        }

        if is_hedged:
            hedged_positions.append(pos_info)
        else:
            unhedged_positions.append(pos_info)

        # Classify by price bucket (using avg_price as entry price)
        if avg_price > 0:
            if avg_price <= 0.30:
                bucket = "10-30%"
            elif avg_price <= 0.50:
                bucket = "30-50%"
            elif avg_price <= 0.70:
                bucket = "50-70%"
            else:
                bucket = "70-90%"

            if is_hedged:
                price_buckets[bucket]["hedged"].append(pos_info)
            else:
                price_buckets[bucket]["unhedged"].append(pos_info)

        # Favorite > 50%, underdog <= 50%. Ledger-reconciled / priceless legs
        # (avg_price == 0) can't be labelled, so they fall to `unknowns`.
        if fair_prob is not None and avg_price > 0:
            if fair_prob > 0.50:
                favorites.append(pos_info)
            else:
                underdogs.append(pos_info)
        else:
            unknowns.append(pos_info)

    def calc_stats(positions: List[Dict]) -> Dict:
        """Calculate stats for a group of positions."""
        if not positions:
            return {"count": 0}
        
        wins = [p for p in positions if p["is_win"]]
        losses = [p for p in positions if not p["is_win"]]
        total_pnl = sum(p["pnl"] for p in positions)
        total_cost = sum(p["cost"] for p in positions)
        
        # Average win/loss magnitude
        avg_win = safe_divide(sum(p["pnl"] for p in wins), len(wins)) if wins else 0
        avg_loss = safe_divide(sum(p["pnl"] for p in losses), len(losses)) if losses else 0
        
        prices = [p["fair_prob"] for p in positions if p.get("fair_prob")]
        avg_price = safe_divide(sum(prices), len(prices)) if prices else None
        
        return {
            "count": len(positions),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": safe_divide(len(wins), len(positions)),
            "total_pnl": total_pnl,
            "total_cost": total_cost,
            "roi": safe_divide(total_pnl, total_cost) if total_cost else None,
            "avg_price": avg_price,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
        }
    
    # Price bucket stats
    bucket_stats = {}
    for bucket in ["10-30%", "30-50%", "50-70%", "70-90%"]:
        data = price_buckets[bucket]
        hedged_count = len(data["hedged"])
        unhedged_count = len(data["unhedged"])
        total = hedged_count + unhedged_count
        bucket_stats[bucket] = {
            "hedged": hedged_count,
            "unhedged": unhedged_count,
            "total": total,
            "hedge_rate": safe_divide(hedged_count, total) if total else 0,
        }
    
    return {
        "total": len(legs),
        "cancelled": cancelled_count,  # Track how many were excluded
        "total_pnl": sum(l.get("pnl", 0) or 0 for l in legs),  # headline sanity (matches capital_analysis)
        "favorites": calc_stats(favorites),
        "underdogs": calc_stats(underdogs),
        "unknowns": len(unknowns),
        "unknown_pnl": sum(p["pnl"] for p in unknowns),
        # New: hedged vs unhedged
        "hedged": calc_stats(hedged_positions),
        "unhedged": calc_stats(unhedged_positions),
        "price_buckets": bucket_stats,
    }


def print_report(results: Dict[str, Any], days_back: Optional[int] = None,
                 start: Optional[datetime] = None, end: Optional[datetime] = None) -> None:
    """Print underdog vs favorite report."""

    if start is not None or end is not None:
        s = start.strftime("%Y-%m-%d") if start else "…"
        e = end.strftime("%Y-%m-%d") if end else "now"
        period = f" ({s} → {e})"
    elif days_back:
        period = f" ({days_back} days)"
    else:
        period = " (All-time)"
    print_header(f"Underdog vs Favorite Analysis{period}")

    if results.get("error"):
        print(f"\n❌ Error: {results['error']}")
        return

    if results.get("message"):
        print(f"\n{results['message']}")
        return

    fav = results.get("favorites", {})
    und = results.get("underdogs", {})
    total = results.get("total", 0)

    # Summary
    print_subheader(f"{EMOJI['chart']} Distribution")
    fav_pct = safe_divide(fav.get("count", 0), total)
    und_pct = safe_divide(und.get("count", 0), total)

    print_stat("Total Resolved", str(total))
    print_stat("Favorites", f"{fav.get('count', 0)} ({format_percentage(fav_pct)})")
    print_stat("Underdogs", f"{und.get('count', 0)} ({format_percentage(und_pct)})")
    if results.get("unknowns", 0) > 0:
        print_stat("Unknown/arb-netted", f"{results['unknowns']} ({format_currency(results.get('unknown_pnl', 0))} P&L, no per-leg price)")
    if results.get("cancelled", 0) > 0:
        print_stat("Excluded", f"{results['cancelled']} (cancelled matches, 50/50 refunds)")
    # Headline sanity: total realized P&L over the classified legs — should line
    # up with capital_analysis (the authoritative ledger-aware number).
    print_stat("Total Realized P&L", f"{format_currency(results.get('total_pnl', 0))} (fav + underdog + unknown, ledger-aware)")
    
    # Comparison table
    print_subheader(f"{EMOJI['target']} Comparison")
    
    print("\n   " + " " * 15 + "FAVORITES".center(20) + "UNDERDOGS".center(20))
    print("   " + "-" * 55)
    
    def row(label: str, fav_val: str, und_val: str):
        print(f"   {label:15} {fav_val:^20} {und_val:^20}")
    
    row("Count", str(fav.get("count", 0)), str(und.get("count", 0)))
    row("Wins", str(fav.get("wins", 0)), str(und.get("wins", 0)))
    row("Losses", str(fav.get("losses", 0)), str(und.get("losses", 0)))
    
    row("Win Rate", 
        format_percentage(fav.get("win_rate")),
        format_percentage(und.get("win_rate")))
    
    row("Avg Fair Prob",
        f"{fav.get('avg_price', 0):.1%}" if fav.get("avg_price") else "N/A",
        f"{und.get('avg_price', 0):.1%}" if und.get("avg_price") else "N/A")
    
    row("Total P&L",
        format_currency(fav.get("total_pnl")),
        format_currency(und.get("total_pnl")))
    
    row("Total Cost",
        format_currency(fav.get("total_cost")),
        format_currency(und.get("total_cost")))
    
    row("ROI",
        format_percentage(fav.get("roi")),
        format_percentage(und.get("roi")))
    
    # Summary
    print_subheader(f"{EMOJI['trophy']} Summary")
    
    fav_roi = fav.get("roi")
    und_roi = und.get("roi")
    
    if fav_roi is not None and und_roi is not None:
        if fav_roi > und_roi + 0.01:
            diff = fav_roi - und_roi
            print(f"   {EMOJI['star']} Favorites outperforming by {format_percentage(diff)} ROI")
        elif und_roi > fav_roi + 0.01:
            diff = und_roi - fav_roi
            print(f"   {EMOJI['star']} Underdogs outperforming by {format_percentage(diff)} ROI")
        else:
            print(f"   {EMOJI['neutral']} Similar performance")
    else:
        print(f"   {EMOJI['neutral']} Insufficient data for comparison")
    
    # Edge analysis
    fav_wr = fav.get("win_rate")
    und_wr = und.get("win_rate")
    
    if fav_wr and und_wr:
        print(f"\n   Win rate: Favorites {format_percentage(fav_wr)} vs Underdogs {format_percentage(und_wr)}")
    
    # ========== NEW: Hedged vs Unhedged Breakdown ==========
    hedged = results.get("hedged", {})
    unhedged = results.get("unhedged", {})
    
    if hedged.get("count", 0) > 0 or unhedged.get("count", 0) > 0:
        print_subheader(f"{EMOJI['money']} Hedged vs Unhedged Breakdown")
        
        print("\n   " + " " * 15 + "HEDGED".center(20) + "UNHEDGED".center(20))
        print("   " + "-" * 55)
        
        def row2(label: str, h_val: str, u_val: str):
            print(f"   {label:15} {h_val:^20} {u_val:^20}")
        
        row2("Count", str(hedged.get("count", 0)), str(unhedged.get("count", 0)))
        row2("Wins", str(hedged.get("wins", 0)), str(unhedged.get("wins", 0)))
        row2("Losses", str(hedged.get("losses", 0)), str(unhedged.get("losses", 0)))
        
        row2("Win Rate",
            format_percentage(hedged.get("win_rate")),
            format_percentage(unhedged.get("win_rate")))
        
        row2("Total P&L",
            format_currency(hedged.get("total_pnl")),
            format_currency(unhedged.get("total_pnl")))
        
        row2("Avg Win",
            format_currency(hedged.get("avg_win")),
            format_currency(unhedged.get("avg_win")))
        
        # Highlight if unhedged losses are bigger
        h_loss = hedged.get("avg_loss", 0)
        u_loss = unhedged.get("avg_loss", 0)
        warn = " ⚠️" if u_loss < h_loss - 1 else ""  # More negative = bigger loss
        row2("Avg Loss",
            format_currency(h_loss),
            f"{format_currency(u_loss)}{warn}")
        
        row2("ROI",
            format_percentage(hedged.get("roi")),
            format_percentage(unhedged.get("roi")))
    
    # ========== NEW: Price Bucket Analysis ==========
    buckets = results.get("price_buckets", {})
    if buckets:
        print_subheader(f"{EMOJI['chart']} Entry Price → Hedge Rate")
        
        print("\n   Entry Price      Hedged   Unhedged   Hedge Rate")
        print("   " + "-" * 50)
        
        for bucket in ["10-30%", "30-50%", "50-70%", "70-90%"]:
            b = buckets.get(bucket, {})
            h = b.get("hedged", 0)
            u = b.get("unhedged", 0)
            rate = b.get("hedge_rate", 0)
            print(f"   {bucket:15} {h:^8} {u:^10} {format_percentage(rate):>10}")
    
    print("")


def _arg_date(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


async def main():
    parser = argparse.ArgumentParser(description="Analyze favorite vs underdog performance")
    parser.add_argument("--days", type=int, default=None, help="Rolling lookback in days (default: all-time)")
    parser.add_argument("--start", type=_arg_date, default=None, help="Window start YYYY-MM-DD (UTC, inclusive)")
    parser.add_argument("--end", type=_arg_date, default=None, help="Window end YYYY-MM-DD (UTC, exclusive)")

    args = parser.parse_args()

    # Resolve the window. Explicit --start/--end win; otherwise --days is a
    # rolling [now-days, now) window.
    start, end = args.start, args.end
    if start is None and end is None and args.days:
        start = datetime.now(timezone.utc) - timedelta(days=args.days)

    # Fetch data (positions + closed + activity ledger)
    data = await fetch_positions_from_api()

    # Get fair probs from DB for classification
    odds_matches = get_fair_probs_from_db()

    # Analyze
    results = analyze_underdog_performance(data, odds_matches, start=start, end=end)

    # Print report
    print_report(results, days_back=args.days, start=start, end=end)


if __name__ == "__main__":
    asyncio.run(main())
