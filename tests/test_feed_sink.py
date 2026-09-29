"""Tests for src/feeds/sink.py — the conformance gate in front of the table."""
from datetime import datetime, timedelta, timezone

import pytest

from src.feeds.conformance import IssueCode
from src.feeds.sink import ON_CONFLICT, TABLE, partition_rows, write_rows

NOW = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)


def _row(**overrides):
    row = {
        "match_id": "basketball:bostonceltics:vs:miamiheat",
        "source": "candidate-feed", "sport": "basketball_nba",
        "team1": "Boston Celtics", "team2": "Miami Heat",
        "market_type": "h2h", "line": 0,
        "outcome1_name": "Boston Celtics", "outcome2_name": "Miami Heat",
        "odds1": 1.80, "odds2": 2.10,
        "fair_prob1": 53.85, "fair_prob2": 46.15,
        "bookmaker_count": 6, "is_live": False,
        "scraped_at": NOW.isoformat(),
    }
    row.update(overrides)
    return row


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeTable:
    def __init__(self, client, name):
        self._client = client
        self._name = name
        self._rows = None
        self._on_conflict = None

    def upsert(self, rows, on_conflict=None):
        self._rows = rows
        self._on_conflict = on_conflict
        return self

    def execute(self):
        self._client.calls.append((self._name, self._rows, self._on_conflict))
        if self._client.fail:
            raise RuntimeError("supabase is down")
        return FakeResponse(self._rows)


class FakeClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def table(self, name):
        return FakeTable(self, name)


# ── Partitioning ───────────────────────────────────────────────────────────

def test_good_rows_are_accepted():
    result = partition_rows([_row()])
    assert len(result.accepted) == 1
    assert result.rejected == ()


def test_bad_row_is_rejected_with_its_reason():
    result = partition_rows([_row(match_id="wrong")])
    assert result.accepted == ()
    assert result.rejected_count == 1
    _, issues = result.rejected[0]
    assert IssueCode.MATCH_ID_MISMATCH in {i.code for i in issues}


def test_one_bad_row_does_not_block_the_others():
    """A producer with one bad market should still publish the rest."""
    rows = [_row(source="a"), _row(source="b", match_id="wrong"), _row(source="c")]
    result = partition_rows(rows)
    assert len(result.accepted) == 2
    assert result.rejected_count == 1
    assert {r["source"] for r in result.accepted} == {"a", "c"}


def test_colliding_rows_are_rejected_so_one_cannot_overwrite_the_other():
    result = partition_rows([_row(), _row()])
    assert len(result.accepted) == 1
    assert result.rejected_count == 1
    _, issues = result.rejected[0]
    assert IssueCode.SOURCE_COLLISION in {i.code for i in issues}


def test_freshness_is_not_checked_by_default():
    old = _row(scraped_at=(NOW - timedelta(hours=5)).isoformat())
    assert len(partition_rows([old], now=NOW).accepted) == 1


def test_freshness_can_be_required():
    old = _row(scraped_at=(NOW - timedelta(hours=5)).isoformat())
    result = partition_rows([old], now=NOW, check_freshness=True)
    assert result.accepted == ()
    _, issues = result.rejected[0]
    assert IssueCode.STALE_HARD_REJECT in {i.code for i in issues}


def test_empty_input_is_not_an_error():
    result = partition_rows([])
    assert result.accepted == () and result.rejected == ()


# ── Writing ────────────────────────────────────────────────────────────────

def test_accepted_rows_are_upserted_with_the_right_conflict_target():
    client = FakeClient()
    result = write_rows(client, [_row()])
    assert result.written == 1
    table, rows, on_conflict = client.calls[0]
    assert table == TABLE
    assert on_conflict == ON_CONFLICT
    assert len(rows) == 1


def test_rejected_rows_are_never_written():
    client = FakeClient()
    result = write_rows(client, [_row(match_id="wrong")])
    assert result.written == 0
    assert client.calls == []
    assert result.rejected_count == 1


def test_a_bad_row_cannot_overwrite_a_good_one_already_in_the_table():
    """The row that would clobber a good row is the row the gate stops."""
    client = FakeClient()
    result = write_rows(client, [_row(fair_prob1=0)])
    assert client.calls == []
    codes = {i.code for _, issues in result.rejected for i in issues}
    assert IssueCode.SILENTLY_DROPPED in codes


def test_large_batches_are_chunked():
    client = FakeClient()
    rows = [_row(source=f"feed-{i}") for i in range(120)]
    result = write_rows(client, rows)
    assert result.written == 120
    assert [len(call[1]) for call in client.calls] == [50, 50, 20]


def test_upsert_failure_is_reported_not_raised():
    client = FakeClient(fail=True)
    result = write_rows(client, [_row()])
    assert result.written == 0
    assert len(result.accepted) == 1  # it passed the gate; the write failed


def test_gate_can_be_bypassed_for_known_good_rows():
    client = FakeClient()
    result = write_rows(client, [_row(match_id="wrong")], gate=False)
    assert result.written == 1


def test_summary_names_the_rejection_reasons():
    result = partition_rows([_row(), _row(source="b", match_id="wrong")])
    assert "REJECTED" in result.summary()
    assert "match_id_mismatch" in result.summary()


def test_summary_is_clean_when_nothing_is_rejected():
    assert "REJECTED" not in partition_rows([_row()]).summary()


# ── The Betfair producer's output goes through the gate unmodified ─────────

def test_betfair_rows_pass_the_gate():
    from tests.test_betfair_feed import _NAMES, MARKET, _state
    from src.feeds.betfair.producer import rows_from_state

    produced = rows_from_state(_state(), {MARKET: _NAMES},
                               game_by_market={MARKET: "football"})
    client = FakeClient()
    result = write_rows(client, produced.rows)
    assert result.rejected == (), result.summary()
    assert result.written == len(produced.rows) == 1
