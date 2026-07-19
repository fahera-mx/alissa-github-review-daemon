"""GitHub access via `gh api`.

Note: this targets gh 2.4.0, which predates `gh search`. Every query goes
through `gh api` against the REST v3 endpoints instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .proc import CommandError, run, run_json

log = logging.getLogger(__name__)

# States that count as "I have reviewed this". PENDING is a draft review that
# was never submitted, so it does not close a round.
SUBMITTED_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}


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

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass(frozen=True)
class Review:
    author: str
    state: str
    commit_id: str
    submitted_at: str
    url: str


class RateLimited(RuntimeError):
    pass


class IdentityMismatch(RuntimeError):
    """Configured reviewer identity disagrees with the gh token."""


class GitHub:
    def __init__(self, login: str | None = None):
        self._login = login

    def token_login(self) -> str:
        """Who the gh token actually belongs to. `gh api --jq` prints scalars
        raw (unquoted), so this is deliberately not parsed as JSON."""
        return run(["gh", "api", "user", "--jq", ".login"]).strip()

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
        like round 1 and respawn forever. Fail loudly instead."""
        actual = self.token_login()
        if self._login is not None and self._login != actual:
            raise IdentityMismatch(
                f"configured reviewer_login={self._login!r} but the gh token "
                f"belongs to {actual!r}. `@me` follows the token, so round "
                f"counting would break. Fix reviewer_login (or set it to null "
                f"to auto-detect), or re-authenticate gh."
            )
        self._login = actual
        return actual

    def _api(self, *args: str, timeout: int = 60):
        try:
            return run_json(["gh", "api", *args], timeout=timeout)
        except CommandError as exc:
            blob = exc.stderr.lower()
            if "rate limit" in blob or "403" in blob:
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
            )
            for r in data
        ]

    def my_reviews(self, owner: str, repo: str, number: int) -> list[Review]:
        """My submitted reviews, oldest first -- one per completed round."""
        mine = [
            r
            for r in self.reviews(owner, repo, number)
            if r.author == self.login and r.state in SUBMITTED_STATES
        ]
        return sorted(mine, key=lambda r: r.submitted_at)

    def comment(self, owner: str, repo: str, number: int, body: str) -> None:
        run_json(
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/issues/{number}/comments",
                "-f",
                f"body={body}",
            ]
        )
