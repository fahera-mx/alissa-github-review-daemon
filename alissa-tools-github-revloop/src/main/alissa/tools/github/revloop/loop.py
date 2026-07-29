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
from .ghclient import GitHub, IssueComment, PullRequest, RateLimited, Review
from .proc import CommandError
from .state import State

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FetchFailed:
    """Sentinel: the PR fetch did not answer, as distinct from "no such PR".

    A distinct type rather than a second None, because the reap sweep caches
    what a probe concluded: "GitHub says this repo has no PR #n" is a fact
    worth pinning, "the call blew up" is not. See loop._fetch_pr.

    `repo` names the candidate that failed, when the failure was noticed
    somewhere that knows which one it was (_probe_allowlist does; _fetch_pr's
    caller always knows already, so it gets the bare singleton). It exists so
    the stuck-probe page can NAME the repo an operator has to go look at --
    without it the page says "a watched repo", and finding which one means
    bisecting the allowlist by hand or raising a paging daemon to DEBUG.
    """

    repo: "str | None" = None


FETCH_FAILED = _FetchFailed()

# How many consecutive un-answerable probes of ONE session make a stuck
# candidate repo an operator problem rather than a blip. A constant, not a
# config key: the point is to catch a failure that is not transient, and three
# polls is already well past any blip at any sane poll interval. Pinned by a
# test. See ReviewWatcher._note_probe_taint.
PROBE_TAINT_ALARM = 3

# The reap sweep's per-pass PR memo, keyed (repo slug, number).
FetchMemo = dict[tuple[str, int], "PullRequest | None | _FetchFailed"]

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


class Action(str, Enum):
    SPAWNED = "spawned"
    IN_FLIGHT = "in-flight"
    CONVERGED = "converged"
    CAPPED = "capped"
    ESCALATED = "escalated"
    SKIPPED = "skipped"


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


def _is_not_found(exc: CommandError) -> bool:
    """Whether a failed `gh api` says the resource does not exist.

    `gh` puts the status line in stderr ("gh: Not Found (HTTP 404)"), which is
    the only signal available without changing proc.run's contract. Matched
    loosely and biased toward False: reading a real 404 as a transient failure
    only costs a re-probe next poll, while the reverse pins a wrong answer.
    """
    text = (exc.stderr or "").lower()
    return "404" in text or "not found" in text


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
        self.github = github or GitHub(config.reviewer_login)
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
        # (repo, number) pairs already warned about, so a fetch condition that
        # does not self-clear is reported on its transition rather than every
        # poll -- see _fetch_pr. Discarded the moment that fetch answers again.
        self._warned_fetch: set[tuple[str, int, bool]] = set()
        # Session name -> consecutive passes whose probe could not be answered.
        # Bounded like _probe_cache (pruned against the live list); see
        # _note_probe_taint for what it guards against.
        self._probe_taint: dict[str, int] = {}

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
        completed = (
            self.alissa.count_verdicts(task.ref) if task is not None
            else len(my_reviews)
        )

        converged = self._convergence_reason(my_reviews, task, pr.head_sha)
        if converged is not None:
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

        # Only checkable once a review task exists; before that there is
        # nowhere for a verdict to have been recorded.
        if task is not None and self.alissa.latest_verdict(task.ref) == VERDICT_APPROVE:
            return f"newest verdict envelope on {task.ref} reads approve"

        return None

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
        fetch per distinct PR as everything else. The exception is a probe no
        candidate could answer: nothing is cached from a pass that may have
        seen a partial truth, so it re-probes (stopping at the first failure)
        until it clears, and pages if it does not. Busy and in-grace sessions
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

        # Drop cached resolutions for sessions that are gone, so the cache
        # stays bounded by the live session count in a process that polls
        # forever. That is the ONLY invalidation needed because a bare
        # `review-pr-<n>` resolves as a function of its number and the repos
        # allowlist, and the allowlist cannot change under a running process.
        # (Not because names are unique per spawn -- the nonce belongs to this
        # daemon's OWN `review-<repo>-pr<n>-r<k>-<nonce>` names, which always
        # have a ledger row and so never reach this cache at all. The shape
        # that does reach it carries no nonce and is reusable across months.)
        live_names = {ses.name for ses in sessions}
        self._probe_cache = {
            name: target
            for name, target in self._probe_cache.items()
            if name in live_names
        }
        self._probe_taint = {
            name: count
            for name, count in self._probe_taint.items()
            if name in live_names
        }
        self._prune_warned_fetch(sessions)

        # Per-sweep memos. The PR fetch is keyed per distinct (repo, number);
        # the round count additionally keys on the task ref, because two spawns
        # of one PR can disagree on it (a round-1 row recorded before the review
        # task existed carries None). None = undecidable this pass.
        prs: FetchMemo = {}
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

    def _prune_warned_fetch(self, sessions: list[ManagedSession]) -> None:
        """Bound `_warned_fetch` the way `_probe_cache` and `_probe_taint` are.

        Those two are rebuilt against the live session names each sweep so
        they stay bounded in a process that polls forever. This one cannot
        ride that prune: it is keyed by (repo, number, condition), not by
        session -- so it gets the equivalent, computed from the PR keys still
        REACHABLE from a live session (its ledger row, or the probe target its
        name resolved to). Without it this set is the only member of the three
        that only ever shrinks when a fetch recovers, which for the conditions
        it reports (repo deleted, transferred, renamed) never happens.

        Reachability, not "fetched this pass", on purpose: a busy or in-grace
        session is never fetched, so pruning against the pass's fetches would
        drop its entry and re-warn every time a session flapped.
        """
        reachable = set()
        for ses in sessions:
            row = self.state.find_spawn_by_session(ses.name)
            if row is not None:
                reachable.add((row["repo"], row["number"]))
            target = self._probe_cache.get(ses.name)
            if target is not None:
                reachable.add(target)
        self._warned_fetch = {
            entry for entry in self._warned_fetch if entry[:2] in reachable
        }

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
        prs: FetchMemo,
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

        The probe is paid ONCE per session name -- unless a candidate fetch
        fails to answer, in which case that pass is discarded uncached and
        re-probed next attempt (see _probe_allowlist, and _note_probe_taint
        for the alarm that surfaces a failure which does not clear). Its
        answer -- the resolved (repo, number), or a sticky None for "could not
        be resolved" -- is cached on the watcher, because the resolution is a
        function of the number and the repos allowlist, and the allowlist
        cannot change under a running process. (It is NOT because the name is
        unique per spawn: the nonce that makes it so belongs to ledger-backed
        daemon names, which return above without touching this cache.)
        Without that, a session spared on an open PR -- the common case, a
        finished round waiting idle for the implementer -- re-ran the whole
        allowlist sweep on every poll for the life of the PR, which at 7 repos
        and a 30s interval is ~840 REST calls an hour for one session. The
        sticky None never re-probes within a process, so an ambiguity that
        later clears is not noticed until a restart -- the safe direction
        (spare, never guess), and the alternative is exactly the cost above.
        """
        if row is not None:
            return self._fetched_pr(ses, holdouts, prs, row["repo"], row["number"])

        ref = ses.ref
        if ref is None:  # pragma: no cover - list_review_sessions filters these
            return None

        if ses.name in self._probe_cache:
            target = self._probe_cache[ses.name]
        else:
            probed = self._probe_allowlist(ses, ref, prs)
            if isinstance(probed, _FetchFailed):
                self._note_probe_taint(ses, ref, probed.repo)
                self._hold(
                    holdouts, ses,
                    f"a probe fetch for PR #{ref.number} failed — re-probing "
                    f"next poll rather than pinning what it seemed to say",
                )
                return None
            self._probe_taint.pop(ses.name, None)  # this pass answered
            target = probed
            self._probe_cache[ses.name] = target
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
        return self._fetched_pr(ses, holdouts, prs, target[0], target[1])

    def _probe_allowlist(
        self,
        ses: ManagedSession,
        ref: SessionRef,
        prs: FetchMemo,
    ) -> tuple[str, int] | None | _FetchFailed:
        """Which watched repo owns this session's PR number.

        The expensive half of _resolve_pr. Three answers, for the same reason
        _fetch_pr has three -- the caller CACHES what this concludes, so "I
        could not tell" must not be representable as "I concluded nothing":

        * a (repo, number) target -- resolved, and worth pinning;
        * None -- the number definitely does not resolve to exactly one
          watched repo, itself a fact worth pinning;
        * FETCH_FAILED -- a candidate did not answer, so nothing this pass
          seemed to see can be trusted or cached.

        The third exists because a blip on the TRUE owner leaves exactly one
        apparent hit: a confident, wrong resolution that can get somebody
        else's live session killed. Before the cache existed the next poll
        re-probed and self-healed; refusing to cache an error-tainted pass is
        how that self-healing survives. It covers the negative too -- a pass
        in which the true owner blipped and nothing answered is not evidence
        that a number is unresolvable, and pinning it would spare the session
        forever on equally bad evidence.

        The candidate loop STOPS at the first failure. The pass's conclusion
        is already discarded by then, so the remaining fetches buy nothing but
        a memo entry for a hypothetical sibling -- and paying them anyway is
        what let a persistently failing candidate re-run the WHOLE allowlist
        sweep every poll, which is the per-poll cost the cache exists to
        remove.
        """
        candidates = self._name_candidates(ref)
        if not candidates:
            # Nothing to probe: stable until the allowlist itself changes,
            # which no poll can do to a running process.
            return None
        hits = []
        for repo_slug in candidates:
            # Quiet: probing a repo that simply has no PR #n is the expected
            # outcome for all but one candidate, not a failure worth a warning.
            found = self._fetch_pr(prs, repo_slug, ref.number, quiet=True)
            if isinstance(found, _FetchFailed):
                log.debug(
                    "reap sweep: a probe fetch for PR #%d (%s) failed at %s — "
                    "abandoning the pass rather than caching what it saw",
                    ref.number, ses.name, repo_slug,
                )
                # Carries the slug: this is the only place that knows WHICH
                # candidate went dark, and the page is useless without it.
                return _FetchFailed(repo_slug)
            if isinstance(found, PullRequest):
                hits.append(repo_slug)
        if len(hits) != 1:
            log.debug(
                "reap sweep: PR #%d of %s resolves to %d watched repo(s)",
                ref.number, ses.name, len(hits),
            )
            return None
        return (hits[0], ref.number)

    def _note_probe_taint(
        self, ses: ManagedSession, ref: SessionRef, repo: "str | None"
    ) -> None:
        """Count consecutive un-answerable probes, and page once past the floor.

        Refusing to cache an error-tainted pass is correct but unbounded on its
        own: while a candidate keeps failing, this session is re-probed every
        poll -- the per-poll allowlist sweep the cache exists to remove -- and
        silently, since probe fetches are quiet, the taint log is debug, and
        the holdout reason only reaches an operator once the session cap is
        exceeded. A candidate that HANGS rather than blips gets there easily:
        proc.run turns a 60s `gh api` timeout into a CommandError, correctly
        read as "did not answer", and that can persist indefinitely.

        Once per episode, like the cap alarm and for the same reason (this runs
        every poll). The counter resets on any pass that answers -- but note
        what that does and does not buy, because an earlier version of this
        docstring claimed the wrong thing: a pass that answers also CACHES the
        resolution, and a cached session short-circuits above without probing
        ever again, so the same live session cannot open a second episode. The
        reset keeps the map honest (and is what makes the count "consecutive"
        rather than cumulative); re-arming happens for a NEW session name, or
        after this one leaves the live list and the prune drops it.

        The unit is PROBE ATTEMPTS, not polls, and the page says so: a session
        that goes busy or falls back inside the reap grace returns above
        _resolve_pr and so neither increments nor resets the count, which then
        survives the gap and resumes. That is deliberate -- a repo that will
        not answer is no less stuck for the session having been briefly busy --
        but it means N attempts can span an arbitrary stretch of wall clock.
        """
        count = self._probe_taint.get(ses.name, 0) + 1
        self._probe_taint[ses.name] = count
        if count == PROBE_TAINT_ALARM:
            log.error(
                "REVIEWER SESSION PROBE STUCK: %s has failed to resolve on %d "
                "consecutive probe attempts because watched repo %s will not "
                "answer for PR #%d — every attempt re-sweeps the allowlist up "
                "to that repo, and the session cannot be resolved or reaped "
                "until it clears",
                ses.name, count, repo or "(unknown)", ref.number,
            )

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
        prs: FetchMemo,
        repo_slug: str,
        number: int,
        *,
        quiet: bool = False,
    ) -> PullRequest | None | _FetchFailed:
        """One memoized PR fetch for the sweep, per distinct (repo, number).

        Three answers, and the third is why this is not simply
        `PullRequest | None`: the PR (a definite yes), None (a definite NO --
        GitHub said 404, that repo has no such PR), or FETCH_FAILED (the call
        did not answer). Collapsing the last two, as this did, makes a
        transient `gh` blip indistinguishable from "no such PR" -- harmless
        while every decision was retried next poll, but `_probe_allowlist`
        now CACHES its conclusion, so a blip would be pinned for the session's
        lifetime. See _probe_allowlist for what it does with the difference.

        `quiet` demotes the log to debug for the allowlist probe, where a miss
        is the expected answer for all but one candidate. RateLimited
        propagates so run_forever backs off instead of hammering the API once
        per session.
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
            missing = _is_not_found(exc)
            # `quiet` alone decides the level, deliberately: a 404 while
            # PROBING is the expected answer for all but one candidate, but a
            # 404 on a PR this daemon recorded in its own spawn ledger means
            # the repo was deleted, transferred or renamed under it -- an
            # operator fact, and the container runs at INFO.
            #
            # Warned ONCE per episode, though. This path re-fetches every
            # sweep by design (it is the fetch that can turn a session
            # reapable), and the conditions it reports -- a repo deleted or
            # renamed under the ledger, a candidate gone dark -- do not
            # self-clear, so an ungated warning is ~2,880 identical lines a
            # day per session at a 30s interval, in the same log as this
            # sweep's two ERROR pages. Same shape as _check_session_cap and
            # _note_probe_taint: fire on the transition, stay quiet while it
            # holds, re-arm when it clears.
            message = "no such PR" if missing else "could not fetch"
            # Keyed on the CONDITION, not just the PR. Rate-limiting alone was
            # the shape of the sibling alarms but not their content: both of
            # those key on what the condition IS (_check_session_cap on the
            # surviving-name set, _note_probe_taint reset by any pass that
            # answers). Keyed on (repo, number) alone, the FIRST fact about a
            # PR silences every later one about it forever -- and the two
            # facts this path reports are exactly the pair worth telling
            # apart: a transient failure (retry, look at the API) and then a
            # definite 404 on the ledger path, which means the repo was
            # deleted, transferred or renamed under the daemon. A blip before
            # a real failure is not exotic on something that polls forever;
            # FETCH_FAILED exists because blips happen.
            warned = (repo_slug, number, missing)
            if quiet:
                log.debug("reap sweep: %s %s#%d: %s", message, repo_slug, number, exc)
            elif warned not in self._warned_fetch:
                self._warned_fetch.add(warned)
                log.warning("reap sweep: %s %s#%d: %s", message, repo_slug, number, exc)
            prs[key] = None if missing else FETCH_FAILED
        else:
            # It answered -- re-arm both conditions for this PR.
            self._warned_fetch -= {(repo_slug, number, True), (repo_slug, number, False)}
        return prs[key]

    def _fetched_pr(
        self,
        ses: ManagedSession,
        holdouts: dict[str, str],
        prs: FetchMemo,
        repo_slug: str,
        number: int,
    ) -> PullRequest | None:
        """`_fetch_pr` narrowed to "the PR, or spare the session and say why".

        Both fetch-returning paths out of _resolve_pr go through here, because
        both used to `return self._fetch_pr(...)` straight through: a None fell
        to `continue` in the sweep without recording anything, so the cap alarm
        printed `(no reason recorded)` for it -- on exactly the path (a `gh`
        outage sparing several sessions at once) where the alarm most needs to
        explain itself.
        """
        found = self._fetch_pr(prs, repo_slug, number)
        if isinstance(found, PullRequest):
            return found
        self._hold(
            holdouts, ses,
            f"{repo_slug}#{number} could not be fetched — retrying next poll"
            if isinstance(found, _FetchFailed)
            else f"{repo_slug}#{number} no longer exists",
        )
        return None

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
            return len(self.github.my_reviews(pr.owner, pr.repo, pr.number))
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
            assignment=assignment, round=round_, cap=cap, session=name
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

        # Fatal: a mismatched identity silently breaks round counting.
        login = self.github.verify_identity()
        log.info("reviewing as GitHub user %s (from the gh token)", login)

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
