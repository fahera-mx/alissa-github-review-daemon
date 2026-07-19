"""CLI entry point: alissa-reviewloop (or python -m alissa.tools.github.reviewloop)"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from .config import (
    HUB_ADD,
    HUB_SKIP,
    ON_MISSING_CREATE,
    ON_MISSING_SKIP,
    ON_MISSING_SPAWN,
    Config,
    load_config_file,
    resolve_config_path,
)
from .ghclient import IdentityMismatch
from .loop import ReviewWatcher
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
        prog="alissa-reviewloop",
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
        help="config file; without it, ./reviewloop.config.json then "
        "<workspace-root>/reviewloop.config.json, else defaults only",
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
    over.add_argument("--poll-interval", type=int, metavar="SECONDS")
    over.add_argument("--round-cap", type=int, metavar="N", help="CR9 round cap")
    over.add_argument("--hub-template", metavar="TEMPLATE")
    over.add_argument("--agent-profile", metavar="NAME")
    over.add_argument("--reviewer-login", metavar="LOGIN")
    over.add_argument("--state-path", type=Path, metavar="PATH")
    over.add_argument(
        "--on-missing-review-task",
        choices=[ON_MISSING_SPAWN, ON_MISSING_CREATE, ON_MISSING_SKIP],
    )
    over.add_argument("--on-missing-hub", choices=[HUB_SKIP, HUB_ADD])

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
        "poll_interval": args.poll_interval,
        "round_cap": args.round_cap,
        "hub_template": args.hub_template,
        "agent_profile": args.agent_profile,
        "reviewer_login": args.reviewer_login,
        "state_path": args.state_path,
        "on_missing_review_task": args.on_missing_review_task,
        "on_missing_hub": args.on_missing_hub,
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

    try:
        config = resolve_config(args)
        log.info("workspace: %s", config.workspace_root)

        watcher = ReviewWatcher(config)
        for warning in watcher.preflight():
            log.warning(warning)

        if args.pr:
            owner, repo, number = parse_pr_ref(args.pr)
            decision = watcher.evaluate(owner, repo, number)
            print(f"\n{args.pr} → {decision.action.value}")
            print(f"  round:  {decision.round}")
            print(f"  reason: {decision.reason or '—'}")
        elif args.once:
            watcher.poll_once()
        else:
            watcher.run_forever()
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
