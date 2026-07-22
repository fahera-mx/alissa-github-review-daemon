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
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .alissa import VERDICT_APPROVE, Alissa, Task
from .config import HUB_ADD, ON_MISSING_SKIP, Config
from .ghclient import GitHub, PullRequest, RateLimited, Review
from .proc import CommandError
from .state import State

log = logging.getLogger(__name__)

# A reviewer session that has not submitted after this long is presumed dead
# (skill failure mode: "reviewer session stalls"). The round is re-enqueued.
STALE_ROUND_SECONDS = 90 * 60

# The sweep only reaps a session that has been idle AND quiet this long. The
# GitHub review count increments the moment a review is submitted, but the
# reviewer still has close-out work after that (CR6 envelope, task status) --
# and a claude session parked at its prompt between turns reports "idle", so
# idleness alone cannot distinguish "between turns" from "done". Recent tmux
# activity can.
REAP_QUIET_SECONDS = 5 * 60

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

ROUND_1_DIRECTIVE = (
    "You are a PR REVIEWER, not an implementer. {assignment} "
    "Load the alissa-code-review skill and follow procedures/review-a-pr.md: "
    "hydrate the task and the PR it names, review per the rubric, post "
    "severity-tagged comments via gh pr review, record the verdict evidence, "
    "move the task to pending_validation. "
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
    + _CLOSE_THE_ROUND +
    "NEVER push commits, merge, or change PR state. "
    "Do NOT create further ali-* sessions. "
    + _RELEASE_SLOT
)

ESCALATION_COMMENT = (
    "**Review loop cap-out (CR9)** — {rounds} rounds ran on this PR without "
    "converging on `approve`. Per the alissa-code-review skill the loop does not "
    "run past the cap and never silently merges; this needs an operator decision "
    "(merge with a recorded waiver, direct specific fixes and re-enter with a "
    "fresh cap, or park it).\n\n"
    "Last verdict: `{last_state}` at `{sha}`."
)


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


def session_name(pr: PullRequest, round_: int) -> str:
    """A tmux-safe reviewer session name, unique per spawn.

    The `review-<repo>-pr<n>-r<round>` prefix stays human-readable, but a short
    random nonce is appended so a re-used or miscounted round number can never
    collide with a still-live session (a collision wedges the worker -- the
    original 'stuck' failure). Safe to be non-deterministic: the generated name
    is recorded in the spawn ledger and is what gets reaped / self-killed, so the
    daemon never re-derives it.
    """
    repo = re.sub(r"[^A-Za-z0-9-]", "-", pr.repo).strip("-").lower()
    return f"review-{repo}-pr{pr.number}-r{round_}-{secrets.token_hex(3)}"


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

        # CR9: never queue round cap+1.
        if completed >= self.config.round_cap:
            if self.state.escalated(pr.full_name, number, pr.head_sha):
                return Decision(Action.CAPPED, "already escalated", completed)
            self._escalate(pr, my_reviews[-1].state if my_reviews else "none", completed)
            return Decision(Action.ESCALATED, f"{completed} rounds, no approve", completed)

        round_ = completed + 1

        age = self.state.spawn_age(pr.full_name, number, round_)
        if age is not None and age < STALE_ROUND_SECONDS:
            return Decision(Action.IN_FLIGHT, f"round {round_} enqueued {int(age)}s ago", round_)
        if age is not None:
            log.warning(
                "%s round %d has been in flight %.0f min with no submitted review "
                "— re-enqueuing (reviewer session presumed stalled)",
                pr.slug,
                round_,
                age / 60,
            )

        return self._spawn(pr, round_, task)

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

    def sweep_sessions(self) -> None:
        """Kill the managed session of every finished round. Runs every poll.

        The predecessor of this sweep ran inside evaluate(), which is fed by
        the review-requested:@me search -- and submitting a review CLEARS the
        request, so a finished round's PR vanished from the search at exactly
        the moment its session became reapable; terminal (approved) rounds
        were never reaped and idle reviewer sessions accumulated in the
        worker. The sweep instead starts from the live session list, which
        cannot lose a finished session, and works back to the round via the
        spawn ledger. It must stay search-independent: never move it (back)
        into the evaluate() path.

        Every-poll cost, honestly: one `alissa tmux ls` when no review-*
        session is live; otherwise one PR fetch per distinct PR with a live
        idle quiet session, plus -- per distinct (PR, task ref) among its
        rows -- exactly one of `alissa task get <ref>` (the row carries a
        task ref) or the reviews fetch (it does not). The ledger ref is used
        deliberately instead of
        find_review_task: that would fetch the actor's ENTIRE task list per
        PR, and its open-status filter would drop a human-validated review
        task back onto the racier GitHub-count fallback. Only individual
        sessions are ever killed (`alissa tmux kill <name>`) -- never the
        server. Best-effort throughout: an undecidable session is spared and
        looked at again next poll.
        """
        try:
            sessions = self.alissa.list_review_sessions()
        except CommandError as exc:
            log.warning("reap sweep skipped: could not list sessions: %s", exc)
            return

        # Per-sweep memos. The PR fetch is keyed per distinct PR; the round
        # count additionally keys on the task ref, because two spawns of one
        # PR can disagree on it (a round-1 row recorded before the review
        # task existed carries None). None = undecidable this pass.
        prs: dict[tuple[str, int], PullRequest | None] = {}
        completed_cache: dict[tuple[str, int, str | None], float | None] = {}

        for ses in sessions:
            if not ses.is_idle:
                # A busy session is still doing something (reviewing, or
                # closing out its round) -- never yank the slot from under it.
                continue
            if time.time() - ses.last_activity < REAP_QUIET_SECONDS:
                # Idle but recently active: likely mid-close-out (the review
                # is submitted before the envelope and task move land). Wait
                # for a real quiet period; see REAP_QUIET_SECONDS.
                continue
            row = self.state.find_spawn_by_session(ses.name)
            if row is None:
                # Not in our ledger: another workspace's daemon (or a human)
                # owns it. Not ours to judge.
                continue
            pr_key = (row["repo"], row["number"])
            if pr_key not in prs:
                prs[pr_key] = self._sweep_pr(row["repo"], row["number"])
            pr = prs[pr_key]
            if pr is None:
                continue  # fetch failed -- spare everything on this PR
            key = (row["repo"], row["number"], row["task_ref"])
            if key not in completed_cache:
                completed_cache[key] = self._completed_rounds(pr, row["task_ref"])
            completed = completed_cache[key]
            if completed is None or row["round"] > completed:
                continue  # undecidable, or the round is still in flight
            if self.config.dry_run:
                log.info(
                    "[dry-run] would reap finished reviewer session %s (round %d done)",
                    ses.name, row["round"],
                )
                continue
            try:
                self.alissa.kill_session(ses.name)
            except Exception:  # pragma: no cover - defence in depth
                log.exception("failed to reap session %s", ses.name)
                continue
            # Bookkeeping only -- deliberately never consulted before a kill.
            # The live list is the authority; gating on the reaps table would
            # spare any session killed behind the ledger's back.
            self.state.record_reap(ses.name)
            log.info(
                "reaped finished reviewer session %s (round %d done)",
                ses.name, row["round"],
            )

    def _sweep_pr(self, repo_slug: str, number: int) -> PullRequest | None:
        """One PR fetch for the sweep; the caller memoizes per distinct PR.

        None = the fetch failed -- every session on that PR is spared this
        pass and looked at again next poll. RateLimited propagates so
        run_forever backs off instead of hammering the API once per session.
        """
        owner, _, repo = repo_slug.partition("/")
        try:
            return self.github.pull_request(owner, repo, number)
        except RateLimited:
            raise
        except CommandError as exc:
            log.warning("reap sweep: could not fetch %s#%d: %s", repo_slug, number, exc)
            return None

    def _completed_rounds(self, pr: PullRequest, task_ref: str | None) -> float | None:
        """How many rounds of this PR are over, judged from GitHub/task state.

        A closed or merged PR terminates every round, so it reports infinity.
        Otherwise rounds completed = verdict envelopes on the review task (the
        authoritative round record), addressed by the task ref the ledger
        captured at spawn time -- NOT find_review_task, which would fetch the
        whole task list and whose open-status filter loses validated tasks.
        The substantive-review count is the fallback only for spawns recorded
        before any review task existed. None means "could not tell" -- the
        sweep spares the session and retries next poll.
        """
        if pr.is_terminal:
            return float("inf")
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

    def _spawn(self, pr: PullRequest, round_: int, task: Task | None) -> Decision:
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
            assignment=assignment, round=round_, cap=self.config.round_cap, session=name
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

        return Decision(
            Action.SPAWNED,
            f"session {name} → {task.ref if task else 'no task'}",
            round_,
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

    def _escalate(self, pr: PullRequest, last_state: str, rounds: int) -> None:
        body = ESCALATION_COMMENT.format(
            rounds=rounds, last_state=last_state.lower(), sha=pr.head_sha[:8]
        )
        log.error("CAP-OUT %s after %d rounds — escalating to operator", pr.slug, rounds)

        if self.config.dry_run:
            log.info("[dry-run] would comment on %s:\n%s", pr.slug, body)
            return

        try:
            self.github.comment(pr.owner, pr.repo, pr.number, body)
        except CommandError as exc:
            log.error("could not post escalation comment on %s: %s", pr.slug, exc)
        self.state.record_escalation(pr.full_name, pr.number, pr.head_sha)

    # -- polling -----------------------------------------------------------

    def poll_once(self) -> list[tuple[str, Decision]]:
        # Sweep BEFORE evaluating: a full worker is exactly when a fresh spawn
        # needs the slot a finished session is squatting on. Deliberately not
        # inside the per-request loop below — the sweep must reach sessions
        # whose PR no longer appears in the search at all.
        self.sweep_sessions()

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
        return results

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
