"""
Write producer rows to `sports_odds_v2`, gated on conformance.

The gate is the point. Every failure mode on this table is silent — a wrong
`match_id` never matches, a missing numeric reads as zero and the row
vanishes, a bad probability is priced against — so the cheapest place to stop
a bad row is before it is written, not after it has overwritten a good one.

Rejected rows are returned, not raised and not dropped: a producer with one
bad market should still publish its other 400.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from src.feeds.conformance import FeedIssue, FeedVerdict, check_batch, check_row

logger = logging.getLogger(__name__)

TABLE = "sports_odds_v2"
ON_CONFLICT = "match_id,market_type,line,source,sport"

# Matches OddsApiService's fallback batch size.
BATCH_SIZE = 50


@dataclass(frozen=True)
class WriteResult:
    accepted: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    rejected: tuple[tuple[int, tuple[FeedIssue, ...]], ...] = field(default_factory=tuple)
    written: int = 0

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    def summary(self) -> str:
        parts = [f"{self.written} written", f"{len(self.accepted)} accepted"]
        if self.rejected:
            codes: dict[str, int] = {}
            for _, issues in self.rejected:
                for issue in issues:
                    codes[issue.code.value] = codes.get(issue.code.value, 0) + 1
            detail = ", ".join(f"{k}={v}" for k, v in sorted(codes.items()))
            parts.append(f"{len(self.rejected)} REJECTED — {detail}")
        return "; ".join(parts)


def partition_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    check_freshness: bool = False,
) -> WriteResult:
    """Split rows into those fit to write and those not, with reasons.

    Freshness is off by default: a row can be correctly formed and simply
    old, and whether stale rows are worth storing is the consumer's call —
    `team_matcher` and `is_fresh` already reject them at read time. Turn it
    on to refuse writing what could never be read.
    """
    accepted: list[dict[str, Any]] = []
    rejected: list[tuple[int, tuple[FeedIssue, ...]]] = []
    for index, row in enumerate(rows):
        issues = check_row(row, row_index=index)
        if check_freshness:
            from src.feeds.conformance import check_freshness as _fresh
            issues = issues + _fresh(row, now=now, row_index=index)
        if issues:
            rejected.append((index, issues))
        else:
            accepted.append(dict(row))

    # Cross-row checks (unique-key collisions) only make sense on the rows
    # that individually passed; a collision means the later row would
    # overwrite the earlier one in the same upsert.
    if accepted:
        verdict = check_batch(accepted, include_freshness=False)
        collisions = {
            issue.row_index for issue in verdict.issues if issue.row_index is not None
        }
        if collisions:
            kept: list[dict[str, Any]] = []
            for index, row in enumerate(accepted):
                if index in collisions:
                    rejected.append(
                        (index, tuple(i for i in verdict.issues if i.row_index == index))
                    )
                else:
                    kept.append(row)
            accepted = kept

    return WriteResult(tuple(accepted), tuple(rejected))


def write_rows(
    client: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    gate: bool = True,
) -> WriteResult:
    """Upsert rows to `sports_odds_v2`.

    `client` is a Supabase client exposing `.table(name).upsert(...)`, the
    same shape `SupabaseClient.client` provides.

    `gate=False` skips the conformance check. It exists for backfills of rows
    already known good; it is not a way to push rows that failed the gate.
    """
    if gate:
        result = partition_rows(rows, now=now)
        for index, issues in result.rejected:
            logger.warning(
                "Refusing row %d: %s", index, "; ".join(str(i) for i in issues)
            )
        payload = result.accepted
    else:
        result = WriteResult(tuple(dict(r) for r in rows))
        payload = result.accepted

    if not payload:
        return result

    written = 0
    for start in range(0, len(payload), BATCH_SIZE):
        batch = payload[start:start + BATCH_SIZE]
        try:
            response = (
                client.table(TABLE).upsert(list(batch), on_conflict=ON_CONFLICT).execute()
            )
        except Exception as exc:
            logger.error("Upsert failed for rows %d-%d: %s", start, start + len(batch), exc)
            continue
        written += len(response.data) if getattr(response, "data", None) else 0

    return WriteResult(result.accepted, result.rejected, written)
