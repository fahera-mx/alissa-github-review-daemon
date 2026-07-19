"""Subprocess helpers. Everything shells out to `gh` and `alissa` so both keep
their own auth handling; we never touch tokens ourselves."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Sequence

log = logging.getLogger(__name__)


class CommandError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{argv[0]} exited {returncode}: {stderr.strip()[:400]}")


def run(
    argv: Sequence[str],
    *,
    timeout: int = 60,
    check: bool = True,
    cwd: "str | os.PathLike[str] | None" = None,
) -> str:
    """Run a command, return stdout. Never uses shell=True."""
    log.debug("exec: %s", " ".join(argv))
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(argv, -1, f"timed out after {timeout}s") from exc

    if check and proc.returncode != 0:
        raise CommandError(argv, proc.returncode, proc.stderr)
    return proc.stdout


def run_json(argv: Sequence[str], *, timeout: int = 60):
    """Run a command whose stdout is JSON."""
    out = run(argv, timeout=timeout).strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise CommandError(argv, 0, f"expected JSON, got: {out[:300]}") from exc
