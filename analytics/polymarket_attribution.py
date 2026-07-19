#!/usr/bin/env python3
"""All-time arb/spec P&L attribution from the Polymarket on-chain record.

Polymarket's public Data API silently truncates older history
(/v1/closed-positions caps around the most recent ~4.5k positions,
/activity around ~3.4k events). For a wallet that's traded longer than
about 2 months that means the per-position arb-vs-spec attribution
computed in `capital_analysis.py` lands on the wrong total — by ~$1.5k
on the SPREAD wallet at time of writing. Reading the public Goldsky
subgraphs directly (orderbook + activity) gives the full history with
no auth and no cap, so this module rebuilds attribution from scratch
against the on-chain truth.

What it produces, per funder address:

  - arb_pnl       — sum of profit on markets where the wallet held
                    BOTH outcomes of a binary or 2+ outcomes of a
                    multi-outcome market (hedged exposure)
  - spec_pnl      — sum of profit on markets where the wallet held a
                    single outcome (directional exposure)
  - arb_count     — number of conditions classified as arb
  - spec_count    — number of conditions classified as spec
  - total_pnl     — arb_pnl + spec_pnl, expected to match Polymarket's
                    leaderboard API to within rounding/fees

Per-condition P&L = (Σ USDC received from sells)
                  + (Σ USDC received from redemptions)
                  − (Σ USDC paid for buys)
                  − (Σ fees)

That's a strictly cash-flow definition — equivalent to what Polymarket's
own leaderboard exposes — and therefore reconciles by construction. It
deliberately does NOT use `avgPrice × shares`-style per-position cost
basis (which is what the data-api `realizedPnl` field is built from);
that's the path that loses precision once the API truncates.
"""
import asyncio
import os
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

import aiohttp


GOLDSKY_PROJECT = "project_cl6mb8i9h0003e201j6li0diw"
ORDERBOOK_URL = (
    f"https://api.goldsky.com/api/public/{GOLDSKY_PROJECT}"
    "/subgraphs/orderbook-subgraph/0.0.1/gn"
)
ACTIVITY_URL = (
    f"https://api.goldsky.com/api/public/{GOLDSKY_PROJECT}"
    "/subgraphs/activity-subgraph/0.0.4/gn"
)
POSITIONS_URL = (
    f"https://api.goldsky.com/api/public/{GOLDSKY_PROJECT}"
    "/subgraphs/positions-subgraph/0.0.7/gn"
)
LB_API_URL = "https://lb-api.polymarket.com/profit"

# USDC has 6 decimals on Polygon. Outcome-token amounts are also in 1e6
# atomic units per the Polymarket CTF Exchange wire format.
USDC_DECIMALS = 1_000_000

# Subgraph page size. Goldsky's free tier hits a Postgres statement
# timeout above ~1k rows when filtering by an unindexed field; 1000 is
# the sweet spot for the orderbook subgraph here.
PAGE_SIZE = 1000


# -------------------------------------------------------------------------
# GraphQL helpers


async def _gql(
    session: aiohttp.ClientSession,
    url: str,
    query: str,
    retries: int = 5,
) -> Dict[str, Any]:
    """POST a GraphQL query, raise on errors, return `data`.

    Goldsky has occasional 502s and statement-timeout 200s during heavy
    traffic. Retry on HTTP failures with exponential backoff before
    bubbling up; GraphQL-level errors are raised immediately since those
    indicate a real query problem (bad field name, wrong cursor type)
    that won't fix itself by retrying.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            async with session.post(
                url,
                json={"query": query},
                timeout=aiohttp.ClientTimeout(total=60),
                headers={"User-Agent": "polymm-attribution/1.0"},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"HTTP {resp.status} from {url}: {body[:200]}"
                    )
                payload = await resp.json()
                if "errors" in payload:
                    # Goldsky surfaces Postgres statement timeouts as 200s
                    # with an `errors` array. Retry those; raise the rest.
                    err_msg = str(payload["errors"])
                    if "statement timeout" in err_msg or "canceling" in err_msg:
                        raise RuntimeError(f"transient gql timeout: {err_msg[:120]}")
                    raise RuntimeError(f"GraphQL errors: {payload['errors']}")
                return payload["data"]
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                await asyncio.sleep(2**attempt)
    assert last_exc is not None
    raise last_exc


# -------------------------------------------------------------------------
# Data fetch — orderFilledEvents (maker + taker sides) and redemptions


async def fetch_all_fills(session: aiohttp.ClientSession, funder: str) -> List[Dict[str, Any]]:
    """All OrderFilledEvent rows where `funder` was maker OR taker.

    The orderbook subgraph indexes both `maker` and `taker` separately,
    so we run two paginated streams and stitch them. Each fill is tagged
    with `_role` ("maker" or "taker") so `classify_fill` knows which
    side our funder was on.
    """
    funder = funder.lower()
    out: List[Dict[str, Any]] = []
    for role in ("maker", "taker"):
        cursor = "0"
        while True:
            query = f"""
            {{
              orderFilledEvents(
                first: {PAGE_SIZE},
                orderBy: timestamp,
                orderDirection: asc,
                where: {{ {role}: "{funder}", timestamp_gt: "{cursor}" }}
              ) {{
                id timestamp maker taker
                makerAssetId takerAssetId
                makerAmountFilled takerAmountFilled
                fee
              }}
            }}
            """
            rows = (await _gql(session, ORDERBOOK_URL, query))["orderFilledEvents"]
            if not rows:
                break
            for r in rows:
                r["_role"] = role
            out.extend(rows)
            cursor = rows[-1]["timestamp"]
            if len(rows) < PAGE_SIZE:
                break
    return out


async def fetch_all_redemptions(session: aiohttp.ClientSession, funder: str) -> List[Dict[str, Any]]:
    """All Redemption rows for `funder`. Paginated by timestamp."""
    funder = funder.lower()
    out: List[Dict[str, Any]] = []
    cursor = "0"
    while True:
        query = f"""
        {{
          redemptions(
            first: {PAGE_SIZE},
            orderBy: timestamp,
            orderDirection: asc,
            where: {{ redeemer: "{funder}", timestamp_gt: "{cursor}" }}
          ) {{
            id timestamp redeemer condition payout
          }}
        }}
        """
        rows = (await _gql(session, ACTIVITY_URL, query))["redemptions"]
        if not rows:
            break
        out.extend(rows)
        cursor = rows[-1]["timestamp"]
        if len(rows) < PAGE_SIZE:
            break
    return out


# -------------------------------------------------------------------------
# Fill semantics — which side of the trade was the funder on


def classify_fill(fill: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce an OrderFilledEvent to a uniform (direction, token, usdc, fee).

    The Polymarket CTF Exchange uses assetId=0 to mean USDC; any other
    assetId is an outcome-token id. So whichever side has assetId=0
    paid USDC, and the other side paid tokens. Cross-reference with
    whether our funder is the `maker` or the `taker` to land on
    BUY (our funder paid USDC) vs SELL (our funder paid tokens).
    """
    role = fill["_role"]  # "maker" or "taker"
    m_asset = fill["makerAssetId"]
    t_asset = fill["takerAssetId"]
    m_amount = int(fill["makerAmountFilled"])
    t_amount = int(fill["takerAmountFilled"])
    fee = int(fill.get("fee") or 0)
    ts = int(fill["timestamp"])

    if role == "maker":
        if m_asset == "0":
            # We're maker giving USDC → buying tokens
            return {"direction": "BUY", "token_id": t_asset,
                    "usdc_atomic": m_amount, "tokens_atomic": t_amount,
                    "fee_atomic": fee, "timestamp": ts}
        else:
            # We're maker giving tokens → selling for USDC
            return {"direction": "SELL", "token_id": m_asset,
                    "usdc_atomic": t_amount, "tokens_atomic": m_amount,
                    "fee_atomic": fee, "timestamp": ts}
    # taker
    if t_asset == "0":
        # We're taker giving USDC → buying tokens
        return {"direction": "BUY", "token_id": m_asset,
                "usdc_atomic": t_amount, "tokens_atomic": m_amount,
                "fee_atomic": fee, "timestamp": ts}
    # We're taker giving tokens → selling for USDC
    return {"direction": "SELL", "token_id": t_asset,
            "usdc_atomic": m_amount, "tokens_atomic": t_amount,
            "fee_atomic": fee, "timestamp": ts}


# -------------------------------------------------------------------------
# tokenId → conditionId mapping


async def map_tokens_to_conditions(
    session: aiohttp.ClientSession, token_ids: List[str]
) -> Dict[str, str]:
    """Resolve a batch of CLOB token_ids to their conditionIds.

    The positions-subgraph has a dedicated `TokenIdCondition` entity
    mapping every CTF outcome-token id to its parent condition. We
    paginate by `id_in: [...]` batches. Gamma was the obvious first
    candidate but returns `[]` for tokens whose markets have aged out of
    its index — the subgraph keeps everything since 2026-04-28 (the V2
    contract migration; pre-V2 trades aren't indexed at all so they're
    silently dropped and end up as "unmapped" singletons, which is the
    honest accounting given we can't classify them).
    """
    CHUNK = 100
    mapping: Dict[str, str] = {}
    unique = list({t for t in token_ids if t and t != "0"})
    for i in range(0, len(unique), CHUNK):
        chunk = unique[i : i + CHUNK]
        ids_lit = ", ".join(f'"{t}"' for t in chunk)
        query = (
            f"{{ tokenIdConditions(first: {CHUNK}, where: {{ id_in: [{ids_lit}] }}) "
            "{ id condition { id } } }"
        )
        try:
            data = await _gql(session, POSITIONS_URL, query)
        except Exception:
            continue
        for row in data.get("tokenIdConditions", []) or []:
            cid = (row.get("condition") or {}).get("id")
            if row.get("id") and cid:
                mapping[row["id"]] = cid
    return mapping


# -------------------------------------------------------------------------
# Attribution


def compute_attribution(
    fills: List[Dict[str, Any]],
    redemptions: List[Dict[str, Any]],
    token_to_condition: Dict[str, str],
) -> Dict[str, Any]:
    """Group classified fills + redemptions by conditionId, then split arb vs spec.

    Per-condition P&L = Σ sells + Σ redemptions − Σ buys − Σ fees,
    all in USDC. A condition is "arb" if the wallet ever held positions
    on 2+ distinct token_ids of that condition (both YES and NO of a
    binary, or 2+ outcomes of a multi-outcome). Otherwise it's "spec".

    Unmapped tokens (Gamma had no metadata — usually an obsolete or
    bridged market) are grouped under their own token_id as a synthetic
    condition. They count as spec; flagging them more loudly is left to
    the caller.
    """
    # Aggregate by condition first
    by_condition: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "usdc_in_atomic": 0,    # sells
            "usdc_out_atomic": 0,   # buys
            "fee_atomic": 0,
            "redeemed_atomic": 0,
            "token_ids": set(),
        }
    )

    for fill in fills:
        c = classify_fill(fill)
        cond = token_to_condition.get(c["token_id"], f"unmapped:{c['token_id']}")
        agg = by_condition[cond]
        if c["direction"] == "BUY":
            agg["usdc_out_atomic"] += c["usdc_atomic"]
        else:
            agg["usdc_in_atomic"] += c["usdc_atomic"]
        agg["fee_atomic"] += c["fee_atomic"]
        agg["token_ids"].add(c["token_id"])

    for r in redemptions:
        cond = r["condition"]
        by_condition[cond]["redeemed_atomic"] += int(r["payout"] or 0)

    # Now classify each condition and roll up
    arb_pnl = 0.0
    spec_pnl = 0.0
    arb_count = 0
    spec_count = 0
    per_condition: List[Dict[str, Any]] = []
    for cond, agg in by_condition.items():
        pnl_atomic = (
            agg["usdc_in_atomic"]
            + agg["redeemed_atomic"]
            - agg["usdc_out_atomic"]
            - agg["fee_atomic"]
        )
        pnl_usdc = pnl_atomic / USDC_DECIMALS
        is_arb = len(agg["token_ids"]) >= 2
        if is_arb:
            arb_pnl += pnl_usdc
            arb_count += 1
        else:
            spec_pnl += pnl_usdc
            spec_count += 1
        per_condition.append(
            {
                "condition_id": cond,
                "is_arb": is_arb,
                "n_token_ids": len(agg["token_ids"]),
                "pnl_usdc": pnl_usdc,
                "buys_usdc": agg["usdc_out_atomic"] / USDC_DECIMALS,
                "sells_usdc": agg["usdc_in_atomic"] / USDC_DECIMALS,
                "redeemed_usdc": agg["redeemed_atomic"] / USDC_DECIMALS,
                "fees_usdc": agg["fee_atomic"] / USDC_DECIMALS,
            }
        )

    return {
        "arb_pnl": arb_pnl,
        "spec_pnl": spec_pnl,
        "arb_count": arb_count,
        "spec_count": spec_count,
        "total_pnl": arb_pnl + spec_pnl,
        "per_condition": per_condition,
    }


# -------------------------------------------------------------------------
# Leaderboard cross-check


async def fetch_leaderboard_pnl(session: aiohttp.ClientSession, funder: str) -> Optional[float]:
    url = f"{LB_API_URL}?window=all&address={funder}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json()
            if isinstance(payload, list) and payload:
                return float(payload[0].get("amount", 0) or 0)
    except Exception:
        return None
    return None


# -------------------------------------------------------------------------
# Top-level orchestration


async def attribute_funder(funder: str, verbose: bool = True) -> Dict[str, Any]:
    """End-to-end: pull fills + redemptions, resolve tokens to conditions,
    classify, return rollup. Also fetches the leaderboard P&L for a
    reconciliation check + computes the post-V2-migration "uncategorized"
    bucket.

    Key fields in the returned dict:

      arb_pnl_v1, spec_pnl_v1     — V1-era (pre 2026-04-28) on-chain
                                    attribution. Exact, reconciles by
                                    construction.
      v1_total_pnl                — arb_pnl_v1 + spec_pnl_v1.
      leaderboard_pnl             — Polymarket's authoritative all-time
                                    P&L for this wallet. The single
                                    number the website UI shows.
      v2_era_pnl                  — leaderboard_pnl − v1_total_pnl. This
                                    captures everything Polymarket migrated
                                    to V2 contracts since 2026-04-28 (no
                                    public subgraph indexes V2 events).
                                    Also catches maker rewards, since
                                    those are USDC transfers outside the
                                    trade-event stream.
    """
    async with aiohttp.ClientSession() as session:
        if verbose:
            print(f"[{funder[:10]}…] fetching fills...", flush=True)
        t0 = time.time()
        fills = await fetch_all_fills(session, funder)
        if verbose:
            print(f"  fills: {len(fills)} ({time.time()-t0:.1f}s)", flush=True)

        if verbose:
            print(f"[{funder[:10]}…] fetching redemptions...", flush=True)
        t1 = time.time()
        redemptions = await fetch_all_redemptions(session, funder)
        if verbose:
            print(f"  redemptions: {len(redemptions)} ({time.time()-t1:.1f}s)", flush=True)

        if verbose:
            print(f"[{funder[:10]}…] resolving tokens→conditions...", flush=True)
        t2 = time.time()
        token_ids = list({classify_fill(f)["token_id"] for f in fills})
        token_to_condition = await map_tokens_to_conditions(session, token_ids)
        if verbose:
            print(
                f"  mapped {len(token_to_condition)}/{len(token_ids)} tokens "
                f"({time.time()-t2:.1f}s)",
                flush=True,
            )

        if verbose:
            print(f"[{funder[:10]}…] computing attribution...", flush=True)
        raw = compute_attribution(fills, redemptions, token_to_condition)
        leaderboard = await fetch_leaderboard_pnl(session, funder)

    # The subgraphs index only V1 events (frozen at 2026-04-28; Polymarket
    # migrated the CTF Exchange to V2 contracts that day and stopped
    # supporting their old subgraph indexer). Polymarket has no public V2
    # subgraph — they pushed users to paid Goldsky Turbo Pipelines. So
    # anything the leaderboard shows above our v1_total is uncategorisable
    # V2-era P&L + any maker rewards.
    last_fill_ts = max(
        (int(f["timestamp"]) for f in fills), default=None
    )
    v1_total = raw["arb_pnl"] + raw["spec_pnl"]
    v2_era = (
        leaderboard - v1_total
        if leaderboard is not None else None
    )
    return {
        "funder_address": funder.lower(),
        "fills_count": len(fills),
        "redemptions_count": len(redemptions),
        "tokens_mapped": len(token_to_condition),
        "tokens_unmapped": len(token_ids) - len(token_to_condition),
        "arb_pnl_v1": raw["arb_pnl"],
        "spec_pnl_v1": raw["spec_pnl"],
        "arb_count_v1": raw["arb_count"],
        "spec_count_v1": raw["spec_count"],
        "v1_total_pnl": v1_total,
        "v2_era_pnl": v2_era,
        "leaderboard_pnl": leaderboard,
        "v1_data_through_timestamp": last_fill_ts,
        "per_condition": raw["per_condition"],
    }



async def _cli():
    """Run against one or more funders, print a reconciliation summary.
    """
    args = sys.argv[1:]
    args = [a for a in args if not a.startswith("--")]
    funders = args
    if not funders:
        # Default: the three known funders from .env
        from dotenv import load_dotenv

        load_dotenv()
        for env in (
            "POLYMARKET_FUNDER_ADDRESS",
            "POLYMARKET_FUNDER_ADDRESS_LIVE",
            "POLYMARKET_FUNDER_ADDRESS_SPREAD",
        ):
            v = os.getenv(env)
            if v:
                funders.append(v)

    if not funders:
        print(
            "usage: polymarket_attribution.py <funder_address> [...]"
        )
        sys.exit(2)

    for funder in funders:
        print("\n" + "=" * 70)
        print(f"Attribution for {funder}")
        print("=" * 70)
        r = await attribute_funder(funder)
        print()
        print(f"  fills (V1):       {r['fills_count']:>6,}")
        print(f"  redemptions (V1): {r['redemptions_count']:>6,}")
        print(f"  tokens mapped:    {r['tokens_mapped']:>6,}  "
              f"(unmapped: {r['tokens_unmapped']})")
        print()
        print(f"  V1 ARB:    ${r['arb_pnl_v1']:>12,.2f}  "
              f"({r['arb_count_v1']:,} conditions)")
        print(f"  V1 SPEC:   ${r['spec_pnl_v1']:>12,.2f}  "
              f"({r['spec_count_v1']:,} conditions)")
        print(f"  V1 total:  ${r['v1_total_pnl']:>12,.2f}")
        if r["v2_era_pnl"] is not None:
            print(f"  V2 era:    ${r['v2_era_pnl']:>12,.2f}  "
                  f"(post 2026-04-28, no public indexer)")
        print(f"  ─────────────────────────")
        if r["leaderboard_pnl"] is not None:
            print(f"  ALL-TIME:  ${r['leaderboard_pnl']:>12,.2f}  "
                  f"(authoritative, from Polymarket leaderboard)")



if __name__ == "__main__":
    asyncio.run(_cli())
