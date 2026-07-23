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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

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
    "agent_profile",
    "reviewer_login",
    "state_path",
    "on_missing_review_task",
    "on_missing_hub",
    "dry_run",
)

MIN_POLL_INTERVAL = 10  # the search API allows 30 req/min


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

    agent_profile: str = "claude"
    reviewer_login: str | None = None  # None -> resolve once via `gh api user`

    # None means "derive from the workspace" -- read `state_db` for the
    # resolved location, never this field.
    state_path: Path | None = None

    on_missing_review_task: str = ON_MISSING_SPAWN
    on_missing_hub: str = HUB_SKIP
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

        repos = tuple(raw.get("repos", ()))
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

        state_path = raw.get("state_path")
        return cls(
            workspace_root=Path(workspace_root),
            hub_template=raw.get("hub_template", cls.hub_template),
            poll_interval=interval,
            round_cap=cap,
            repos=repos,
            agent_profile=raw.get("agent_profile", "claude"),
            reviewer_login=raw.get("reviewer_login"),
            state_path=Path(state_path).expanduser() if state_path else None,
            on_missing_review_task=mode,
            on_missing_hub=hub_mode,
            dry_run=bool(raw.get("dry_run", False)),
        )


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
