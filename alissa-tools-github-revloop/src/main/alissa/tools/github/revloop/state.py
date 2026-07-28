"""Local spawn ledger.

Deliberately thin: GitHub is the source of truth for how many rounds have run
(one submitted review per round). This table exists only to stop the daemon
double-spawning a reviewer while a round is still in flight, to map a live
session name back to the round it was spawned for (so the reap sweep can tell
a finished round's session from an in-flight one), to remember that a cap-out
was already escalated, and to count the operator re-entry acks that raise a
single PR's effective cap. The ledger tolerates sessions dying or being
killed behind its back: a reap record is bookkeeping, never a precondition.

The `poll_snapshots` table is a different animal from the ledger above: it
records what each poll pass OBSERVED, not what the daemon must remember to
avoid double-work. One row per pass carries the timing, the candidate count,
the decision-summary counts, and a compact JSON column of the pass's per-item
stages -- everything a future console sidecar (the UI-1 pattern ported from
the devloop) needs to render live daemon state without spending a single
GitHub API call of its own. It is self-bounding: the newest SNAPSHOT_RETENTION
rows are kept and older ones pruned on every write. `read_snapshots` is the
reader that sidecar will consume (newest first, `stages` decoded back from
JSON). Adding the table is itself the migration for an existing database --
`CREATE TABLE IF NOT EXISTS` creates it on the next open of a DB that predates
it, alongside the untouched legacy ledgers.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

# Poll-snapshot retention: the newest N rows are kept, older ones pruned on
# every write. Fixed, not a config key -- `poll_snapshots` is an observation
# buffer for a future console sidecar, and a bounded ring is all it needs (it
# reads the recent tail). A change to this constant is a change to the
# observable buffer size, so it is pinned by a test.
SNAPSHOT_RETENTION = 1000

# Shared between SCHEMA and the migration so the two can never drift.
_SPAWNS_TABLE = """
CREATE TABLE IF NOT EXISTS spawns (
    repo       TEXT    NOT NULL,
    number     INTEGER NOT NULL,
    round      INTEGER NOT NULL,
    head_sha   TEXT    NOT NULL,
    session    TEXT    NOT NULL PRIMARY KEY,
    task_ref   TEXT,
    spawned_at INTEGER NOT NULL
)"""

SCHEMA = f"""
{_SPAWNS_TABLE};

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

CREATE TABLE IF NOT EXISTS grants (
    repo       TEXT    NOT NULL,
    number     INTEGER NOT NULL,
    comment_id INTEGER NOT NULL,
    author     TEXT    NOT NULL,
    rounds     INTEGER NOT NULL,
    granted_at INTEGER NOT NULL,
    PRIMARY KEY (repo, number, comment_id)
);

CREATE TABLE IF NOT EXISTS pings (
    repo      TEXT    NOT NULL,
    number    INTEGER NOT NULL,
    kind      TEXT    NOT NULL,
    pinged_at INTEGER NOT NULL,
    PRIMARY KEY (repo, number, kind)
);

CREATE TABLE IF NOT EXISTS poll_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER NOT NULL,
    duration_ms       INTEGER NOT NULL,
    candidates        INTEGER NOT NULL,
    spawned           INTEGER NOT NULL,
    stale_reenqueued  INTEGER NOT NULL,
    in_flight         INTEGER NOT NULL,
    deferred          INTEGER NOT NULL,
    converged         INTEGER NOT NULL,
    capped            INTEGER NOT NULL,
    escalated         INTEGER NOT NULL,
    skipped           INTEGER NOT NULL,
    reaped            INTEGER NOT NULL,
    stages_json       TEXT    NOT NULL
);
"""


class State:
    def __init__(self, path: Path, *, read_only: bool = False):
        """Open the ledger. The daemon opens it read-write (creating the file,
        applying the schema, migrating an old `spawns` key); a read-only
        CONSUMER -- the console sidecar -- must not do any of that.

        `read_only` connects through the `mode=ro` URI, which cannot create the
        database and raises `sqlite3.OperationalError` when it does not exist.
        That is the point: a console pointed at a workspace with no daemon
        state must report "no state here", not silently CREATE a state.db (and
        thereby render an empty, healthy-looking dashboard the operator cannot
        tell from an idle daemon), and must never run the migration on a
        database the daemon owns.

        The URI is built with `as_uri()`, never by interpolating the path into
        an f-string: sqlite parses a `file:` URI, so an unescaped `#` truncates
        the filename at a fragment, `?` at the query, and `%XX` is
        percent-decoded. Any of the three in `--workspace-root` would yield a
        DIFFERENT file and -- for `#` and `?` -- one with no `mode` parameter
        left, silently falling back to `rwc`: read-only mode creating a
        database, at a path that is not even the one asked for. `as_uri()`
        percent-encodes everything but the separator, so the guarantee holds
        for any path an operator can type.
        """
        path = Path(path).expanduser()
        if read_only:
            uri = Path(path).absolute().as_uri() + "?mode=ro"
            self._db = sqlite3.connect(uri, uri=True)
            self._db.row_factory = sqlite3.Row
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        if self._spawns_keyed_by_round():
            self._migrate_spawns()
        self._db.executescript(SCHEMA)
        self._db.commit()

    def _migrate_spawns(self) -> None:
        """Re-key an old round-keyed `spawns` by session, in ONE transaction.

        Deliberately not executescript (it COMMITs the open transaction before
        running): a crash between the rename and the copy would otherwise
        leave an empty new `spawns` that no longer looks stale, stranding
        every row in spawns_v0 — an empty ledger makes the sweep spare every
        live session as "not ours". All-or-nothing instead: any failure rolls
        back to the untouched old table and the next open retries.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("ALTER TABLE spawns RENAME TO spawns_v0")
            self._db.execute(_SPAWNS_TABLE)
            self._db.execute(
                "INSERT OR REPLACE INTO spawns "
                "(repo, number, round, head_sha, session, task_ref, spawned_at) "
                "SELECT repo, number, round, head_sha, session, task_ref, spawned_at "
                "FROM spawns_v0"
            )
            self._db.execute("DROP TABLE spawns_v0")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

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

    def pinged(self, repo: str, number: int, kind: str) -> bool:
        """Whether this KIND of operator ping already went out for the PR.

        Kind is free-form TEXT (devloop's escalation-kind pattern): a caller
        narrows a kind's dedupe scope by folding identity into the string --
        e.g. one stalled ping per deferral episode, "stalled:<session>" (see
        loop.stalled_kind). Kept apart from `escalations`, whose key is
        (repo, number, head_sha) and whose rows page terminal states.
        """
        row = self._db.execute(
            "SELECT 1 FROM pings WHERE repo=? AND number=? AND kind=?",
            (repo, number, kind),
        ).fetchone()
        return row is not None

    def record_ping(self, repo: str, number: int, kind: str) -> None:
        """Idempotent per kind: OR IGNORE keeps the FIRST ping's timestamp,
        so `pinged_at` is an audit field for when the episode was first
        raised, not the most recent re-raise."""
        self._db.execute(
            "INSERT OR IGNORE INTO pings (repo, number, kind, pinged_at) "
            "VALUES (?,?,?,?)",
            (repo, number, kind, int(time.time())),
        )
        self._db.commit()

    def escalated(self, repo: str, number: int, head_sha: str) -> bool:
        """Whether this head has been paged at all. Not the whole dedupe story
        once re-entry grants exist -- a grant consumed without an approve is a
        new decision on the SAME head, deduped through the ping ledger; see
        loop._escalation_owed."""
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

    # -- operator re-entry grants ------------------------------------------
    #
    # One row per HONOURED operator ack comment (issue #42): a capped PR is
    # re-entered only by an explicit, allowlisted, bounded ack, and this table
    # is what makes the grant *counted* rather than inferred. The comment id is
    # the grant's identity, so the same ack is honoured exactly once however
    # many polls read it, and a second grant genuinely requires a second
    # comment. Rows are never pruned: the effective cap of a PR is
    # `round_cap + sum(rounds)` over its grants, so dropping one would silently
    # lower the cap and re-escalate a PR that was already re-entered.

    def record_grant(
        self, repo: str, number: int, comment_id: int, author: str, rounds: int
    ) -> bool:
        """Record one honoured ack. True when it is NEW (first sighting).

        INSERT OR IGNORE keyed on the comment id: re-reading the same ack on
        every poll must not re-grant its rounds, and the caller uses the return
        value to log/announce the grant exactly once.
        """
        cur = self._db.execute(
            "INSERT OR IGNORE INTO grants "
            "(repo, number, comment_id, author, rounds, granted_at) "
            "VALUES (?,?,?,?,?,?)",
            (repo, number, int(comment_id), author, int(rounds), int(time.time())),
        )
        self._db.commit()
        return cur.rowcount > 0

    def granted_rounds(self, repo: str, number: int) -> int:
        """Extra rounds this PR has been granted, summed over its acks."""
        row = self._db.execute(
            "SELECT COALESCE(SUM(rounds), 0) AS total FROM grants "
            "WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()
        return int(row["total"]) if row is not None else 0

    def newest_grant(self, repo: str, number: int) -> sqlite3.Row | None:
        """The most recently recorded ack for this PR, or None.

        The escalation names it ("the re-entry granted by @x was consumed"),
        and its timestamp is what `escalated_at` is compared against.
        """
        return self._db.execute(
            "SELECT * FROM grants WHERE repo=? AND number=? "
            "ORDER BY granted_at DESC, rowid DESC LIMIT 1",
            (repo, number),
        ).fetchone()

    def read_grants(
        self,
        repo: str | None = None,
        number: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Grant rows, newest first; narrowed to one PR when repo/number are
        given (the loop's use) and unfiltered for a console-style read."""
        sql = "SELECT repo, number, comment_id, author, rounds, granted_at FROM grants"
        params: tuple = ()
        if repo is not None and number is not None:
            sql += " WHERE repo=? AND number=?"
            params = (repo, number)
        sql += " ORDER BY granted_at DESC, rowid DESC"
        return self._read_rows(sql, limit, params)

    # -- poll snapshots (the console sidecar's exhaust buffer) -------------

    def record_snapshot(
        self,
        *,
        duration_ms: int,
        candidates: int,
        spawned: int = 0,
        stale_reenqueued: int = 0,
        in_flight: int = 0,
        deferred: int = 0,
        converged: int = 0,
        capped: int = 0,
        escalated: int = 0,
        skipped: int = 0,
        reaped: int = 0,
        stages: list[dict],
    ) -> None:
        """Append one poll-pass observation, then prune to the newest
        SNAPSHOT_RETENTION rows. `ts` is stamped here (wall-clock seconds,
        like every other row in this ledger); `stages` is the compact
        per-item list a future console reads back through read_snapshots,
        serialized to JSON. Purely observational -- written on every pass,
        dry-run included -- and pruned on write, so the table is
        self-bounding. The count kwargs default to 0 so a caller need only
        pass the ones a given pass produced.
        """
        self._db.execute(
            "INSERT INTO poll_snapshots "
            "(ts, duration_ms, candidates, spawned, stale_reenqueued, "
            "in_flight, deferred, converged, capped, escalated, skipped, "
            "reaped, stages_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(time.time()),
                duration_ms,
                candidates,
                spawned,
                stale_reenqueued,
                in_flight,
                deferred,
                converged,
                capped,
                escalated,
                skipped,
                reaped,
                json.dumps(stages, separators=(",", ":")),
            ),
        )
        # Prune on write: keep the newest SNAPSHOT_RETENTION rows by id. The
        # autoincrement id is monotonic across prunes, so "newest" is well
        # defined even when a wall-clock step would leave `ts` unordered.
        self._db.execute(
            "DELETE FROM poll_snapshots WHERE id NOT IN "
            "(SELECT id FROM poll_snapshots ORDER BY id DESC LIMIT ?)",
            (SNAPSHOT_RETENTION,),
        )
        self._db.commit()

    def read_snapshots(self, limit: int | None = None) -> list[dict]:
        """Poll snapshots newest-first, each with its per-item `stages` list
        decoded back from JSON (the round-trip counterpart of
        record_snapshot). `limit` caps the rows returned; None returns every
        retained row. This is the whole contract the future console depends
        on -- everything it needs is already here, so it makes no GitHub
        calls."""
        sql = "SELECT * FROM poll_snapshots ORDER BY id DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        out = []
        for row in self._db.execute(sql, params).fetchall():
            record = dict(row)
            record["stages"] = json.loads(record.pop("stages_json"))
            out.append(record)
        return out

    # -- console ledger bridge (read-only lists + the retry-now UPDATE) -----
    #
    # The console sidecar (webui) renders the spawn ledger, the escalation
    # table and the ping ledger directly -- the operator inbox, the session ->
    # PR mapping, and the retry-now action -- the way it reads poll_snapshots
    # through read_snapshots: no GitHub call, just the local tables.
    #
    # Rows come back newest-first, and every reader takes the same optional
    # `limit` read_snapshots does. Unlike poll_snapshots, `escalations` and
    # `pings` are NEVER pruned (they are per-head / per-episode dedupe keys the
    # daemon must keep), so a reader that returned all of them would grow an
    # operator inbox that never clears. Bounding belongs here, in SQL, not in
    # the caller's slice -- and, for `pings`, AFTER the kind filter rather than
    # before it, or the noisier telemetry kind evicts the operator pages.
    #
    # `spawns` is bounded differently on purpose: it is a lookup table, not a
    # display list, so it is selected by key rather than truncated by recency.

    def read_spawns(
        self,
        limit: int | None = None,
        *,
        sessions: "Iterable[str] | None" = None,
    ) -> list[dict]:
        """Spawn rows (one per enqueued reviewer round), newest first.

        `sessions` restricts the read to those session names. That is the
        bound the console's session->round pairing wants, and a recency bound
        (row count or time window) is NOT: a stale round whose session is
        still alive is deferred indefinitely (see loop._defer_stale_round --
        only an operator kill unblocks the respawn), so a live session's spawn
        row can be arbitrarily old. Truncating by recency therefore drops the
        pairing for exactly the wedged session an operator is looking for.
        Selecting by name bounds the read by the live-session count instead,
        at any row age. An empty collection reads nothing.
        """
        sql = (
            "SELECT repo, number, round, head_sha, session, task_ref, spawned_at "
            "FROM spawns"
        )
        params: tuple = ()
        if sessions is not None:
            names = tuple(sessions)
            if not names:
                return []
            sql += " WHERE session IN (%s)" % ",".join("?" * len(names))
            params = names
        sql += " ORDER BY spawned_at DESC, number DESC, round DESC"
        return self._read_rows(sql, limit, params)

    def read_escalations(self, limit: int | None = None) -> list[dict]:
        """Cap-out escalations (the terminal half of the operator inbox),
        newest first. Keyed per head_sha: a fresh push after a cap-out is a new
        row, so the console can show that the PR capped out again."""
        return self._read_rows(
            "SELECT repo, number, head_sha, escalated_at "
            "FROM escalations ORDER BY escalated_at DESC, number DESC",
            limit,
        )

    def read_pings(
        self,
        limit: int | None = None,
        *,
        kind_prefix: str | None = None,
    ) -> list[dict]:
        """Operator-ping rows, newest first. `kind` is free-form and carries
        the episode identity (`stalled:<session>`,
        `activity-deferred:<session>`).

        `kind_prefix` selects one kind IN SQL, so `limit` bounds the rows the
        caller actually wants. Filtering after the limit instead would let the
        other kind evict them: only a deferral episode that outlasts
        STALLED_DEFER_MULTIPLE stale windows writes a `stalled:` row, while
        EVERY episode writes an `activity-deferred:` one, so the kind the
        console does not page on is structurally the more numerous. Matched
        with `substr`, not `LIKE`: the prefix is a literal, and `LIKE` would
        need `%`/`_` escaped to keep it one.
        """
        sql = "SELECT repo, number, kind, pinged_at FROM pings"
        params: tuple = ()
        if kind_prefix is not None:
            sql += " WHERE substr(kind, 1, ?) = ?"
            params = (len(kind_prefix), kind_prefix)
        sql += " ORDER BY pinged_at DESC, number DESC"
        return self._read_rows(sql, limit, params)

    def _read_rows(
        self, sql: str, limit: int | None, params: tuple = ()
    ) -> list[dict]:
        if limit is not None:
            sql += " LIMIT ?"
            params = params + (limit,)
        return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    def age_out_spawn(self, repo: str, number: int, round_: int, new_ts: int) -> bool:
        """Retry-now: stamp the NEWEST spawn row of THIS round back to
        `new_ts` (the console passes a time just past the stale window), so
        `spawn_age` reads as stale and the daemon's own re-enqueue path can
        respawn the round on its next pass. An UPDATE, never a DELETE: the
        spawn history stays intact (the reap sweep still maps the old session
        name back to its round), only the newest row's clock moves. Keyed per
        round, like `get_spawn` -- a retry re-arms only the round the console
        named. Returns False when there is no row to age (the console then
        reports nothing to retry).

        Aging is necessary but not sufficient for a respawn: the daemon defers
        a stale round whose session still shows signs of life (loop's liveness
        signal, which exists to stop double-spending a round). Kill the wedged
        session first, then retry -- that is exactly the operator sequence the
        stalled ping asks for.
        """
        row = self._db.execute(
            "SELECT session FROM spawns WHERE repo=? AND number=? AND round=? "
            "ORDER BY spawned_at DESC, rowid DESC LIMIT 1",
            (repo, number, round_),
        ).fetchone()
        if row is None:
            return False
        self._db.execute(
            "UPDATE spawns SET spawned_at=? WHERE session=?",
            (int(new_ts), row["session"]),
        )
        self._db.commit()
        return True
