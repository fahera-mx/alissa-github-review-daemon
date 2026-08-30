"""The console's read-only data layer (plus the one retry-now UPDATE).

Every panel the dashboard renders is assembled here from four kinds of source,
in strict budget order:

1. **The local state.db** -- poll snapshots (through `State.read_snapshots`, the
   UI-1 reader), the spawn ledger, the escalation table and the ping ledger. No
   GitHub call: the daemon already wrote everything down.
2. **Local process state** -- `alissa tmux ls --json` for the session list, and
   a `/proc` walk (sysinfo) of each session's pane-PID tree for CPU%/RSS, plus
   the two container-wide reads the per-session sums cannot answer: the cgroup
   memory charge split into resident vs reclaimable, and the top processes by
   RSS across the whole host. One `/proc` scan serves both.
3. **Two cached remote checks** -- `gh api rate_limit` (60s cache) for the rate
   meter, and the PyPI version JSON (10m cache) for the running-vs-latest drift
   chip. These are the *only* network calls, and both are cached so a room full
   of refreshing operators cannot move the daemon's rate budget.
4. **Config echo + log tail** -- the effective config the daemon resolved, and
   the tail of its log file.

Reviewer semantics throughout, which is where this diverges from the devloop
console it was ported from: the unit of work is a PR **round** (round k of the
cap), not an issue attempt, so the pipeline board is PR-centric and there is no
worker-tasks panel (reviewers create no tasks) and no maintenance edge.

Every external call is failure-tolerant: a missing `gh`, an unreachable PyPI, a
truncated log, an absent state.db all degrade to empty/None, never an exception
that would blank the whole dashboard. All IO is injected (run/http_get/clock),
so the whole layer is drivable from tests without a subprocess or a socket.

Read-only means read-only: every read opens the ledger through State's
`read_only` mode (the sqlite `mode=ro` URI), which cannot create the file and
cannot run the daemon's schema/migration path. A workspace with no daemon
state therefore reports ABSENT (`state_present` in the payload, a banner on the
page) instead of quietly conjuring an empty state.db that renders exactly like
an idle daemon -- the one distinction the console exists to make, and easy to
get wrong because `--workspace-root` defaults to the cwd.

The single mutation is `retry_now`: it ages the newest ledger row of one round
past the stale window (an UPDATE, via `State.age_out_spawn`), so the daemon's
own re-enqueue path can respawn it on its next pass. No new retry logic lives
here -- the console only moves a clock the daemon already reads. It reports a
tri-state (aged / no row / state unavailable) so the audit trail never records
a lost write as "nothing to retry".
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from ..alissa import REVIEW_SESSION_PREFIX
from ..config import Config
from ..loop import (
    ESCALATION_STABILITY,
    ESCALATION_STALLED,
    STALE_ROUND_SECONDS,
    parse_stability_kind,
)
from ..proc import CommandError, run as proc_run
from ..state import State
from ..version import Version
from . import sysinfo

# How many recent snapshots feed the sparklines / pipeline board.
SPARK_POINTS = 60
# How many processes the host-wide top-by-RSS list carries. Five is the whole
# point of the panel: it names what is holding a resident charge, it is not a
# process browser, and every extra row is payload on a ~10s poll.
TOP_PROCS = 5
# Cache lifetimes for the remote checks (seconds).
RATE_CACHE_TTL = 60.0
VERSION_CACHE_TTL = 600.0
# PyPI JSON endpoint for the running-vs-latest drift chip.
PYPI_URL = "https://pypi.org/pypi/alissa-tools-github-revloop/json"
# How many seconds past the stale window retry-now ages a row: the daemon
# considers a round stale when spawn_age >= STALE_ROUND_SECONDS, so we push the
# newest row a minute past that so the very next poll pass sees it as stale.
RETRY_AGE_BUFFER = 60

# The inbox kinds the console pages, in the vocabulary it shows the operator.
# `cap-out` comes from the escalations table (terminal per head), `stalled`
# from the ping ledger (one per deferral episode). The other ping kind
# (`activity-deferred:*`) is the dedupe key for a PR activity-comment line --
# telemetry, not an operator page -- so it is deliberately NOT an inbox item.
INBOX_CAP_OUT = "cap-out"
INBOX_STALLED = "stalled"
# The product-stability hold (issue #105). Its own kind rather than a shade of
# `cap-out`: both stop the loop and both are lifted by the same re-entry ack,
# but the operator's question is different -- a cap-out asks "is ten rounds
# enough?", a stability hold says "the product has not moved since <sha>", and
# the two shas are the whole of what makes that checkable.
INBOX_STABILITY = "stability-held"

# How many INBOX ITEMS reach the payload. `escalations` and `pings` are never
# pruned (their rows are the daemon's dedupe keys), so the console bounds its
# own view the way SPARK_POINTS bounds the snapshot tail -- an inbox that never
# clears stops being an inbox. The ping read applies the kind filter in SQL, so
# this counts pages and not the telemetry rows interleaved with them.
INBOX_LIMIT = 50
# How many poll intervals a freshly raised page is live for regardless of the
# snapshot. The liveness test reads the LATEST snapshot, so a page raised
# between two passes has no snapshot to appear in yet and would flicker into
# `settled` for one refresh; two intervals covers the raise-to-next-pass gap
# with a pass to spare. Erring long is the safe direction -- the failure this
# split exists to prevent is an operator skipping the inbox, and a settled row
# shown one refresh too long costs nothing.
INBOX_LIVE_GRACE_INTERVALS = 2
# The kind prefix that makes a ping row an operator page. `read_pings` matches
# it in SQL; `_inbox` re-checks it to split the session out of the kind.
PING_STALLED_PREFIX = f"{ESCALATION_STALLED}:"
PING_STABILITY_PREFIX = f"{ESCALATION_STABILITY}:"

# retry_now outcomes. Distinguishing "no row" from "the write was lost" keeps
# the audit line honest: both degrade to a failed action, only one means the
# operator asked to retry something that isn't there.
RETRY_OK = "retried"
RETRY_NO_ROW = "no ledger row"
RETRY_UNAVAILABLE = "state unavailable"


def _pr_key(repo: object, number: object) -> "tuple[str, int] | None":
    """The identity the inbox and the pipeline board compare PRs by.

    The two sides reach this from different stores -- an inbox row's `number`
    comes out of sqlite, a board row's out of a snapshot's JSON -- so the key
    is normalised rather than compared as-is, and a row too malformed to key
    (a null number) returns None instead of raising: every read path here
    degrades, it never blanks the dashboard.
    """
    if not isinstance(number, (int, str)):
        return None
    try:
        return (str(repo), int(number))
    except ValueError:
        return None


def is_managed(name: "str | None") -> bool:
    """Whether a tmux session belongs to this daemon's reviewer namespace.

    Sessions outside it (another workspace's daemon, a hand-started one) still
    appear in the table -- an operator wants to see what holds worker slots --
    but they are marked unmanaged, and only the ledger can pair a session with
    a PR round.
    """
    return bool(name) and str(name).startswith(REVIEW_SESSION_PREFIX)


def _default_http_get(url: str, timeout: float) -> "bytes | None":
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


class _Cache:
    """A one-value TTL cache over a monotonic clock. Refreshes lazily on read;
    a fetch that returns None does NOT overwrite the last good value with junk
    (an unreachable PyPI keeps showing the last known latest version).

    A FAILED fetch still arms the TTL, so a missing `gh` or an unreachable PyPI
    is retried once per window rather than on every poll: without that, the
    "cached so a room of operators cannot move the rate budget" contract only
    held once a check had succeeded, and a broken check made every /api/state
    block on its full timeout.
    """

    def __init__(self, ttl: float, clock: Callable[[], float]) -> None:
        self._ttl = ttl
        self._clock = clock
        self._value: object = None
        self._expires = 0.0

    def get(self, fetch: Callable[[], object]) -> object:
        now = self._clock()
        if now >= self._expires:
            fresh = fetch()
            if fresh is not None:
                self._value = fresh
            # Arm the window either way: on success with the fresh value, on
            # failure keeping the last good one (or None) as a negative cache.
            self._expires = now + self._ttl
        return self._value


class Sources:
    def __init__(
        self,
        *,
        config: Config,
        running_version: str,
        log_path: "Path | None" = None,
        run: "Callable[..., str]" = proc_run,
        http_get: "Callable[[str, float], bytes | None]" = _default_http_get,
        proc_root: str = "/proc",
        cgroup_root: str = "/sys/fs/cgroup",
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.running_version = running_version
        self.log_path = Path(log_path) if log_path else None
        self._run = run
        self._http_get = http_get
        self._proc_root = proc_root
        self._cgroup_root = cgroup_root
        self._clock = clock
        self._wall = wall_clock
        self._rate_cache = _Cache(RATE_CACHE_TTL, clock)
        self._version_cache = _Cache(VERSION_CACHE_TTL, clock)
        self.boot_wall = int(wall_clock())

    # -- thin, failure-tolerant subprocess helpers -------------------------

    def _safe_run(self, argv: "list[str]", *, timeout: int = 30) -> "str | None":
        try:
            return self._run(argv, timeout=timeout)
        except CommandError:
            return None

    def _safe_json(self, argv: "list[str]", *, timeout: int = 30):
        out = self._safe_run(argv, timeout=timeout)
        if not out or not out.strip():
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    # -- state.db (no GitHub call) -----------------------------------------

    def state_present(self) -> bool:
        """Whether the daemon's state.db exists at the resolved path. False is
        an ANSWER, not an error: it means this workspace has no revloop state
        (usually a mistyped `--workspace-root`), and the page says so rather
        than rendering an empty dashboard that looks like an idle daemon."""
        return Path(self.config.state_db).is_file()

    def _read_state(self, default, fn, *, write: bool = False):
        """Open a short-lived `State` and run `fn(state)`, degrading to
        `default` if the db is absent or can't be read/written.

        Reads go through State's read-only mode, so a console read can never
        create or migrate the daemon's database (and an absent file surfaces as
        `sqlite3.OperationalError`, handled here). The retry mutation needs a
        writable handle, so it is guarded by an explicit existence check first
        -- a retry click must never be the thing that creates a state.db.

        `write=True` is a FULL daemon handle, deliberately: it applies the
        schema and, on a pre-0.8 database, runs `_migrate_spawns`. The module
        docstring's "read-only means read-only" covers the read paths, not this
        one. Suppressing the migration here would be worse than allowing it --
        `age_out_spawn`'s `WHERE session=?` does not match a `spawns` still
        keyed by `(repo, number, round)`, so the console would be writing to a
        schema it declined to verify. In practice a console reaching an
        unmigrated db means the daemon has not started since the upgrade, and
        the migration is the daemon's own all-or-nothing path either way.

        The daemon writes state.db from a separate process; without WAL a
        console read/write can collide with a daemon write and raise
        `sqlite3.OperationalError` (a corrupt db raises regardless). Catching
        here honors this module's contract: a state access never raises an
        exception that would blank the dashboard or 500 the retry action -- it
        degrades like every other source.
        """
        if write and not self.state_present():
            return default
        try:
            with State(self.config.state_db, read_only=not write) as st:
                return fn(st)
        except sqlite3.Error:
            return default

    def snapshots(self, limit: int = SPARK_POINTS) -> "list[dict]":
        return self._read_state([], lambda st: st.read_snapshots(limit))

    def ledgers(self) -> dict:
        """The two INBOX tables, each bounded to INBOX_LIMIT rows of the kind
        the console pages on. The spawn ledger is deliberately not here: it is
        a lookup table read by key, not a display list bounded by recency --
        `sessions` reads it for exactly the session names it renders."""
        empty: "dict[str, list]" = {
            "escalations": [], "pings": [], "stability_pings": []
        }
        return self._read_state(empty, lambda st: {
            "escalations": st.read_escalations(INBOX_LIMIT),
            "pings": st.read_pings(
                INBOX_LIMIT, kind_prefix=PING_STALLED_PREFIX
            ),
            # A SECOND bounded read rather than one unfiltered one: `read_pings`
            # narrows to a single prefix in SQL so its limit bounds the rows the
            # console actually renders, and the telemetry kinds interleaved with
            # both pages would otherwise evict them.
            "stability_pings": st.read_pings(
                INBOX_LIMIT, kind_prefix=PING_STABILITY_PREFIX
            ),
        })

    # -- local process state -----------------------------------------------

    def pane_pid(self, session_id: str) -> "int | None":
        """The pane PID of a managed tmux session, or None. tmux is the only
        way to bridge a session name to a PID we can walk in /proc; a session
        that has gone leaves no pane, so None is normal, not an error."""
        out = self._safe_run(
            ["tmux", "list-panes", "-t", session_id, "-F", "#{pane_pid}"],
            timeout=10,
        )
        if not out:
            return None
        first = out.strip().splitlines()[0] if out.strip() else ""
        try:
            return int(first)
        except ValueError:
            return None

    def spawn_pairs(self, names: "list[str]") -> "dict[str, dict]":
        """The session -> spawn-row lookup, read for EXACTLY the names about to
        be rendered.

        Reading by key rather than by recency is what keeps the pairing correct
        for a wedged session: the daemon defers a stale round behind a session
        that still shows life and never respawns over it, so the row an
        operator most needs paired is the OLDEST one in the ledger -- the first
        casualty of any row-count or time-window cap. The read is still
        bounded, just by the live-session count (worker slots) instead of by
        the ledger's unbounded growth. No live sessions, no query at all.
        """
        if not names:
            return {}
        rows = self._read_state([], lambda st: st.read_spawns(sessions=names))
        return {row["session"]: row for row in rows}

    def sessions(
        self,
        spawns: "list[dict] | None" = None,
        index: "tuple[dict[int, list[int]], dict[int, dict]] | None" = None,
    ) -> "list[dict]":
        """The managed-session table: liveness from `alissa tmux ls`, footprint
        from /proc, and the PR round each session is reviewing.

        The round comes from the spawn ledger (session name is its primary
        key), not from the session name itself: the name carries a nonce and
        a rename would silently break the pairing, whereas the ledger row IS
        what the daemon's own reap sweep consults. A session with no ledger
        row is simply unpaired -- the panel shows it without a PR.

        `spawns` supplies the ledger rows directly; when it is None (the
        dashboard's path) they are read here, keyed by the names tmux just
        returned -- which is why the session list is fetched first.

        `index` supplies the `/proc` snapshot. The dashboard now needs one
        anyway for the host-wide top-process list, so it builds the index once
        and hands the same one here; passed None (a caller that only wants the
        table) the old lazy build is unchanged and a table with no live pane
        still never scans `/proc`.
        """
        raw = self._safe_json(["alissa", "tmux", "ls", "--json"]) or []
        if not isinstance(raw, list):
            return []
        if spawns is not None:
            by_session = {row["session"]: row for row in spawns}
        else:
            by_session = self.spawn_pairs([
                e["name"] for e in raw
                if isinstance(e, dict) and isinstance(e.get("name"), str)
            ])
        now = int(self._wall())
        out: list[dict] = []
        # ONE /proc snapshot for the whole table: the index is identical for
        # every session in this build, so rebuilding it per session would make
        # the walk O(sessions x processes). Built lazily when the caller did
        # not supply one -- a table with no live pane never scans /proc at all.
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            session_id = entry.get("session") or name
            status = entry.get("status")
            live = bool(entry.get("live", status != "gone"))
            last = entry.get("lastActivity")
            age = now - int(last) if isinstance(last, int) else None
            usage = {"pids": 0, "rss_bytes": None, "cpu_percent": None}
            pid = None
            if live and session_id:
                pid = self.pane_pid(session_id)
                if pid is not None:
                    if index is None:
                        index = sysinfo.build_index(self._proc_root)
                    usage = sysinfo.tree_usage(
                        pid, proc_root=self._proc_root, index=index
                    )
            row = by_session.get(name)
            out.append(
                {
                    "name": name,
                    "session": session_id,
                    "managed": is_managed(name),
                    "status": status,
                    "busy": status == "busy",
                    "live": live,
                    "age_seconds": age,
                    "pane_pid": pid,
                    "cpu_percent": usage["cpu_percent"],
                    "rss_bytes": usage["rss_bytes"],
                    "pr": f"{row['repo']}#{row['number']}" if row else None,
                    "round": row["round"] if row else None,
                    "task_ref": row["task_ref"] if row else None,
                    "retry": (
                        {
                            "repo_slug": row["repo"],
                            "number": row["number"],
                            "round": row["round"],
                        }
                        if row
                        else None
                    ),
                }
            )
        return out

    # -- two cached remote checks ------------------------------------------

    def rate_limit(self) -> "dict | None":
        def fetch() -> "dict | None":
            data = self._safe_json(["gh", "api", "rate_limit"], timeout=20)
            if not isinstance(data, dict):
                return None
            core = (data.get("resources") or {}).get("core")
            if not isinstance(core, dict):
                return None
            return {
                "limit": core.get("limit"),
                "remaining": core.get("remaining"),
                "used": core.get("used"),
                "reset": core.get("reset"),
            }
        return self._rate_cache.get(fetch)  # type: ignore[return-value]

    def latest_version(self) -> "str | None":
        def fetch() -> "str | None":
            body = self._http_get(PYPI_URL, 10.0)
            if not body:
                return None
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return None
            version = (data.get("info") or {}).get("version")
            return version if isinstance(version, str) else None
        return self._version_cache.get(fetch)  # type: ignore[return-value]

    def drift(self) -> dict:
        latest = self.latest_version()
        running = self.running_version
        state = "unknown"
        if latest:
            try:
                run_t = tuple(Version("x", running).components(as_int=True))
                lat_t = tuple(Version("x", latest).components(as_int=True))
                state = ("current" if run_t == lat_t
                         else "behind" if run_t < lat_t else "ahead")
            except (ValueError, TypeError):
                state = "current" if latest == running else "behind"
        return {"running": running, "latest": latest, "state": state}

    # -- config echo + log tail --------------------------------------------

    def config_echo(self) -> dict:
        c = self.config
        return {
            "workspace_root": str(c.workspace_root),
            "repos": list(c.repos),
            # Who may re-open a capped PR with a re-entry ack. Part of the
            # operator's own picture: the escalation inbox below is where that
            # lever gets used.
            "operators": list(c.operators),
            "hub_template": c.hub_template,
            "poll_interval": c.poll_interval,
            "round_cap": c.round_cap,
            "stale_round_seconds": STALE_ROUND_SECONDS,
            "dry_run": c.dry_run,
            "agent_profile": c.agent_profile,
            "reviewer_login": c.reviewer_login,
            "state_db": str(c.state_db),
            "on_missing_review_task": c.on_missing_review_task,
            "on_missing_hub": c.on_missing_hub,
        }

    def log_tail(self, lines: int = 200) -> dict:
        path = self.log_path
        if path is None:
            return {"path": None, "lines": []}
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return {"path": str(path), "lines": []}
        tail = text.splitlines()[-lines:]
        return {"path": str(path), "lines": tail}

    # -- the retry-now mutation --------------------------------------------

    def retry_now(self, repo_slug: str, number: int, round_: int) -> str:
        """Age the newest ledger row of one round past the stale window so the
        daemon can respawn it next pass. Returns one of RETRY_OK,
        RETRY_NO_ROW, or RETRY_UNAVAILABLE.

        The tri-state exists for the audit trail: a write lost to a lock (or an
        absent state.db) still degrades to a failed action rather than a 500 --
        the read paths' contract -- but it must not be RECORDED as "the round
        had no ledger row", which is an operator-error outcome, not a
        console-failure one.

        The console adds no retry logic of its own: the daemon decides whether
        an aged round is really dead (its liveness signal defers a respawn
        behind a session that still shows life), so retry-now only moves the
        clock that decision reads.
        """
        new_ts = int(self._wall()) - STALE_ROUND_SECONDS - RETRY_AGE_BUFFER
        outcome = self._read_state(
            RETRY_UNAVAILABLE,
            lambda st: (
                RETRY_OK
                if st.age_out_spawn(repo_slug, number, round_, new_ts)
                else RETRY_NO_ROW
            ),
            write=True,
        )
        return str(outcome)

    # -- the assembled dashboard payload -----------------------------------

    def dashboard(self) -> dict:
        """The single JSON document the client polls (~10s). Assembled from all
        sources above -- shaped, not raw -- so the page is pure rendering."""
        snaps = self.snapshots(SPARK_POINTS)
        latest = snaps[0] if snaps else None
        ledgers = self.ledgers()
        # ONE /proc scan for this whole build: the session table walks pane
        # trees out of it and the top-process list ranks the same snapshot, so
        # the two panels can never disagree about a process that exited between
        # them -- and the host pays for one walk per poll, not two.
        #
        # The cost on the other side of that trade, so nobody reorders this
        # thinking it is free: the snapshot is now taken BEFORE `sessions()`
        # shells out to tmux, so a pane that starts inside that window is
        # missing from `stats` and its row shows "--" for CPU%/RSS for one
        # poll. Self-healing in ~10s, and cheaper than walking /proc twice.
        proc_index = sysinfo.build_index(self._proc_root)
        sessions = self.sessions(index=proc_index)
        rate = self.rate_limit()
        disk = sysinfo.disk_usage(self.config.workspace_root)
        memory = sysinfo.cgroup_memory(self._cgroup_root)
        top_procs = sysinfo.top_procs(
            TOP_PROCS, proc_root=self._proc_root, index=proc_index
        )

        # Sparklines want oldest -> newest for left-to-right drawing. "Active"
        # counts both buckets a live reviewer session sits in: a round enqueued
        # and still working (in_flight) and one whose respawn is deferred
        # behind a session that still shows life (deferred).
        chrono = list(reversed(snaps))
        # The board rows and the inbox liveness test read the SAME item set --
        # built once, so the two panels can never disagree about which PRs the
        # newest pass still had in hand.
        items = self._pipeline(latest)
        inbox = self._inbox(
            ledgers["escalations"],
            ledgers["pings"],
            ledgers.get("stability_pings", []),
            live_prs=self._live_prs(latest, items),
        )
        sparklines = {
            "poll_duration_ms": [s["duration_ms"] for s in chrono],
            "active_sessions": [s["in_flight"] + s["deferred"] for s in chrono],
        }

        return {
            "generated_at": int(self._wall()),
            "header": {
                "drift": self.drift(),
                # False = this workspace has no revloop state.db at all. The
                # page renders a banner: an empty dashboard here means "wrong
                # workspace", not "idle daemon".
                "state_present": self.state_present(),
                "state_db": str(self.config.state_db),
                "uptime_seconds": int(self._wall()) - self.boot_wall,
                "poll_interval": self.config.poll_interval,
                "round_cap": self.config.round_cap,
                "dry_run": self.config.dry_run,
                "reviewer_login": self.config.reviewer_login,
                "repos": list(self.config.repos),
                "workspace_root": str(self.config.workspace_root),
            },
            "config": self.config_echo(),
            "tiles": {
                # Reviewer sessions first: the operator's question is "how many
                # rounds are being worked", not "how busy is the host". The
                # host-wide live count rides along as the subtitle -- another
                # daemon's workers hold the same worker slots.
                "active_sessions": sum(
                    1 for s in sessions if s["live"] and s["managed"]
                ),
                "live_sessions": sum(1 for s in sessions if s["live"]),
                "rate": rate,
                "volume": disk,
                # The container's own charge, split three ways. Every field is
                # None on a host without cgroup v2 (dev laptop, macOS) and the
                # tile renders "unavailable" -- the console must not require
                # Linux to load.
                "memory": memory,
                "queue_depth": latest["candidates"] if latest else 0,
            },
            "sparklines": sparklines,
            "pipeline": {
                "snapshot_ts": latest["ts"] if latest else None,
                "duration_ms": latest["duration_ms"] if latest else None,
                "round_cap": self.config.round_cap,
                "items": items,
            },
            "inbox": inbox["live"],
            # Exhaust, not backlog: pages whose PR has left the poll's
            # candidate set. Kept in the payload (the operator can still audit
            # what was raised) but out of the list that means "you owe this".
            "inbox_settled": inbox["settled"],
            "inbox_settled_count": len(inbox["settled"]),
            "sessions": sessions,
            # Host-wide, not per session: when the memory tile says the charge
            # IS resident, this is what names the holder.
            "top_procs": top_procs,
            "log": self.log_tail(),
        }

    def _pipeline(self, latest: "dict | None") -> "list[dict]":
        """The PR-centric board: one row per PR the newest poll pass saw.

        Each stage record already carries the PR slug and number, the round,
        the session and the review task ref; we add the repo slug (the stage's
        `slug` is `<owner>/<repo>#<n>`), the PR URL, the round cap the round is
        counted against, and the retry descriptor so a board row can drive
        retry-now directly. `attempt` rides along as the schema-parity field
        the daemon writes (always None -- the review loop is round-based).
        """
        if not latest:
            return []
        items = []
        for stage in latest.get("stages", []):
            slug = (stage.get("slug") or "").split("#", 1)[0]
            number = stage.get("number")
            round_ = stage.get("round")
            item = dict(stage)
            item["repo_slug"] = slug
            item["round_cap"] = self.config.round_cap
            item["url"] = f"https://github.com/{slug}/pull/{number}" if slug else None
            item["retry"] = (
                {"repo_slug": slug, "number": number, "round": round_}
                if slug and number is not None and round_ is not None
                else None
            )
            items.append(item)
        return items

    def _live_prs(
        self, latest: "dict | None", items: "list[dict]"
    ) -> "set[tuple[str, int]] | None":
        """The `(repo, number)` set an inbox page must still be in to be worth
        an operator's attention, or None when there is no evidence either way.

        The daemon's newest poll pass is the whole oracle: every PR with a
        review pending from the reviewer identity is a candidate, so a page
        whose PR is absent from the latest snapshot has had its trigger
        cleared -- merged, closed, or the review request withdrawn -- and
        nothing the console offers can act on it any more. A capped (or
        stability-held) PR that is still open keeps its review request and so
        stays in the set: its page is exactly the one that needs a re-entry
        ack, and it must not be filed away.

        Derived from the rendered board rows rather than the raw stages, so
        the inbox and the pipeline panel cannot disagree about what the pass
        had in hand. None (no snapshot at all -- a fresh boot, an unreadable
        state.db) means "no evidence", and the caller treats every row as
        live: the same rule the daemon's liveness oracle uses for a failed
        listing, because hiding a page on missing evidence is the one
        unrecoverable direction.
        """
        if not latest:
            return None
        keys = (
            _pr_key(item.get("repo_slug"), item.get("number")) for item in items
        )
        return {key for key in keys if key is not None}

    def _inbox(
        self,
        escalations: "list[dict]",
        pings: "list[dict]",
        stability_pings: "list[dict] | None" = None,
        live_prs: "set[tuple[str, int]] | None" = None,
    ) -> "dict[str, list[dict]]":
        """The operator inbox: everything the daemon paged a human about, in
        one list, newest first.

        Two tables feed it, because the daemon pages twice for different
        reasons: `escalations` is the CR9 cap-out (terminal for that head --
        the loop stops), `pings` carries the stalled-deferral episodes (the
        loop is still deferring behind a session that may be wedged). Both are
        PR references -- the reviewer edge has no issue edge -- so every link
        is a `/pull/` link. Ping kinds other than `stalled` are dedupe keys
        for telemetry, not operator pages, and never reach here: `read_pings`
        drops them in SQL, BEFORE its limit, so INBOX_LIMIT bounds pages and
        not the telemetry interleaved with them. The kind check below is what
        splits the session out of the kind, and re-checks the prefix in the
        process -- a caller that passed unfiltered rows still gets an inbox of
        pages only, just a shorter one.

        Bounded twice over: each reader is already capped at INBOX_LIMIT rows
        (newest first), and each returned list is capped again, so the payload
        cannot grow without bound as the never-pruned tables accumulate.

        Returns the rows split two ways -- `live` (what the operator still
        owes) and `settled` (the PR has left the poll's candidate set, so the
        page is exhaust). `escalations` and `pings` are dedupe key stores and
        must never be pruned, so this read-time split is the only place the
        distinction can be made, and it is made from the local snapshot alone:
        a page load still costs the GitHub API nothing. A row raised less than
        INBOX_LIVE_GRACE_INTERVALS poll intervals ago is live whatever the
        snapshot says, and `live_prs` of None (no snapshot) means every row is
        live -- see `_live_prs`.
        """
        now = int(self._wall())
        grace = INBOX_LIVE_GRACE_INTERVALS * self.config.poll_interval
        out: list[dict] = []
        for row in escalations:
            out.append(
                {
                    "kind": INBOX_CAP_OUT,
                    "repo_slug": row["repo"],
                    "number": row["number"],
                    "detail": str(row["head_sha"])[:8],
                    "age_seconds": max(0, now - int(row["escalated_at"])),
                    "url": f"https://github.com/{row['repo']}/pull/{row['number']}",
                }
            )
        for row in pings:
            kind, _, detail = str(row["kind"]).partition(":")
            if kind != ESCALATION_STALLED:
                continue
            out.append(
                {
                    "kind": INBOX_STALLED,
                    "repo_slug": row["repo"],
                    "number": row["number"],
                    "detail": detail,
                    "age_seconds": max(0, now - int(row["pinged_at"])),
                    "url": f"https://github.com/{row['repo']}/pull/{row['number']}",
                }
            )
        for row in stability_pings or []:
            parsed = parse_stability_kind(str(row["kind"]))
            if parsed is None:
                # A row this version does not understand is dropped rather than
                # rendered half-parsed: the inbox is read to decide whether to
                # act on a PR, and "stability-held at ???" invites the wrong
                # action.
                continue
            head, base, _granted = parsed
            out.append(
                {
                    "kind": INBOX_STABILITY,
                    "repo_slug": row["repo"],
                    "number": row["number"],
                    # Both shas, in the order the hold is stated in: the head
                    # the product stopped moving at, then the head it is still
                    # at. One sha would say "held" without saying since when.
                    "detail": f"{base[:8]}…{head[:8]}",
                    "age_seconds": max(0, now - int(row["pinged_at"])),
                    "url": f"https://github.com/{row['repo']}/pull/{row['number']}",
                }
            )
        out.sort(key=lambda item: item["age_seconds"])
        live: list[dict] = []
        settled: list[dict] = []
        for item in out:
            key = _pr_key(item["repo_slug"], item["number"])
            if (
                live_prs is None
                or item["age_seconds"] < grace
                or (key is not None and key in live_prs)
            ):
                live.append(item)
            else:
                settled.append(item)
        return {"live": live[:INBOX_LIMIT], "settled": settled[:INBOX_LIMIT]}
