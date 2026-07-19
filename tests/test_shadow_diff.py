"""Unit tests for the shadow-mode diff logger."""
import json
from pathlib import Path

from src.scanning.shadow_diff import compute_diff, ShadowDiff, ShadowDiffFileSink


def _opp(token: str, edge: float) -> dict:
    return {"token_id": token, "edge": edge}


def test_identical_sets_agree_with_no_divergence():
    py = [_opp("tok_a", 0.09), _opp("tok_b", 0.15)]
    rs = [_opp("tok_a", 0.09), _opp("tok_b", 0.15)]
    d = compute_diff(1, py, rs)

    assert d.tick_id == 1
    assert d.agreed == 2
    assert d.python_only == []
    assert d.rust_only == []
    assert not d.has_divergence


def test_python_only_opportunity_flagged():
    py = [_opp("tok_a", 0.09), _opp("tok_b", 0.15)]
    rs = [_opp("tok_a", 0.09)]
    d = compute_diff(2, py, rs)

    assert d.agreed == 1
    assert d.python_only == [("tok_b", 0.15)]
    assert d.rust_only == []
    assert d.has_divergence


def test_rust_only_opportunity_flagged():
    py = [_opp("tok_a", 0.09)]
    rs = [_opp("tok_a", 0.09), _opp("tok_c", 0.22)]
    d = compute_diff(3, py, rs)

    assert d.agreed == 1
    assert d.python_only == []
    assert d.rust_only == [("tok_c", 0.22)]
    assert d.has_divergence


def test_edge_rounds_to_three_decimals():
    # 0.0804 → 0.08, 0.0796 → 0.08 — same bucket despite ~0.001 drift.
    py = [_opp("tok_a", 0.0804)]
    rs = [_opp("tok_a", 0.0796)]
    d = compute_diff(4, py, rs)

    assert d.agreed == 1
    assert not d.has_divergence


def test_edge_divergence_beyond_rounding():
    # 0.08 vs 0.09 are distinct at 3 decimals.
    py = [_opp("tok_a", 0.080)]
    rs = [_opp("tok_a", 0.090)]
    d = compute_diff(5, py, rs)

    assert d.agreed == 0
    assert d.python_only == [("tok_a", 0.08)]
    assert d.rust_only == [("tok_a", 0.09)]


def test_malformed_opps_are_silently_dropped():
    # A malformed row shouldn't blow up the whole diff — just skip it.
    py = [_opp("tok_a", 0.09), {"no_token_id": True}, {"token_id": "x"}]
    rs = [_opp("tok_a", 0.09), {"token_id": "y", "edge": "not-a-number"}]
    d = compute_diff(6, py, rs)

    assert d.agreed == 1
    assert d.python_count == 1
    assert d.rust_count == 1
    assert not d.has_divergence


def test_empty_inputs():
    d = compute_diff(7, [], [])
    assert d.agreed == 0
    assert d.python_count == 0
    assert d.rust_count == 0
    assert not d.has_divergence


def test_summary_no_divergence():
    d = ShadowDiff(tick_id=8, python_count=3, rust_count=3, agreed=3, python_only=[], rust_only=[])
    s = d.summary()
    assert "DIVERGENCE" not in s
    assert "tick=8" in s
    assert "agreed=3" in s


def test_summary_with_divergence():
    d = ShadowDiff(
        tick_id=9,
        python_count=3,
        rust_count=2,
        agreed=2,
        python_only=[("tok_x", 0.1)],
        rust_only=[],
    )
    s = d.summary()
    assert "DIVERGENCE" in s
    assert "py_only=1" in s
    assert "rs_only=0" in s


def test_duplicate_opportunities_dedup_by_key():
    # Python scanner sometimes emits duplicate entries for the same
    # token — they shouldn't inflate the count.
    py = [_opp("tok_a", 0.09), _opp("tok_a", 0.09)]
    rs = [_opp("tok_a", 0.09)]
    d = compute_diff(10, py, rs)

    assert d.python_count == 1
    assert d.rust_count == 1
    assert d.agreed == 1


def test_to_record_shape_no_divergence():
    d = ShadowDiff(tick_id=11, python_count=2, rust_count=2, agreed=2, python_only=[], rust_only=[])
    rec = d.to_record()

    assert rec["tick_id"] == 11
    assert rec["agreed"] == 2
    assert rec["has_divergence"] is False
    assert rec["python_only"] == []
    assert "ts" in rec
    # Must round-trip through JSON.
    json.dumps(rec)


def test_to_record_serializes_opp_tuples_as_lists():
    d = ShadowDiff(
        tick_id=12, python_count=1, rust_count=0, agreed=0,
        python_only=[("tok_a", 0.09)], rust_only=[],
    )
    rec = d.to_record()

    assert rec["python_only"] == [["tok_a", 0.09]]
    assert rec["has_divergence"] is True
    # No raw tuples — JSON can't encode them.
    json.dumps(rec)


def test_file_sink_disabled_when_no_path(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("POLYMM_SIDECAR_DIFF_LOG", raising=False)
    sink = ShadowDiffFileSink()
    assert not sink.enabled

    d = compute_diff(1, [_opp("tok_a", 0.09)], [_opp("tok_a", 0.09)])
    sink.write(d)  # no-op — must not raise


def test_file_sink_appends_one_ndjson_line_per_tick(tmp_path: Path):
    log_path = tmp_path / "diff.ndjson"
    sink = ShadowDiffFileSink(path=str(log_path))

    d1 = compute_diff(1, [_opp("tok_a", 0.09)], [_opp("tok_a", 0.09)])
    d2 = compute_diff(2, [_opp("tok_b", 0.10)], [])
    sink.write(d1)
    sink.write(d2)

    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    rec1 = json.loads(lines[0])
    rec2 = json.loads(lines[1])
    assert rec1["tick_id"] == 1 and rec1["has_divergence"] is False
    assert rec2["tick_id"] == 2 and rec2["has_divergence"] is True
    assert rec2["python_only"] == [["tok_b", 0.1]]


def test_file_sink_disables_itself_on_oserror(tmp_path: Path):
    # Point the sink at a path whose parent does not exist — first write
    # errors, subsequent writes must be silent no-ops.
    bogus = tmp_path / "does_not_exist" / "diff.ndjson"
    sink = ShadowDiffFileSink(path=str(bogus))
    assert sink.enabled  # until first failure

    d = compute_diff(1, [], [])
    sink.write(d)  # fails internally, disables the sink
    assert not sink.enabled
    sink.write(d)  # must not re-attempt / raise
