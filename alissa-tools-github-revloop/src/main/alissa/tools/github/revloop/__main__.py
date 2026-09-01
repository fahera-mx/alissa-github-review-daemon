"""CLI entry point: alissa-revloop (or python -m alissa.tools.github.revloop)"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from .config import (
    HUB_ADD,
    HUB_SKIP,
    LOOP_EVENTS_ENV,
    ON_MISSING_CREATE,
    ON_MISSING_SKIP,
    ON_MISSING_SPAWN,
    TASK_LIST_BOW_ENV,
    Config,
    load_config_file,
    resolve_config_path,
)
from .ghclient import IdentityMismatch
from .loop import LedgerUnwritable, ReviewWatcher
from .proc import CommandError

log = logging.getLogger(__name__)


def parse_pr_ref(ref: str) -> tuple[str, str, int]:
    """Parse `owner/repo#123` (or a full PR URL) into its parts."""
    match = re.search(r"([\w.-]+)/([\w.-]+?)(?:#|/pull/)(\d+)", ref)
    if not match:
        raise ValueError(f"expected OWNER/REPO#N or a PR URL, got {ref!r}")
    return match.group(1), match.group(2), int(match.group(3))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alissa-revloop",
        description="Watch GitHub for review requests and run the adversarial "
        "review loop (alissa-code-review CR1-CR9).",
        epilog="Every setting below can also live in the config file; CLI "
        "arguments win. workspace_root is CLI-only, so one config can drive "
        "several daemons over different workspaces.",
    )

    p.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        metavar="PATH",
        help="the Alissa Code Workspace to watch (default: current directory)",
    )
    p.add_argument(
        "-c",
        "--config-path",
        "--config",
        dest="config_path",
        type=Path,
        default=None,
        metavar="PATH",
        help="config file; without it, ./revloop.config.json then "
        "<workspace-root>/revloop.config.json, else defaults only",
    )

    mode = p.add_argument_group("mode")
    mode.add_argument("--once", action="store_true", help="run a single poll pass and exit")
    mode.add_argument(
        "--pr",
        metavar="OWNER/REPO#N",
        help="evaluate one PR directly, bypassing the search — use this to tell "
        "'the search did not find it' apart from 'the decision was no'",
    )
    mode.add_argument("-v", "--verbose", action="store_true")

    over = p.add_argument_group("config overrides (win over the config file)")
    over.add_argument(
        "--repo",
        dest="repos",
        action="append",
        metavar="OWNER/REPO",
        help="only watch this repo; repeatable. Replaces the config list entirely.",
    )
    over.add_argument(
        "--author",
        dest="authors",
        action="append",
        metavar="LOGIN",
        help="only review PRs opened by this GitHub login; repeatable. "
        "Replaces the config list entirely. Empty = every author is served "
        "(a scope filter, not a grant); the self-review skip still applies.",
    )
    over.add_argument(
        "--operator",
        dest="operators",
        action="append",
        metavar="LOGIN",
        help="GitHub login allowed to ack a review-loop re-entry "
        "(`alissa-review: re-enter +N` on a capped PR); repeatable. Replaces "
        "the config list entirely. Empty = no ack is ever honoured.",
    )
    over.add_argument("--poll-interval", type=int, metavar="SECONDS")
    over.add_argument("--round-cap", type=int, metavar="N", help="CR9 round cap")
    over.add_argument(
        "--stability-rounds",
        type=int,
        metavar="N",
        help="PRODUCT-STABILITY GUARD: hold the loop once the shipped-product "
        "diff has been empty for N consecutive request_changes rounds (0 "
        "disables the guard entirely)",
    )
    over.add_argument("--hub-template", metavar="TEMPLATE")
    over.add_argument("--agent-profile", metavar="NAME")
    over.add_argument("--reviewer-login", metavar="LOGIN")
    over.add_argument(
        "--reviewer-token-env",
        metavar="VAR",
        help="NAME of the environment variable holding the reviewer "
        "identity's GitHub token (never the token itself). Set it and every "
        "`gh` call runs under that credential explicitly instead of whatever "
        "the container happened to export",
    )
    over.add_argument("--state-path", type=Path, metavar="PATH")
    over.add_argument(
        "--on-missing-review-task",
        choices=[ON_MISSING_SPAWN, ON_MISSING_CREATE, ON_MISSING_SKIP],
    )
    over.add_argument("--on-missing-hub", choices=[HUB_SKIP, HUB_ADD])
    over.add_argument(
        "--reap-grace-seconds",
        type=int,
        metavar="SECONDS",
        help="how long a reviewer session must be idle AND quiet before the "
        "sweep reaps it (and before a stale round reads it as dead)",
    )
    over.add_argument(
        "--reap-session-cap",
        type=int,
        metavar="N",
        help="page-worthy threshold: more live reviewer sessions than this "
        "after a sweep and the daemon logs loudly",
    )
    over.add_argument(
        "--max-concurrent-sessions",
        type=int,
        metavar="N",
        help="spawn gate: at this many live reviewer sessions an owed round "
        "waits for a slot instead of spawning (must be <= --reap-session-cap)",
    )
    over.add_argument(
        "--checks-wait-seconds",
        type=int,
        metavar="SECONDS",
        help="how long a round holds its approve while the judged head's CI "
        "rollup is still running (or unreadable) before recording the verdict "
        "as a comment instead; a red rollup never waits and never approves",
    )
    over.add_argument(
        "--checks-spawn-wait-seconds",
        type=int,
        metavar="SECONDS",
        help="how long an owed round waits for the head's CI to conclude before "
        "its reviewer is queued at all, so a session cannot approve ahead of the "
        "evidence; 0 queues immediately and relies on the directive alone",
    )

    over.add_argument(
        "--review-task-miss-ttl-polls",
        type=int,
        metavar="N",
        help="how many polls a PR with NO review task is taken on trust before "
        "the task corpus is searched for one again; must be >= 1",
    )

    scope = over.add_mutually_exclusive_group()
    scope.add_argument(
        "--task-list-self-scope",
        dest="task_list_self_scope",
        action="store_true",
        default=None,
        help="narrow `alissa task list` to this actor's own rows (--self), "
        "dropping the sponsor's corpus. Only for deployments where EVERY "
        "review task is created by this daemon's own sessions: a review task "
        "the list cannot see is a round the daemon cannot count",
    )
    scope.add_argument(
        "--no-task-list-self-scope",
        dest="task_list_self_scope",
        action="store_false",
        help="list the sponsor-union corpus even if the config narrows it",
    )

    over.add_argument(
        "--task-list-bow",
        dest="task_list_bow_id",
        metavar="BOW_ID",
        help="scope `alissa task list` to one body of work (--bow): the Convex "
        "_id of the BOW review tasks are created into, NOT a repo's `autodev:` "
        "feed BOW and not a mirrorInstanceId. A review task outside it is "
        f"invisible to this daemon. Overridden by ${TASK_LIST_BOW_ENV}",
    )

    events = over.add_mutually_exclusive_group()
    events.add_argument(
        "--loop-events",
        dest="loop_events_enabled",
        action="store_true",
        default=None,
        help="push loop telemetry (rounds spawned, verdicts, cap-outs, "
        "stability holds, stalls, checks holds) to Studio's POST "
        "/v1/loop-events once per poll pass — best-effort, never fatal. "
        f"Overridden by ${LOOP_EVENTS_ENV}",
    )
    events.add_argument(
        "--no-loop-events",
        dest="loop_events_enabled",
        action="store_false",
        help="do not push loop telemetry even if the config enables it",
    )
    over.add_argument(
        "--alissa-endpoint",
        dest="alissa_endpoint",
        metavar="URL",
        help="the Alissa API base the loop-events client posts to "
        "(default: https://api.alissa.app)",
    )

    dry = over.add_mutually_exclusive_group()
    dry.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="decide and log, but never enqueue a session or comment",
    )
    dry.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="act for real even if the config sets dry_run",
    )
    return p


def overrides_from(args: argparse.Namespace) -> dict:
    """CLI values, with None meaning 'not specified' so the config file shows
    through. `repos` becomes a tuple so it matches the config-file form."""
    return {
        "repos": tuple(args.repos) if args.repos else None,
        "authors": tuple(args.authors) if args.authors else None,
        "operators": tuple(args.operators) if args.operators else None,
        "poll_interval": args.poll_interval,
        "round_cap": args.round_cap,
        "stability_rounds": args.stability_rounds,
        "hub_template": args.hub_template,
        "agent_profile": args.agent_profile,
        "reviewer_login": args.reviewer_login,
        "reviewer_token_env": args.reviewer_token_env,
        "state_path": args.state_path,
        "on_missing_review_task": args.on_missing_review_task,
        "on_missing_hub": args.on_missing_hub,
        "reap_grace_seconds": args.reap_grace_seconds,
        "reap_session_cap": args.reap_session_cap,
        "max_concurrent_sessions": args.max_concurrent_sessions,
        "checks_wait_seconds": args.checks_wait_seconds,
        "checks_spawn_wait_seconds": args.checks_spawn_wait_seconds,
        "review_task_miss_ttl_polls": args.review_task_miss_ttl_polls,
        "task_list_self_scope": args.task_list_self_scope,
        "task_list_bow_id": args.task_list_bow_id,
        "loop_events_enabled": args.loop_events_enabled,
        "alissa_endpoint": args.alissa_endpoint,
        "dry_run": args.dry_run,
    }


def resolve_config(args: argparse.Namespace) -> Config:
    workspace_root = args.workspace_root or Path.cwd()
    path = resolve_config_path(args.config_path, workspace_root)

    file_data = load_config_file(path) if path else {}
    log.info("config: %s", path or "none found — defaults + CLI arguments only")

    return Config.build(workspace_root, file_data, overrides_from(args))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    # STARTUP and STEADY STATE are separated on purpose (issue #62). The
    # handlers below label a FileNotFoundError / ValueError "config error" and
    # exit 2, which is right for the startup phase -- a missing config file or
    # an unparseable value cannot be fixed by trying again. It was fatally
    # wrong for the poll loop, where the same classes mean a transient
    # subprocess ENOENT or a bad response: `run_forever` now firewalls those
    # per iteration and never lets them reach here at all. The one-shot modes
    # (`--pr`, `--once`) still surface a failure to their caller, as a
    # one-shot must -- including a refused pass, which leaves through the
    # LedgerUnwritable handler below with exit 1 rather than looking clean.
    try:
        config = resolve_config(args)
        log.info("workspace: %s", config.workspace_root)

        watcher = ReviewWatcher(config)
        for warning in watcher.preflight():
            log.warning(warning)

        if args.pr:
            owner, repo, number = parse_pr_ref(args.pr)
            # This mode exists to tell "the search did not find it" apart from
            # "the decision was no", so it must never be answered by the
            # negative cache -- a suppressed pass would report "no review task"
            # without looking, which is precisely the confusion the flag is for.
            # Re-arming here (rather than plumbing a bypass through `evaluate`)
            # keeps the poll path with exactly one way in, and costs the daemon
            # one corpus fetch on a hand-run diagnostic.
            watcher.state.forget_review_task_miss(f"{owner}/{repo}", number)
            decision = watcher.evaluate(owner, repo, number)
            print(f"\n{args.pr} → {decision.action.value}")
            print(f"  round:  {decision.round}")
            print(f"  reason: {decision.reason or '—'}")
        elif args.once:
            watcher.poll_once()
        else:
            watcher.run_forever()
    except LedgerUnwritable as exc:
        # Only reachable from `--once`: run_forever handles its own refusals and
        # keeps polling. A one-shot REPORTS rather than retries, so this must
        # not look like a clean pass to `... --once && echo ok` or to a health
        # probe. Exit 1 ("the environment failed"), not the 2 reserved for
        # "your config is wrong" -- a config error tells you to edit a file, an
        # unwritable ledger tells you to look at the volume mount.
        print(
            f"ledger error: {exc} is not writable — no decisions were taken. "
            "The daemon refuses to spawn, escalate, grant or post what it "
            "cannot record; fix the volume mount or its ownership.",
            file=sys.stderr,
        )
        return 1
    except IdentityMismatch as exc:
        print(f"identity error: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
