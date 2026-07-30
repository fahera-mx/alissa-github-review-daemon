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

# The per-character half of the same rule, used to name the offending
# characters in an error without echoing the value (see _describe).
_ENV_CHAR_RE = re.compile(r"[A-Za-z0-9_]")

# ...which, on its own, accepts every GitHub credential format: `ghp_`, `gho_`,
# `ghu_`, `ghs_`, `ghr_` and `github_pat_` tokens are `[A-Za-z0-9_]` throughout
# and therefore valid identifiers. So the shape has to be rejected explicitly,
# or the one mistake this validation exists to catch -- pasting the token where
# its variable's NAME belongs -- sails through.
_TOKEN_SHAPE_RE = re.compile(r"^(gh[pousr]_|github_pat_)", re.IGNORECASE)

# Generous on purpose. A tighter ceiling (32 was tried) rejects names an
# operator can plausibly write -- `REVLOOP_REVIEWER_GITHUB_TOKEN_ENV` is 33 --
# and this bound is NOT what catches credentials: MIN_SECRET_RUN_LENGTH is,
# and it catches a 40-character hex token on its shape whatever the ceiling
# says. So the ceiling exists only to refuse the obviously absurd, and it is
# deliberately kept OUT of `_is_credential_shaped` (see there).
MAX_ENV_NAME_LENGTH = 40

# Length is not the whole discriminator, because a secret can be short enough
# to fit under the ceiling. The shape is: a long, undifferentiated run of
# alphanumerics. Names are conventionally SCREAMING_SNAKE and separated -- the
# underscore is what a legacy 40-hex token and a generic alnum API key lack.
MIN_SECRET_RUN_LENGTH = 20


def _is_credential_shaped(value: str) -> bool:
    """Whether a `reviewer_token_env` value looks like a pasted secret.

    A heuristic, and deliberately one that errs toward accusing: the cost of a
    false positive is renaming a variable, and the cost of a false negative is
    a live credential sitting in a config file on a shared volume.

    Length is deliberately NOT one of the clauses. It was, and it made a merely
    LONG name get classified as a credential -- rejected, redacted, and told to
    rotate itself. Over-length is a separate refusal with its own message; only
    an actual credential SHAPE drives redaction.
    """
    return bool(
        _TOKEN_SHAPE_RE.match(value)
        or (
            len(value) >= MIN_SECRET_RUN_LENGTH
            and "_" not in value
            and value.isalnum()
        )
    )


def _describe(value: str) -> str:
    """Say what is wrong with a rejected value without reprinting a secret.

    `__main__` prints this to stderr as `config error: …`, straight into the
    container log, so a pasted credential must never be echoed there. But the
    other way to fail this check is an ordinary typo
    (`REVLOOP-REVIEWER-GH-TOKEN`), and there the value IS the diagnostic.

    Redacting everything loses the typo case; echoing whatever the GitHub-shape
    heuristic does not recognise hands back the container-log disclosure for
    every non-GitHub secret (`sk-proj-…`, `xoxb-…`, `glpat-…` all carry
    punctuation and match no clause above). So neither branch prints the value:
    what an operator needs is WHICH CHARACTERS were rejected, and those can be
    named with their positions and nothing else.
    """
    if _is_credential_shaped(value):
        return f"a {len(value)}-character value that looks like a credential"
    offenders = sorted({c for c in value if not _ENV_CHAR_RE.match(c)})
    if offenders:
        where = ", ".join(
            f"{c!r} at {', '.join(str(i) for i, ch in enumerate(value) if ch == c)}"
            for c in offenders
        )
        return f"a {len(value)}-character value containing {where}"
    return f"a {len(value)}-character value"


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
    "max_concurrent_sessions",
    "checks_wait_seconds",
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

# The spawn gate: how many reviewer sessions of THIS daemon's own grammar may be
# live before an owed round waits for a slot instead of spawning (issue #70).
#
# Distinct from `reap_session_cap` above in kind, not just in number: that one is
# an ALARM on a condition the loop cannot fix (sessions the sweep could not
# reap), this one is a LIMIT the loop enforces on itself before it acts. Nothing
# bounded concurrency before it -- `round_cap` bounds rounds per PR, and the
# alarm only logs -- so a merge wave spawned one interactive claude session per
# PR, all at once, against a fixed container budget. On 2026-07-29 the 18:45-19:00Z
# burst pegged the deployment's 2 vCPU ceiling with 4+ concurrent reviewers plus
# the poll loop; throttled sessions review slower, hold their round slots longer,
# and widen the very burst that is starving them.
#
# 4 is the deployed shape's honest ceiling: two vCPUs, and a reviewer session is
# a full interactive agent. It is deliberately BELOW the reap alarm (6) so the
# steady state never pages -- and `Config.build` refuses a config where the alarm
# sits under the limit, which would page on healthy load.
DEFAULT_MAX_CONCURRENT_SESSIONS = 4

# How long a round holds its APPROVE while the head's CI rollup is still
# running (or unreadable) before it gives up and records the verdict as a
# COMMENT instead. An approve from the reviewer identity is the operator's cue
# to merge, so it must never claim a head whose checks this loop did not see
# conclude -- but the round cannot hold open forever either, or a CI system that
# never reports strands the PR outside the loop.
#
# 30 minutes is the trade: longer than any check suite in this fleet (the studio
# runs finish in single-digit minutes), short enough that a stuck rollup
# surfaces as a comment within the same working hour instead of the following
# day. Configurable because the right value is a property of a deployment's CI,
# not of the daemon.
DEFAULT_CHECKS_WAIT_SECONDS = 30 * 60


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

    # The spawn gate's limit -- see DEFAULT_MAX_CONCURRENT_SESSIONS. At or above
    # it an owed round defers to a later poll instead of spawning; it burns no
    # round number and no attempt while it waits.
    max_concurrent_sessions: int = DEFAULT_MAX_CONCURRENT_SESSIONS

    # The bound on holding a round's approve for a rollup that has not settled;
    # see DEFAULT_CHECKS_WAIT_SECONDS. 0 is legal and means "never hold": a
    # rollup that is not already green degrades the verdict to a comment on the
    # first poll that would have posted it.
    checks_wait_seconds: int = DEFAULT_CHECKS_WAIT_SECONDS

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

        max_sessions = int(
            raw.get("max_concurrent_sessions", cls.max_concurrent_sessions)
        )
        if max_sessions < 1:
            # 0 would defer every round forever: no session may spawn, so no
            # slot ever frees. "Review nothing" is not a tuning value.
            raise ValueError(
                f"max_concurrent_sessions must be >= 1, got {max_sessions}"
            )
        if session_cap < max_sessions:
            # The alarm would then fire on load the gate considers healthy --
            # every poll of a fully-loaded, correctly-behaving daemon pages the
            # operator, and a page that fires in the steady state trains people
            # to ignore the one that matters. Refused at load rather than
            # discovered at 3am.
            raise ValueError(
                f"reap_session_cap ({session_cap}) must be >= "
                f"max_concurrent_sessions ({max_sessions}): the cap is the "
                f"page-worthy alarm and the gate is the spawn limit, so an "
                f"alarm below the limit pages on healthy load"
            )

        checks_wait = int(raw.get("checks_wait_seconds", cls.checks_wait_seconds))
        if checks_wait < 0:
            raise ValueError(f"checks_wait_seconds must be >= 0, got {checks_wait}")

        token_env = raw.get("reviewer_token_env")
        if token_env is not None:
            token_env = str(token_env).strip()
            # The overwhelmingly likely way to get here is pasting the TOKEN in
            # place of the variable's name, which would leave a live credential
            # in a config file on a shared volume and then fail as "unset
            # variable" — an error that reads as though the secret were missing
            # rather than exposed. So the check is two-sided: it must LOOK like
            # a name, and it must not look like a credential. The value itself
            # is never echoed back (see _redact).
            if (
                not _ENV_NAME_RE.match(token_env)
                or _is_credential_shaped(token_env)
                or len(token_env) > MAX_ENV_NAME_LENGTH
            ):
                rotate = (
                    " If that is a credential, rotate it: it is now in a config file."
                    if _is_credential_shaped(token_env)
                    else ""
                )
                raise ValueError(
                    f"reviewer_token_env must be an environment variable NAME "
                    f"(e.g. 'REVLOOP_REVIEWER_GH_TOKEN') of at most "
                    f"{MAX_ENV_NAME_LENGTH} characters, not a value or a "
                    f"token — got {_describe(token_env)}.{rotate}"
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
            max_concurrent_sessions=max_sessions,
            checks_wait_seconds=checks_wait,
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
