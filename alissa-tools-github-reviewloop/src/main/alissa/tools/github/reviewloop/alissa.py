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
