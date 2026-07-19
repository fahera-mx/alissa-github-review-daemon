"""Daemon configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(frozen=True)
class Config:
    # Workspace layout (alissa-code-workspace R1-R14). The reviewer session's
    # cwd is the hub's pristine main/ mirror -- reviewers never write (CR6).
    workspace_root: Path
    hub_template: str = "{root}/{repo}/main"

    poll_interval: int = 60  # seconds; search API allows 30 req/min
    round_cap: int = 3  # CR9 default

    # Empty tuple means "every repo that requests a review from me".
    repos: tuple[str, ...] = ()

    agent_profile: str = "claude"
    reviewer_login: str | None = None  # None -> resolve once via `gh api user`

    state_path: Path = Path("~/.local/state/reviewloop/state.db")
    on_missing_review_task: str = ON_MISSING_SPAWN
    on_missing_hub: str = HUB_SKIP
    dry_run: bool = False

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
    def load(cls, path: Path) -> "Config":
        raw = json.loads(Path(path).expanduser().read_text())

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
                "on_missing_hub='add' requires a non-empty `repos` allowlist — "
                "auto-cloning whatever repo requests a review is unbounded"
            )

        cap = int(raw.get("round_cap", 3))
        if cap < 1:
            raise ValueError(f"round_cap must be >= 1, got {cap}")

        interval = int(raw.get("poll_interval", 60))
        if interval < 10:
            # 30 search req/min ceiling; anything under 10s risks 403 backoff
            raise ValueError(f"poll_interval must be >= 10 seconds, got {interval}")

        return cls(
            workspace_root=Path(raw["workspace_root"]).expanduser(),
            hub_template=raw.get("hub_template", cls.hub_template),
            poll_interval=interval,
            round_cap=cap,
            repos=repos,
            agent_profile=raw.get("agent_profile", "claude"),
            reviewer_login=raw.get("reviewer_login"),
            state_path=Path(
                raw.get("state_path", "~/.local/state/reviewloop/state.db")
            ).expanduser(),
            on_missing_review_task=mode,
            on_missing_hub=hub_mode,
            dry_run=bool(raw.get("dry_run", False)),
        )
