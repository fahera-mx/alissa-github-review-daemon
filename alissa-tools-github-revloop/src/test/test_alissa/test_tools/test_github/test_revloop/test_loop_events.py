"""Loop-telemetry tests (issue #112): derivation, emitter, client, config.

What is pinned here, per the issue's testing contract:

* derivation from fixture ledger rows yields exactly the documented kinds with
  DETERMINISTIC dedupe keys (derived twice, byte-identical);
* a `verdict_posts` row emits NOTHING until `posted_at` (or `abandoned_at`)
  is set;
* batches split at the ingest cap of 200;
* a failed post is ONE warn and the pass completes — and the un-advanced
  watermark makes the next pass re-send;
* disabled by default is SILENT (no emitter, no client, no post);
* the REST client sends the bearer token and honors its timeout;
* the config key / CLI flags / env var layer correctly, and the container
  renderer's env var name is the one the library reads.

The ledger fixtures are built through the same `record_*` methods the daemon
uses — never hand-inserted rows — so a schema move breaks these tests instead
of silently derailing the derivation.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error

import pytest

from alissa.tools.github.revloop import alissa_client as client_module
from alissa.tools.github.revloop import loop as loop_module
from alissa.tools.github.revloop import loop_events
from alissa.tools.github.revloop import state as state_module
from alissa.tools.github.revloop.__main__ import build_parser, overrides_from
from alissa.tools.github.revloop.alissa_client import (
    AlissaAuthError,
    AlissaClient,
    AlissaError,
    AlissaTransient,
    MAX_EVENTS_PER_POST,
)
from alissa.tools.github.revloop.config import (
    DEFAULT_ALISSA_ENDPOINT,
    LOOP_EVENTS_ENV,
    Config,
)
from alissa.tools.github.revloop.loop import ReviewWatcher
from alissa.tools.github.revloop.loop_events import (
    LoopEventsEmitter,
    derive_events,
)
from alissa.tools.github.revloop.state import State

REPO = "acme/widgets"
HEAD = "a" * 40


@pytest.fixture
def ledger(tmp_path):
    with State(tmp_path / "state.db") as st:
        yield st


@pytest.fixture
def clock(monkeypatch):
    """Drive the ledger's clock, one distinct second per tick."""
    now = {"t": 1_000_000}

    def tick(step=1):
        now["t"] += step
        return now["t"]

    monkeypatch.setattr(state_module.time, "time", lambda: now["t"])
    tick.now = lambda: now["t"]
    return tick


class FakeClient:
    """Records batches; raises what it is told to."""

    def __init__(self, fail_with=None):
        self.batches: list[list[dict]] = []
        self.fail_with = fail_with

    def post_loop_events(self, events):
        if self.fail_with is not None:
            raise self.fail_with
        self.batches.append(list(events))
        return {"accepted": len(events), "duplicates": 0}

    @property
    def events(self):
        return [e for batch in self.batches for e in batch]


def by_kind(events, kind):
    return [e for e in events if e["kind"] == kind]


# -- derivation: documented kinds, deterministic keys ----------------------


def test_round_spawned_derives_from_the_spawn_ledger(ledger, clock):
    clock()
    ledger.record_spawn(
        repo=REPO, number=7, round_=2, head_sha=HEAD,
        session="review-widgets-pr7-r2-abcd", task_ref="TASK-500",
    )
    (event,) = derive_events(ledger)
    assert event["seat"] == "revloop"
    assert event["kind"] == "round.spawned"
    assert event["dedupeKey"] == f"revloop:round.spawned:{REPO}:7:2:{HEAD}"
    assert event["at"] == clock.now() * 1000
    assert event["repo"] == REPO
    assert event["prNumber"] == 7
    assert event["round"] == 2
    assert event["session"] == "review-widgets-pr7-r2-abcd"
    assert event["data"] == {"headSha": HEAD, "taskRef": "TASK-500"}


def test_an_owed_verdict_emits_nothing_until_posted_at_is_set(ledger, clock):
    clock()
    ledger.note_verdict_post_owed(REPO, 7, 1, HEAD)
    assert derive_events(ledger) == []

    clock()
    ledger.record_verdict_post(
        REPO, 7, 1, "https://github.com/acme/widgets/pull/7#r1",
        verdict="approve",
    )
    (event,) = derive_events(ledger)
    assert event["kind"] == "round.verdict"
    assert event["dedupeKey"] == f"revloop:round.verdict:{REPO}:7:1:{HEAD}"
    assert event["at"] == clock.now() * 1000
    assert event["round"] == 1
    assert event["data"]["verdict"] == "approve"
    assert event["data"]["headSha"] == HEAD
    assert event["data"]["reviewUrl"].endswith("#r1")
    assert event["data"]["attempts"] == 0
    # The round was never held on checks, so the field is absent, not zero.
    assert "checksHeldMs" not in event["data"]


def test_a_held_round_reports_checks_held_ms(ledger, clock):
    clock()
    ledger.note_verdict_post_owed(REPO, 7, 1, HEAD)
    ledger.record_checks_hold(REPO, 7, 1, "pending")
    clock(30)
    ledger.record_verdict_post(REPO, 7, 1, "url", verdict="approve")
    (event,) = derive_events(ledger)
    assert event["data"]["checksHeldMs"] == 30_000


def test_a_legacy_row_with_no_stored_verdict_omits_the_field(ledger, clock):
    clock()
    ledger.note_verdict_post_owed(REPO, 7, 1, HEAD)
    ledger.record_verdict_post(REPO, 7, 1, "url")  # pre-#112 caller shape
    (event,) = derive_events(ledger)
    assert event["kind"] == "round.verdict"
    assert "verdict" not in event["data"]


def test_an_abandoned_round_emits_round_abandoned_with_the_reason(
    ledger, clock
):
    clock()
    ledger.note_verdict_post_owed(REPO, 7, 3, HEAD)
    clock()
    ledger.record_verdict_post_abandoned(REPO, 7, 3, "head force-pushed away")
    (event,) = derive_events(ledger)
    assert event["kind"] == "round.abandoned"
    assert event["dedupeKey"] == f"revloop:round.abandoned:{REPO}:7:3:{HEAD}"
    assert event["reason"] == "head force-pushed away"
    assert event["data"] == {"headSha": HEAD}


def test_a_cap_out_emits_round_capped_with_the_round_at_cap_time(
    ledger, clock
):
    clock()
    ledger.record_spawn(
        repo=REPO, number=7, round_=1, head_sha=HEAD, session="s1",
        task_ref=None,
    )
    ledger.record_spawn(
        repo=REPO, number=7, round_=3, head_sha=HEAD, session="s3",
        task_ref=None,
    )
    clock()
    ledger.record_escalation(REPO, 7, HEAD)
    capped_at = clock.now()
    # A round spawned AFTER the cap-out (an operator re-entry) must not
    # retro-write the cap-out's round on a later re-derivation/backfill.
    clock()
    ledger.record_spawn(
        repo=REPO, number=7, round_=4, head_sha=HEAD, session="s4",
        task_ref=None,
    )
    (event,) = by_kind(derive_events(ledger), "round.capped")
    assert event["dedupeKey"] == (
        f"revloop:round.capped:{REPO}:7:{HEAD}:{capped_at}"
    )
    assert event["round"] == 3
    assert event["data"] == {"headSha": HEAD}


def test_a_re_cap_out_on_the_same_head_derives_a_distinct_key(ledger, clock):
    """[major, round 1] A grant consumed without an approve re-escalates the
    SAME head — a new decision the loop pages separately, so its event must
    not be swallowed as a server-side duplicate of the first cap-out."""
    clock()
    ledger.record_escalation(REPO, 7, HEAD)
    first = by_kind(derive_events(ledger), "round.capped")[0]["dedupeKey"]
    clock()
    ledger.record_escalation(REPO, 7, HEAD)  # REPLACEs the row in place
    second = by_kind(derive_events(ledger), "round.capped")[0]["dedupeKey"]
    assert first != second


def test_a_cap_out_with_no_spawn_row_omits_the_round(ledger, clock):
    clock()
    ledger.record_escalation(REPO, 7, HEAD)
    (event,) = derive_events(ledger)
    assert event["kind"] == "round.capped"
    assert "round" not in event


def test_a_stalled_ping_emits_stalled_with_the_session(ledger, clock):
    clock()
    ledger.record_ping(REPO, 7, loop_module.stalled_kind("review-pr7-r2-xy"))
    (event,) = derive_events(ledger)
    assert event["kind"] == "stalled"
    assert event["dedupeKey"] == f"revloop:stalled:{REPO}:7:review-pr7-r2-xy"
    assert event["session"] == "review-pr7-r2-xy"


def test_a_stability_ping_emits_stability_hold_with_the_notice_numbers(
    ledger, clock
):
    clock()
    # grants_seen deliberately DIFFERS from the ping kind's granted total:
    # grantsSeen must come from the kind (episode-correct even on a
    # backfill), while rcRounds/round come from the notice join, which is
    # documented as current-at-derivation (round-1 minor).
    ledger.record_stability_notice(REPO, 7, round_=5, rc_rounds=4,
                                   grants_seen=9)
    clock()
    base = "b" * 40
    ledger.record_ping(REPO, 7, loop_module.stability_kind(HEAD, base, 1))
    (event,) = by_kind(derive_events(ledger), "stability.hold")
    assert event["dedupeKey"] == (
        f"revloop:stability.hold:{REPO}:7:{HEAD}:{base}:1"
    )
    assert event["round"] == 5
    assert event["data"]["headSha"] == HEAD
    assert event["data"]["rcRounds"] == 4
    assert event["data"]["grantsSeen"] == 1  # the kind's, not the notice's


def test_checks_held_derives_from_both_gates_with_distinct_keys(
    ledger, clock
):
    clock()
    ledger.record_ping(REPO, 7, loop_module.checks_unsettled_kind(2, HEAD))
    ledger.note_spawn_checks_hold(REPO, 7, 2, HEAD)
    events = by_kind(derive_events(ledger), "checks.held")
    assert len(events) == 2
    keys = {e["dedupeKey"] for e in events}
    # The same (PR, round, head) waited at both gates: two facts, two keys —
    # one must not swallow the other's event server-side.
    assert keys == {
        f"revloop:checks.held:{REPO}:7:2:{HEAD}",
        f"revloop:checks.held:{REPO}:7:2:{HEAD}:spawn",
    }
    gates = {e["data"]["gate"] for e in events}
    assert gates == {"verdict", "spawn"}
    assert all(e["round"] == 2 for e in events)
    assert all(e["data"]["headSha"] == HEAD for e in events)


def test_read_grants_refuses_a_partial_filter(ledger):
    """[nit, round 2] repo-without-number must not fall through to the
    unfiltered read — a superset answer is the quiet kind of wrong."""
    with pytest.raises(ValueError, match="partial"):
        ledger.read_grants(REPO)
    with pytest.raises(ValueError, match="partial"):
        ledger.read_grants(number=7)
    assert ledger.read_grants() == []          # unfiltered form
    assert ledger.read_grants(REPO, 7) == []   # paired form


def test_a_grant_and_a_reap_derive_their_events(ledger, clock):
    clock()
    ledger.record_grant(REPO, 7, comment_id=987, author="RHDZMOTA", rounds=2)
    clock()
    ledger.record_reap("review-widgets-pr7-r1-zz")
    reaped_at = clock.now()
    events = derive_events(ledger)
    (grant,) = by_kind(events, "grant")
    assert grant["dedupeKey"] == f"revloop:grant:{REPO}:7:987"
    assert grant["data"] == {"author": "RHDZMOTA", "rounds": 2}
    (reap,) = by_kind(events, "reap")
    # The stamp is in the key so the event does not lean on session-name
    # nonce-uniqueness holding forever (round-1 nit).
    assert reap["dedupeKey"] == (
        f"revloop:reap:review-widgets-pr7-r1-zz:{reaped_at}"
    )
    assert reap["session"] == "review-widgets-pr7-r1-zz"
    assert "repo" not in reap  # the reaps table carries no repo/PR


def test_comment_dedupe_ping_kinds_derive_no_event(ledger, clock):
    """`activity-deferred:`, `capout:`, `checks-hold:` and
    `verdict-post-failed:` dedupe GitHub-side comments; they are not facts of
    their own and must not leak into telemetry as unknown kinds."""
    clock()
    ledger.record_ping(REPO, 7, "activity-deferred:sess")
    ledger.record_ping(REPO, 7, loop_module.capout_kind(HEAD, 0))
    ledger.record_ping(REPO, 7, loop_module.checks_hold_kind(1, HEAD))
    ledger.record_ping(REPO, 7, loop_module.verdict_post_kind(1))
    assert derive_events(ledger) == []


def test_derivation_is_deterministic_and_oldest_first(ledger, clock):
    clock()
    ledger.record_reap("s-old")
    clock()
    ledger.record_spawn(repo=REPO, number=7, round_=1, head_sha=HEAD,
                        session="s-new", task_ref=None)
    first = derive_events(ledger)
    second = derive_events(ledger)
    assert first == second
    assert [e["kind"] for e in first] == ["reap", "round.spawned"]


def test_since_filters_old_rows_inclusively(ledger, clock):
    clock()
    ledger.record_reap("s1")
    boundary = clock.now()
    clock()
    ledger.record_reap("s2")
    kept = derive_events(ledger, since=boundary)
    # >= on the boundary: a second row written in the boundary second must
    # never be lost, and re-sending the first is what the dedupe keys absorb.
    assert {e["session"] for e in kept} == {"s1", "s2"}
    assert derive_events(ledger, since=boundary + 1) != kept


def test_kind_prefixes_match_the_loops_own_constants():
    """The parser's prefixes are pinned against loop's kind builders, so a
    renamed ping kind fails HERE instead of silently deriving nothing."""
    assert loop_events.STALLED_PREFIX == loop_module.ESCALATION_STALLED + ":"
    assert (
        loop_events.STABILITY_PREFIX
        == loop_module.ESCALATION_STABILITY + ":"
    )
    assert loop_module.checks_unsettled_kind(4, HEAD).startswith(
        loop_events.CHECKS_UNSETTLED_PREFIX
    )
    assert loop_module.stalled_kind("s").startswith(
        loop_events.STALLED_PREFIX
    )
    assert loop_module.stability_kind(HEAD, HEAD, 0).startswith(
        loop_events.STABILITY_PREFIX
    )


def test_config_default_endpoint_matches_the_clients():
    assert DEFAULT_ALISSA_ENDPOINT == client_module.DEFAULT_ENDPOINT


# -- the emitter: batching, watermark, best-effort -------------------------


def test_batches_split_at_the_ingest_cap(ledger, clock):
    for i in range(MAX_EVENTS_PER_POST + 5):
        clock()
        ledger.record_reap(f"session-{i:04d}")
    client = FakeClient()
    emitter = LoopEventsEmitter(ledger, client)
    assert emitter.emit_once() is True
    assert [len(b) for b in client.batches] == [MAX_EVENTS_PER_POST, 5]
    assert len(client.events) == MAX_EVENTS_PER_POST + 5


def test_a_quiet_ledger_makes_no_request_at_all(ledger, clock):
    """[minor, round 1] The inclusive watermark must not turn into a standing
    re-POST: with nothing new, the boundary row's key is remembered and the
    pass derives an empty batch — no request, no log line."""
    clock()
    ledger.record_reap("s1")
    clock()
    ledger.record_reap("s2")
    client = FakeClient()
    emitter = LoopEventsEmitter(ledger, client)
    assert emitter.emit_once() is True
    assert {e["session"] for e in client.events} == {"s1", "s2"}

    client.batches.clear()
    assert emitter.emit_once() is True
    assert client.batches == []


def test_a_newcomer_in_the_watermark_second_is_still_sent(ledger, clock):
    """The other edge of the inclusive boundary: a row that lands in the SAME
    second as the watermark after the pass that set it must go out on the
    next pass — its key is not in the remembered set."""
    clock()
    ledger.record_reap("s1")
    client = FakeClient()
    emitter = LoopEventsEmitter(ledger, client)
    assert emitter.emit_once() is True
    ledger.record_reap("s2")  # same clock second as s1
    client.batches.clear()
    assert emitter.emit_once() is True
    assert {e["session"] for e in client.events} == {"s2"}
    # ...and exactly once: the set grew without moving the watermark.
    client.batches.clear()
    assert emitter.emit_once() is True
    assert client.batches == []


def test_a_failed_post_warns_once_and_the_next_pass_resends(
    ledger, clock, caplog
):
    clock()
    ledger.record_reap("s1")
    client = FakeClient(fail_with=AlissaTransient(503, "boom"))
    emitter = LoopEventsEmitter(ledger, client)
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        assert emitter.emit_once() is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "best-effort" in warnings[0].getMessage()

    # No retry queue: recovery is the un-advanced watermark re-deriving.
    client.fail_with = None
    assert emitter.emit_once() is True
    assert {e["session"] for e in client.events} == {"s1"}


def test_an_auth_failure_warns_once_and_latches_the_emitter_off(
    ledger, clock, caplog
):
    """[minor, round 1] Auth failures are permanent and operator-fixable, so
    they get ONE warn and no further attempts this process — not an
    identical WARN per poll for the daemon's whole life."""
    clock()
    ledger.record_reap("s1")
    client = FakeClient(fail_with=AlissaAuthError(0, "no token"))
    calls = {"n": 0}
    original = client.post_loop_events

    def counting(events):
        calls["n"] += 1
        return original(events)

    client.post_loop_events = counting
    emitter = LoopEventsEmitter(ledger, client)
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        assert emitter.emit_once() is False
        # Later passes return immediately: no derivation retry, no request,
        # no second warning — the restart that fixes the token re-arms.
        client.fail_with = None
        assert emitter.emit_once() is False
        assert emitter.emit_once() is False
    assert calls["n"] == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "restart" in warnings[0].getMessage()


def test_an_empty_ledger_posts_nothing(ledger):
    client = FakeClient()
    assert LoopEventsEmitter(ledger, client).emit_once() is True
    assert client.batches == []


# -- wiring into the watcher -----------------------------------------------


def _watcher(config, state):
    # The watcher stores its collaborators; stubs are enough for wiring tests.
    return ReviewWatcher(
        config, github=object(), alissa=object(), state=state
    )


def test_disabled_by_default_builds_no_emitter(tmp_path, ledger):
    config = Config(workspace_root=tmp_path, state_path=tmp_path / "state.db")
    assert config.loop_events_enabled is False
    w = _watcher(config, ledger)
    assert w._loop_events is None
    w._emit_loop_events()  # silent no-op, nothing to post with


def test_enabled_config_wires_an_emitter_over_the_endpoint(tmp_path, ledger):
    config = Config(
        workspace_root=tmp_path,
        state_path=tmp_path / "state.db",
        loop_events_enabled=True,
        alissa_endpoint="https://staging.example",
    )
    w = _watcher(config, ledger)
    assert isinstance(w._loop_events, LoopEventsEmitter)
    assert w._loop_events._client.base == "https://staging.example"


def test_dry_run_never_posts_even_when_enabled(tmp_path, ledger, clock):
    clock()
    ledger.record_reap("s1")
    config = Config(
        workspace_root=tmp_path,
        state_path=tmp_path / "state.db",
        loop_events_enabled=True,
        dry_run=True,
    )
    w = _watcher(config, ledger)
    client = FakeClient()
    w._loop_events = LoopEventsEmitter(ledger, client)
    w._emit_loop_events()
    assert client.batches == []


def test_an_emitter_defect_cannot_take_down_the_pass(tmp_path, ledger, caplog):
    config = Config(
        workspace_root=tmp_path,
        state_path=tmp_path / "state.db",
        loop_events_enabled=True,
    )
    w = _watcher(config, ledger)

    class Broken:
        def emit_once(self):
            raise RuntimeError("defect")

    w._loop_events = Broken()
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        w._emit_loop_events()  # must not raise
    assert any("best-effort" in r.getMessage() for r in caplog.records)


def test_record_verdict_post_stores_the_verdict_for_the_emitter(ledger):
    ledger.note_verdict_post_owed(REPO, 7, 1, HEAD)
    ledger.record_verdict_post(REPO, 7, 1, "url", verdict="request_changes")
    (row,) = ledger.read_verdict_posts()
    assert row["verdict"] == "request_changes"


def test_the_verdict_column_is_migrated_into_an_old_database(tmp_path):
    """A database created before the column exists gains it on open, with
    NULL on the historical rows — the emitter then omits the field."""
    path = tmp_path / "state.db"
    with State(path) as st:
        st._db.execute("ALTER TABLE verdict_posts DROP COLUMN verdict")
        st._db.commit()
    with State(path) as st:
        st.note_verdict_post_owed(REPO, 7, 1, HEAD)
        st.record_verdict_post(REPO, 7, 1, "url", verdict="approve")
        (row,) = st.read_verdict_posts()
        assert row["verdict"] == "approve"


# -- the REST client -------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for the module's redirect-refusing opener."""

    def __init__(self, fn):
        self._fn = fn

    def open(self, req, timeout=None):
        return self._fn(req, timeout=timeout)


def test_the_client_sends_the_bearer_token_and_honors_the_timeout(
    monkeypatch,
):
    seen = {}

    def fake_open(req, timeout=None):
        seen["req"] = req
        seen["timeout"] = timeout
        return FakeResponse(b'{"accepted": 1, "duplicates": 0}')

    monkeypatch.setattr(client_module, "_opener", FakeOpener(fake_open))
    client = AlissaClient(token="tok-123", base="https://api.example/",
                          timeout=7)
    result = client.post_loop_events([{"kind": "reap"}])

    assert result == {"accepted": 1, "duplicates": 0}
    req = seen["req"]
    assert req.full_url == "https://api.example/v1/loop-events"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer tok-123"
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data.decode()) == {"events": [{"kind": "reap"}]}
    assert seen["timeout"] == 7


def test_the_client_reads_the_env_token_when_none_is_passed(monkeypatch):
    monkeypatch.setenv(client_module.ENV_TOKEN, "from-env")
    seen = {}

    def fake_open(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return FakeResponse(b"{}")

    monkeypatch.setattr(client_module, "_opener", FakeOpener(fake_open))
    AlissaClient().post_loop_events([])
    assert seen["auth"] == "Bearer from-env"


def test_a_missing_token_is_an_auth_error_before_any_network(monkeypatch):
    monkeypatch.delenv(client_module.ENV_TOKEN, raising=False)

    def explode(req, timeout=None):  # pragma: no cover - must not be reached
        raise AssertionError("network was touched")

    monkeypatch.setattr(client_module, "_opener", FakeOpener(explode))
    with pytest.raises(AlissaAuthError):
        AlissaClient().post_loop_events([{"kind": "reap"}])


@pytest.mark.parametrize(
    "status,exc_type",
    [(401, AlissaAuthError), (403, AlissaAuthError),
     (429, AlissaTransient), (503, AlissaTransient), (400, AlissaError),
     (302, AlissaError)],  # a refused redirect surfaces as its own status
)
def test_http_errors_classify_onto_the_taxonomy(monkeypatch, status, exc_type):
    def fake_open(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, status, "err", None,
            io.BytesIO(b'{"error": "CODE", "message": "m"}'),
        )

    monkeypatch.setattr(client_module, "_opener", FakeOpener(fake_open))
    with pytest.raises(exc_type):
        AlissaClient(token="t").post_loop_events([])


def test_a_transport_failure_is_transient(monkeypatch):
    def fake_open(req, timeout=None):
        raise urllib.error.URLError("dns says no")

    monkeypatch.setattr(client_module, "_opener", FakeOpener(fake_open))
    with pytest.raises(AlissaTransient):
        AlissaClient(token="t").post_loop_events([])


def test_the_module_opener_refuses_redirects(monkeypatch):
    """[minor, round 1] The default redirect handler would replay the
    Authorization header toward wherever Location points (across hosts) and
    downgrade the POST to a GET. The module's opener must refuse instead."""
    handler = client_module._RefuseRedirects()
    req = urllib.request.Request("https://api.alissa.app/v1/loop-events")
    assert handler.redirect_request(
        req, io.BytesIO(b""), 302, "Found", {},
        "https://evil.example/collect",
    ) is None
    # ...and the opener the client actually uses carries that handler.
    installed = [
        h for h in client_module._opener.handlers
        if isinstance(h, client_module._RefuseRedirects)
    ]
    assert installed, "the module opener must install _RefuseRedirects"


def test_an_oversized_batch_is_refused_as_a_code_defect():
    with pytest.raises(ValueError):
        AlissaClient(token="t").post_loop_events(
            [{}] * (MAX_EVENTS_PER_POST + 1)
        )


# -- config key, CLI flags, env var ----------------------------------------


def test_config_defaults_are_off_and_the_public_endpoint(tmp_path):
    config = Config.build(tmp_path, environ={})
    assert config.loop_events_enabled is False
    assert config.alissa_endpoint == DEFAULT_ALISSA_ENDPOINT


def test_the_file_enables_and_the_cli_overrides_both_ways(tmp_path):
    args = build_parser().parse_args(["--no-loop-events"])
    config = Config.build(
        tmp_path, {"loop_events_enabled": True}, overrides_from(args),
        environ={},
    )
    assert config.loop_events_enabled is False

    args = build_parser().parse_args(["--loop-events"])
    config = Config.build(tmp_path, {}, overrides_from(args), environ={})
    assert config.loop_events_enabled is True


def test_the_env_var_outranks_the_file_and_the_cli(tmp_path):
    args = build_parser().parse_args(["--no-loop-events"])
    config = Config.build(
        tmp_path, {"loop_events_enabled": False}, overrides_from(args),
        environ={LOOP_EVENTS_ENV: "1"},
    )
    assert config.loop_events_enabled is True
    config = Config.build(
        tmp_path, {"loop_events_enabled": True}, None,
        environ={LOOP_EVENTS_ENV: "off"},
    )
    assert config.loop_events_enabled is False


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("No", False), ("OFF", False),
])
def test_env_var_boolean_spellings(tmp_path, raw, expected):
    config = Config.build(tmp_path, environ={LOOP_EVENTS_ENV: raw})
    assert config.loop_events_enabled is expected


def test_an_empty_env_var_is_unset_not_false(tmp_path):
    """The Dockerfile bakes `ALISSA_REV_LOOP_EVENTS_ENABLED=""` — empty must
    fall through to the file layer, exactly like the BOW id's env var."""
    config = Config.build(
        tmp_path, {"loop_events_enabled": True},
        environ={LOOP_EVENTS_ENV: "  "},
    )
    assert config.loop_events_enabled is True


def test_a_non_boolean_env_var_is_refused_not_silently_false(tmp_path):
    with pytest.raises(ValueError, match=LOOP_EVENTS_ENV):
        Config.build(tmp_path, environ={LOOP_EVENTS_ENV: "enable"})


def test_the_endpoint_comes_from_the_file_and_the_cli(tmp_path):
    config = Config.build(
        tmp_path, {"alissa_endpoint": "https://staging.example"}, environ={},
    )
    assert config.alissa_endpoint == "https://staging.example"
    args = build_parser().parse_args(
        ["--alissa-endpoint", "https://cli.example"]
    )
    config = Config.build(
        tmp_path, {"alissa_endpoint": "https://staging.example"},
        overrides_from(args), environ={},
    )
    assert config.alissa_endpoint == "https://cli.example"


def test_a_non_string_endpoint_is_refused_and_empty_means_default(tmp_path):
    with pytest.raises(ValueError, match="alissa_endpoint"):
        Config.build(tmp_path, {"alissa_endpoint": 8080}, environ={})
    config = Config.build(tmp_path, {"alissa_endpoint": "  "}, environ={})
    assert config.alissa_endpoint == DEFAULT_ALISSA_ENDPOINT


@pytest.mark.parametrize("bad", [
    "http://staging.example",       # cleartext toward a real host
    "ftp://api.alissa.app",         # not an HTTP scheme at all
    "api.alissa.app",               # no scheme — urllib would choke later
    "https://",                     # right scheme, no host (round-2 nit) —
                                    # would fail on the wire as a per-pass
                                    # transient WARN instead of at load
    "https:///v1",                  # schemed-but-hostless variant
])
def test_a_cleartext_or_schemeless_endpoint_is_refused_at_load(tmp_path, bad):
    """[minor, round 1] The client sends a bearer token with every POST, so a
    non-https endpoint puts it on the wire — refused where the operator
    reads `config error`, not discovered on the network."""
    with pytest.raises(ValueError, match="https"):
        Config.build(tmp_path, {"alissa_endpoint": bad}, environ={})


@pytest.mark.parametrize("ok", [
    "http://localhost:8080",
    "http://127.0.0.1:9999",
    "https://staging.example",
])
def test_https_and_loopback_http_endpoints_are_accepted(tmp_path, ok):
    config = Config.build(tmp_path, {"alissa_endpoint": ok}, environ={})
    assert config.alissa_endpoint == ok
