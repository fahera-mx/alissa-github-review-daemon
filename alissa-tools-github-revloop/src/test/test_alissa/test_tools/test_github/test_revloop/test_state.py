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
          escalated=0, skipped=0, reaped=0, posted=0, awaiting_post=0,
          stages=None):
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
        posted=posted,
        awaiting_post=awaiting_post,
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


# -- operator re-entry grants ---------------------------------------------


def test_fresh_ledger_has_no_grants(ledger):
    assert ledger.read_grants(REPO, 7) == []
    assert ledger.granted_rounds(REPO, 7) == 0
    assert ledger.newest_grant(REPO, 7) is None


def test_a_grant_is_recorded_once_per_comment(ledger):
    """The ack comment stays on the PR forever, so the same id must never
    grant twice however many polls read it."""
    assert ledger.record_grant(REPO, 7, 1001, "rhdzmota", 1) is True
    assert ledger.record_grant(REPO, 7, 1001, "rhdzmota", 1) is False

    assert ledger.granted_rounds(REPO, 7) == 1
    assert len(ledger.read_grants(REPO, 7)) == 1


def test_grants_sum_across_comments(ledger):
    ledger.record_grant(REPO, 7, 1001, "rhdzmota", 1)
    ledger.record_grant(REPO, 7, 1002, "ops-bot", 2)

    assert ledger.granted_rounds(REPO, 7) == 3


def test_grants_are_scoped_per_pr(ledger):
    ledger.record_grant(REPO, 7, 1001, "rhdzmota", 2)

    assert ledger.granted_rounds(REPO, 8) == 0
    assert ledger.granted_rounds("other/repo", 7) == 0


def test_newest_grant_is_the_one_the_escalation_names(ledger, monkeypatch):
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(state_module.time, "time", lambda: clock["t"])
    ledger.record_grant(REPO, 7, 1001, "rhdzmota", 1)
    clock["t"] += 3600
    ledger.record_grant(REPO, 7, 1002, "ops-bot", 2)

    newest = ledger.newest_grant(REPO, 7)
    assert newest["author"] == "ops-bot"
    assert newest["rounds"] == 2
    assert newest["comment_id"] == 1002


def test_grants_read_back_newest_first(ledger, monkeypatch):
    """Newest first, like every reader here — the announce walk reverses it so
    each activity line reports its own cap transition, in order."""
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(state_module.time, "time", lambda: clock["t"])
    for i in range(3):
        ledger.record_grant(REPO, 7, 1000 + i, "rhdzmota", 1)
        clock["t"] += 60
    ledger.record_grant("other/repo", 9, 2001, "ops-bot", 1)

    rows = ledger.read_grants(REPO, 7)
    assert [r["comment_id"] for r in rows] == [1002, 1001, 1000]
    assert all(r["repo"] == REPO for r in rows), "another PR's grants never leak in"


def test_grants_survive_reopen(tmp_path):
    path = tmp_path / "state.db"
    with State(path) as st:
        st.record_grant(REPO, 7, 1001, "rhdzmota", 2)

    with State(path) as st:
        assert st.granted_rounds(REPO, 7) == 2
        # ...and a re-read of the same ack still grants nothing more.
        assert st.record_grant(REPO, 7, 1001, "rhdzmota", 2) is False


def test_grants_table_is_added_to_a_database_that_predates_it(tmp_path):
    """CREATE TABLE IF NOT EXISTS is the whole migration: an existing daemon's
    state.db gains `grants` on the next open, ledgers untouched."""
    path = tmp_path / "state.db"
    _legacy_db(path)

    with State(path) as st:
        assert st.get_spawn(REPO, 7, 1) is not None      # legacy data intact
        assert st.granted_rounds(REPO, 7) == 0
        assert st.record_grant(REPO, 7, 1001, "rhdzmota", 1) is True
        assert st.granted_rounds(REPO, 7) == 1


# -- native verdict posts (issue #51) ---------------------------------------


def test_a_verdict_post_row_keeps_its_first_observation(ledger):
    """The grace window before the daemon posts is measured from when the gap
    was FIRST seen — a timestamp that moved every poll would push the deadline
    out forever and the round would never close."""
    first = ledger.note_verdict_post_owed(REPO, 7, 1)
    again = ledger.note_verdict_post_owed(REPO, 7, 1)

    assert again == first
    row = ledger.get_verdict_post(REPO, 7, 1)
    assert row["attempts"] == 0 and row["posted_at"] is None


def test_failed_post_attempts_accumulate_with_the_last_error(ledger):
    ledger.note_verdict_post_owed(REPO, 7, 1)

    assert ledger.record_verdict_post_failure(REPO, 7, 1, "401 Bad credentials") == 1
    assert ledger.record_verdict_post_failure(REPO, 7, 1, "401 Bad credentials") == 2
    assert "401" in ledger.get_verdict_post(REPO, 7, 1)["last_error"]


def test_a_landed_post_clears_the_error_and_records_the_url(ledger):
    ledger.note_verdict_post_owed(REPO, 7, 1)
    ledger.record_verdict_post_failure(REPO, 7, 1, "boom")
    ledger.record_verdict_post(REPO, 7, 1, "https://github.com/acme/widgets/pull/7#r1")

    row = ledger.get_verdict_post(REPO, 7, 1)
    assert row["posted_at"] is not None
    assert row["last_error"] is None
    assert row["review_url"].endswith("#r1")
    assert ledger.read_verdict_posts(1)[0]["round"] == 1


def test_rounds_track_their_posts_independently(ledger):
    ledger.note_verdict_post_owed(REPO, 7, 1)
    ledger.record_verdict_post(REPO, 7, 1, "url-1")
    ledger.note_verdict_post_owed(REPO, 7, 2)

    assert ledger.get_verdict_post(REPO, 7, 1)["posted_at"] is not None
    assert ledger.get_verdict_post(REPO, 7, 2)["posted_at"] is None


def test_the_new_snapshot_columns_are_altered_into_an_older_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS is NOT a migration for a table that already
    exists: `posted` / `awaiting_post` have to be ALTERed in, and every
    historical row backfills to 0 (an old pass really did post no verdicts)."""
    path = tmp_path / "state.db"
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE poll_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
            candidates INTEGER NOT NULL, spawned INTEGER NOT NULL,
            stale_reenqueued INTEGER NOT NULL, in_flight INTEGER NOT NULL,
            deferred INTEGER NOT NULL, converged INTEGER NOT NULL,
            capped INTEGER NOT NULL, escalated INTEGER NOT NULL,
            skipped INTEGER NOT NULL, reaped INTEGER NOT NULL,
            stages_json TEXT NOT NULL
        );
        INSERT INTO poll_snapshots VALUES
            (1, 100, 5, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, '[]');
        """
    )
    con.commit()
    con.close()

    with State(path) as st:
        rows = st.read_snapshots()
        assert rows[0]["posted"] == 0 and rows[0]["awaiting_post"] == 0
        _snap(st, posted=1, awaiting_post=2, stages=[])
        newest = st.read_snapshots(1)[0]
        assert newest["posted"] == 1 and newest["awaiting_post"] == 2

    # Idempotent: a second open must not try to re-ALTER the same columns.
    with State(path) as st:
        assert len(st.read_snapshots()) == 2


def test_an_abandoned_verdict_post_is_terminal(ledger):
    """The exit for a post that can never succeed — the head it was pinned to
    is gone from the PR. Without it the round is held open forever and the PR
    leaves the review loop until a human edits this table."""
    ledger.note_verdict_post_owed(REPO, 7, 1, "abc123")
    assert ledger.verdict_post_abandoned(REPO, 7, 1) is False

    ledger.record_verdict_post_abandoned(REPO, 7, 1, "abc123 is gone from the PR")

    assert ledger.verdict_post_abandoned(REPO, 7, 1) is True
    row = ledger.get_verdict_post(REPO, 7, 1)
    assert row["posted_at"] is None, "abandoned is not posted"
    assert "gone from the PR" in row["last_error"]
    assert ledger.verdict_post_abandoned(REPO, 7, 2) is False, "per round"


def test_a_failed_attempt_stamps_when_it_ran(ledger):
    """The retry delay is measured from the last attempt, not from a fixed
    origin — so the attempt has to record when it happened."""
    ledger.note_verdict_post_owed(REPO, 7, 1, "abc123")
    assert ledger.get_verdict_post(REPO, 7, 1)["last_attempt_at"] is None

    ledger.record_verdict_post_failure(REPO, 7, 1, "boom")

    assert ledger.get_verdict_post(REPO, 7, 1)["last_attempt_at"] is not None


def test_the_judged_head_is_recorded_and_frozen(ledger):
    ledger.note_verdict_post_owed(REPO, 7, 1, "abc123")
    ledger.note_verdict_post_owed(REPO, 7, 1, "pushed-since")
    assert ledger.get_verdict_post(REPO, 7, 1)["head_sha"] == "abc123"
    assert ledger.read_verdict_posts(1)[0]["head_sha"] == "abc123"


def test_abandoned_rounds_counts_the_permanent_holes(ledger):
    """Each abandonment leaves an envelope with no review record forever, so
    every later comparison of the two counts has to subtract it."""
    assert ledger.abandoned_rounds(REPO, 7) == 0

    ledger.note_verdict_post_owed(REPO, 7, 1, "abc123")
    ledger.record_verdict_post_abandoned(REPO, 7, 1, "gone")
    ledger.note_verdict_post_owed(REPO, 7, 2, "def456")
    ledger.record_verdict_post(REPO, 7, 2, "url")

    assert ledger.abandoned_rounds(REPO, 7) == 1, "a posted round is not a hole"
    assert ledger.abandoned_rounds(REPO, 8) == 0, "per PR"
