"""Daemon configuration.

Settings come from three layers, later winning over earlier:

1. the defaults on `Config`
2. a JSON config file (see `resolve_config_path`)
3. CLI arguments

`workspace_root` is deliberately **not** a config key — it is a property of the
running process, not of the settings. That lets one config file drive several
daemons over different workspaces on the same machine, each pointed with
`--workspace-root` and narrowed with `--repo`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# A POSIX-ish environment variable name -- what `reviewer_token_env` must be.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# What to do when a PR has a pending review request but no matching Alissa
# review task (CR2 is implementer-side, so a third-party PR may not have one).
ON_MISSING_SPAWN = "spawn_anyway"  # review anyway, PR URL carries the context
ON_MISSING_SKIP = "skip"  # ignore the PR until a review task appears
ON_MISSING_CREATE = "warn_and_spawn"  # spawn, but log loudly

_MISSING_MODES = {ON_MISSING_SPAWN, ON_MISSING_SKIP, ON_MISSING_CREATE}

# What to do when a review arrives for a repo that has no worktree hub yet.
HUB_SKIP = "skip"
HUB_ADD = "add"  # `alissa code workspace add <org>/<repo>`

_HUB_MODES = {HUB_SKIP, HUB_ADD}

CONFIG_FILENAME = "revloop.config.json"

# Keys accepted in the config file. workspace_root is excluded on purpose.
CONFIG_KEYS = (
    "hub_template",
    "poll_interval",
    "round_cap",
    "repos",
    "operators",
    "agent_profile",
    "reviewer_login",
    "reviewer_token_env",
    "state_path",
    "on_missing_review_task",
    "on_missing_hub",
    "reap_grace_seconds",
    "reap_session_cap",
    "dry_run",
)

MIN_POLL_INTERVAL = 10  # the search API allows 30 req/min

# A reviewer session that has not submitted after this long is presumed dead
# (skill failure mode: "reviewer session stalls"). The round is re-enqueued --
# but only with a second signal agreeing: the timer alone cannot tell a dead
# session from a slow one, and a timer-only re-enqueue double-spends the round
# (two sessions review it, both submit -- observed live twice: double round-2
# approves on devloop's PR #11, double approves on this repo's PR #19). See
# loop._defer_stale_round for the liveness signal.
#
# It lives here rather than in `loop` (which re-exports it, so every import
# site is unchanged) because `reap_grace_seconds` is validated against it, and
# config cannot import loop without a cycle.
STALE_ROUND_SECONDS = 90 * 60

# How long a reviewer session must have been idle AND quiet (no tmux activity)
# before the sweep may reap it, and before the stale-round liveness probe
# reads it as finished rather than working.
#
# Both readings need the same number because they ask the same question -- "is
# this session done?" -- and a claude session parked at its prompt between
# turns reports "idle", so only the absence of activity can answer it. The
# default is generous on purpose: a just-merged PR's reviewer still has
# in-session close-out to do (CR6 envelope, task move, its own self-kill), and
# reaping under it loses that work for nothing. Configurable because the right
# value tracks how long a round's close-out takes on a given deployment.
DEFAULT_REAP_GRACE_SECONDS = 30 * 60

# Post-sweep alarm threshold: more live reviewer sessions than this and the
# sweep logs page-worthy, because the reaper is not keeping up with the spawn
# rate. Each idle agent session holds hundreds of MB forever, and the worker
# container is shared -- the 2026-07-28 incident was this exact drift, climbing
# past 10 GB with every review session idle and its PR long merged. A healthy
# loop runs a couple of concurrent rounds, so the default is a threshold no
# healthy deployment reaches, not a capacity limit.
DEFAULT_REAP_SESSION_CAP = 6


def default_state_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / ".revloop" / "state.db"


@dataclass(frozen=True)
class Config:
    # A property of the process, supplied by --workspace-root (default: cwd).
    workspace_root: Path

    hub_template: str = "{root}/{repo}/main"
    poll_interval: int = 60
    round_cap: int = 10  # CR9 default

    # Empty tuple means "every repo that requests a review from me".
    repos: tuple[str, ...] = ()

    # GitHub logins whose re-entry ack may raise a capped PR's effective cap
    # (loop.parse_reentry_ack). Empty -- the default -- means NO ack is ever
    # honoured: the lever fails closed, because anyone who can comment on a PR
    # could otherwise buy it more rounds. The reviewer identity itself is never
    # an operator however it is configured (the daemon's own escalation quotes
    # the grammar, and self-granting would defeat CR9's cap outright).
    operators: tuple[str, ...] = ()

    agent_profile: str = "claude"
    reviewer_login: str | None = None  # None -> resolve once via `gh api user`

    # NAME of the environment variable holding the reviewer identity's GitHub
    # token -- never the token itself, which has no business in a config file
    # on a mounted volume. Set it and every `gh` call the daemon makes runs
    # under that credential explicitly, with the container's inherited
    # GH_TOKEN/GITHUB_TOKEN stripped; leave it None and the daemon inherits,
    # which is the pre-#51 behaviour and warns loudly at preflight because a
    # shared container is exactly where inheritance picks the wrong identity.
    reviewer_token_env: str | None = None

    # None means "derive from the workspace" -- read `state_db` for the
    # resolved location, never this field.
    state_path: Path | None = None

    on_missing_review_task: str = ON_MISSING_SPAWN
    on_missing_hub: str = HUB_SKIP

    # The reap sweep's two knobs -- see the constants above for what each one
    # buys and why it is tunable rather than pinned.
    reap_grace_seconds: int = DEFAULT_REAP_GRACE_SECONDS
    reap_session_cap: int = DEFAULT_REAP_SESSION_CAP

    dry_run: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "workspace_root", Path(self.workspace_root).expanduser().resolve()
        )

    @property
    def state_db(self) -> Path:
        """Where the spawn ledger lives. Defaults inside the workspace so two
        daemons watching different workspaces never share one."""
        if self.state_path is None:
            return default_state_path(self.workspace_root)
        return Path(self.state_path).expanduser()

    @property
    def manifest_path(self) -> Path:
        return self.workspace_root / "alissa-workspace.yaml"

    def hub_for(self, owner: str, repo: str) -> Path:
        return Path(
            self.hub_template.format(
                root=str(self.workspace_root), owner=owner, repo=repo
            )
        ).expanduser()

    def watches(self, full_name: str) -> bool:
        return not self.repos or full_name in self.repos

    @classmethod
    def build(
        cls,
        workspace_root: Path,
        file_data: Mapping[str, Any] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> "Config":
        """Merge the layers and validate. `overrides` entries that are None mean
        "not specified on the CLI" and fall through to the file / defaults."""
        raw: dict[str, Any] = dict(file_data or {})

        if "workspace_root" in raw:
            raise ValueError(
                "workspace_root is not a config key — it is a property of the "
                "running process. Pass --workspace-root (or run the daemon from "
                "the workspace), and remove it from the config file."
            )

        # Allow "_"-prefixed keys as inline comments, since JSON has none.
        unknown = {k for k in set(raw) - set(CONFIG_KEYS) if not k.startswith("_")}
        if unknown:
            raise ValueError(
                f"unknown config key(s): {', '.join(sorted(unknown))}. "
                f"Valid keys: {', '.join(CONFIG_KEYS)}"
            )

        for key, value in (overrides or {}).items():
            if value is not None:
                raw[key] = value

        mode = raw.get("on_missing_review_task", ON_MISSING_SPAWN)
        if mode not in _MISSING_MODES:
            raise ValueError(
                f"on_missing_review_task must be one of {sorted(_MISSING_MODES)}, got {mode!r}"
            )

        hub_mode = raw.get("on_missing_hub", HUB_SKIP)
        if hub_mode not in _HUB_MODES:
            raise ValueError(
                f"on_missing_hub must be one of {sorted(_HUB_MODES)}, got {hub_mode!r}"
            )

        repos = _string_list(raw.get("repos", ()), "repos", "owner/repo entries")
        operators = _string_list(
            raw.get("operators", ()), "operators", "GitHub logins"
        )
        if hub_mode == HUB_ADD and not repos:
            # Anyone who can request a review could otherwise cause an arbitrary
            # repo to be cloned onto this machine and opened as an agent's cwd.
            raise ValueError(
                "on_missing_hub='add' requires a non-empty repos allowlist "
                "(config `repos`, or one or more --repo flags) — auto-cloning "
                "whatever repo requests a review is unbounded"
            )

        cap = int(raw.get("round_cap", cls.round_cap))
        if cap < 1:
            raise ValueError(f"round_cap must be >= 1, got {cap}")

        interval = int(raw.get("poll_interval", 60))
        if interval < MIN_POLL_INTERVAL:
            raise ValueError(
                f"poll_interval must be >= {MIN_POLL_INTERVAL} seconds, got {interval}"
            )

        grace = int(raw.get("reap_grace_seconds", cls.reap_grace_seconds))
        if grace < 0:
            raise ValueError(f"reap_grace_seconds must be >= 0, got {grace}")
        if grace >= STALE_ROUND_SECONDS:
            # The same number answers the stale-round liveness probe, whose
            # own window is STALE_ROUND_SECONDS. At or above it the
            # "idle-finished -> dead -> respawn" branch is unreachable: a
            # session can never have been quiet longer than the grace by the
            # time its round goes stale, so every stale round defers forever
            # and only the operator ping ever fires. Loud here rather than
            # silently wedged there.
            raise ValueError(
                f"reap_grace_seconds must be well under the {STALE_ROUND_SECONDS}s "
                f"stale-round window (it also gates the stale-round liveness "
                f"probe, whose respawn branch it would make unreachable), "
                f"got {grace}"
            )

        session_cap = int(raw.get("reap_session_cap", cls.reap_session_cap))
        if session_cap < 1:
            # 0 would page on every single live reviewer, which is the normal
            # state of a working loop -- an alarm that always fires is noise.
            raise ValueError(f"reap_session_cap must be >= 1, got {session_cap}")

        token_env = raw.get("reviewer_token_env")
        if token_env is not None:
            token_env = str(token_env).strip()
            if not _ENV_NAME_RE.match(token_env):
                # The overwhelmingly likely way to get here is pasting the
                # TOKEN in place of the variable's name, which would leave a
                # live credential in a config file on a shared volume and then
                # fail as "unset variable" — an error that reads as though the
                # secret were missing rather than exposed. Say what it is.
                raise ValueError(
                    f"reviewer_token_env must be an environment variable NAME "
                    f"(e.g. 'REVLOOP_REVIEWER_GH_TOKEN'), not a value or a "
                    f"token, got {token_env!r}"
                )

        state_path = raw.get("state_path")
        return cls(
            workspace_root=Path(workspace_root),
            hub_template=raw.get("hub_template", cls.hub_template),
            poll_interval=interval,
            round_cap=cap,
            repos=repos,
            operators=operators,
            agent_profile=raw.get("agent_profile", "claude"),
            reviewer_login=raw.get("reviewer_login"),
            reviewer_token_env=token_env or None,
            state_path=Path(state_path).expanduser() if state_path else None,
            on_missing_review_task=mode,
            on_missing_hub=hub_mode,
            reap_grace_seconds=grace,
            reap_session_cap=session_cap,
            dry_run=bool(raw.get("dry_run", False)),
        )


def _string_list(value: Any, key: str, what: str) -> tuple[str, ...]:
    """A config list key as a tuple of non-empty, stripped strings.

    The guard is the point: JSON makes `"repos": "org/repo"` an easy typo, and
    Python would iterate it into single CHARACTERS -- an allowlist of 30-odd
    one-character names, which `watches()` then matches against nothing and the
    daemon quietly reviews no PR at all. Both list keys go through here so
    neither can grow the footgun back.
    """
    if isinstance(value, str):
        raise ValueError(
            f"{key} must be a list of {what}, not a string (got {value!r})"
        )
    return tuple(str(item).strip() for item in value if str(item).strip())


def load_config_file(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).expanduser().read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object, got {type(data).__name__}")
    return data


def resolve_config_path(
    explicit: Path | None, workspace_root: Path, cwd: Path | None = None
) -> Path | None:
    """Find the config file: explicit path, then cwd, then the workspace root.

    Returns None when no config file exists — CLI arguments and defaults alone
    are a valid way to run. An explicit path that does not exist is an error.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
        return path

    cwd = Path.cwd() if cwd is None else Path(cwd)
    for candidate in (cwd / CONFIG_FILENAME, Path(workspace_root) / CONFIG_FILENAME):
        if candidate.is_file():
            return candidate
    return None
