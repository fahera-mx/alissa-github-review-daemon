"""Loop telemetry: derive Studio loop events from the ledger, once per pass.

The per-round record this daemon keeps — `verdict_posts` (first seen, attempts,
posted/abandoned, checks-held state, review URL), `spawns`, `escalations`,
`pings`, `grants`, `stability_notices`, `spawn_checks_holds`, `reaps` — lives
only in `.revloop/state.db` on the deployment's volume. The Factory console
needs rounds-to-approve, verdict mix, cap-outs and stability holds over time,
and Studio now ingests exactly that (`POST /v1/loop-events`, issue #112). This
module is the writer: at the end of each poll pass it derives events from the
ledger and posts ONE batch (split at 200, the ingest cap).

Design rules, all load-bearing:

* **The ledger is the source, not the pass.** Events carry the LEDGER's
  timestamps (`at` = the row's stamp, in epoch ms) and dedupe keys built from
  the LEDGER's own keys, so however many times a row is read, it derives the
  same event. That is what lets everything else be simple.

* **Best-effort, never fatal.** A failed post is one WARN and the pass
  completes; there is no retry queue, because there does not need to be one —
  the watermark below does not advance on failure, so the next pass re-derives
  and re-sends, and the deterministic keys make the re-emission land as silent
  duplicates server-side (idempotent on `(user, dedupeKey)`).

* **The watermark is in-memory and starts at zero.** Each successful emission
  advances it to the newest ledger stamp sent; rows at or after it are
  (re-)sent, which re-posts at most the boundary second's rows per pass —
  duplicates by construction, harmless by contract. A daemon restart resets it
  and the first pass re-sends the WHOLE ledger, batched: that is the backfill,
  not a bug — Studio dedupes every previously-seen key and keeps the history a
  fresh console needs, and the ledger's own retention is what bounds it.

* **Derivation is pure reads.** No GitHub call, no ledger write, no new state
  table. A row the ledger deletes (a spawn-side CI hold whose wait ended
  between passes) is simply not observed; a row enriched from another table
  (`stability_notices` onto a `stability:` ping) reads whatever that table
  says NOW, which is also what the guard itself decides from.
"""

from __future__ import annotations

import logging
from typing import Any

from .alissa_client import AlissaClient, AlissaError, MAX_EVENTS_PER_POST
from .state import State

log = logging.getLogger(__name__)

SEAT = "revloop"

# Ping-kind prefixes this module parses back out of the ledger. Kept as local
# constants rather than imported from `loop` because loop imports THIS module
# (the emitter is wired into the watcher), and a cycle is a worse trade than a
# pinned duplicate: a test asserts each against loop's own constant, so a
# renamed kind fails the suite instead of silently deriving nothing.
STALLED_PREFIX = "stalled:"
STABILITY_PREFIX = "stability:"
CHECKS_UNSETTLED_PREFIX = "checks-unsettled:"


def _ms(seconds: "int | float") -> int:
    """A ledger stamp (epoch seconds) as the API's epoch-ms `at`."""
    return int(seconds) * 1000


def _event(
    kind: str,
    at_s: int,
    dedupe_key: str,
    *,
    repo: "str | None" = None,
    pr: "int | None" = None,
    round_: "int | None" = None,
    session: "str | None" = None,
    reason: "str | None" = None,
    data: "dict[str, Any] | None" = None,
) -> "tuple[int, dict]":
    """One (ledger stamp, event payload) pair, with absent fields omitted
    rather than sent as null — the API validates shape per field."""
    payload: dict[str, Any] = {
        "seat": SEAT,
        "kind": kind,
        "at": _ms(at_s),
        "dedupeKey": dedupe_key,
    }
    if repo:
        payload["repo"] = repo
    if pr is not None:
        payload["prNumber"] = int(pr)
    if round_ is not None:
        payload["round"] = int(round_)
    if session:
        payload["session"] = session
    if reason:
        # The API caps `reason` at 2000 chars and refuses (never trims) an
        # over-cap value, which would fail the WHOLE batch — so the trim
        # happens here, where it costs one event's tail instead.
        payload["reason"] = reason[:2000]
    if data:
        payload["data"] = data
    return int(at_s), payload


def _spawn_events(rows: "list[dict]") -> "list[tuple[int, dict]]":
    out = []
    for row in rows:
        repo, number = row["repo"], int(row["number"])
        head = row["head_sha"] or ""
        data: dict[str, Any] = {"headSha": head}
        if row.get("task_ref"):
            data["taskRef"] = row["task_ref"]
        out.append(_event(
            "round.spawned",
            int(row["spawned_at"]),
            f"revloop:round.spawned:{repo}:{number}:{row['round']}:{head}",
            repo=repo,
            pr=number,
            round_=int(row["round"]),
            session=row["session"],
            data=data,
        ))
    return out


def _verdict_events(rows: "list[dict]") -> "list[tuple[int, dict]]":
    """`round.verdict` for posted rows, `round.abandoned` for abandoned ones.

    A row with neither `posted_at` nor `abandoned_at` is an OPEN obligation —
    the round's native verdict has not landed — and emits nothing until it
    closes one way or the other. `data.verdict` is omitted (never invented)
    on rows that predate the ledger's verdict column."""
    out = []
    for row in rows:
        repo, number = row["repo"], int(row["number"])
        round_, head = int(row["round"]), row["head_sha"] or ""
        if row.get("posted_at"):
            posted = int(row["posted_at"])
            data: dict[str, Any] = {"headSha": head}
            if row.get("verdict"):
                data["verdict"] = row["verdict"]
            if row.get("review_url"):
                data["reviewUrl"] = row["review_url"]
            data["attempts"] = int(row.get("attempts") or 0)
            if row.get("checks_held_at"):
                held = posted - int(row["checks_held_at"])
                data["checksHeldMs"] = max(held, 0) * 1000
            out.append(_event(
                "round.verdict",
                posted,
                f"revloop:round.verdict:{repo}:{number}:{round_}:{head}",
                repo=repo,
                pr=number,
                round_=round_,
                data=data,
            ))
        elif row.get("abandoned_at"):
            out.append(_event(
                "round.abandoned",
                int(row["abandoned_at"]),
                f"revloop:round.abandoned:{repo}:{number}:{round_}:{head}",
                repo=repo,
                pr=number,
                round_=round_,
                reason=row.get("last_error") or None,
                data={"headSha": head},
            ))
    return out


def _capped_events(
    rows: "list[dict]", newest_round: "dict[tuple[str, int], int]"
) -> "list[tuple[int, dict]]":
    """`round.capped` from the escalation table (CR9 cap-outs).

    `escalations` is keyed per head and carries no round of its own, so the
    round is read from the PR's newest spawn row — the round in flight when
    the cap was hit — and omitted when the ledger has no spawn to say."""
    out = []
    for row in rows:
        repo, number = row["repo"], int(row["number"])
        head = row["head_sha"] or ""
        out.append(_event(
            "round.capped",
            int(row["escalated_at"]),
            f"revloop:round.capped:{repo}:{number}:{head}",
            repo=repo,
            pr=number,
            round_=newest_round.get((repo, number)),
            data={"headSha": head},
        ))
    return out


def _ping_events(
    rows: "list[dict]", notices: "dict[tuple[str, int], dict]"
) -> "list[tuple[int, dict]]":
    """`stalled`, `stability.hold` and `checks.held` out of the ping ledger.

    `kind` is free-form text carrying the episode identity, so it is parsed
    by prefix; kinds this module does not report (`activity-deferred:`,
    `capout:`, `checks-hold:`, `verdict-post-failed:` — each the dedupe of a
    GitHub-side comment, not a fact of its own) derive nothing."""
    out = []
    for row in rows:
        repo, number = row["repo"], int(row["number"])
        kind, at = str(row["kind"]), int(row["pinged_at"])
        if kind.startswith(STALLED_PREFIX):
            session = kind[len(STALLED_PREFIX):]
            out.append(_event(
                "stalled",
                at,
                f"revloop:stalled:{repo}:{number}:{session}",
                repo=repo,
                pr=number,
                session=session,
            ))
        elif kind.startswith(STABILITY_PREFIX):
            tail = kind[len(STABILITY_PREFIX):]
            data: dict[str, Any] = {}
            head = tail.split(":", 1)[0]
            if head:
                data["headSha"] = head
            notice = notices.get((repo, number))
            if notice is not None:
                data["rcRounds"] = int(notice["rc_rounds"])
                data["grantsSeen"] = int(notice["grants_seen"])
            out.append(_event(
                "stability.hold",
                at,
                f"revloop:stability.hold:{repo}:{number}:{tail}",
                repo=repo,
                pr=number,
                round_=(int(notice["round"]) if notice is not None else None),
                data=data or None,
            ))
        elif kind.startswith(CHECKS_UNSETTLED_PREFIX):
            tail = kind[len(CHECKS_UNSETTLED_PREFIX):]
            round_str, _, head = tail.partition(":")
            round_ = int(round_str) if round_str.isdigit() else None
            out.append(_event(
                "checks.held",
                at,
                f"revloop:checks.held:{repo}:{number}:{round_str}:{head}",
                repo=repo,
                pr=number,
                round_=round_,
                data={"headSha": head, "gate": "verdict"},
            ))
    return out


def _spawn_hold_events(rows: "list[dict]") -> "list[tuple[int, dict]]":
    """`checks.held` from the pre-spawn CI gate's in-flight holds.

    Same kind as the verdict-side hold above — both mean "this round is
    waiting on this head's checks" — but a distinct dedupe key (`:spawn`
    suffix) and `data.gate`, because the two waits are different facts about
    the same round and one must not swallow the other's event."""
    out = []
    for row in rows:
        repo, number = row["repo"], int(row["number"])
        round_, head = int(row["round"]), row["head_sha"] or ""
        out.append(_event(
            "checks.held",
            int(row["first_at"]),
            f"revloop:checks.held:{repo}:{number}:{round_}:{head}:spawn",
            repo=repo,
            pr=number,
            round_=round_,
            data={"headSha": head, "gate": "spawn"},
        ))
    return out


def _grant_events(rows: "list[dict]") -> "list[tuple[int, dict]]":
    return [
        _event(
            "grant",
            int(row["granted_at"]),
            f"revloop:grant:{row['repo']}:{row['number']}:{row['comment_id']}",
            repo=row["repo"],
            pr=int(row["number"]),
            data={"author": row["author"], "rounds": int(row["rounds"])},
        )
        for row in rows
    ]


def _reap_events(rows: "list[dict]") -> "list[tuple[int, dict]]":
    return [
        _event(
            "reap",
            int(row["reaped_at"]),
            f"revloop:reap:{row['session']}",
            session=row["session"],
        )
        for row in rows
    ]


def derive_events(state: State, *, since: int = 0) -> "list[dict]":
    """Every loop event the ledger implies whose stamp is >= `since`,
    oldest first.

    Inclusive on the boundary on purpose: two rows can share a second, and a
    strictly-greater filter advanced to the first one's stamp would lose the
    second forever, while inclusion merely re-sends a key the API dedupes.
    """
    spawns = state.read_spawns()
    newest_round: "dict[tuple[str, int], int]" = {}
    for row in spawns:
        key = (row["repo"], int(row["number"]))
        newest_round[key] = max(newest_round.get(key, 0), int(row["round"]))

    notices = {
        (row["repo"], int(row["number"])): row
        for row in state.read_stability_notices()
    }

    stamped: "list[tuple[int, dict]]" = []
    stamped += _spawn_events(spawns)
    stamped += _verdict_events(state.read_verdict_posts())
    stamped += _capped_events(state.read_escalations(), newest_round)
    stamped += _ping_events(state.read_pings(), notices)
    stamped += _spawn_hold_events(state.read_spawn_checks_holds())
    stamped += _grant_events(state.read_grants())
    stamped += _reap_events(state.read_reaps())

    stamped = [(at, event) for at, event in stamped if at >= since]
    stamped.sort(key=lambda pair: pair[0])
    return [event for _, event in stamped]


class LoopEventsEmitter:
    """The once-per-pass push, watermarked and best-effort.

    Owned by the watcher when `loop_events_enabled` is on; `emit_once` is
    called at the end of every poll pass and NEVER raises for an API or
    transport condition — one WARN, the pass completes, and the un-advanced
    watermark is the whole retry story (see the module docstring).
    """

    def __init__(self, state: State, client: AlissaClient):
        self._state = state
        self._client = client
        # Epoch seconds of the newest ledger stamp successfully emitted.
        # Zero until the first success, so a fresh process backfills.
        self._since = 0

    def emit_once(self) -> bool:
        """Derive and post this pass's batch. True when everything landed
        (an empty derivation is vacuous success)."""
        try:
            events = derive_events(self._state, since=self._since)
        except Exception as exc:
            # A derivation failure is a ledger read gone wrong — the same
            # best-effort telemetry classification the snapshot writer has:
            # warn and let the pass complete, never kill the poll over it.
            log.warning(
                "loop-events: derivation failed (%s: %s) — skipping this "
                "pass's telemetry; the loop keeps polling",
                type(exc).__name__, exc,
            )
            return False
        if not events:
            return True
        sent = 0
        try:
            for start in range(0, len(events), MAX_EVENTS_PER_POST):
                chunk = events[start:start + MAX_EVENTS_PER_POST]
                result = self._client.post_loop_events(chunk)
                sent += len(chunk)
                log.debug(
                    "loop-events: posted %d event(s) (accepted=%s, "
                    "duplicates=%s)",
                    len(chunk), result.get("accepted"),
                    result.get("duplicates"),
                )
        except AlissaError as exc:
            # ONE warn per failed pass, naming how far it got; the watermark
            # stays put so the next pass re-derives and re-sends, and the
            # deterministic keys make the overlap land as duplicates.
            log.warning(
                "loop-events: post failed after %d of %d event(s) (%s) — "
                "telemetry is best-effort, the pass completes; the next "
                "pass re-sends (idempotent keys)",
                sent, len(events), exc,
            )
            return False
        self._since = max(int(event["at"]) // 1000 for event in events)
        log.info("loop-events: %d event(s) pushed", len(events))
        return True


def build_emitter(
    state: State, *, endpoint: "str | None" = None
) -> LoopEventsEmitter:
    """The emitter the watcher wires in when `loop_events_enabled` is on.

    A seam, so tests build emitters over fake clients while the watcher's
    call stays one line. The token comes from the environment inside
    `AlissaClient` (the CLI's own `ALISSA_API_TOKEN`); its absence surfaces
    as the emitter's WARN, never at construction — a daemon must boot and
    poll whether or not telemetry can authenticate.
    """
    return LoopEventsEmitter(state, AlissaClient(base=endpoint))
