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


def _title_pattern(owner: str, repo: str, number: int) -> re.Pattern[str]:
    """CR2 title convention: `Review PR <org>/<repo>#<n> (TASK-<origin>)`."""
    return re.compile(
        rf"^Review PR\s+{re.escape(owner)}/{re.escape(repo)}#{number}\b",
        re.IGNORECASE,
    )


class Alissa:
    def list_tasks(self) -> list[Task]:
        data = run_json(["alissa", "task", "list", "--json"], timeout=90) or []
        tasks = []
        for row in data:
            # `taskNumber` is the ref the API resolves; `taskSeq` is a display
            # ordinal and 404s as `TASK-<seq>`.
            number = row.get("taskNumber")
            if number is None:
                continue
            tasks.append(
                Task(
                    ref=f"TASK-{number}",
                    title=row.get("title", ""),
                    status=row.get("status", ""),
                )
            )
        return tasks

    def find_review_task(self, owner: str, repo: str, number: int) -> Task | None:
        """CR2: exactly one review task per PR. Reuse it across rounds (CR7)."""
        pattern = _title_pattern(owner, repo, number)
        matches = [t for t in self.list_tasks() if pattern.match(t.title) and t.is_open]

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
