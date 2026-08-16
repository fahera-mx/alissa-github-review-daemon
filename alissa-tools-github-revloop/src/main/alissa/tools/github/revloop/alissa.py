"""Alissa CLI access: locate the review task (CR2) and enqueue the fresh
reviewer session (orchestration P1)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .proc import CommandError, run, run_json

log = logging.getLogger(__name__)

# A review task is "open" while it can still receive a verdict.
OPEN_STATUSES = {"committed", "in_progress", "pending_validation", "todo"}

# -- narrowing the `alissa task list` call (issue #87) ------------------------
#
# `list_tasks` is the widest query this daemon issues -- no query string at all,
# the actor's entire non-terminal corpus, sponsor-union scoped -- and it was the
# single largest contributor to the Alissa deployment's #1 Database-I/O offender
# over 2026-08-12..16. Everything below is applied ONLY when the installed CLI
# advertises it (see Alissa.probe_task_list): an issue's claim about a flag is
# not evidence, and this daemon turns a non-zero `alissa` exit into a SKIPPED
# decision, so sending a flag the CLI does not have costs a review.

# Statuses a LIVE review task can hold. Deliberately OPEN_STATUSES itself and
# not a hand-written list: `is_review_task_for` already rejects every other
# status client-side, so filtering server-side on exactly this set cannot change
# which task the daemon resolves -- it only stops shipping the rows over the
# wire. Any status added to `is_open` is added here by construction.
TASK_LIST_STATUS_FLAG = "--status"
TASK_LIST_STATUS_FILTER = ",".join(sorted(OPEN_STATUSES))

# `--self` drops the SPONSOR's corpus and keeps only the calling actor's rows.
#
# NOT enabled by default, and the reason is measured rather than cautious. On
# the live fleet corpus (2026-08-16, 932 non-terminal rows) `--self` removes 36
# rows, 4% of the payload -- and 3 of the 371 `Review PR ...` tasks in it are
# among the rows it removes: they are owned by another actor, not by the agent
# actor whose sessions write the other 368. Review tasks are therefore
# PREDOMINANTLY actor-owned but not exclusively so, and a review task this call
# cannot see is a round the daemon cannot count. 4% of the wire is not worth
# that, so the flag is opt-in per deployment (`task_list_self_scope`).
TASK_LIST_SELF_FLAG = "--self"

# A lean projection of each row. The daemon keeps only taskNumber/title/status
# (see `_task_from_row`), so a digest view is pure saving with no semantics --
# which is why it is adopted whenever it exists and has no knob. It ships in the
# studio repo separately; until then the probe simply does not find it.
TASK_LIST_VIEW_FLAG = "--view"
TASK_LIST_DIGEST_VIEW = "digest"


@dataclass(frozen=True)
class TaskListFlags:
    """What the installed `alissa task list` advertises in its own help.

    All-False is both the "old CLI" answer and the "the probe could not run"
    answer, and they are deliberately the same value: each means "make the call
    the daemon has always made".
    """

    status: bool = False
    self_scope: bool = False
    digest: bool = False


def _advertises(helptext: str, flag: str) -> bool:
    """Whether `flag` appears as an OPTION in a CLI help listing.

    Anchored to the start of a help line (allowing a short alias in front, as in
    `-h, --help`) so a flag merely NAMED in some other option's prose -- "Pair
    with --include-shared" is in this very help text -- is not read as an offer
    of that flag. The trailing guard rejects a longer flag that merely starts
    with this one (`--self-only` is not `--self`).
    """
    return re.search(
        rf"(?m)^\s*(?:-\w,\s+)?{re.escape(flag)}(?![\w-])", helptext
    ) is not None


# CR6 verdict envelope outcomes.
VERDICT_APPROVE = "approve"
VERDICT_REQUEST_CHANGES = "request_changes"

# Envelope titles and bodies both read:
#   Review verdict: <org>/<repo>#<n> — request_changes (round 3, ...)
#   # Review verdict: <org>/<repo>#<n> — approve
# The separator is an em-dash in practice; en-dash and hyphen are accepted too
# so a hand-written envelope does not silently fail to parse.
# Kept to a single line on purpose: the verdict word sits on the same line as
# the "Review verdict:" lead-in, so matching across newlines could only pick up
# a later round's wording out of order.
_VERDICT_RE = re.compile(
    r"Review\s+verdict\s*:[^\n]*?[—–-]\s*(approve|request_changes)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Task:
    ref: str  # TASK-<taskNumber>
    title: str
    status: str

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


# The namespace loop.session_name() spawns reviewers into. Kept as the
# display-level answer to "is this session in the reviewer namespace?" (the
# console marks unmanaged sessions with it); the REAP path uses the stricter
# grammar below, because a prefix is not a strong enough claim of ownership to
# kill on.
REVIEW_SESSION_PREFIX = "review-"

# The reviewer-session GRAMMAR. The sweep kills sessions and the worker
# container is shared with other lanes (`develop-*`, `fix-*`, `maintain-*`,
# ...), so only a name that parses as one of the two shapes the review loop
# itself produces is ever a reap candidate; anything else is another daemon's
# (or a human's) and is never touched. Two shapes, both ours:
#
#   review-<repo>-pr<n>-r<k>-<nonce>   this daemon's spawns (loop.session_name)
#   review-pr-<n>[-r<k>]               the alissa-code-review skill's own
#                                      procedures (spawn-a-reviewer-session.md,
#                                      run-the-review-loop.md) -- hand-driven
#                                      rounds of the SAME loop. No spawn ledger
#                                      knows about those and nothing reaped
#                                      them: every session in the 2026-07-28
#                                      memory incident was of this shape.
#
# The daemon shape is matched with a non-greedy repo so a hyphenated repo name
# (`alissa-github-review-daemon`) still lands in the repo group, and it is
# anchored on the `-pr<n>-r<k>-<nonce>` tail that loop.session_name always
# emits. The skill shape carries no repo and no nonce -- that is the whole
# reason a bare `review-pr-<n>` needs the repos allowlist to be resolvable at
# all (see loop.ReviewWatcher._resolve_pr).
_DAEMON_SESSION_RE = re.compile(
    r"^review-(?P<repo>[a-z0-9-]+?)-pr(?P<number>\d+)-r(?P<round>\d+)-[0-9a-f]{4,16}$"
)
_SKILL_SESSION_RE = re.compile(
    r"^review-pr-(?P<number>\d+)(?:-r(?P<round>\d+))?$"
)


@dataclass(frozen=True)
class SessionRef:
    """What a reviewer session's NAME says about the round it is running.

    `repo` is the sanitized repo slug (`session_repo_slug`), not an
    `owner/repo`: session names never carry the owner. `repo` and `round` are
    None for the skill shape, which encodes neither.
    """

    number: int
    repo: "str | None" = None
    round: "int | None" = None


def session_repo_slug(repo: str) -> str:
    """The repo component of a session name: tmux-safe, lowercase, no dots.

    Shared by the producer (loop.session_name) and the parser below so the two
    can never drift -- a rename here that the matcher did not follow would make
    the daemon stop recognizing its own sessions.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", repo).strip("-").lower()


def parse_session_name(name: "str | None") -> "SessionRef | None":
    """Parse a reviewer session name, or None when it is not one of ours.

    None is the security-relevant answer: it is what keeps the sweep off every
    other lane's sessions in a shared container.
    """
    if not isinstance(name, str):
        return None
    for pattern in (_SKILL_SESSION_RE, _DAEMON_SESSION_RE):
        match = pattern.match(name)
        if match is None:
            continue
        parts = match.groupdict()
        round_ = parts.get("round")
        return SessionRef(
            number=int(parts["number"]),
            repo=parts.get("repo"),
            round=int(round_) if round_ else None,
        )
    return None


@dataclass(frozen=True)
class ManagedSession:
    name: str
    status: str  # the worker's view: "idle", "busy", ...
    # Epoch seconds of the session's last tmux activity. 0 when the CLI did
    # not report one -- treated as "long quiet", so a missing field can never
    # indefinitely immunize a session against the sweep.
    last_activity: float = 0.0

    @property
    def is_idle(self) -> bool:
        return self.status == "idle"

    @property
    def ref(self) -> "SessionRef | None":
        """What the name says about the round -- see parse_session_name."""
        return parse_session_name(self.name)


def _title_pattern(owner: str, repo: str, number: int) -> re.Pattern[str]:
    """CR2 title convention: `Review PR <org>/<repo>#<n> (TASK-<origin>)`."""
    return re.compile(
        rf"^Review PR\s+{re.escape(owner)}/{re.escape(repo)}#{number}\b",
        re.IGNORECASE,
    )


def is_review_task_for(owner: str, repo: str, number: int, task: "Task") -> bool:
    """Whether `task` is THE open CR2 review task for this PR.

    The one predicate, shared by the search (`find_review_task`, over a whole
    task list) and by the cache check (loop._review_task, over a single task
    read back by ref). They must agree: a cached ref that the search would not
    have returned is a mapping the daemon has to drop, and a divergence here
    would either pin a wrong task forever or re-fetch the corpus every pass
    while disagreeing with itself.
    """
    return bool(_title_pattern(owner, repo, number).match(task.title)) and task.is_open


def _task_from_row(row: object) -> "Task | None":
    """One task out of a CLI payload row, or None when it carries no usable ref.

    Shared by the list reader and the single-task reader so both agree on which
    field is the resolvable ref.
    """
    if not isinstance(row, dict):
        return None
    # `taskNumber` is the ref the API resolves; `taskSeq` is a display
    # ordinal and 404s as `TASK-<seq>`.
    number = row.get("taskNumber")
    if number is None:
        return None
    title = row.get("title")
    status = row.get("status")
    return Task(
        ref=f"TASK-{number}",
        title=title if isinstance(title, str) else "",
        status=status if isinstance(status, str) else "",
    )


@dataclass(frozen=True)
class TaskDetail:
    """One task as `alissa task get` sees it: the task itself, plus everything
    the decide path reads off its CR6 verdict evidence.

    All three travel together because they come out of ONE payload. The decide
    path needs every one of them (is this still the PR's open review task? how
    many rounds has it recorded? what did the newest round decide?), and
    `_count_verdicts` and `_newest_verdict` are deliberately written as mirrors
    over the same evidence array -- so reading them apart would mean fetching
    and re-parsing the same task two and three times per PR per poll, which is
    the exact cost this whole path exists to stop paying.

    `verdict` is None when no envelope on the task parses -- the normal round-1
    case, indistinguishable here from "no verdict of record yet", which is what
    the caller wants it to mean anyway.
    """

    task: Task
    verdicts: int
    verdict: "str | None"


class Alissa:
    def __init__(self, *, task_list_self_scope: bool = False) -> None:
        """`task_list_self_scope` opts the list call into `--self`.

        Default OFF, and that default is evidence, not caution -- see
        TASK_LIST_SELF_FLAG. It is still a knob because ownership is a property
        of a DEPLOYMENT (who creates its review tasks), not of this code, and an
        operator who knows their review tasks are all actor-owned should be able
        to say so.
        """
        self._task_list_self_scope = bool(task_list_self_scope)
        # The probe's answer, memoized for the process; None = not probed yet.
        # A probe that FAILS is deliberately not memoized (see probe_task_list).
        self._task_list_flags: "TaskListFlags | None" = None
        # Set when a narrowed call has been disproved at RUNTIME -- the CLI
        # advertised a flag whose call then failed or came back empty. From then
        # on this process makes the plain call, because a list that answers
        # wrongly is worse than a list that is large: `find_review_task` reads an
        # empty corpus as "this PR has no review task".
        self._task_list_narrowing_disabled = False

    # -- the `alissa task list` narrowing probe -----------------------------

    def probe_task_list(self) -> "TaskListFlags":
        """Which narrowing flags the INSTALLED `alissa task list` advertises.

        Read off the CLI's own `--help`, which is local, tokenless and free.
        The alternative -- send the flag and fall back when the call fails --
        cannot tell an unknown flag from an auth hiccup, and this daemon turns a
        non-zero `alissa` exit into a SKIPPED decision, so a mis-sent flag does
        not cost a slower call, it costs a REVIEW.

        Probed off the help OUTPUT rather than the exit status on purpose: this
        CLI is commander-based and answers an unknown *subcommand* by printing
        the parent help and exiting 0, so "it exited 0" reports every old CLI as
        capable. (Flags are stricter than subcommands here, but the rule is the
        same one and there is no reason to keep two.)

        A probe that ANSWERS is memoized for the process -- the CLI cannot
        change under a running daemon. A probe that FAILS is not: a transient
        `alissa` failure then degrades one pass instead of pinning the daemon to
        the widest call until someone restarts it.
        """
        if self._task_list_flags is not None:
            return self._task_list_flags
        try:
            helptext = run(["alissa", "task", "list", "--help"], timeout=20)
        except CommandError as exc:
            log.warning(
                "could not probe `alissa task list --help` (%s) — this pass "
                "lists tasks unnarrowed, as the daemon always did", exc,
            )
            return TaskListFlags()
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure probing `alissa task list --help`")
            return TaskListFlags()

        flags = TaskListFlags(
            status=_advertises(helptext, TASK_LIST_STATUS_FLAG),
            self_scope=_advertises(helptext, TASK_LIST_SELF_FLAG),
            digest=_advertises(helptext, TASK_LIST_VIEW_FLAG),
        )
        self._task_list_flags = flags
        return flags

    def task_list_argv(self, *, narrow_status: bool = True) -> list[str]:
        """The narrowest `alissa task list` this CLI actually supports.

        Every addition is probe-gated, so an older CLI -- today's, which offers
        none of them -- produces exactly the call the daemon has always made.
        """
        argv = ["alissa", "task", "list", "--json"]
        if self._task_list_narrowing_disabled:
            return argv
        flags = self.probe_task_list()
        if flags.status and narrow_status:
            argv += [TASK_LIST_STATUS_FLAG, TASK_LIST_STATUS_FILTER]
        if flags.self_scope and self._task_list_self_scope:
            argv.append(TASK_LIST_SELF_FLAG)
        if flags.digest:
            argv += [TASK_LIST_VIEW_FLAG, TASK_LIST_DIGEST_VIEW]
        return argv

    def list_tasks(self, *, narrow_status: bool = True) -> list[Task]:
        """This actor's live task corpus -- the expensive call.

        `alissa task list` (CLI 0.1.0) exposed no server-side narrowing at all:
        its only flags were `--json` and `--include-terminal`, and omitting the
        latter -- already the default -- was the whole of the available
        filtering. Newer CLIs offer more, so the call is now assembled from a
        boot-time probe of the installed CLI's help (`task_list_argv`): a status
        filter covering exactly the statuses a live review task can hold, a lean
        `--view digest`, and `--self` when the deployment says its review tasks
        are actor-owned. None of it is required; an absent flag is simply not
        sent.

        Narrowing is still the SECOND line of defence, not the first. Even a
        perfectly narrowed call is the actor's whole review-task corpus, so the
        daemon's job remains to call this RARELY: see loop._review_task (the
        persisted PR -> task mapping), loop._pass_task_list (at most one fetch
        per poll pass) and the negative cache behind them (state's
        `review_task_misses`, which bounds the ONE case where none of those
        help -- a PR that has no review task at all).

        `narrow_status=False` is for callers that must see review tasks the
        daemon's own `is_open` predicate would reject (prreview reads a task's
        verdict envelope after the round is over). It suppresses only the status
        filter; every other narrowing still applies.

        A narrowed call that FAILS, or that answers with an empty corpus, is
        retried once unnarrowed and turns the narrowing off for the rest of the
        process. Both are how a CLI that advertises a flag its API does not
        serve would present, and either would otherwise read as "this actor has
        no review tasks" -- which is a skipped review, not a slower one.
        """
        argv = self.task_list_argv(narrow_status=narrow_status)
        plain = ["alissa", "task", "list", "--json"]
        try:
            data = run_json(argv, timeout=90) or []
        except CommandError:
            if argv == plain:
                raise
            log.warning(
                "`%s` failed — retrying the plain task list and dropping the "
                "narrowing for this process", " ".join(argv),
            )
            self._task_list_narrowing_disabled = True
            data = run_json(plain, timeout=90) or []

        tasks = self._tasks_from(data)
        if tasks or argv == plain:
            return tasks

        # An empty answer from a narrowed call. A genuinely empty corpus is
        # possible and costs one extra list; a filter the API does not serve
        # would cost every review this actor owns.
        log.warning(
            "`%s` returned no tasks — retrying the plain task list to tell an "
            "empty corpus from a filter this API does not serve", " ".join(argv),
        )
        tasks = self._tasks_from(run_json(plain, timeout=90) or [])
        if tasks:
            self._task_list_narrowing_disabled = True
            log.warning(
                "the plain task list returned %d task(s) — the narrowed call is "
                "dropping rows, so this process stops narrowing", len(tasks),
            )
        return tasks

    @staticmethod
    def _tasks_from(data: object) -> list[Task]:
        tasks = []
        for row in data if isinstance(data, list) else []:
            task = _task_from_row(row)
            if task is not None:
                tasks.append(task)
        return tasks

    def get_task(self, ref: str) -> "TaskDetail | None":
        """Read ONE task by ref: title, status and verdict count in one call.

        This is what makes a cached PR -> review-task mapping usable. Resolving
        the task by ref costs a single-task fetch; resolving it by searching
        titles costs the actor's entire corpus, which is the read this whole
        path exists to stop paying every poll.

        None means "could not be read" and NOTHING more -- a deleted task and a
        transient CLI failure are indistinguishable from here, so a caller must
        not treat None as proof that a cached mapping is wrong (see
        loop._review_task, which keeps the row and falls back to the search).
        Never raises: the daemon polls forever and this runs inside every pass.
        """
        try:
            data = run_json(["alissa", "task", "get", ref, "--json"], timeout=90)
        except CommandError as exc:
            log.warning("could not read task %s: %s", ref, exc)
            return None
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure reading task %s", ref)
            return None

        try:
            task = _task_from_row(data)
            if task is None:
                return None
            return TaskDetail(
                task=task,
                verdicts=self._count_verdicts(data),
                verdict=self._newest_verdict(data),
            )
        except Exception:  # pragma: no cover - defence in depth
            log.exception("could not parse task payload for %s", ref)
            return None

    def find_review_task(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        tasks: "list[Task] | None" = None,
    ) -> Task | None:
        """CR2: exactly one review task per PR. Reuse it across rounds (CR7).

        `tasks` supplies a corpus the caller already fetched, so several PRs
        missing the cache in the SAME poll pass share one list call instead of
        issuing an identical one each (the observed 2-4 same-second bursts).
        Omitted -- the console-script path, and any caller with no pass to
        scope to -- fetches its own.
        """
        pool = self.list_tasks() if tasks is None else tasks
        matches = [t for t in pool if is_review_task_for(owner, repo, number, t)]

        if not matches:
            return None
        if len(matches) > 1:
            # Several verdicts on one task are fine; several tasks per PR are not.
            log.warning(
                "CR2 violation: %d open review tasks for %s/%s#%d (%s) -- using %s",
                len(matches),
                owner,
                repo,
                number,
                ", ".join(t.ref for t in matches),
                matches[0].ref,
            )
        return matches[0]

    def latest_verdict(self, task_ref: str) -> str | None:
        """The newest CR6 verdict envelope on a review task, or None.

        Returns VERDICT_APPROVE / VERDICT_REQUEST_CHANGES. This is the verdict
        of record: reviewers post comment-mode reviews, so the GitHub review
        state is always COMMENTED and cannot express approval at all.

        Never raises. The daemon polls forever and this runs inside every pass,
        so absent, empty or malformed evidence degrades to "no verdict" rather
        than taking the loop down.
        """
        try:
            data = run_json(["alissa", "task", "get", task_ref, "--json"], timeout=90)
        except CommandError as exc:
            log.warning("could not read verdict evidence for %s: %s", task_ref, exc)
            return None
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure reading verdict evidence for %s", task_ref)
            return None

        try:
            return self._newest_verdict(data)
        except Exception:  # pragma: no cover - defence in depth
            log.exception("could not parse verdict evidence for %s", task_ref)
            return None

    @staticmethod
    def _created_key(value: object) -> tuple[int, float]:
        """One evidence item's `createdAt`, as a sortable stamp.

        `alissa task get --json` dates evidence with epoch MILLISECONDS as an
        int; the API's other surfaces (and hand-written fixtures) use an ISO-8601
        string. Both are normalised here to one float, because the previous key
        -- `created if isinstance(created, str) else ""` -- collapsed every real
        item to the empty string, leaving `max` to keep the FIRST element of an
        all-equal set. Evidence comes back oldest-first, so on live data the
        OLDEST verdict won: a PR whose round 1 was request_changes and round 2
        approve never converged through the envelope branch (TASK-194837655).

        Normalising rather than widening the isinstance is deliberate: a task
        carrying both shapes would produce `(str, ...)` and `(int, ...)` keys
        that raise TypeError the moment sorting compared them.

        Returns `(has_stamp, seconds)`. An absent or unparseable stamp is
        `(0, 0.0)` and sorts FIRST, preserving the old rule that a dated
        envelope always beats one that lost its timestamp.
        """
        if isinstance(value, bool):  # bool is an int; never a timestamp
            return (0, 0.0)
        if isinstance(value, (int, float)):
            seconds = float(value)
            # Milliseconds, by magnitude: 1e11 seconds is the year 5138, while
            # 1e11 milliseconds is 1973 -- so anything above it is ms, and a
            # task holding both units still orders correctly.
            if abs(seconds) > 1e11:
                seconds /= 1000.0
            return (1, seconds)
        if isinstance(value, str) and value:
            try:
                return (1, datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            except ValueError:
                return (0, 0.0)
        return (0, 0.0)

    @staticmethod
    def _newest_verdict(payload: object) -> str | None:
        """Pick the newest parseable verdict out of a task's evidence array.

        Every layer is optional by design -- the payload shape is whatever the
        CLI printed, and a task with no evidence is the normal round-1 case.
        """
        if not isinstance(payload, dict):
            return None
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            return None

        found: list[tuple[tuple[int, float], int, str]] = []
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            content = item.get("markdownContent")
            for blob in (title, content):
                if not isinstance(blob, str):
                    continue
                match = _VERDICT_RE.search(blob)
                if match:
                    found.append(
                        (Alissa._created_key(item.get("createdAt")),
                         index,
                         match.group(1).lower())
                    )
                    break

        if not found:
            return None
        # Newest stamp wins; undated evidence sorts first, so a dated envelope
        # always beats one that lost its timestamp (see _created_key). The
        # append INDEX breaks ties: evidence comes back oldest-first, so two
        # envelopes sharing a stamp resolve to the later-recorded one rather
        # than to whichever `max` happened to reach first.
        return max(found, key=lambda item: (item[0], item[1]))[2]

    def count_verdicts(self, task_ref: str) -> int:
        """How many CR6 verdict envelopes are on the review task.

        One envelope per completed round (CR7, append-only), so this is the
        authoritative round count -- unlike the GitHub review count it cannot be
        thrown off by an empty-bodied review or two reviews in one cycle. Never
        raises: absent, empty, or malformed evidence degrades to 0 (round 1)
        rather than taking the loop down.
        """
        try:
            data = run_json(["alissa", "task", "get", task_ref, "--json"], timeout=90)
        except CommandError as exc:
            log.warning("could not read verdict evidence for %s: %s", task_ref, exc)
            return 0
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure reading verdict evidence for %s", task_ref)
            return 0
        try:
            return self._count_verdicts(data)
        except Exception:  # pragma: no cover - defence in depth
            log.exception("could not count verdict evidence for %s", task_ref)
            return 0

    @staticmethod
    def _count_verdicts(payload: object) -> int:
        """Count evidence items carrying a verdict envelope. Mirrors
        `_newest_verdict`'s tolerant parsing -- one match per item at most."""
        if not isinstance(payload, dict):
            return 0
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            return 0
        count = 0
        for item in evidence:
            if not isinstance(item, dict):
                continue
            for blob in (item.get("title"), item.get("markdownContent")):
                if isinstance(blob, str) and _VERDICT_RE.search(blob):
                    count += 1
                    break
        return count

    def enqueue_reviewer(
        self,
        *,
        session: str,
        directive: str,
        cwd: Path,
        agent: str,
        task_ref: str | None,
        dry_run: bool = False,
    ) -> None:
        argv = [
            "alissa",
            "tmux",
            "queue",
            "add",
            session,
            "--agent",
            agent,
            "--cwd",
            str(cwd),
        ]
        if task_ref:
            argv += ["--task", task_ref]
        argv.append(directive)

        if dry_run:
            log.info("[dry-run] would enqueue: %s", " ".join(argv[:-1]) + " <directive>")
            return

        run(argv, timeout=60)

        # Reviewers are one-shot per round (CR3): once the session finishes and is
        # reaped, it must never be respawned. Make that explicit so a self-kill or
        # a daemon reap can't trigger a respawn loop. Best-effort — an older CLI
        # without `queue set` should not fail the enqueue.
        try:
            run(["alissa", "tmux", "queue", "set", session, "respawn", "off"],
                timeout=30, check=False)
        except CommandError:  # pragma: no cover - defence in depth
            log.warning("could not set respawn off for %s", session)

    def list_review_sessions(self) -> list[ManagedSession]:
        """The live reviewer-grammar managed sessions, from `alissa tmux ls`.

        The reap sweep's starting point. Unlike the review-requested search,
        the live session list cannot lose a finished session, so every reap
        candidate is reachable from here. `--live` because a session that is
        already gone (self-killed, or killed by an operator) holds no worker
        slot and needs no reap. Raises CommandError upward -- the sweep skips
        this pass and tries again next poll.

        The filter is `parse_session_name`, not the bare `review-` prefix: this
        list is what the sweep kills from, the container is shared with other
        daemons, and a name that does not parse as a reviewer session is never
        enumerated here at all -- so no later bug in the sweep can reach one.
        """
        data = run_json(["alissa", "tmux", "ls", "--json", "--live"], timeout=60) or []
        sessions = []
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            if isinstance(name, str) and parse_session_name(name) is not None:
                last = row.get("lastActivity")
                sessions.append(
                    ManagedSession(
                        name=name,
                        status=str(row.get("status") or ""),
                        last_activity=float(last) if isinstance(last, (int, float)) else 0.0,
                    )
                )
        return sessions

    def kill_session(self, session: str) -> None:
        """Kill ONE finished reviewer's managed session to free its worker slot.

        Per-session, always: `alissa tmux kill <name>` and never a server-wide
        kill. The worker container is shared with every other lane, so a
        `kill-server` here would take down unrelated daemons' sessions along
        with the one that is actually finished. A test pins the absence of that
        verb across the whole package.

        Best-effort and idempotent-friendly: the session may already be gone (the
        reviewer self-killed), so a non-zero exit is not an error here. Dry-run
        is the caller's job (the sweep decides and logs before calling).
        """
        run(["alissa", "tmux", "kill", session], timeout=30, check=False)

    def add_repo_to_workspace(
        self, owner: str, repo: str, workspace_root: Path, *, dry_run: bool = False
    ) -> None:
        """Hub-ify a repo into the workspace (bare clone + main/ worktree) and
        record it in alissa-workspace.yaml. Idempotent per the CLI's contract."""
        argv = ["alissa", "code", "workspace", "add", f"{owner}/{repo}"]
        if dry_run:
            log.info("[dry-run] would run: %s (cwd=%s)", " ".join(argv), workspace_root)
            return

        log.info("hub-ifying %s/%s into %s", owner, repo, workspace_root)
        # Cloning a repo can be slow; the poll loop tolerates a long pass.
        run(argv, timeout=600, cwd=workspace_root)

    def worker_running(self) -> bool:
        """The queue only drains while `alissa worker` reconciles it."""
        try:
            out = run(["alissa", "worker", "status"], timeout=30, check=False)
        except CommandError:
            return False
        return "not running" not in out.lower() and "no worker" not in out.lower()
