"""Local spawn ledger.

Deliberately thin: GitHub is the source of truth for how many rounds have run
(one submitted review per round). This table exists only to stop the daemon
double-spawning a reviewer while a round is still in flight, and to remember
that a cap-out was already escalated.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS spawns (
    repo       TEXT    NOT NULL,
    number     INTEGER NOT NULL,
    round      INTEGER NOT NULL,
    head_sha   TEXT    NOT NULL,
    session    TEXT    NOT NULL,
    task_ref   TEXT,
    spawned_at INTEGER NOT NULL,
    PRIMARY KEY (repo, number, round)
);

CREATE TABLE IF NOT EXISTS escalations (
    repo         TEXT    NOT NULL,
    number       INTEGER NOT NULL,
    head_sha     TEXT    NOT NULL,
    escalated_at INTEGER NOT NULL,
    PRIMARY KEY (repo, number, head_sha)
);

CREATE TABLE IF NOT EXISTS reaps (
    session   TEXT    NOT NULL PRIMARY KEY,
    reaped_at INTEGER NOT NULL
);
"""


class State:
    def __init__(self, path: Path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_spawn(self, repo: str, number: int, round_: int) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM spawns WHERE repo=? AND number=? AND round=?",
            (repo, number, round_),
        ).fetchone()

    def spawn_age(self, repo: str, number: int, round_: int) -> float | None:
        """Seconds since round `round_` was enqueued, or None if never spawned."""
        row = self.get_spawn(repo, number, round_)
        return None if row is None else time.time() - row["spawned_at"]

    def record_spawn(
        self,
        *,
        repo: str,
        number: int,
        round_: int,
        head_sha: str,
        session: str,
        task_ref: str | None,
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO spawns "
            "(repo, number, round, head_sha, session, task_ref, spawned_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (repo, number, round_, head_sha, session, task_ref, int(time.time())),
        )
        self._db.commit()

    def is_reaped(self, session: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM reaps WHERE session=?", (session,)
        ).fetchone()
        return row is not None

    def record_reap(self, session: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO reaps (session, reaped_at) VALUES (?,?)",
            (session, int(time.time())),
        )
        self._db.commit()

    def escalated(self, repo: str, number: int, head_sha: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM escalations WHERE repo=? AND number=? AND head_sha=?",
            (repo, number, head_sha),
        ).fetchone()
        return row is not None

    def record_escalation(self, repo: str, number: int, head_sha: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO escalations "
            "(repo, number, head_sha, escalated_at) VALUES (?,?,?,?)",
            (repo, number, head_sha, int(time.time())),
        )
        self._db.commit()
