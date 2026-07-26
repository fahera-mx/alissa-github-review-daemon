"""The console's read-only data layer (plus the one retry-now UPDATE).

Every panel the dashboard renders is assembled here from four kinds of source,
in strict budget order:

1. **The local state.db** -- poll snapshots (through `State.read_snapshots`, the
   UI-1 reader), the spawn ledger, the escalation table and the ping ledger. No
   GitHub call: the daemon already wrote everything down.
2. **Local process state** -- `alissa tmux ls --json` for the session list, and
   a `/proc` walk (sysinfo) of each session's pane-PID tree for CPU%/RSS.
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

The single mutation is `retry_now`: it ages the newest ledger row of one round
past the stale window (an UPDATE, via `State.age_out_spawn`), so the daemon's
own re-enqueue path can respawn it on its next pass. No new retry logic lives
here -- the console only moves a clock the daemon already reads.
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
from ..loop import ESCALATION_STALLED, STALE_ROUND_SECONDS
from ..proc import CommandError, run as proc_run
from ..state import State
from ..version import Version
from . import sysinfo

# How many recent snapshots feed the sparklines / pipeline board.
SPARK_POINTS = 60
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
    (an unreachable PyPI keeps showing the last known latest version)."""

    def __init__(self, ttl: float, clock: Callable[[], float]) -> None:
        self._ttl = ttl
        self._clock = clock
        self._value: object = None
        self._expires = 0.0

    def get(self, fetch: Callable[[], object]) -> object:
        now = self._clock()
        if self._value is None or now >= self._expires:
            fresh = fetch()
            if fresh is not None:
                self._value = fresh
                self._expires = now + self._ttl
            elif self._value is not None:
                # keep the stale-but-good value; retry next call
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
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.running_version = running_version
        self.log_path = Path(log_path) if log_path else None
        self._run = run
        self._http_get = http_get
        self._proc_root = proc_root
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

    def _read_state(self, default, fn):
        """Open a short-lived `State` and run `fn(state)`, degrading to
        `default` if the db can't be read/written. The daemon writes state.db
        from a separate process; without WAL a console read/write can collide
        with a daemon write and raise `sqlite3.OperationalError` (a corrupt db
        raises regardless). Catching here honors this module's contract: a
        state access never raises an exception that would blank the dashboard
        or 500 the retry action -- it degrades like every other source."""
        try:
            with State(self.config.state_db) as st:
                return fn(st)
        except sqlite3.Error:
            return default

    def snapshots(self, limit: int = SPARK_POINTS) -> "list[dict]":
        return self._read_state([], lambda st: st.read_snapshots(limit))

    def ledgers(self) -> dict:
        empty: "dict[str, list]" = {"spawns": [], "escalations": [], "pings": []}
        return self._read_state(empty, lambda st: {
            "spawns": st.read_spawns(),
            "escalations": st.read_escalations(),
            "pings": st.read_pings(),
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

    def sessions(self, spawns: "list[dict] | None" = None) -> "list[dict]":
        """The managed-session table: liveness from `alissa tmux ls`, footprint
        from /proc, and the PR round each session is reviewing.

        The round comes from the spawn ledger (session name is its primary
        key), not from the session name itself: the name carries a nonce and
        a rename would silently break the pairing, whereas the ledger row IS
        what the daemon's own reap sweep consults. A session with no ledger
        row is simply unpaired -- the panel shows it without a PR.
        """
        by_session = {
            row["session"]: row for row in (spawns if spawns is not None else [])
        }
        raw = self._safe_json(["alissa", "tmux", "ls", "--json"]) or []
        if not isinstance(raw, list):
            return []
        now = int(self._wall())
        out: list[dict] = []
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
                    usage = sysinfo.tree_usage(pid, proc_root=self._proc_root)
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

    def retry_now(self, repo_slug: str, number: int, round_: int) -> bool:
        """Age the newest ledger row of one round past the stale window so the
        daemon can respawn it next pass. Returns whether a row was actually
        aged (False when the round has no ledger row to retry).

        The console adds no retry logic of its own: the daemon decides whether
        an aged round is really dead (its liveness signal defers a respawn
        behind a session that still shows life), so retry-now only moves the
        clock that decision reads.
        """
        new_ts = int(self._wall()) - STALE_ROUND_SECONDS - RETRY_AGE_BUFFER

        # A write collision with the daemon degrades to False (a clean "no row
        # aged" action failure) rather than surfacing as a 500 on the operator's
        # Retry click -- same contract as the read paths above.
        return self._read_state(
            False, lambda st: st.age_out_spawn(repo_slug, number, round_, new_ts)
        )

    # -- the assembled dashboard payload -----------------------------------

    def dashboard(self) -> dict:
        """The single JSON document the client polls (~10s). Assembled from all
        sources above -- shaped, not raw -- so the page is pure rendering."""
        snaps = self.snapshots(SPARK_POINTS)
        latest = snaps[0] if snaps else None
        ledgers = self.ledgers()
        sessions = self.sessions(ledgers["spawns"])
        rate = self.rate_limit()
        disk = sysinfo.disk_usage(self.config.workspace_root)

        # Sparklines want oldest -> newest for left-to-right drawing. "Active"
        # counts both buckets a live reviewer session sits in: a round enqueued
        # and still working (in_flight) and one whose respawn is deferred
        # behind a session that still shows life (deferred).
        chrono = list(reversed(snaps))
        sparklines = {
            "poll_duration_ms": [s["duration_ms"] for s in chrono],
            "active_sessions": [s["in_flight"] + s["deferred"] for s in chrono],
        }

        return {
            "generated_at": int(self._wall()),
            "header": {
                "drift": self.drift(),
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
                "queue_depth": latest["candidates"] if latest else 0,
            },
            "sparklines": sparklines,
            "pipeline": {
                "snapshot_ts": latest["ts"] if latest else None,
                "duration_ms": latest["duration_ms"] if latest else None,
                "round_cap": self.config.round_cap,
                "items": self._pipeline(latest),
            },
            "inbox": self._inbox(ledgers["escalations"], ledgers["pings"]),
            "sessions": sessions,
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

    def _inbox(
        self, escalations: "list[dict]", pings: "list[dict]"
    ) -> "list[dict]":
        """The operator inbox: everything the daemon paged a human about, in
        one list, newest first.

        Two tables feed it, because the daemon pages twice for different
        reasons: `escalations` is the CR9 cap-out (terminal for that head --
        the loop stops), `pings` carries the stalled-deferral episodes (the
        loop is still deferring behind a session that may be wedged). Both are
        PR references -- the reviewer edge has no issue edge -- so every link
        is a `/pull/` link. Ping kinds other than `stalled` are dedupe keys
        for telemetry, not operator pages, and are filtered out.
        """
        now = int(self._wall())
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
        out.sort(key=lambda item: item["age_seconds"])
        return out
