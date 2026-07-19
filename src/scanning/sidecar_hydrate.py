"""
Hydration helper for the Rust scanner sidecar (Phase A step 7).

Converts a Rust-side NDJSON `opportunity` event back into the dict shape
that `SportsBot._execute_opportunity` consumes. The executor expects two
rich objects on each opportunity:

* `poly_event` — duck-typed with `.team1`, `.team2`, `.start_time`, and
  `.markets` (a list whose first element exposes `.get("condition_id")`
  and `.get("outcomes")`).
* `odds_match`  — duck-typed with `.match_id`, `.team1`, `.team2`,
  `.game`.

When the `OddsService` cache still holds the corresponding
`AggregatedMatch`, we reuse it as `odds_match` so downstream code keeps
access to the full source data. Otherwise we synthesize a minimal
stand-in from the Rust payload itself — Phase A executor paths only
touch the four attributes above, so this is sufficient.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping, Optional


REQUIRED_FIELDS: tuple[str, ...] = (
    "token_id",
    "team",
    "hedge_team",
    "hedge_token",
    "entry_price",
    "fair",
    "edge",
    "match_id",
    "poly_event_team1",
    "poly_event_team2",
)


class HydrationError(ValueError):
    """Raised when a Rust opportunity is missing fields required to execute."""


def _validate(opp: Mapping[str, Any]) -> None:
    missing = [k for k in REQUIRED_FIELDS if opp.get(k) in (None, "")]
    if missing:
        raise HydrationError(f"rust opportunity missing required fields: {missing}")


def hydrate_opportunity(
    opp: Mapping[str, Any],
    *,
    odds_cache: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Convert a Rust NDJSON opportunity dict into Python execute shape.

    Args:
        opp: the parsed NDJSON `opportunity` event from the sidecar.
        odds_cache: optional mapping from `match_id` to `AggregatedMatch`
            (typically `OddsService._cache`). When the lookup hits, the
            AggregatedMatch becomes `odds_match`; otherwise a minimal
            stand-in is synthesized from `opp` itself.

    Returns: dict ready to pass to `SportsBot._execute_opportunity`.
    """
    _validate(opp)

    match_id = opp["match_id"]
    game = opp.get("game") or ""
    team1 = opp["poly_event_team1"]
    team2 = opp["poly_event_team2"]

    odds_match = None
    if odds_cache is not None:
        odds_match = odds_cache.get(match_id)

    if odds_match is None:
        odds_match = SimpleNamespace(
            match_id=match_id,
            team1=team1,
            team2=team2,
            game=game,
        )

    market_dict = {
        "condition_id": opp.get("condition_id", ""),
        "outcomes": [team1, team2],
    }
    poly_event = SimpleNamespace(
        id=opp.get("poly_event_id", ""),
        team1=team1,
        team2=team2,
        start_time=opp.get("start_time"),
        markets=[market_dict],
    )

    return {
        "token_id": opp["token_id"],
        "team": opp["team"],
        "hedge_team": opp["hedge_team"],
        "hedge_token": opp["hedge_token"],
        "entry_price": float(opp["entry_price"]),
        "fair": float(opp["fair"]),
        "edge": float(opp["edge"]),
        "expected_profit": float(opp.get("expected_profit", 0.0)),
        "market_type": "h2h",
        "line": 0,
        "market": market_dict,
        "poly_event": poly_event,
        "odds_match": odds_match,
    }


def hydrate_opportunities(
    opps: list[dict],
    *,
    odds_cache: Optional[Mapping[str, Any]] = None,
) -> list[dict]:
    """Hydrate a batch, dropping any opportunity that fails validation.

    Per Phase A step 7, a single malformed event must not take down the
    whole tick — skip it with a best-effort warning and continue.
    """
    import logging

    logger = logging.getLogger(__name__)
    out: list[dict] = []
    for o in opps:
        try:
            out.append(hydrate_opportunity(o, odds_cache=odds_cache))
        except HydrationError as e:
            logger.warning("[sidecar primary] dropping malformed opp: %s (%r)", e, o)
    return out
