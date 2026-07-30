"""The watcher loop.

One pass = poll GitHub for pending review requests, decide per PR whether a
fresh reviewer round is owed, and enqueue it. Rounds are derived from GitHub
(one *substantive* submitted review per round -- empty-bodied records are
inline-comment artifacts, not rounds), not from local bookkeeping. Convergence
comes from either the GitHub review state or the CR6 verdict envelope on the
Alissa review task.
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .alissa import (
    VERDICT_APPROVE,
    VERDICT_REQUEST_CHANGES,
    Alissa,
    ManagedSession,
    SessionRef,
    Task,
    session_repo_slug,
)
from .config import (
    HUB_ADD,
    ON_MISSING_SKIP,
    STALE_ROUND_SECONDS,
    Config,
)
from .ghclient import (
    CHECKS_GREEN,
    CHECKS_PENDING,
    CHECKS_RED,
    CHECKS_UNKNOWN,
    EVENT_APPROVE,
    EVENT_COMMENT,
    EVENT_REQUEST_CHANGES,
    CheckRollup,
    GitHub,
    IssueComment,
    PullRequest,
    RateLimited,
    Review,
    countable_rounds,
    verdict_marker,
)
from .proc import CommandError
from .state import State

log = logging.getLogger(__name__)

# The floor under the liveness deferral: a live session defers the stale
# respawn indefinitely -- correct for a genuinely slow round, silent forever
# for a session that is wedged but still registers tmux activity. Once the
# newest spawn's age reaches this multiple of STALE_ROUND_SECONDS, the loop
# posts one "stalled" operator comment per deferral episode (stalled_kind)
# and keeps deferring. 2 means the deferral itself has lasted a full extra
# stale window beyond the point the timer first fired -- long enough that a
# healthy round has almost always submitted by then, early enough that a
# wedged one surfaces the same day.
STALLED_DEFER_MULTIPLE = 2

# The sweep only reaps a session that has been idle AND quiet for
# `config.reap_grace_seconds`. The GitHub review count increments the moment a
# review is submitted, but the reviewer still has close-out work after that
# (CR6 envelope, task status) -- and a claude session parked at its prompt
# between turns reports "idle", so idleness alone cannot distinguish "between
# turns" from "done". Recent tmux activity can. The same number answers the
# stale-round liveness probe, which asks the same question; see
# config.DEFAULT_REAP_GRACE_SECONDS.

# How long the daemon waits, after first seeing a round whose verdict envelope
# has no native reviewer-identity review, before posting that review itself.
# The window exists for one race: the reviewer session writes its envelope and
# submits its own review moments apart, and a poll landing between the two
# would post a second record for a round that was about to close itself. Five
# minutes is far longer than that gap and far shorter than a round, so a
# genuinely missing post (the studio #298 shape) still heals within one cycle
# of the operator noticing nothing.
VERDICT_POST_GRACE_SECONDS = 5 * 60

# Failed post attempts before the daemon stops treating it as transient and
# pages a human. It keeps retrying afterwards -- the round stays OPEN either
# way, which is the invariant -- but a permanent failure (a revoked token, a
# repo the reviewer identity cannot review) must not stay a log line.
MAX_VERDICT_POST_ATTEMPTS = 5

# Ceiling on the retry backoff below. "Keep retrying" is the invariant; "keep
# retrying every poll forever" is not: at the deployed 30s interval a
# permanently-failing post costs ~2,880 review POSTs and as many task reads a
# day, per stuck PR, with the operator paged exactly once.
MAX_VERDICT_POST_BACKOFF_SECONDS = 60 * 60

# The GitHub review states the CI gate can produce for a round it refused to
# approve: a red head lands as CHANGES_REQUESTED, a rollup that never concluded
# as COMMENTED. Read by _convergence_reason, which must not converge on an
# approve envelope whose native verdict is one of these -- and must not be
# broader than this, or it would silently redefine what a DISMISSED review means
# for convergence (see the comment there).
GATED_VERDICT_STATES = ("COMMENTED", "CHANGES_REQUESTED")


def _post_delay_after(attempts: int) -> float:
    """Seconds to wait after attempt `attempts` before trying again.

    An inter-attempt DELAY, deliberately not a deadline measured from a fixed
    origin: a capped absolute deadline stops bounding anything the moment the
    row is older than the cap, and every later poll attempts the post again --
    the hot loop back an hour late. Measured from `last_attempt_at`, the wait
    keeps holding at any age.

    Exponential in the attempt count and capped, so the first retries still
    come at roughly poll cadence (the right answer to a blip) and a hopeless
    post settles at a single attempt an hour. The round never closes either
    way; only the cadence is bounded.
    """
    return min(2 ** max(attempts, 0) * 60, MAX_VERDICT_POST_BACKOFF_SECONDS)


# The native verdict review body. The verdict word is the payload; the round
# and the review task make it auditable, and the marker (invisible in the
# rendered comment) is what keeps this from being counted as a second round
# alongside the reviewer session's own write-up.
NATIVE_VERDICT_BODY = (
    "**Review round {round} — `{verdict}`**\n\n"
    "Submitted by the review daemon under the configured reviewer identity, so "
    "this round has a verdict of record on GitHub. The round's findings and "
    "reasoning are in the reviewer session's own comments on this PR{task_note}."
    "{head_note}\n\n"
    "{marker}"
)

# Appended to the body when the round's review task is known.
_VERDICT_TASK_NOTE = ", and the CR6 verdict envelope is on `{task_ref}`"

# Appended when the head moved between the round being queued and this post.
# The review record already says so (it is pinned to `{judged}`), but only a
# reader who checks the commit would notice, and "an approve that does not
# cover the current head" is the one thing about this record that changes what
# happens next: the loop treats the PR as owing another round.
HEAD_MOVED_NOTE = (
    "\n\n> This verdict judged `{judged}`; the head is now `{head}`. It is "
    "recorded against the commit it actually reviewed, so it does **not** "
    "count as a verdict on the newer code — the next round is owed."
)

# Prepended to a verdict body whose APPROVE the CI gate turned into a
# REQUEST_CHANGES: the head's own checks are red. It leads with the failing
# names and run URLs because that is the only actionable content of the review
# -- the code review found nothing (the envelope reads approve) and the operator
# would otherwise have to go hunting for what "not approved" refers to.
CHECKS_RED_LEAD = (
    "**Not approving `{sha}` — its CI checks are red.**\n\n"
    "{failing}\n\n"
    "The code review itself reached `{verdict}`: nothing below is a new finding "
    "about the diff. What blocks the approve is the head's own check rollup. An "
    "approve from this identity is the operator's merge cue, so it has to mean "
    "*reviewed AND green* — on 2026-07-29 (studio #323) a round approved 3.4 "
    "hours after CI had gone red, and the red sat unaddressed because the "
    "approve read as ready.\n\n"
    "Fix (or re-run) the check(s) above and re-request review; the next round "
    "can approve the same code on a green head. No label was touched.\n\n"
)

# One bullet per blocking context. The URL is what makes the finding walkable;
# a check run without one (rare, and never for Actions) still names itself.
CHECKS_FAILING_LINE = "- `{name}` — {conclusion}{url}"

# Prepended when the gate held the approve for the whole wait bound and the
# rollup still had not settled, so the verdict lands as a COMMENT.
CHECKS_UNSETTLED_LEAD = (
    "**Recorded as a comment, not an approve — the CI rollup at `{sha}` never "
    "concluded.**\n\n"
    "{detail}\n\n"
    "This round's verdict was held for {waited} min waiting for the head's "
    "checks to settle ({bound} min bound){total_note} and they did not, so it is "
    "recorded as a comment: an approve would claim a head this loop never saw go "
    "green. "
    "Nothing about the review itself changed — the verdict below is the round's "
    "own.\n\n"
    "Submitting this review consumes the pending review request, so the daemon "
    "will not look at this PR again on its own: **conclude or fix the checks and "
    "re-request review**, and the next round can approve the same code on a "
    "green head. No label was touched.\n\n"
)

# The operator page that follows a DEGRADED verdict, and the reason it exists:
# submitting any review -- `COMMENT` included -- consumes the pending review
# request, so the PR leaves `review-requested:@me` the moment the degraded
# verdict lands. Nothing then brings it back on its own: a `COMMENT` is not what
# the DEV fix/re-request flow keys on (that reads REQUEST_CHANGES), and the
# operator's own cue, an APPROVE, never comes. Without this the PR strands
# silently with an unsettled rollup and no cap-out -- the same class as the #227
# latch. The red path needs no page: REQUEST_CHANGES re-enters by itself.
#
# One page per (round, judged head) through the ping ledger, and it names what
# to do rather than just what happened.
CHECKS_UNSETTLED_PAGE = (
    "**Review round {round} could not approve `{sha}` — its CI rollup never "
    "concluded.**\n\n"
    "{detail}\n\n"
    "The round's verdict is on the PR as a comment-mode review ({url}) rather "
    "than an approve: the code review itself reached `{verdict}`, but an approve "
    "from `{reviewer}` is the merge cue and this loop never saw the head go "
    "green.\n\n"
    "**This needs a human, because the loop cannot re-enter on its own.** "
    "Submitting that review consumed the pending review request, so this PR has "
    "left the daemon's attention set. Two ways forward:\n\n"
    "1. conclude or fix the checks, then **re-request review from "
    "`{reviewer}`** — the next round reviews the same code and can approve it on "
    "a green head;\n"
    "2. merge with a recorded waiver, if the unsettled checks are known-broken "
    "rather than known-red.\n\n"
    "No label was touched and no further round is queued."
)

# Appended to `{bound}` above only when the hold was PROMOTED -- an unreadable
# wait that became a genuine pending one restarts the clock, so the bound the
# operator configured applies per condition and the round can be held up to
# twice it. Saying "held 30 min (30 min bound)" after 60 real minutes is the
# report being wrong about the one number an operator tunes.
CHECKS_TOTAL_HELD = ", {total} min in total across both waits,"

# The `{detail}` above, per reason the rollup did not settle.
CHECKS_STILL_RUNNING = "Still running at the bound: {names}."
CHECKS_UNREADABLE = (
    "The rollup could not be read: `{why}`. An unreadable rollup is not a green "
    "one — check that the reviewer credential carries `checks: read` on this "
    "repo."
)

# The operator page for a native verdict post that keeps failing. Loud on
# purpose: until it lands the round is NOT closed, so the loop is stalled on
# this PR and no amount of waiting fixes it.
VERDICT_POST_FAILED_COMMENT = (
    "**Review round {round} cannot be closed** — its verdict (`{verdict}`) is "
    "recorded on the review task, but the daemon has failed {attempts} times to "
    "submit it as a native GitHub review under the reviewer identity "
    "(`{reviewer}`).\n\n"
    "Last error:\n```\n{error}\n```\n\n"
    "A round is not complete until that review exists: the verdict has no "
    "record on GitHub, the pending review request is never consumed, and the "
    "daemon will not queue the next round. It keeps retrying, but this usually "
    "needs a credential fix — check that the reviewer token is present, valid, "
    "and belongs to `{reviewer}`. Submitting the review by hand as `{reviewer}` "
    "also closes the round."
)

# Told to the reviewer session when the daemon knows which environment variable
# carries the reviewer credential. Sessions run in a shared container whose
# default `gh` credential belongs to the IMPLEMENTER identity, which is how
# round-1 verdicts landed under the wrong login on studio #298/#302; naming the
# variable (never its value) is what lets a session route around the default.
# It deliberately does NOT assume the variable is there: the daemon populates
# its own environment, not the worker's, so an unexported variable would expand
# to an empty string -- and `gh` reads an empty GH_TOKEN as "no token" and falls
# back to the container default, which is the very identity this clause exists
# to avoid. Verify, then write or skip; the daemon's own post closes the round
# either way, so skipping costs nothing and a blind write costs the verdict.
_POST_AS_REVIEWER = (
    "CREDENTIAL — this container's default `gh` credential is NOT the reviewer "
    "identity. Before any `gh` call that WRITES to the PR, check that "
    "`${env_var}` is non-empty AND that "
    "`GH_TOKEN=\"${env_var}\" gh api user --jq .login` prints `{reviewer}`. If "
    "both hold, prefix every write with `GH_TOKEN=\"${env_var}\" ` (the "
    "variable, never its value). If either fails, do NOT post the review at "
    "all — say so in your verdict evidence and stop: an empty GH_TOKEN falls "
    "back to the default credential, and a review under it is not the round's "
    "verdict of record. The daemon submits the native verdict either way, so "
    "skipping the write loses nothing. "
)

# The closing contract is spelled out in both directives (not just the skill)
# because it is the reviewer's most-skipped step: on re-review, sessions produce
# findings but never register the review on the PR, or stop without a verdict.
_CLOSE_THE_ROUND = (
    "CLOSE THE ROUND — both are mandatory or the round does not count: "
    "(1) SUBMIT your review so it lands as one registered review record ON the "
    "PR (gh pr review / the reviews API) and confirm it with "
    "`gh api repos/<org>/<repo>/pulls/<n>/reviews` — findings left only in your "
    "session do not exist; (2) end with a decisive verdict — approve OR "
    "request_changes, never neither, never comment-only. You are read-only: "
    "never commit or fix, even a one-character typo — a needed fix IS "
    "request_changes. "
)

# Reviewers are one-shot per round (CR3), so a finished session should not linger
# holding a worker slot. The daemon reaps it as a backstop, but the fast path is
# the reviewer releasing its own slot as its very last action. {session} is the
# reviewer's own managed session name, injected at spawn.
_RELEASE_SLOT = (
    "FINALLY, and only once the round is fully closed above (review registered "
    "AND verdict recorded), release your worker slot as your last action: run "
    "`alissa tmux kill {session}`. Do nothing after it."
)

# The cap the reviewer must write down. The review-task description template in
# the alissa-code-review skill documents a default of 3, which this daemon's
# config has not used since the cap moved to 10 -- so round 1 (the round that
# CREATES the review task) has to carry the effective number, or every task
# description keeps repeating the stale one. Both directives say it: a round-k
# reviewer reading a task that records the wrong cap is the case that surfaced
# the drift in the first place.
_RECORD_THE_CAP = (
    "This loop's EFFECTIVE round cap is {cap} (the daemon's config, plus any "
    "operator re-entry grants) — record THAT number in the review task "
    "description, and correct it if the description carries a different cap "
    "from a stale template default. "
)

ROUND_1_DIRECTIVE = (
    "You are a PR REVIEWER, not an implementer. {assignment} "
    "Load the alissa-code-review skill and follow procedures/review-a-pr.md: "
    "hydrate the task and the PR it names, review per the rubric, post "
    "severity-tagged comments via gh pr review, record the verdict evidence, "
    "move the task to pending_validation. "
    + _RECORD_THE_CAP
    + "{credential}"
    + _CLOSE_THE_ROUND +
    "NEVER push commits, merge, or change PR state. "
    "Do NOT create further ali-* sessions. "
    + _RELEASE_SLOT
)

ROUND_K_DIRECTIVE = (
    "You are a PR REVIEWER, not an implementer — round {round} of a review loop "
    "(cap {cap}). {assignment} "
    "Load the alissa-code-review skill and follow procedures/review-a-pr.md "
    "including its round-k section: verify the triage of every prior finding, "
    "verify the fixes, sweep the new diff with the full rubric, record a "
    "round-{round} verdict envelope, move the task to pending_validation. "
    + _RECORD_THE_CAP
    + "{credential}"
    + _CLOSE_THE_ROUND +
    "NEVER push commits, merge, or change PR state. "
    "Do NOT create further ali-* sessions. "
    + _RELEASE_SLOT
)

# The operator re-entry ack (issue #42). A capped PR is unreviewable until an
# operator says otherwise, and the cap-out message used to OFFER "re-enter with
# a fresh cap" without any mechanism existing: the only lever was raising
# round_cap for every PR and restarting the daemon. The grammar below is that
# mechanism, and it is deliberately dull -- one line, a literal prefix, an
# explicit +N -- so it is trivially auditable in the PR's comment history and
# impossible to fire by accident.
REENTRY_GRAMMAR = "alissa-review: re-enter +N"

# The ceiling on a single ack. A constant, not a config key: the point of the
# ack is a SMALL bounded re-entry (the live case wanted exactly one round), and
# an operator who needs more says so again in another comment -- which leaves
# two auditable rows instead of one giant grant. Pinned by a test.
MAX_REENTRY_ROUNDS = 5

# "Is this line trying to be a directive?" -- the loose sieve. A line that
# reaches for the prefix but misses the grammar is reported as malformed
# (a silent typo would look exactly like a cap that refuses to lift).
_ACK_LEAD_RE = re.compile(r"^`?\s*alissa-review\s*:", re.IGNORECASE)

# The grammar itself: the WHOLE line, optionally wrapped in backticks, is the
# directive. Anchored on both ends so a mention inside prose ("just post
# `alissa-review: re-enter +1`") is not a directive, and quoted lines (`>`) are
# dropped before matching so replying to the escalation cannot re-grant it.
_ACK_RE = re.compile(
    r"^`?\s*alissa-review:\s+re-enter\s+\+(\d{1,3})\s*`?$", re.IGNORECASE
)

ESCALATION_COMMENT = (
    "**Review loop cap-out (CR9)** — {rounds} rounds ran on this PR without "
    "converging on `approve` (effective cap {cap}).{grant_note} Per the "
    "alissa-code-review skill the loop does not run past the cap and never "
    "silently merges, so it stops here: this needs an operator decision.\n\n"
    "{verdict_line}\n"
    "{verification}"
    "\n**Operator re-entry — grant N more rounds.** Comment with a line that "
    "reads exactly:\n\n"
    "```\n" + REENTRY_GRAMMAR + "\n```\n\n"
    "…with `N` from 1 to {max_rounds}. It is honoured only from an allowlisted "
    "operator account (the daemon's `operators` config), counted once per "
    "comment — a further grant needs a further comment — logged, and appended "
    "to the review-loop activity comment. Anything else is ignored: a quoted "
    "line, another wording, a bigger N, any other author.\n\n"
    "Without an ack no further round runs. The other options are unchanged: "
    "merge with a recorded waiver, or park it."
)

# The last-verdict line. Two shapes, because one sha is not enough once the
# head can have moved: the page has to say what was judged AND what is sitting
# there now, or an operator reading "last verdict at <current head>" concludes
# the verdict already covers the pushed fixes -- the exact opposite of what the
# verification hint below is telling them.
VERDICT_LINE = "Last verdict: `{last_state}` at `{sha}`."
VERDICT_LINE_MOVED = (
    "Last verdict: `{last_state}` on `{reviewed}` — the head is now `{sha}`."
)

# Only when the head has moved past the head the last verdict was written
# against -- the PR #277 shape, where the fixes are already pushed and sitting
# unreviewed. That case wants exactly one round, and saying so at the moment
# the operator reads the page is the whole point of the lever being
# discoverable.
VERIFICATION_HINT = (
    "\n**The head has moved since that verdict** (`{sha}`, reviewed at "
    "`{reviewed}`) — fixes are already pushed and unreviewed, so one round is "
    "usually all this needs: ack `alissa-review: re-enter +1` and the next "
    "reviewer verifies the fix against its own final findings and flips to "
    "approve (or re-requests, consuming the grant).\n"
)

# Named in the fresh escalation that fires once a grant has been spent without
# an approve, so the page says which decision was already tried.
GRANT_CONSUMED_NOTE = (
    " The re-entry granted by @{author} (`+{rounds}`, comment {comment_id}) "
    "has been consumed without an approve."
)

STALLED_COMMENT = (
    "**Review round stalled?** — round {round} has been in flight {minutes} min "
    "(stale window: {stale} min), but its reviewer session `{session}` still "
    "shows signs of life, so the daemon keeps deferring the respawn — "
    "respawning over a live session double-spends the round: two reviewers "
    "work it, both submit. Is that session actually making progress? Operator "
    "options: inspect it (`alissa tmux ls`) and, if it is wedged, kill it "
    "(`alissa tmux kill {session}`) so the respawn proceeds next poll, or "
    "finish the round by hand."
)

# The hidden marker that identifies THE activity comment on a PR. Find-or-create
# keys on it (plus own authorship -- anyone can paste the marker into their own
# comment, and a spoofed marker must never be PATCHed), so every append lands in
# the same single comment however many rounds run.
ACTIVITY_MARKER = "<!-- alissa-revloop:activity -->"

ACTIVITY_HEADER = (
    ACTIVITY_MARKER + "\n"
    "**Review-loop activity** — mechanical spawn/round log; the daemon appends "
    "a line each time it queues (or defers) a reviewer round on this PR."
)

# The ping-ledger kind prefix for the stalled-deferral operator ping. Unlike
# the cap-out escalation (terminal per head), a stall can recur, so the kind
# is narrowed per episode -- see stalled_kind.
ESCALATION_STALLED = "stalled"


def deferral_activity_kind(session: str) -> str:
    """The ping-ledger kind that dedupes ONE deferral episode's activity line.

    A deferral is re-decided every poll, so appending per decision would grow
    the activity comment by one identical line a minute for as long as the
    session holds out. One line per episode carries the same information --
    "the daemon is deferring behind this session" -- and the session name is
    the episode identity (nonce-unique per spawn), exactly as stalled_kind
    reasons for the operator ping. Recorded only after the append lands, so a
    transient failure retries next poll.
    """
    return f"activity-deferred:{session}"


def stalled_kind(session: str) -> str:
    """The ping-ledger kind that dedupes ONE deferral episode's operator ping.

    Devloop's stalled_kind reasoning, transposed: a stall can recur -- every
    spawn of every round can wedge mid-flight, and episode k's ping must not
    silence episode k+1's. Keyed on the bare kind (or even on the round), the
    re-enqueue of a round that wedges AGAIN would defer silently forever. The
    session name already IS the episode identity -- nonce-unique per spawn
    (see session_name) -- so it folds into the key. Delivery contract: the
    ledger row lands only AFTER the comment posts (see _escalate_stalled), so
    a transient comment failure retries next poll and the ping lands exactly
    once per episode.
    """
    return f"{ESCALATION_STALLED}:{session}"


def capout_kind(head_sha: str, granted: int) -> str:
    """The ping-ledger kind that dedupes ONE cap-out page.

    `escalations` is keyed by head alone, which was the whole story while the
    cap was global: same head, same decision, one page. A re-entry grant breaks
    that -- rounds run on the operator's own authority and end without an
    approve on the very SAME head, and that is a new decision, not a repeat of
    the page the operator already answered. Folding the PR's granted total into
    the kind says exactly that: one page per (head, grant total), so a consumed
    grant pages once more and then goes quiet until the next ack. Deliberately
    not a timestamp comparison against the escalation row -- ordering by
    wall-clock seconds ties when two events land in the same second, and the
    tie fails in the loud direction (a page every poll).
    """
    return f"capout:{head_sha}:{granted}"


def identity_drift_kind(requested: str, author: str) -> str:
    """The ping-ledger kind that dedupes ONE request/author identity-drift warning.

    Scope is once per PR per ordered login pair -- the ledger keys on
    `(repo, number, kind)`, so folding the pair into the kind narrows the
    dedupe WITHIN a PR and cannot lift it above one. What the pair buys is that
    a drift which later becomes a DIFFERENT drift announces itself again
    instead of staying quiet because this PR once warned about something else.

    Durable rather than in-memory because it is a configuration alarm, not a
    per-poll observation: a restart re-stating it on every open PR would be a
    wall of noise. Per-PR is bounded in practice by the withdrawal itself --
    once the request is gone the PR leaves the poll set, so a drifted
    deployment emits one block per PR it closes a round on, not one per poll.
    """
    return f"reqdrift:{requested}->{author}"


def drift_probe_kind(head_sha: str) -> str:
    """The ping-ledger kind that bounds the drift check's EXTRA read.

    The check needs the unfiltered review list, which `my_reviews` cannot
    supply -- a review by another identity is exactly what it filters out. That
    is one more API call, and on a PR whose withdrawal fails permanently (a
    reviewer identity without `pull_requests: write`) the close-out path runs
    every poll, so "one extra call on the rare path" would quietly become one
    per poll forever.

    Keyed on the head so the probe re-runs when the code moves -- a new head
    means a new round and a new chance for its verdict to land under the wrong
    login -- and recorded only once the review list has actually been READ, so
    an unreadable list retries instead of being settled by a failure.
    """
    return f"driftprobe:{head_sha}"


def withdraw_failed_kind(exc: BaseException) -> str:
    """The ping-ledger kind that dedupes a failing review-request withdrawal.

    Retrying a failed DELETE is right -- a 403 can be a permission grant away
    from working. Re-LOGGING it every poll is not: the common failure is
    permanent and per-PR, so at a 30s poll interval one such PR would emit
    ~2,880 warning lines a day that never converge on anything.

    Keyed on the exception class, not its message: a message carries the PR
    and the moment, which would defeat the dedupe, while a 403 that later
    becomes a 422 is a genuinely different condition and says so once.
    """
    return f"withdraw-failed:{type(exc).__name__}"


def withdrawn_kind(head_sha: str) -> str:
    """The ping-ledger kind that marks a review request already withdrawn at
    this head -- so a request that COMES BACK is distinguishable from the first
    one.

    Withdrawing is the daemon undoing a state change a human or an automation
    made on GitHub. Doing it once at a head is routine close-out; doing it
    again means somebody deliberately asked for another look and the daemon
    took it away, which is worth a louder line than the first.
    """
    return f"withdrawn:{head_sha}"


def _now() -> str:
    """The activity comment's timestamp format (UTC, seconds)."""
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def grant_activity_kind(comment_id: int) -> str:
    """The ping-ledger kind that dedupes ONE grant's activity line.

    The grant itself is recorded the moment it is honoured (the effective cap
    must not depend on a comment API call succeeding), but the activity line is
    a separate best-effort append -- so the ledger row lands only AFTER the
    append does, and a failed append is retried on the next poll instead of
    losing the audit line the grant is supposed to leave.
    """
    return f"activity-grant:{comment_id}"


@dataclass(frozen=True)
class Ack:
    """One comment read against the re-entry grammar.

    Three outcomes, deliberately distinguishable: not a directive at all
    (`rounds` and `problem` both None -- an ordinary comment, no log), a
    directive that cannot be honoured (`problem` set -- ignored WITH a log
    line, because a silent typo is indistinguishable from a cap that refuses
    to lift), or a good ack (`rounds` set).
    """

    rounds: int | None = None
    problem: str | None = None

    @property
    def is_directive(self) -> bool:
        return self.rounds is not None or self.problem is not None


def parse_reentry_ack(body: str) -> Ack:
    """Parse a comment body for the operator re-entry directive.

    The grammar is one whole line, `alissa-review: re-enter +N`, optionally
    wrapped in backticks. Everything else about the comment is ignored, so an
    operator may explain themselves above or below the line -- but the line
    itself has to be exact, and a quoted (`>`) line never counts, which is what
    stops a reply that quotes the escalation from re-granting it.

    Two or more directives disagreeing in one comment is a refusal, not a
    guess: "acks are counted, never inferred". Repeating the SAME `+N` is fine
    (it is still one grant, and one comment is one grant however many times it
    says so).
    """
    lead = False
    found: set[int] = set()
    for raw in (body or "").splitlines():
        line = raw.strip()
        if line.startswith(">"):  # a quoted escalation is not a directive
            continue
        if not _ACK_LEAD_RE.match(line):
            continue
        lead = True
        match = _ACK_RE.match(line)
        if match:
            found.add(int(match.group(1)))

    if not found:
        if lead:
            return Ack(
                problem=f"malformed re-entry directive (expected `{REENTRY_GRAMMAR}`)"
            )
        return Ack()
    if len(found) > 1:
        return Ack(
            problem="contradictory re-entry directives in one comment "
            f"(+{', +'.join(str(n) for n in sorted(found))})"
        )
    rounds = found.pop()
    if not 1 <= rounds <= MAX_REENTRY_ROUNDS:
        return Ack(
            problem=f"+{rounds} is outside the re-entry ceiling "
            f"(+1 to +{MAX_REENTRY_ROUNDS})"
        )
    return Ack(rounds=rounds)


def checks_unsettled_kind(round_: int, head_sha: str) -> str:
    """The ping-ledger kind that dedupes ONE degraded round's operator page.

    Per (round, head) like the hold note, and recorded only AFTER the comment
    lands: this page is the operator's only signal that a PR has left the loop
    holding an unsettled rollup, so a transient comment failure must retry
    rather than be swallowed.
    """
    return f"checks-unsettled:{round_}:{head_sha}"


def checks_hold_kind(round_: int, head_sha: str) -> str:
    """The ping-ledger kind that dedupes ONE held round's activity line.

    A hold is re-decided every poll, so an append per decision would grow the
    activity comment by an identical line a minute for as long as CI runs. One
    line per (round, head) says the whole thing -- "this round is waiting on the
    checks at this commit" -- and the head is in the key because a round whose
    verdict is pinned to a different commit is a different wait.

    The issue's contract is at most ONE waiting note per round and no new
    comments; this appends to the existing mechanical activity comment rather
    than posting its own, so a held round costs zero new comments.
    """
    return f"checks-hold:{round_}:{head_sha}"


def verdict_post_kind(round_: int) -> str:
    """The ping-ledger kind that dedupes ONE round's failed-post page.

    Per round, not per PR: round 2 failing to close is a new problem even if
    round 1's page is still unanswered. The row is written only AFTER the
    comment posts, like the stalled ping -- this is the operator's only signal
    that the loop has stopped on this PR, so a transient comment failure must
    retry rather than be swallowed.
    """
    return f"verdict-post-failed:{round_}"


class Action(str, Enum):
    SPAWNED = "spawned"
    IN_FLIGHT = "in-flight"
    CONVERGED = "converged"
    CAPPED = "capped"
    ESCALATED = "escalated"
    SKIPPED = "skipped"
    # The round's verdict just landed as a native reviewer-identity review --
    # the moment the round actually closes.
    POSTED = "posted"
    # The round has a verdict but no native review yet: inside the grace
    # window, or the post failed and is being retried. Either way the round is
    # NOT closed and nothing downstream of it runs.
    AWAITING_POST = "awaiting-post"
    # The round's verdict can never be posted -- the head it judged is gone
    # from the PR. Distinct from AWAITING_POST on purpose: that one retries,
    # this one has given up, and the poll snapshot and console aggregate the
    # action rather than the reason.
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""
    round: int | None = None
    # Descriptive metadata for the poll-snapshot exhaust (see
    # ReviewWatcher._stage_record). Populated where a decision names a concrete
    # reviewer -- the SPAWNED path and the liveness-deferral IN_FLIGHT paths --
    # and left at its default elsewhere. Purely observational: nothing in the
    # decision logic reads these, so they never change which branch is taken.
    # `deferred` marks an IN_FLIGHT that is a liveness deferral (a live session
    # holding the respawn back) rather than a freshly-enqueued round;
    # `reenqueued` marks a SPAWNED that respawned a round whose prior session
    # was presumed dead (the "stale-re-enqueued" summary bucket).
    session: str | None = None
    task_ref: str | None = None
    deferred: bool = False
    reenqueued: bool = False


@dataclass(frozen=True)
class ChecksGate:
    """What the head's CI rollup does to a round's APPROVE verdict.

    Three shapes, matching the three things the gate can decide:

    * `hold` set -- do not post at all this poll (the rollup has not settled and
      the wait bound has not run out). The round stays open, exactly as it does
      inside the pre-post grace window.
    * `event` changed -- post, but not as an approve: REQUEST_CHANGES on a red
      head, COMMENT when the rollup never settled. `lead` is prepended to the
      verdict body and names the checks.
    * neither -- green (or nothing to gate): the post proceeds unchanged.
    """

    hold: Decision | None = None
    event: str = EVENT_APPROVE
    lead: str = ""
    # The rollup state that produced this decision, for the log/activity line.
    state: str = CHECKS_GREEN
    # The human-readable "why it did not settle", reused verbatim in the
    # operator page that follows a degraded verdict (see _page_unsettled_checks)
    # so the page and the review record cannot drift apart.
    detail: str = ""


def session_name(pr: PullRequest, round_: int) -> str:
    """A tmux-safe reviewer session name, unique per spawn.

    The `review-<repo>-pr<n>-r<round>` prefix stays human-readable, but a short
    random nonce is appended so a re-used or miscounted round number can never
    collide with a still-live session (a collision wedges the worker -- the
    original 'stuck' failure). Safe to be non-deterministic: the generated name
    is recorded in the spawn ledger and is what gets reaped / self-killed, so the
    daemon never re-derives it.

    The shape is a contract, not just a convention: alissa.parse_session_name
    matches it, and only names it matches are ever reap candidates. Both sides
    build the repo component through `session_repo_slug` so they cannot drift.
    """
    return (
        f"review-{session_repo_slug(pr.repo)}-pr{pr.number}"
        f"-r{round_}-{secrets.token_hex(3)}"
    )


class ReviewWatcher:
    def __init__(
        self,
        config: Config,
        github: GitHub | None = None,
        alissa: Alissa | None = None,
        state: State | None = None,
    ):
        self.config = config
        self.github = github or GitHub(
            config.reviewer_login, token_env=config.reviewer_token_env
        )
        self.alissa = alissa or Alissa()
        self.state = state or State(config.state_db)
        # (repo, number, comment id) of every re-entry directive already
        # refused in this process -- see _log_ignored_ack.
        self._ignored_acks: set[tuple[str, int, int]] = set()
        # Reviewer session name -> the (repo slug, number) its NAME resolved
        # to, or None for "could not be resolved". Process-lifetime, pruned
        # against the live list each sweep; see _resolve_pr for why the probe
        # behind it must not be paid per poll.
        self._probe_cache: dict[str, tuple[str, int] | None] = {}
        # The surviving-session set the cap alarm last paged on, so a standing
        # over-cap condition pages once per episode -- see _check_session_cap.
        self._paged_cap: frozenset[str] | None = None
        # (repo slug, number, drift kind) already announced by THIS process in
        # dry-run. Deliberately in-memory: a dry-run pass must never be
        # suppressed by anything durable (a production pass writing the ledger
        # would silence the operator's diagnostic), but a daemon left in
        # dry-run should not re-announce the same drift every poll either --
        # see _warn_identity_drift.
        self._dry_run_drift: set[tuple[str, int, str]] = set()
        # (repo slug, number, round, judged head) -> the rollup summary this
        # process already read for it in DRY-RUN. The dry-run branch returns
        # before any ledger write, so without this the diagnostic re-reads the
        # rollup (two API calls) on every poll, forever, for every PR with an
        # owed approve. In-memory for the same reason _dry_run_drift is.
        self._dry_run_rollups: dict[tuple[str, int, int, str], str] = {}

    # -- per-PR decision ---------------------------------------------------

    def evaluate(self, owner: str, repo: str, number: int) -> Decision:
        pr = self.github.pull_request(owner, repo, number)

        # CR1: draft PRs are never reviewed. The search already filters these;
        # this catches a flip back to draft between search and fetch.
        if pr.draft:
            return Decision(Action.SKIPPED, "PR is a draft (CR1)")

        if pr.author == self.github.login:
            # GitHub rejects a self review-request, so this should be
            # unreachable -- but a shared bot identity would land here.
            return Decision(
                Action.SKIPPED,
                f"PR author is the reviewer identity ({pr.author}); "
                "GitHub forbids self-review",
            )

        my_reviews = self.github.my_reviews(owner, repo, number)

        # The review task (CR2) is the round record: one verdict envelope per
        # round (CR7), so counting envelopes is the authoritative "rounds
        # completed" -- immune to the GitHub heuristics that drift. A round whose
        # review has an empty body undercounts (round_ repeats -> the session name
        # collides -> the worker wedges); two reviews in one cycle overcount.
        # Fall back to the substantive-review count only before the review task
        # exists (round 1). Looked up here (not in _spawn) because both the count
        # and convergence need it.
        task = self.alissa.find_review_task(owner, repo, number)
        native = countable_rounds(my_reviews)
        completed = (
            self.alissa.count_verdicts(task.ref) if task is not None else native
        )

        # A round is not over until its verdict exists as a native review by
        # the reviewer identity (issue #51). An envelope ahead of the native
        # count is exactly that gap, and it is checked BEFORE convergence on
        # purpose: an approve envelope with no native APPROVE behind it would
        # otherwise close the loop with no verdict of record on GitHub and the
        # review request still dangling -- the studio #298 failure.
        # ...discounting rounds whose post was ABANDONED, which happens only
        # when the head the round judged is gone from the PR. There is then no
        # commit left to record that verdict against, and holding the round
        # open would strand the PR outside the loop forever; the round is
        # released instead and a fresh one is owed against the new head (see
        # _abandon_verdict).
        #
        # The discount is a SUBTRACTION, not just a check on the newest round.
        # An abandoned round's envelope stays on the task forever while its
        # review record never exists, so it leaves a permanent hole between the
        # two counts -- and an uncorrected hole reads, on the NEXT round, as
        # "that round has no native verdict", producing a duplicate post over a
        # session that closed its own round correctly.
        owed = completed - self.state.abandoned_rounds(pr.full_name, number)
        if task is not None and owed > native:
            # Terminal for this pass either way. On a landed post the review
            # request it consumed drops the PR out of the search, so
            # convergence and the next round belong to the next pass, decided
            # from GitHub rather than from this pass's now-stale counts; on
            # anything else the round is still open and nothing may follow it.
            return self._close_round_natively(pr, task, round_=completed)

        converged = self._convergence_reason(my_reviews, task, pr.head_sha)
        if converged is not None:
            # THE terminal branch: a verdict of record exists at the current
            # head, so no round k+1 can be owed from here -- every path that
            # could open one (head moved, non-approve verdict, cap re-entry
            # ack) is either upstream of this return or unreachable past it.
            # That is what makes it the only safe place to withdraw a dangling
            # self review request; see _clear_own_review_request.
            self._clear_own_review_request(pr, my_reviews, converged)
            return Decision(Action.CONVERGED, converged, completed)

        # The effective cap is the configured one plus every re-entry an
        # operator has explicitly acked on THIS PR (issue #42). The sum is a
        # local read, so it costs nothing on the common path; the GitHub scan
        # that can DISCOVER a new ack runs only when the loop would otherwise
        # be capped -- exactly the state an ack exists to unstick, and the only
        # one where an extra comments fetch per poll is worth paying for.
        granted = self.state.granted_rounds(pr.full_name, number)
        if completed >= self.config.round_cap + granted:
            granted = self._collect_acks(pr, granted)
            self._announce_grants(pr)
        cap = self.config.round_cap + granted

        # CR9: never queue round cap+1 -- where "cap" is now the effective one.
        # No ack, no rounds: with no grant this is byte-for-byte the old
        # behaviour.
        if completed >= cap:
            if not self._escalation_owed(pr, granted):
                return Decision(Action.CAPPED, "already escalated", completed)
            grant = self.state.newest_grant(pr.full_name, number)
            self._escalate(pr, my_reviews, completed, cap, grant)
            return Decision(Action.ESCALATED, f"{completed} rounds, no approve", completed)

        round_ = completed + 1

        age = self.state.spawn_age(pr.full_name, number, round_)
        if age is not None and age < STALE_ROUND_SECONDS:
            return Decision(Action.IN_FLIGHT, f"round {round_} enqueued {int(age)}s ago", round_)
        if age is not None:
            deferred = self._defer_stale_round(pr, round_, age, cap)
            if deferred is not None:
                return deferred
            log.warning(
                "%s round %d has been in flight %.0f min with no submitted review "
                "and its session is gone or finished — re-enqueuing (reviewer "
                "session presumed dead)",
                pr.slug,
                round_,
                age / 60,
            )

        return self._spawn(pr, round_, task, cap, reenqueued=age is not None)

    # -- native verdict post -----------------------------------------------

    def _close_round_natively(
        self, pr: PullRequest, task: Task, round_: int
    ) -> Decision:
        """Land round `round_`'s verdict as a native reviewer-identity review.

        Called when the review task carries more verdict envelopes than the PR
        has countable reviewer reviews -- i.e. a round produced a verdict that
        never became a review of record. Three outcomes, and none of them
        closes the round unless a review actually lands:

        * inside the grace window -> AWAITING_POST, so a reviewer session that
          is seconds from submitting its own review is not raced;
        * post succeeds -> POSTED, and the pending review request GitHub was
          holding is consumed by that very submission;
        * post fails -> AWAITING_POST, retried next poll, and paged to an
          operator once the attempts pass MAX_VERDICT_POST_ATTEMPTS.

        Never raises past RateLimited: a broken post must stall this PR, not
        the whole poll pass.
        """
        verdict = self.alissa.latest_verdict(task.ref)
        if verdict not in (VERDICT_APPROVE, VERDICT_REQUEST_CHANGES):
            # count_verdicts and latest_verdict read the same envelopes with
            # the same pattern, so this is nearly unreachable -- but "I know a
            # round finished and cannot tell you its verdict" must never be
            # resolved by guessing one onto the PR.
            log.error(
                "%s round %d has a verdict envelope on %s that does not parse "
                "to a verdict (%r) — cannot post it; the round stays open",
                pr.slug, round_, task.ref, verdict,
            )
            return Decision(
                Action.AWAITING_POST,
                f"round {round_}'s envelope on {task.ref} has no readable verdict",
                round_,
                task_ref=task.ref,
            )

        if self.config.dry_run:
            # The rollup is READ here even in dry-run (a read takes nothing on
            # GitHub and writes no ledger row), because "would this approve have
            # been gated?" is exactly the question an operator reaches for
            # dry-run to answer -- the same argument the identity-drift check
            # makes for observing rather than staying silent.
            # ...but ONCE per round per head, not every poll. A dry-run pass
            # returns before the ledger bookkeeping (deliberately: it must write
            # no durable state), so the dedupe is in-memory and
            # process-lifetime, exactly as _dry_run_drift is and for the same
            # reason -- a dry-run observation must never be silenced by
            # something a production pass could have written, or the other way
            # round.
            checks = ""
            if verdict == VERDICT_APPROVE:
                judged = self._judged_head(pr, round_)
                seen = (pr.full_name, pr.number, round_, judged)
                if seen not in self._dry_run_rollups:
                    rollup = self.github.check_rollup(pr.owner, pr.repo, judged)
                    self._dry_run_rollups[seen] = rollup.summary
                checks = (
                    f"; CI rollup at the judged head is "
                    f"{self._dry_run_rollups[seen]}"
                )
            log.info(
                "[dry-run] would submit a native %s review for round %d of %s%s",
                verdict, round_, pr.slug, checks,
            )
            return Decision(
                Action.AWAITING_POST,
                f"[dry-run] round {round_} would be closed with a native "
                f"{verdict} review{checks}",
                round_,
                task_ref=task.ref,
            )

        row = self.state.note_verdict_post_owed(
            pr.full_name, pr.number, round_, self._judged_head(pr, round_)
        )
        attempts = int(row["attempts"])
        # `left` decides; `reason` only ever reaches a caller when left > 0.
        if attempts == 0:
            # The grace window: measured from the first observation, because it
            # is about the round's own session getting its chance, not about
            # retrying.
            left = VERDICT_POST_GRACE_SECONDS - (time.time() - int(row["first_seen_at"]))
            reason = f"{int(left)}s of grace left for its own session to submit one"
        else:
            # Every later attempt is spaced from the PREVIOUS one; see
            # _post_delay_after for why not from a fixed origin.
            since = time.time() - int(row["last_attempt_at"] or row["first_seen_at"])
            left = _post_delay_after(attempts) - since
            reason = (
                f"backing off after {attempts} failed attempt(s), "
                f"retrying in {int(left)}s"
            )
        if left > 0:
            return Decision(
                Action.AWAITING_POST,
                f"round {round_} has a {verdict} envelope but no native review "
                f"yet — {reason}",
                round_,
                task_ref=task.ref,
            )

        judged = str(row["head_sha"] or pr.head_sha)
        moved = bool(judged) and judged != pr.head_sha
        event = EVENT_APPROVE if verdict == VERDICT_APPROVE else EVENT_REQUEST_CHANGES

        # THE CI GATE (issue #58). Only an APPROVE is gated: a REQUEST_CHANGES on
        # a red head is already a "not ready" signal and says nothing that CI
        # could contradict.
        gate = ChecksGate()
        if event == EVENT_APPROVE:
            gate = self._gate_on_checks(pr, task, round_, judged)
            if gate.hold is not None:
                return gate.hold
            event = gate.event

        body = gate.lead + NATIVE_VERDICT_BODY.format(
            round=round_,
            verdict=verdict,
            task_note=_VERDICT_TASK_NOTE.format(task_ref=task.ref),
            head_note=(
                HEAD_MOVED_NOTE.format(judged=judged[:8], head=pr.head_sha[:8])
                if moved
                else ""
            ),
            marker=verdict_marker(round_),
        )
        try:
            url = self.github.submit_review(
                pr.owner, pr.repo, pr.number,
                event=event, body=body, commit_id=judged,
            )
        except RateLimited:
            # run_forever's backoff is the whole response to a rate limit.
            raise
        except Exception as exc:
            # Broad on purpose. The named failures -- CommandError (gh said
            # no), IdentityMismatch (the credential is the wrong identity),
            # ReviewerTokenUnset (it is missing entirely) -- are the expected
            # ones, and every one of them means the same thing: the round did
            # not close. An unexpected failure means that too, and must not
            # take down a poll pass that has other PRs to decide.
            return self._verdict_post_failed(pr, task, round_, verdict, judged, exc)

        # Named in all three records when the gate downgraded the verdict, so
        # "the envelope says approve but GitHub says otherwise" is never a
        # mystery in the log, the activity comment, or the poll snapshot.
        gate_note = (
            ""
            if gate.state == CHECKS_GREEN
            else f" — CI gate: the rollup at {judged[:8]} is {gate.state}, so the "
            f"{verdict} envelope did not post as an APPROVE"
        )
        self.state.record_verdict_post(pr.full_name, pr.number, round_, url)
        log.info(
            "%s round %d closed: native %s review submitted as %s (%s)%s",
            pr.slug, round_, event, self.github.login, url or "no url", gate_note,
        )
        self._append_activity(
            pr,
            f"- {_now()} — round {round_} — native `{event}` review submitted "
            f"as `{self.github.login}` (verdict of record){gate_note}",
        )
        if event == EVENT_COMMENT:
            # A degraded verdict takes the PR out of the loop (the review
            # request is consumed) without leaving a signal anything acts on.
            self._page_unsettled_checks(pr, round_, judged, verdict, url, gate.detail)
        return Decision(
            Action.POSTED,
            f"round {round_} closed with a native {event} review as "
            f"{self.github.login}{gate_note}",
            round_,
            task_ref=task.ref,
        )

    def _gate_on_checks(
        self, pr: PullRequest, task: Task, round_: int, judged: str
    ) -> ChecksGate:
        """Decide what the CI rollup at `judged` does to this round's approve.

        The rule the gate exists to keep: an APPROVE by the reviewer identity
        means *reviewed AND green*, because it is the operator's cue to merge.
        On studio #323 a round approved 3.4 hours after the head's `test` check
        had gone red, the operator's merge gate received an "approved, ready" PR
        that was unmergeable-red, and the failure sat unaddressed until a human
        noticed (issue #58).

        Read on the head the verdict is PINNED to, never the PR's current head:
        approving commit A on commit B's rollup is the same class of error as
        stamping an old verdict onto a new head (see _judged_head). When the two
        differ the existing head-moved handling still does its job -- the review
        is recorded against `judged` and does not converge the loop -- and this
        gate additionally refuses to call that commit green on someone else's
        checks.

        What it never does: touch labels. `alissa:maintain` and every other
        cross-daemon trigger stays an operator/devloop concern; revloop shapes
        its own verdict and nothing else. Nor does it post a comment of its own
        while waiting -- the wait note goes into the existing mechanical activity
        comment, once per held round (checks_hold_kind).
        """
        rollup = self.github.check_rollup(pr.owner, pr.repo, judged)

        if rollup.state == CHECKS_GREEN:
            log.debug(
                "%s round %d: CI rollup at %s is %s — approving as usual",
                pr.slug, round_, judged[:8], rollup.summary,
            )
            return ChecksGate()

        if rollup.state == CHECKS_RED:
            log.warning(
                "%s round %d: NOT approving %s — its CI rollup is %s. The "
                "%s envelope lands as a %s review instead",
                pr.slug, round_, judged[:8], rollup.summary,
                VERDICT_APPROVE, EVENT_REQUEST_CHANGES,
            )
            failing = "\n".join(
                CHECKS_FAILING_LINE.format(
                    name=context.name,
                    conclusion=context.conclusion or "not concluded",
                    url=f" — {context.url}" if context.url else "",
                )
                for context in rollup.failing
            )
            return ChecksGate(
                event=EVENT_REQUEST_CHANGES,
                lead=CHECKS_RED_LEAD.format(
                    sha=judged[:8], failing=failing, verdict=VERDICT_APPROVE
                ),
                state=CHECKS_RED,
            )

        # PENDING (checks still running) or UNKNOWN (the rollup could not be
        # read) -- neither can support an approve, and both can settle, so both
        # HOLD the round first and only degrade at the bound.
        #
        # The clock is stamped against the CONDITION being waited on, and gets
        # exactly one restart: when an `unknown` hold is promoted to a `pending`
        # one. A transient read failure is not an observation of checks running,
        # so it must not eat the bound a real suite is entitled to. One restart,
        # never "restart whenever the state changes" -- a reader flapping between
        # the two would then push the bound out forever, which is precisely the
        # unbounded hold this bound exists to prevent.
        hold = self.state.checks_hold(pr.full_name, pr.number, round_)
        promoted = hold.condition == CHECKS_UNKNOWN and rollup.state == CHECKS_PENDING
        if hold.since is None or promoted:
            self.state.record_checks_hold(
                pr.full_name, pr.number, round_, rollup.state
            )
            hold = self.state.checks_hold(pr.full_name, pr.number, round_)
        # Two numbers, both reported: `waited` is the wait THIS condition has had
        # and is what the bound applies to; `held` is how long the round has been
        # held at all. They differ by up to a full bound once a hold has been
        # promoted, so a report that shows only the first tells an operator who
        # set 30 minutes that a 60-minute hold waited 30.
        waited = max(time.time() - (hold.since or time.time()), 0)
        held = max(time.time() - (hold.first_at or time.time()), 0)
        bound = self.config.checks_wait_seconds
        if waited < bound:
            # One line per poll, one activity note per held round; see
            # checks_hold_kind.
            log.info(
                "%s round %d: holding its %s — CI rollup at %s is %s (%dm on this "
                "condition of the %dm bound; %dm held in total)",
                pr.slug, round_, VERDICT_APPROVE, judged[:8], rollup.summary,
                waited // 60, bound // 60, held // 60,
            )
            self._note_checks_hold(pr, round_, judged, rollup)
            return ChecksGate(
                hold=Decision(
                    Action.AWAITING_POST,
                    f"round {round_} holds its {VERDICT_APPROVE} — the CI rollup "
                    f"at {judged[:8]} is {rollup.summary}; "
                    f"{int((bound - waited) // 60)}m of the wait bound left "
                    f"({int(held // 60)}m held in total)",
                    round_,
                    task_ref=task.ref,
                ),
                state=rollup.state,
            )

        log.warning(
            "%s round %d: the CI rollup at %s is still %s after %dm on this "
            "condition (%dm held in total) — recording the %s envelope as a %s "
            "review, never an APPROVE on an unverified head",
            pr.slug, round_, judged[:8], rollup.summary, waited // 60, held // 60,
            VERDICT_APPROVE, EVENT_COMMENT,
        )
        detail = (
            CHECKS_STILL_RUNNING.format(
                names=", ".join(f"`{c.name}`" for c in rollup.running) or "none"
            )
            if rollup.state == CHECKS_PENDING
            else CHECKS_UNREADABLE.format(why=rollup.unreadable or "no reason recorded")
        )
        return ChecksGate(
            event=EVENT_COMMENT,
            lead=CHECKS_UNSETTLED_LEAD.format(
                sha=judged[:8],
                detail=detail,
                waited=int(waited // 60),
                bound=int(bound // 60),
                total_note=(
                    CHECKS_TOTAL_HELD.format(total=int(held // 60))
                    if hold.promoted
                    else ""
                ),
            ),
            state=rollup.state,
            detail=detail,
        )

    def _page_unsettled_checks(
        self,
        pr: PullRequest,
        round_: int,
        judged: str,
        verdict: str,
        url: str,
        detail: str,
    ) -> None:
        """Page the operator ONCE for a round that degraded to a comment.

        See CHECKS_UNSETTLED_PAGE for why this is owed: the degraded verdict
        consumes the review request, so the PR silently leaves the poll set with
        nothing downstream of it. The ledger row lands only AFTER the comment
        does, as with every ping here -- but note this one may not get a second
        chance, because the very post that made it necessary is what takes the
        PR out of the search. So a failure logs at ERROR (with the body, which is
        then the only place the diagnosis exists) rather than at warning.
        """
        kind = checks_unsettled_kind(round_, judged)
        if self.state.pinged(pr.full_name, pr.number, kind):
            return
        body = CHECKS_UNSETTLED_PAGE.format(
            round=round_,
            sha=judged[:8],
            detail=detail,
            url=url or "no url recorded",
            verdict=verdict,
            reviewer=self.github.login,
        )
        try:
            self.github.comment(pr.owner, pr.repo, pr.number, body)
        except Exception as exc:
            log.error(
                "%s round %d: could not post the unsettled-checks page (%s). The "
                "degraded verdict consumed the review request, so this PR has "
                "left the poll set and the page may never be retried — it needed "
                "to say:\n%s",
                pr.slug, round_, exc, body,
            )
            return
        self.state.record_ping(pr.full_name, pr.number, kind)

    def _note_checks_hold(
        self, pr: PullRequest, round_: int, judged: str, rollup: CheckRollup
    ) -> None:
        """Append ONE activity line for a round held on its checks.

        Ledger row after the append, like every other activity note here, so a
        transient comment failure retries next poll instead of losing the line.
        """
        kind = checks_hold_kind(round_, judged)
        if self.state.pinged(pr.full_name, pr.number, kind):
            return
        if not self._append_activity(
            pr,
            f"- {_now()} — round {round_} — verdict held: the CI rollup at "
            f"`{judged[:8]}` is {rollup.summary}. An approve has to mean "
            f"reviewed AND green, so the round stays open "
            f"(bound: {self.config.checks_wait_seconds // 60}m).",
        ):
            return
        self.state.record_ping(pr.full_name, pr.number, kind)

    def _judged_head(self, pr: PullRequest, round_: int) -> str:
        """The head round `round_`'s verdict is ABOUT — not the current one.

        A review carries the commit it judged, and `_convergence_reason` reads
        that commit to decide whether an approval still covers the code. Post
        with the head at POST time and an old approve is restamped onto commits
        no reviewer has seen, and the next pass converges on it: the #227 latch,
        rebuilt inside the daemon's own post. The gap is not small -- it spans
        an implementer triaging visible findings and pushing, a daemon restart,
        or a rate-limit backoff.

        The spawn ledger is the answer: its row for this round records the head
        the round was QUEUED against. That is deliberately the conservative
        choice over the current head. If a session actually reviewed a newer
        head pushed mid-round, attributing to the queued head under-claims --
        convergence is missed and one extra round runs. Over-claiming produces
        a false approve. Only one of those is recoverable.

        Falls back to the current head when there is no ledger row at all (a
        hand-spawned round, or one whose ledger was lost); that is no worse
        than the behaviour without a ledger, and the caller records whatever
        this returns so the answer cannot drift between polls.
        """
        row = self.state.get_spawn(pr.full_name, pr.number, round_)
        if row is not None and row["head_sha"]:
            return str(row["head_sha"])
        return pr.head_sha

    def _abandon_verdict(
        self,
        pr: PullRequest,
        task: Task,
        round_: int,
        judged: str,
        exc: BaseException,
    ) -> Decision | None:
        """Give up on a post that can never succeed, or None to keep retrying.

        Pinning the verdict to the head its round judged (the fix for the
        stale-approve latch) makes one new failure possible: a rebase or an
        amended force-push removes that commit from the PR, GitHub rejects the
        review, and no amount of retrying will change that. Without an exit the
        round stays open forever -- which means round k+1 is never spawned and
        the PR leaves the loop until a human edits sqlite. That is a worse
        outcome than any verdict-of-record concern this path exists to serve.

        The exit is to ABANDON the owed post, never to fall back to the current
        head: the verdict judged code that no longer exists in the PR, so
        restamping it forward is exactly the latch that was just removed.
        Abandoning releases the round, and the next pass spawns a fresh one
        against the new head -- the same place the head-moved path lands, minus
        the unpostable POST.

        Detection does not trust the error string. A rejection that mentions
        the commit only *starts* the question; the answer comes from reading
        the PR's commits and finding the judged SHA genuinely absent. Any other
        422, an unreadable commit list, or a listing too long to prove absence
        from all stay on the ordinary retry path -- the conservative direction,
        since abandoning wrongly discards a real verdict.
        """
        blob = str(exc).lower()
        if not ("422" in blob or "commit_id" in blob or "unprocessable" in blob):
            return None
        if not judged:
            return None
        try:
            commits = self.github.pull_request_commits(pr.owner, pr.repo, pr.number)
        except RateLimited:
            raise
        except Exception as probe_exc:
            log.warning(
                "%s round %d: the review POST was rejected and the PR's commit "
                "list is unreadable (%s) — retrying rather than assuming the "
                "judged head is gone",
                pr.slug, round_, probe_exc,
            )
            return None
        if judged in commits:
            return None  # the pin is fine; the 422 is about something else

        why = (
            f"the head this round judged ({judged[:8]}) is no longer a commit of "
            f"the PR — a force-push landed under it"
        )
        self.state.record_verdict_post_abandoned(pr.full_name, pr.number, round_, why)
        log.error(
            "%s round %d: ABANDONING its native verdict — %s. The round is "
            "released so a fresh round can run against %s; the verdict stays on "
            "%s as the record of what was judged.",
            pr.slug, round_, why, pr.head_sha[:8], task.ref,
        )
        self._append_activity(
            pr,
            f"- {_now()} — round {round_} — native verdict abandoned: {why}. "
            f"A fresh round is owed against `{pr.head_sha[:8]}`.",
        )
        return Decision(
            Action.ABANDONED,
            f"round {round_}'s native verdict was abandoned — {why}",
            round_,
            task_ref=task.ref,
        )

    def _verdict_post_failed(
        self,
        pr: PullRequest,
        task: Task,
        round_: int,
        verdict: str,
        judged: str,
        exc: BaseException,
    ) -> Decision:
        """Count a failed post, page once it stops looking transient, and keep
        the round OPEN. The page is the point: a silently missing native
        verdict is the exact defect this whole path exists to remove, so it
        must not degrade into a quiet retry loop.

        One failure is NOT retryable and gets its own exit: the commit the
        verdict was pinned to is no longer in the PR. See _abandon_verdict.
        """
        attempts = self.state.record_verdict_post_failure(
            pr.full_name, pr.number, round_, str(exc)
        )
        abandoned = self._abandon_verdict(pr, task, round_, judged, exc)
        if abandoned is not None:
            return abandoned
        log.error(
            "%s round %d: native %s review FAILED (attempt %d) — %s; the round "
            "stays open and the post retries next poll",
            pr.slug, round_, verdict, attempts, exc,
        )
        if attempts >= MAX_VERDICT_POST_ATTEMPTS and not self.state.pinged(
            pr.full_name, pr.number, verdict_post_kind(round_)
        ):
            body = VERDICT_POST_FAILED_COMMENT.format(
                round=round_,
                verdict=verdict,
                attempts=attempts,
                reviewer=self.github.login,
                error=str(exc)[:500],
            )
            try:
                self.github.comment(pr.owner, pr.repo, pr.number, body)
            except Exception as comment_exc:  # pragma: no cover - defence in depth
                log.error(
                    "could not post the failed-verdict page on %s: %s — the ping "
                    "retries next poll",
                    pr.slug, comment_exc,
                )
            else:
                self.state.record_ping(
                    pr.full_name, pr.number, verdict_post_kind(round_)
                )
        return Decision(
            Action.AWAITING_POST,
            f"round {round_}'s native {verdict} review failed to submit "
            f"(attempt {attempts}) — the round is NOT closed",
            round_,
            task_ref=task.ref,
        )

    # -- operator re-entry -------------------------------------------------

    def _is_operator(self, author: str) -> bool:
        """Whether an ack from this author may be honoured.

        Allowlist membership, case-insensitively (GitHub logins are), minus the
        reviewer identity itself: the daemon's own escalation comment carries
        the grammar, so an identity that could ack its own page would be able
        to lift CR9's cap without a human ever touching it.

        The PR author is deliberately NOT excluded the same way. Refusing them
        outright would block the ordinary case -- an operator who opened the PR
        by hand -- and the protection that matters is structural rather than
        per-identity: the allowlist is opt-in and empty by default, so putting
        an AGENT identity on it (an implementer that can comment on its own PR
        and has read the escalation) is an operator's deliberate choice, not
        something the daemon does for them. The reviewer identity is different
        in kind because it is not opt-in at all.
        """
        if not author or author.lower() == (self.github.login or "").lower():
            return False
        return author.lower() in {o.lower() for o in self.config.operators}

    def _collect_acks(self, pr: PullRequest, granted: int) -> int:
        """Scan the capped PR's comments for operator acks; record new grants.

        Returns the PR's total granted rounds. Every honoured ack is recorded
        under its comment id, so the same comment can never grant twice however
        many polls read it, and the log/announce side effects fire once.

        With no operator allowlist -- the default, and what the container bakes
        on purpose -- NO ack can ever be honoured, so the scan does not run at
        all: a capped PR keeps its review request pending and therefore stays
        in the search result set indefinitely, which would make this a full
        comment-thread fetch (up to COMMENT_PAGE_LIMIT paged requests, on
        exactly the long threads paging exists for) every poll, forever, to
        discard everything it found. The fail-closed default stays free.

        Comment reads are best-effort: an unreadable comment list leaves the PR
        capped for this pass (the conservative direction -- it can only ever
        withhold rounds, never invent them) and the scan retries next poll.
        """
        if not self.config.operators:
            return granted

        try:
            comments = self.github.issue_comments(pr.owner, pr.repo, pr.number)
        except RateLimited:
            # Not swallowed: run_forever's backoff is the whole response to a
            # rate limit, and eating it here would keep the pass hammering.
            raise
        except Exception as exc:
            log.warning(
                "%s is at its cap but its comments are unreadable (%s) — no "
                "re-entry ack can be honoured this pass; retrying next poll",
                pr.slug,
                exc,
            )
            return granted

        for comment in comments:
            # Our own comments are never directives -- the escalation itself
            # quotes the grammar.
            if comment.author == self.github.login:
                continue
            ack = parse_reentry_ack(comment.body)
            if not ack.is_directive:
                continue
            if not self._is_operator(comment.author):
                self._log_ignored_ack(
                    pr, comment, f"{comment.author} is not an allowlisted operator"
                )
                continue
            if ack.rounds is None:
                self._log_ignored_ack(pr, comment, ack.problem or "malformed directive")
                continue
            if self.config.dry_run:
                log.info(
                    "[dry-run] would honour re-entry ack +%d from %s on %s "
                    "(comment %d)",
                    ack.rounds, comment.author, pr.slug, comment.id,
                )
                granted += ack.rounds
                continue
            if not self.state.record_grant(
                pr.full_name, pr.number, comment.id, comment.author, ack.rounds
            ):
                continue  # already counted in `granted`
            granted += ack.rounds
            log.warning(
                "RE-ENTRY GRANT %s — operator %s acked +%d round(s) in comment "
                "%d; effective cap %d → %d",
                pr.slug,
                comment.author,
                ack.rounds,
                comment.id,
                self.config.round_cap + granted - ack.rounds,
                self.config.round_cap + granted,
            )
        return granted

    def _log_ignored_ack(
        self, pr: PullRequest, comment: IssueComment, why: str
    ) -> None:
        """Report an ack that will not be honoured — once per comment.

        A capped PR is re-scanned every poll, so an unconditional warning would
        repeat a line a minute for as long as the PR stays capped. The comment
        id is the identity of the thing being refused, and an in-memory set is
        the right scope for it: this is a log line, not a delivery guarantee,
        so a daemon restart re-stating the refusal once is fine (and a ledger
        row per typo is not).
        """
        key = (pr.full_name, pr.number, comment.id)
        if key in self._ignored_acks:
            return
        self._ignored_acks.add(key)
        log.warning(
            "ignoring re-entry directive on %s (comment %d by %s): %s — the PR "
            "stays capped",
            pr.slug,
            comment.id,
            comment.author,
            why,
        )

    def _announce_grants(self, pr: PullRequest) -> None:
        """Append the activity line of every grant that has not had one yet.

        Separate from recording the grant on purpose: the cap change is
        authoritative state and must not hinge on a comment API call, while the
        audit line must survive one failing -- so it is retried here on later
        polls until it lands (see grant_activity_kind).

        Walked OLDEST first (read_grants is newest-first, like its siblings)
        with a running total, so each line reports its OWN transition rather
        than the PR's total: two acks read in one pass -- or, more commonly, a
        retried append landing beside a newer grant, which is the whole reason
        this retries -- would otherwise both claim the same before/after cap
        and go out in reverse order into an append-only log.
        """
        running = self.config.round_cap
        for row in reversed(self.state.read_grants(pr.full_name, pr.number)):
            before, running = running, running + row["rounds"]
            kind = grant_activity_kind(row["comment_id"])
            if self.state.pinged(pr.full_name, pr.number, kind):
                continue
            line = (
                f"- {_now()} — operator `{row['author']}` — re-entry ack "
                f"(comment {row['comment_id']}) — +{row['rounds']} round(s) — "
                f"effective cap {before} → {running}"
            )
            if self._append_activity(pr, line):
                self.state.record_ping(pr.full_name, pr.number, kind)

    def _escalation_owed(self, pr: PullRequest, granted: int) -> bool:
        """Whether a cap-out page is owed, or already delivered.

        Two ways a page is owed: this head has never been paged (a fresh
        cap-out, or the implementer pushed since -- the decision is about the
        new state), or rounds granted by an operator ack have since been
        consumed without an approve, which is a new decision on an unmoved head
        (capout_kind). Everything else is the same page the operator already
        has, and CR9's escalation stays once-only.

        A PR with no grant takes the first branch alone, so the pre-#42
        behaviour -- and any state.db written by it -- is untouched: no
        already-escalated PR re-pages just because the daemon was upgraded.
        """
        if not self.state.escalated(pr.full_name, pr.number, pr.head_sha):
            return True
        if granted == 0:
            return False
        return not self.state.pinged(
            pr.full_name, pr.number, capout_kind(pr.head_sha, granted)
        )

    def _defer_stale_round(
        self, pr: PullRequest, round_: int, age: float, cap: int
    ) -> Decision | None:
        """The liveness signal under the stale timer: a deferral, or None to
        respawn.

        Staleness needs TWO signals, not one: the ledger timer says the
        newest spawn is old, but elapsed time alone cannot tell a dead
        session from a slow one -- a thorough round can outlast
        STALE_ROUND_SECONDS, and a timer-only re-enqueue respawns a reviewer
        over the still-working first one; two sessions review the same
        round and both submit. So before respawning, consult external
        evidence of life: the round's recorded session in the live list
        (the reap sweep's own probe). Busy, or idle without a real quiet
        period (mid-close-out between turns; the same `reap_grace_seconds`
        the sweep waits out, because it is the same question) -> the
        round is alive, defer with a reason. Gone, or idle-finished -> dead,
        respawn (the sweep separately handles any corpse). An unprobeable
        live list defers too: respawning on missing evidence is exactly the
        double-spend, and the probe retries next poll.

        The deferral is floored, not unbounded: past STALLED_DEFER_MULTIPLE
        stale windows with the session still alive, one operator ping per
        deferral episode (stalled_kind) -- then keep deferring. This method
        never respawns over a live session; only the operator killing the
        session (or it finishing/dying) unblocks the respawn.
        """
        row = self.state.get_spawn(pr.full_name, pr.number, round_)
        if row is None:  # age came from this row; belt and braces
            return None
        session = row["session"]
        try:
            live = {s.name: s for s in self.alissa.list_review_sessions()}
        except CommandError as exc:
            log.warning(
                "%s round %d is stale but the session list is unavailable (%s) "
                "— deferring the respawn rather than risking a double-spawned "
                "round; the probe retries next poll",
                pr.slug,
                round_,
                exc,
            )
            return Decision(
                Action.IN_FLIGHT,
                f"round {round_} is stale but liveness is unprobeable — deferring",
                round_,
                session=session,
                deferred=True,
            )

        ses = live.get(session)
        if ses is None:
            return None  # session gone -> presumed dead -> respawn
        quiet_for = time.time() - ses.last_activity
        if ses.is_idle and quiet_for >= self.config.reap_grace_seconds:
            return None  # idle-finished: it died without submitting -> respawn

        if (
            age >= STALLED_DEFER_MULTIPLE * STALE_ROUND_SECONDS
            and not self.state.pinged(pr.full_name, pr.number, stalled_kind(session))
        ):
            self._escalate_stalled(pr, round_, session, age)

        life = "active" if ses.is_idle else "busy"
        if not self.state.pinged(pr.full_name, pr.number, deferral_activity_kind(session)):
            appended = self._append_activity(
                pr,
                self._activity_line(
                    session, round_, f"deferred — session `{session}` still {life}", cap
                ),
            )
            if appended:
                self.state.record_ping(
                    pr.full_name, pr.number, deferral_activity_kind(session)
                )

        return Decision(
            Action.IN_FLIGHT,
            f"round {round_} is stale ({int(age / 60)} min) but session "
            f"{session} is still {life} — not "
            f"respawning over a live reviewer",
            round_,
            session=session,
            deferred=True,
        )

    def _convergence_reason(
        self, my_reviews: list[Review], task: Task | None, head_sha: str
    ) -> str | None:
        """Why the loop is done, or None if it is not.

        Two independent signals, because neither alone is sufficient:

        * The GitHub review state. Authoritative when it says APPROVED, but
          reviewers work in comment mode, which can only ever produce
          COMMENTED -- #210 has zero APPROVED records across its whole history.
          On its own this made convergence unreachable: every PR, however
          clean, ran to the round cap and escalated.
        * The CR6 verdict envelope on the Alissa review task. The review skill
          declares this the verdict of record, and unlike the GitHub state it
          can actually express approval, so it is the signal that closes the
          loop in practice.

        BOTH are bound to the current head. An approval means "this code is
        good", so once the implementer pushes past the reviewed commit it is
        about old code and the next round is owed. Without this bind a stale
        approve latches the loop shut forever -- #227: round 1 approved
        `fa304de`, the implementer pushed `fd500fc` and re-requested (and even
        dismissed the approve), yet the envelope still read approve and no round
        2 was ever queued.
        """
        if not my_reviews:
            return None

        # New commits since the newest review -> its verdict is about old code.
        # A falsy commit_id (older records lack one) can't be checked, so it
        # falls through rather than blocking convergence.
        newest = my_reviews[-1]
        if newest.commit_id and newest.commit_id != head_sha:
            return None

        if newest.state == "APPROVED":
            return "last GitHub review state is APPROVED"

        # A native verdict THIS daemon posted at this head that is NOT an
        # approve is the daemon's own refusal to approve the head, and it
        # outranks the envelope it was posted from. Today only the CI gate
        # produces that shape (issue #58): a red or never-concluded rollup turns
        # an approve envelope into a REQUEST_CHANGES or COMMENT review. Without
        # this, the very next poll would converge on the envelope, withdraw the
        # review request, and take the PR out of the loop on a verdict the
        # daemon deliberately declined to post as an APPROVE -- leaving nothing
        # to re-enter and no later green round to approve it.
        #
        # Deliberately narrowed to the two states the gate can produce.
        # `SUBMITTED_STATES` also includes DISMISSED, and a daemon-posted review
        # carries its `verdict_round` forever -- so a broader test would ALSO
        # change what a dismissed approve does to convergence (today the
        # envelope still converges it). That may well be wrong, but it is
        # pre-existing and its own question: TASK-939082213. This change is
        # exactly the one its rationale claims.
        if (
            newest.verdict_round is not None
            and newest.state in GATED_VERDICT_STATES
        ):
            return None

        # Only checkable once a review task exists; before that there is
        # nowhere for a verdict to have been recorded.
        if task is not None and self.alissa.latest_verdict(task.ref) == VERDICT_APPROVE:
            return f"newest verdict envelope on {task.ref} reads approve"

        return None

    # -- round close-out ---------------------------------------------------

    def _clear_own_review_request(
        self, pr: PullRequest, my_reviews: list[Review], why: str
    ) -> None:
        """Withdraw this daemon's own dangling review request on a closed round.

        Normally there is nothing to do: GitHub consumes a review request the
        moment the requested identity submits a review, so a converged PR has
        no request left and drops straight out of `review_requests`. The
        request survives when the review that closed the round was NOT posted
        by the requested identity -- studio #298, where a ready-flip
        auto-requested `alissa-app` while the round ran under another login.
        Nothing then ever consumes it, so the PR stays in the search set and
        every poll pays a full re-verification (PR fetch, reviews, task,
        envelope count) to reach this same no-op. Removing the request is what
        ends that: the search is the evaluation set, so the PR leaves it.

        Four properties this must not violate:

        * Only ever OUR login. Human reviewers and any second bot in the same
          `requested_reviewers` array are somebody else's pending work, and a
          daemon that withdrew them would silently cancel real reviews.
        * Only from the terminal branch. The caller is the one return where a
          verdict of record stands at the current head and no further round can
          be owed; a request withdrawn while a round could still open would
          delete the very trigger that surfaces the PR.
        * Only on a HEAD-BOUND verdict. `_convergence_reason` cannot check a
          review that carries no `commit_id` and lets it converge anyway; that
          convergence is not about the current head, so withdrawing on it would
          take a PR out of the daemon's sight on a verdict that might be about
          old code. Such a review is not producible today -- `submit_review`
          always pins a commit and GitHub populates it -- but the terminal
          argument must hold on its own terms rather than by inheritance, so
          the check is repeated here.
        * Never blocking. The removal is a side effect of a decision already
          made, so any failure -- including a throttled one -- is logged and
          dropped rather than raised: the pass keeps walking and the next poll
          retries, which is strictly better than converting a no-op into an
          aborted pass. (Deliberately unlike the read paths, which let
          RateLimited reach run_forever's backoff.)

        `my_reviews` is non-empty at every call site -- `_convergence_reason`
        returns None on an empty one -- so its newest entry IS the verdict of
        record, read positionally rather than defended against.
        """
        mine = self.github.login
        if mine not in pr.requested_reviewers:
            return

        verdict = my_reviews[-1]
        if not verdict.commit_id:
            log.debug(
                "%s: not withdrawing the review request — the converged review "
                "%s carries no commit_id, so convergence is not head-bound",
                pr.slug, verdict.url or "with no url",
            )
            return

        # Above the dry-run guard: the drift check reads and logs but takes
        # nothing on GitHub, and in dry-run it records nothing either -- it
        # OBSERVES the pass rather than acting in it. Dry-run is the mode an
        # operator reaches for to diagnose exactly this, so staying silent
        # about it there is the worst place. See _warn_identity_drift for why
        # the ledger is untouched in BOTH directions there.
        self._warn_identity_drift(pr)

        if self.config.dry_run:
            log.info(
                "[dry-run] would withdraw the dangling review request for %s on "
                "%s — the round is closed (%s)",
                mine, pr.slug, why,
            )
            return

        try:
            self.github.remove_review_request(pr.owner, pr.repo, pr.number, mine)
        except Exception as exc:
            kind = withdraw_failed_kind(exc)
            if self.state.pinged(pr.full_name, pr.number, kind):
                # Still retried, just not re-announced; see withdraw_failed_kind.
                log.debug(
                    "%s: withdrawal still failing for %s (%s)", pr.slug, mine, exc
                )
                return
            self.state.record_ping(pr.full_name, pr.number, kind)
            log.warning(
                "%s: could not withdraw the dangling review request for %s (%s) "
                "— the closed round will be re-evaluated next poll and the "
                "removal retried. Withdrawing needs `pull_requests: write`, "
                "which is more than reviewing needs; further failures of this "
                "kind log at debug",
                pr.slug, mine, exc,
            )
            return

        others = [login for login in pr.requested_reviewers if login != mine]
        left = ", ".join(others) if others else "no other reviewers"
        kind = withdrawn_kind(pr.head_sha)
        if self.state.pinged(pr.full_name, pr.number, kind):
            # The request came back at a head this daemon already closed out.
            # Someone asked for another look at code that still carries its
            # approve -- and the daemon has just taken their request away, so
            # say so where they will find it.
            log.warning(
                "%s round close-out: the review request for %s came back at "
                "head %s and was withdrawn AGAIN — the approve at that head "
                "still stands (%s), so no round is owed; only a new commit "
                "opens one. Verdict of record %s. Left in place: %s",
                pr.slug, mine, pr.head_sha[:8], why, verdict.url, left,
            )
            return
        self.state.record_ping(pr.full_name, pr.number, kind)
        log.info(
            "%s round close-out: withdrew the dangling review request for %s — "
            "%s; verdict of record %s at head %s. Left in place: %s",
            pr.slug, mine, why, verdict.url, pr.head_sha[:8], left,
        )

    def _drift_gated(self, pr: PullRequest, kind: str, record: bool) -> bool:
        """Whether this drift-check gate is already closed for `pr`.

        The two backing stores are not interchangeable and the split is the
        whole point: durable in production, process-lifetime in dry-run, so
        neither mode can ever silence the other. See _warn_identity_drift.
        """
        if record:
            return self.state.pinged(pr.full_name, pr.number, kind)
        return (pr.full_name, pr.number, kind) in self._dry_run_drift

    def _note_drift_gate(self, pr: PullRequest, kind: str, record: bool) -> None:
        if record:
            self.state.record_ping(pr.full_name, pr.number, kind)
        else:
            self._dry_run_drift.add((pr.full_name, pr.number, kind))

    def _warn_identity_drift(self, pr: PullRequest) -> None:
        """Page once when the round's write-up landed under a login GitHub does
        not hold the request against.

        This is the #298 shape: the request names one identity, the newest
        review on the PR carries another, and GitHub only ever consumes a
        request when the REQUESTED login submits. Withdrawing the request
        cleans up this PR; the config that produced it will produce the next
        one too, so it is worth one loud line naming both identities.

        Diagnostic only -- it never gates the removal, and an unreadable review
        list just means no warning, not a stalled close-out.

        Two gates, both of them once-per-condition: the extra read runs once per
        (PR, head) and the warning once per (PR, login pair). See
        drift_probe_kind and identity_drift_kind.

        In `--dry-run` the SAME two gates apply, held in memory instead of in
        the ping ledger -- which the run must not touch in either direction.
        Writing would let a diagnostic pass durably silence the daemon it was
        run to diagnose (`state_db` has no dry-run branch, so the rows land in
        the same `state.db` production reads, and the alarm is once-per-PR).
        Reading would silence the DIAGNOSTIC instead: a production pass that
        already probed this head would make the operator's dry-run print
        nothing. Process-lifetime scope gives both -- a fresh dry-run always
        announces, and a daemon left running in dry-run does not re-emit the
        same block every poll. Same reasoning as `_ignored_acks`: a statement
        about one process belongs in the process, not in the ledger.
        """
        record = not self.config.dry_run

        probe = drift_probe_kind(pr.head_sha)
        if self._drift_gated(pr, probe, record):
            return

        try:
            reviews = self.github.reviews(pr.owner, pr.repo, pr.number)
        except Exception as exc:
            # Deliberately not recorded: a read that failed settles nothing.
            log.debug("%s: could not read reviews for the drift check: %s", pr.slug, exc)
            return
        self._note_drift_gate(pr, probe, record)

        substantive = [r for r in reviews if r.is_substantive]
        if not substantive:
            return
        newest = max(substantive, key=lambda r: r.submitted_at)
        if newest.author == self.github.login:
            return

        kind = identity_drift_kind(self.github.login, newest.author)
        if self._drift_gated(pr, kind, record):
            return
        self._note_drift_gate(pr, kind, record)
        log.warning(
            "IDENTITY DRIFT on %s: the review request is held against %r but "
            "the round's newest review was submitted by %r (%s). GitHub only "
            "consumes a review request when the REQUESTED login submits, so "
            "native review consumption cannot work for this deployment: every "
            "closed round will leave a dangling request for the daemon to "
            "withdraw. Point reviewer_login/reviewer_token_env at one identity "
            "and have that identity post the verdict of record.",
            pr.slug,
            self.github.login,
            newest.author,
            newest.url or "no url",
        )

    # -- reap sweep --------------------------------------------------------

    def sweep_sessions(self) -> int:
        """Kill the managed session of every finished round. Runs every poll.

        Returns the number of sessions actually reaped this pass (0 in
        `--dry-run`, where the sweep only logs) -- the poll-snapshot exhaust
        records it, and it is the one count the snapshot cannot derive from
        the per-PR Decision list.

        The predecessor of this sweep ran inside evaluate(), which is fed by
        the review-requested:@me search -- and submitting a review CLEARS the
        request, so a finished round's PR vanished from the search at exactly
        the moment its session became reapable; terminal (approved) rounds
        were never reaped and idle reviewer sessions accumulated in the
        worker. The sweep instead starts from the live session list, which
        cannot lose a finished session, and works back to the round via the
        spawn ledger. It must stay search-independent: never move it (back)
        into the evaluate() path.

        Two reap edges, and the difference is what the sweep KNOWS about a
        session:

        * With a spawn-ledger row the session's PR and round are exact, so a
          round whose verdict already landed is reapable on its own -- an
          approved-but-unmerged PR is the common case, and waiting for a human
          to merge before freeing the slot is the leak this sweep predates.
        * Without one -- the hand-driven `review-pr-<n>` rounds the
          alissa-code-review skill's procedures spawn, and any spawn whose
          ledger was lost -- the name is the only evidence, and it cannot tell
          a superseded round from an in-flight one. Those are reaped on a
          TERMINAL PR only (issue #46): merged or closed ends every round on
          the PR, so no round accounting is needed to be sure. Superseded
          rounds of an OPEN PR are deliberately out of scope -- an operator
          re-entry (`alissa-review: re-enter +N`) may still want that context.

        Both edges additionally require the session to be idle and to have been
        quiet for `reap_grace_seconds`: a busy session is NEVER killed, even on
        a merged PR (scoped post-merge re-reviews of fold commits are an
        established pattern), and the grace period leaves a just-merged PR's
        reviewer time to finish its in-session close-out.

        Every-poll cost, honestly: one `alissa tmux ls` when no reviewer
        session is live; otherwise one PR fetch per distinct PR with a live
        idle quiet session, plus -- per distinct (PR, task ref) among its
        ledger rows -- exactly one of `alissa task get <ref>` (the row carries
        a task ref) or the reviews fetch (it does not). The ledger ref is used
        deliberately instead of find_review_task: that would fetch the actor's
        ENTIRE task list per PR, and its open-status filter would drop a
        human-validated review task back onto the racier GitHub-count
        fallback. A ledger-less session costs its allowlist probe ONCE per
        session lifetime, not once per poll -- `_probe_cache` remembers the
        answer, including the negative one -- after which it is the same one
        fetch per distinct PR as everything else. Busy and in-grace sessions
        are filtered out BEFORE anything is fetched at all. Only individual
        sessions are ever killed (`alissa tmux kill <name>`) -- never the
        server, which in this shared container would take every other lane's
        workers with it. Best-effort throughout: an undecidable session is
        spared, logged at debug, and looked at again next poll; only genuine
        failures (a fetch that blew up, a kill that raised) are logged louder.

        Every spare records WHY against the session name. Those reasons are
        debug-level (they repeat every poll), but the cap alarm interpolates
        them, so the one line an operator does see in a container running at
        INFO carries the explanation with it.
        """
        try:
            sessions = self.alissa.list_review_sessions()
        except CommandError as exc:
            log.warning("reap sweep skipped: could not list sessions: %s", exc)
            return 0

        # Drop cached resolutions for sessions that are gone. Names are unique
        # per spawn (that is what the nonce is for) and a session's PR never
        # changes, so this is the only invalidation the cache needs.
        live_names = {ses.name for ses in sessions}
        self._probe_cache = {
            name: target
            for name, target in self._probe_cache.items()
            if name in live_names
        }

        # Per-sweep memos. The PR fetch is keyed per distinct (repo, number);
        # the round count additionally keys on the task ref, because two spawns
        # of one PR can disagree on it (a round-1 row recorded before the review
        # task existed carries None). None = undecidable this pass.
        prs: dict[tuple[str, int], PullRequest | None] = {}
        completed_cache: dict[tuple[str, int, str | None], float | None] = {}
        holdouts: dict[str, str] = {}
        reaped: list[str] = []

        for ses in sessions:
            idle_for = time.time() - ses.last_activity
            if not ses.is_idle:
                # A busy session is still doing something (reviewing, or
                # closing out its round) -- never yank the slot from under it,
                # whatever its PR's state. Logged, not killed. Deliberately
                # without resolving the PR: the state cannot change the
                # outcome, and paying a fetch per poll for every working
                # reviewer just to log it is not worth the API budget.
                self._hold(holdouts, ses, "busy — never reaped")
                continue
            if idle_for < self.config.reap_grace_seconds:
                # Idle but recently active: likely mid-close-out (the review
                # is submitted before the envelope and task move land). Wait
                # out the grace period.
                self._hold(
                    holdouts, ses,
                    f"idle {idle_for // 60:.0f} min, inside the "
                    f"{self.config.reap_grace_seconds // 60} min grace",
                )
                continue

            row = self.state.find_spawn_by_session(ses.name)
            pr = self._resolve_pr(ses, row, prs, holdouts)
            if pr is None:
                continue  # undecidable -- already recorded, retried next poll

            reason = self._reap_reason(pr, ses, row, completed_cache, holdouts)
            if reason is None:
                continue

            # `last_activity` is 0 when the CLI reported none: that reads as
            # "quiet for ages" to the guard above, but printing the epoch as a
            # duration would be a lie, so the evidence says so instead.
            idle_note = (
                f"idle {idle_for // 60:.0f} min"
                if ses.last_activity
                else "idle, no activity timestamp"
            )
            evidence = (
                f"{pr.slug} is {'merged' if pr.merged else pr.state}, "
                f"{idle_note} — {reason}"
            )
            if self.config.dry_run:
                log.info("[dry-run] would reap reviewer session %s (%s)", ses.name, evidence)
                holdouts[ses.name] = f"dry-run: would have reaped ({evidence})"
                continue
            try:
                self.alissa.kill_session(ses.name)
            except Exception:  # pragma: no cover - defence in depth
                log.exception("failed to reap session %s", ses.name)
                holdouts[ses.name] = "the kill raised — see the traceback above"
                continue
            # Bookkeeping only -- deliberately never consulted before a kill.
            # The live list is the authority; gating on the reaps table would
            # spare any session killed behind the ledger's back.
            self.state.record_reap(ses.name)
            reaped.append(ses.name)
            log.info("reaped reviewer session %s (%s)", ses.name, evidence)

        self._check_session_cap(sessions, reaped, holdouts)
        return len(reaped)

    def _hold(self, holdouts: dict[str, str], ses: ManagedSession, why: str) -> None:
        """Record (and log at debug) why a live session was not reaped.

        Debug because it repeats every poll for the whole life of a spared
        session -- and the deployed container runs at INFO, so these lines are
        invisible there by design. They are not the operator's channel: the
        cap alarm is, and it interpolates what is recorded here.
        """
        holdouts[ses.name] = why
        log.debug("reap sweep: %s spared — %s", ses.name, why)

    def _resolve_pr(
        self,
        ses: ManagedSession,
        row: sqlite3.Row | None,
        prs: dict[tuple[str, int], PullRequest | None],
        holdouts: dict[str, str],
    ) -> PullRequest | None:
        """The PR a reapable session is about, or None when undecidable.

        A ledger row names the PR outright. Without one the session name is
        the only evidence, so the number it carries is resolved against the
        `repos` allowlist. A name carrying a repo component picks its
        allowlist entry directly; a bare `review-pr-<n>` (the skill's shape)
        names no repo, so every watched repo is probed and EXACTLY one must
        have that PR. An empty allowlist -- "watch every repo that asks" --
        can never resolve a bare name at all; nothing bounds the search.

        What that does and does NOT establish, precisely, because the
        difference decides whose session can be killed: the allowlist BOUNDS
        THE SEARCH, it does not prove ownership. A bare number carries no
        repo, so a session reviewing a PR in an UNWATCHED repo is reaped if
        exactly one watched repo happens to have a terminal PR of the same
        number. Requiring a unique hit bounds the blast radius rather than
        removing it. The converse costs reaps instead: once two watched repos
        both have a PR #n -- inevitable as newer repos' numbering catches up
        with older ones -- every bare `review-pr-<n>` in the overlap resolves
        to two hits and is spared forever. Both are known v1 limits, recorded
        in the README; the durable fix is a repo in the name (skill-side) or a
        ledger row for hand-spawned sessions.

        The probe is paid ONCE per session name. Its answer -- the resolved
        (repo, number), or a sticky None for "could not be resolved" -- is
        cached on the watcher, because a name is unique per spawn and its PR
        never changes. Without that, a session spared on an open PR (the
        common case: a finished round waits idle for the implementer) re-ran
        the whole allowlist sweep on every poll for the life of the PR, which
        at 7 repos and a 30s interval is ~840 REST calls an hour for one
        session. The sticky None never re-probes within a process, so an
        ambiguity that later clears is not noticed until a restart -- the safe
        direction (spare, never guess), and the alternative is exactly the
        cost above.
        """
        if row is not None:
            return self._fetch_pr(prs, row["repo"], row["number"])

        ref = ses.ref
        if ref is None:  # pragma: no cover - list_review_sessions filters these
            return None

        if ses.name not in self._probe_cache:
            self._probe_cache[ses.name] = self._probe_allowlist(ses, ref, prs)
        target = self._probe_cache[ses.name]
        if target is None:
            self._hold(
                holdouts, ses,
                f"no ledger row and PR #{ref.number} does not resolve to exactly "
                f"one watched repo — sparing it rather than guessing",
            )
            return None
        # A memo hit in the common case: the probe that resolved this session
        # already fetched it. On a later poll this is the one fetch that can
        # turn the session reapable, so it is not cacheable.
        return self._fetch_pr(prs, target[0], target[1])

    def _probe_allowlist(
        self,
        ses: ManagedSession,
        ref: SessionRef,
        prs: dict[tuple[str, int], PullRequest | None],
    ) -> tuple[str, int] | None:
        """Which watched repo owns this session's PR number, or None.

        The expensive half of _resolve_pr, called once per session name.
        """
        candidates = self._name_candidates(ref)
        if not candidates:
            return None
        hits = []
        for repo_slug in candidates:
            # Quiet: probing a repo that simply has no PR #n is the expected
            # outcome for all but one candidate, not a failure worth a warning.
            if self._fetch_pr(prs, repo_slug, ref.number, quiet=True) is not None:
                hits.append(repo_slug)
        if len(hits) != 1:
            log.debug(
                "reap sweep: PR #%d of %s resolves to %d watched repo(s)",
                ref.number, ses.name, len(hits),
            )
            return None
        return (hits[0], ref.number)

    def _name_candidates(self, ref: SessionRef) -> list[str]:
        """The watched repos a session name could be about.

        A name-borne repo component narrows the allowlist to (at most) its own
        entry; a bare name leaves the whole allowlist as candidates. Matching
        goes through `session_repo_slug` on both sides so `studio.alissa.app`
        and the `studio-alissa-app` a session name can carry compare equal.
        """
        if ref.repo is None:
            return list(self.config.repos)
        return [
            full_name
            for full_name in self.config.repos
            if session_repo_slug(full_name.partition("/")[2]) == ref.repo
        ]

    def _reap_reason(
        self,
        pr: PullRequest,
        ses: ManagedSession,
        row: sqlite3.Row | None,
        completed_cache: dict[tuple[str, int, str | None], float | None],
        holdouts: dict[str, str],
    ) -> str | None:
        """Why this session may be reaped, or None to spare it (recorded)."""
        if pr.is_terminal:
            return "the PR is terminal, so every round on it is over"

        if row is None:
            # v1 reaps a ledger-less session on a terminal PR only. The name's
            # `-r<k>` cannot tell a superseded round from an in-flight one, and
            # an operator re-entry may still want the earlier round's context;
            # superseded-round reaping is a v2 with its own analysis (#46).
            self._hold(
                holdouts, ses,
                f"{pr.slug} is open and there is no ledger row — v1 reaps "
                f"ledger-less sessions on terminal PRs only",
            )
            return None

        key = (row["repo"], row["number"], row["task_ref"])
        if key not in completed_cache:
            completed_cache[key] = self._completed_rounds(pr, row["task_ref"])
        completed = completed_cache[key]
        if completed is None or row["round"] > completed:
            self._hold(
                holdouts, ses,
                f"round {row['round']} of {pr.slug} is not finished "
                f"({completed} completed)",
            )
            return None
        return f"round {row['round']} of an open PR is done"

    def _check_session_cap(
        self,
        sessions: list[ManagedSession],
        reaped: list[str],
        holdouts: dict[str, str],
    ) -> None:
        """Page-worthy log when the sweep is not keeping up.

        Counted AFTER the sweep, from the same live list the sweep walked
        minus what it killed, so the number is "sessions this pass could not
        free". Every idle agent session holds hundreds of MB forever and the
        worker container is shared, so a count that stays above the cap is the
        2026-07-28 incident happening again.

        The page carries each survivor's spare reason inline, because the
        per-session holdout lines are debug and the deployed container runs at
        INFO: an alarm whose explanation is invisible is an alarm an operator
        cannot act on.

        Deduped in-process on the set of surviving names: the sweep runs every
        poll (every 30s in the deployed config), and 120 identical pages an
        hour is not a page. It re-fires when the set changes, and clears once
        the count falls back inside the cap so the next episode pages again.
        Deliberately NOT the `pings` ledger the stalled-round escalation uses:
        that table is keyed (repo, number, kind) and this alarm belongs to no
        PR, so persisting it would mean a sentinel row every console reader
        then has to filter around -- and a standing over-cap condition SHOULD
        announce itself once to a freshly restarted daemon, which a persisted
        ping would suppress forever.
        """
        killed = set(reaped)
        remaining = sorted(s.name for s in sessions if s.name not in killed)
        if len(remaining) <= self.config.reap_session_cap:
            self._paged_cap = None
            return
        episode = frozenset(remaining)
        if episode == self._paged_cap:
            return
        self._paged_cap = episode
        log.error(
            "REVIEWER SESSION CAP EXCEEDED: %d live reviewer sessions after the "
            "sweep (cap %d) — each holds hundreds of MB in a shared container. "
            "Survivors and why the sweep spared each: %s",
            len(remaining),
            self.config.reap_session_cap,
            "; ".join(
                f"{name} ({holdouts.get(name, 'no reason recorded')})"
                for name in remaining
            ),
        )

    def _fetch_pr(
        self,
        prs: dict[tuple[str, int], PullRequest | None],
        repo_slug: str,
        number: int,
        *,
        quiet: bool = False,
    ) -> PullRequest | None:
        """One memoized PR fetch for the sweep, per distinct (repo, number).

        None = the PR could not be fetched -- it does not exist in that repo,
        or the call failed. Either way every session resolving through it is
        spared this pass and looked at again next poll. `quiet` demotes the
        log to debug for the allowlist probe, where a miss is the expected
        answer for all but one candidate. RateLimited propagates so
        run_forever backs off instead of hammering the API once per session.
        """
        key = (repo_slug, number)
        if key in prs:
            return prs[key]
        owner, _, repo = repo_slug.partition("/")
        try:
            prs[key] = self.github.pull_request(owner, repo, number)
        except RateLimited:
            raise
        except CommandError as exc:
            log.log(
                logging.DEBUG if quiet else logging.WARNING,
                "reap sweep: could not fetch %s#%d: %s", repo_slug, number, exc,
            )
            prs[key] = None
        return prs[key]

    def _completed_rounds(self, pr: PullRequest, task_ref: str | None) -> float | None:
        """How many rounds of this OPEN PR are over, judged from task/GitHub.

        Rounds completed = verdict envelopes on the review task (the
        authoritative round record), addressed by the task ref the ledger
        captured at spawn time -- NOT find_review_task, which would fetch the
        whole task list and whose open-status filter loses validated tasks.
        The substantive-review count is the fallback only for spawns recorded
        before any review task existed. None means "could not tell" -- the
        sweep spares the session and retries next poll.

        Terminal PRs never reach here: the caller answers those before asking
        about rounds, because a merged or closed PR ends every round on it
        whatever the envelopes say.
        """
        if task_ref:
            # count_verdicts never raises; unreadable evidence degrades to 0,
            # which spares the session (round >= 1 > 0).
            return self.alissa.count_verdicts(task_ref)
        try:
            return countable_rounds(self.github.my_reviews(pr.owner, pr.repo, pr.number))
        except RateLimited:
            raise
        except CommandError as exc:
            log.warning("reap sweep: could not count reviews on %s: %s", pr.slug, exc)
            return None

    # -- actions -----------------------------------------------------------

    def _spawn(
        self,
        pr: PullRequest,
        round_: int,
        task: Task | None,
        cap: int,
        *,
        reenqueued: bool = False,
    ) -> Decision:
        if task is None:
            if self.config.on_missing_review_task == ON_MISSING_SKIP:
                return Decision(
                    Action.SKIPPED, "no open Alissa review task (CR2)", round_
                )
            log.warning(
                "%s has no open Alissa review task (CR2) — spawning against the PR "
                "URL; the reviewer must create or locate one before recording a verdict",
                pr.slug,
            )
            assignment = (
                f"Review the GitHub PR {pr.url} . There is no Alissa review task for "
                f"it yet — locate the origin task from the PR and create the downstream "
                f"review task per CR2 before recording your verdict."
            )
        else:
            assignment = f"You've been assigned Alissa review task {task.ref}."

        name = session_name(pr, round_)
        template = ROUND_1_DIRECTIVE if round_ == 1 else ROUND_K_DIRECTIVE
        directive = template.format(
            assignment=assignment,
            round=round_,
            cap=cap,
            session=name,
            credential=self._credential_clause(),
        )

        hub, problem = self._ensure_hub(pr)
        if problem is not None:
            return Decision(Action.SKIPPED, problem, round_)

        self.alissa.enqueue_reviewer(
            session=name,
            directive=directive,
            cwd=hub,
            agent=self.config.agent_profile,
            task_ref=task.ref if task else None,
            dry_run=self.config.dry_run,
        )

        if not self.config.dry_run:
            self.state.record_spawn(
                repo=pr.full_name,
                number=pr.number,
                round_=round_,
                head_sha=pr.head_sha,
                session=name,
                task_ref=task.ref if task else None,
            )

        # AFTER the enqueue on purpose: the activity comment is telemetry and
        # must never gate the spawn it reports on.
        context = (
            "re-enqueued — previous session presumed dead" if reenqueued else "spawned"
        )
        self._append_activity(pr, self._activity_line(name, round_, context, cap))

        return Decision(
            Action.SPAWNED,
            f"session {name} → {task.ref if task else 'no task'}",
            round_,
            session=name,
            task_ref=task.ref if task else None,
            reenqueued=reenqueued,
        )

    def _credential_clause(self) -> str:
        """The directive's credential-routing clause, or "" when there is
        nothing useful to say.

        `alissa tmux queue add` has no env-injection flag, so a reviewer
        session inherits the worker's environment -- in this container, the
        IMPLEMENTER identity's `gh` credential. The daemon cannot fix that from
        outside the session; what it can do is tell the session which variable
        holds the right token, by NAME, so its own `gh` calls can route around
        the default. With no `reviewer_token_env` configured there is no name
        to give and the clause is omitted rather than replaced with advice the
        session cannot act on -- the daemon's own native post is the guarantee
        either way.
        """
        if not self.config.reviewer_token_env:
            return ""
        return _POST_AS_REVIEWER.format(
            env_var=self.config.reviewer_token_env, reviewer=self.github.login
        )

    def _ensure_hub(self, pr: PullRequest) -> tuple[Path, str | None]:
        """Resolve the reviewer's cwd, hub-ifying the repo first if configured.

        Returns (hub, problem). `problem` is non-None when the round cannot run.
        """
        hub = self.config.hub_for(pr.owner, pr.repo)
        if hub.is_dir():
            return hub, None

        if self.config.on_missing_hub != HUB_ADD:
            return hub, (
                f"no worktree hub at {hub} — add the repo with "
                f"`alissa code workspace add {pr.full_name}`, or set "
                f"on_missing_hub='add' (requires a repos allowlist)"
            )

        # Guarded twice: config.load() rejects 'add' without an allowlist, and
        # poll_once() only reaches here for watched repos. Belt and braces --
        # this path clones code onto the machine and opens it as an agent cwd.
        if not self.config.watches(pr.full_name):
            return hub, f"{pr.full_name} is not in the repos allowlist"

        if not self.config.manifest_path.is_file():
            return hub, (
                f"{self.config.workspace_root} is not an Alissa Code Workspace "
                f"(no alissa-workspace.yaml) — run `alissa code workspace init`"
            )

        try:
            self.alissa.add_repo_to_workspace(
                pr.owner,
                pr.repo,
                self.config.workspace_root,
                dry_run=self.config.dry_run,
            )
        except CommandError as exc:
            return hub, f"could not hub-ify {pr.full_name}: {exc}"

        if self.config.dry_run:
            return hub, None
        if not hub.is_dir():
            return hub, (
                f"`alissa code workspace add {pr.full_name}` reported success but "
                f"{hub} still does not exist — check hub_template against the "
                f"manifest's `dir:` override"
            )
        return hub, None

    def preflight(self) -> list[str]:
        """Startup checks. Returns warnings; raises on anything fatal."""
        warnings: list[str] = []

        # Fatal: a mismatched identity silently breaks round counting -- and
        # this is also the once-per-process identity assertion every later
        # review post is held to (see GitHub.assert_review_identity), so the
        # resolved login is logged rather than merely checked.
        login = self.github.verify_identity()
        source = (
            f"from ${self.config.reviewer_token_env}"
            if self.config.reviewer_token_env
            else "from the inherited gh credential"
        )
        log.info("reviewing as GitHub user %s (%s)", login, source)

        if not self.config.reviewer_token_env:
            warnings.append(
                "no reviewer_token_env configured — every `gh` call uses "
                "whatever GH_TOKEN/GITHUB_TOKEN this process inherited. In a "
                "container that holds more than one GitHub identity that is "
                "how a round's verdict lands under the wrong login; set "
                "reviewer_token_env to the NAME of the variable carrying the "
                f"reviewer ({login}) token"
            )

        if not self.config.workspace_root.is_dir():
            warnings.append(f"workspace_root {self.config.workspace_root} does not exist")
        elif not self.config.manifest_path.is_file():
            warnings.append(
                f"{self.config.workspace_root} has no alissa-workspace.yaml — it is "
                f"not an Alissa Code Workspace yet (`alissa code workspace init`)"
            )

        if not self.config.dry_run and not self.alissa.worker_running():
            warnings.append(
                "`alissa worker` does not appear to be running — queued reviewer "
                "sessions will not spawn until it is (`alissa worker start`)"
            )

        return warnings

    def _activity_line(self, session: str, round_: int, context: str, cap: int) -> str:
        """One mechanical line. `cap` is the PR's EFFECTIVE cap (config plus
        granted re-entries) -- printing the config value would render a granted
        round 11 as "round 11 of 10"."""
        return f"- {_now()} — `{session}` — round {round_} of {cap} — {context}"

    def _append_activity(self, pr: PullRequest, line: str) -> bool:
        """Append one line to THE activity comment on the PR; True if it landed.

        Find-or-create: the PR's issue comments are filtered to OWN authorship
        AND the hidden marker, and the line is PATCH-appended to the first
        match; with no match, one marker-carrying comment is created. A marker
        pasted by anyone else fails the author filter and is never touched.

        Best-effort by contract: this is telemetry about a spawn that has
        already happened, so no failure here may surface to the caller. The
        except is deliberately broad -- _api turns rate-limit errors into
        RateLimited (not CommandError), and letting that fly out of evaluate()
        would fail the whole poll pass over a log line.
        """
        if self.config.dry_run:
            log.info("[dry-run] would append activity line on %s: %s", pr.slug, line)
            return False
        try:
            mine = [
                c
                for c in self.github.issue_comments(pr.owner, pr.repo, pr.number)
                if c.author == self.github.login and ACTIVITY_MARKER in c.body
            ]
            if mine:
                self.github.update_comment(
                    pr.owner, pr.repo, mine[0].id, mine[0].body + "\n" + line
                )
            else:
                self.github.comment(
                    pr.owner, pr.repo, pr.number, ACTIVITY_HEADER + "\n" + line
                )
        except Exception as exc:
            log.warning("could not append activity line on %s: %s", pr.slug, exc)
            return False
        return True

    def _escalate_stalled(
        self, pr: PullRequest, round_: int, session: str, age: float
    ) -> None:
        """Operator ping when the liveness deferral itself runs long: the
        session showing life is the only thing holding the respawn back, so
        a human must check whether it is progressing or wedged. Posted once
        per deferral EPISODE (the ping ledger row is keyed
        stalled_kind(session); a re-enqueued round that stalls again is a
        new session, so it pings again), and the row is recorded only AFTER
        the comment posts: this ping is the operator's only signal for the
        episode, so a transient comment failure must retry next poll --
        exactly-once delivered, unlike the cap-out page (a terminal state,
        recorded despite failure). The decision stays a deferral either way
        -- this comments, it never respawns."""
        body = STALLED_COMMENT.format(
            round=round_,
            minutes=int(age / 60),
            stale=STALE_ROUND_SECONDS // 60,
            session=session,
        )
        log.warning(
            "STALLED %s round %d has been deferred %.0f min behind live session "
            "%s — escalating to operator (once per episode)",
            pr.slug,
            round_,
            age / 60,
            session,
        )

        if self.config.dry_run:
            log.info("[dry-run] would comment on %s:\n%s", pr.slug, body)
            return

        try:
            self.github.comment(pr.owner, pr.repo, pr.number, body)
        except CommandError as exc:
            log.error(
                "could not post the stalled-round comment on %s: %s — not "
                "recording the episode; the ping retries next poll",
                pr.slug,
                exc,
            )
            return
        self.state.record_ping(pr.full_name, pr.number, stalled_kind(session))

    def _escalate(
        self,
        pr: PullRequest,
        my_reviews: list[Review],
        rounds: int,
        cap: int,
        grant: sqlite3.Row | None = None,
    ) -> None:
        """Page the operator, and teach the lever that gets past the page.

        The message carries the re-entry grammar because the cap-out is exactly
        the moment an operator needs it, and -- when the head has moved past
        the head the last verdict was written against -- the recommendation to
        grant a single verification round. A `grant` names the re-entry that
        was already spent, so a second page cannot read as a repeat of the
        first.
        """
        last = my_reviews[-1] if my_reviews else None
        last_state = (last.state if last else "none").lower()
        reviewed = (last.commit_id if last else "") or ""
        moved = bool(reviewed) and reviewed != pr.head_sha
        verification = ""
        if moved:
            verification = VERIFICATION_HINT.format(
                sha=pr.head_sha[:8], reviewed=reviewed[:8]
            )
        template = VERDICT_LINE_MOVED if moved else VERDICT_LINE
        verdict_line = template.format(
            last_state=last_state, sha=pr.head_sha[:8], reviewed=reviewed[:8]
        )
        grant_note = ""
        if grant is not None:
            grant_note = GRANT_CONSUMED_NOTE.format(
                author=grant["author"],
                rounds=grant["rounds"],
                comment_id=grant["comment_id"],
            )
        body = ESCALATION_COMMENT.format(
            rounds=rounds,
            cap=cap,
            grant_note=grant_note,
            verdict_line=verdict_line,
            verification=verification,
            max_rounds=MAX_REENTRY_ROUNDS,
        )
        log.error(
            "CAP-OUT %s after %d rounds (effective cap %d) — escalating to operator",
            pr.slug,
            rounds,
            cap,
        )

        if self.config.dry_run:
            log.info("[dry-run] would comment on %s:\n%s", pr.slug, body)
            return

        try:
            self.github.comment(pr.owner, pr.repo, pr.number, body)
        except CommandError as exc:
            log.error("could not post escalation comment on %s: %s", pr.slug, exc)
        # Recorded even when the comment failed: a cap-out is terminal state,
        # and re-paging it every poll because GitHub was briefly unavailable
        # would be worse than the one missed comment (the log line above and
        # the escalations row both survive it).
        self.state.record_escalation(pr.full_name, pr.number, pr.head_sha)
        self.state.record_ping(
            pr.full_name, pr.number, capout_kind(pr.head_sha, cap - self.config.round_cap)
        )

    # -- polling -----------------------------------------------------------

    def poll_once(self) -> list[tuple[str, Decision]]:
        # Sweep BEFORE evaluating: a full worker is exactly when a fresh spawn
        # needs the slot a finished session is squatting on. Deliberately not
        # inside the per-request loop below — the sweep must reach sessions
        # whose PR no longer appears in the search at all.
        started = time.monotonic()
        reaped = self.sweep_sessions()

        requests = self.github.review_requests(self.config.repos)
        log.info("%d PR(s) with a review pending from %s", len(requests), self.github.login)

        results = []
        for owner, repo, number in requests:
            slug = f"{owner}/{repo}#{number}"
            if not self.config.watches(f"{owner}/{repo}"):
                continue
            try:
                decision = self.evaluate(owner, repo, number)
            except RateLimited:
                raise
            except CommandError as exc:
                log.error("%s: %s", slug, exc)
                decision = Decision(Action.SKIPPED, str(exc))

            level = logging.INFO if decision.action != Action.SKIPPED else logging.DEBUG
            log.log(level, "%s → %s (%s)", slug, decision.action.value, decision.reason)
            results.append((slug, decision))

        # Persist one poll_snapshots row per pass, built entirely from the
        # Decision list already in hand plus the reap count -- no new GitHub
        # calls. Written in dry-run too: a snapshot OBSERVES the pass, it is
        # not a side effect the daemon takes, so a future console sees dry-run
        # passes as well as live ones.
        self._write_snapshot(
            results, reaped, duration_ms=int((time.monotonic() - started) * 1000)
        )
        return results

    def _stage_record(self, slug: str, decision: Decision) -> dict:
        """One per-item entry of a poll snapshot's compact JSON: the PR
        reference (the slug and the number parsed from it), the current stage
        (the decision's action, refined to name the stale-re-enqueue and
        liveness-deferral buckets the bare action folds together), and the
        round, session name, and origin task ref carried on the Decision.
        `attempt` is carried as a fixed None for schema parity with the
        devloop's per-item record -- the reviewloop is round-based and has no
        attempt dimension -- so one console can read both loops' snapshots."""
        _, _, tail = slug.partition("#")
        stage = decision.action.value
        if decision.reenqueued:
            stage = "stale-re-enqueued"
        elif decision.deferred:
            stage = "deferred"
        return {
            "slug": slug,
            "number": int(tail),
            "round": decision.round,
            "attempt": None,
            "session": decision.session,
            "stage": stage,
            "reason": decision.reason,
            "task_ref": decision.task_ref,
        }

    def _write_snapshot(
        self,
        results: list[tuple[str, Decision]],
        reaped: int,
        *,
        duration_ms: int,
    ) -> None:
        """Persist one poll_snapshots row from the pass's Decision list and the
        reaper count -- no new GitHub calls. The SPAWNED and IN_FLIGHT buckets
        each split in two off observational Decision flags: a SPAWNED that
        respawned a presumed-dead round is `stale_reenqueued`, and an
        IN_FLIGHT that is a liveness deferral (not a freshly-enqueued round) is
        `deferred`."""
        stages = [self._stage_record(slug, d) for slug, d in results]
        counts = Counter(d.action for _, d in results)
        spawned = sum(
            1 for _, d in results
            if d.action is Action.SPAWNED and not d.reenqueued
        )
        stale_reenqueued = sum(
            1 for _, d in results
            if d.action is Action.SPAWNED and d.reenqueued
        )
        in_flight = sum(
            1 for _, d in results
            if d.action is Action.IN_FLIGHT and not d.deferred
        )
        deferred = sum(
            1 for _, d in results
            if d.action is Action.IN_FLIGHT and d.deferred
        )
        self.state.record_snapshot(
            duration_ms=duration_ms,
            candidates=len(results),
            spawned=spawned,
            stale_reenqueued=stale_reenqueued,
            in_flight=in_flight,
            deferred=deferred,
            converged=counts[Action.CONVERGED],
            capped=counts[Action.CAPPED],
            escalated=counts[Action.ESCALATED],
            skipped=counts[Action.SKIPPED],
            reaped=reaped,
            posted=counts[Action.POSTED],
            awaiting_post=counts[Action.AWAITING_POST],
            abandoned=counts[Action.ABANDONED],
            stages=stages,
        )

    def run_forever(self) -> None:
        # preflight() is the caller's responsibility -- the CLI runs it once for
        # every mode, so calling it here too would double every check.
        backoff = self.config.poll_interval
        while True:
            # The sleep lives INSIDE the KeyboardInterrupt guard: with a 60s
            # poll interval (up to 900s backing off) the loop spends nearly
            # all its wall-clock sleeping, so a real Ctrl-C almost always
            # lands there and must hit the same clean-exit path.
            try:
                try:
                    self.poll_once()
                    backoff = self.config.poll_interval
                except RateLimited as exc:
                    backoff = min(backoff * 2, 900)
                    log.warning("rate limited (%s) — backing off %ds", exc, backoff)
                except CommandError as exc:
                    backoff = min(backoff * 2, 900)
                    log.error("poll failed: %s — retrying in %ds", exc, backoff)
                time.sleep(backoff)
            except KeyboardInterrupt:
                log.info("stopping")
                return
