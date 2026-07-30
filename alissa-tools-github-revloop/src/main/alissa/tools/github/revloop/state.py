"""Local spawn ledger.

Deliberately thin: GitHub is the source of truth for how many rounds have run
(one submitted review per round). This table exists only to stop the daemon
double-spawning a reviewer while a round is still in flight, to map a live
session name back to the round it was spawned for (so the reap sweep can tell
a finished round's session from an in-flight one), to remember that a cap-out
was already escalated, and to count the operator re-entry acks that raise a
single PR's effective cap. The ledger tolerates sessions dying or being
killed behind its back: a reap record is bookkeeping, never a precondition.

The `review_tasks` table is a cache, not a ledger: it remembers which CR2
review task each PR resolved to so the decide path can read that one task by
ref instead of searching the actor's whole task corpus every poll. Every row is
re-checked on use and dropped when it stops matching, and losing the table
costs nothing but the search it was avoiding.

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
import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Poll-snapshot retention: the newest N rows are kept, older ones pruned on
# every write. Fixed, not a config key -- `poll_snapshots` is an observation
# buffer for a future console sidecar, and a bounded ring is all it needs (it
# reads the recent tail). A change to this constant is a change to the
# observable buffer size, so it is pinned by a test.
SNAPSHOT_RETENTION = 1000

# Streak limiting for the best-effort telemetry writer's WARN (issue #62), on
# the same rule the poll firewall uses: the first few failures in full, then
# one in ten. A ledger that has gone read-only fails on every single poll, and
# the warning is worth nothing if it drowns the decisions around it.
TELEMETRY_LOG_HEAD = 3
TELEMETRY_LOG_EVERY = 10

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

CREATE TABLE IF NOT EXISTS verdict_posts (
    repo          TEXT    NOT NULL,
    number        INTEGER NOT NULL,
    round         INTEGER NOT NULL,
    first_seen_at INTEGER NOT NULL,
    -- The head this round's verdict is ABOUT, captured when the gap was first
    -- seen. Never re-read from the PR at post time: a review carries the
    -- commit it judged, and stamping an old verdict onto a head pushed since
    -- makes the next pass read it as a current approval.
    head_sha      TEXT    NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    -- When the last attempt ran. The retry delay is measured from HERE, not
    -- from first_seen_at: a delay measured from a fixed origin and capped stops
    -- bounding anything once the row is older than the cap, and the hot loop
    -- comes back.
    last_attempt_at INTEGER,
    posted_at     INTEGER,
    -- Set when the post can never succeed -- the head this verdict judged is
    -- no longer a commit of the PR (a force-push landed under it). The round is
    -- then released rather than held open forever; see loop._abandon_verdict.
    abandoned_at  INTEGER,
    -- When this round's APPROVE was FIRST held back because the judged head's
    -- CI rollup had not settled -- and it is never overwritten afterwards, so
    -- "how long has this round really been held?" always has an answer. NULL
    -- means the gate never held this round.
    checks_held_at INTEGER,
    -- WHICH unsettled condition that stamp belongs to ('pending' -- checks are
    -- genuinely running -- or 'unknown' -- the rollup could not be read). The
    -- bound is defined against the first observation of the condition being
    -- waited on, and a transient read error is not that observation, so the
    -- clock gets exactly one restart when an 'unknown' hold is promoted to a
    -- 'pending' one. The policy lives in loop._gate_on_checks; this column is
    -- what lets it be decided from the ledger instead of from memory.
    checks_held_state TEXT,
    -- When that promotion happened: the stamp the bound is measured from once
    -- the wait is on checks that are genuinely running. Separate from
    -- `checks_held_at` rather than replacing it, because the two answer
    -- different questions and a report that conflates them says a promoted hold
    -- waited one bound when it waited two. NULL until (and unless) the promotion
    -- happens; the bound then reads `checks_pending_at or checks_held_at`.
    checks_pending_at INTEGER,
    review_url    TEXT,
    last_error    TEXT,
    PRIMARY KEY (repo, number, round)
);

-- The PR -> CR2 review-task mapping, remembered so the decide path does not
-- have to search for it. CR2 guarantees one review task per PR and CR7 reuses
-- it across every round, so the mapping is stable for the PR's whole life --
-- which is exactly what makes it cacheable. A row here is a HINT, never a
-- fact: it is re-checked by ref on use and dropped when it stops matching, and
-- losing the whole table only costs one title search per PR (see
-- loop._review_task). That is why this is best-effort like the snapshots
-- above, not a correctness write.
CREATE TABLE IF NOT EXISTS review_tasks (
    repo        TEXT    NOT NULL,
    number      INTEGER NOT NULL,
    task_ref    TEXT    NOT NULL,
    resolved_at INTEGER NOT NULL,
    PRIMARY KEY (repo, number)
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
    posted            INTEGER NOT NULL DEFAULT 0,
    awaiting_post     INTEGER NOT NULL DEFAULT 0,
    abandoned         INTEGER NOT NULL DEFAULT 0,
    stages_json       TEXT    NOT NULL
);
"""

# Columns added to a table after it first shipped. CREATE TABLE IF NOT EXISTS is
# not a migration: an existing database keeps the ORIGINAL table, so these are
# ALTERed in on open. A NOT NULL default is what lets an ALTER backfill every
# historical row without a rewrite -- an old pass genuinely posted no verdicts,
# so 0 is the true value, not a placeholder. Where "never happened" has no
# numeric truth (`checks_held_at`), the column is nullable instead and NULL
# means exactly that.
_ADDED_COLUMNS = {
    "poll_snapshots": (
        ("posted", "INTEGER NOT NULL DEFAULT 0"),
        ("awaiting_post", "INTEGER NOT NULL DEFAULT 0"),
        ("abandoned", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "verdict_posts": (
        ("checks_held_at", "INTEGER"),
        ("checks_held_state", "TEXT"),
        ("checks_pending_at", "INTEGER"),
    ),
}


@dataclass(frozen=True)
class ChecksHold:
    """One round's CI hold, as the ledger remembers it.

    Two stamps, because the gate has two honest numbers to report and they can
    differ by a whole wait bound:

    * `first_at` -- when the round was first held at all, on whatever condition;
    * `pending_at` -- when an unreadable hold was promoted to a genuinely
      pending one, which restarts the clock the bound is measured from (see
      loop._gate_on_checks for why exactly once).

    `since` is the one the bound uses. `first_at` is the one an operator means by
    "how long has this been held?", and reporting only `since` after a promotion
    understates it by up to the full bound.
    """

    first_at: int | None = None
    condition: str | None = None
    pending_at: int | None = None

    @property
    def since(self) -> int | None:
        """The stamp the wait bound is measured from."""
        return self.pending_at or self.first_at

    @property
    def promoted(self) -> bool:
        return self.pending_at is not None


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
        # Kept so a best-effort telemetry write can RECONNECT after a failure
        # (see _reconnect): the daemon's ledger lives on a platform volume, and
        # a remount leaves the open handle pointing at a file descriptor that
        # is gone while the path is perfectly good again.
        self._path = path
        self._read_only = read_only
        # Consecutive failures of the best-effort writer, for streak-limited
        # logging and for firing the one reconnect attempt on the FIRST failure
        # of a streak rather than on every write.
        self._telemetry_failures = 0
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
        self._migrate_added_columns()
        self._db.commit()

    def _migrate_added_columns(self) -> None:
        """Add any column a pre-existing database predates.

        Runs after the schema script (which creates every table with its full
        column list on a fresh database, making this a no-op there) and is
        idempotent: the existing columns are read first, so a restart never
        re-ALTERs. Read-only openers never reach it -- the console must not
        migrate a database the daemon owns.
        """
        for table, columns in _ADDED_COLUMNS.items():
            existing = {
                row["name"]
                for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns:
                if name not in existing:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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

    # -- best-effort writes (issue #62) ------------------------------------
    #
    # CLASSIFICATION. Every write in this class is one of two kinds, and only
    # one of them may ever be swallowed:
    #
    # * TELEMETRY -- `record_snapshot`, and nothing else. `poll_snapshots` is an
    #   observation buffer: the daemon never reads it back to make a decision
    #   (only `read_snapshots`, for the console, does), so a row lost to a
    #   read-only volume costs one missing datapoint on a dashboard. On
    #   2026-07-29 it cost the whole daemon instead -- the sqlite exception
    #   escaped `poll_once` and killed the process mid-poll.
    #
    # * CORRECTNESS -- every other write here (`record_spawn`, `record_reap`,
    #   `record_ping`, `record_escalation`, `record_grant`, the `verdict_posts`
    #   writes, `age_out_spawn`). Each is a dedupe key or an in-flight marker
    #   for an action the daemon TAKES: swallowing one does not lose a
    #   datapoint, it re-spawns a reviewer round, re-pages an operator, or
    #   re-grants a cap. Those stay strict and raise.
    #
    # STRICTNESS IS NOT, BY ITSELF, THE PROTECTION -- and the first draft of
    # this change claimed it was (PR #63 round-1 blocker). Raising aborts the
    # pass that failed; it says nothing about the next one. The poll firewall
    # then hands the loop straight back to the same code path, and the side
    # effect the write was meant to dedupe has ALREADY been taken -- so a
    # read-only volume turned "enqueue a reviewer, fail to record it" into a
    # fresh reviewer session every poll, indefinitely, where before it merely
    # killed the daemon after one. What actually protects the side effect is
    # `writable()` above, checked by `loop.poll_once` before the pass takes any
    # decision at all: the daemon does not take an action it cannot record.
    # Strictness is what makes an unrecordable action VISIBLE; the gate is what
    # makes it not repeat.

    @staticmethod
    def _write_probe(db: sqlite3.Connection) -> bool:
        """Can this connection actually write? A no-op header write, which
        exercises exactly the path a real write needs, changes nothing, and
        costs one page. Read-only-ness is the thing being detected, so it
        cannot be answered by inspecting the file's mode: sqlite decides it at
        open time and a handle can be read-only over a writable file (and, for
        one recoverable moment, the reverse)."""
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            db.execute(f"PRAGMA user_version = {int(version)}")
            db.commit()
        except sqlite3.DatabaseError:
            return False
        return True

    def writable(self) -> bool:
        """Whether the ledger can accept a write RIGHT NOW.

        The daemon asks this before it takes any action it would have to
        record (issue #62, round-1 blocker). Keeping a correctness write strict
        aborts the pass that fails, but the poll firewall hands the loop
        straight back to the same code path -- so without this gate a read-only
        volume turns "enqueue a reviewer, then fail to record it" into a fresh
        reviewer session every poll, forever, each one a live agent. The
        invariant the gate buys is simple: the daemon does not take an action
        it cannot record.

        A failing probe retries through `_reconnect`, whose candidate is
        write-probed before adoption -- so a stale handle over a live file
        heals here too, and only a genuinely unwritable ledger answers False.
        A read-only `State` (the console's) is never writable by construction.
        """
        if self._read_only:
            return False
        return self._write_probe(self._db) or self._reconnect()

    def _reconnect(self) -> bool:
        """Swap in a fresh connection, but ONLY if the fresh one is better.
        True when the swap happened -- which, because the candidate is
        write-probed, is also proof that the ledger is writable.

        Deliberately raw: it re-establishes the connection and NOTHING else --
        no schema script, no migration. The reconnect exists for the
        stale-handle-after-remount case, where the database on disk is the one
        this process already migrated; re-running DDL through a path that only
        a failed telemetry write reaches would be a far larger act than the
        failure justifies.

        The candidate is WRITE-PROBED before it is adopted, and the old
        connection is kept when the probe fails, because a blind reconnect
        makes the read-only case permanently worse rather than better: sqlite
        decides read-only-ness when it OPENS the file, so a handle opened while
        the volume was read-only stays read-only for the rest of its life even
        after the volume comes back -- while the handle opened before the fault
        heals by itself the moment writes are possible again. Replacing the
        healable handle with a poisoned one would trade a transient outage for
        a permanent one.

        WRITE MODE ONLY. A read-only `State` is the console's, it must never
        write, and there is nothing a reconnect could improve for it -- so it
        returns False rather than swapping one equivalent handle for another.
        That also keeps the contract absolute: a True from here always means a
        candidate passed the write probe, which is what `writable()` relies on.
        """
        if self._read_only:
            return False
        candidate: sqlite3.Connection | None = None
        try:
            candidate = sqlite3.connect(str(self._path))
            candidate.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            log.debug("state: reconnect to %s declined: %s", self._path, exc)
            return False
        if not self._write_probe(candidate):
            log.debug("state: reconnect to %s declined (candidate cannot write)", self._path)
            try:
                candidate.close()
            except sqlite3.Error:
                pass
            return False
        try:
            self._db.close()
        except sqlite3.Error:
            pass  # already broken; the point was to replace it
        self._db = candidate
        return True

    def _write_telemetry(self, write: "Callable[[], None]", what: str) -> bool:
        """Run a TELEMETRY write, absorbing any database error. True on success.

        One reconnect attempt on the FIRST failure of a streak (not on every
        one: a database that is read-only stays read-only, and reconnecting per
        poll would add a file open to every pass for nothing), then a
        streak-limited WARN and back to polling.
        """
        try:
            write()
        except sqlite3.DatabaseError as exc:
            first = self._telemetry_failures == 0
            if first and self._reconnect():
                try:
                    write()
                except sqlite3.DatabaseError as retry_exc:
                    exc = retry_exc
                else:
                    log.info(
                        "state: %s succeeded after reconnecting to %s",
                        what, self._path,
                    )
                    return True
            self._telemetry_failures += 1
            n = self._telemetry_failures
            if n <= TELEMETRY_LOG_HEAD or n % TELEMETRY_LOG_EVERY == 0:
                log.warning(
                    "state: %s failed (%s: %s) — failure %d of this streak; "
                    "telemetry is best-effort, the loop keeps polling",
                    what, type(exc).__name__, exc, n,
                )
            return False
        if self._telemetry_failures:
            log.info(
                "state: %s succeeded after %d failed attempt(s) — telemetry "
                "is persisting again",
                what, self._telemetry_failures,
            )
            self._telemetry_failures = 0
        return True

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
        and it is the newest ack by `granted_at`, so a second grant supersedes
        the first in the page's wording. Nothing compares that timestamp with
        the escalation row -- the once-only dedupe is the ping-ledger key; see
        loop.capout_kind for why not a wall-clock comparison.
        """
        return self._db.execute(
            "SELECT * FROM grants WHERE repo=? AND number=? "
            "ORDER BY granted_at DESC, rowid DESC LIMIT 1",
            (repo, number),
        ).fetchone()

    def read_grants(self, repo: str, number: int) -> list[dict]:
        """One PR's grant rows, newest first (like every reader here).

        Deliberately narrow: no unfiltered form and no `limit` until something
        needs them. The console does not read this table yet -- showing "this
        PR was re-entered by @x" in the operator inbox wants rendering as well
        as data, so it lands as its own change rather than as unused
        parameters here.
        """
        return self._read_rows(
            "SELECT repo, number, comment_id, author, rounds, granted_at "
            "FROM grants WHERE repo=? AND number=? "
            "ORDER BY granted_at DESC, rowid DESC",
            None,
            (repo, number),
        )

    # -- native verdict posts ----------------------------------------------
    #
    # One row per (PR, round) whose verdict envelope exists but whose native
    # reviewer-identity review does not yet. The row is the daemon's memory of
    # an OPEN obligation: when the gap was first seen (which is what the grace
    # window before posting is measured from), how many post attempts have
    # failed, and -- once one lands -- that the round is finally closed.

    def get_verdict_post(
        self, repo: str, number: int, round_: int
    ) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM verdict_posts WHERE repo=? AND number=? AND round=?",
            (repo, number, round_),
        ).fetchone()

    def note_verdict_post_owed(
        self, repo: str, number: int, round_: int, head_sha: str = ""
    ) -> sqlite3.Row:
        """Record that this round is owed a native verdict; return its row.

        OR IGNORE keeps the FIRST observation on purpose, and both of the
        columns it freezes matter:

        * `first_seen_at` is what the pre-post grace window and the retry
          backoff are measured from. A reviewer session about to submit its own
          review must be given the chance, and a timestamp that moved every
          poll would push both deadlines out forever.
        * `head_sha` is the head the verdict is about. Re-reading it later
          would defeat the point -- by post time the implementer may have
          pushed, and a verdict stamped onto the new head reads as an approval
          of code no reviewer has seen.
        """
        self._db.execute(
            "INSERT OR IGNORE INTO verdict_posts "
            "(repo, number, round, first_seen_at, head_sha) VALUES (?,?,?,?,?)",
            (repo, number, round_, int(time.time()), head_sha),
        )
        self._db.commit()
        row = self.get_verdict_post(repo, number, round_)
        assert row is not None  # just inserted, or already there
        return row

    def record_verdict_post_failure(
        self, repo: str, number: int, round_: int, error: str
    ) -> int:
        """Count one failed post attempt; return the new attempt total.

        Stamps `last_attempt_at`, which is what the retry delay is measured
        from -- see the column comment.
        """
        self._db.execute(
            "UPDATE verdict_posts SET attempts = attempts + 1, last_error = ?, "
            "last_attempt_at = ? WHERE repo=? AND number=? AND round=?",
            (error[:500], int(time.time()), repo, number, round_),
        )
        self._db.commit()
        row = self.get_verdict_post(repo, number, round_)
        return int(row["attempts"]) if row else 0

    def checks_hold(self, repo: str, number: int, round_: int) -> "ChecksHold":
        """This round's CI hold: when it began, what it is waiting on, and when
        that became a genuine `pending` -- an all-None ChecksHold if it has never
        been held.

        A read, deliberately: whether an existing stamp still applies is a
        policy question about CI (see loop._gate_on_checks), and this table's job
        is to remember the answer, not to make it. It remembers BOTH stamps
        because the two answer different questions -- `since` bounds the wait,
        `first_at` is how long the round has really been held -- and a report
        that conflates them tells an operator a promoted hold waited 30 minutes
        when it waited 60.
        """
        row = self.get_verdict_post(repo, number, round_)
        if row is None or not row["checks_held_at"]:
            return ChecksHold()
        pending_at = row["checks_pending_at"]
        return ChecksHold(
            first_at=int(row["checks_held_at"]),
            condition=(row["checks_held_state"] or None),
            pending_at=int(pending_at) if pending_at else None,
        )

    def record_checks_hold(
        self, repo: str, number: int, round_: int, condition: str
    ) -> int:
        """Record that this round is held on `condition`; return the stamp the
        bound is measured from.

        The caller decides WHEN to call this -- once when the hold begins, and at
        most once more when an unreadable hold is promoted to a genuinely pending
        one, because the bound is defined against the first observation of the
        condition actually being waited on (loop._gate_on_checks owns that rule).

        The promotion NO LONGER overwrites `checks_held_at`: it fills
        `checks_pending_at` instead, so the ledger keeps when the round was first
        held as well as when its current wait started. Nothing about the bound
        changes; what changes is that the daemon can now say both numbers out
        loud, which the operator-facing report needs.
        """
        now = int(time.time())
        if self.checks_hold(repo, number, round_).first_at is None:
            self._db.execute(
                "UPDATE verdict_posts SET checks_held_at = ?, checks_held_state = ? "
                "WHERE repo=? AND number=? AND round=?",
                (now, condition, repo, number, round_),
            )
        else:
            self._db.execute(
                "UPDATE verdict_posts SET checks_pending_at = ?, "
                "checks_held_state = ? WHERE repo=? AND number=? AND round=?",
                (now, condition, repo, number, round_),
            )
        self._db.commit()
        return now

    def record_verdict_post_abandoned(
        self, repo: str, number: int, round_: int, why: str
    ) -> None:
        """Give up on this round's native verdict, permanently.

        Reserved for a post that can never succeed: the head the round judged
        is gone from the PR, so there is no commit left to record the verdict
        against. Holding the round open then stalls the PR out of the loop
        forever -- the abandonment releases it so a fresh round can run against
        the new head.
        """
        self._db.execute(
            "UPDATE verdict_posts SET abandoned_at = ?, last_error = ? "
            "WHERE repo=? AND number=? AND round=?",
            (int(time.time()), why[:500], repo, number, round_),
        )
        self._db.commit()

    def verdict_post_abandoned(self, repo: str, number: int, round_: int) -> bool:
        """Whether this round's owed native verdict was given up on."""
        row = self.get_verdict_post(repo, number, round_)
        return bool(row is not None and row["abandoned_at"])

    def abandoned_rounds(self, repo: str, number: int) -> int:
        """How many of this PR's rounds will never have a native verdict.

        Each one leaves a permanent hole between the envelope count and the
        review count -- the envelope is on the task forever, the review record
        never exists -- so every later comparison of the two has to subtract
        it. Without that the daemon reads the hole as "the newest round has no
        native verdict" and posts a redundant one on the round AFTER each
        abandonment.
        """
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM verdict_posts "
            "WHERE repo=? AND number=? AND abandoned_at IS NOT NULL",
            (repo, number),
        ).fetchone()
        return int(row["n"]) if row else 0

    def record_verdict_post(
        self, repo: str, number: int, round_: int, review_url: str
    ) -> None:
        """Mark the round's native verdict as landed. Bookkeeping and evidence
        only: GitHub's reviews list stays the authority on whether a native
        verdict exists, so a row lost behind the daemon's back costs one
        duplicate post, never a round that silently counts as closed."""
        self._db.execute(
            "UPDATE verdict_posts SET posted_at = ?, review_url = ?, last_error = NULL "
            "WHERE repo=? AND number=? AND round=?",
            (int(time.time()), review_url, repo, number, round_),
        )
        self._db.commit()

    def read_verdict_posts(self, limit: int | None = None) -> list[dict]:
        """Verdict-post rows, newest observation first."""
        return self._read_rows(
            "SELECT repo, number, round, first_seen_at, head_sha, attempts, "
            "last_attempt_at, posted_at, abandoned_at, checks_held_at, "
            "checks_held_state, checks_pending_at, review_url, last_error "
            "FROM verdict_posts "
            "ORDER BY first_seen_at DESC, number DESC",
            limit,
        )

    # -- poll snapshots (the console sidecar's exhaust buffer) -------------

    def review_task(self, repo: str, number: int) -> "str | None":
        """The review-task ref last resolved for this PR, or None.

        None is the FIRST-SIGHTING answer and also the answer after a database
        that could not be written: both mean "search for it", which is what the
        daemon did unconditionally before this table existed. There is nothing
        a caller may conclude from None beyond that.

        The only READ in this class that absorbs a database error, and for the
        same reason its writes do: this table is an optimization, so a ledger
        that cannot answer must cost the daemon a task search, never a review.
        Everything else here is ledger state whose loss the caller has to see.
        """
        try:
            row = self._db.execute(
                "SELECT task_ref FROM review_tasks WHERE repo=? AND number=?",
                (repo, number),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            log.warning(
                "state: review-task cache unreadable (%s: %s) — this pass "
                "resolves %s#%d by searching, as it did before the cache",
                type(exc).__name__, exc, repo, number,
            )
            return None
        return None if row is None else str(row["task_ref"])

    def record_review_task(self, repo: str, number: int, task_ref: str) -> bool:
        """Remember which review task a PR resolved to. Best-effort.

        REPLACE, not IGNORE: re-resolving is how a mapping is corrected, so the
        newest answer has to win. `resolved_at` is when the mapping was last
        confirmed by a search, which is the audit question worth answering.

        A database error here is absorbed exactly like a snapshot's: the cache
        is an optimization, and a pass that cannot persist it still decides the
        round correctly -- it just pays the search again next time.
        """
        return self._write_telemetry(
            lambda: self._replace_review_task(repo, number, task_ref),
            "review-task cache write",
        )

    def _replace_review_task(self, repo: str, number: int, task_ref: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO review_tasks "
            "(repo, number, task_ref, resolved_at) VALUES (?,?,?,?)",
            (repo, number, task_ref, int(time.time())),
        )
        self._db.commit()

    def forget_review_task(self, repo: str, number: int) -> bool:
        """Drop a mapping that has been DISPROVED. Best-effort.

        Only ever called on a positive disproof -- the task was read and is no
        longer this PR's open review task, or a fresh search found none. Never
        on a failed read: an unreadable task is not a wrong mapping, and
        forgetting one on a transient CLI error would throw away a good cache
        entry every time the API hiccups.
        """
        return self._write_telemetry(
            lambda: self._delete_review_task(repo, number),
            "review-task cache invalidation",
        )

    def _delete_review_task(self, repo: str, number: int) -> None:
        self._db.execute(
            "DELETE FROM review_tasks WHERE repo=? AND number=?", (repo, number)
        )
        self._db.commit()

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
        posted: int = 0,
        awaiting_post: int = 0,
        abandoned: int = 0,
        stages: list[dict],
    ) -> bool:
        """Append one poll-pass observation, then prune to the newest
        SNAPSHOT_RETENTION rows. `ts` is stamped here (wall-clock seconds,
        like every other row in this ledger); `stages` is the compact
        per-item list a future console reads back through read_snapshots,
        serialized to JSON. Purely observational -- written on every pass,
        dry-run included -- and pruned on write, so the table is
        self-bounding. The count kwargs default to 0 so a caller need only
        pass the ones a given pass produced.

        BEST-EFFORT, and the only write in this class that is (issue #62): a
        snapshot observes the pass, it is not something the daemon has to
        remember, so a database error here is absorbed, reported once per
        streak-limited window, and the loop keeps polling. Returns whether the
        row landed, for a caller that wants to say so; nothing in the daemon
        depends on it.
        """
        return self._write_telemetry(
            lambda: self._insert_snapshot(
                duration_ms=duration_ms,
                candidates=candidates,
                spawned=spawned,
                stale_reenqueued=stale_reenqueued,
                in_flight=in_flight,
                deferred=deferred,
                converged=converged,
                capped=capped,
                escalated=escalated,
                skipped=skipped,
                reaped=reaped,
                posted=posted,
                awaiting_post=awaiting_post,
                abandoned=abandoned,
                stages=stages,
            ),
            "poll snapshot",
        )

    def _insert_snapshot(
        self,
        *,
        duration_ms: int,
        candidates: int,
        spawned: int,
        stale_reenqueued: int,
        in_flight: int,
        deferred: int,
        converged: int,
        capped: int,
        escalated: int,
        skipped: int,
        reaped: int,
        posted: int,
        awaiting_post: int,
        abandoned: int,
        stages: list[dict],
    ) -> None:
        """The snapshot INSERT + prune itself, strict. Split out so the
        best-effort wrapper can RETRY it verbatim after a reconnect."""
        self._db.execute(
            "INSERT INTO poll_snapshots "
            "(ts, duration_ms, candidates, spawned, stale_reenqueued, "
            "in_flight, deferred, converged, capped, escalated, skipped, "
            "reaped, posted, awaiting_post, abandoned, stages_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                posted,
                awaiting_post,
                abandoned,
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
