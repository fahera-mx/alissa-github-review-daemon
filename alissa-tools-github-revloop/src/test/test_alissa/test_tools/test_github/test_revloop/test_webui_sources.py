"""The read-only data layer: payload shape from a seeded state.db, the two
cached checks, session parsing, drift, inbox links, and the retry-now UPDATE."""

from __future__ import annotations

import json
import sqlite3

import pytest

from alissa.tools.github.revloop.config import Config
from alissa.tools.github.revloop.loop import STALE_ROUND_SECONDS
from alissa.tools.github.revloop.state import State
from alissa.tools.github.revloop.webui import sources as sources_mod
from alissa.tools.github.revloop.webui.sources import (
    RETRY_NO_ROW,
    RETRY_OK,
    RETRY_UNAVAILABLE,
    Sources,
    is_managed,
)

SESSION = "review-widgets-pr16-r1-ab12cd"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


def seed(db_path):
    with State(db_path) as st:
        st.record_spawn(repo="acme/widgets", number=16, round_=1,
                        head_sha="cafe1234", session=SESSION, task_ref="TASK-9")
        st.record_escalation("acme/widgets", 12, "deadbeefcafe")
        st.record_ping("acme/widgets", 16, "stalled:" + SESSION)
        # telemetry dedupe, NOT an operator page -- must not reach the inbox
        st.record_ping("acme/widgets", 16, "activity-deferred:" + SESSION)
        st.record_snapshot(
            duration_ms=42, candidates=2, spawned=1, in_flight=0, deferred=1,
            converged=0, capped=0, escalated=0, skipped=0, reaped=0,
            stages=[
                {"slug": "acme/widgets#16", "number": 16, "round": 1,
                 "attempt": None, "session": SESSION, "stage": "spawned",
                 "reason": "session " + SESSION, "task_ref": "TASK-9"},
                {"slug": "acme/widgets#12", "number": 12, "round": None,
                 "attempt": None, "session": None, "stage": "capped",
                 "reason": "already escalated", "task_ref": None},
            ],
        )


def make_sources(tmp_path, *, runner=None, http=None, clock=None, proc_root="/proc",
                 log_path=None):
    config = Config.build(tmp_path, {"repos": ["acme/widgets"]}, {})
    seed(config.state_db)

    def default_run(argv, **kw):
        raise AssertionError(f"unexpected run: {argv}")

    def default_http(url, timeout):
        return json.dumps({"info": {"version": "0.9.0"}}).encode()

    clk = clock or Clock()
    return Sources(
        config=config, running_version="0.14.0", log_path=log_path,
        run=runner or default_run, http_get=http or default_http,
        proc_root=proc_root, clock=clk, wall_clock=lambda: 5000.0,
    )


def _quiet_runner(argv, **kw):
    """No sessions, no rate limit -- for tests about everything else."""
    if argv[:3] == ["alissa", "tmux", "ls"]:
        return "[]"
    if argv[:3] == ["gh", "api", "rate_limit"]:
        return "{}"
    raise AssertionError(argv)


# -- managed-session namespace ---------------------------------------------

def test_is_managed():
    assert is_managed(SESSION) is True
    assert is_managed("develop-acme-widgets-i7-a1") is False
    assert is_managed("") is False
    assert is_managed(None) is False


# -- state reads -----------------------------------------------------------

def test_snapshots_and_ledgers(tmp_path):
    src = make_sources(tmp_path)
    assert len(src.snapshots()) == 1
    led = src.ledgers()
    assert len(led["escalations"]) == 1
    # the spawn ledger is NOT an inbox table: it is read by key, in sessions()
    assert "spawns" not in led
    # the reader filters the telemetry kind in SQL -- the seed writes one of
    # each, only the `stalled:` one is a page
    assert len(led["pings"]) == 1
    assert led["pings"][0]["kind"] == "stalled:" + SESSION


def test_state_read_degrades_on_lock(tmp_path, monkeypatch):
    """A write collision with the daemon (sqlite3.OperationalError) must degrade
    to empty/False -- never bubble as a 500 / blanked dashboard. Honors the
    module contract for the read paths AND the retry mutation."""
    src = make_sources(tmp_path)

    class LockedState:
        def __init__(self, path, *, read_only=False):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def _locked(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

        read_snapshots = read_spawns = read_escalations = _locked
        read_pings = age_out_spawn = _locked

    monkeypatch.setattr(sources_mod, "State", LockedState)
    assert src.snapshots() == []
    assert src.ledgers() == {"escalations": [], "pings": []}
    # the retry mutation degrades to a clean failure, not a 500 -- and says
    # "state unavailable", never "no ledger row" (that is an operator-error
    # outcome, and conflating them would corrupt the audit trail)
    assert src.retry_now("acme/widgets", 16, 1) == RETRY_UNAVAILABLE


# -- sessions --------------------------------------------------------------

def test_sessions_parsed_gone_skips_proc(tmp_path):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return json.dumps([
                {"name": SESSION, "session": "s1", "status": "busy",
                 "live": True, "lastActivity": 4970},
                {"name": "review-widgets-pr17-r1-ffff", "session": "s2",
                 "status": "gone", "live": False},
            ])
        if argv[:2] == ["tmux", "list-panes"]:
            return ""  # no pane -> usage stays None
        raise AssertionError(argv)

    src = make_sources(tmp_path, runner=runner)
    sess = src.sessions()
    assert [s["name"] for s in sess] == [SESSION, "review-widgets-pr17-r1-ffff"]
    assert sess[0]["busy"] is True
    assert sess[0]["age_seconds"] == 30  # 5000 - 4970
    assert sess[0]["managed"] is True
    # the ledger pairs the session with its PR round and drives retry-now
    assert sess[0]["pr"] == "acme/widgets#16"
    assert sess[0]["round"] == 1
    assert sess[0]["retry"] == {"repo_slug": "acme/widgets", "number": 16, "round": 1}
    # the gone session is never walked in /proc, and has no ledger row
    assert not any(a[:2] == ["tmux", "list-panes"] and "s2" in a for a in calls)
    assert sess[1]["cpu_percent"] is None
    assert sess[1]["retry"] is None


def test_sessions_unmanaged_and_unpaired(tmp_path):
    """A session outside the reviewer namespace still shows (it holds a worker
    slot) but is marked unmanaged and carries no PR/retry."""
    def runner(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return json.dumps([{"name": "develop-acme-widgets-i7-a1",
                                "session": "s9", "status": "idle", "live": False}])
        raise AssertionError(argv)

    src = make_sources(tmp_path, runner=runner)
    row = src.sessions()[0]
    assert row["managed"] is False
    assert row["pr"] is None and row["retry"] is None


def test_sessions_tolerates_bad_json(tmp_path):
    src = make_sources(tmp_path, runner=lambda argv, **kw: "not json")
    assert src.sessions() == []


def test_sessions_walks_proc_for_live_pane(tmp_path):
    proc = tmp_path / "proc"
    (proc / "77").mkdir(parents=True)
    tail = ["S"] + ["0"] * 21
    tail[1] = "1"
    tail[21] = "10"  # rss pages
    (proc / "77" / "stat").write_text("77 (claude) " + " ".join(tail))

    def runner(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return json.dumps([{"name": SESSION, "session": "s1",
                                "status": "busy", "live": True}])
        if argv[:2] == ["tmux", "list-panes"]:
            return "77\n"
        raise AssertionError(argv)

    src = make_sources(tmp_path, runner=runner, proc_root=str(proc))
    row = src.sessions()[0]
    assert row["pane_pid"] == 77
    assert row["rss_bytes"] == 10 * sources_mod.sysinfo._PAGE_SIZE


def test_pane_pid_bad_output_is_none(tmp_path):
    src = make_sources(tmp_path, runner=lambda argv, **kw: "not-a-pid\n")
    assert src.pane_pid("s1") is None


# -- rate limit cache ------------------------------------------------------

def test_rate_limit_parsed_and_cached(tmp_path):
    clock = Clock()
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return json.dumps({"resources": {"core": {
            "limit": 5000, "remaining": 4990, "used": 10, "reset": 123}}})

    src = make_sources(tmp_path, runner=runner, clock=clock)
    r1 = src.rate_limit()
    assert r1["remaining"] == 4990
    src.rate_limit()  # within TTL -> cached, no second call
    assert len(calls) == 1
    clock.tick(sources_mod.RATE_CACHE_TTL + 1)
    src.rate_limit()
    assert len(calls) == 2


def test_rate_limit_failure_returns_none(tmp_path):
    from alissa.tools.github.revloop.proc import CommandError

    def runner(argv, **kw):
        raise CommandError(argv, 1, "boom")

    src = make_sources(tmp_path, runner=runner)
    assert src.rate_limit() is None


def test_version_check_cached(tmp_path):
    clock = Clock()
    calls = []

    def http(url, timeout):
        calls.append(url)
        return json.dumps({"info": {"version": "0.14.0"}}).encode()

    src = make_sources(tmp_path, http=http, clock=clock)
    assert src.latest_version() == "0.14.0"
    src.latest_version()
    assert len(calls) == 1  # within TTL -> cached
    clock.tick(sources_mod.VERSION_CACHE_TTL + 1)
    src.latest_version()
    assert len(calls) == 2
    assert calls[0].endswith("/alissa-tools-github-revloop/json")


def test_version_cache_keeps_last_good_value(tmp_path):
    """A failed fetch must not overwrite the last known latest version."""
    clock = Clock()
    bodies = [json.dumps({"info": {"version": "0.14.0"}}).encode(), None]

    def http(url, timeout):
        return bodies.pop(0) if bodies else None

    src = make_sources(tmp_path, http=http, clock=clock)
    assert src.latest_version() == "0.14.0"
    clock.tick(sources_mod.VERSION_CACHE_TTL + 1)
    assert src.latest_version() == "0.14.0"  # stale-but-good, not None


# -- drift -----------------------------------------------------------------

@pytest.mark.parametrize("running,latest,state", [
    ("0.14.0", "0.14.0", "current"),
    ("0.13.0", "0.14.0", "behind"),
    ("1.0.0", "0.14.0", "ahead"),
])
def test_drift_states(tmp_path, running, latest, state):
    src = make_sources(tmp_path, http=lambda u, t: json.dumps(
        {"info": {"version": latest}}).encode())
    src.running_version = running
    assert src.drift()["state"] == state


def test_drift_unknown_when_pypi_unreachable(tmp_path):
    src = make_sources(tmp_path, http=lambda u, t: None)
    assert src.drift()["state"] == "unknown"


def test_drift_unparseable_version_falls_back_to_equality(tmp_path):
    src = make_sources(tmp_path, http=lambda u, t: json.dumps(
        {"info": {"version": "0.14.0rc1"}}).encode())
    assert src.drift()["state"] == "behind"


# -- inbox -----------------------------------------------------------------

def test_inbox_pages_capouts_and_stalls_only(tmp_path):
    src = make_sources(tmp_path)
    led = src.ledgers()
    inbox = src._inbox(led["escalations"], led["pings"])
    kinds = [item["kind"] for item in inbox]
    assert sorted(kinds) == ["cap-out", "stalled"]  # activity-deferred filtered
    by_kind = {item["kind"]: item for item in inbox}
    # every reviewer-edge reference is a PR reference
    assert by_kind["cap-out"]["url"] == "https://github.com/acme/widgets/pull/12"
    assert by_kind["stalled"]["url"] == "https://github.com/acme/widgets/pull/16"
    assert by_kind["cap-out"]["detail"] == "deadbeef"        # short head sha
    assert by_kind["stalled"]["detail"] == SESSION           # the stalled session


def test_inbox_ages_and_sorts_newest_first(tmp_path):
    src = make_sources(tmp_path)
    inbox = src._inbox(
        [{"repo": "acme/widgets", "number": 12, "head_sha": "aa",
          "escalated_at": 1000}],
        [{"repo": "acme/widgets", "number": 16, "kind": "stalled:s",
          "pinged_at": 4900}],
    )
    assert [i["age_seconds"] for i in inbox] == [100, 4000]  # wall clock 5000


# -- log tail --------------------------------------------------------------

def test_log_tail_none_and_present(tmp_path):
    src = make_sources(tmp_path)
    assert src.log_tail()["lines"] == []
    logf = tmp_path / "daemon.log"
    logf.write_text("\n".join(f"line {i}" for i in range(500)))
    src.log_path = logf
    tail = src.log_tail(lines=10)
    assert len(tail["lines"]) == 10
    assert tail["lines"][-1] == "line 499"


def test_log_tail_unreadable_path(tmp_path):
    src = make_sources(tmp_path, log_path=tmp_path / "nope.log")
    assert src.log_tail() == {"path": str(tmp_path / "nope.log"), "lines": []}


# -- retry-now -------------------------------------------------------------

def test_retry_now_ages_past_the_daemons_stale_window(tmp_path):
    src = make_sources(tmp_path)
    assert src.retry_now("acme/widgets", 16, 1) == RETRY_OK
    with State(src.config.state_db) as st:
        row = st.read_spawns()[0]
        # wall_clock 5000, minus the daemon's own stale window, minus the buffer
        assert row["spawned_at"] == (
            5000 - STALE_ROUND_SECONDS - sources_mod.RETRY_AGE_BUFFER
        )
        # the ledger row survives the retry (UPDATE, not DELETE)
        assert row["session"] == SESSION


def test_retry_now_absent_round_says_no_row(tmp_path):
    src = make_sources(tmp_path)
    assert src.retry_now("acme/widgets", 999, 1) == RETRY_NO_ROW
    assert src.retry_now("acme/widgets", 16, 7) == RETRY_NO_ROW


# -- config echo -----------------------------------------------------------

def test_config_echo_reports_reviewer_semantics(tmp_path):
    src = make_sources(tmp_path)
    echo = src.config_echo()
    assert echo["round_cap"] == src.config.round_cap
    assert echo["stale_round_seconds"] == STALE_ROUND_SECONDS
    assert echo["repos"] == ["acme/widgets"]
    assert echo["state_db"] == str(src.config.state_db)


# -- full dashboard payload ------------------------------------------------

def test_dashboard_shape(tmp_path):
    def runner(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return json.dumps([{"name": SESSION, "session": "s1",
                                "status": "busy", "live": True,
                                "lastActivity": 4970}])
        if argv[:2] == ["tmux", "list-panes"]:
            return ""
        if argv[:3] == ["gh", "api", "rate_limit"]:
            return json.dumps({"resources": {"core": {
                "limit": 5000, "remaining": 4900, "used": 100, "reset": 0}}})
        raise AssertionError(argv)

    src = make_sources(tmp_path, runner=runner)
    d = src.dashboard()
    assert set(d) >= {"header", "config", "tiles", "sparklines", "pipeline",
                      "inbox", "sessions", "log", "generated_at"}
    # reviewers create no tasks -- there is no worker-tasks panel
    assert "tasks" not in d
    assert d["header"]["round_cap"] == src.config.round_cap
    # the tile counts REVIEWER sessions; the host-wide live count rides along
    assert d["tiles"]["active_sessions"] == 1
    assert d["tiles"]["live_sessions"] == 1
    assert d["tiles"]["rate"]["remaining"] == 4900
    assert d["tiles"]["queue_depth"] == 2  # candidates in the newest snapshot
    assert d["sparklines"]["poll_duration_ms"] == [42]
    assert d["sparklines"]["active_sessions"] == [1]  # in_flight 0 + deferred 1

    # pipeline: PR-centric, round k of the cap, with a retry descriptor
    items = d["pipeline"]["items"]
    assert [i["slug"] for i in items] == ["acme/widgets#16", "acme/widgets#12"]
    assert items[0]["repo_slug"] == "acme/widgets"
    assert items[0]["round"] == 1 and items[0]["round_cap"] == d["pipeline"]["round_cap"]
    assert items[0]["stage"] == "spawned"
    assert items[0]["url"] == "https://github.com/acme/widgets/pull/16"
    assert items[0]["retry"] == {"repo_slug": "acme/widgets", "number": 16, "round": 1}
    # a capped PR carries no round -> nothing to retry
    assert items[1]["retry"] is None

    assert len(d["inbox"]) == 2
    assert d["sessions"][0]["retry"]["number"] == 16


def test_tiles_count_reviewer_sessions_apart_from_the_hosts(tmp_path):
    """Another daemon's workers hold worker slots and belong in the table, but
    the headline tile answers "how many reviewer rounds are being worked"."""
    def runner(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return json.dumps([
                {"name": SESSION, "session": "s1", "status": "busy", "live": True},
                {"name": "develop-acme-widgets-i7-a1", "session": "s2",
                 "status": "busy", "live": True},
            ])
        if argv[:2] == ["tmux", "list-panes"]:
            return ""
        if argv[:3] == ["gh", "api", "rate_limit"]:
            return "{}"
        raise AssertionError(argv)

    tiles = make_sources(tmp_path, runner=runner).dashboard()["tiles"]
    assert tiles["active_sessions"] == 1
    assert tiles["live_sessions"] == 2


def test_dashboard_empty_state(tmp_path):
    """No snapshots / sessions: the payload is still well-formed (empty)."""
    config = Config.build(tmp_path, {"repos": ["acme/widgets"]}, {})
    src = Sources(config=config, running_version="0.14.0",
                  run=lambda argv, **kw: "[]" if argv[1] == "tmux" else "",
                  http_get=lambda u, t: None, wall_clock=lambda: 1.0)
    d = src.dashboard()
    assert d["tiles"]["queue_depth"] == 0
    assert d["tiles"]["active_sessions"] == 0 and d["tiles"]["live_sessions"] == 0
    assert d["pipeline"]["items"] == []
    assert d["pipeline"]["snapshot_ts"] is None
    assert d["sessions"] == []
    assert d["inbox"] == []


def test_dashboard_spends_no_github_budget_beyond_the_cached_checks(tmp_path):
    """Zero GitHub polling of its own: the only `gh` call a dashboard build may
    make is the cached rate_limit read."""
    seen = []

    def runner(argv, **kw):
        seen.append(list(argv))
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return "[]"
        if argv[:3] == ["gh", "api", "rate_limit"]:
            return json.dumps({"resources": {"core": {"limit": 1, "remaining": 1,
                                                      "used": 0, "reset": 0}}})
        raise AssertionError(argv)

    src = make_sources(tmp_path, runner=runner)
    src.dashboard()
    src.dashboard()  # a second poll: rate_limit is cached, so no second gh call
    gh_calls = [a for a in seen if a and a[0] == "gh"]
    assert gh_calls == [["gh", "api", "rate_limit"]]


# -- read-only posture: the console never creates the daemon's state --------

def test_reads_never_create_the_state_db(tmp_path):
    """A wrong --workspace-root must not leave a phantom .revloop/state.db --
    and must be distinguishable from an idle daemon."""
    config = Config.build(tmp_path / "elsewhere", {}, {})
    src = Sources(config=config, running_version="0.14.0",
                  run=lambda argv, **kw: "[]", http_get=lambda u, t: None,
                  wall_clock=lambda: 5000.0)
    assert src.state_present() is False
    assert src.snapshots() == []
    assert src.ledgers() == {"escalations": [], "pings": []}
    assert src.spawn_pairs([SESSION]) == {}
    assert not config.state_db.exists()
    assert not config.state_db.parent.exists()

    payload = src.dashboard()
    assert payload["header"]["state_present"] is False
    assert payload["header"]["state_db"] == str(config.state_db)


def test_retry_never_creates_the_state_db(tmp_path):
    config = Config.build(tmp_path / "elsewhere", {}, {})
    src = Sources(config=config, running_version="0.14.0",
                  run=lambda argv, **kw: "", http_get=lambda u, t: None,
                  wall_clock=lambda: 5000.0)
    assert src.retry_now("acme/widgets", 16, 1) == RETRY_UNAVAILABLE
    assert not config.state_db.exists()


def test_state_present_true_for_a_seeded_workspace(tmp_path):
    src = make_sources(tmp_path, runner=_quiet_runner)
    assert src.state_present() is True
    assert src.dashboard()["header"]["state_present"] is True


# -- caching a FAILED check (negative cache) --------------------------------

def test_failed_remote_checks_are_retried_once_per_window(tmp_path):
    """A missing `gh` / unreachable PyPI must not be retried on every poll --
    each /api/state would otherwise block on the full timeouts."""
    from alissa.tools.github.revloop.proc import CommandError

    clock = Clock()
    gh_calls, pypi_calls = [], []

    def runner(argv, **kw):
        gh_calls.append(argv)
        raise CommandError(argv, 1, "gh: not found")

    def http(url, timeout):
        pypi_calls.append(url)
        return None

    src = make_sources(tmp_path, runner=runner, http=http, clock=clock)
    for _ in range(6):  # six polls, 10s apart, inside both windows
        assert src.rate_limit() is None
        assert src.latest_version() is None
        clock.tick(10)
    assert len(gh_calls) == 1
    assert len(pypi_calls) == 1
    # ...and the window still expires: the next poll past the TTL retries
    clock.tick(sources_mod.RATE_CACHE_TTL)
    src.rate_limit()
    assert len(gh_calls) == 2


# -- one /proc snapshot per dashboard build ---------------------------------

def test_sessions_builds_the_proc_index_once(tmp_path, monkeypatch):
    """The index is identical for every session in one build; rebuilding it per
    session would make the walk O(sessions x processes)."""
    proc = tmp_path / "proc"
    for pid in (77, 78, 79):
        (proc / str(pid)).mkdir(parents=True)
        tail = ["S"] + ["0"] * 21
        tail[1] = "1"
        (proc / str(pid) / "stat").write_text(f"{pid} (claude) " + " ".join(tail))

    def runner(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return json.dumps([
                {"name": f"review-widgets-pr{n}-r1-aaa", "session": f"s{n}",
                 "status": "busy", "live": True} for n in (77, 78, 79)])
        if argv[:2] == ["tmux", "list-panes"]:
            return argv[3].lstrip("s") + "\n"  # session sN -> pane pid N
        raise AssertionError(argv)

    builds = []
    real_build = sources_mod.sysinfo.build_index
    monkeypatch.setattr(sources_mod.sysinfo, "build_index",
                        lambda root: builds.append(root) or real_build(root))
    src = make_sources(tmp_path, runner=runner, proc_root=str(proc))
    rows = src.sessions()
    assert len(rows) == 3 and all(r["pane_pid"] for r in rows)
    assert len(builds) == 1


def test_sessions_never_walks_proc_without_a_live_pane(tmp_path, monkeypatch):
    def runner(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return json.dumps([{"name": SESSION, "session": "s1",
                                "status": "gone", "live": False}])
        raise AssertionError(argv)

    builds = []
    monkeypatch.setattr(sources_mod.sysinfo, "build_index",
                        lambda root: builds.append(root) or ({}, {}))
    src = make_sources(tmp_path, runner=runner)
    assert src.sessions()[0]["rss_bytes"] is None
    assert builds == []


# -- bounded inbox ----------------------------------------------------------

def test_inbox_is_bounded(tmp_path):
    """`escalations` and `pings` are never pruned, so an unbounded inbox would
    keep every page ever raised on the dashboard forever."""
    src = make_sources(tmp_path, runner=_quiet_runner)
    with State(src.config.state_db) as st:
        for n in range(sources_mod.INBOX_LIMIT + 20):
            st.record_escalation("acme/widgets", 100 + n, f"sha{n}")
            st.record_ping("acme/widgets", 100 + n, f"stalled:s{n}")
    led = src.ledgers()
    assert len(led["escalations"]) == sources_mod.INBOX_LIMIT
    assert len(led["pings"]) == sources_mod.INBOX_LIMIT
    assert len(src.dashboard()["inbox"]) == sources_mod.INBOX_LIMIT


def test_inbox_bound_counts_pages_not_telemetry(tmp_path):
    """The bound must be applied AFTER the kind filter, not before it.

    `activity-deferred:*` is written once per deferral EPISODE; `stalled:*`
    only once an episode outlasts STALLED_DEFER_MULTIPLE stale windows -- so
    the telemetry kind is structurally the more numerous. Reading the newest
    INBOX_LIMIT raw rows and then dropping the telemetry would let ordinary
    deferral churn evict the real operator pages, and render `Inbox clear.`
    while the daemon is actively paging a human.
    """
    src = make_sources(tmp_path, runner=_quiet_runner)
    with State(src.config.state_db) as st:
        # the page comes FIRST, so every telemetry row below is newer than it
        st.record_ping("acme/widgets", 16, "stalled:wedged")
        for n in range(sources_mod.INBOX_LIMIT + 20):
            st.record_ping("acme/widgets", 200 + n, f"activity-deferred:s{n}")

    pings = src.ledgers()["pings"]
    assert all(p["kind"].startswith("stalled:") for p in pings)
    assert "stalled:wedged" in {p["kind"] for p in pings}

    inbox = src.dashboard()["inbox"]
    stalled = [i for i in inbox if i["kind"] == sources_mod.INBOX_STALLED]
    assert {i["detail"] for i in stalled} >= {"wedged"}
    assert all(i["repo_slug"] == "acme/widgets" for i in stalled)


def test_session_pairing_survives_an_old_spawn_row(tmp_path):
    """The pairing is read by session name, never by recency.

    A wedged session defers its round indefinitely (loop._defer_stale_round
    never respawns over a live session), so the row an operator most needs
    paired is the OLDEST in the ledger -- the first casualty of any row-count
    or time-window cap. It must still resolve its PR, round and retry.
    """
    def runner(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return json.dumps([{"name": SESSION, "session": "s1",
                                "status": "busy", "live": True}])
        if argv[:2] == ["tmux", "list-panes"]:
            return ""
        raise AssertionError(argv)

    src = make_sources(tmp_path, runner=runner)
    with State(src.config.state_db) as st:
        for n in range(sources_mod.INBOX_LIMIT + 200):
            st.record_spawn(repo="acme/widgets", number=300 + n, round_=1,
                            head_sha=f"sha{n}", session=f"other-{n}",
                            task_ref=None)

    row = src.sessions()[0]
    assert row["name"] == SESSION
    assert row["pr"] == "acme/widgets#16"
    assert row["round"] == 1
    assert row["retry"] == {"repo_slug": "acme/widgets", "number": 16, "round": 1}
    # and the read is bounded by the names asked for, not by the ledger size
    assert set(src.spawn_pairs([SESSION])) == {SESSION}
    assert src.spawn_pairs([]) == {}
