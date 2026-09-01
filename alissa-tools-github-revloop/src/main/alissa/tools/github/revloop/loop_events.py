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
  advances it to the newest ledger stamp sent. The boundary is INCLUSIVE (two
  rows can share a second, and a strict `>` advanced to the first one's stamp
  would lose the second forever); the standing re-post that inclusion would
  otherwise cause is closed by remembering the dedupe keys already sent at the
  watermark second and dropping them from the next derivation — so a quiet
  ledger derives an EMPTY batch and no request is made. A daemon restart
  resets both and the first pass re-sends the WHOLE ledger, batched: that is
  the backfill, not a bug — Studio dedupes every previously-seen key and
  keeps the history a fresh console needs. Be honest about the bound: NONE of
  the seven tables this module reads is pruned (`poll_snapshots` is the
  ledger's only self-bounding table, and this module does not read it), so
  the backfill — and the per-pass re-derivation the watermark then filters —
  is bounded only by the deployment's actual row counts. At today's volumes
  (hundreds of rows over a deployment's life) that is a fine trade for
  stateless simplicity; if a ledger ever outgrows it, the upgrade path is
  pushing the `since` bound into the readers' SQL, or persisting the
  watermark in the ledger so a restart does not backfill at all.

* **Derivation is pure reads.** No GitHub call, no ledger write, no new state
  table. A row the ledger deletes (a spawn-side CI hold whose wait ended
  between passes) is simply not observed; a row enriched from another table
  (`stability_notices` onto a `stability:` ping) reads whatever that table
  says NOW, which is also what the guard itself decides from.
"""

from __future__ import annotations

import logging
from typing import Any

from .alissa_client import (
    AlissaAuthError,
    AlissaClient,
    AlissaError,
    MAX_EVENTS_PER_POST,
)
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
    on rows that predate the ledger's verdict column.

    COVERAGE BOUND (PR #113 round 2, major): `verdict_posts` is the
    per-round OBLIGATION record, not the per-round record — a row exists
    only when the reviewer session defaulted and the daemon posted the
    native fallback verdict itself. On a fleet whose sessions post every
    review, these two kinds are expected to be EMPTY; issue #112 maps them
    to this table and puts the round bookkeeping a session-covering source
    needs out of scope, so the widening is TASK-1086576582, and the README's
    telemetry table states the bound where an operator will read it."""
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
    rows: "list[dict]", spawns: "list[dict]"
) -> "list[tuple[int, dict]]":
    """`round.capped` from the escalation table (CR9 cap-outs).

    The dedupe key folds the row's own `escalated_at` (PR #113 round 1,
    major): `escalations` is keyed per head and REPLACEd in place, and a
    re-cap-out on the SAME head is a designed path — an operator-granted
    round consumed without an approve is a new decision, which is the whole
    reason `loop.capout_kind` folds the granted total into its ping key. A
    key without the stamp would make Studio swallow the re-cap-out as a
    duplicate and under-report exactly the re-entry case. The stamp, not the
    granted total, because the stamp is ON the row: re-deriving the same row
    always yields the same key, while a granted total read at derive time
    would re-key an old row during a backfill.

    `escalations` carries no round of its own, so the round is read from the
    newest spawn at or before `escalated_at` — the round in flight when THIS
    cap-out was recorded, which stays true for a row backfilled long after
    the PR moved on — and omitted when no spawn row qualifies."""
    out = []
    for row in rows:
        repo, number = row["repo"], int(row["number"])
        head = row["head_sha"] or ""
        at = int(row["escalated_at"])
        rounds = [
            int(s["round"]) for s in spawns
            if s["repo"] == repo and int(s["number"]) == number
            and int(s["spawned_at"]) <= at
        ]
        out.append(_event(
            "round.capped",
            at,
            f"revloop:round.capped:{repo}:{number}:{head}:{at}",
            repo=repo,
            pr=number,
            round_=max(rounds) if rounds else None,
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
    GitHub-side comment, not a fact of its own) derive nothing.

    A stability event's payload is split by provenance, deliberately
    (PR #113 round 1, minor). `data.headSha` and `data.grantsSeen` come from
    the ping KIND itself (`stability:<head>:<base>:<granted>`, where the
    granted total is what the guard had accounted for when it held), so they
    are episode-correct even for a row backfilled long after. `data.rcRounds`
    and `round` come from the `stability_notices` join, and that table is
    REPLACEd per episode — so those two are CURRENT-AT-DERIVATION: a
    backfilled episode-1 ping carries the newest episode's numbers, because
    the ledger keeps nothing episode-scoped for them. Stated here so no
    reader mistakes the join for an episode guarantee."""
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
            parts = tail.split(":")
            head = parts[0]
            if head:
                data["headSha"] = head
            # The kind's own granted total — episode-correct, see above.
            if len(parts) == 3 and parts[2].isdigit():
                data["grantsSeen"] = int(parts[2])
            notice = notices.get((repo, number))
            if notice is not None:
                data["rcRounds"] = int(notice["rc_rounds"])
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
    # `reaped_at` folds into the key (PR #113 round 1, nit): `reaps` is
    # REPLACEd per session name, and while names are nonce-unique per spawn
    # today, the key should not depend on that holding forever — a re-reaped
    # name is a new decision, and the stamp is on the row, so re-derivation
    # stays deterministic.
    return [
        _event(
            "reap",
            int(row["reaped_at"]),
            f"revloop:reap:{row['session']}:{int(row['reaped_at'])}",
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
    notices = {
        (row["repo"], int(row["number"])): row
        for row in state.read_stability_notices()
    }

    stamped: "list[tuple[int, dict]]" = []
    stamped += _spawn_events(spawns)
    stamped += _verdict_events(state.read_verdict_posts())
    stamped += _capped_events(state.read_escalations(), spawns)
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
        # The dedupe keys already sent at exactly the watermark second. The
        # `since` filter is inclusive so a second row written in the boundary
        # second can never be lost — this set is what stops the OTHER edge of
        # that choice, the boundary row being re-posted every pass forever on
        # a ledger that has stopped changing (PR #113 round 1, minor).
        self._sent_at_watermark: "set[str]" = set()
        # Latched True by a permanent auth failure — see emit_once. A
        # process-lifetime latch on purpose: a restart is both how a fixed
        # token takes effect and how the operator re-arms the emitter.
        self._auth_failed = False

    def emit_once(self) -> bool:
        """Derive and post this pass's batch. True when everything landed
        (an empty derivation is vacuous success).

        An `AlissaAuthError` LATCHES the emitter off for the life of the
        process (PR #113 round 1, minor): the taxonomy calls it permanent and
        operator-fixable — a missing `ALISSA_API_TOKEN`, a token the ingest
        403s — so re-trying it would re-derive the whole ledger and write an
        identical WARN every poll interval for as long as the daemon runs.
        One WARN names the fix; later passes return at a DEBUG line, and the
        restart that installs a corrected credential also re-arms this.
        Transient errors keep the re-send behaviour — the watermark stays
        put and the next pass retries.
        """
        if self._auth_failed:
            log.debug(
                "loop-events: disabled since an authentication failure — "
                "restart the daemon after fixing the credential",
            )
            return False
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
        events = [
            e for e in events
            if not (
                int(e["at"]) // 1000 == self._since
                and e["dedupeKey"] in self._sent_at_watermark
            )
        ]
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
        except AlissaAuthError as exc:
            self._auth_failed = True
            log.warning(
                "loop-events: authentication failed (%s) — this is permanent "
                "and operator-fixable (set/rotate the token the emitter's "
                "client reads, then restart the daemon); loop telemetry is "
                "now off for this process, the loop keeps polling",
                exc,
            )
            return False
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
        newest = max(int(event["at"]) // 1000 for event in events)
        boundary_keys = {
            e["dedupeKey"] for e in events if int(e["at"]) // 1000 == newest
        }
        if newest == self._since:
            # The watermark did not move (a same-second newcomer): the set
            # GROWS, because the earlier boundary sends are still boundary.
            self._sent_at_watermark |= boundary_keys
        else:
            self._since = newest
            self._sent_at_watermark = boundary_keys
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
