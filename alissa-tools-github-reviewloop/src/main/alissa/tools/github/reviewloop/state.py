"""Local spawn ledger.

Deliberately thin: GitHub is the source of truth for how many rounds have run
(one submitted review per round). This table exists only to stop the daemon
double-spawning a reviewer while a round is still in flight, to map a live
session name back to the round it was spawned for (so the reap sweep can tell
a finished round's session from an in-flight one), and to remember that a
cap-out was already escalated. The ledger tolerates sessions dying or being
killed behind its back: a reap record is bookkeeping, never a precondition.
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
    session    TEXT    NOT NULL PRIMARY KEY,
    task_ref   TEXT,
    spawned_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS spawns_by_round ON spawns (repo, number, round);

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
        stale = self._spawns_keyed_by_round()
        if stale:
            self._db.execute("ALTER TABLE spawns RENAME TO spawns_v0")
        self._db.executescript(SCHEMA)
        if stale:
            self._db.execute(
                "INSERT OR REPLACE INTO spawns "
                "(repo, number, round, head_sha, session, task_ref, spawned_at) "
                "SELECT repo, number, round, head_sha, session, task_ref, spawned_at "
                "FROM spawns_v0"
            )
            self._db.execute("DROP TABLE spawns_v0")
        self._db.commit()

    def _spawns_keyed_by_round(self) -> bool:
        """True when `spawns` still has the pre-0.8 (repo, number, round) key.

        That key made `record_spawn` overwrite the row when a stalled round
        was re-enqueued, orphaning the original -- possibly still-live --
        session so the reap sweep spared it forever as "not ours". The key is
        now the session name (unique per spawn, thanks to the nonce). SQLite
        cannot alter a primary key in place, so an old table is renamed and
        copied over exactly once on open.
        """
        info = self._db.execute("PRAGMA table_info(spawns)").fetchall()
        if not info:
            return False  # fresh database, nothing to migrate
        return [r["name"] for r in info if r["pk"]] != ["session"]

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_spawn(self, repo: str, number: int, round_: int) -> sqlite3.Row | None:
        """The NEWEST spawn recorded for this round, or None.

        A stalled round can be re-enqueued, so one round may have several
        spawns; aging and the in-flight check are about the latest attempt.
        """
        return self._db.execute(
            "SELECT * FROM spawns WHERE repo=? AND number=? AND round=? "
            "ORDER BY spawned_at DESC, rowid DESC LIMIT 1",
            (repo, number, round_),
        ).fetchone()

    def find_spawn_by_session(self, session: str) -> sqlite3.Row | None:
        """The spawn a live session name belongs to, or None if it is not ours.

        Session names carry a random nonce, so a name maps to at most one
        spawn. The reap sweep starts from live tmux state and uses this to
        recover (repo, number, round); a session with no row (another
        workspace's daemon, or a hand-started one) is not ours to judge.
        """
        return self._db.execute(
            "SELECT * FROM spawns WHERE session=?", (session,)
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
