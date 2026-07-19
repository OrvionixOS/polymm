"""
Shadow-mode diff logger for the Rust scanner sidecar (Phase A step 6).

Compares Python-side scanner output against the Rust sidecar's tick
result so we can measure logic-level divergence before promoting Rust
to primary. Identity of an opportunity is `(token_id, round(edge, 3))`
as specified in `rust/PHASE_A_PLAN.md` §7.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional


OppKey = tuple[str, float]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowDiff:
    """Set-difference stats between Python and Rust opportunity sets."""

    tick_id: int
    python_count: int
    rust_count: int
    agreed: int
    python_only: list[OppKey]
    rust_only: list[OppKey]

    @property
    def has_divergence(self) -> bool:
        return bool(self.python_only or self.rust_only)

    def summary(self) -> str:
        if not self.has_divergence:
            return (
                f"tick={self.tick_id} agreed={self.agreed} "
                f"(py={self.python_count} rs={self.rust_count})"
            )
        return (
            f"tick={self.tick_id} DIVERGENCE agreed={self.agreed} "
            f"py_only={len(self.python_only)} rs_only={len(self.rust_only)} "
            f"(py={self.python_count} rs={self.rust_count})"
        )

    def to_record(self) -> dict:
        """JSON-serializable snapshot — one line per tick in the diff log."""
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tick_id": self.tick_id,
            "python_count": self.python_count,
            "rust_count": self.rust_count,
            "agreed": self.agreed,
            "has_divergence": self.has_divergence,
            "python_only": [[t, e] for t, e in self.python_only],
            "rust_only": [[t, e] for t, e in self.rust_only],
        }


def _key(opp: dict, edge_precision: int = 3) -> OppKey | None:
    """Extract the comparison key from an opportunity dict.

    Returns None when the dict is missing `token_id` or `edge`, which
    shouldn't happen but we don't want a malformed row to take down the
    diff computation for the whole tick.
    """
    token = opp.get("token_id")
    edge = opp.get("edge")
    if not isinstance(token, str) or not isinstance(edge, (int, float)):
        return None
    return (token, round(float(edge), edge_precision))


def compute_diff(
    tick_id: int,
    python_opps: Iterable[dict],
    rust_opps: Iterable[dict],
    edge_precision: int = 3,
) -> ShadowDiff:
    """Compare two opportunity streams and return the set-diff stats."""
    py_keys = {k for o in python_opps if (k := _key(o, edge_precision))}
    rs_keys = {k for o in rust_opps if (k := _key(o, edge_precision))}

    agreed = py_keys & rs_keys
    py_only = sorted(py_keys - rs_keys)
    rs_only = sorted(rs_keys - py_keys)

    return ShadowDiff(
        tick_id=tick_id,
        python_count=len(py_keys),
        rust_count=len(rs_keys),
        agreed=len(agreed),
        python_only=py_only,
        rust_only=rs_only,
    )


class ShadowDiffFileSink:
    """Append one NDJSON record per tick to a file.

    Configured via `POLYMM_SIDECAR_DIFF_LOG`. If the env var is unset or
    the file cannot be opened, the sink is a silent no-op — diff logging
    must never break trading. Plan §10 calls for "writes per-tick
    divergence stats to a file we can grep".
    """

    def __init__(self, path: Optional[str] = None) -> None:
        if path is None:
            path = os.environ.get("POLYMM_SIDECAR_DIFF_LOG")
        self.path: Optional[str] = path or None
        self._failed = False

    @property
    def enabled(self) -> bool:
        return self.path is not None and not self._failed

    def write(self, diff: ShadowDiff) -> None:
        if not self.enabled:
            return
        try:
            line = json.dumps(diff.to_record(), separators=(",", ":")) + "\n"
            # Append-mode open/close per tick — cadence is 30s so the
            # syscall cost is irrelevant and we avoid holding a handle
            # across config reloads / rotations.
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            logger.warning("[sidecar shadow] diff-log write failed (%s); disabling sink", e)
            self._failed = True
