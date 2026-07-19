#!/usr/bin/env python3
"""
Realized P&L by month — arb vs directional.

Uses the same arb/spec splitting math as `spec_loss_investigation.py`
(the canonical example in this repo):

  - For each `conditionId` we resolved, look at how many shares we held on
    each outcome at settlement.
  - `min_shares` across outcomes is the BALANCED arb portion (risk locked
    at entry — the pair pays $1 per matched share).
  - Anything above `min_shares` on the over-bought side is the SPEC
    (directional) portion — naked exposure that resolved either +shares
    or -cost.
  - For an unbalanced arb partial:
        spec_pnl = realized_pnl × (spec_shares / total_shares)
        arb_pnl  = realized_pnl − spec_pnl   (per-row)
  - For a pure-spec position (no opposite outcome held):
        spec_pnl = realized_pnl
        arb_pnl  = 0
  - For a perfectly balanced arb (spec_shares ≤ 0.5):
        spec_pnl = 0
        arb_pnl  = realized_pnl

Aggregated to monthly totals (using `endDate` for the resolution month).

Usage:
    python -m analytics.realized_pnl_by_month
    python -m analytics.realized_pnl_by_month --funder 0x...
    python -m analytics.realized_pnl_by_month --live
"""
import argparse
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from analytics.base import (
    print_header, print_subheader,
    format_currency, safe_divide,
)

load_dotenv()


def fetch_closed_positions(funder: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    offset = 0
    page_size = 50
    while True:
        url = (
            "https://data-api.polymarket.com/v1/closed-positions"
            f"?user={funder}&limit={page_size}&offset={offset}"
        )
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 Chrome/130",
                                       "Accept": "application/json"}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"closed-positions HTTP {r.status_code}: {r.text[:200]}")
        page = r.json()
        if not page:
            break
        out.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return out


def fetch_open_positions(funder: str) -> List[Dict[str, Any]]:
    """Open positions endpoint. Includes resolved markets the user hasn't
    claimed payouts on yet — those still report curPrice = 0/1 even though
    they're not in /closed-positions. position_breakdown.py imputes PnL
    from curPrice for those.

    IMPORTANT: `/positions` silently caps at 500 results regardless of the
    limit param, so we must use page_size <= 500 — otherwise the
    "len(page) < page_size" break fires on the first page and we miss
    every subsequent page. This bug ate the realized-loss tail in earlier
    runs (wallet has 1000+ open-but-resolved unclaimed positions, only
    the first 500 came through). See capital_analysis.py:56-57 for the
    same warning."""
    out: List[Dict[str, Any]] = []
    offset = 0
    page_size = 100  # ≤ 500 hard cap
    while True:
        url = (
            "https://data-api.polymarket.com/positions"
            f"?user={funder}&limit={page_size}&offset={offset}"
        )
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 Chrome/130",
                                       "Accept": "application/json"}, timeout=30)
        if r.status_code != 200:
            break
        page = r.json()
        if not page:
            break
        out.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return out


def normalize_open_resolved(open_positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turn 'open but resolved' positions into closed-position-shaped rows.

    Mirrors the stale-CTF imputation pattern in
    analytics/underdog_analysis.py:260-275 and position_breakdown.py:290-:
      - Keep only positions whose curPrice is 0 (lost) or 1 (won).
      - Compute synthetic realizedPnl: shares*(1-avgPrice) if won; else -cost.
      - Carry totalBought = size for the splitter."""
    out: List[Dict[str, Any]] = []
    for p in open_positions:
        try:
            size = float(p.get("size") or 0)
            cur = float(p.get("curPrice") or 0)
            avg = float(p.get("avgPrice") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0.01:
            continue
        # Active (not yet resolved) — skip; we only impute the resolved ones.
        if 0.02 < cur < 0.98:
            continue
        cost = size * avg
        pnl = (size * 1.0 - cost) if cur >= 0.98 else (-cost)
        synth = dict(p)
        synth["totalBought"] = size
        synth["realizedPnl"] = pnl
        # Already has avgPrice, curPrice, conditionId, outcome, title, endDate.
        out.append(synth)
    return out


def is_cancelled_market(p: Dict[str, Any]) -> bool:
    """Cancelled-market detection via payout-per-share — mirrors the
    canonical method in analytics/capital_analysis.py:532-541.

    For a cancelled match (50/50 refund), the effective settlement price
    per share is ~$0.50:
        realized_pnl   = (settlement_price - avg_price) * shares
        payout_per_share = realized_pnl / shares + avg_price
                         = settlement_price

    Cancelled when 0.49 <= payout_per_share <= 0.51. We do NOT use
    curPrice for this check — the API can ship curPrice = 0/1/0.5/etc
    for cancelled markets depending on which side is queried, while
    payout-per-share unambiguously identifies the refund.

    These rows DO get included in the total realized P&L (the wallet
    really did clear that cash), but they go into a SEPARATE 'cancelled'
    bucket — they are not arbs and not directional bets."""
    try:
        rp = float(p.get("realizedPnl") or 0)
        shares = float(p.get("totalBought") or p.get("size") or 0)
        avg = float(p.get("avgPrice") or 0)
    except (TypeError, ValueError):
        return False
    if shares <= 0:
        return False
    payout = rp / shares + avg
    return 0.49 <= payout <= 0.51


def month_of(ts: Any) -> Optional[str]:
    if isinstance(ts, str) and len(ts) >= 7 and ts[4] == "-":
        return ts[:7]
    try:
        return datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m")
    except (TypeError, ValueError):
        return None


def split_arb_spec(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For each position row, compute arb_pnl + spec_pnl using the same
    splitting rule as spec_loss_investigation.py. Returns a flat list of
    enriched rows."""
    by_cond: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in positions:
        cid = p.get("conditionId")
        if cid:
            by_cond[cid].append(p)

    enriched: List[Dict[str, Any]] = []
    for cid, rows in by_cond.items():
        outcomes = {r.get("outcome") for r in rows}
        if len(outcomes) >= 2:
            # Unbalanced or balanced arb
            shares_by_outcome = {
                r.get("outcome"): float(r.get("totalBought") or r.get("size") or 0)
                for r in rows
            }
            min_shares = min(shares_by_outcome.values())
            for r in rows:
                total = float(r.get("totalBought") or r.get("size") or 0)
                pnl = float(r.get("realizedPnl") or 0)
                cost = float(r.get("avgPrice") or 0) * total
                spec_shares = max(0.0, total - min_shares)
                if total <= 0 or spec_shares <= 0.5:
                    # this row is fully arb (no spec excess)
                    enriched.append({
                        **r,
                        "_arb_pnl": pnl, "_spec_pnl": 0.0,
                        "_arb_cost": cost, "_spec_cost": 0.0,
                        "_arb_shares": total, "_spec_shares": 0.0,
                        "_is_partial_arb": False,
                    })
                else:
                    spec_ratio = spec_shares / total
                    spec_pnl = pnl * spec_ratio
                    arb_pnl = pnl - spec_pnl
                    spec_cost = cost * spec_ratio
                    arb_cost = cost - spec_cost
                    enriched.append({
                        **r,
                        "_arb_pnl": arb_pnl, "_spec_pnl": spec_pnl,
                        "_arb_cost": arb_cost, "_spec_cost": spec_cost,
                        "_arb_shares": min_shares, "_spec_shares": spec_shares,
                        "_is_partial_arb": True,
                    })
        else:
            # Pure spec — single outcome
            for r in rows:
                total = float(r.get("totalBought") or r.get("size") or 0)
                pnl = float(r.get("realizedPnl") or 0)
                cost = float(r.get("avgPrice") or 0) * total
                enriched.append({
                    **r,
                    "_arb_pnl": 0.0, "_spec_pnl": pnl,
                    "_arb_cost": 0.0, "_spec_cost": cost,
                    "_arb_shares": 0.0, "_spec_shares": total,
                    "_is_partial_arb": False,
                })
    return enriched


def run(funder: str) -> None:
    print_header("Realized P&L by month — arb vs directional")
    print(f"  funder: {funder}")
    closed_raw = fetch_closed_positions(funder)
    open_raw = fetch_open_positions(funder)
    print(f"  /v1/closed-positions (raw):       {len(closed_raw):,}")
    print(f"  /positions (raw):                 {len(open_raw):,}")

    # Imputed resolved positions from /positions — those stuck unclaimed,
    # not yet in /closed-positions but with curPrice=0 or 1.
    imputed_resolved = normalize_open_resolved(open_raw)
    print(f"  /positions resolved (imputed):    {len(imputed_resolved):,}")

    combined_raw = closed_raw + imputed_resolved

    # Split into normal vs cancelled-match — both are kept and bucketed
    # separately. Mirrors capital_analysis.py's bookkeeping.
    cancelled = [p for p in combined_raw if is_cancelled_market(p)]
    normal = [p for p in combined_raw if not is_cancelled_market(p)]
    print(f"  cancelled-match rows (in totals): {len(cancelled):,}")
    print(f"  normal rows feeding splitter:     {len(normal):,}")

    if not normal and not cancelled:
        return

    rows = split_arb_spec(normal)

    by_month: Dict[str, Dict[str, float]] = defaultdict(lambda: {
        "arb_pnl": 0.0, "arb_cost": 0.0, "arb_n": 0, "arb_won_n": 0,
        "spec_pnl": 0.0, "spec_cost": 0.0, "spec_n": 0, "spec_won_n": 0,
        "can_pnl": 0.0, "can_cost": 0.0, "can_n": 0,
    })

    skipped_no_month = 0
    for r in rows:
        m = month_of(r.get("endDate")) or month_of(r.get("timestamp"))
        if not m:
            skipped_no_month += 1
            continue
        cur = float(r.get("curPrice") or 0)
        won = cur >= 0.99
        d = by_month[m]
        if r["_arb_shares"] > 0:
            d["arb_pnl"] += r["_arb_pnl"]
            d["arb_cost"] += r["_arb_cost"]
            d["arb_n"] += 1
            if won:
                d["arb_won_n"] += 1
        if r["_spec_shares"] > 0.5:
            d["spec_pnl"] += r["_spec_pnl"]
            d["spec_cost"] += r["_spec_cost"]
            d["spec_n"] += 1
            if won:
                d["spec_won_n"] += 1

    # Cancelled match P&L by month
    for p in cancelled:
        m = month_of(p.get("endDate")) or month_of(p.get("timestamp"))
        if not m:
            skipped_no_month += 1
            continue
        try:
            pnl = float(p.get("realizedPnl") or 0)
            shares = float(p.get("totalBought") or p.get("size") or 0)
            avg = float(p.get("avgPrice") or 0)
        except (TypeError, ValueError):
            continue
        d = by_month[m]
        d["can_pnl"] += pnl
        d["can_cost"] += shares * avg
        d["can_n"] += 1

    print_subheader("Monthly P&L: arb vs directional vs cancelled (canonical split)")
    print(f"\n  {'month':<10} "
          f"{'arb pos':>8} {'arb PnL':>12} {'arb ROI':>9}  "
          f"{'dir pos':>8} {'dir PnL':>12} {'dir ROI':>9}  "
          f"{'can pos':>8} {'can PnL':>12}  "
          f"{'TOTAL':>12}")
    print(f"  {'-'*128}")

    grand = defaultdict(float)
    for m in sorted(by_month.keys()):
        d = by_month[m]
        a_roi = safe_divide(d["arb_pnl"], d["arb_cost"]) * 100
        s_roi = safe_divide(d["spec_pnl"], d["spec_cost"]) * 100
        total = d["arb_pnl"] + d["spec_pnl"] + d["can_pnl"]
        print(
            f"  {m:<10} "
            f"{d['arb_n']:>8} {format_currency(d['arb_pnl']):>12} {a_roi:>8.2f}%  "
            f"{d['spec_n']:>8} {format_currency(d['spec_pnl']):>12} {s_roi:>8.2f}%  "
            f"{d['can_n']:>8} {format_currency(d['can_pnl']):>12}  "
            f"{format_currency(total):>12}"
        )
        for k, v in d.items():
            grand[k] += v

    print(f"  {'-'*128}")
    a_roi_g = safe_divide(grand["arb_pnl"], grand["arb_cost"]) * 100
    s_roi_g = safe_divide(grand["spec_pnl"], grand["spec_cost"]) * 100
    total_g = grand["arb_pnl"] + grand["spec_pnl"] + grand["can_pnl"]
    print(
        f"  {'TOTAL':<10} "
        f"{int(grand['arb_n']):>8} {format_currency(grand['arb_pnl']):>12} {a_roi_g:>8.2f}%  "
        f"{int(grand['spec_n']):>8} {format_currency(grand['spec_pnl']):>12} {s_roi_g:>8.2f}%  "
        f"{int(grand['can_n']):>8} {format_currency(grand['can_pnl']):>12}  "
        f"{format_currency(total_g):>12}"
    )

    if skipped_no_month:
        print(f"\n  ({skipped_no_month} rows skipped — no parseable endDate)")

    # Directional PnL by game (canonical math)
    print_subheader("Directional (spec) PnL by game")
    by_game: Dict[str, Dict[str, float]] = defaultdict(lambda: {"pnl": 0.0, "cost": 0.0, "n": 0, "won_n": 0})
    for r in rows:
        if r["_spec_shares"] <= 0.5:
            continue
        title = (r.get("title") or "").lower()
        game = "other"
        for needle, tag in [
            ("counter-strike", "cs2"), ("counter strike", "cs2"), ("cs2", "cs2"),
            ("league of legends", "lol"), ("lol", "lol"),
            ("dota 2", "dota2"), ("dota2", "dota2"),
            ("valorant", "valorant"), ("rainbow six", "r6"),
            ("call of duty", "cod"), ("ncaa", "ncaab"),
            ("nba", "nba"), ("basketball", "basketball"),
            ("nfl", "nfl"), ("football", "football"),
            ("tennis", "tennis"), ("ufc", "ufc"), ("mma", "ufc"),
            ("starcraft", "sc2"), ("hok", "hok"), ("overwatch", "overwatch"),
            ("honor of kings", "hok"), ("mobile legends", "mlbb"),
        ]:
            if needle in title:
                game = tag
                break
        g = by_game[game]
        g["pnl"] += r["_spec_pnl"]
        g["cost"] += r["_spec_cost"]
        g["n"] += 1
        if float(r.get("curPrice") or 0) >= 0.99:
            g["won_n"] += 1

    print(f"\n  {'game':<14} {'N':>6} {'cost':>12} {'PnL':>12} {'ROI':>9} {'win%':>8}")
    print(f"  {'-'*70}")
    for g, d in sorted(by_game.items(), key=lambda kv: kv[1]["pnl"]):
        roi = safe_divide(d["pnl"], d["cost"]) * 100
        win = safe_divide(d["won_n"], d["n"]) * 100
        print(f"  {g:<14} {d['n']:>6} {format_currency(d['cost']):>12} "
              f"{format_currency(d['pnl']):>12} {roi:>8.1f}% {win:>7.1f}%")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--funder", type=str, default=None)
    p.add_argument("--live", action="store_true")
    args = p.parse_args()
    funder = args.funder or (
        os.getenv("POLYMARKET_FUNDER_ADDRESS_LIVE") if args.live
        else os.getenv("POLYMARKET_FUNDER_ADDRESS")
    )
    if not funder:
        print("❌ no funder")
        return 2
    run(funder)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
