"""Alissa CLI access: locate the review task (CR2) and enqueue the fresh
reviewer session (orchestration P1)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .proc import CommandError, run, run_json

log = logging.getLogger(__name__)

# A review task is "open" while it can still receive a verdict.
OPEN_STATUSES = {"committed", "in_progress", "pending_validation", "todo"}

# CR6 verdict envelope outcomes.
VERDICT_APPROVE = "approve"
VERDICT_REQUEST_CHANGES = "request_changes"

# Envelope titles and bodies both read:
#   Review verdict: <org>/<repo>#<n> — request_changes (round 3, ...)
#   # Review verdict: <org>/<repo>#<n> — approve
# The separator is an em-dash in practice; en-dash and hyphen are accepted too
# so a hand-written envelope does not silently fail to parse.
# Kept to a single line on purpose: the verdict word sits on the same line as
# the "Review verdict:" lead-in, so matching across newlines could only pick up
# a later round's wording out of order.
_VERDICT_RE = re.compile(
    r"Review\s+verdict\s*:[^\n]*?[—–-]\s*(approve|request_changes)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Task:
    ref: str  # TASK-<taskNumber>
    title: str
    status: str

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


# The namespace loop.session_name() spawns reviewers into. Kept as the
# display-level answer to "is this session in the reviewer namespace?" (the
# console marks unmanaged sessions with it); the REAP path uses the stricter
# grammar below, because a prefix is not a strong enough claim of ownership to
# kill on.
REVIEW_SESSION_PREFIX = "review-"

# The reviewer-session GRAMMAR. The sweep kills sessions and the worker
# container is shared with other lanes (`develop-*`, `fix-*`, `maintain-*`,
# ...), so only a name that parses as one of the two shapes the review loop
# itself produces is ever a reap candidate; anything else is another daemon's
# (or a human's) and is never touched. Two shapes, both ours:
#
#   review-<repo>-pr<n>-r<k>-<nonce>   this daemon's spawns (loop.session_name)
#   review-pr-<n>[-r<k>]               the alissa-code-review skill's own
#                                      procedures (spawn-a-reviewer-session.md,
#                                      run-the-review-loop.md) -- hand-driven
#                                      rounds of the SAME loop. No spawn ledger
#                                      knows about those and nothing reaped
#                                      them: every session in the 2026-07-28
#                                      memory incident was of this shape.
#
# The daemon shape is matched with a non-greedy repo so a hyphenated repo name
# (`alissa-github-review-daemon`) still lands in the repo group, and it is
# anchored on the `-pr<n>-r<k>-<nonce>` tail that loop.session_name always
# emits. The skill shape carries no repo and no nonce -- that is the whole
# reason a bare `review-pr-<n>` needs the repos allowlist to be resolvable at
# all (see loop.ReviewWatcher._resolve_pr).
_DAEMON_SESSION_RE = re.compile(
    r"^review-(?P<repo>[a-z0-9-]+?)-pr(?P<number>\d+)-r(?P<round>\d+)-[0-9a-f]{4,16}$"
)
_SKILL_SESSION_RE = re.compile(
    r"^review-pr-(?P<number>\d+)(?:-r(?P<round>\d+))?$"
)


@dataclass(frozen=True)
class SessionRef:
    """What a reviewer session's NAME says about the round it is running.

    `repo` is the sanitized repo slug (`session_repo_slug`), not an
    `owner/repo`: session names never carry the owner. `repo` and `round` are
    None for the skill shape, which encodes neither.
    """

    number: int
    repo: "str | None" = None
    round: "int | None" = None


def session_repo_slug(repo: str) -> str:
    """The repo component of a session name: tmux-safe, lowercase, no dots.

    Shared by the producer (loop.session_name) and the parser below so the two
    can never drift -- a rename here that the matcher did not follow would make
    the daemon stop recognizing its own sessions.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", repo).strip("-").lower()


def parse_session_name(name: "str | None") -> "SessionRef | None":
    """Parse a reviewer session name, or None when it is not one of ours.

    None is the security-relevant answer: it is what keeps the sweep off every
    other lane's sessions in a shared container.
    """
    if not isinstance(name, str):
        return None
    for pattern in (_SKILL_SESSION_RE, _DAEMON_SESSION_RE):
        match = pattern.match(name)
        if match is None:
            continue
        parts = match.groupdict()
        round_ = parts.get("round")
        return SessionRef(
            number=int(parts["number"]),
            repo=parts.get("repo"),
            round=int(round_) if round_ else None,
        )
    return None


@dataclass(frozen=True)
class ManagedSession:
    name: str
    status: str  # the worker's view: "idle", "busy", ...
    # Epoch seconds of the session's last tmux activity. 0 when the CLI did
    # not report one -- treated as "long quiet", so a missing field can never
    # indefinitely immunize a session against the sweep.
    last_activity: float = 0.0

    @property
    def is_idle(self) -> bool:
        return self.status == "idle"

    @property
    def ref(self) -> "SessionRef | None":
        """What the name says about the round -- see parse_session_name."""
        return parse_session_name(self.name)


def _title_pattern(owner: str, repo: str, number: int) -> re.Pattern[str]:
    """CR2 title convention: `Review PR <org>/<repo>#<n> (TASK-<origin>)`."""
    return re.compile(
        rf"^Review PR\s+{re.escape(owner)}/{re.escape(repo)}#{number}\b",
        re.IGNORECASE,
    )


def is_review_task_for(owner: str, repo: str, number: int, task: "Task") -> bool:
    """Whether `task` is THE open CR2 review task for this PR.

    The one predicate, shared by the search (`find_review_task`, over a whole
    task list) and by the cache check (loop._review_task, over a single task
    read back by ref). They must agree: a cached ref that the search would not
    have returned is a mapping the daemon has to drop, and a divergence here
    would either pin a wrong task forever or re-fetch the corpus every pass
    while disagreeing with itself.
    """
    return bool(_title_pattern(owner, repo, number).match(task.title)) and task.is_open


def _task_from_row(row: object) -> "Task | None":
    """One task out of a CLI payload row, or None when it carries no usable ref.

    Shared by the list reader and the single-task reader so both agree on which
    field is the resolvable ref.
    """
    if not isinstance(row, dict):
        return None
    # `taskNumber` is the ref the API resolves; `taskSeq` is a display
    # ordinal and 404s as `TASK-<seq>`.
    number = row.get("taskNumber")
    if number is None:
        return None
    title = row.get("title")
    status = row.get("status")
    return Task(
        ref=f"TASK-{number}",
        title=title if isinstance(title, str) else "",
        status=status if isinstance(status, str) else "",
    )


@dataclass(frozen=True)
class TaskDetail:
    """One task as `alissa task get` sees it: the task itself, plus everything
    the decide path reads off its CR6 verdict evidence.

    All three travel together because they come out of ONE payload. The decide
    path needs every one of them (is this still the PR's open review task? how
    many rounds has it recorded? what did the newest round decide?), and
    `_count_verdicts` and `_newest_verdict` are deliberately written as mirrors
    over the same evidence array -- so reading them apart would mean fetching
    and re-parsing the same task two and three times per PR per poll, which is
    the exact cost this whole path exists to stop paying.

    `verdict` is None when no envelope on the task parses -- the normal round-1
    case, indistinguishable here from "no verdict of record yet", which is what
    the caller wants it to mean anyway.
    """

    task: Task
    verdicts: int
    verdict: "str | None"


class Alissa:
    def list_tasks(self) -> list[Task]:
        """EVERY non-terminal task owned by this actor -- the expensive call.

        `alissa task list` (CLI 0.1.0) exposes no server-side narrowing at all:
        its only flags are `--json` and `--include-terminal`. Omitting the
        latter is therefore the whole of the available filtering, and it is
        already the default here -- validated and cancelled tasks never come
        back. What remains is the actor's live corpus (hundreds of tasks,
        ~250 KB), so the daemon's job is to call this RARELY rather than to
        call it narrowly: see loop._review_task (persisted PR -> task mapping)
        and loop._pass_task_list (at most one fetch per poll pass).
        """
        data = run_json(["alissa", "task", "list", "--json"], timeout=90) or []
        tasks = []
        for row in data if isinstance(data, list) else []:
            task = _task_from_row(row)
            if task is not None:
                tasks.append(task)
        return tasks

    def get_task(self, ref: str) -> "TaskDetail | None":
        """Read ONE task by ref: title, status and verdict count in one call.

        This is what makes a cached PR -> review-task mapping usable. Resolving
        the task by ref costs a single-task fetch; resolving it by searching
        titles costs the actor's entire corpus, which is the read this whole
        path exists to stop paying every poll.

        None means "could not be read" and NOTHING more -- a deleted task and a
        transient CLI failure are indistinguishable from here, so a caller must
        not treat None as proof that a cached mapping is wrong (see
        loop._review_task, which keeps the row and falls back to the search).
        Never raises: the daemon polls forever and this runs inside every pass.
        """
        try:
            data = run_json(["alissa", "task", "get", ref, "--json"], timeout=90)
        except CommandError as exc:
            log.warning("could not read task %s: %s", ref, exc)
            return None
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure reading task %s", ref)
            return None

        try:
            task = _task_from_row(data)
            if task is None:
                return None
            return TaskDetail(
                task=task,
                verdicts=self._count_verdicts(data),
                verdict=self._newest_verdict(data),
            )
        except Exception:  # pragma: no cover - defence in depth
            log.exception("could not parse task payload for %s", ref)
            return None

    def find_review_task(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        tasks: "list[Task] | None" = None,
    ) -> Task | None:
        """CR2: exactly one review task per PR. Reuse it across rounds (CR7).

        `tasks` supplies a corpus the caller already fetched, so several PRs
        missing the cache in the SAME poll pass share one list call instead of
        issuing an identical one each (the observed 2-4 same-second bursts).
        Omitted -- the console-script path, and any caller with no pass to
        scope to -- fetches its own.
        """
        pool = self.list_tasks() if tasks is None else tasks
        matches = [t for t in pool if is_review_task_for(owner, repo, number, t)]

        if not matches:
            return None
        if len(matches) > 1:
            # Several verdicts on one task are fine; several tasks per PR are not.
            log.warning(
                "CR2 violation: %d open review tasks for %s/%s#%d (%s) -- using %s",
                len(matches),
                owner,
                repo,
                number,
                ", ".join(t.ref for t in matches),
                matches[0].ref,
            )
        return matches[0]

    def latest_verdict(self, task_ref: str) -> str | None:
        """The newest CR6 verdict envelope on a review task, or None.

        Returns VERDICT_APPROVE / VERDICT_REQUEST_CHANGES. This is the verdict
        of record: reviewers post comment-mode reviews, so the GitHub review
        state is always COMMENTED and cannot express approval at all.

        Never raises. The daemon polls forever and this runs inside every pass,
        so absent, empty or malformed evidence degrades to "no verdict" rather
        than taking the loop down.
        """
        try:
            data = run_json(["alissa", "task", "get", task_ref, "--json"], timeout=90)
        except CommandError as exc:
            log.warning("could not read verdict evidence for %s: %s", task_ref, exc)
            return None
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure reading verdict evidence for %s", task_ref)
            return None

        try:
            return self._newest_verdict(data)
        except Exception:  # pragma: no cover - defence in depth
            log.exception("could not parse verdict evidence for %s", task_ref)
            return None

    @staticmethod
    def _newest_verdict(payload: object) -> str | None:
        """Pick the newest parseable verdict out of a task's evidence array.

        Every layer is optional by design -- the payload shape is whatever the
        CLI printed, and a task with no evidence is the normal round-1 case.
        """
        if not isinstance(payload, dict):
            return None
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            return None

        found: list[tuple[str, str]] = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            content = item.get("markdownContent")
            for blob in (title, content):
                if not isinstance(blob, str):
                    continue
                match = _VERDICT_RE.search(blob)
                if match:
                    created = item.get("createdAt")
                    found.append(
                        (created if isinstance(created, str) else "", match.group(1).lower())
                    )
                    break

        if not found:
            return None
        # ISO-8601 timestamps sort lexicographically. Undated evidence sorts
        # first (empty string), so a dated envelope always wins over one that
        # lost its timestamp.
        return max(found, key=lambda pair: pair[0])[1]

    def count_verdicts(self, task_ref: str) -> int:
        """How many CR6 verdict envelopes are on the review task.

        One envelope per completed round (CR7, append-only), so this is the
        authoritative round count -- unlike the GitHub review count it cannot be
        thrown off by an empty-bodied review or two reviews in one cycle. Never
        raises: absent, empty, or malformed evidence degrades to 0 (round 1)
        rather than taking the loop down.
        """
        try:
            data = run_json(["alissa", "task", "get", task_ref, "--json"], timeout=90)
        except CommandError as exc:
            log.warning("could not read verdict evidence for %s: %s", task_ref, exc)
            return 0
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure reading verdict evidence for %s", task_ref)
            return 0
        try:
            return self._count_verdicts(data)
        except Exception:  # pragma: no cover - defence in depth
            log.exception("could not count verdict evidence for %s", task_ref)
            return 0

    @staticmethod
    def _count_verdicts(payload: object) -> int:
        """Count evidence items carrying a verdict envelope. Mirrors
        `_newest_verdict`'s tolerant parsing -- one match per item at most."""
        if not isinstance(payload, dict):
            return 0
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            return 0
        count = 0
        for item in evidence:
            if not isinstance(item, dict):
                continue
            for blob in (item.get("title"), item.get("markdownContent")):
                if isinstance(blob, str) and _VERDICT_RE.search(blob):
                    count += 1
                    break
        return count

    def enqueue_reviewer(
        self,
        *,
        session: str,
        directive: str,
        cwd: Path,
        agent: str,
        task_ref: str | None,
        dry_run: bool = False,
    ) -> None:
        argv = [
            "alissa",
            "tmux",
            "queue",
            "add",
            session,
            "--agent",
            agent,
            "--cwd",
            str(cwd),
        ]
        if task_ref:
            argv += ["--task", task_ref]
        argv.append(directive)

        if dry_run:
            log.info("[dry-run] would enqueue: %s", " ".join(argv[:-1]) + " <directive>")
            return

        run(argv, timeout=60)

        # Reviewers are one-shot per round (CR3): once the session finishes and is
        # reaped, it must never be respawned. Make that explicit so a self-kill or
        # a daemon reap can't trigger a respawn loop. Best-effort — an older CLI
        # without `queue set` should not fail the enqueue.
        try:
            run(["alissa", "tmux", "queue", "set", session, "respawn", "off"],
                timeout=30, check=False)
        except CommandError:  # pragma: no cover - defence in depth
            log.warning("could not set respawn off for %s", session)

    def list_review_sessions(self) -> list[ManagedSession]:
        """The live reviewer-grammar managed sessions, from `alissa tmux ls`.

        The reap sweep's starting point. Unlike the review-requested search,
        the live session list cannot lose a finished session, so every reap
        candidate is reachable from here. `--live` because a session that is
        already gone (self-killed, or killed by an operator) holds no worker
        slot and needs no reap. Raises CommandError upward -- the sweep skips
        this pass and tries again next poll.

        The filter is `parse_session_name`, not the bare `review-` prefix: this
        list is what the sweep kills from, the container is shared with other
        daemons, and a name that does not parse as a reviewer session is never
        enumerated here at all -- so no later bug in the sweep can reach one.
        """
        data = run_json(["alissa", "tmux", "ls", "--json", "--live"], timeout=60) or []
        sessions = []
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            if isinstance(name, str) and parse_session_name(name) is not None:
                last = row.get("lastActivity")
                sessions.append(
                    ManagedSession(
                        name=name,
                        status=str(row.get("status") or ""),
                        last_activity=float(last) if isinstance(last, (int, float)) else 0.0,
                    )
                )
        return sessions

    def kill_session(self, session: str) -> None:
        """Kill ONE finished reviewer's managed session to free its worker slot.

        Per-session, always: `alissa tmux kill <name>` and never a server-wide
        kill. The worker container is shared with every other lane, so a
        `kill-server` here would take down unrelated daemons' sessions along
        with the one that is actually finished. A test pins the absence of that
        verb across the whole package.

        Best-effort and idempotent-friendly: the session may already be gone (the
        reviewer self-killed), so a non-zero exit is not an error here. Dry-run
        is the caller's job (the sweep decides and logs before calling).
        """
        run(["alissa", "tmux", "kill", session], timeout=30, check=False)

    def add_repo_to_workspace(
        self, owner: str, repo: str, workspace_root: Path, *, dry_run: bool = False
    ) -> None:
        """Hub-ify a repo into the workspace (bare clone + main/ worktree) and
        record it in alissa-workspace.yaml. Idempotent per the CLI's contract."""
        argv = ["alissa", "code", "workspace", "add", f"{owner}/{repo}"]
        if dry_run:
            log.info("[dry-run] would run: %s (cwd=%s)", " ".join(argv), workspace_root)
            return

        log.info("hub-ifying %s/%s into %s", owner, repo, workspace_root)
        # Cloning a repo can be slow; the poll loop tolerates a long pass.
        run(argv, timeout=600, cwd=workspace_root)

    def worker_running(self) -> bool:
        """The queue only drains while `alissa worker` reconciles it."""
        try:
            out = run(["alissa", "worker", "status"], timeout=30, check=False)
        except CommandError:
            return False
        return "not running" not in out.lower() and "no worker" not in out.lower()
