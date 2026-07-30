"""Crash-resilience tests: the poll-loop firewall and best-effort telemetry.

Issue #62, from three Railway outages on 2026-07-29. Two separate defects, both
of which turned one bad poll into a dead daemon:

* an exception from `poll_once` (a subprocess ENOENT on the `alissa` CLI, whose
  image-layer file vanished mid-run) escaped `run_forever` and reached
  __main__'s startup-shaped handler -> "config error" -> exit 2;
* a `sqlite3.OperationalError: attempt to write a readonly database` from the
  poll SNAPSHOT -- pure observability -- did the same.

What is under test is that the daemon now survives both: it logs, backs off,
keeps polling, escalates a sustained same-class failure to ERROR, and resumes
normally when the substrate heals. Startup config errors keep their fast exit,
which is the one place the old behaviour was right.
"""

from __future__ import annotations

import logging
import sqlite3
import time as real_time

import pytest

from alissa.tools.github.revloop import loop as loop_module
from alissa.tools.github.revloop import state as state_module
from alissa.tools.github.revloop.__main__ import main
from alissa.tools.github.revloop.config import Config
from alissa.tools.github.revloop.loop import (
    LedgerUnwritable,
    POLL_BACKOFF_CAP_SECONDS,
    POLL_ESCALATE_SECONDS,
    POLL_FAILURE_LOG_EVERY,
    POLL_FAILURE_LOG_HEAD,
    PollFailures,
    ReviewWatcher,
    Streak,
)
from alissa.tools.github.revloop.proc import CommandError
from alissa.tools.github.revloop.state import State


def _cmd_error():
    """A CommandError shaped like the real thing (argv, rc, stderr)."""
    return CommandError(["alissa", "task", "get"], 127, "alissa: not found")


@pytest.fixture
def config(tmp_path):
    return Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=tmp_path / "state.db",
        poll_interval=60,
    )


class _Clock:
    """A stand-in for `loop.py`'s view of the `time` module: a monotonic clock
    the loop drives itself, where every `sleep(n)` advances it by n. So a test
    can watch a 30-minute escalation window pass in the same number of
    iterations the daemon would really take, without sleeping.

    It replaces the module ATTRIBUTE rather than patching `time.sleep` on the
    stdlib module object, which `loop_module.time` *is* — patching that would
    stop the whole process sleeping for the duration of the test, an isolation
    claim the fixture could not honour. `time()` still delegates to the real
    module: `loop.py` uses wall-clock time on paths these tests do not stub.
    """

    def __init__(self):
        self.now = 1000.0
        self.slept: list[int] = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now

    def time(self):
        return real_time.time()


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(loop_module, "time", c)
    return c


def _watcher(config, polls):
    """A watcher whose `poll_once` walks `polls`: each entry is either an
    exception to raise or None for a clean pass. KeyboardInterrupt at the end
    is how run_forever is asked to return."""
    w = ReviewWatcher(config, github=object(), alissa=object(), state=None)
    calls = {"n": 0}

    def poll_once():
        i = calls["n"]
        calls["n"] += 1
        outcome = polls[i] if i < len(polls) else KeyboardInterrupt()
        if isinstance(outcome, BaseException):
            raise outcome
        return []

    w.poll_once = poll_once  # type: ignore[method-assign]
    w._calls = calls  # type: ignore[attr-defined]
    return w


# -- defect 1: the per-iteration exception firewall -------------------------


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError(2, "No such file or directory", "alissa"),
        ValueError("expected OWNER/REPO#N"),
        CommandError(["gh", "api", "user"], 1, "gh: not found"),
        OSError("stale file handle"),
        sqlite3.OperationalError("attempt to write a readonly database"),
        RuntimeError("something nobody anticipated"),
    ],
)
def test_any_poll_exception_is_survived_and_the_loop_keeps_going(config, clock, exc):
    """The 19:52Z incident: a call site deep in the pass raised, the exception
    escaped every handler, and the process returned 2. Every class the poll can
    raise must now be one bad poll, not the last one."""
    w = _watcher(config, [exc, exc, None])

    w.run_forever()

    # 3 real polls (two failed, one recovered) and then the stop signal.
    assert w._calls["n"] == 4


def test_consecutive_failures_back_off_and_resume_when_the_fault_clears(config, clock, caplog):
    """AC 1: several consecutive FileNotFoundErrors (the missing CLI) -> logs,
    backoff, still running; the fault clears -> normal polling resumes."""
    caplog.set_level(logging.INFO)
    missing = FileNotFoundError(2, "No such file or directory", "alissa")
    w = _watcher(config, [missing] * 4 + [None, None])

    w.run_forever()

    # Doubling from the poll interval while failing, straight back to the
    # interval on the first clean pass.
    assert clock.slept[:4] == [120, 240, 480, 900]
    assert clock.slept[4:6] == [60, 60]
    assert POLL_BACKOFF_CAP_SECONDS == 900

    # Four failures, THREE lines: the log head is the streak limit, and the
    # fourth is suppressed rather than repeating a line that says nothing new.
    failed = [r for r in caplog.records if "poll failed" in r.message]
    assert len(failed) == POLL_FAILURE_LOG_HEAD == 3
    assert all(r.levelno == logging.WARNING for r in failed)
    assert "FileNotFoundError" in caplog.text
    # The recovery is stated out loud -- otherwise the last line an operator
    # sees about a healed daemon is the failure.
    assert "poll recovered after 4 consecutive failure(s)" in caplog.text


def test_a_sustained_same_class_failure_escalates_to_error(config, clock, caplog):
    """AC 1: degraded-but-alive with a loud signal. Once the SAME class has
    fired on every poll for the escalation window, the level goes to ERROR --
    and the daemon is still polling, which is the whole point."""
    caplog.set_level(logging.INFO)
    missing = FileNotFoundError(2, "No such file or directory", "alissa")
    # At the capped 900s backoff, 30 minutes is two sleeps -- but take plenty
    # more so the streak is unambiguously sustained.
    w = _watcher(config, [missing] * 12)

    w.run_forever()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "a sustained same-class failure must escalate"
    assert "is no longer transient" in errors[0].getMessage()
    assert "still retrying" in errors[0].getMessage()
    # It escalated only AFTER the window, not on the first failures.
    assert caplog.records[0].levelno == logging.WARNING
    # 12 failed polls plus the stop signal: it never stopped polling.
    assert w._calls["n"] == 13


def test_a_different_exception_class_restarts_the_streak(config, clock, caplog):
    """A new class is a new fault. Reporting 'FileNotFoundError for 40 minutes'
    when the last 30 were sqlite errors is a lie an operator would act on."""
    caplog.set_level(logging.INFO)
    a = FileNotFoundError(2, "No such file or directory", "alissa")
    b = sqlite3.OperationalError("attempt to write a readonly database")
    w = _watcher(config, [a] * 8 + [b] * 3)

    w.run_forever()

    per_streak = [
        r.getMessage() for r in caplog.records if "of this streak" in r.getMessage()
    ]
    # The sqlite streak counts from 1 again, at its own log-head allowance.
    assert any("OperationalError" in m and "failure 1 of this streak" in m for m in per_streak)


def test_rate_limiting_is_not_counted_as_a_substrate_failure(config, clock, caplog):
    """RateLimited keeps its own branch: GitHub asking the daemon to slow down
    is not a fault, and folding it into the firewall's streak would page an
    operator for a busy hour."""
    caplog.set_level(logging.INFO)
    limited = loop_module.RateLimited("secondary rate limit")
    w = _watcher(config, [limited, limited, None])

    w.run_forever()

    assert "rate limited" in caplog.text
    assert "poll failed" not in caplog.text
    assert "poll recovered" not in caplog.text


def test_a_rate_limit_does_not_cancel_a_pending_escalation(config, clock, caplog):
    """A rate limit must not END a substrate streak either.

    `review_requests` is the first GitHub call in a pass, so a rate limit can
    pre-empt the failing call site entirely. Letting it resolve the streak
    re-armed the escalation clock, and a busy hour could keep a genuine fault
    from ever reaching the page-worthy ERROR the DoD requires.
    """
    caplog.set_level(logging.INFO)
    missing = FileNotFoundError(2, "No such file or directory", "alissa")
    limited = loop_module.RateLimited("secondary rate limit")
    # A real fault, interleaved with rate limits throughout.
    w = _watcher(config, [missing, limited, missing, limited, missing] * 3)

    w.run_forever()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "the interleaved rate limits must not cancel the page"
    assert "FileNotFoundError" in errors[0].getMessage()
    # ...and the rate-limited polls did not inflate the failure count either.
    counted = [
        r.getMessage() for r in caplog.records if "of this streak" in r.getMessage()
    ]
    assert all("OSError" not in m and "RateLimited" not in m for m in counted)


def test_the_firewall_log_is_streak_limited(config, clock):
    """A substrate outage costs a handful of lines an hour, not one per poll."""
    streak = PollFailures()
    logged = [
        streak.record(_cmd_error(), now=float(i))[0]
        for i in range(1, 41)
    ]

    assert logged[:POLL_FAILURE_LOG_HEAD] == [True] * POLL_FAILURE_LOG_HEAD
    assert logged[POLL_FAILURE_LOG_HEAD] is False
    assert sum(logged) == POLL_FAILURE_LOG_HEAD + 40 // POLL_FAILURE_LOG_EVERY - (
        POLL_FAILURE_LOG_HEAD // POLL_FAILURE_LOG_EVERY
    )


def test_escalation_is_always_logged_even_mid_suppression(config):
    """The crossing is a state change; suppressing it would hide the one
    transition the log exists to show."""
    streak = PollFailures()
    for i in range(1, 6):  # past the log head, inside the window
        streak.record(_cmd_error(), now=float(i))
    should_log, crossing = streak.record(_cmd_error(), now=POLL_ESCALATE_SECONDS + 1)

    assert (should_log, crossing) == (True, True)
    # ...and only once per episode.
    assert streak.record(_cmd_error(), now=POLL_ESCALATE_SECONDS + 2)[1] is False


def test_keyboard_interrupt_and_system_exit_still_pass_through(config, clock):
    """The firewall catches Exception, never BaseException: Ctrl-C must stop
    the daemon, and a SystemExit must remain an exit."""
    w = _watcher(config, [SystemExit(3)])

    with pytest.raises(SystemExit):
        w.run_forever()


def test_startup_config_errors_still_exit_two_fast(tmp_path, monkeypatch, capsys):
    """AC 1's other half: the fast exit was RIGHT for startup. A bad config
    cannot be fixed by polling again, so it must not be firewalled."""
    monkeypatch.setattr(
        loop_module.ReviewWatcher, "preflight", lambda self: (_ for _ in ()).throw(
            ValueError("reviewer_login is not a login")
        )
    )

    rc = main(["--workspace-root", str(tmp_path)])

    assert rc == 2
    assert "config error" in capsys.readouterr().err


# -- defect 2: telemetry writes are best-effort -----------------------------


def _read_only(path):
    """Make an sqlite database unwritable the way a remount does: the FILE and
    the directory it needs for its journal."""
    path.chmod(0o444)
    path.parent.chmod(0o555)


def _writable(path):
    path.parent.chmod(0o755)
    path.chmod(0o644)


def _snapshot(ledger, **over):
    return ledger.record_snapshot(
        duration_ms=over.pop("duration_ms", 10), candidates=0, stages=[], **over
    )


def test_a_readonly_ledger_warns_and_never_raises(tmp_path, caplog):
    """AC 2 / the 14:34Z incident: `record_snapshot` raised
    OperationalError('attempt to write a readonly database') straight through
    poll_once and killed the daemon."""
    caplog.set_level(logging.INFO)
    db = tmp_path / "state.db"
    with State(db) as ledger:
        _read_only(db)
        try:
            landed = [_snapshot(ledger) for _ in range(4)]
        finally:
            _writable(db)

    assert landed == [False, False, False, False]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a swallowed telemetry failure must still be reported"
    assert "poll snapshot failed" in warnings[0].getMessage()
    assert "readonly database" in caplog.text
    assert "best-effort" in caplog.text


def test_the_poll_loop_completes_further_passes_over_a_readonly_ledger(tmp_path, caplog):
    """AC 2: >=3 further poll cycles with the ledger read-only, and persistence
    resumes once it is writable again -- no restart in between.

    What the pass DOES over a read-only ledger is decide nothing at all: the
    gate at the top of `poll_once` refuses it, because the daemon must not take
    an action it cannot record (PR #63 round-1 blocker). The point that matters
    for this AC is unchanged and is what is asserted -- the loop survives, it
    says so, streak-limited, and it heals by itself.
    """
    caplog.set_level(logging.INFO)
    db = tmp_path / "state.db"
    config = Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=db,
    )
    with State(db) as ledger:
        w = ReviewWatcher(config, github=_NoRequests(), alissa=_NoSessions(), state=ledger)
        _read_only(db)
        try:
            for _ in range(3):
                # The refusal is a SIGNAL, not an empty result: `run_forever`
                # has to tell it apart from "polled, nothing to do" so it does
                # not read a pass the daemon never took as a recovery.
                with pytest.raises(LedgerUnwritable):
                    w.poll_once()
        finally:
            _writable(db)

        assert "cannot be written — skipping this pass entirely" in caplog.text

        # Healed: the very next pass decides and persists again, through the
        # same object and with no restart. Note WHICH connection heals it --
        # the one opened before the fault. A reconnect correctly DECLINES to
        # adopt a handle opened while the volume was read-only, because sqlite
        # fixes read-only-ness at open time and that handle would never have
        # recovered (the reconnect path proper is exercised below).
        assert w.poll_once() == []
        assert len(ledger.read_snapshots()) == 1

    assert "is writable again after 3 skipped pass(es)" in caplog.text


def test_a_snapshot_failure_alone_does_not_end_the_pass(tmp_path, monkeypatch, caplog):
    """The gate and the best-effort writer are BOTH live, and neither makes the
    other redundant. The gate probes at the start of a pass; a telemetry write
    happens at the end of it, and can fail on its own (a lock timeout, a full
    disk, a handle that died mid-pass) over a ledger that probed writable.
    That must still not end the pass."""
    caplog.set_level(logging.INFO)
    db = tmp_path / "state.db"
    config = Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=db,
    )
    with State(db) as ledger:
        w = ReviewWatcher(config, github=_NoRequests(), alissa=_NoSessions(), state=ledger)
        monkeypatch.setattr(State, "_reconnect", lambda self: False)
        monkeypatch.setattr(
            ledger, "_insert_snapshot",
            lambda **kw: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
        )

        for _ in range(3):
            assert w.poll_once() == []

    assert "poll snapshot failed" in caplog.text
    assert "best-effort" in caplog.text
    # The gate never fired: the ledger itself was writable throughout.
    assert "skipping this pass entirely" not in caplog.text


def test_a_failed_correctness_write_does_not_re_enqueue_on_the_next_pass(tmp_path, clock, caplog):
    """THE round-1 blocker, pinned at the loop level (PR #63).

    `State`-level strictness only aborts the pass that fails. The firewall then
    hands the loop straight back to the same code path, and the side effect the
    write was meant to dedupe has already been taken -- so a read-only volume
    turned "enqueue a reviewer, fail to record it" into a fresh reviewer session
    every poll, forever. Driven through the REAL `poll_once` so the gate is what
    is under test, not a paraphrase of it.
    """
    caplog.set_level(logging.INFO)
    db = tmp_path / "state.db"
    config = Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=db,
        poll_interval=60,
    )
    with State(db) as ledger:
        w = _SpawningWatcher(
            config, github=_OneRequest(), alissa=_NoSessions(), state=ledger
        )
        _read_only(db)
        try:
            # Five passes' worth of loop, then the stop signal.
            w.stop_after = 5
            w.run_forever()

            assert w.enqueued == [], "no session may be queued that cannot be recorded"
            assert w.polls == 5, "and the daemon must still be polling"
            assert "cannot be written — skipping this pass entirely" in caplog.text
            assert any(
                r.levelno >= logging.WARNING
                and "skipping this pass entirely" in r.getMessage()
                for r in caplog.records
            ), "a daemon deciding nothing must say so at WARNING or above"
        finally:
            _writable(db)

        # The gate is a gate, not a permanent stop: one pass once writable.
        w.stop_after = w.polls + 1
        w.run_forever()

        assert len(w.enqueued) == 1
        assert ledger.get_spawn("acme/widgets", 7, 1) is not None


def test_the_first_failure_of_a_streak_reconnects_once(tmp_path, caplog):
    """AC 2's reconnect path, isolated: a STALE HANDLE (the shape a volume
    remount leaves) over a database that is perfectly writable. One reconnect
    and the row lands -- and it is attempted once per streak, not per write."""
    caplog.set_level(logging.INFO)
    with State(tmp_path / "state.db") as ledger:
        broken = _StaleHandle()
        ledger._db = broken

        assert _snapshot(ledger) is True
        assert broken.closed, "the dead handle must be closed, not leaked"
        assert len(ledger.read_snapshots()) == 1
        assert "succeeded after reconnecting" in caplog.text


def test_the_reconnect_is_not_retried_on_every_write_of_a_streak(tmp_path, monkeypatch):
    """A read-only database stays read-only; reconnecting per poll would add a
    file open to every pass and heal nothing."""
    with State(tmp_path / "state.db") as ledger:
        reconnects = {"n": 0}
        monkeypatch.setattr(
            State, "_reconnect",
            lambda self: reconnects.__setitem__("n", reconnects["n"] + 1) or False,
        )
        monkeypatch.setattr(
            ledger, "_insert_snapshot",
            lambda **kw: (_ for _ in ()).throw(sqlite3.OperationalError("readonly")),
        )

        for _ in range(5):
            assert _snapshot(ledger) is False

        assert reconnects["n"] == 1


def test_correctness_writes_are_not_downgraded(tmp_path):
    """The classification, pinned. Only `record_snapshot` is best-effort: every
    other write is a dedupe key or an in-flight marker for an action the daemon
    TAKES, and swallowing one re-spawns a round or re-pages an operator. They
    raise, and the poll firewall is what keeps that from being fatal."""
    db = tmp_path / "state.db"
    with State(db) as ledger:
        _read_only(db)
        try:
            for call in (
                lambda: ledger.record_spawn(
                    repo="acme/widgets", number=7, round_=1,
                    head_sha="abc", session="s", task_ref=None,
                ),
                lambda: ledger.record_reap("s"),
                lambda: ledger.record_ping("acme/widgets", 7, "stalled:s"),
                lambda: ledger.record_escalation("acme/widgets", 7, "abc"),
                lambda: ledger.record_grant("acme/widgets", 7, 1, "rhdzmota", 2),
                lambda: ledger.note_verdict_post_owed("acme/widgets", 7, 1, "abc"),
            ):
                with pytest.raises(sqlite3.DatabaseError):
                    call()
        finally:
            _writable(db)


def test_the_telemetry_warning_is_streak_limited(tmp_path, monkeypatch, caplog):
    """One WARN per streak-limited window, not one per poll: a ledger that has
    gone read-only fails on every single pass."""
    caplog.set_level(logging.WARNING)
    with State(tmp_path / "state.db") as ledger:
        monkeypatch.setattr(State, "_reconnect", lambda self: False)
        monkeypatch.setattr(
            ledger, "_insert_snapshot",
            lambda **kw: (_ for _ in ()).throw(sqlite3.OperationalError("readonly")),
        )

        for _ in range(state_module.TELEMETRY_LOG_EVERY * 2):
            _snapshot(ledger)

    warnings = [r for r in caplog.records if "poll snapshot failed" in r.message]
    assert len(warnings) == state_module.TELEMETRY_LOG_HEAD + 2


class _NoRequests:
    """The narrowest GitHub stand-in a poll pass needs: nothing to review."""

    login = "alissa-app"

    def review_requests(self, repos):
        return []


class _OneRequest(_NoRequests):
    """One pending review request, so the pass has something to decide."""

    def review_requests(self, repos):
        return [("acme", "widgets", 7)]


class _SpawningWatcher(ReviewWatcher):
    """A watcher whose `evaluate` is the shape of `_spawn`: it takes the side
    effect FIRST (enqueue a reviewer session) and only then records it, which
    is the ordering the real `_spawn` has and the reason the blocker existed."""

    stop_after = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enqueued: list[str] = []
        self.polls = 0

    def poll_once(self):
        if self.polls >= self.stop_after:
            raise KeyboardInterrupt
        self.polls += 1
        return super().poll_once()

    def evaluate(self, owner, repo, number):
        session = f"review-{repo}-pr{number}-r1-{len(self.enqueued)}"
        self.enqueued.append(session)          # the side effect, taken
        self.state.record_spawn(               # ...and then recorded. Strict.
            repo=f"{owner}/{repo}", number=number, round_=1,
            head_sha="abc", session=session, task_ref=None,
        )
        return loop_module.Decision(loop_module.Action.SPAWNED, "spawned", 1)


class _NoSessions:
    """The narrowest Alissa stand-in a poll pass needs: no reviewer sessions
    live, so the reap sweep has nothing to decide."""

    def list_review_sessions(self):
        return []


class _StaleHandle:
    """A connection whose every call fails the way a handle over a remounted
    volume does, while the file behind it is fine."""

    def __init__(self):
        self.closed = False

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    def commit(self):
        raise sqlite3.OperationalError("disk I/O error")

    def close(self):
        self.closed = True


# -- round 2: a refused pass is not a successful one ------------------------


def test_a_gate_refused_pass_does_not_clear_the_failure_streak(tmp_path, clock, caplog):
    """A refused pass must leave the firewall's streak EXACTLY as it was.

    The gate returning normally made `run_forever` treat it as a recovery: it
    printed "poll recovered" at the moment the daemon stopped deciding
    anything, and re-armed POLL_ESCALATE_SECONDS for a fault that had not
    cleared. That is the same power the RateLimited branch was stripped of --
    a refused pass is not evidence the fault cleared, it is evidence the daemon
    did not look.
    """
    caplog.set_level(logging.INFO)
    db = tmp_path / "state.db"
    config = Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=db,
        poll_interval=60,
    )
    missing = FileNotFoundError(2, "No such file or directory", "alissa")
    with State(db) as ledger:
        w = _watcher(config, [missing] * 40)
        w.state = ledger
        # Every 4th pass the ledger is unwritable: the flapping-volume shape.
        real_poll, calls = w.poll_once, {"n": 0}

        def flapping_poll():
            calls["n"] += 1
            if calls["n"] % 4 == 0:
                w._note_ledger_unwritable()
                raise LedgerUnwritable(str(db))
            w._note_ledger_writable()   # as the real gate does on a live pass
            return real_poll()

        w.poll_once = flapping_poll  # type: ignore[method-assign]
        w.run_forever()

    # Specifically the FIREWALL's page, not the gate's own -- the gate escalates
    # on its own streak either way, and matching that would pass vacuously.
    escalations = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "poll has failed with" in r.getMessage()
    ]
    assert escalations, "interleaved refusals must not cancel the fault's page"
    assert "FileNotFoundError" in escalations[0].getMessage()
    # ...and the daemon never claimed to have recovered while deciding nothing.
    assert "poll recovered" not in caplog.text


def test_a_refused_pass_resets_the_backoff_to_the_poll_interval(tmp_path, clock):
    """Deliberate, and the counterpart of the assertion above: the streak is
    untouched, but the CADENCE returns to normal. Probing at cadence is the
    point of the gate -- inheriting a failing streak's 15-minute backoff would
    leave a healed volume unnoticed that long."""
    config = Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=tmp_path / "state.db",
        poll_interval=60,
    )
    missing = FileNotFoundError(2, "No such file or directory", "alissa")
    w = _watcher(config, [missing, missing, LedgerUnwritable("/x"), missing])

    w.run_forever()

    # 120, 240 while failing; back to the interval on the refusal; doubling
    # resumes from there.
    assert clock.slept[:4] == [120, 240, 60, 120]


def test_a_one_shot_over_an_unwritable_ledger_exits_non_zero(tmp_path, monkeypatch, capsys):
    """`--once` reports rather than retries, so a refused pass must not look
    clean to `... --once && echo ok` or to a health probe."""
    monkeypatch.setattr(loop_module.ReviewWatcher, "preflight", lambda self: [])
    monkeypatch.setattr(
        loop_module.ReviewWatcher, "poll_once",
        lambda self: (_ for _ in ()).throw(LedgerUnwritable("/vol/state.db")),
    )

    rc = main(["--once", "--workspace-root", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1, "1 is 'the environment failed'; 2 is reserved for a bad config"
    assert "ledger error" in err
    assert "/vol/state.db" in err
    assert "no decisions were taken" in err


def test_dry_run_is_not_gated_by_an_unwritable_ledger(tmp_path, caplog):
    """Dry-run suppresses every side effect AND every correctness write already,
    so the gate protects nothing there -- and refusing it would cost the
    operator the one tool that answers "what would you do right now" during
    exactly the incident this change is about."""
    caplog.set_level(logging.INFO)
    db = tmp_path / "state.db"
    config = Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=db,
        dry_run=True,
    )
    with State(db) as ledger:
        w = ReviewWatcher(config, github=_NoRequests(), alissa=_NoSessions(), state=ledger)
        _read_only(db)
        try:
            assert w.poll_once() == []   # evaluated, not refused
        finally:
            _writable(db)

    assert "skipping this pass entirely" not in caplog.text
    # Its one ledger write is the snapshot, which degrades to best-effort.
    assert "poll snapshot failed" in caplog.text


def test_the_ledger_gate_shares_the_firewall_s_escalation_contract(tmp_path, clock, caplog):
    """The gate's streak is the same `Streak` the firewall uses, so the
    crossing bypasses the streak limit here too -- the hand-rolled copy landed
    the first page-worthy line on whichever later pass happened to satisfy the
    modulo filter instead of on the pass that crossed the window."""
    caplog.set_level(logging.INFO)
    config = Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=tmp_path / "state.db",
    )
    w = ReviewWatcher(config, github=_NoRequests(), alissa=_NoSessions(), state=None)

    # Past the log head, still inside the window: suppressed.
    for _ in range(5):
        w._note_ledger_unwritable()
    before = len([r for r in caplog.records if r.levelno == logging.ERROR])
    # The pass that crosses the window logs, whatever the modulo says.
    clock.now += POLL_ESCALATE_SECONDS + 1
    w._note_ledger_unwritable()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert before == 0 and len(errors) == 1
    assert "no longer transient" in errors[0].getMessage()


def test_streak_is_one_implementation_for_both_users():
    """The nit's actual contract: whatever the streak limit and escalation are,
    both callers get the same ones, including the crossing's bypass."""
    s = Streak()
    logged = [s.record(float(i))[0] for i in range(1, 6)]

    assert logged[:POLL_FAILURE_LOG_HEAD] == [True] * POLL_FAILURE_LOG_HEAD
    assert logged[POLL_FAILURE_LOG_HEAD] is False
    assert s.record(POLL_ESCALATE_SECONDS + 1) == (True, True), "crossing bypasses the limit"
    assert s.resolve(POLL_ESCALATE_SECONDS + 2)[0] == 6
    assert s.resolve(0.0) is None
