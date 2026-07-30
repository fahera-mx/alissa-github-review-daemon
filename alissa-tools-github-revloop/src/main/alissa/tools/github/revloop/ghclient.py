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

# The review events the daemon may submit. COMMENT is deliberately absent: a
# comment-mode review cannot express approval, which is the whole reason the
# GitHub state was useless as a convergence signal in the first place.
EVENT_APPROVE = "APPROVE"
EVENT_REQUEST_CHANGES = "REQUEST_CHANGES"

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
        """
        if event not in (EVENT_APPROVE, EVENT_REQUEST_CHANGES):
            raise ValueError(
                f"event must be {EVENT_APPROVE} or {EVENT_REQUEST_CHANGES}, "
                f"got {event!r} — a COMMENT review cannot close a round"
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

    def update_comment(self, owner: str, repo: str, comment_id: int, body: str) -> None:
        self._api(
            "-X",
            "PATCH",
            f"repos/{owner}/{repo}/issues/comments/{comment_id}",
            "-f",
            f"body={body}",
        )
