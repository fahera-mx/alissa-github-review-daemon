"""State-layer tests for the `poll_snapshots` exhaust buffer.

The spawn/escalation/reap/ping ledgers are exercised through the decision
tests in test_loop.py; what is under test here is the snapshot table a future
console sidecar reads: that a row records and reads back every column, that the
per-item `stages` JSON round-trips through the reader, that the table is pruned
to SNAPSHOT_RETENTION on write, and that it is added in place to a database
that predates it.
"""

from __future__ import annotations

import sqlite3

import pytest

from alissa.tools.github.revloop import state as state_module
from alissa.tools.github.revloop.state import SNAPSHOT_RETENTION, State

REPO = "acme/widgets"


@pytest.fixture
def ledger(tmp_path):
    with State(tmp_path / "state.db") as st:
        yield st


def _snap(ledger, *, duration_ms=42, candidates=1, spawned=0,
          stale_reenqueued=0, in_flight=0, deferred=0, converged=0, capped=0,
          escalated=0, skipped=0, reaped=0, stages=None):
    """Record one poll snapshot with sensible defaults; overrides per call."""
    ledger.record_snapshot(
        duration_ms=duration_ms,
        candidates=candidates,
        spawned=spawned,
        stale_reenqueued=stale_reenqueued,
        in_flight=in_flight,
        deferred=deferred,
        converged=converged,
        capped=capped,
        escalated=escalated,
        skipped=skipped,
        reaped=reaped,
        stages=stages if stages is not None else [],
    )


def test_fresh_ledger_has_no_snapshots(ledger):
    assert ledger.read_snapshots() == []


def test_snapshot_records_and_reads_back_all_columns(ledger, monkeypatch):
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(state_module.time, "time", lambda: clock["t"])

    _snap(
        ledger, duration_ms=123, candidates=9, spawned=1, stale_reenqueued=2,
        in_flight=3, deferred=4, converged=5, capped=6, escalated=7,
        skipped=8, reaped=10,
    )
    rows = ledger.read_snapshots()
    assert len(rows) == 1
    row = rows[0]
    assert row["ts"] == 1_700_000_000
    assert row["duration_ms"] == 123
    assert row["candidates"] == 9
    assert row["spawned"] == 1
    assert row["stale_reenqueued"] == 2
    assert row["in_flight"] == 3
    assert row["deferred"] == 4
    assert row["converged"] == 5
    assert row["capped"] == 6
    assert row["escalated"] == 7
    assert row["skipped"] == 8
    assert row["reaped"] == 10


def test_count_kwargs_default_to_zero(ledger):
    """Every decision-count kwarg is optional (defaults to 0), so a caller
    passes only the ones a given pass produced and reads back real zeros."""
    ledger.record_snapshot(duration_ms=1, candidates=0, stages=[])
    row = ledger.read_snapshots()[0]
    for col in (
        "spawned", "stale_reenqueued", "in_flight", "deferred", "converged",
        "capped", "escalated", "skipped", "reaped",
    ):
        assert row[col] == 0, col


def test_snapshot_stages_json_round_trips_through_the_reader(ledger):
    """The reader the console depends on: the compact per-item stage list goes
    in as Python objects and comes back out identical, decoded from JSON."""
    stages = [
        {
            "slug": "acme/widgets#7",
            "number": 7,
            "round": 1,
            "attempt": None,
            "session": "review-acme-widgets-pr7-r1-abc123",
            "stage": "spawned",
            "reason": "session review-acme-widgets-pr7-r1-abc123 → TASK-500",
            "task_ref": "TASK-500",
        },
        {
            "slug": "acme/widgets#8",
            "number": 8,
            "round": 2,
            "attempt": None,
            "session": "review-acme-widgets-pr8-r2-def456",
            "stage": "deferred",
            "reason": "round 2 is stale but session is still busy",
            "task_ref": None,
        },
    ]
    _snap(ledger, stages=stages)

    read_back = ledger.read_snapshots()[0]["stages"]
    assert read_back == stages


def test_snapshots_read_newest_first(ledger, monkeypatch):
    clock = {"t": 1_000.0}
    monkeypatch.setattr(state_module.time, "time", lambda: clock["t"])
    for i in range(3):
        clock["t"] = 1_000.0 + i
        _snap(ledger, duration_ms=i)

    durations = [r["duration_ms"] for r in ledger.read_snapshots()]
    assert durations == [2, 1, 0], "newest snapshot first"


def test_snapshot_read_limit_caps_rows(ledger):
    for i in range(5):
        _snap(ledger, duration_ms=i)

    assert len(ledger.read_snapshots(limit=2)) == 2
    assert len(ledger.read_snapshots()) == 5


def test_retention_default_is_1000():
    """The retention target is fixed at 1000 -- a change to the constant is a
    change to the observable buffer size."""
    assert SNAPSHOT_RETENTION == 1000


def test_snapshots_prune_to_the_retention_boundary_on_write(ledger, monkeypatch):
    """The newest N rows are kept and the write that crosses N evicts the
    oldest -- pruned on the write itself, not lazily. Tested at a small N so
    the boundary is crisp; the constant's real value is pinned separately."""
    monkeypatch.setattr(state_module, "SNAPSHOT_RETENTION", 3)

    for i in range(3):
        _snap(ledger, duration_ms=i)
    assert [r["duration_ms"] for r in ledger.read_snapshots()] == [2, 1, 0]

    # The 4th write is the boundary crossing: still exactly 3 rows, and the
    # OLDEST (duration_ms=0) is the one evicted.
    _snap(ledger, duration_ms=3)
    kept = [r["duration_ms"] for r in ledger.read_snapshots()]
    assert kept == [3, 2, 1], "newest 3 kept, oldest pruned on write"


def _legacy_db(path):
    """A pre-snapshot state DB: the ledgers that existed before poll_snapshots,
    and nothing else. Written with raw sqlite3 so no current-code CREATE runs.
    The `spawns` table already carries the post-0.8 session primary key, so the
    only migration the open must perform is adding poll_snapshots."""
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE spawns (
            repo       TEXT    NOT NULL,
            number     INTEGER NOT NULL,
            round      INTEGER NOT NULL,
            head_sha   TEXT    NOT NULL,
            session    TEXT    NOT NULL PRIMARY KEY,
            task_ref   TEXT,
            spawned_at INTEGER NOT NULL
        );
        CREATE TABLE escalations (
            repo TEXT NOT NULL, number INTEGER NOT NULL,
            head_sha TEXT NOT NULL, escalated_at INTEGER NOT NULL,
            PRIMARY KEY (repo, number, head_sha)
        );
        """
    )
    con.execute(
        "INSERT INTO spawns VALUES (?,?,?,?,?,?,?)",
        (REPO, 7, 1, "abc123", "review-acme-widgets-pr7-r1-abc123", "TASK-500", 1_000),
    )
    con.commit()
    con.close()


def test_migrates_a_pre_snapshot_db_in_place(tmp_path):
    """Opening a database that predates poll_snapshots adds the table in place
    (CREATE TABLE IF NOT EXISTS) without disturbing the legacy ledgers."""
    path = tmp_path / "state.db"
    _legacy_db(path)

    with State(path) as st:
        # Legacy data survived the migration.
        row = st.get_spawn(REPO, 7, 1)
        assert row is not None
        assert row["session"] == "review-acme-widgets-pr7-r1-abc123"
        # The new table now exists and is usable.
        assert st.read_snapshots() == []
        _snap(st, spawned=1, stages=[{"slug": "acme/widgets#7", "number": 7}])
        rows = st.read_snapshots()
        assert len(rows) == 1
        assert rows[0]["spawned"] == 1
        assert rows[0]["stages"] == [{"slug": "acme/widgets#7", "number": 7}]


def test_snapshots_survive_reopen(tmp_path):
    path = tmp_path / "state.db"
    with State(path) as st:
        _snap(st, duration_ms=99, spawned=2)

    with State(path) as st:
        rows = st.read_snapshots()
        assert len(rows) == 1
        assert rows[0]["duration_ms"] == 99
        assert rows[0]["spawned"] == 2
