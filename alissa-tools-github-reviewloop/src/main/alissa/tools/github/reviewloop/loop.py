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

ROUND_1_DIRECTIVE = (
    "You are a PR REVIEWER, not an implementer. {assignment} "
    "Load the alissa-code-review skill and follow procedures/review-a-pr.md: "
    "hydrate the task and the PR it names, review per the rubric, post "
    "severity-tagged comments via gh pr review, record the verdict evidence, "
    "move the task to pending_validation. "
    "NEVER push commits, merge, or change PR state. "
    "Do NOT create further ali-* sessions."
)

ROUND_K_DIRECTIVE = (
    "You are a PR REVIEWER, not an implementer — round {round} of a review loop "
    "(cap {cap}). {assignment} "
    "Load the alissa-code-review skill and follow procedures/review-a-pr.md "
    "including its round-k section: verify the triage of every prior finding, "
    "verify the fixes, sweep the new diff with the full rubric, record a "
    "round-{round} verdict envelope, move the task to pending_validation. "
    "NEVER push commits, merge, or change PR state. "
    "Do NOT create further ali-* sessions."
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
    """tmux-safe, unique per (repo, pr, round)."""
    repo = re.sub(r"[^A-Za-z0-9-]", "-", pr.repo).strip("-").lower()
    return f"review-{repo}-pr{pr.number}-r{round_}"


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
        completed = len(my_reviews)

        # Looked up here rather than inside _spawn: convergence needs the ref
        # too. _spawn still handles `task is None` exactly as before.
        task = self.alissa.find_review_task(owner, repo, number)

        converged = self._convergence_reason(my_reviews, task)
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

    def _convergence_reason(self, my_reviews: list[Review], task: Task | None) -> str | None:
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
        """
        if my_reviews and my_reviews[-1].state == "APPROVED":
            return "last GitHub review state is APPROVED"

        # Only checkable once a review task exists; before that there is
        # nowhere for a verdict to have been recorded.
        if task is not None and self.alissa.latest_verdict(task.ref) == VERDICT_APPROVE:
            return f"newest verdict envelope on {task.ref} reads approve"

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

        template = ROUND_1_DIRECTIVE if round_ == 1 else ROUND_K_DIRECTIVE
        directive = template.format(
            assignment=assignment, round=round_, cap=self.config.round_cap
        )

        hub, problem = self._ensure_hub(pr)
        if problem is not None:
            return Decision(Action.SKIPPED, problem, round_)

        name = session_name(pr, round_)
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
            try:
                self.poll_once()
                backoff = self.config.poll_interval
            except RateLimited as exc:
                backoff = min(backoff * 2, 900)
                log.warning("rate limited (%s) — backing off %ds", exc, backoff)
            except CommandError as exc:
                backoff = min(backoff * 2, 900)
                log.error("poll failed: %s — retrying in %ds", exc, backoff)
            except KeyboardInterrupt:
                log.info("stopping")
                return
            time.sleep(backoff)
