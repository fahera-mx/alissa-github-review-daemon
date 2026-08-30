"""The read-only data layer: payload shape from a seeded state.db, the two
cached checks, session parsing, drift, inbox links, and the retry-now UPDATE."""

from __future__ import annotations

import json
import sqlite3
import time

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
        # the product-stability hold (issue #105): its own operator page,
        # carrying both shas in the kind
        st.record_ping("acme/widgets", 21, "stability:beefcafe1234:0ldbase99:0")
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
                 cgroup_root="/sys/fs/cgroup", log_path=None, wall=None):
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
        proc_root=proc_root, cgroup_root=cgroup_root, clock=clk,
        # 5000 by default; `wall` overrides it for the tests that need the
        # seeded ledger rows (stamped with the REAL clock by State) to read as
        # old, which no fixed wall in the past can do.
        wall_clock=wall or (lambda: 5000.0),
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
    assert src.ledgers() == {
        "escalations": [], "pings": [], "stability_pings": []
    }
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
    inbox = src._inbox(
        led["escalations"], led["pings"], led["stability_pings"]
    )["live"]
    kinds = [item["kind"] for item in inbox]
    # activity-deferred filtered
    assert sorted(kinds) == ["cap-out", "stability-held", "stalled"]
    by_kind = {item["kind"]: item for item in inbox}
    # every reviewer-edge reference is a PR reference
    assert by_kind["cap-out"]["url"] == "https://github.com/acme/widgets/pull/12"
    assert by_kind["stalled"]["url"] == "https://github.com/acme/widgets/pull/16"
    assert by_kind["cap-out"]["detail"] == "deadbeef"        # short head sha
    assert by_kind["stalled"]["detail"] == SESSION           # the stalled session
    # BOTH shas, in the order the hold is stated in: the head the product
    # stopped moving at, then the head it is still sitting at. One sha would
    # say "held" without saying since when.
    assert by_kind["stability-held"]["detail"] == "0ldbase9…beefcafe"
    assert by_kind["stability-held"]["url"] == "https://github.com/acme/widgets/pull/21"


def test_inbox_ages_and_sorts_newest_first(tmp_path):
    src = make_sources(tmp_path)
    inbox = src._inbox(
        [{"repo": "acme/widgets", "number": 12, "head_sha": "aa",
          "escalated_at": 1000}],
        [{"repo": "acme/widgets", "number": 16, "kind": "stalled:s",
          "pinged_at": 4900}],
    )["live"]
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
    assert echo["operators"] == list(src.config.operators)


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
                      "inbox", "sessions", "top_procs", "log", "generated_at"}
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

    assert len(d["inbox"]) == 3, "cap-out, stalled, stability-held"
    # every seeded page was raised moments ago, so none of them has settled
    assert d["inbox_settled"] == [] and d["inbox_settled_count"] == 0
    # three rows is nowhere near a read bound, so the window was complete
    assert d["inbox_truncated"] is False
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
    assert d["inbox_settled"] == [] and d["inbox_settled_count"] == 0
    assert d["inbox_truncated"] is False


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
    assert src.ledgers() == {
        "escalations": [], "pings": [], "stability_pings": []
    }
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
    keep every page ever raised on the dashboard forever. Two bounds, not one:
    the READ is capped at INBOX_READ_LIMIT (wide enough for the live/settled
    split to have something to choose from) and what reaches the page is capped
    again at INBOX_LIMIT."""
    src = make_sources(tmp_path, runner=_quiet_runner)
    with State(src.config.state_db) as st:
        for n in range(sources_mod.INBOX_READ_LIMIT + 20):
            st.record_escalation("acme/widgets", 100 + n, f"sha{n}")
            st.record_ping("acme/widgets", 100 + n, f"stalled:s{n}")
    led = src.ledgers()
    assert len(led["escalations"]) == sources_mod.INBOX_READ_LIMIT
    assert len(led["pings"]) == sources_mod.INBOX_READ_LIMIT
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


# -- container memory tile + host-wide top processes ------------------------

def write_proc_and_cgroup(tmp_path):
    """A crafted /proc (one reviewer pane tree plus two processes OUTSIDE it)
    and a cgroup v2 tree carrying the audited Railway plateau."""
    proc = tmp_path / "proc"
    for pid, ppid, rss, comm in ((77, 1, 10, "claude"), (78, 77, 20, "node"),
                                 (900, 1, 5000, "postgres"), (901, 1, 30, "bash")):
        (proc / str(pid)).mkdir(parents=True)
        tail = ["S"] + ["0"] * 21
        tail[1] = str(ppid)
        tail[21] = str(rss)
        (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) " + " ".join(tail))
    cg = tmp_path / "cgroup"
    cg.mkdir()
    (cg / "memory.current").write_text("6420496384\n")
    (cg / "memory.stat").write_text(
        "anon 75091968\nfile 2362232832\nshmem 0\ninactive_file 2000000000\n"
        "slab_reclaimable 3983993651\nslab_unreclaimable 2065528\nslab 3986059179\n"
    )
    return proc, cg


def _one_session_runner(argv, **kw):
    if argv[:3] == ["alissa", "tmux", "ls"]:
        return json.dumps([{"name": SESSION, "session": "s1",
                            "status": "busy", "live": True}])
    if argv[:2] == ["tmux", "list-panes"]:
        return "77\n"
    if argv[:3] == ["gh", "api", "rate_limit"]:
        return "{}"
    raise AssertionError(argv)


def test_dashboard_splits_the_container_memory_charge(tmp_path):
    """The tile the console was missing: a multi-GB plateau with no reviewer
    sessions reads as a little real memory plus a lot of reclaimable cache,
    without shelling into the container."""
    proc, cg = write_proc_and_cgroup(tmp_path)
    src = make_sources(tmp_path, runner=_one_session_runner,
                       proc_root=str(proc), cgroup_root=str(cg))
    mem = src.dashboard()["tiles"]["memory"]
    assert mem["charged"] == 6420496384
    assert mem["resident"] == 75091968
    assert mem["reclaimable"] == 2362232832 + 3983993651
    # and the per-session sums cannot answer it: the whole session table is
    # two orders of magnitude smaller than the charge it sits inside
    assert src.dashboard()["sessions"][0]["rss_bytes"] < mem["resident"]


def test_memory_tile_is_unavailable_without_cgroup_v2(tmp_path):
    """A dev laptop / macOS has no cgroup v2: the tile renders "unavailable"
    and the rest of the dashboard is untouched -- never an exception."""
    proc, _ = write_proc_and_cgroup(tmp_path)
    src = make_sources(tmp_path, runner=_one_session_runner, proc_root=str(proc),
                       cgroup_root=str(tmp_path / "no-such-cgroup"))
    d = src.dashboard()
    assert all(v is None for v in d["tiles"]["memory"].values())
    assert d["tiles"]["volume"] is not None  # the neighbouring tile still works
    assert d["sessions"][0]["pane_pid"] == 77


def test_top_procs_are_host_wide_not_session_trees(tmp_path):
    """The biggest process on the host is deliberately NOT in any reviewer's
    pane tree -- that is the case the sessions panel cannot show."""
    proc, cg = write_proc_and_cgroup(tmp_path)
    src = make_sources(tmp_path, runner=_one_session_runner,
                       proc_root=str(proc), cgroup_root=str(cg))
    rows = src.dashboard()["top_procs"]
    assert len(rows) == 4  # bounded by TOP_PROCS, here by the fixture's size
    assert rows[0] == {"pid": 900, "comm": "postgres",
                       "rss_bytes": 5000 * sources_mod.sysinfo._PAGE_SIZE}
    assert [r["pid"] for r in rows] == [900, 901, 78, 77]


def test_top_procs_are_bounded(tmp_path):
    proc = tmp_path / "proc"
    for pid in range(100, 120):
        (proc / str(pid)).mkdir(parents=True)
        tail = ["S"] + ["0"] * 21
        tail[1] = "1"
        tail[21] = str(pid)
        (proc / str(pid) / "stat").write_text(f"{pid} (claude) " + " ".join(tail))

    def runner(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return "[]"
        if argv[:3] == ["gh", "api", "rate_limit"]:
            return "{}"
        raise AssertionError(argv)

    rows = make_sources(tmp_path, runner=runner,
                        proc_root=str(proc)).dashboard()["top_procs"]
    assert len(rows) == sources_mod.TOP_PROCS
    assert rows[0]["pid"] == 119  # rss == pid in this fixture: largest first


def test_dashboard_scans_proc_once_for_both_panels(tmp_path, monkeypatch):
    """The session table and the top-process list rank the SAME snapshot: one
    walk per build, and the two panels can never disagree about a process that
    exited between them."""
    proc, cg = write_proc_and_cgroup(tmp_path)
    builds = []
    real_build = sources_mod.sysinfo.build_index
    monkeypatch.setattr(sources_mod.sysinfo, "build_index",
                        lambda root: builds.append(root) or real_build(root))
    src = make_sources(tmp_path, runner=_one_session_runner,
                       proc_root=str(proc), cgroup_root=str(cg))
    d = src.dashboard()
    assert len(builds) == 1
    assert d["sessions"][0]["rss_bytes"] is not None and d["top_procs"]


# -- the product-stability hold in the inbox (issue #105) -------------------


def test_a_stability_ping_this_version_cannot_parse_is_dropped(tmp_path):
    """The inbox is read to decide whether to act on a PR, so a row this
    version cannot read is dropped rather than rendered half-parsed."""
    src = make_sources(tmp_path)
    inbox = src._inbox(
        [],
        [],
        [{"repo": "acme/widgets", "number": 21, "kind": "stability:only-a-head",
          "pinged_at": 4900}],
    )
    assert inbox == {"live": [], "settled": [], "truncated": False}


def test_stability_pings_are_read_under_their_own_bound(tmp_path):
    """Their own filtered read, not a share of the stalled one: `read_pings`
    narrows in SQL, so a wave of one kind cannot evict the other."""
    with State(tmp_path / ".revloop" / "state.db") as st:
        for i in range(sources_mod.INBOX_READ_LIMIT + 5):
            st.record_ping("acme/widgets", i, f"stability:head{i}:base{i}:0")
            st.record_ping("acme/widgets", i, f"stalled:session-{i}")
    src = make_sources(tmp_path)
    led = src.ledgers()
    assert len(led["stability_pings"]) == sources_mod.INBOX_READ_LIMIT
    assert all(
        row["kind"].startswith("stability:") for row in led["stability_pings"]
    )
    assert all(row["kind"].startswith("stalled:") for row in led["pings"])


# -- settled inbox pages (issue #108) ---------------------------------------
#
# `escalations` and `pings` are dedupe key stores the daemon must keep, so they
# are never pruned and the console cannot clear the inbox by deleting rows. It
# has to tell backlog from exhaust at READ time, and from the local snapshot
# alone -- a page load still costs the GitHub API nothing.


def _escalation(number, at=0):
    """A cap-out page. `at` is the wall stamp; the fixtures' clock reads 5000,
    so the default is 5000s old -- far past any grace window here."""
    return {"repo": "acme/widgets", "number": number, "head_sha": "aa",
            "escalated_at": at}


def _stalled(number, at=0):
    return {"repo": "acme/widgets", "number": number, "kind": "stalled:s",
            "pinged_at": at}


def _held(number, at=0):
    return {"repo": "acme/widgets", "number": number,
            "kind": "stability:beefcafe1234:0ldbase99:0", "pinged_at": at}


def _ledger_rows(db_path):
    con = sqlite3.connect(db_path)
    try:
        return {
            "escalations": con.execute(
                "SELECT * FROM escalations ORDER BY number").fetchall(),
            "pings": con.execute(
                "SELECT * FROM pings ORDER BY number, kind").fetchall(),
        }
    finally:
        con.close()


def test_a_page_whose_pr_is_in_the_latest_snapshot_is_live(tmp_path):
    """The trigger is still standing: the PR is open with a review pending, so
    the pass still lists it and the page is still the operator's to answer."""
    src = make_sources(tmp_path)
    inbox = src._inbox([_escalation(12)], [], [],
                       live_prs={("acme/widgets", 12)})
    assert [i["number"] for i in inbox["live"]] == [12]
    assert inbox["settled"] == []


def test_a_page_whose_pr_left_the_candidate_set_is_settled(tmp_path):
    """Absent from the newest pass = the trigger cleared (merged, closed, or
    the review request withdrawn). Either way nothing this console offers can
    act on it, so it is exhaust."""
    src = make_sources(tmp_path)
    inbox = src._inbox([_escalation(12)], [], [],
                       live_prs={("acme/widgets", 99)})
    assert inbox["live"] == []
    assert [i["number"] for i in inbox["settled"]] == [12]


def test_a_stalled_row_follows_the_same_liveness_rule(tmp_path):
    """One rule for every page kind -- the question the split answers ("can I
    still act on this?") does not depend on why the daemon paged."""
    src = make_sources(tmp_path)
    inbox = src._inbox([], [_stalled(16), _stalled(77)], [],
                       live_prs={("acme/widgets", 16)})
    assert [i["number"] for i in inbox["live"]] == [16]
    assert [i["number"] for i in inbox["settled"]] == [77]


def test_a_stability_hold_follows_the_same_liveness_rule(tmp_path):
    """The third page kind (issue #105) postdates the issue that asked for this
    split, and it is the kind most likely to outlive its PR: a hold is lifted
    by an ack, not by the loop, so a merged held PR pages forever."""
    src = make_sources(tmp_path)
    inbox = src._inbox([], [], [_held(21), _held(88)],
                       live_prs={("acme/widgets", 21)})
    assert [i["number"] for i in inbox["live"]] == [21]
    assert [i["number"] for i in inbox["settled"]] == [88]


def test_a_page_raised_between_snapshots_stays_live(tmp_path):
    """The oracle is the LATEST snapshot only, so a page raised after the
    newest pass has no snapshot to appear in yet and would flicker into
    `settled` for one refresh. The grace window is a strict `<`: a row exactly
    at the bound has had a whole pass to show up."""
    src = make_sources(tmp_path)
    grace = sources_mod.INBOX_LIVE_GRACE_INTERVALS * src.config.poll_interval
    assert grace > 0
    inbox = src._inbox(
        [_escalation(12, at=5000 - grace + 1),   # age grace-1 -> live
         _escalation(13, at=5000 - grace)],      # age grace   -> settled
        [], [], live_prs=set(),
    )
    assert [i["number"] for i in inbox["live"]] == [12]
    assert [i["number"] for i in inbox["settled"]] == [13]


def test_no_snapshot_means_every_row_is_live(tmp_path):
    """Never hide a page on missing evidence -- the same rule the daemon's
    liveness oracle uses for a failed listing. A fresh boot has no snapshot to
    judge against, and an inbox that hides pages then is worse than one that
    shows a few stale ones."""
    src = make_sources(tmp_path)
    assert src._live_prs(None, []) is None
    inbox = src._inbox([_escalation(12)], [_stalled(16)], [], live_prs=None)
    assert [i["number"] for i in inbox["live"]] == [12, 16]
    assert inbox["settled"] == []


def test_an_empty_poll_pass_is_evidence_not_a_missing_snapshot(tmp_path):
    """A pass that found no candidates is a real answer: every page is settled.
    Only the ABSENCE of a snapshot is the no-evidence case."""
    src = make_sources(tmp_path)
    assert src._live_prs({"ts": 1, "stages": []}, []) == set()


def test_the_live_set_is_the_rendered_board_rows(tmp_path):
    """Built from the pipeline items, not the raw stages, so the inbox and the
    board can never disagree about what the pass had in hand."""
    src = make_sources(tmp_path)
    latest = src.snapshots()[0]
    assert src._live_prs(latest, src._pipeline(latest)) == {
        ("acme/widgets", 16), ("acme/widgets", 12),
    }


def test_settled_list_is_newest_first_and_bounded(tmp_path):
    """The settled half is fed by the same never-pruned tables, so it needs the
    same bound the live half has -- and it keeps the NEWEST rows, because a
    page filed away yesterday is the one an operator might still want."""
    src = make_sources(tmp_path)
    rows = [_escalation(200 + n, at=n)
            for n in range(sources_mod.INBOX_LIMIT + 20)]
    inbox = src._inbox(rows, [], [], live_prs=set())
    assert inbox["live"] == []
    ages = [i["age_seconds"] for i in inbox["settled"]]
    assert len(ages) == sources_mod.INBOX_LIMIT
    assert ages == sorted(ages)                                  # newest first
    assert ages[0] == 5000 - (sources_mod.INBOX_LIMIT + 19)      # the newest


def test_telemetry_ping_kinds_reach_neither_list(tmp_path):
    """`activity-deferred:*` is the dedupe key for a PR comment line, not an
    operator page. Filing rows away is still rendering them, so the split must
    not become a back door into the panel for the kind the inbox excludes."""
    src = make_sources(tmp_path)
    row = {"repo": "acme/widgets", "number": 4242,
           "kind": "activity-deferred:s", "pinged_at": 0}
    empty = {"live": [], "settled": [], "truncated": False}
    assert src._inbox([], [row], [], live_prs=set()) == empty
    assert src._inbox([], [row], [], live_prs=None) == empty


def test_dashboard_partitions_the_inbox_and_counts_the_settled(tmp_path):
    """End to end. The seeded snapshot holds #16 and #12, so their pages stay
    in `inbox`; #21 (a hold) and #99 (a cap-out) were never in a pass, so they
    move to `inbox_settled`. The count rides along so the panel can render a
    collapsed counter without re-deriving it."""
    src = make_sources(tmp_path, runner=_quiet_runner,
                       wall=lambda: time.time() + 10_000)
    with State(src.config.state_db) as st:
        st.record_escalation("acme/widgets", 99, "sha99")
    d = src.dashboard()
    assert {(i["kind"], i["number"]) for i in d["inbox"]} == {
        (sources_mod.INBOX_CAP_OUT, 12), (sources_mod.INBOX_STALLED, 16),
    }
    assert {(i["kind"], i["number"]) for i in d["inbox_settled"]} == {
        (sources_mod.INBOX_CAP_OUT, 99), (sources_mod.INBOX_STABILITY, 21),
    }
    assert d["inbox_settled_count"] == len(d["inbox_settled"]) == 2
    # a settled row keeps the live row's shape -- the panel renders one markup
    assert set(d["inbox_settled"][0]) == set(d["inbox"][0])


def test_the_split_costs_no_github_call_and_writes_no_ledger_row(tmp_path):
    """The console's contract, unchanged by the split: a page load spends no
    GitHub budget beyond the cached rate meter, and the two dedupe key stores
    are read-only to it -- pruning them would break the daemon's dedupe, which
    is exactly why this is a read-time filter and not a delete."""
    seen = []

    def runner(argv, **kw):
        seen.append(list(argv))
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return "[]"
        if argv[:3] == ["gh", "api", "rate_limit"]:
            return "{}"
        raise AssertionError(argv)

    src = make_sources(tmp_path, runner=runner,
                       wall=lambda: time.time() + 10_000)
    with State(src.config.state_db) as st:
        st.record_escalation("acme/widgets", 99, "sha99")
    before = _ledger_rows(src.config.state_db)
    d = src.dashboard()
    assert d["inbox_settled_count"] == 2          # the split really ran
    assert [a for a in seen if a and a[0] == "gh"] == [["gh", "api",
                                                        "rate_limit"]]
    assert _ledger_rows(src.config.state_db) == before


def test_a_row_too_malformed_to_key_degrades_instead_of_raising(tmp_path):
    """Every read path here degrades; none blanks the dashboard. A snapshot
    item with no number simply does not join the live set (and a page with no
    number cannot match one), which leaves the page live -- the safe side."""
    assert sources_mod._pr_key("acme/widgets", 16) == ("acme/widgets", 16)
    assert sources_mod._pr_key("acme/widgets", "16") == ("acme/widgets", 16)
    assert sources_mod._pr_key("acme/widgets", None) is None
    assert sources_mod._pr_key("acme/widgets", "not-a-number") is None
    src = make_sources(tmp_path)
    latest = {"ts": 1, "stages": [{"slug": "acme/widgets#16", "number": None}]}
    assert src._live_prs(latest, src._pipeline(latest)) == set()


# -- the split must not empty the live half (PR #109 round 1, [major]) -------
#
# The split picks from whatever `ledgers()` read, so a read bounded at exactly
# what the panel renders would make the live half "the live pages among the
# newest INBOX_LIMIT rows" -- and a full window of settled rows would empty it
# while a page is outstanding, with the console then positively asserting
# `Inbox clear.`. Two guards: read wider than you render, and refuse the claim
# when the read was truncated anyway.


def test_a_live_page_survives_a_full_window_of_settled_ones(tmp_path):
    """The reviewer's construction: every row newer than the live one has
    settled. With the read bounded at what is rendered, the live page is row
    INBOX_LIMIT+1 and never reaches the payload."""
    src = make_sources(tmp_path, runner=_quiet_runner,
                       wall=lambda: time.time() + 10_000)
    with State(src.config.state_db) as st:
        st.record_escalation("acme/widgets", 16, "livesha")     # PR in the snap
        for n in range(sources_mod.INBOX_LIMIT + 20):           # all settled
            st.record_escalation("acme/widgets", 500 + n, f"sha{n}")
    d = src.dashboard()
    # the CAP-OUT for #16 specifically -- #16 also has a stalled ping, so a
    # bare number check would pass off the wrong row
    assert (sources_mod.INBOX_CAP_OUT, 16) in {
        (i["kind"], i["number"]) for i in d["inbox"]
    }
    assert d["inbox_settled_count"] == sources_mod.INBOX_LIMIT


def test_a_truncated_read_is_reported_so_the_panel_cannot_claim_clear(tmp_path):
    """`Inbox clear.` is a positive claim that nothing is owed; it may only be
    made off a complete read. A wider window is still a window, so the payload
    says when it hit the bound."""
    src = make_sources(tmp_path, runner=_quiet_runner,
                       wall=lambda: time.time() + 10_000)
    with State(src.config.state_db) as st:
        for n in range(sources_mod.INBOX_READ_LIMIT + 5):
            st.record_escalation("acme/widgets", 500 + n, f"sha{n}")
    d = src.dashboard()
    assert d["inbox_truncated"] is True
    # and the pages that displaced the window are all settled, so the live half
    # is empty -- exactly the state the flag exists to qualify
    assert [i for i in d["inbox"] if i["kind"] == sources_mod.INBOX_CAP_OUT] == []


def test_truncation_is_reported_for_any_of_the_three_reads(tmp_path):
    """Each store is read under its own bound, so ANY of them coming back full
    means there are rows the split never saw."""
    src = make_sources(tmp_path)
    n = sources_mod.INBOX_READ_LIMIT
    esc = [_escalation(i, at=i) for i in range(n)]
    ping = [_stalled(i, at=i) for i in range(n)]
    held = [_held(i, at=i) for i in range(n)]
    assert src._inbox(esc, [], [], live_prs=set())["truncated"] is True
    assert src._inbox([], ping, [], live_prs=set())["truncated"] is True
    assert src._inbox([], [], held, live_prs=set())["truncated"] is True
    # one row short of the bound is a complete read
    assert src._inbox(esc[:-1], [], [], live_prs=set())["truncated"] is False


def test_the_read_window_is_wider_than_the_rendered_one(tmp_path):
    """Stated as a relation, not a number: the read must leave the split room
    to fill the live half past a run of settled rows."""
    assert sources_mod.INBOX_READ_LIMIT > sources_mod.INBOX_LIMIT
