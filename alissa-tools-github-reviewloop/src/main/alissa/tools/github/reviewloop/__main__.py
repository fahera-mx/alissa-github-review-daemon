"""CLI entry point: python -m reviewloop"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from .config import Config
from .ghclient import IdentityMismatch
from .loop import ReviewWatcher
from .proc import CommandError

DEFAULT_CONFIG = Path("./reviewloop.config.json")


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
    )
    p.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument(
        "--once", action="store_true", help="run a single poll pass and exit"
    )
    p.add_argument(
        "--pr",
        metavar="OWNER/REPO#N",
        help="evaluate one PR directly, bypassing the search — use this to tell "
        "'the search did not find it' apart from 'the decision was no'",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and log, but never enqueue a session or comment",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.config.exists():
        print(
            f"config not found: {args.config}\n"
            f"copy reviewloop.config.example.json and edit it.",
            file=sys.stderr,
        )
        return 2

    try:
        config = Config.load(args.config)
    except (ValueError, KeyError) as exc:
        print(f"invalid config {args.config}: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        config = replace_dry_run(config)

    watcher = ReviewWatcher(config)
    try:
        if args.pr:
            for warning in watcher.preflight():
                logging.warning(warning)
            owner, repo, number = parse_pr_ref(args.pr)
            decision = watcher.evaluate(owner, repo, number)
            print(f"\n{args.pr} → {decision.action.value}")
            print(f"  round:  {decision.round}")
            print(f"  reason: {decision.reason or '—'}")
        elif args.once:
            for warning in watcher.preflight():
                logging.warning(warning)
            watcher.poll_once()
        else:
            watcher.run_forever()
    except IdentityMismatch as exc:
        print(f"identity error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


def replace_dry_run(config: Config) -> Config:
    import dataclasses

    return dataclasses.replace(config, dry_run=True)


if __name__ == "__main__":
    raise SystemExit(main())
