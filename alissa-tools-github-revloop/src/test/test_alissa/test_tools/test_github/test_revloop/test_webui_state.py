"""The console ledger bridge on State: read-only list readers, and the
retry-now ager that UPDATEs (never DELETEs) the newest row of a round."""

from __future__ import annotations

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
