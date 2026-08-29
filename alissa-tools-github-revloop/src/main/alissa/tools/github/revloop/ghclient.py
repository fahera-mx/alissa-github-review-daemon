"""GitHub access via `gh api`.

Note: this targets gh 2.4.0, which predates `gh search`. Every query goes
through `gh api` against the REST v3 endpoints instead.

**Credential routing.** The daemon runs in a container shared with the
implementer lane, so several GitHub identities are present at once and `gh`
resolves whichever `GH_TOKEN`/`GITHUB_TOKEN` it happens to inherit. That is not
a theoretical hazard: it is how studio #298/#302 round-1 verdicts landed under
the DEV login instead of the reviewer's (issue #51). With `token_env` set every
`gh` call this client makes runs under an environment built HERE, with the
reviewer's token read from the named variable and the inherited ones stripped;
without it the client inherits, exactly as it always did.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from .proc import CommandError, run, run_json

log = logging.getLogger(__name__)

# States that count as "I have reviewed this". PENDING is a draft review that
# was never submitted, so it does not close a round.
SUBMITTED_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}

# GitHub's page size ceiling, and how many pages of issue comments we will walk.
# Both readers of the comment list need the WHOLE thread, and a truncated read
# fails silently in each: the activity-comment finder would stop seeing its own
# comment and fork a second one, and the re-entry ack scan would drop an
# operator's ack on the floor -- on exactly the long-lived, capped-out PRs where
# the thread is longest and the ack matters most. Bounded anyway so a
# pathological thread cannot stall a poll pass; 20 pages is 2000 comments, and
# the truncation is logged rather than assumed away. Paged explicitly rather
# than with `gh api --paginate`: this targets gh 2.4.0, whose --paginate
# concatenates one JSON document per page and would not parse.
PER_PAGE = 100
COMMENT_PAGE_LIMIT = 20

# The compare endpoint's own file page. It serves at most 300 files per page
# whatever `per_page` says, so the stop condition is a SHORT page rather than a
# short `per_page` -- reading 100 back and stopping would silently truncate the
# file list of any comparison with more than 100 changed files, and a truncated
# file list is precisely how a shipped change would be missed and a still-moving
# PR held. The page bound is the backstop: 10 pages is 3000 files, far past any
# PR this loop reviews, and past it the read is refused rather than trusted (see
# `compare_files`).
COMPARE_FILE_PAGE = 300
COMPARE_PAGE_LIMIT = 10

# GitHub's OWN cap on the pull-request commits endpoint: it "lists a maximum of
# 250 commits" and refers callers with more to the repository commits endpoint.
# That, not a page count of ours, is where absence stops being provable -- a
# 250-entry answer looks complete (the last page is short) while saying nothing
# about commit 251. And the direction is hostile: the endpoint returns commits
# OLDEST first, so on a longer PR the 250 returned are the oldest and a recent
# `judged` head is absent from every read. Unguarded, the "is the pinned commit
# gone?" probe would answer yes on every large PR and abandon real verdicts.
PR_COMMIT_CAP = 250

# Environment variables `gh` reads a token from. Both are cleared before an
# explicitly-routed call, so an inherited implementer credential cannot win by
# being the one variable we did not think to overwrite.
GH_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")

# Stderr substrings that identify GitHub throttling however it is dressed up.
# A secondary rate limit answers 403, so the status code alone cannot separate
# throttling from an authorization failure -- these can. See `_api`.
RATE_LIMIT_MARKERS = ("rate limit", "abuse detection", "429")

# The review events the daemon may submit.
EVENT_APPROVE = "APPROVE"
EVENT_REQUEST_CHANGES = "REQUEST_CHANGES"
# COMMENT cannot express approval, which is exactly why it exists here: it is
# the DEGRADED form of an approve the CI gate refused to post (a rollup that
# never concluded). It is never a verdict the loop chooses on its own -- see
# loop._gate_on_checks -- and `submit_review` still refuses anything else.
EVENT_COMMENT = "COMMENT"

# -- CI check rollup ------------------------------------------------------
#
# An APPROVE by the reviewer identity is the operator's cue to merge, so it has
# to mean "reviewed AND green". These read one commit's rollup so the verdict
# path can hold that promise; see loop._gate_on_checks for what each state does
# to a round.

CHECKS_GREEN = "green"
CHECKS_PENDING = "pending"
CHECKS_RED = "red"
# The rollup could not be read at all (an API failure, or more check runs than
# the page bound). Deliberately its own state rather than folded into either
# neighbour: unreadable must never approve, and must never post a red verdict
# naming checks nobody has seen fail.
CHECKS_UNKNOWN = "unknown"

# Conclusions that do NOT block an approve. `skipped` and `neutral` are the
# normal answer for a path-filtered matrix job (studio #323's api/cli/mcp/plugin
# jobs), so treating them as red would block every approve on every repo that
# filters by path. Every OTHER completed conclusion blocks -- including ones
# GitHub may add later, which is the conservative direction for a gate whose
# whole job is to not approve a head it cannot vouch for.
CHECKS_PASSING_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})

# Legacy commit-status states, mapped into the check-run conclusion vocabulary
# so both kinds of context classify through one rule. `pending` becomes the
# empty conclusion -- "still running" -- and an unrecognised state stays itself,
# which lands it on the blocking side above.
_STATUS_CONCLUSIONS = {
    "success": "success",
    "failure": "failure",
    "error": "error",
    "pending": "",
}

# How many pages either rollup listing will walk. A matrix job set can be large;
# it cannot plausibly be 500 contexts on one commit, and a bound is what keeps a
# pathological commit from stalling a poll pass. Past it the rollup is UNKNOWN
# rather than green -- a partial read cannot support an approve.
CHECK_RUN_PAGE_LIMIT = 5

# What a fallback-answered rollup says about itself, in the log line and in the
# `summary` that reaches a verdict body. The Actions API sees only contexts
# GitHub Actions produced -- a check run posted by a third-party check app is
# invisible to it, so a fallback rollup could call green a commit whose
# non-Actions check failed. Every repo this fleet reviews runs Actions-only CI,
# which is why the fallback is taken at all; the note is how an operator reading
# "approved on green" can tell that this is the narrower read.
ACTIONS_FALLBACK_NOTE = "via the Actions API — check-runs forbidden for this credential"

# The workflow-run states that mean "this run has finished". Read exactly like a
# check run's `status`: anything else is still going, whatever conclusion the
# payload carries.
_RUN_COMPLETED = "completed"

# The hidden marker the daemon stamps into every native verdict review it
# submits, carrying the round it closes. Two jobs, both load-bearing:
#
#   * it names the round, so a reviewer session's OWN review of the same round
#     and the daemon's native post are not counted as two rounds against the
#     cap (see `countable_rounds`);
#   * it makes the daemon's posts identifiable in the reviews list, which is
#     what the PR evidence for issue #51 is read from.
_VERDICT_MARKER_RE = re.compile(r"<!--\s*alissa-revloop:verdict\s+round=(\d+)\s*-->")


def verdict_marker(round_: int) -> str:
    """The hidden round marker for a native verdict review body."""
    return f"<!-- alissa-revloop:verdict round={round_} -->"


@dataclass(frozen=True)
class PullRequest:
    owner: str
    repo: str
    number: int
    title: str
    author: str
    head_sha: str
    draft: bool
    url: str
    # "open" or "closed"; merged PRs report state "closed" AND merged True.
    state: str = "open"
    merged: bool = False
    # Logins GitHub currently holds a pending review request against. Read off
    # the same PR payload as everything else here, so knowing whether this
    # daemon's own request is still dangling costs no extra call. USERS only:
    # `requested_teams` is a separate field and is deliberately not carried,
    # because nothing in the loop may ever withdraw a team's request.
    requested_reviewers: tuple[str, ...] = ()

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def is_terminal(self) -> bool:
        """Closed or merged: no round can ever be owed on this PR again."""
        return self.merged or self.state != "open"


@dataclass(frozen=True)
class IssueComment:
    """An issue comment on a PR (a PR is an issue). Distinct from Review:
    issue comments live on the issues endpoints and never create review
    records, so posting one can never disturb round counting."""

    id: int
    author: str
    body: str


@dataclass(frozen=True)
class Review:
    author: str
    state: str
    commit_id: str
    submitted_at: str
    url: str
    body: str = ""

    @property
    def is_substantive(self) -> bool:
        """A real review round, not a side effect of an inline comment.

        Posting a standalone inline comment on a PR creates its own review
        record with an empty body, so review records outnumber rounds. The
        round-closing review always carries the verdict write-up in its body.
        """
        return bool(self.body.strip())

    @property
    def verdict_round(self) -> int | None:
        """The round this review closes, when the daemon posted it.

        None for every review the daemon did not submit -- a reviewer
        session's own review, or anything a human wrote.
        """
        match = _VERDICT_MARKER_RE.search(self.body)
        return int(match.group(1)) if match else None


@dataclass(frozen=True)
class CheckContext:
    """One check on a commit -- a check run or a legacy commit status.

    `conclusion` is normalised lowercase and EMPTY while the check is still
    running, which is the whole discriminator the rollup needs: a context with
    no conclusion yet cannot vouch for the commit, and one with a conclusion
    outside CHECKS_PASSING_CONCLUSIONS votes against it.
    """

    name: str
    conclusion: str = ""
    url: str = ""

    @property
    def running(self) -> bool:
        return not self.conclusion

    @property
    def passing(self) -> bool:
        return self.conclusion in CHECKS_PASSING_CONCLUSIONS


@dataclass(frozen=True)
class CheckRollup:
    """One commit's CI rollup, reduced to the question the verdict path asks:
    may an APPROVE claim this head?

    `failing` and `running` carry the contexts behind the answer, because a
    verdict that declines to approve has to say WHICH check it is declining on
    (with its run URL) or the operator is left to go find it.
    """

    state: str
    failing: tuple[CheckContext, ...] = ()
    running: tuple[CheckContext, ...] = ()
    total: int = 0
    # Why `state` is CHECKS_UNKNOWN -- an API error, or the page bound. EVERY
    # read failure lands here, including a 404: this deliberately does not try
    # to tell "the commit is gone" from any other failure by its error string.
    # A 404 also answers "the token cannot see this private repo", and the
    # branch that stepped aside for a presumed force-push posted an UNGATED
    # approve on a head whose rollup was never read -- fail-open, on a substring
    # match, in the one component whose contract is to fail closed. The
    # force-push case is still released: the degraded post gets the same 422
    # from GitHub and reaches loop._abandon_verdict, which proves absence from
    # the PR's commit list rather than trusting the message.
    unreadable: str = ""
    # True when the contexts came from the Actions API instead of the check-runs
    # rollup, because the credential cannot read checks. That read is NARROWER
    # than the one it stands in for (see ACTIONS_FALLBACK_NOTE), so the answer
    # carries which path produced it everywhere the rollup is reported.
    via_actions_fallback: bool = False

    @property
    def summary(self) -> str:
        """One log-line description of the rollup.

        The fallback marker rides on EVERY state, not just green: an operator
        reading "approved on green" has to be able to see which read path
        answered, and so does one reading a request_changes that named a job.
        """
        if self.state == CHECKS_RED:
            base = f"red — failing: {check_names(self.failing)}"
        elif self.state == CHECKS_PENDING:
            base = f"pending — still running: {check_names(self.running)}"
        elif self.state == CHECKS_UNKNOWN:
            base = f"unreadable — {self.unreadable or 'no reason recorded'}"
        else:
            base = f"green — {self.total} context(s), none failing or running"
        if self.via_actions_fallback:
            return f"{base} [{ACTIONS_FALLBACK_NOTE}]"
        return base


def check_names(contexts: "tuple[CheckContext, ...]") -> str:
    """The contexts' names for a log line, or "none"."""
    return ", ".join(c.name for c in contexts) or "none"


def _completed_conclusion(payload: dict) -> str:
    """A check run's / workflow job's conclusion, EMPTY unless it finished.

    The one discriminator both read paths share: a payload that is not
    `completed` carries no conclusion here whatever it says, so "queued",
    "in_progress", "waiting" and whatever GitHub adds next all read as running
    -- exactly the states a gate must not mistake for a verdict.
    """
    completed = str(payload.get("status") or "").lower() == _RUN_COMPLETED
    return str(payload.get("conclusion") or "").lower() if completed else ""


def _run_order(run_: dict) -> "tuple[int, int]":
    """How recent a workflow run is, among runs of the same workflow.

    `run_number` is the workflow's own monotonic counter and `id` breaks its
    ties. A payload missing either sorts LAST (-1), so a malformed entry can
    never displace a run that identifies itself.
    """
    def as_int(value: object) -> int:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return -1

    return (as_int(run_.get("run_number")), as_int(run_.get("id")))


def rollup_of(
    contexts: "list[CheckContext]", via_actions_fallback: bool = False
) -> CheckRollup:
    """Reduce read contexts to a rollup state.

    Precedence is failure over running, deliberately: a commit with one job
    already red and three still going is not "wait and see" -- it cannot be
    approved at all, and saying so now beats saying it half an hour later.

    NO contexts is GREEN, not pending. A repo (or a commit) with no CI is the
    pre-gate behaviour and must approve exactly as it always did; the pending
    hold is for checks that exist and have not finished.
    """
    failing = tuple(c for c in contexts if not c.running and not c.passing)
    running = tuple(c for c in contexts if c.running)
    if failing:
        state = CHECKS_RED
    elif running:
        state = CHECKS_PENDING
    else:
        state = CHECKS_GREEN
    return CheckRollup(
        state=state,
        failing=failing,
        running=running,
        total=len(contexts),
        via_actions_fallback=via_actions_fallback,
    )


def countable_rounds(reviews: list["Review"]) -> int:
    """Rounds represented by the reviewer's substantive reviews.

    The naive count -- one round per review record -- double-counts as soon as
    BOTH a reviewer session's own review and the daemon's native verdict post
    exist for the same round, which is the normal state once the daemon closes
    rounds itself (issue #51, requirement 5). The daemon's posts carry the
    round in a hidden marker, so when any is present the highest marked round
    IS the round count: the daemon posts exactly one marked review per round,
    and it posts one for every round that lacks one.

    Deliberately NOT `len(marked) + len(unmarked)`: the two disagree exactly
    when an unmarked review exists for a round the daemon also marked, which is
    the double-count this exists to prevent.

    But `max(marked)` alone is not enough either, because the daemon does not
    post every round -- it posts the ones that lack a native review. Once round
    k is daemon-closed, a LATER round whose session submitted its own review
    correctly would be invisible, and the daemon would post a redundant second
    verdict on top of it, every round, forever. So unmarked reviews submitted
    strictly AFTER the newest marked one are counted on top.

    Ordering is the only evidence available -- an unmarked review carries no
    round number -- and it is sound in the normal flow: the daemon posts only
    after VERDICT_POST_GRACE_SECONDS, so an unmarked review landing after a
    marked round-k post belongs to round k+1. The residual error is a session
    that posts its round-k review MORE than a grace window late, after the
    daemon already marked round k: that overcounts by one, and the loop skips a
    round NUMBER rather than emitting a duplicate approve. That is the better
    failure -- a skipped number is visible in the activity comment, a duplicate
    APPROVE record is a merge signal.

    `reviews` must be oldest-first (what `my_reviews` returns).

    With no marked review at all this is the old count, byte for byte, so PRs
    reviewed before this shipped are unaffected.
    """
    marked = [r for r in reviews if r.verdict_round is not None]
    if not marked:
        return len(reviews)
    newest_marked = max(marked, key=lambda r: (r.submitted_at, r.verdict_round or 0))
    later = sum(
        1
        for r in reviews
        if r.verdict_round is None and r.submitted_at > newest_marked.submitted_at
    )
    return max(r.verdict_round or 0 for r in marked) + later


class RateLimited(RuntimeError):
    pass


class IdentityMismatch(RuntimeError):
    """Configured reviewer identity disagrees with the gh token."""


class ReviewerTokenUnset(RuntimeError):
    """`reviewer_token_env` names a variable that is absent or empty."""


def _is_authorization_forbidden(exc: CommandError) -> bool:
    """Is this a 403 about PERMISSION rather than about throttling?

    GitHub answers a secondary rate limit with 403 too, and the two want
    opposite responses: throttling is waited out, a missing permission never
    resolves itself. `_api(forbidden_is_rate_limit=False)` already raises
    RateLimited for anything carrying an explicit throttling marker, so today
    every CommandError reaching a caller with a 403 in it is an authorization
    fact. The markers are re-checked anyway rather than inherited from that
    contract, because what this answer decides is whether to answer the gate
    from a NARROWER source -- and doing that for a condition that clears itself
    in seconds is the wrong trade in the one component whose job is to fail
    closed.
    """
    blob = (exc.stderr or "").lower()
    if any(marker in blob for marker in RATE_LIMIT_MARKERS):
        return False
    return "403" in blob


class TruncatedListing(RuntimeError):
    """A paged listing exceeded its page bound, so absence cannot be inferred."""


class GitHub:
    def __init__(self, login: str | None = None, token_env: str | None = None):
        self._login = login
        self._token_env = token_env
        # The login this process ASSERTED at start (preflight). Distinct from
        # `_login`, which may be a configured-but-unverified value: only a
        # login that has been compared against `GET /user` lands here, and it
        # is what the posting gate holds later calls to.
        self._asserted_login: str | None = None

    # -- credential routing ------------------------------------------------

    @property
    def token_env(self) -> str | None:
        return self._token_env

    def _env(self) -> "dict[str, str] | None":
        """The environment every `gh` call of this client runs under.

        None -- inherit the process environment -- when no `reviewer_token_env`
        is configured, which is the pre-#51 behaviour and stays the default so
        an existing deployment is not broken by an upgrade.

        Otherwise the reviewer's token is read from the named variable and
        placed in BOTH variables `gh` consults, with nothing inherited into
        either. Building the mapping (rather than merging into os.environ) is
        the guarantee: there is no ordering by which a container-default
        credential can survive.
        """
        if self._token_env is None:
            return None
        token = (os.environ.get(self._token_env) or "").strip()
        if not token:
            raise ReviewerTokenUnset(
                f"reviewer_token_env={self._token_env!r} but that variable is "
                f"unset or empty. It must carry the REVIEWER identity's GitHub "
                f"token — the daemon refuses to fall back to whatever "
                f"credential the container happens to have inherited, because "
                f"that is how a review lands under the wrong login."
            )
        env = {k: v for k, v in os.environ.items() if k not in GH_TOKEN_VARS}
        for var in GH_TOKEN_VARS:
            env[var] = token
        return env

    def token_login(self) -> str:
        """Who the gh token actually belongs to. `gh api --jq` prints scalars
        raw (unquoted), so this is deliberately not parsed as JSON."""
        return run(["gh", "api", "user", "--jq", ".login"], env=self._env()).strip()

    @property
    def login(self) -> str:
        if self._login is None:
            self._login = self.token_login()
        return self._login

    def verify_identity(self) -> str:
        """`review-requested:@me` resolves server-side from the gh token, but
        round counting filters reviews by `self.login`. If a configured
        reviewer_login disagrees with the token, the daemon would search one
        account's queue and count another's reviews — every round would look
        like round 1 and respawn forever. Fail loudly instead.

        Called once per process (preflight); the login it resolves is the
        identity `assert_review_identity` holds every later post to.
        """
        actual = self.token_login()
        if self._login is not None and self._login != actual:
            raise IdentityMismatch(
                f"configured reviewer_login={self._login!r} but the gh token "
                f"belongs to {actual!r}. `@me` follows the token, so round "
                f"counting would break. Fix reviewer_login (or set it to null "
                f"to auto-detect), or re-authenticate gh."
            )
        self._login = actual
        self._asserted_login = actual
        return actual

    def assert_review_identity(self) -> str:
        """The gate in front of every review this daemon submits.

        A review is the one call whose AUTHOR is the payload: post it under the
        implementer's login and the round has no verdict of record, the
        pending review request is never consumed, and the daemon re-verifies
        the closed round forever (studio #298). So the login is re-read from
        `GET /user` at post time and compared against the identity this process
        asserted at start — a wrong-identity post is worse than a late one, and
        the failure mode being defended against is precisely a credential that
        was right at boot and is wrong now.

        Raises IdentityMismatch (never posts) on disagreement. With no
        `reviewer_login` configured the reviewer IS whoever the token resolved
        to at preflight, so the comparison still catches mid-process drift.
        """
        expected = self._asserted_login or self._login or self.verify_identity()
        actual = self.token_login()
        if actual != expected:
            raise IdentityMismatch(
                f"refusing to submit a review: the gh credential now resolves "
                f"to {actual!r}, but the configured reviewer identity is "
                f"{expected!r}. A review posted under the wrong login is not "
                f"the round's verdict of record and does not consume the "
                f"review request"
                + (
                    f" — check that {self._token_env} still carries the "
                    f"reviewer's token"
                    if self._token_env
                    else " — the daemon has no reviewer_token_env configured, "
                    "so it is using whatever credential it inherited"
                )
                + "."
            )
        return actual

    def _api(
        self,
        *args: str,
        timeout: int = 60,
        forbidden_is_rate_limit: bool = True,
        body: "dict | None" = None,
    ):
        """One `gh api` call, with GitHub's throttling mapped to RateLimited.

        `body` is for requests whose payload must have a particular JSON SHAPE.
        `gh`'s `-f` fields cannot express one portably: `-f 'k[]=v'` is encoded
        as the array `{"k": ["v"]}` only by modern `gh`, and as a string field
        NAMED `k[]` by the 2.4.0 this client targets -- silently, so the
        request goes out well-formed and simply missing the key the endpoint
        wants. Passing the body on stdin through `--input -` (which 2.4.0 does
        support) takes every `gh` version out of the encoding decision.

        `forbidden_is_rate_limit` is about one ambiguity: GitHub answers a
        secondary rate limit with 403, so a bare "403" in stderr is read as
        throttling by default, and `run_forever` backs off. That default is
        right for the READ path -- backing off is the correct response and a
        skipped poll costs nothing.

        It is wrong for a write whose failure has its own handling. A review
        POST that 403s because the reviewer identity cannot review the repo is
        an authorization failure, and collapsing it into RateLimited aborts the
        whole poll pass (every other PR skipped, backoff doubled toward 900s)
        while the round's retry-and-page path never runs at all. Callers like
        that pass False: an explicit throttling marker still raises
        RateLimited, and everything else stays a CommandError they can handle.
        """
        argv = ["gh", "api", *args]
        stdin = None
        if body is not None:
            argv += ["--input", "-"]
            stdin = json.dumps(body)
        try:
            return run_json(argv, timeout=timeout, env=self._env(), stdin=stdin)
        except CommandError as exc:
            blob = exc.stderr.lower()
            throttled = any(marker in blob for marker in RATE_LIMIT_MARKERS)
            if throttled or (forbidden_is_rate_limit and "403" in blob):
                raise RateLimited(exc.stderr.strip()[:300]) from exc
            raise

    def review_requests(self, repos: tuple[str, ...] = ()) -> list[tuple[str, str, int]]:
        """PRs with a review pending from me.

        `draft:false` enforces CR1 -- draft PRs are never reviewed. GitHub
        clears the request once a review is submitted and re-adds it when the
        implementer re-requests, so this doubles as the CR9 round edge-trigger.
        """
        query = "is:open is:pr draft:false review-requested:@me"
        for full_name in repos:
            query += f" repo:{full_name}"

        payload = self._api(
            "-X",
            "GET",
            "search/issues",
            "-f",
            f"q={query}",
            "-f",
            "per_page=100",
        )
        items = (payload or {}).get("items", [])

        out: list[tuple[str, str, int]] = []
        for item in items:
            # repository_url looks like https://api.github.com/repos/<owner>/<repo>
            parts = item.get("repository_url", "").rstrip("/").split("/")
            if len(parts) < 2:
                log.warning("could not parse repo from %s", item.get("repository_url"))
                continue
            out.append((parts[-2], parts[-1], int(item["number"])))
        return out

    def pull_request(self, owner: str, repo: str, number: int) -> PullRequest:
        data = self._api(f"repos/{owner}/{repo}/pulls/{number}")
        return PullRequest(
            owner=owner,
            repo=repo,
            number=number,
            title=data.get("title", ""),
            author=(data.get("user") or {}).get("login", ""),
            head_sha=(data.get("head") or {}).get("sha", ""),
            draft=bool(data.get("draft")),
            url=data.get("html_url", ""),
            state=data.get("state") or "open",
            merged=bool(data.get("merged")),
            requested_reviewers=tuple(
                str((u or {}).get("login") or "")
                for u in (data.get("requested_reviewers") or [])
                if (u or {}).get("login")
            ),
        )

    def reviews(self, owner: str, repo: str, number: int) -> list[Review]:
        data = (
            self._api(
                "-X",
                "GET",
                f"repos/{owner}/{repo}/pulls/{number}/reviews",
                "-f",
                "per_page=100",
            )
            or []
        )
        return [
            Review(
                author=(r.get("user") or {}).get("login", ""),
                state=r.get("state", ""),
                commit_id=r.get("commit_id") or "",
                submitted_at=r.get("submitted_at") or "",
                url=r.get("html_url", ""),
                body=r.get("body") or "",
            )
            for r in data
        ]

    def pull_request_commits(self, owner: str, repo: str, number: int) -> list[str]:
        """Every commit SHA currently in the PR, oldest first.

        Read on ONE path only: after a review POST was rejected, to decide
        whether the commit the verdict was pinned to is still part of the PR --
        i.e. whether a force-push landed under it. That answer separates "retry
        this" from "this can never succeed", so it is worth a call, but only
        there; nothing on the common path reads it.

        Raises `TruncatedListing` rather than returning a possibly-incomplete
        list, because the caller uses ABSENCE as evidence and a short answer
        cannot support that. The bound is GitHub's own 250 (PR_COMMIT_CAP), not
        a page count of ours: at 250 the last page is short and the listing
        looks complete while saying nothing about the commits past it.
        """
        out: list[str] = []
        page = 1
        while True:
            data = (
                self._api(
                    "-X",
                    "GET",
                    f"repos/{owner}/{repo}/pulls/{number}/commits",
                    "-f",
                    f"per_page={PER_PAGE}",
                    "-f",
                    f"page={page}",
                )
                or []
            )
            out.extend(str(c.get("sha") or "") for c in data)
            if len(out) >= PR_COMMIT_CAP:
                log.warning(
                    "%s/%s#%d reached GitHub's %d-commit listing cap — the "
                    "commit list is not complete, so a missing SHA cannot be "
                    "read as absent",
                    owner, repo, number, PR_COMMIT_CAP,
                )
                raise TruncatedListing(
                    f"{owner}/{repo}#{number} reached GitHub's {PR_COMMIT_CAP}-commit "
                    f"listing cap; absence cannot be proven from it"
                )
            if len(data) < PER_PAGE:
                return out
            page += 1

    # -- CI check rollup ---------------------------------------------------

    def check_rollup(self, owner: str, repo: str, sha: str) -> CheckRollup:
        """The CI rollup for ONE commit -- never for "the PR".

        Pinned to a SHA on purpose, and the SHA the caller passes is the one its
        verdict is recorded against: `gh pr checks` (and the GraphQL
        `statusCheckRollup` behind it) answers for whatever the PR's head is at
        the moment of the call, which is a different commit as soon as the
        implementer pushes mid-round. Approving commit A on commit B's rollup is
        the failure this whole path exists to prevent, so the commit endpoints
        are read instead: they answer about the commit named in the URL and
        nothing else.

        BOTH kinds of context are read. Check runs are what GitHub Actions
        produces; legacy commit statuses are what external CI still posts, and a
        repo can have either or both. The combined-status endpoint answers
        `state: pending` for a commit with NO statuses at all, so an empty list
        is dropped rather than read as "something is running" -- otherwise every
        approve on every Actions-only repo would hold until the wait bound.

        A check-runs read that answers an authorization 403 does NOT stop here.
        A fine-grained PAT cannot hold GitHub's `Checks` permission at all, so
        on such a deployment that 403 is permanent and CHECKS_UNKNOWN would mean
        no round can ever approve, on any repo, forever. The same credential's
        `Actions: Read` answers for the same commit, so the rollup is read
        through `_actions_contexts` instead and the answer is LABELLED as the
        narrower read it is. Everything else about the failure handling is
        unchanged: a 403 from the Actions API too, a TruncatedListing from
        either listing, or any other error is still CHECKS_UNKNOWN, and
        RateLimited still propagates.

        Never raises except RateLimited (which run_forever's backoff owns): an
        unreadable rollup is a CHECKS_UNKNOWN answer the caller can hold on, not
        a reason to abort a poll pass that has other PRs to decide.
        """
        contexts: list[CheckContext] = []
        fallback = False
        try:
            try:
                contexts.extend(self._check_runs(owner, repo, sha))
            except CommandError as exc:
                if not _is_authorization_forbidden(exc):
                    raise
                log.warning(
                    "%s/%s %s: check-runs is forbidden for this credential — "
                    "reading CI %s instead (%s)",
                    owner, repo, sha[:8], ACTIONS_FALLBACK_NOTE,
                    str(exc)[:200],
                )
                contexts.extend(self._actions_contexts(owner, repo, sha))
                fallback = True
            contexts.extend(self._commit_statuses(owner, repo, sha))
        except RateLimited:
            raise
        except TruncatedListing as exc:
            return CheckRollup(CHECKS_UNKNOWN, unreadable=str(exc)[:300])
        except Exception as exc:
            return CheckRollup(
                CHECKS_UNKNOWN, unreadable=f"{type(exc).__name__}: {exc}"[:300]
            )
        return rollup_of(contexts, via_actions_fallback=fallback)

    def _rollup_listing(
        self, path: str, key: str, what: str, params: "tuple[str, ...]" = ()
    ) -> list[dict]:
        """Page ONE rollup listing to completion, or refuse to answer.

        Completeness is decided by two signals, and the pair is the point --
        neither alone is sound here:

        * the payload's own `total_count`. Verified against this repo's live
          `check-runs` (6 runs, `total_count: 6`), and it is what makes the page
          bound exact rather than off by one: a listing of exactly
          CHECK_RUN_PAGE_LIMIT * PER_PAGE entries is COMPLETE and says so,
          instead of reading as truncated because its last page happened to be
          full.
        * a short page, the standard end-of-listing signal, used when the
          endpoint reports no count.

        `total_count` never ends the read EARLY except by being satisfied, and a
        page that comes back empty ends it too -- an endpoint that claims more
        than it will serve must not spin the bound. But a count that is still
        unsatisfied when the pages run out REFUSES: `TruncatedListing`, which
        `check_rollup` turns into CHECKS_UNKNOWN. That is the case the finding
        on this method was about -- an unpaged read of a 35-context commit saw
        30 successes and called a red head green -- and the direction has to be
        "cannot answer", never "nothing failing in what I got".

        `params` are extra `-f key=value` query fields for endpoints that need
        one (`actions/runs` is per-commit only via `head_sha=`), carried through
        every page so the filter cannot silently drop off the second one.

        `forbidden_is_rate_limit=False` for the same reason `submit_review`
        passes it: a 403 here is an authorization fact about the deployment (a
        credential without `checks: read`) with its own handling one layer up,
        and collapsing it into RateLimited would abort the whole poll pass and
        double run_forever's backoff instead of degrading this one verdict.
        That handling is now `check_rollup`'s Actions fallback, which is why the
        CommandError has to arrive intact rather than as RateLimited.
        """
        out: list[dict] = []
        total: int | None = None
        for page in range(1, CHECK_RUN_PAGE_LIMIT + 1):
            data = (
                self._api(
                    "-X",
                    "GET",
                    path,
                    "-f",
                    f"per_page={PER_PAGE}",
                    "-f",
                    f"page={page}",
                    *[arg for param in params for arg in ("-f", param)],
                    forbidden_is_rate_limit=False,
                )
                or {}
            )
            items = list(data.get(key) or [])
            if total is None and isinstance(data.get("total_count"), int):
                total = int(data["total_count"])
            out.extend(items)
            if total is None:
                if len(items) < PER_PAGE:
                    return out
                continue
            if len(out) >= total:
                return out
            if not items:
                break
        raise TruncatedListing(
            f"{path} reports {total if total is not None else 'more'} {what} but "
            f"served {len(out)} within {CHECK_RUN_PAGE_LIMIT} page(s); the rollup "
            f"cannot be read completely, so it cannot be called green"
        )

    def _check_runs(self, owner: str, repo: str, sha: str) -> list[CheckContext]:
        """The commit's check runs.

        A run that is not `completed` carries no conclusion here whatever its
        payload says, so "queued", "in_progress" and "waiting" all read as
        running -- the states GitHub can add to that list are exactly the ones a
        gate must not mistake for a verdict.
        """
        runs = self._rollup_listing(
            f"repos/{owner}/{repo}/commits/{sha}/check-runs", "check_runs", "check runs"
        )
        out: list[CheckContext] = []
        for run_ in runs:
            out.append(
                CheckContext(
                    name=str(run_.get("name") or "unnamed check"),
                    conclusion=_completed_conclusion(run_),
                    url=str(run_.get("html_url") or run_.get("details_url") or ""),
                )
            )
        return out

    def _actions_contexts(self, owner: str, repo: str, sha: str) -> list[CheckContext]:
        """The commit's CI as GitHub ACTIONS sees it -- the fallback read.

        Taken only when the check-runs rollup answered an authorization 403.
        GitHub does not offer the `Checks` permission on fine-grained PATs at
        all (community discussion 129512), so on a fine-grained-PAT deployment
        that read is not "sometimes unavailable", it is a permanent dead end:
        `check_rollup` degrades to CHECKS_UNKNOWN forever and no round can ever
        approve. The same credential's `Actions: Read` answers 200 for the same
        commit's CI, one layer lower down -- workflow runs, and each run's jobs.

        The jobs, not the runs, are the contexts: a job maps one-to-one onto the
        check run GitHub would have published for it (same name, same status,
        same conclusion), so `rollup_of` judges the fallback answer under
        exactly the rules it judges the real one under, including `skipped` and
        `neutral` passing for a path-filtered matrix.

        What this read CANNOT see is any check run posted by a third-party check
        app; see ACTIONS_FALLBACK_NOTE for why that is accepted here and how the
        answer says so.

        COST, stated exactly, because it is the sentence an operator would size
        rate-limit headroom from: this path costs one call for the run listing
        plus one per KEPT workflow, where the read it replaces cost one.

        Nothing caches that on the production path. `loop._dry_run_rollups` is
        the only rollup memo and only the dry-run branch reads it, so what
        bounds how often the cost is paid is the POLL INTERVAL -- both while
        `_checks_at_spawn` holds a round on `checks_spawn_wait_seconds` and
        while `_gate_on_checks` holds a finished verdict on
        `checks_wait_seconds`, each of which re-reads the rollup every pass.

        Nor is the fan-out bounded. CHECK_RUN_PAGE_LIMIT bounds the run
        LISTING; the number of kept workflows is whatever the sha carries, and
        each one costs a `jobs` call with `_api`'s 60s timeout, so a
        many-workflow sha turns one rollup read into a long synchronous stall
        inside a poll pass that has other PRs to decide. On this fleet's repos
        (one or two workflows per commit) it is two or three calls; that is a
        fact about these repos, not a bound this code enforces.

        The trade is still worth taking where it applies: the alternative there
        is a permanent CHECKS_UNKNOWN, which costs one call and can never
        approve.
        """
        runs = self._rollup_listing(
            f"repos/{owner}/{repo}/actions/runs",
            "workflow_runs",
            "workflow runs",
            params=(f"head_sha={sha}",),
        )
        out: list[CheckContext] = []
        for run_ in self._latest_run_per_workflow(runs):
            out.extend(self._run_contexts(owner, repo, run_))
        return out

    @staticmethod
    def _latest_run_per_workflow(runs: list[dict]) -> list[dict]:
        """One run per workflow AND trigger event: the most recent.

        Two different facts are being separated here, and collapsing them was a
        fail-open bug.

        SUPERSEDED ATTEMPTS collapse. A sha carries more than one run of the
        same workflow for the same event whenever it was triggered again -- a
        re-run shares its run id, a re-dispatch does not. Reading all of them
        would judge the commit on a superseded attempt: an earlier failed run
        whose re-trigger passed would hold the head red forever.

        DISTINCT EVENTS DO NOT. A workflow declaring `on: [push, pull_request]`
        produces a `push` run and a `pull_request` run for the same sha, and
        GitHub publishes a check run per job for BOTH -- so the read this
        stands in for reports both. Keying on `workflow_id` alone dropped the
        lower `run_number` of the pair unread, and when the dropped one was the
        red one the fallback answered GREEN for a commit whose real rollup is
        red. That is the fail-open direction this whole component exists to
        avoid, so the key carries `event` as well. A re-run preserves its run's
        `event`, so the superseded-attempt collapse above is unaffected.

        Recency is `run_number` then `id`, both monotonic per workflow and both
        integers -- deliberately not a timestamp string, which is the field a
        re-run rewrites. A run with no `workflow_id` is not grouped with
        anything (its position in the listing is its key), because collapsing
        unidentified runs together would silently drop contexts.
        """
        latest: dict[object, dict] = {}
        for index, run_ in enumerate(runs):
            workflow_id = run_.get("workflow_id")
            key: object = (
                ("workflow", workflow_id, str(run_.get("event") or ""))
                if workflow_id is not None
                else ("unidentified", index)
            )
            current = latest.get(key)
            if current is None or _run_order(run_) >= _run_order(current):
                latest[key] = run_
        return list(latest.values())

    def _run_contexts(self, owner: str, repo: str, run_: dict) -> list[CheckContext]:
        """One workflow run's jobs as contexts, or the run itself if it has none.

        A run exposes no jobs while it is still queued, and the empty list must
        never read as "nothing failing here". So a run with no readable jobs
        contributes ITSELF as one context, under the same completed/conclusion
        rule: not completed reads as running (the case the gate exists for), and
        a completed run with no jobs is judged on its own conclusion rather than
        vanishing from the rollup.
        """
        run_id = run_.get("id")
        run_name = str(run_.get("name") or run_.get("display_title") or "workflow run")
        run_url = str(run_.get("html_url") or "")
        jobs: list[dict] = []
        if run_id is not None:
            jobs = self._rollup_listing(
                f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs", "jobs", "jobs"
            )
        if not jobs:
            return [
                CheckContext(
                    name=run_name,
                    conclusion=_completed_conclusion(run_),
                    url=run_url,
                )
            ]
        return [
            CheckContext(
                name=str(job.get("name") or "unnamed job"),
                conclusion=_completed_conclusion(job),
                url=str(job.get("html_url") or run_url),
            )
            for job in jobs
        ]

    def _commit_statuses(self, owner: str, repo: str, sha: str) -> list[CheckContext]:
        """The commit's legacy statuses, one context each.

        Paged like the check runs, and for a sharper reason: this endpoint's
        default page size is 30, so the unpaged read this replaced could see 30
        successes on a 35-context commit and call a red head green.

        The combined `state` is deliberately ignored and only the `statuses`
        array is read: the endpoint answers `state: pending` for a commit with NO
        statuses at all (verified live: `total_count: 0`, `state: pending`), so
        trusting it would hold every approve on every check-runs-only repo.
        """
        statuses = self._rollup_listing(
            f"repos/{owner}/{repo}/commits/{sha}/status", "statuses", "statuses"
        )
        out: list[CheckContext] = []
        for status in statuses:
            state = str(status.get("state") or "").lower()
            out.append(
                CheckContext(
                    name=str(status.get("context") or "unnamed status"),
                    conclusion=_STATUS_CONCLUSIONS.get(state, state),
                    url=str(status.get("target_url") or ""),
                )
            )
        return out

    def my_reviews(self, owner: str, repo: str, number: int) -> list[Review]:
        """My substantive submitted reviews, oldest first -- one per round.

        Empty-bodied records are dropped: a standalone inline comment creates
        its own zero-body review record, so counting raw records overcounts
        rounds badly. On fahera-mx/studio.alissa.app#210 three real rounds
        produced six records (round 1 plus three inline-comment artifacts, then
        rounds 2 and 3), and round 3's reviewer was told it was "round 6 of
        cap 10".

        Do NOT dedupe by `commit_id` instead -- it looks like the natural
        grouping key but it UNDERCOUNTS. A round reviews whatever head is
        current, and consecutive rounds routinely land on the same commit when
        the implementer triages findings without pushing: on #210 rounds 2 and
        3 both carry head 805398a and would collapse into one. Body presence
        tracks "a reviewer wrote a verdict"; commit identity does not.
        """
        mine = [
            r
            for r in self.reviews(owner, repo, number)
            if r.author == self.login
            and r.state in SUBMITTED_STATES
            and r.is_substantive
        ]
        return sorted(mine, key=lambda r: r.submitted_at)

    def submit_review(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        event: str,
        body: str,
        commit_id: str | None = None,
    ) -> str:
        """Submit ONE native review, as the reviewer identity. Returns its URL.

        This is the round's verdict of record. A reviewer session's own
        write-up is a transcript artifact however good it is: only a review
        submitted by the CONFIGURED reviewer login expresses APPROVE /
        REQUEST_CHANGES on GitHub and consumes the pending review request.

        The identity is asserted first and the call refuses rather than posting
        under any other login (see assert_review_identity). `commit_id` pins
        the review to the head it judged, so a later push cannot make an old
        verdict look current.

        COMMENT is accepted, narrowly: it is what an approve DEGRADES to when
        the head's CI rollup never concluded (loop._gate_on_checks). It still
        cannot express approval -- that is why the gate reaches for it -- so a
        round closed by one converges nothing.
        """
        if event not in (EVENT_APPROVE, EVENT_REQUEST_CHANGES, EVENT_COMMENT):
            raise ValueError(
                f"event must be one of {EVENT_APPROVE}, {EVENT_REQUEST_CHANGES}, "
                f"{EVENT_COMMENT}, got {event!r}"
            )
        login = self.assert_review_identity()
        log.info(
            "submitting %s review on %s/%s#%d as %s", event, owner, repo, number, login
        )
        argv = [
            "-X",
            "POST",
            f"repos/{owner}/{repo}/pulls/{number}/reviews",
            "-f",
            f"event={event}",
            "-f",
            f"body={body}",
        ]
        if commit_id:
            argv += ["-f", f"commit_id={commit_id}"]
        # forbidden_is_rate_limit=False: a 403 here is the reviewer identity
        # being unable to review this repo, and it must reach the caller's
        # retry-and-page path instead of backing off the whole poll pass.
        data = self._api(*argv, forbidden_is_rate_limit=False) or {}
        return str(data.get("html_url") or "")

    def remove_review_request(
        self, owner: str, repo: str, number: int, login: str
    ) -> None:
        """Withdraw the pending review request held against ONE login.

        The payload names exactly one reviewer, never the whole list: GitHub's
        DELETE removes only what it is given, so a human reviewer or a second
        bot sitting in the same `requested_reviewers` array is untouched. The
        caller is the round close-out (loop._clear_own_review_request), and the
        only login it ever passes is the daemon's own.

        The body goes out through `--input` rather than `-f 'reviewers[]=…'`,
        because that field syntax means different things on different `gh`
        versions and this client targets 2.4.0, where it produces a string
        field literally named `reviewers[]` -- no `reviewers` key, a 422, and a
        feature that never worked. See `_api`.

        `forbidden_is_rate_limit=False` for the same reason `submit_review`
        passes it: a 403 here is this identity not being allowed to edit the
        PR's reviewers, which is a fact about the deployment that its caller
        logs and degrades on -- not a throttle worth backing the whole poll
        pass off for. Note the permission is `pull_requests: write`, strictly
        more than reviewing needs.
        """
        self._api(
            "-X",
            "DELETE",
            f"repos/{owner}/{repo}/pulls/{number}/requested_reviewers",
            body={"reviewers": [login]},
            forbidden_is_rate_limit=False,
        )

    def comment(self, owner: str, repo: str, number: int, body: str) -> None:
        run_json(
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/issues/{number}/comments",
                "-f",
                f"body={body}",
            ],
            env=self._env(),
        )

    def issue_comments(self, owner: str, repo: str, number: int) -> list[IssueComment]:
        """Every issue comment on the PR, oldest first -- see COMMENT_PAGE_LIMIT
        for why this pages instead of reading the first 100 and hoping."""
        out: list[IssueComment] = []
        for page in range(1, COMMENT_PAGE_LIMIT + 1):
            data = (
                self._api(
                    "-X",
                    "GET",
                    f"repos/{owner}/{repo}/issues/{number}/comments",
                    "-f",
                    f"per_page={PER_PAGE}",
                    "-f",
                    f"page={page}",
                )
                or []
            )
            out.extend(
                IssueComment(
                    id=int(c.get("id") or 0),
                    author=(c.get("user") or {}).get("login", ""),
                    body=c.get("body") or "",
                )
                for c in data
            )
            if len(data) < PER_PAGE:
                break
        else:
            log.warning(
                "%s/%s#%d has more than %d comments — only the first %d were "
                "read; an operator re-entry ack past that point cannot be seen",
                owner, repo, number, COMMENT_PAGE_LIMIT * PER_PAGE,
                COMMENT_PAGE_LIMIT * PER_PAGE,
            )
        return out

    def compare_files(
        self, owner: str, repo: str, base_sha: str, head_sha: str
    ) -> list[str]:
        """Every path that differs between two commits, in the order GitHub
        returns them.

        Read on ONE path: the product-stability guard, which asks whether the
        diff between the head a round judged N rounds ago and the head now
        contains anything outside the non-shipped globs. That makes ABSENCE the
        load-bearing answer, so the listing is paged to exhaustion and a listing
        that runs past `COMPARE_PAGE_LIMIT` raises `TruncatedListing` rather
        than returning a short list the caller would read as "nothing else
        moved".

        A RENAME contributes BOTH names. GitHub reports it as one entry whose
        `filename` is the new path and whose `previous_filename` is the old one,
        and only counting the new one loses exactly the case the guard must not
        miss: `src/thing.ts` renamed to `tests/thing.test.ts` moved shipped
        code, and reading only the destination makes it look like a test-only
        change.

        403 is NOT caught here (`forbidden_is_rate_limit=False` lets it through
        as a CommandError): whether an unreadable comparison is fatal or merely
        makes a guard inert is the caller's decision, not this client's -- and
        this credential is the same PAT that cannot read check-runs, so it is a
        live case rather than a theoretical one.
        """
        out: list[str] = []
        seen: set[str] = set()
        for page in range(1, COMPARE_PAGE_LIMIT + 1):
            payload = (
                self._api(
                    "-X",
                    "GET",
                    f"repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
                    "-f",
                    f"per_page={COMPARE_FILE_PAGE}",
                    "-f",
                    f"page={page}",
                    forbidden_is_rate_limit=False,
                )
                or {}
            )
            files = payload.get("files") or []
            for entry in files:
                for key in ("filename", "previous_filename"):
                    name = str(entry.get(key) or "")
                    if name and name not in seen:
                        seen.add(name)
                        out.append(name)
            if len(files) < COMPARE_FILE_PAGE:
                return out
        log.warning(
            "%s/%s comparison %s...%s served %d file page(s) without ending — "
            "the file list is not complete, so 'nothing shipped moved' cannot "
            "be read from it",
            owner, repo, base_sha[:8], head_sha[:8], COMPARE_PAGE_LIMIT,
        )
        raise TruncatedListing(
            f"{owner}/{repo} comparison {base_sha[:8]}...{head_sha[:8]} exceeded "
            f"{COMPARE_PAGE_LIMIT} file pages; absence cannot be proven from it"
        )

    def update_comment(self, owner: str, repo: str, comment_id: int, body: str) -> None:
        self._api(
            "-X",
            "PATCH",
            f"repos/{owner}/{repo}/issues/comments/{comment_id}",
            "-f",
            f"body={body}",
        )
