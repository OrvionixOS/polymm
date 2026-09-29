"""
Conformance spec for `sports_odds_v2` producers.

Odds enter this system through exactly one door: rows upserted into the
`sports_odds_v2` table. `OddsApiService` is one producer; the local scrapers
(`source_b`, `source_l`, `source_n`) were others. Any replacement feed is a
new producer writing the same rows — no bot code changes.

That makes the table the contract, and this module states it. Run a candidate
producer's rows through `check_batch` before wiring it to anything that trades.

Why this exists rather than "just look at the data": every way a producer can
be wrong here fails SILENTLY.

  - A `match_id` that disagrees with `make_match_id()` never raises. The bot
    simply never matches that market, forever, with no log line.
  - The read path coerces missing numerics to zero
    (`float(r.get("odds1", 0) or 0)`, `odds_service.py:1152`) and then drops
    rows whose fair probability is <= 0. An absent field and a genuine zero
    are indistinguishable downstream, and the row vanishes without an error.
  - Probabilities are trusted as written. Nothing downstream re-derives them
    from `odds1/odds2`, so a producer that miscomputes vig removal — or
    substitutes a placeholder when its own maths throws — feeds confident
    numbers straight into the edge calculation.

So the checks below re-derive what can be re-derived and refuse to treat
absence as a value.

Freshness gates are read from the modules that enforce them
(`AggregatedMatch.is_fresh`, `team_matcher.MAX_ODDS_AGE_SECONDS`) rather than
restated here, so this spec cannot drift from the code it protects.
"""
from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

from src.core.match_id import make_match_id
from src.scanning.team_matcher import MAX_ODDS_AGE_SECONDS
from src.services.odds_service import AggregatedMatch


# ── Gates, read from the code that enforces them ───────────────────────────

def _is_fresh_default(param: str) -> int:
    """Read a default off `AggregatedMatch.is_fresh` so this spec tracks it.

    Hardcoding 60/1800 here would let the spec silently disagree with the
    gate after someone retunes `is_fresh`, which is the exact class of drift
    this module exists to catch.
    """
    default = inspect.signature(AggregatedMatch.is_fresh).parameters[param].default
    if not isinstance(default, int):
        raise TypeError(
            f"AggregatedMatch.is_fresh({param}=...) default is {default!r}, "
            "expected an int number of seconds"
        )
    return default


MAX_AGE_LIVE_SECONDS = _is_fresh_default("max_age_live")
MAX_AGE_PREMATCH_SECONDS = _is_fresh_default("max_age_prematch")
MAX_AGE_HARD_REJECT_SECONDS = MAX_ODDS_AGE_SECONDS


# ── Row shape, from sql/sports_odds_v2.sql ─────────────────────────────────

REQUIRED_FIELDS = (
    "match_id", "source", "sport", "team1", "team2",
    "market_type", "line",
    "outcome1_name", "outcome2_name",
    "odds1", "odds2",
    "fair_prob1", "fair_prob2",
    "scraped_at",
)

# The three-way fields travel together: all present or all absent. A row with
# a draw price but no draw probability reads as two-way downstream and the
# draw leg is silently lost.
DRAW_FIELDS = ("outcome_draw_name", "odds_draw", "fair_prob_draw")

# Unique index on sports_odds_v2. Two producers collide iff they agree on all
# five, which is why `source` is in it — and why a producer that forgets to
# set a distinct `source` overwrites another's rows instead of coexisting.
UNIQUE_KEY = ("match_id", "market_type", "line", "source", "sport")

# Producers round fair probabilities to 2dp; allow that plus float slack.
PROB_TOLERANCE = 0.1


class IssueCode(Enum):
    """Why a row is not fit to trade on.

    Distinct codes rather than a bool because the remedies differ: a stale
    row means poll faster, a mismatched match_id means fix normalization,
    and a disagreeing probability means the producer's maths is wrong.
    """

    MISSING_FIELD = "missing_field"
    NULL_FIELD = "null_field"
    NOT_NUMERIC = "not_numeric"
    MATCH_ID_MISMATCH = "match_id_mismatch"
    ODDS_IMPLAUSIBLE = "odds_implausible"
    PROB_OUT_OF_RANGE = "prob_out_of_range"
    PROB_SUM_INVALID = "prob_sum_invalid"
    DEVIG_DISAGREEMENT = "devig_disagreement"
    DRAW_FIELDS_PARTIAL = "draw_fields_partial"
    SILENTLY_DROPPED = "silently_dropped"
    SOURCE_COLLISION = "source_collision"
    TIMESTAMP_UNPARSEABLE = "timestamp_unparseable"
    TIMESTAMP_IN_FUTURE = "timestamp_in_future"
    STALE_LIVE = "stale_live"
    STALE_PREMATCH = "stale_prematch"
    STALE_HARD_REJECT = "stale_hard_reject"


@dataclass(frozen=True)
class FeedIssue:
    code: IssueCode
    field: str
    detail: str
    row_index: Optional[int] = None

    def __str__(self) -> str:
        where = "" if self.row_index is None else f"row {self.row_index}: "
        return f"{where}{self.code.value} [{self.field}] {self.detail}"


@dataclass(frozen=True)
class FeedVerdict:
    """Result of checking a candidate producer's output."""

    rows_checked: int
    issues: tuple[FeedIssue, ...] = field(default_factory=tuple)

    @property
    def conforming(self) -> bool:
        return not self.issues

    def by_code(self, code: IssueCode) -> tuple[FeedIssue, ...]:
        return tuple(i for i in self.issues if i.code is code)

    def summary(self) -> str:
        if self.conforming:
            return f"conforming: {self.rows_checked} row(s), no issues"
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.code.value] = counts.get(issue.code.value, 0) + 1
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return f"NOT conforming: {self.rows_checked} row(s), {len(self.issues)} issue(s) — {detail}"


# ── Helpers ────────────────────────────────────────────────────────────────

def _as_float(value: Any) -> Optional[float]:
    """Parse a numeric, distinguishing 'absent' from 'zero'.

    Returns None for anything not numeric — deliberately NOT 0.0, which is
    what the read path does and what makes bad rows disappear quietly.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and not math.isfinite(value) else float(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def parse_scraped_at(value: Any) -> Optional[datetime]:
    """Parse the producer's timestamp the way `odds_service` does.

    `odds_service._fetch_from_supabase_v2` falls back to `now` when parsing
    fails, which makes an unparseable timestamp look perfectly fresh. Here it
    is an issue instead.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _game_for_row(row: Mapping[str, Any], sport_key_to_game: Mapping[str, str]) -> str:
    """Resolve the canonical game name the same way the reader does.

    `odds_service` maps `sport` through `SPORT_KEY_TO_GAME` and falls back to
    the raw sport key, so `make_match_id` must be fed the mapped value.
    """
    sport = str(row.get("sport", ""))
    return sport_key_to_game.get(sport, sport)


# ── Per-row checks ─────────────────────────────────────────────────────────

def check_row(
    row: Mapping[str, Any],
    *,
    row_index: Optional[int] = None,
    sport_key_to_game: Optional[Mapping[str, str]] = None,
) -> tuple[FeedIssue, ...]:
    """Check one candidate row for everything verifiable without a clock."""
    if sport_key_to_game is None:
        from src.services.odds_api_service import SPORT_KEY_TO_GAME
        sport_key_to_game = SPORT_KEY_TO_GAME

    issues: list[FeedIssue] = []

    def add(code: IssueCode, fld: str, detail: str) -> None:
        issues.append(FeedIssue(code, fld, detail, row_index))

    # 1. Presence. Absent and null are different mistakes with the same
    #    downstream symptom, so they get different codes.
    for name in REQUIRED_FIELDS:
        if name not in row:
            add(IssueCode.MISSING_FIELD, name, "required by sports_odds_v2, not present")
        elif row[name] is None:
            add(IssueCode.NULL_FIELD, name, "required by sports_odds_v2, present but null")

    # 2. Draw fields travel as a set.
    present_draw = [f for f in DRAW_FIELDS if row.get(f) is not None]
    if present_draw and len(present_draw) != len(DRAW_FIELDS):
        missing = [f for f in DRAW_FIELDS if row.get(f) is None]
        add(
            IssueCode.DRAW_FIELDS_PARTIAL,
            ",".join(missing),
            f"three-way row has {present_draw} but not {missing}; "
            "the draw leg is dropped downstream without error",
        )

    # 3. Numerics. Re-parsed strictly: a value the reader would coerce to 0
    #    is reported here rather than silently removing the row later.
    odds1 = _as_float(row.get("odds1"))
    odds2 = _as_float(row.get("odds2"))
    odds_draw = _as_float(row.get("odds_draw")) if row.get("odds_draw") is not None else None
    prob1 = _as_float(row.get("fair_prob1"))
    prob2 = _as_float(row.get("fair_prob2"))
    prob_draw = _as_float(row.get("fair_prob_draw")) if row.get("fair_prob_draw") is not None else None

    for name, raw, parsed in (
        ("odds1", row.get("odds1"), odds1),
        ("odds2", row.get("odds2"), odds2),
        ("fair_prob1", row.get("fair_prob1"), prob1),
        ("fair_prob2", row.get("fair_prob2"), prob2),
    ):
        if name in row and row[name] is not None and parsed is None:
            add(IssueCode.NOT_NUMERIC, name, f"{raw!r} is not a finite number")

    if row.get("line") is not None and _as_float(row.get("line")) is None:
        add(IssueCode.NOT_NUMERIC, "line", f"{row.get('line')!r} is not a finite number")

    # 4. Odds plausibility. Decimal odds <= 1 mean a sub-1.0 payout; the
    #    vig-removal helpers return (0.0, 0.0) for these, which then reads as
    #    "no fair value" rather than "bad input".
    for name, value in (("odds1", odds1), ("odds2", odds2), ("odds_draw", odds_draw)):
        if value is not None and value <= 1:
            add(
                IssueCode.ODDS_IMPLAUSIBLE, name,
                f"decimal odds {value} <= 1; vig removal yields 0.0 and the row reads as unpriced",
            )

    # 5. Probabilities are on a 0-100 scale downstream.
    for name, value in (("fair_prob1", prob1), ("fair_prob2", prob2), ("fair_prob_draw", prob_draw)):
        if value is not None and not (0 < value < 100):
            add(
                IssueCode.PROB_OUT_OF_RANGE, name,
                f"{value} outside (0, 100); values <= 0 are dropped by the reader without a log line",
            )

    # 6. The row must be internally consistent: probabilities that do not
    #    follow from the row's own odds are the failure mode nothing else
    #    catches, because nothing downstream re-derives them.
    if prob1 is not None and prob2 is not None:
        legs = [prob1, prob2] + ([prob_draw] if prob_draw is not None else [])
        total = sum(legs)
        if abs(total - 100.0) > PROB_TOLERANCE:
            add(
                IssueCode.PROB_SUM_INVALID, "fair_prob*",
                f"{' + '.join(f'{p}' for p in legs)} = {total:.2f}, expected 100 "
                f"(+/-{PROB_TOLERANCE}) after vig removal",
            )

        expected = _expected_probabilities(odds1, odds2, odds_draw)
        if expected is not None:
            names = ("fair_prob1", "fair_prob2", "fair_prob_draw")
            actual = (prob1, prob2, prob_draw)
            for name, got, want in zip(names, actual, expected):
                if want is None or got is None:
                    continue
                if abs(got - want) > PROB_TOLERANCE:
                    add(
                        IssueCode.DEVIG_DISAGREEMENT, name,
                        f"{got} does not follow from this row's odds "
                        f"(proportional vig removal gives {want:.2f})",
                    )

    # 7. Identity. The silent killer: a match_id that disagrees with
    #    make_match_id never errors, it just never matches a market.
    if row.get("match_id") and row.get("team1") and row.get("team2") and row.get("sport"):
        game = _game_for_row(row, sport_key_to_game)
        expected_id = make_match_id(str(row["team1"]), str(row["team2"]), game)
        if str(row["match_id"]) != expected_id:
            add(
                IssueCode.MATCH_ID_MISMATCH, "match_id",
                f"{row['match_id']!r} != make_match_id({row['team1']!r}, {row['team2']!r}, "
                f"{game!r}) == {expected_id!r}; this market will never be matched",
            )

    # 8. Would the reader keep this row at all?
    if (prob1 is None or prob1 <= 0) or (prob2 is None or prob2 <= 0):
        add(
            IssueCode.SILENTLY_DROPPED, "fair_prob1/fair_prob2",
            "odds_service skips rows whose fair probabilities are missing or <= 0; "
            "this row would never reach the scanner",
        )

    # 9. Timestamp must be parseable — the reader substitutes `now` when it
    #    is not, disguising an ancient row as a fresh one.
    if "scraped_at" in row and row["scraped_at"] is not None:
        scraped_at = parse_scraped_at(row["scraped_at"])
        if scraped_at is None:
            add(
                IssueCode.TIMESTAMP_UNPARSEABLE, "scraped_at",
                f"{row['scraped_at']!r} is not ISO-8601; the reader falls back to now() "
                "and the row looks perfectly fresh",
            )

    return tuple(issues)


def _expected_probabilities(
    odds1: Optional[float],
    odds2: Optional[float],
    odds_draw: Optional[float],
) -> Optional[tuple[Optional[float], Optional[float], Optional[float]]]:
    """Fair probabilities implied by the row's own odds, as percentages.

    Mirrors what producers do: proportional vig removal over the available
    legs. Returns None when the odds cannot support a derivation, so the
    caller reports bad odds rather than a spurious disagreement.
    """
    if odds1 is None or odds2 is None or odds1 <= 1 or odds2 <= 1:
        return None
    if odds_draw is not None:
        if odds_draw <= 1:
            return None
        total = 1 / odds1 + 1 / odds_draw + 1 / odds2
        return (
            (1 / odds1) / total * 100,
            (1 / odds2) / total * 100,
            (1 / odds_draw) / total * 100,
        )
    total = 1 / odds1 + 1 / odds2
    return ((1 / odds1) / total * 100, (1 / odds2) / total * 100, None)


# ── Freshness ──────────────────────────────────────────────────────────────

def check_freshness(
    row: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    row_index: Optional[int] = None,
) -> tuple[FeedIssue, ...]:
    """Check one row against the gates the bot actually enforces.

    Separate from `check_row` because a feed can be perfectly well-formed and
    still useless: the shipped the-odds-api producer polls every 300s against
    a 60s live gate, which is the hole the local scrapers filled.
    """
    now = now or datetime.now(timezone.utc)
    scraped_at = parse_scraped_at(row.get("scraped_at"))
    if scraped_at is None:
        return ()  # check_row already reports the unparseable timestamp

    age = (now - scraped_at).total_seconds()
    is_live = bool(row.get("is_live", False))

    if age < 0:
        return (
            FeedIssue(
                IssueCode.TIMESTAMP_IN_FUTURE, "scraped_at",
                f"{-age:.1f}s in the future; a clock-skewed producer stays 'fresh' "
                "long after its data is not",
                row_index,
            ),
        )

    # The gates are reported independently, not as a chain. They belong to
    # different consumers and a row can fail one while passing the other:
    #
    #   - team_matcher's hard reject (600s) governs FAIR VALUE lookup. Past
    #     it, `get_fair_value_with_staleness_check` returns nothing, so the
    #     row cannot be priced.
    #   - is_fresh's gates (60s live / 1800s prematch) govern whether
    #     `get_matches(fresh_only=True)` yields the match at all.
    #
    # Note these disagree: because 600 < 1800, a prematch row between 600s
    # and 1800s old is still "fresh" to the scanner but has no fair value.
    # Chaining these with elif would hide that, and would make the prematch
    # gate unreachable.
    issues: list[FeedIssue] = []
    if age > MAX_AGE_HARD_REJECT_SECONDS:
        issues.append(
            FeedIssue(
                IssueCode.STALE_HARD_REJECT, "scraped_at",
                f"{age:.1f}s old, past team_matcher's {MAX_AGE_HARD_REJECT_SECONDS}s "
                "reject; fair value lookup returns nothing",
                row_index,
            )
        )
    if is_live and age > MAX_AGE_LIVE_SECONDS:
        issues.append(
            FeedIssue(
                IssueCode.STALE_LIVE, "scraped_at",
                f"{age:.1f}s old, past the {MAX_AGE_LIVE_SECONDS}s live gate",
                row_index,
            )
        )
    if not is_live and age > MAX_AGE_PREMATCH_SECONDS:
        issues.append(
            FeedIssue(
                IssueCode.STALE_PREMATCH, "scraped_at",
                f"{age:.1f}s old, past the {MAX_AGE_PREMATCH_SECONDS}s prematch gate",
                row_index,
            )
        )
    return tuple(issues)


# ── Batch ──────────────────────────────────────────────────────────────────

def check_batch(
    rows: Sequence[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    include_freshness: bool = True,
    sport_key_to_game: Optional[Mapping[str, str]] = None,
) -> FeedVerdict:
    """Check a candidate producer's output.

    Pass `include_freshness=False` for fixtures with fixed timestamps, where
    only the row shape is under test.
    """
    issues: list[FeedIssue] = []
    for index, row in enumerate(rows):
        issues.extend(check_row(row, row_index=index, sport_key_to_game=sport_key_to_game))
        if include_freshness:
            issues.extend(check_freshness(row, now=now, row_index=index))

    issues.extend(_check_unique_key(rows))
    return FeedVerdict(rows_checked=len(rows), issues=tuple(issues))


def _check_unique_key(rows: Sequence[Mapping[str, Any]]) -> tuple[FeedIssue, ...]:
    """Two rows sharing the unique key overwrite each other on upsert.

    Within one batch this is a producer bug — most often a feed emitting
    several bookmakers' prices for one market without collapsing them, or
    reusing another producer's `source`.
    """
    seen: dict[tuple, int] = {}
    issues: list[FeedIssue] = []
    for index, row in enumerate(rows):
        key = tuple(_normalize_key_part(row.get(part)) for part in UNIQUE_KEY)
        if any(part is None for part in key):
            continue  # missing fields already reported
        if key in seen:
            issues.append(
                FeedIssue(
                    IssueCode.SOURCE_COLLISION, ",".join(UNIQUE_KEY),
                    f"same unique key as row {seen[key]}; the later upsert silently "
                    f"overwrites the earlier one (key={key})",
                    index,
                )
            )
        else:
            seen[key] = index
    return tuple(issues)


def _normalize_key_part(value: Any) -> Any:
    """`line` arrives as int, float, str or Decimal; the index treats 0 == 0.0."""
    if value is None:
        return None
    numeric = _as_float(value)
    return numeric if numeric is not None else str(value)


# ── Latency measurement ────────────────────────────────────────────────────

@dataclass(frozen=True)
class LatencyReport:
    """End-to-end age of a producer's rows, against the gates.

    `worst_live_age` is the number that decides whether a candidate feed is
    viable at all: it has to stay under MAX_AGE_LIVE_SECONDS on live markets.
    """

    rows: int
    live_rows: int
    worst_age: Optional[float]
    worst_live_age: Optional[float]
    median_age: Optional[float]

    @property
    def clears_live_gate(self) -> bool:
        """True only if evidenced by live rows — no live rows is not a pass."""
        return self.live_rows > 0 and (self.worst_live_age or 0) <= MAX_AGE_LIVE_SECONDS

    def summary(self) -> str:
        if not self.rows:
            return "no rows measured"
        if self.live_rows == 0:
            return (
                f"{self.rows} row(s), median age {self.median_age:.1f}s, "
                f"worst {self.worst_age:.1f}s — NO live rows, live gate unproven"
            )
        verdict = "clears" if self.clears_live_gate else "FAILS"
        return (
            f"{self.rows} row(s) ({self.live_rows} live), median age {self.median_age:.1f}s, "
            f"worst live {self.worst_live_age:.1f}s — {verdict} the "
            f"{MAX_AGE_LIVE_SECONDS}s live gate"
        )


def measure_latency(
    rows: Iterable[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> LatencyReport:
    """Measure how old a producer's rows are when they land.

    Rows with unparseable timestamps are excluded rather than counted as
    fresh; `check_batch` reports them separately.
    """
    now = now or datetime.now(timezone.utc)
    ages: list[float] = []
    live_ages: list[float] = []
    for row in rows:
        scraped_at = parse_scraped_at(row.get("scraped_at"))
        if scraped_at is None:
            continue
        age = (now - scraped_at).total_seconds()
        ages.append(age)
        if row.get("is_live", False):
            live_ages.append(age)

    if not ages:
        return LatencyReport(0, 0, None, None, None)

    ordered = sorted(ages)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return LatencyReport(
        rows=len(ages),
        live_rows=len(live_ages),
        worst_age=max(ages),
        worst_live_age=max(live_ages) if live_ages else None,
        median_age=median,
    )
