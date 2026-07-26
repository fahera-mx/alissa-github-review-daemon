"""The console ledger bridge on State: read-only list readers, and the
retry-now ager that UPDATEs (never DELETEs) the newest row of a round."""

from __future__ import annotations

import sqlite3

import pytest

from alissa.tools.github.revloop.state import State

REPO = "acme/widgets"


@pytest.fixture
def ledger(tmp_path):
    with State(tmp_path / "state.db") as st:
        yield st


def spawn(st, *, number=16, round_=1, session="review-widgets-pr16-r1-aaa",
          head_sha="deadbeef", task_ref="TASK-1"):
    st.record_spawn(repo=REPO, number=number, round_=round_, head_sha=head_sha,
                    session=session, task_ref=task_ref)


# -- readers ---------------------------------------------------------------

def test_read_spawns_shape_and_order(ledger):
    spawn(ledger, number=16, round_=1, session="s1")
    spawn(ledger, number=17, round_=1, session="s2")
    rows = ledger.read_spawns()
    assert len(rows) == 2
    assert set(rows[0]) == {"repo", "number", "round", "head_sha", "session",
                            "task_ref", "spawned_at"}
    # newest first: PR 17 (last inserted) leads on the tie-broken order
    assert rows[0]["number"] == 17


def test_read_escalations_and_pings(ledger):
    ledger.record_escalation(REPO, 16, "cafe1234")
    ledger.record_ping(REPO, 16, "stalled:review-widgets-pr16-r1-aaa")
    esc = ledger.read_escalations()
    assert set(esc[0]) == {"repo", "number", "head_sha", "escalated_at"}
    assert esc[0]["head_sha"] == "cafe1234"
    pings = ledger.read_pings()
    assert set(pings[0]) == {"repo", "number", "kind", "pinged_at"}
    assert pings[0]["kind"].startswith("stalled:")


def test_empty_tables_read_empty(ledger):
    assert ledger.read_spawns() == []
    assert ledger.read_escalations() == []
    assert ledger.read_pings() == []


# -- age_out_spawn: an UPDATE, per round, newest row ------------------------

def test_age_out_spawn_is_update_not_delete(ledger):
    spawn(ledger, session="s1")
    assert ledger.age_out_spawn(REPO, 16, 1, 1000) is True
    rows = ledger.read_spawns()
    assert len(rows) == 1  # the row still exists -- an UPDATE, not a DELETE
    assert rows[0]["spawned_at"] == 1000
    assert rows[0]["session"] == "s1"  # session mapping preserved for the sweep


def test_age_out_spawn_targets_newest_row_of_the_round(ledger):
    """A stalled round can be re-enqueued, so one round may carry several
    spawns; retry-now must age the newest -- the very row `get_spawn` /
    `spawn_age` read, so the daemon sees the retry."""
    spawn(ledger, session="first")
    spawn(ledger, session="second")
    # Same wall-clock second: the insertion order (rowid) breaks the tie, and
    # the second spawn is the newest.
    assert ledger.age_out_spawn(REPO, 16, 1, 500) is True
    by_session = {r["session"]: r["spawned_at"] for r in ledger.read_spawns()}
    assert by_session["second"] == 500
    assert by_session["first"] != 500
    assert len(by_session) == 2  # nothing deleted
    assert ledger.get_spawn(REPO, 16, 1)["session"] == "first"  # newest by ts now
    # ...and a second retry follows spawn_age's row, not the insertion order
    assert ledger.age_out_spawn(REPO, 16, 1, 400) is True
    assert {r["session"]: r["spawned_at"] for r in ledger.read_spawns()}["first"] == 400


def test_age_out_spawn_is_per_round(ledger):
    spawn(ledger, round_=1, session="r1")
    spawn(ledger, round_=2, session="r2")
    assert ledger.age_out_spawn(REPO, 16, 2, 700) is True
    by_round = {r["round"]: r["spawned_at"] for r in ledger.read_spawns()}
    assert by_round[2] == 700
    assert by_round[1] != 700  # the other round is untouched


def test_age_out_spawn_makes_round_stale(ledger):
    spawn(ledger, session="s1")
    assert ledger.spawn_age(REPO, 16, 1) < 5  # freshly spawned
    ledger.age_out_spawn(REPO, 16, 1, 0)  # epoch -> ancient
    assert ledger.spawn_age(REPO, 16, 1) > 60  # now reads stale to the daemon


def test_age_out_absent_row_returns_false(ledger):
    spawn(ledger, session="s1")
    assert ledger.age_out_spawn(REPO, 99, 1, 0) is False   # other PR
    assert ledger.age_out_spawn(REPO, 16, 9, 0) is False   # other round
    assert ledger.age_out_spawn("other/repo", 16, 1, 0) is False


# -- read-only mode: a consumer must not create or migrate the daemon's DB ---

def test_read_only_refuses_to_create_the_database(tmp_path):
    missing = tmp_path / "nope" / "state.db"
    with pytest.raises(sqlite3.OperationalError):
        State(missing, read_only=True)
    assert not missing.exists()
    assert not missing.parent.exists()  # not even the .revloop dir


@pytest.mark.parametrize("name", ["ws#1", "ws?x", "ws%2f", "ws with space",
                                  "ws&a=b", "plain"])
def test_read_only_uri_escapes_path_metacharacters(tmp_path, name):
    """sqlite parses `file:` URIs, so an unescaped path is a different file.

    `#` truncates at a fragment and `?` at a query -- both dropping `mode=ro`
    and falling back to `rwc`, i.e. read-only mode CREATING a database, at a
    path nobody asked for. `%XX` is percent-decoded. Each must read its own
    seeded row and leave the tree untouched.
    """
    root = tmp_path / name
    root.mkdir()
    db = root / "state.db"
    with State(db) as st:
        spawn(st, session="s1")
    before = {p for p in tmp_path.rglob("*")}

    with State(db, read_only=True) as ro:
        rows = ro.read_spawns()

    assert [r["session"] for r in rows] == ["s1"]
    assert {p for p in tmp_path.rglob("*")} == before  # nothing created


def test_read_only_refuses_to_create_a_metacharacter_path(tmp_path):
    """The absent-db signal the console's banner depends on must survive
    escaping too -- an OperationalError, not a silently created file."""
    missing = tmp_path / "ws#1" / "nope" / "state.db"
    with pytest.raises(sqlite3.OperationalError):
        State(missing, read_only=True)
    assert not missing.exists()
    assert not missing.parent.exists()


def test_read_only_reads_but_never_writes(tmp_path):
    db = tmp_path / "state.db"
    with State(db) as st:
        spawn(st, session="s1")
    with State(db, read_only=True) as ro:
        assert len(ro.read_spawns()) == 1
        with pytest.raises(sqlite3.OperationalError):
            ro.record_escalation(REPO, 16, "sha")


# -- bounded reads (escalations/pings are never pruned) ---------------------

def test_readers_take_a_limit(ledger):
    for n in range(5):
        spawn(ledger, number=20 + n, session=f"s{n}")
        ledger.record_escalation(REPO, 20 + n, f"sha{n}")
        ledger.record_ping(REPO, 20 + n, f"stalled:s{n}")
    assert len(ledger.read_spawns()) == 5
    assert len(ledger.read_spawns(2)) == 2
    assert len(ledger.read_escalations(2)) == 2
    assert len(ledger.read_pings(2)) == 2


def test_read_pings_filters_the_kind_before_the_limit(ledger):
    """The LIMIT must bound the rows the caller WANTS.

    `activity-deferred:*` is written once per deferral episode and
    `stalled:*` only for the long ones, so filtering after the limit would let
    telemetry evict every operator page from a bounded read.
    """
    ledger.record_ping(REPO, 16, "stalled:wedged")
    for n in range(20):
        ledger.record_ping(REPO, 100 + n, f"activity-deferred:s{n}")

    rows = ledger.read_pings(5, kind_prefix="stalled:")
    assert [r["kind"] for r in rows] == ["stalled:wedged"]
    # unfiltered, the same window is entirely telemetry -- the defect
    assert all(r["kind"].startswith("activity-deferred:")
               for r in ledger.read_pings(5))
    # the prefix is matched literally, not as a LIKE pattern
    assert ledger.read_pings(kind_prefix="stalled:%") == []
    assert ledger.read_pings(kind_prefix="_talled:") == []


def test_read_spawns_selects_by_session_at_any_age(ledger):
    """The pairing lookup is keyed, not truncated: a wedged session's row is
    the OLDEST in the ledger and must still resolve."""
    spawn(ledger, number=16, round_=1, session="wedged")
    for n in range(50):
        spawn(ledger, number=300 + n, session=f"other-{n}")

    rows = ledger.read_spawns(sessions=["wedged"])
    assert [r["session"] for r in rows] == ["wedged"]
    assert rows[0]["number"] == 16
    # a name that is not in the ledger is simply unpaired, not an error
    assert ledger.read_spawns(sessions=["wedged", "ghost"]) == rows
    assert ledger.read_spawns(sessions=[]) == []
    # no argument keeps the unrestricted read
    assert len(ledger.read_spawns()) == 51
