"""alissa-pr-review: the implementer-side driver for ONE review round.

The reviewer daemon (`alissa-revloop`) is autonomous but only the *reviewer*
half: a review request in, a review out. This command is the *implementer* half,
run by the dev session that just finished the work — it fires the trigger and
blocks on the verdict, so the loop closes without a second always-on daemon.

One invocation = one round:

  1. resolve the PR from the branch
  2. flip it ready-for-review (from draft)
  3. request the reviewer          ← this is the daemon's edge-trigger
  4. block until a NEW review round lands, or the timeout

It reads the verdict from the **review task envelope**, never from GitHub's
review state: reviewers work in comment mode, so the GitHub state is always
COMMENTED and cannot express approval. The round-completion signal is the
reviewer's substantive-review count going up; the verdict word then comes from
`Alissa.latest_verdict` — the exact logic the daemon uses, so the two halves
cannot disagree.

The triage -> fix -> re-enter loop and the round cap live in the caller (see the
`run-the-review-loop` how-to): on `request_changes` the dev triages, fixes, and
runs this again for round k+1.

Exit codes:  0 approve · 1 request_changes · 2 timeout/no verdict · 3 usage/setup
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time

from .alissa import Alissa, VERDICT_APPROVE, VERDICT_REQUEST_CHANGES
from .ghclient import GitHub
from .proc import CommandError, run, run_json

log = logging.getLogger(__name__)

# After a new review lands, the verdict envelope is written on the task around
# the same moment; give it a short grace window to appear before giving up.
_ENVELOPE_GRACE_SECONDS = 120


def resolve_pr(branch: str | None) -> tuple[str, str, int, str, bool]:
    """(owner, repo, number, url, is_draft) for `branch` (or the current branch).

    Runs in the repo working tree, so `gh` infers the repo; the head branch
    selects the PR. owner/repo come from the PR URL, which is the base repo.
    """
    argv = ["gh", "pr", "view"]
    if branch:
        argv.append(branch)
    argv += ["--json", "url,number,isDraft,state"]
    data = run_json(argv)
    if not data:
        raise ValueError(
            f"no pull request found for {'branch ' + branch if branch else 'the current branch'}"
        )
    url = data.get("url", "")
    match = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", url)
    if not match:
        raise ValueError(f"could not parse owner/repo from PR url {url!r}")
    if data.get("state") not in (None, "OPEN"):
        raise ValueError(f"PR {url} is {data.get('state')}, not open — nothing to review")
    return match.group(1), match.group(2), int(match.group(3)), url, bool(data.get("isDraft"))


def _review_task_ref(alissa: Alissa, owner: str, repo: str, number: int) -> str | None:
    """The CR2 review task for this PR, by title, regardless of status.

    Deliberately not `Alissa.find_review_task` (which filters to open tasks): we
    still want the ref just after the task is validated, to read its verdict.
    """
    pattern = re.compile(
        rf"^Review PR\s+{re.escape(owner)}/{re.escape(repo)}#{number}\b", re.IGNORECASE
    )
    matches = [t for t in alissa.list_tasks() if pattern.match(t.title)]
    return matches[0].ref if matches else None


def _reviewer_review_count(gh: GitHub, owner: str, repo: str, number: int, reviewer: str) -> int:
    """Substantive reviews the reviewer has submitted — one per completed round.

    Same 'substantive' rule the daemon counts by (empty-bodied inline-comment
    artifacts don't close a round), so a fresh round is a clean +1 edge.
    """
    return sum(
        1
        for r in gh.reviews(owner, repo, number)
        if r.author == reviewer and r.is_substantive
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alissa-pr-review",
        description="Implementer-side: flip a PR ready, request a reviewer, and "
        "block until the review verdict lands (one round of the adversarial loop).",
    )
    p.add_argument(
        "--reviewer",
        required=True,
        metavar="LOGIN",
        help="GitHub login to request review from (e.g. alissa-app)",
    )
    p.add_argument(
        "--branch",
        metavar="NAME",
        help="head branch of the PR (default: the current branch)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=2700,
        metavar="SECONDS",
        help="give up waiting after this long (default: 2700 = 45 min)",
    )
    p.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        metavar="SECONDS",
        help="seconds between checks (default: 30)",
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

    gh = GitHub()
    alissa = Alissa()

    try:
        # Identity guard: GitHub forbids requesting review from the PR author, so
        # the dev's gh token must not be the reviewer.
        dev = gh.token_login()
        if dev == args.reviewer:
            print(
                f"identity error: the gh token belongs to {dev!r}, the same as "
                f"--reviewer. GitHub forbids requesting review from the PR author; "
                f"run this as a different account than the reviewer.",
                file=sys.stderr,
            )
            return 3

        owner, repo, number, url, is_draft = resolve_pr(args.branch)
        slug = f"{owner}/{repo}#{number}"
        log.info("PR %s (%s), reviewing as reviewer=%s, dev=%s", slug, url, args.reviewer, dev)

        if is_draft:
            log.info("flipping %s ready-for-review", slug)
            run(["gh", "pr", "ready", str(number)])

        before = _reviewer_review_count(gh, owner, repo, number, args.reviewer)
        log.info("requesting review from %s (round %d)", args.reviewer, before + 1)
        run(["gh", "pr", "edit", str(number), "--add-reviewer", args.reviewer])

        deadline = time.time() + args.timeout
        ref: str | None = None
        round_landed_at: float | None = None

        while time.time() < deadline:
            time.sleep(args.poll_interval)

            if round_landed_at is None:
                now = _reviewer_review_count(gh, owner, repo, number, args.reviewer)
                if now > before:
                    round_landed_at = time.time()
                    log.info("%s: a new review landed — reading the verdict", slug)
                else:
                    log.debug("%s: still waiting (reviews=%d)", slug, now)
                    continue

            ref = ref or _review_task_ref(alissa, owner, repo, number)
            verdict = alissa.latest_verdict(ref) if ref else None
            if verdict == VERDICT_APPROVE:
                print(f"\n✔ APPROVE — {slug} converged.\n  PR:   {url}\n  task: {ref}")
                return 0
            if verdict == VERDICT_REQUEST_CHANGES:
                print(
                    f"\n✗ REQUEST_CHANGES — {slug}.\n  PR:   {url}\n  task: {ref}\n"
                    f"  Triage each finding on its PR thread, fix, then run this again "
                    f"for the next round."
                )
                return 1

            # Review landed but the envelope isn't readable yet — brief grace.
            if round_landed_at and time.time() - round_landed_at > _ENVELOPE_GRACE_SECONDS:
                print(
                    f"\n⚠ a review landed on {slug} but no verdict envelope was found "
                    f"on the review task {ref or '(not found)'} within "
                    f"{_ENVELOPE_GRACE_SECONDS}s.\n  PR: {url}\n  Read the PR review "
                    f"and the review task directly.",
                    file=sys.stderr,
                )
                return 2

        print(
            f"\n⏱ timed out after {args.timeout}s waiting for a review on {slug} — no "
            f"verdict yet.\n  PR: {url}\n  The reviewer daemon re-spawns a stalled "
            f"reviewer at ~90 min; re-run this, or check the worker.",
            file=sys.stderr,
        )
        return 2

    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
