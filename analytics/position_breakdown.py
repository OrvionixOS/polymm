#!/usr/bin/env python3
"""
Detailed Closed Positions Breakdown

Features:
- Splits arb/spec portions when shares don't match
- Groups arbs by match
- Shows fair prob and edge for spec positions (from filled_orders)
"""
import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Dict, List, Any

import aiohttp
from dotenv import load_dotenv
load_dotenv()

import sys
sys.path.insert(0, '.')

from src.core.match_id import normalize_team, parse_match_question


def get_teams_from_title(title: str) -> tuple:
    """Extract normalized team1, team2 from Polymarket title."""
    try:
        parsed = parse_match_question(title)
        if parsed and len(parsed) >= 3:
            # parse_match_question returns (game, team1, team2)
            t1 = normalize_team(parsed[1])
            t2 = normalize_team(parsed[2])
            return (t1, t2)
    except:
        pass
    return ("", "")


async def fetch_all_data() -> Dict[str, Any]:
    """Fetch positions from Polymarket API."""
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    
    if funder:
        user_address = funder
    elif private_key:
        from eth_account import Account
        account = Account.from_key(private_key)
        user_address = account.address
    else:
        print("❌ No wallet configured")
        return {}
    
    async with aiohttp.ClientSession() as session:
        # Fetch open positions with pagination
        open_positions = []
        page_size = 1000
        offset = 0
        
        while True:
            positions_url = f"https://data-api.polymarket.com/positions?user={user_address}&limit={page_size}&offset={offset}"
            async with session.get(positions_url) as resp:
                if resp.status != 200:
                    break
                page = await resp.json()
                if not page:
                    break
                open_positions.extend(page)
                if len(page) < page_size:
                    break
                offset += page_size
        
        closed_positions = []
        offset = 0
        page_size = 50
        while True:
            closed_url = f"https://data-api.polymarket.com/v1/closed-positions?user={user_address}&limit={page_size}&offset={offset}"
            async with session.get(closed_url) as resp:
                if resp.status != 200:
                    break
                page = await resp.json()
                if not page:
                    break
                closed_positions.extend(page)
                if len(page) < page_size:
                    break
                offset += page_size
    
    return {"open": open_positions, "closed": closed_positions}


def fetch_filled_orders_data() -> Dict[str, Dict]:
    """fetch_filled_orders_data: stubbed in the open-source build."""
    # Requires a private odds / fair-value source not shipped in this open-source build; the wallet-based analysis runs without it.
    return {}


def analyze_positions(data: Dict[str, Any], fair_value_lookup: Dict, days_filter: int = None) -> Dict[str, Any]:
    """Analyze positions with proper arb/spec splitting."""
    
    closed_api = data.get("closed", [])
    open_positions = data.get("open", [])
    
    # Calculate cutoff date if days_filter is set
    cutoff_date = None
    if days_filter:
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_filter)).strftime("%Y-%m-%d")
    
    # First pass: collect all positions by condition
    all_raw = []
    by_condition = defaultdict(list)
    
    # Process closed positions
    for pos in closed_api:
        realized_pnl = float(pos.get("realizedPnl", 0) or 0)
        total_bought = float(pos.get("totalBought", 0) or 0)
        avg_price = float(pos.get("avgPrice", 0) or 0)
        cur_price = float(pos.get("curPrice", 0) or 0)
        cost = total_bought * avg_price
        outcome = pos.get("outcome", "Unknown")
        title = pos.get("title", "Unknown")
        condition_id = pos.get("conditionId", "")
        end_date = pos.get("endDate", "")[:10] if pos.get("endDate") else ""

        # Win/loss is the RESOLUTION outcome (curPrice), not realizedPnl. The
        # data-API reports realizedPnl=0 for a position that has resolved but
        # isn't redeemed/settled yet; the old `realizedPnl >= 0` check then
        # mislabeled those — including outright losses — as "✅ Win" with $0 P&L.
        is_winner = cur_price >= 0.99
        resolved = cur_price >= 0.99 or cur_price <= 0.01
        # Prefer the actual realized P&L; if it's still 0 (resolved-but-unsettled)
        # fall back to the resolution value so a real win/loss isn't shown as $0.
        if realized_pnl != 0:
            pnl = realized_pnl
        elif resolved:
            pnl = (total_bought - cost) if is_winner else -cost
        else:
            pnl = 0.0  # genuinely still pending (market not resolved)

        info = {
            "outcome": outcome,
            "title": title,
            "shares": total_bought,
            "avg_price": avg_price,
            "cost": cost,
            "pnl": pnl,
            "is_winner": is_winner,
            "pending": (not resolved),
            "end_date": end_date,
            "condition_id": condition_id,
        }
        if cutoff_date and end_date < cutoff_date:
            continue
        all_raw.append(info)
        if condition_id:
            by_condition[condition_id].append(info)
    
    # Process open positions (resolved wins/losses)
    for pos in open_positions:
        size = float(pos.get("size", 0))
        if size <= 0.01:
            continue
        
        cur_price = float(pos.get("curPrice", 0))
        if cur_price > 0.01 and cur_price < 0.99:
            continue  # Still active
        
        condition_id = pos.get("conditionId", "")
        outcome = pos.get("outcome", "Unknown")
        avg_price = float(pos.get("avgPrice", 0))
        title = pos.get("title", "")
        end_date = str(pos.get("endDate", ""))[:10] if pos.get("endDate") else ""
        
        cost = size * avg_price
        is_winner = cur_price >= 0.99
        pnl = (size - cost) if is_winner else -cost
        
        info = {
            "outcome": outcome,
            "title": title,
            "shares": size,
            "avg_price": avg_price,
            "cost": cost,
            "pnl": pnl,
            "is_winner": is_winner,
            "end_date": end_date,
            "condition_id": condition_id,
        }
        if cutoff_date and end_date < cutoff_date:
            continue
        all_raw.append(info)
        if condition_id:
            by_condition[condition_id].append(info)
    
    # Second pass: split arb vs spec portions
    arb_entries = []  # Grouped by match
    spec_entries = []
    
    for condition_id, positions in by_condition.items():
        outcomes = set(p["outcome"] for p in positions)
        
        if len(outcomes) >= 2:
            # This is an arb - find min shares as arb portion
            min_shares = min(p["shares"] for p in positions)
            
            arb_group = {
                "condition_id": condition_id,
                "title": positions[0]["title"],
                "end_date": max(p["end_date"] for p in positions),
                "sides": [],
            }
            
            for p in positions:
                arb_shares = min_shares
                spec_shares = p["shares"] - min_shares
                
                # Arb portion
                arb_pnl_ratio = arb_shares / p["shares"] if p["shares"] > 0 else 0
                arb_side = {
                    "outcome": p["outcome"],
                    "shares": arb_shares,
                    "avg_price": p["avg_price"],
                    "cost": arb_shares * p["avg_price"],
                    "pnl": p["pnl"] * arb_pnl_ratio,
                    "is_winner": p["is_winner"],
                }
                arb_group["sides"].append(arb_side)
                
                # Spec portion (if any)
                if spec_shares > 0.01:
                    spec_pnl = p["pnl"] * (1 - arb_pnl_ratio)
                    t1_norm, t2_norm = get_teams_from_title(p["title"])
                    team_norm = normalize_team(p["outcome"])
                    pair_sorted = tuple(sorted([t1_norm, t2_norm]))
                    lookup_key = (team_norm, pair_sorted[0], pair_sorted[1])
                    fair_info = fair_value_lookup.get(lookup_key, {})
                    
                    spec_entries.append({
                        "outcome": p["outcome"],
                        "title": p["title"],
                        "shares": spec_shares,
                        "avg_price": p["avg_price"],
                        "cost": spec_shares * p["avg_price"],
                        "pnl": spec_pnl,
                        "is_winner": p["is_winner"],
                        "end_date": p["end_date"],
                        "fair_prob": fair_info.get("fair_prob"),
                        "edge": fair_info.get("edge"),
                        "raw_odds": fair_info.get("raw_odds", {}),
                        "swap_detected": fair_info.get("swap_detected", False),
                        "expected_prob": fair_info.get("expected_prob"),
                    })
            
            # Calculate arb group totals
            arb_group["total_cost"] = sum(s["cost"] for s in arb_group["sides"])
            arb_group["total_pnl"] = sum(s["pnl"] for s in arb_group["sides"])
            arb_group["arb_shares"] = min_shares
            arb_entries.append(arb_group)
        
        else:
            # Pure spec position
            for p in positions:
                t1_norm, t2_norm = get_teams_from_title(p["title"])
                team_norm = normalize_team(p["outcome"])
                pair_sorted = tuple(sorted([t1_norm, t2_norm]))
                lookup_key = (team_norm, pair_sorted[0], pair_sorted[1])
                fair_info = fair_value_lookup.get(lookup_key, {})
                spec_entries.append({
                    "outcome": p["outcome"],
                    "title": p["title"],
                    "shares": p["shares"],
                    "avg_price": p["avg_price"],
                    "cost": p["cost"],
                    "pnl": p["pnl"],
                    "is_winner": p["is_winner"],
                    "end_date": p["end_date"],
                    "fair_prob": fair_info.get("fair_prob"),
                    "edge": fair_info.get("edge"),
                    "raw_odds": fair_info.get("raw_odds", {}),
                    "swap_detected": fair_info.get("swap_detected", False),
                    "expected_prob": fair_info.get("expected_prob"),
                })
    
    return {"arbs": arb_entries, "specs": spec_entries}


def print_report(data: Dict[str, Any]):
    """Print detailed report."""
    
    arbs = data["arbs"]
    specs = data["specs"]
    
    print("\n" + "=" * 120)
    print("CLOSED POSITIONS BREAKDOWN")
    print("=" * 120)
    
    # ========== ARBS (grouped by match) ==========
    arbs.sort(key=lambda x: x["end_date"], reverse=True)
    
    total_arb_pnl = sum(a["total_pnl"] for a in arbs)
    total_arb_cost = sum(a["total_cost"] for a in arbs)
    
    print(f"\n{'='*60}")
    print(f"🔒 ARBITRAGES: {len(arbs)} matches | Cost: ${total_arb_cost:.0f} | P&L: ${total_arb_pnl:+.2f}")
    print(f"{'='*60}")
    
    # Group by date
    arbs_by_date = defaultdict(list)
    for a in arbs:
        arbs_by_date[a["end_date"]].append(a)
    
    for date in sorted(arbs_by_date.keys(), reverse=True):
        day_arbs = arbs_by_date[date]
        day_pnl = sum(a["total_pnl"] for a in day_arbs)
        pnl_emoji = "📈" if day_pnl >= 0 else "📉"
        
        print(f"\n📅 {date} | {len(day_arbs)} arbs | {pnl_emoji} ${day_pnl:+.2f}")
        print("-" * 100)
        
        for arb in sorted(day_arbs, key=lambda x: -abs(x["total_pnl"])):
            title = arb["title"][:50] + "..." if len(arb["title"]) > 50 else arb["title"]
            pnl_str = f"${arb['total_pnl']:+.2f}"
            print(f"  {arb['arb_shares']:.0f} shares | ${arb['total_cost']:.2f} → {pnl_str:>8} | {title}")
            
            for side in arb["sides"]:
                wl = "✅" if side["is_winner"] else "❌"
                team = side["outcome"][:20]
                print(f"      {wl} {team:<20} @ {side['avg_price']:.2f}")
    
    # ========== SPECS ==========
    specs.sort(key=lambda x: x["end_date"], reverse=True)
    
    total_spec_pnl = sum(s["pnl"] for s in specs)
    total_spec_cost = sum(s["cost"] for s in specs)
    spec_wins = sum(1 for s in specs if s["is_winner"])
    spec_losses = len(specs) - spec_wins
    
    print(f"\n{'='*60}")
    print(f"📊 SPECULATIVE: {len(specs)} positions | {spec_wins}W/{spec_losses}L | Cost: ${total_spec_cost:.0f} | P&L: ${total_spec_pnl:+.2f}")
    print(f"{'='*60}")
    
    # Group by date
    specs_by_date = defaultdict(list)
    for s in specs:
        specs_by_date[s["end_date"]].append(s)
    
    for date in sorted(specs_by_date.keys(), reverse=True):
        day_specs = specs_by_date[date]
        day_pnl = sum(s["pnl"] for s in day_specs)
        day_wins = sum(1 for s in day_specs if s["is_winner"])
        day_losses = len(day_specs) - day_wins
        pnl_emoji = "📈" if day_pnl >= 0 else "📉"
        
        print(f"\n📅 {date} | {day_wins}W/{day_losses}L | {pnl_emoji} ${day_pnl:+.2f}")
        print("-" * 130)
        print(f"{'W/L':<4} {'Shares':>7} {'Entry':>6} {'Cost':>8} {'P&L':>9} {'Fair':>6} {'Edge':>7} | Team / Match")
        print("-" * 130)
        
        for s in sorted(day_specs, key=lambda x: -abs(x["pnl"])):
            wl = "✅" if s["is_winner"] else "❌"
            swap = "🔄" if s.get("swap_detected") else ""
            fair_str = f"{s['fair_prob']*100:.0f}%" if s.get("fair_prob") else "  -"
            edge_str = f"{s['edge']*100:+.1f}%" if s.get("edge") else "   -"
            team = s["outcome"][:18]
            title = s["title"][:45] + "..." if len(s["title"]) > 45 else s["title"]
            
            print(f"{wl:<4} {s['shares']:>7.1f} {s['avg_price']:>6.2f} ${s['cost']:>7.2f} ${s['pnl']:>+8.2f} {fair_str:>6} {edge_str:>7} {swap:>2} | {team}: {title}")
            
            # Show raw odds if available
            raw_odds = s.get("raw_odds", {})
            if raw_odds:
                odds_parts = []
                for bookie, data in raw_odds.items():
                    if isinstance(data, dict):
                        o1 = data.get("odds1", 0)
                        o2 = data.get("odds2", 0)
                        fp1 = data.get("fair_prob1", 0)
                        fp2 = data.get("fair_prob2", 0)
                        scraped = data.get("scraped_at", "")
                        age_str = ""
                        if scraped:
                            try:
                                scraped_dt = datetime.fromisoformat(scraped.replace("Z", "+00:00"))
                                age_hours = (datetime.now(timezone.utc) - scraped_dt).total_seconds() / 3600
                                if age_hours < 1:
                                    age_str = f"({age_hours*60:.0f}m ago)"
                                elif age_hours < 48:
                                    age_str = f"({age_hours:.0f}h ago)"
                                else:
                                    age_str = f"({age_hours/24:.0f}d ago)"
                            except:
                                pass
                        bookie_label = f"{bookie}{age_str}" if age_str else bookie
                        odds_parts.append(f"{bookie_label}: {o1:.2f}/{o2:.2f} → {fp1:.0f}%/{fp2:.0f}%")
                if odds_parts:
                    exp_str = f" [expected: {s['expected_prob']*100:.0f}%]" if s.get("expected_prob") else ""
                    swap_warn = " ⚠️ TEAM SWAP BUG" if s.get("swap_detected") else ""
                    print(f"     📊 {' | '.join(odds_parts)}{exp_str}{swap_warn}")
    
    # Summary
    print(f"\n{'=' * 120}")
    print("SUMMARY")
    print(f"{'=' * 120}")
    print(f"  🔒 Arb: {len(arbs)} matches | ${total_arb_pnl:+.2f}")
    print(f"  📊 Spec: {len(specs)} positions | ${total_spec_pnl:+.2f}")
    print(f"  💰 Total P&L: ${total_arb_pnl + total_spec_pnl:+.2f}")
    print()


async def main():
    parser = argparse.ArgumentParser(description="Detailed Closed Positions Breakdown")
    parser.add_argument("--days", type=int, default=None, help="Only show positions from the last N days")
    args = parser.parse_args()
    
    period = f" (last {args.days} days)" if args.days else ""
    print(f"🔄 Fetching positions from Polymarket{period}...")
    data = await fetch_all_data()
    print(f"   API: {len(data.get('closed', []))} closed, {len(data.get('open', []))} open")
    
    print("🔄 Fetching fair values from database...")
    fair_lookup = fetch_filled_orders_data()
    print(f"   Found {len(fair_lookup)} teams with fair value data")
    
    result = analyze_positions(data, fair_lookup, days_filter=args.days)
    print_report(result)


if __name__ == "__main__":
    asyncio.run(main())
