"""
Turn Betfair market state into `sports_odds_v2` rows.

Betfair is an exchange, so there is no bookmaker margin to undo — the work is
different from the-odds-api path:

  - A runner has two prices, best-back and best-lay, straddling the true
    price. The estimate used here is the midpoint in PROBABILITY space:
    p = (1/back + 1/lay) / 2. Averaging the odds instead would bias toward
    the longer price.
  - `odds1`/`odds2` are stored as 1/p, NOT the raw back price. This is
    deliberate: nothing downstream re-derives probabilities, but the
    conformance spec checks that `fair_prob*` follows from the row's own
    odds. Storing a back price beside a midpoint-derived probability would
    make the row internally inconsistent and unverifiable. The odds we store
    are the odds the fair value came from.
  - Probabilities are then normalized to sum to 100 across active runners,
    which is what every consumer of the table assumes.

Refusals are explicit and typed (`SkipReason`) rather than silent, for the
same reason the conformance spec exists: on this table, everything that goes
wrong goes wrong quietly.

Not handled, deliberately: ASIAN_HANDICAP and LINE/RANGE markets. Their line
semantics differ from `spreads` in this schema (quarter-lines, push
handling), and guessing a mapping would produce rows that look right and
price wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from src.core.match_id import make_match_id, normalize_team
from src.feeds.betfair.stream_state import MarketBook, RunnerBook, StreamState

# Distinct from "the-odds-api" so both producers coexist in the table rather
# than overwriting each other on the unique key.
SOURCE = "betfair"

# Betfair marketType -> (polymm market_type, line). OVER_UNDER_* carries its
# line in the name and is handled separately.
MARKET_TYPE_MAP: dict[str, tuple[str, float]] = {
    "MATCH_ODDS": ("h2h", 0.0),
    "BOTH_TEAMS_TO_SCORE": ("btts", 0.0),
}

_OVER_UNDER = re.compile(r"^OVER_UNDER_(\d+)$")

# Betfair event-name separators. " @ " is the US convention and reverses the
# sides: "Away @ Home".
_SEPARATORS = ((" v ", False), (" vs ", False), (" vs. ", False), (" @ ", True))

_DRAW_NAMES = frozenset({"the draw", "draw"})


class SkipReason(Enum):
    """Why a market produced no row."""

    NOT_OPEN = "not_open"
    UNKNOWN_MARKET_TYPE = "unknown_market_type"
    NAMES_UNAVAILABLE = "names_unavailable"
    NO_PUBLISH_TIME = "no_publish_time"
    WRONG_RUNNER_COUNT = "wrong_runner_count"
    ONE_SIDED_BOOK = "one_sided_book"
    UNNAMED_RUNNER = "unnamed_runner"
    TEAMS_UNPARSEABLE = "teams_unparseable"
    NO_DRAW_RUNNER = "no_draw_runner"


@dataclass(frozen=True)
class MarketNames:
    """Names for one market, from `listMarketCatalogue`.

    The stream carries no names at all, so without this a market cannot be
    turned into a row — and guessing is worse than skipping.
    """

    event_name: str
    runner_names: Mapping[int, str]


@dataclass(frozen=True)
class Skipped:
    market_id: str
    reason: SkipReason
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.market_id}: {self.reason.value} {self.detail}".strip()


@dataclass(frozen=True)
class ProducerResult:
    rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    skipped: tuple[Skipped, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        if not self.skipped:
            return f"{len(self.rows)} row(s), nothing skipped"
        counts: dict[str, int] = {}
        for s in self.skipped:
            counts[s.reason.value] = counts.get(s.reason.value, 0) + 1
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return f"{len(self.rows)} row(s), {len(self.skipped)} skipped — {detail}"


# ── Probability derivation ─────────────────────────────────────────────────

def mid_probability(back: float, lay: float) -> Optional[float]:
    """Midpoint of back and lay in probability space.

    On a two-sided book best_back < best_lay, so 1/back > 1/lay and the
    result sits between them. Returns None for prices that cannot imply a
    probability.
    """
    if back <= 1 or lay <= 1:
        return None
    return (1 / back + 1 / lay) / 2


def _runner_probabilities(
    runners: Sequence[RunnerBook],
) -> Optional[tuple[float, ...]]:
    """Normalized probabilities (percent) for every runner, or None.

    None when any runner lacks a two-sided price: a partial normalization
    would silently redistribute the missing runner's probability across the
    others, inflating every one of them.
    """
    # Every active runner must be two-sided. A one-sided book has no
    # midpoint, and pricing off the single available side is exactly where
    # you get picked off — so the market is refused rather than guessed at.
    raw: list[float] = []
    for runner in runners:
        back, lay = runner.best_back(), runner.best_lay()
        if back is None or lay is None:
            return None
        p = mid_probability(back, lay)
        if p is None:
            return None
        raw.append(p)

    if not raw:
        return None
    total = sum(raw)
    if total <= 0:
        return None
    return tuple(p / total * 100 for p in raw)


def _odds_from_probability(p_percent: float, total_raw: float) -> float:
    """The decimal odds the stored probability was derived from.

    Reconstructed so that proportional vig removal over the stored odds
    reproduces the stored probabilities exactly — the invariant the
    conformance spec checks. Since fair_i = raw_i / total and odds_i =
    1 / raw_i, storing 1 / (fair_i * total) is that same price.
    """
    return 1 / (p_percent / 100 * total_raw)


# ── Event name parsing ─────────────────────────────────────────────────────

def parse_event_teams(event_name: str) -> Optional[tuple[str, str]]:
    """Split "Home v Away" into (home, away).

    " @ " reverses the sides, since the US convention writes "Away @ Home".
    Returns None when no separator is recognised, rather than guessing a
    split that would produce a wrong match_id and a market that never
    matches.
    """
    if not event_name:
        return None
    for separator, reversed_sides in _SEPARATORS:
        if separator in event_name:
            left, _, right = event_name.partition(separator)
            left, right = left.strip(), right.strip()
            if not left or not right:
                return None
            return (right, left) if reversed_sides else (left, right)
    return None


def polymm_market(betfair_market_type: str) -> Optional[tuple[str, float]]:
    """Map a Betfair marketType to (market_type, line) in this schema."""
    if betfair_market_type in MARKET_TYPE_MAP:
        return MARKET_TYPE_MAP[betfair_market_type]
    match = _OVER_UNDER.match(betfair_market_type or "")
    if match:
        digits = match.group(1)
        # OVER_UNDER_25 -> 2.5, OVER_UNDER_105 -> 10.5, OVER_UNDER_05 -> 0.5
        return ("totals", int(digits) / 10)
    return None


def _is_draw(name: str) -> bool:
    return name.strip().lower() in _DRAW_NAMES


# ── Row construction ───────────────────────────────────────────────────────

def rows_for_market(
    book: MarketBook,
    names: Optional[MarketNames],
    *,
    game: str,
) -> tuple[Optional[dict[str, Any]], Optional[Skipped]]:
    """Build one row from one market, or say why not.

    `game` is the canonical polymm game name ("football", "basketball", ...),
    which is also written to the `sport` column. That is required, not
    stylistic: `odds_service` maps `sport` through `SPORT_KEY_TO_GAME` and
    falls back to the raw value, and `make_match_id` must receive the mapped
    result. Any other value (a Betfair competition, say) would fall through
    unmapped and produce a match_id nothing matches.

    The cost is that `sport` no longer distinguishes competitions, so the
    same two teams meeting in a league and a cup share a unique key. Rare,
    and preferable to a market that silently never matches.
    """
    def skip(reason: SkipReason, detail: str = "") -> tuple[None, Skipped]:
        return None, Skipped(book.market_id, reason, detail)

    if not book.is_open:
        return skip(SkipReason.NOT_OPEN, f"status={book.status}")

    mapped = polymm_market(book.market_type or "")
    if mapped is None:
        return skip(SkipReason.UNKNOWN_MARKET_TYPE, f"marketType={book.market_type}")
    market_type, line = mapped

    if names is None:
        return skip(SkipReason.NAMES_UNAVAILABLE, "no listMarketCatalogue entry")

    # The publish time is the only honest timestamp: it is when Betfair
    # generated the change, so a delayed key's 180s conflation shows up as
    # 180s of age. Substituting the local clock would present conflated data
    # as fresh and walk straight past the freshness gates.
    if book.publish_time_ms is None:
        return skip(SkipReason.NO_PUBLISH_TIME, "no pt on any message for this market")
    scraped_at = datetime.fromtimestamp(book.publish_time_ms / 1000, tz=timezone.utc)

    runners = book.active_runners()
    if len(runners) not in (2, 3):
        return skip(SkipReason.WRONG_RUNNER_COUNT, f"{len(runners)} active runner(s)")

    runner_names: list[str] = []
    for runner in runners:
        name = names.runner_names.get(runner.selection_id)
        if not name:
            return skip(SkipReason.UNNAMED_RUNNER, f"selectionId={runner.selection_id}")
        runner_names.append(name)

    probabilities = _runner_probabilities(runners)
    if probabilities is None:
        return skip(
            SkipReason.ONE_SIDED_BOOK,
            f"runners without a two-sided price: {book.runner_ids_missing_prices()}",
        )

    total_raw = sum(
        mid_probability(r.best_back(), r.best_lay()) or 0.0 for r in runners
    )

    # Split off the draw leg, if any, so the two win legs stay in order.
    draw_index = next(
        (i for i, name in enumerate(runner_names) if _is_draw(name)), None
    )
    if len(runners) == 3 and draw_index is None:
        return skip(SkipReason.NO_DRAW_RUNNER, f"runners={runner_names}")

    indices = [i for i in range(len(runners)) if i != draw_index]
    name1, name2 = runner_names[indices[0]], runner_names[indices[1]]
    prob1, prob2 = probabilities[indices[0]], probabilities[indices[1]]
    odds1 = _odds_from_probability(prob1, total_raw)
    odds2 = _odds_from_probability(prob2, total_raw)

    if market_type == "h2h":
        # The selection names in a MATCH_ODDS market ARE the teams, so no
        # event-name parsing is needed for the market that matters most.
        home, away = name1, name2
    else:
        teams = parse_event_teams(names.event_name)
        if teams is None:
            return skip(SkipReason.TEAMS_UNPARSEABLE, f"event={names.event_name!r}")
        home, away = teams

    row: dict[str, Any] = {
        "match_id": make_match_id(home, away, game),
        "source": SOURCE,
        "sport": game,
        "team1": normalize_team(home),
        "team2": normalize_team(away),
        "market_type": market_type,
        "line": line,
        "outcome1_name": name1,
        "outcome2_name": name2,
        "odds1": round(odds1, 3),
        "odds2": round(odds2, 3),
        "fair_prob1": round(prob1, 2),
        "fair_prob2": round(prob2, 2),
        # An exchange market has one book, not a panel of bookmakers. Saying
        # 1 is truthful; saying "many" would overstate corroboration.
        "bookmaker_count": 1,
        "is_live": book.in_play,
        "scraped_at": scraped_at.isoformat(),
        "event_id": book.event_id,
        "commence_time": _iso_or_none(book.definition.get("marketTime")),
    }

    if draw_index is not None:
        row["outcome_draw_name"] = runner_names[draw_index]
        draw_prob = probabilities[draw_index]
        row["odds_draw"] = round(_odds_from_probability(draw_prob, total_raw), 3)
        row["fair_prob_draw"] = round(draw_prob, 2)

    return row, None


def rows_from_state(
    state: StreamState,
    names_by_market: Mapping[str, MarketNames],
    *,
    game_by_market: Mapping[str, str],
) -> ProducerResult:
    """Build rows for every market in state that can produce one.

    `game_by_market` carries the canonical game per market, derived from
    Betfair's eventTypeId by the caller — that mapping is a catalogue
    concern, not a stream one.
    """
    rows: list[dict[str, Any]] = []
    skipped: list[Skipped] = []
    for market_id in sorted(state.markets):
        book = state.markets[market_id]
        game = game_by_market.get(market_id)
        if not game:
            skipped.append(
                Skipped(market_id, SkipReason.NAMES_UNAVAILABLE, "no game mapping")
            )
            continue
        row, skip = rows_for_market(book, names_by_market.get(market_id), game=game)
        if row is not None:
            rows.append(row)
        if skip is not None:
            skipped.append(skip)
    return ProducerResult(tuple(rows), tuple(skipped))


def _iso_or_none(value: Any) -> Optional[str]:
    """Betfair sends marketTime as millis since epoch or an ISO string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    return None
