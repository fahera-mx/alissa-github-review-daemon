"""Decision-logic tests for the review loop state machine.

GitHub and Alissa are faked; what is under test is when a round is owed, when
it is in flight, when the loop has converged, and when CR9 caps out.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time

import pytest

from alissa.tools.github.revloop.config import (
    CONFIG_FILENAME,
    DEFAULT_CHECKS_WAIT_SECONDS,
    DEFAULT_REAP_GRACE_SECONDS,
    DEFAULT_REAP_SESSION_CAP,
    HUB_ADD,
    ON_MISSING_SKIP,
    Config,
    resolve_config_path,
)
from alissa.tools.github.revloop.alissa import (
    VERDICT_APPROVE,
    VERDICT_REQUEST_CHANGES,
    ManagedSession,
    SessionRef,
    parse_session_name,
    session_repo_slug,
)
from alissa.tools.github.revloop.ghclient import (
    CHECK_RUN_PAGE_LIMIT,
    CHECKS_GREEN,
    CHECKS_PENDING,
    CHECKS_RED,
    CHECKS_UNKNOWN,
    COMMENT_PAGE_LIMIT,
    PER_PAGE,
    CheckContext,
    CheckRollup,
    GitHub,
    IdentityMismatch,
    IssueComment,
    PullRequest,
    RateLimited,
    Review,
    ReviewerTokenUnset,
    countable_rounds,
    rollup_of,
    verdict_marker,
)
from alissa.tools.github.revloop import ghclient as ghclient_module
from alissa.tools.github.revloop import loop as loop_module
from alissa.tools.github.revloop.loop import (
    ACTIVITY_MARKER,
    MAX_REENTRY_ROUNDS,
    MAX_VERDICT_POST_ATTEMPTS,
    REENTRY_GRAMMAR,
    STALE_ROUND_SECONDS,
    STALLED_DEFER_MULTIPLE,
    VERDICT_POST_GRACE_SECONDS,
    Action,
    ReviewWatcher,
    checks_hold_kind,
    deferral_activity_kind,
    drift_probe_kind,
    identity_drift_kind,
    parse_reentry_ack,
    session_name,
    stalled_kind,
    verdict_post_kind,
)
from alissa.tools.github.revloop.proc import CommandError
from alissa.tools.github.revloop.state import State

OWNER, REPO, NUMBER = "acme", "widgets", 7
SLUG = f"{OWNER}/{REPO}"

# What GitHub records for each review event the daemon may submit — the fake's
# half of the round-counting contract (a COMMENT review is a submitted state, so
# it closes a round and converges nothing).
_REVIEW_STATES = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}


class FakeGitHub:
    def __init__(self, pr: PullRequest, reviews: list[Review], login: str = "alissa-app"):
        self.login = login
        self._pr = pr
        self._reviews = reviews
        self.comments: list[str] = []
        # Issue comments live in their own store, exactly as on GitHub: posting
        # or PATCHing one can never touch self._reviews, which is what the
        # activity-comment pinning tests lean on.
        self.issue_store: list[IssueComment] = []
        self._next_comment_id = 1000
        self.requests = [(OWNER, REPO, NUMBER)]
        self.pr_fetches = 0
        # Native verdict posts: what was submitted, and an optional failure to
        # raise instead of submitting (the credential-broken case).
        self.submitted: list[dict] = []
        self.submit_error: BaseException | None = None
        self.identity_error: BaseException | None = None
        # The PR's commit list, as the abandon probe reads it. Defaults to the
        # head the fixtures use, so a pinned verdict is postable unless a test
        # says otherwise (a force-push).
        self.commits: list[str] = [pr.head_sha, "abc123"]
        self.commits_error: BaseException | None = None
        # Review requests withdrawn through remove_review_request, and an
        # optional failure to raise instead of withdrawing.
        self.removed: list[str] = []
        self.remove_error: BaseException | None = None
        self.reviews_error: BaseException | None = None
        # Unfiltered review-list reads — the drift check's extra call, whose
        # per-poll cost is what the probe ledger exists to bound.
        self.reviews_calls = 0
        # The CI rollup the verdict gate reads, keyed by commit sha, with a
        # default that mirrors the pre-gate world: nothing failing, nothing
        # running, so the happy path approves exactly as it always did.
        self.rollups: dict[str, CheckRollup] = {}
        self.default_rollup = CheckRollup(CHECKS_GREEN)
        self.rollup_reads: list[str] = []

    def pull_request(self, owner, repo, number):
        self.pr_fetches += 1
        return self._pr

    def reviews(self, owner, repo, number):
        self.reviews_calls += 1
        if self.reviews_error:
            raise self.reviews_error
        return list(self._reviews)

    def remove_review_request(self, owner, repo, number, login):
        """Mirrors GitHub.remove_review_request AND what GitHub does next: the
        request drops off the PR, and -- because the search IS
        review-requested:@me -- withdrawing my own drops the PR out of it."""
        if self.remove_error:
            raise self.remove_error
        self.removed.append(login)
        self._pr = dataclasses.replace(
            self._pr,
            requested_reviewers=tuple(
                r for r in self._pr.requested_reviewers if r != login
            ),
        )
        if login == self.login:
            self.requests = [r for r in self.requests if r != (owner, repo, number)]

    def my_reviews(self, owner, repo, number):
        # Mirrors GitHub.my_reviews: mine, substantive, oldest first.
        mine = [
            r for r in self._reviews if r.author == self.login and r.is_substantive
        ]
        return sorted(mine, key=lambda r: r.submitted_at)

    def pull_request_commits(self, owner, repo, number):
        if self.commits_error:
            raise self.commits_error
        return list(self.commits)

    def check_rollup(self, owner, repo, sha):
        """Mirrors GitHub.check_rollup: an answer about the SHA it is asked
        about, so a test can make the judged head red while the current head is
        green (and catch a gate that read the wrong commit)."""
        self.rollup_reads.append(sha)
        return self.rollups.get(sha, self.default_rollup)

    def assert_review_identity(self):
        if self.identity_error:
            raise self.identity_error
        return self.login

    def submit_review(self, owner, repo, number, *, event, body, commit_id=None):
        """Mirrors GitHub.submit_review: assert the identity, then land a real
        review record — so a posted verdict shows up in my_reviews exactly as
        GitHub would show it on the next poll."""
        self.assert_review_identity()
        if self.submit_error:
            raise self.submit_error
        self.submitted.append(
            {"event": event, "body": body, "commit_id": commit_id}
        )
        self._reviews.append(
            Review(
                author=self.login,
                state=_REVIEW_STATES[event],
                commit_id=commit_id or "",
                submitted_at=f"2026-07-20T0{len(self.submitted)}:00:00Z",
                url=f"https://github.com/{SLUG}/pull/{NUMBER}#pullrequestreview-{len(self.submitted)}",
                body=body,
            )
        )
        return self._reviews[-1].url

    def comment(self, owner, repo, number, body):
        self.comments.append(body)
        self.seed_comment(self.login, body)

    def seed_comment(self, author, body):
        """Plant an issue comment as any author — the spoofed-marker case."""
        self.issue_store.append(
            IssueComment(id=self._next_comment_id, author=author, body=body)
        )
        self._next_comment_id += 1

    def issue_comments(self, owner, repo, number):
        return list(self.issue_store)

    def update_comment(self, owner, repo, comment_id, body):
        for i, c in enumerate(self.issue_store):
            if c.id == comment_id:
                self.issue_store[i] = IssueComment(id=c.id, author=c.author, body=body)
                return
        raise AssertionError(f"PATCH of unknown comment id {comment_id}")

    def review_requests(self, repos=()):
        # The starved case the sweep exists for is a PR ABSENT from this
        # search; sweep tests empty it out.
        return list(self.requests)


class FakeAlissa:
    def __init__(self, task=None, verdict=None, verdict_count=0):
        self.task = task
        self.verdict = verdict  # newest CR6 envelope verdict, or None
        self.verdict_count = verdict_count  # envelopes on the task = rounds done
        self.enqueued: list[dict] = []
        self.added: list[tuple] = []
        self.killed: list[str] = []
        self.on_add = None  # optional side effect: actually create the hub
        self.sessions: list = []  # live ManagedSessions, as `alissa tmux ls` sees them

    def find_review_task(self, owner, repo, number):
        return self.task

    def latest_verdict(self, task_ref):
        return self.verdict

    def count_verdicts(self, task_ref):
        return self.verdict_count

    def enqueue_reviewer(self, **kwargs):
        self.enqueued.append(kwargs)

    def list_review_sessions(self):
        # The real client filters on the grammar, not a prefix — the fake
        # must too, or a test could 'reap' a name production never sees.
        return [s for s in self.sessions if parse_session_name(s.name)]

    def kill_session(self, session):
        self.killed.append(session)
        # A killed session drops off the live list, like real tmux.
        self.sessions = [s for s in self.sessions if s.name != session]

    def add_repo_to_workspace(self, owner, repo, workspace_root, *, dry_run=False):
        self.added.append((owner, repo, workspace_root))
        if self.on_add:
            self.on_add(owner, repo)

    def worker_running(self):
        return True


class FakeTask:
    ref = "TASK-500"
    title = "Review PR acme/widgets#7 (TASK-499)"
    status = "committed"
    is_open = True


def make_pr(
    *,
    draft=False,
    author="teammate",
    sha="abc123",
    state="open",
    merged=False,
    requested=(),
) -> PullRequest:
    return PullRequest(
        owner=OWNER,
        repo=REPO,
        number=NUMBER,
        title="Add widget cache",
        author=author,
        head_sha=sha,
        draft=draft,
        url=f"https://github.com/{SLUG}/pull/{NUMBER}",
        state=state,
        merged=merged,
        requested_reviewers=tuple(requested),
    )


def review(
    state="CHANGES_REQUESTED",
    sha="abc123",
    at="2026-07-18T10:00:00Z",
    body="## Review verdict\n\nFindings follow.",
):
    """A substantive review by default -- pass body="" for the zero-body record
    that a standalone inline comment leaves behind."""
    return Review(
        author="alissa-app",
        state=state,
        commit_id=sha,
        submitted_at=at,
        url=f"https://github.com/{SLUG}/pull/{NUMBER}#r1",
        body=body,
    )


def operator_comments(gh):
    """Escalation/stall pings only — the mechanical activity log is separate
    traffic and must not trip 'must not escalate' style assertions."""
    return [c for c in gh.comments if ACTIVITY_MARKER not in c]


def activity_comments(gh):
    return [c for c in gh.issue_store if ACTIVITY_MARKER in c.body]


@pytest.fixture
def no_post_grace(monkeypatch):
    """Collapse the pre-post wait: both the grace window and the retry backoff.

    The grace window exists to let a reviewer session submit its OWN review
    before the daemon posts one, and the backoff exists to stop a hopeless post
    retrying every poll forever. Every test using this fixture is about what
    happens once the wait has passed, so waiting it out would only be waiting.
    Both schedules are pinned by their own tests.
    """
    monkeypatch.setattr(loop_module, "VERDICT_POST_GRACE_SECONDS", 0)
    monkeypatch.setattr(loop_module, "_post_delay_after", lambda attempts: 0)


@pytest.fixture
def config(tmp_path):
    hub = tmp_path / REPO / "main"
    hub.mkdir(parents=True)
    return Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=tmp_path / "state.db",
        round_cap=3,
    )


def watcher(config, pr, reviews, task=FakeTask(), state=None, verdict=None, verdict_count=None):
    # Default the review task's envelope count to the number of substantive
    # GitHub reviews, so a scenario's rounds are consistent across both signals.
    # Tests that exercise github-vs-envelope divergence pass verdict_count.
    gh = FakeGitHub(pr, reviews)
    default_count = sum(1 for r in reviews if r.is_substantive)
    al = FakeAlissa(
        task, verdict=verdict,
        verdict_count=default_count if verdict_count is None else verdict_count,
    )
    w = ReviewWatcher(config, github=gh, alissa=al, state=state or State(config.state_db))
    return w, gh, al


# -- round 1 ---------------------------------------------------------------


def test_pending_request_with_no_prior_review_spawns_round_1(config):
    w, _, al = watcher(config, make_pr(), [])
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert d.round == 1
    assert al.enqueued[0]["session"].startswith("review-widgets-pr7-r1-")
    assert al.enqueued[0]["task_ref"] == "TASK-500"
    directive = al.enqueued[0]["directive"]
    assert "TASK-500" in directive
    assert "NEVER push commits" in directive
    assert "round" not in directive.split("procedures")[0].lower()  # round-1 template


def test_draft_pr_is_never_reviewed(config):
    w, _, al = watcher(config, make_pr(draft=True), [])
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED
    assert "draft" in d.reason.lower()
    assert al.enqueued == []


def test_self_authored_pr_is_skipped(config):
    w, _, al = watcher(config, make_pr(author="alissa-app"), [])
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED
    assert "self-review" in d.reason
    assert al.enqueued == []


# -- in-flight / idempotency ----------------------------------------------


def test_round_is_not_respawned_while_in_flight(config):
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    second = w.evaluate(OWNER, REPO, NUMBER)

    assert second.action is Action.IN_FLIGHT
    assert len(al.enqueued) == 1, "a second poll must not double-spawn the same round"


def test_stalled_round_is_respawned_after_grace_period(config):
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)

    # Backdate the spawn past the staleness threshold.
    st._db.execute(
        "UPDATE spawns SET spawned_at=? WHERE repo=? AND number=?",
        (int(time.time()) - STALE_ROUND_SECONDS - 60, SLUG, NUMBER),
    )
    st._db.commit()

    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.SPAWNED
    assert len(al.enqueued) == 2


# -- rounds k > 1 ----------------------------------------------------------


def test_second_request_after_changes_requested_spawns_round_2(config):
    w, _, al = watcher(config, make_pr(sha="def456"), [review("CHANGES_REQUESTED")])
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert d.round == 2
    assert al.enqueued[0]["session"].startswith("review-widgets-pr7-r2-")
    directive = al.enqueued[0]["directive"]
    assert "round 2 of a review loop (cap 3)" in directive
    assert "verify the triage of every prior finding" in directive


def test_comment_only_review_still_closes_a_round(config):
    """Single-operator workspaces post comment-mode reviews (CR5); they must
    still count as a completed round or the loop would never advance."""
    w, _, _ = watcher(config, make_pr(sha="def456"), [review("COMMENTED")])
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.round == 2


# -- round counting: records vs rounds -------------------------------------


def pr210_review_records():
    """The real record shape from fahera-mx/studio.alissa.app#210.

    Three rounds produced six review records: round 1, then three zero-body
    artifacts left by standalone inline comments, then rounds 2 and 3.
    """
    return [
        review("COMMENTED", sha="111aaa", at="2026-07-18T18:31:59Z", body="x" * 4399),
        review("COMMENTED", sha="111aaa", at="2026-07-18T18:32:30Z", body=""),
        review("COMMENTED", sha="111aaa", at="2026-07-18T18:32:30Z", body=""),
        review("COMMENTED", sha="111aaa", at="2026-07-18T18:32:31Z", body=""),
        review("COMMENTED", sha="805398a", at="2026-07-18T20:17:14Z", body="y" * 8030),
        review("COMMENTED", sha="805398a", at="2026-07-18T20:20:22Z", body="z" * 4826),
    ]


def test_inline_comment_artifacts_do_not_count_as_rounds(config):
    """#210: 6 review records, 3 real rounds. The daemon told round 3's
    reviewer it was on round 6."""
    import dataclasses

    cfg = dataclasses.replace(config, round_cap=10)
    w, gh, _ = watcher(cfg, make_pr(sha="805398a"), pr210_review_records())

    assert len(gh.my_reviews(OWNER, REPO, NUMBER)) == 3
    assert w.evaluate(OWNER, REPO, NUMBER).round == 4, "next round after 3, not after 6"


def test_rounds_are_not_grouped_by_commit_id(config):
    """Deduping by commit_id is the obvious-looking fix and it UNDERCOUNTS:
    #210's rounds 2 and 3 both ran on head 805398a."""
    import dataclasses

    cfg = dataclasses.replace(config, round_cap=10)
    w, _, _ = watcher(cfg, make_pr(sha="805398a"), pr210_review_records())

    # Commit grouping would see 2 distinct commits and say round 3.
    assert w.evaluate(OWNER, REPO, NUMBER).round == 4


def test_a_zero_body_record_alone_does_not_close_round_1(config):
    w, _, al = watcher(config, make_pr(), [review("COMMENTED", body="")])
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.round == 1, "an inline-comment artifact is not a completed round"
    assert al.enqueued[0]["session"].startswith("review-widgets-pr7-r1-")


def test_whitespace_only_body_is_not_substantive(config):
    w, _, _ = watcher(config, make_pr(), [review("COMMENTED", body="   \n\t ")])
    assert w.evaluate(OWNER, REPO, NUMBER).round == 1


def test_artifacts_do_not_push_the_loop_into_a_false_cap_out(config):
    """cap=3 with 1 real round plus 3 artifacts must still spawn round 2."""
    reviews = [
        review("COMMENTED", at="2026-07-18T18:31:59Z", body="the round-1 review"),
        review("COMMENTED", at="2026-07-18T18:32:30Z", body=""),
        review("COMMENTED", at="2026-07-18T18:32:31Z", body=""),
        review("COMMENTED", at="2026-07-18T18:32:32Z", body=""),
    ]
    w, gh, al = watcher(config, make_pr(), reviews)
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert d.round == 2
    assert operator_comments(gh) == [], "must not escalate on artifact count"


# -- convergence and cap-out ----------------------------------------------


def test_approved_pr_is_converged(config):
    w, _, al = watcher(
        config, make_pr(), [review("CHANGES_REQUESTED"), review("APPROVED")]
    )
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.CONVERGED
    assert al.enqueued == []


def test_approve_verdict_envelope_converges_a_comment_mode_review(config):
    """Reviewers post comment-mode reviews, so COMMENTED is the only state
    GitHub ever carries. The CR6 envelope on the task is the verdict of
    record; without it convergence is unreachable."""
    w, _, al = watcher(
        config, make_pr(), [review("COMMENTED")], verdict="approve"
    )
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.CONVERGED
    assert "TASK-500" in d.reason
    assert al.enqueued == []


def test_request_changes_envelope_does_not_converge(config):
    w, _, al = watcher(
        config, make_pr(), [review("COMMENTED")], verdict="request_changes"
    )
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert d.round == 2


def test_comment_mode_without_an_envelope_runs_to_the_cap(config):
    """The pre-fix behaviour, still correct when nobody ever approved."""
    reviews = [review("COMMENTED", at=f"2026-07-18T1{i}:00:00Z") for i in range(3)]
    w, gh, _ = watcher(config, make_pr(), reviews, verdict=None)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.ESCALATED


def test_convergence_is_skipped_when_there_is_no_review_task(config):
    """No task means nowhere for a verdict to live; behaviour is unchanged."""
    w, _, al = watcher(
        config, make_pr(), [review("COMMENTED")], task=None, verdict="approve"
    )
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert al.enqueued[0]["task_ref"] is None


def test_stale_approve_envelope_does_not_converge_after_new_commits(config):
    """#227: round 1 approved an old commit; the implementer then pushed new
    code and re-requested. The approve envelope is about the old head, so the
    loop must NOT latch converged -- round 2 is owed."""
    pr = make_pr(sha="fd500fc")                      # current head
    reviews = [review("COMMENTED", sha="fa304de")]   # round 1 reviewed the OLD head
    w, _, al = watcher(config, pr, reviews, verdict="approve")
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert d.round == 2


def test_stale_github_approve_does_not_converge_after_new_commits(config):
    """The GitHub APPROVED signal is head-bound too: an approve on an earlier
    commit doesn't converge once the head has moved."""
    pr = make_pr(sha="new")
    reviews = [review("APPROVED", sha="old")]
    w, _, al = watcher(config, pr, reviews, verdict=None)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED


def test_cap_out_escalates_and_never_spawns_round_four(config):
    reviews = [review("CHANGES_REQUESTED", at=f"2026-07-18T1{i}:00:00Z") for i in range(3)]
    w, gh, al = watcher(config, make_pr(), reviews)
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.ESCALATED
    assert al.enqueued == [], "must never queue round cap+1"
    assert len(gh.comments) == 1
    assert "cap-out" in gh.comments[0].lower()


def test_escalation_is_posted_only_once_per_head_sha(config):
    st = State(config.state_db)
    reviews = [review("CHANGES_REQUESTED", at=f"2026-07-18T1{i}:00:00Z") for i in range(3)]
    w, gh, _ = watcher(config, make_pr(), reviews, state=st)

    w.evaluate(OWNER, REPO, NUMBER)
    second = w.evaluate(OWNER, REPO, NUMBER)

    assert second.action is Action.CAPPED
    assert len(gh.comments) == 1, "cap-out must not comment on every poll"


def test_new_commits_after_cap_out_re_escalate(config):
    """A push moves head; the operator decision is about the new state."""
    st = State(config.state_db)
    reviews = [review("CHANGES_REQUESTED", at=f"2026-07-18T1{i}:00:00Z") for i in range(3)]
    w, gh, _ = watcher(config, make_pr(sha="aaa"), reviews, state=st)
    w.evaluate(OWNER, REPO, NUMBER)

    w2, gh2, _ = watcher(config, make_pr(sha="bbb"), reviews, state=st)
    assert w2.evaluate(OWNER, REPO, NUMBER).action is Action.ESCALATED


def test_custom_cap_is_respected(config):
    import dataclasses

    cfg = dataclasses.replace(config, round_cap=1)
    w, gh, al = watcher(cfg, make_pr(), [review("CHANGES_REQUESTED")])
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.ESCALATED
    assert al.enqueued == []


# -- CR2 review-task handling ---------------------------------------------


def test_missing_review_task_spawns_with_pr_url_by_default(config):
    w, _, al = watcher(config, make_pr(), [], task=None)
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert al.enqueued[0]["task_ref"] is None
    assert "https://github.com/acme/widgets/pull/7" in al.enqueued[0]["directive"]


def test_missing_review_task_skips_when_configured(config):
    import dataclasses

    cfg = dataclasses.replace(config, on_missing_review_task=ON_MISSING_SKIP)
    w, _, al = watcher(cfg, make_pr(), [], task=None)
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED
    assert al.enqueued == []


def test_missing_hub_directory_is_reported_not_spawned(config):
    import dataclasses

    cfg = dataclasses.replace(config, hub_template="{root}/nonexistent/{repo}/main")
    w, _, al = watcher(cfg, make_pr(), [])
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED
    assert "no worktree hub" in d.reason
    assert "alissa code workspace add" in d.reason
    assert al.enqueued == []


# -- hub provisioning (on_missing_hub) ------------------------------------


def hub_add_config(tmp_path, **overrides):
    """A workspace root that is a real workspace but has no hub for the repo."""
    import dataclasses

    (tmp_path / "alissa-workspace.yaml").write_text("name: test\nrepos: []\n")
    cfg = Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=tmp_path / "state.db",
        repos=(SLUG,),
        on_missing_hub=HUB_ADD,
    )
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


def test_hub_is_provisioned_then_reviewer_spawns(tmp_path):
    cfg = hub_add_config(tmp_path)
    gh = FakeGitHub(make_pr(), [])
    al = FakeAlissa(FakeTask())
    # Simulate the CLI actually creating the hub.
    al.on_add = lambda o, r: (tmp_path / r / "main").mkdir(parents=True)
    w = ReviewWatcher(cfg, github=gh, alissa=al, state=State(cfg.state_db))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert al.added == [(OWNER, REPO, tmp_path)]
    assert d.action is Action.SPAWNED
    assert al.enqueued[0]["cwd"] == tmp_path / REPO / "main"


def test_hub_add_that_does_not_produce_the_hub_is_reported(tmp_path):
    cfg = hub_add_config(tmp_path)
    w, _, al = watcher(cfg, make_pr(), [], state=State(cfg.state_db))

    d = w.evaluate(OWNER, REPO, NUMBER)  # FakeAlissa.add is a no-op

    assert d.action is Action.SKIPPED
    assert "still does not exist" in d.reason
    assert al.enqueued == []


def test_hub_add_refuses_outside_a_real_workspace(tmp_path):
    cfg = hub_add_config(tmp_path)
    cfg.manifest_path.unlink()
    w, _, al = watcher(cfg, make_pr(), [], state=State(cfg.state_db))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED
    assert "not an Alissa Code Workspace" in d.reason
    assert al.added == [], "must not clone into a non-workspace directory"


def test_hub_add_refuses_repo_outside_allowlist(tmp_path):
    import dataclasses

    cfg = dataclasses.replace(hub_add_config(tmp_path), repos=("other/repo",))
    w, _, al = watcher(cfg, make_pr(), [], state=State(cfg.state_db))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED
    assert "allowlist" in d.reason
    assert al.added == []


def test_config_rejects_auto_add_without_allowlist(tmp_path):
    with pytest.raises(ValueError, match="allowlist"):
        Config.build(tmp_path, {"on_missing_hub": "add", "repos": []})

    cfg = Config.build(tmp_path, {"on_missing_hub": "add", "repos": ["acme/widgets"]})
    assert cfg.on_missing_hub == HUB_ADD


def test_allowlist_may_be_supplied_by_cli_instead_of_config(tmp_path):
    """The allowlist guard runs after merging, so --repo satisfies it."""
    cfg = Config.build(
        tmp_path, {"on_missing_hub": "add"}, {"repos": ("acme/widgets",)}
    )
    assert cfg.repos == ("acme/widgets",)


# -- identity --------------------------------------------------------------


def test_configured_login_disagreeing_with_token_is_fatal(config):
    gh = GitHub(login="someone-else")
    gh.token_login = lambda: "alissa-app"

    with pytest.raises(IdentityMismatch, match="someone-else"):
        gh.verify_identity()


def test_identity_verification_adopts_the_token_login(config):
    gh = GitHub(login=None)
    gh.token_login = lambda: "alissa-app"

    assert gh.verify_identity() == "alissa-app"
    assert gh.login == "alissa-app"


def test_matching_configured_login_passes(config):
    gh = GitHub(login="alissa-app")
    gh.token_login = lambda: "alissa-app"

    assert gh.verify_identity() == "alissa-app"


# -- comment paging --------------------------------------------------------
#
# Both readers of the issue-comment list fail SILENTLY on a truncated read: the
# activity finder forks a second comment, the re-entry ack scan drops the ack.


def _paging_github(pages):
    """A GitHub whose `_api` serves `pages` (a list of page payloads)."""
    gh = GitHub(login="alissa-app")
    calls = []

    def api(*args, **kwargs):
        calls.append(args)
        page = int([a for a in args if a.startswith("page=")][0].split("=")[1])
        return pages[page - 1] if page <= len(pages) else []

    gh._api = api
    return gh, calls


def _comment_page(n, start=0):
    return [{"id": start + i, "user": {"login": "someone"}, "body": f"c{start + i}"}
            for i in range(n)]


def test_issue_comments_stop_at_a_short_page(config):
    gh, calls = _paging_github([_comment_page(3)])

    comments = gh.issue_comments(OWNER, REPO, NUMBER)

    assert len(comments) == 3
    assert len(calls) == 1, "a short page is the last page — no speculative fetch"


def test_issue_comments_page_past_the_first_hundred(config):
    gh, calls = _paging_github(
        [_comment_page(PER_PAGE), _comment_page(PER_PAGE, start=100), _comment_page(7, start=200)]
    )

    comments = gh.issue_comments(OWNER, REPO, NUMBER)

    assert len(comments) == 207
    assert len(calls) == 3
    assert comments[-1].body == "c206", "later pages are appended, oldest first"


def test_issue_comment_paging_is_bounded_and_says_so(config, caplog):
    """A pathological thread must not spin the poll pass — and the truncation
    is logged, not assumed away."""
    gh, calls = _paging_github([_comment_page(PER_PAGE, start=i * 100)
                                for i in range(COMMENT_PAGE_LIMIT + 5)])

    with caplog.at_level(logging.WARNING):
        comments = gh.issue_comments(OWNER, REPO, NUMBER)

    assert len(calls) == COMMENT_PAGE_LIMIT
    assert len(comments) == COMMENT_PAGE_LIMIT * PER_PAGE
    assert "only the first" in caplog.text


# -- misc ------------------------------------------------------------------


def test_dry_run_never_enqueues_or_records(config):
    import dataclasses

    cfg = dataclasses.replace(config, dry_run=True)
    st = State(cfg.state_db)
    w, _, al = watcher(cfg, make_pr(), [], state=st)

    w.evaluate(OWNER, REPO, NUMBER)
    assert st.get_spawn(SLUG, NUMBER, 1) is None
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED


def test_session_names_are_tmux_safe_round_scoped_and_unique():
    pr = make_pr()
    # Round-scoped, human-readable prefix...
    assert session_name(pr, 1).startswith("review-widgets-pr7-r1-")
    assert session_name(pr, 2).startswith("review-widgets-pr7-r2-")
    # ...but a unique nonce, so re-spawning the SAME round never collides.
    assert session_name(pr, 1) != session_name(pr, 1)

    dotted = PullRequest(
        owner="acme",
        repo="Widgets.App",
        number=7,
        title="",
        author="x",
        head_sha="a",
        draft=False,
        url="",
    )
    name = session_name(dotted, 1)
    assert name.startswith("review-widgets-app-pr7-r1-")
    # tmux-safe: only [A-Za-z0-9-]
    import re as _re
    assert _re.fullmatch(r"[A-Za-z0-9-]+", name)


def test_config_rejects_bad_values(tmp_path):
    with pytest.raises(ValueError, match="round_cap"):
        Config.build(tmp_path, {"round_cap": 0})

    with pytest.raises(ValueError, match="poll_interval"):
        Config.build(tmp_path, {"poll_interval": 2})

    with pytest.raises(ValueError, match="unknown config key"):
        Config.build(tmp_path, {"pol_interval": 60})

    with pytest.raises(ValueError, match="reap_grace_seconds"):
        Config.build(tmp_path, {"reap_grace_seconds": -1})

    # A cap of 0 would page on every live reviewer, i.e. on a healthy loop.
    with pytest.raises(ValueError, match="reap_session_cap"):
        Config.build(tmp_path, {"reap_session_cap": 0})


def test_reap_knobs_default_and_layer_like_every_other_key(tmp_path):
    assert Config.build(tmp_path).reap_grace_seconds == DEFAULT_REAP_GRACE_SECONDS
    assert Config.build(tmp_path).reap_session_cap == DEFAULT_REAP_SESSION_CAP

    cfg = Config.build(
        tmp_path,
        {"reap_grace_seconds": 900, "reap_session_cap": 3},
        {"reap_grace_seconds": 120},  # CLI wins, the unset override does not
    )
    assert (cfg.reap_grace_seconds, cfg.reap_session_cap) == (120, 3)


# -- config layering -------------------------------------------------------


def test_workspace_root_is_rejected_as_a_config_key(tmp_path):
    """It is a property of the process, not of the settings — one config file
    is meant to drive several daemons over different workspaces."""
    with pytest.raises(ValueError, match="not a config key"):
        Config.build(tmp_path, {"workspace_root": str(tmp_path)})


def test_cli_overrides_win_over_the_config_file(tmp_path):
    cfg = Config.build(
        tmp_path,
        {"poll_interval": 60, "round_cap": 3, "agent_profile": "claude"},
        {"poll_interval": 300, "round_cap": 5, "agent_profile": None},
    )
    assert cfg.poll_interval == 300
    assert cfg.round_cap == 5
    assert cfg.agent_profile == "claude", "None override must not clobber the file"


def test_cli_repos_replace_rather_than_extend(tmp_path):
    cfg = Config.build(
        tmp_path, {"repos": ["a/one", "a/two"]}, {"repos": ("b/three",)}
    )
    assert cfg.repos == ("b/three",)


def test_config_file_is_optional(tmp_path):
    cfg = Config.build(tmp_path, None, {"poll_interval": 45})
    assert cfg.poll_interval == 45
    assert cfg.round_cap == 10


def test_round_cap_default_is_ten(tmp_path):
    """CR9 default is 10 (operator decision 2026-07-23). Pin it in both the
    dataclass default and the from-raw fallback so a silent revert to 3 fails."""
    assert Config.round_cap == 10
    # No round_cap in the file or on the CLI -> the from-raw fallback applies.
    assert Config.build(tmp_path).round_cap == 10
    # An explicit override still wins.
    assert Config.build(tmp_path, {"round_cap": 7}).round_cap == 7


def test_underscore_keys_are_treated_as_comments(tmp_path):
    cfg = Config.build(tmp_path, {"_note": "json has no comments", "round_cap": 2})
    assert cfg.round_cap == 2


def test_state_path_defaults_inside_the_workspace(tmp_path):
    """Two daemons over different workspaces must not share a spawn ledger."""
    one = Config.build(tmp_path / "ws-one")
    two = Config.build(tmp_path / "ws-two")

    assert one.state_db == (tmp_path / "ws-one" / ".revloop" / "state.db")
    assert one.state_db != two.state_db


def test_explicit_state_path_still_wins(tmp_path):
    cfg = Config.build(tmp_path, {"state_path": str(tmp_path / "custom.db")})
    assert cfg.state_db == tmp_path / "custom.db"


def test_workspace_root_is_resolved_to_an_absolute_path(tmp_path):
    nested = tmp_path / "ws" / "sub" / ".."
    (tmp_path / "ws" / "sub").mkdir(parents=True)
    assert Config.build(nested).workspace_root == (tmp_path / "ws").resolve()


# -- config file discovery -------------------------------------------------


def test_explicit_config_path_wins(tmp_path):
    explicit = tmp_path / "custom.json"
    explicit.write_text("{}")
    (tmp_path / CONFIG_FILENAME).write_text("{}")

    assert resolve_config_path(explicit, tmp_path, cwd=tmp_path) == explicit


def test_missing_explicit_config_path_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="config file not found"):
        resolve_config_path(tmp_path / "nope.json", tmp_path, cwd=tmp_path)


def test_cwd_config_is_preferred_over_workspace_config(tmp_path):
    cwd, ws = tmp_path / "cwd", tmp_path / "ws"
    cwd.mkdir()
    ws.mkdir()
    (cwd / CONFIG_FILENAME).write_text("{}")
    (ws / CONFIG_FILENAME).write_text("{}")

    assert resolve_config_path(None, ws, cwd=cwd) == cwd / CONFIG_FILENAME


def test_workspace_config_is_the_fallback(tmp_path):
    cwd, ws = tmp_path / "cwd", tmp_path / "ws"
    cwd.mkdir()
    ws.mkdir()
    (ws / CONFIG_FILENAME).write_text("{}")

    assert resolve_config_path(None, ws, cwd=cwd) == ws / CONFIG_FILENAME


def test_no_config_anywhere_is_not_an_error(tmp_path):
    assert resolve_config_path(None, tmp_path, cwd=tmp_path) is None


# -- CLI wiring ------------------------------------------------------------


def cli(*argv):
    from alissa.tools.github.revloop.__main__ import build_parser

    return build_parser().parse_args(list(argv))


def test_workspace_root_defaults_to_cwd(tmp_path, monkeypatch):
    from alissa.tools.github.revloop.__main__ import resolve_config

    monkeypatch.chdir(tmp_path)
    assert resolve_config(cli()).workspace_root == tmp_path.resolve()


def test_workspace_root_flag_beats_cwd(tmp_path, monkeypatch):
    from alissa.tools.github.revloop.__main__ import resolve_config

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(tmp_path)

    cfg = resolve_config(cli("--workspace-root", str(ws)))
    assert cfg.workspace_root == ws.resolve()


def test_repeated_repo_flags_accumulate(tmp_path, monkeypatch):
    from alissa.tools.github.revloop.__main__ import resolve_config

    monkeypatch.chdir(tmp_path)
    cfg = resolve_config(cli("--repo", "a/one", "--repo", "a/two"))
    assert cfg.repos == ("a/one", "a/two")


def test_cli_fills_in_over_a_discovered_config_file(tmp_path, monkeypatch):
    import json

    from alissa.tools.github.revloop.__main__ import resolve_config

    (tmp_path / CONFIG_FILENAME).write_text(
        json.dumps({"poll_interval": 60, "round_cap": 3, "dry_run": True})
    )
    monkeypatch.chdir(tmp_path)

    cfg = resolve_config(cli("--round-cap", "5"))
    assert cfg.round_cap == 5, "CLI wins"
    assert cfg.poll_interval == 60, "config fills in"
    assert cfg.dry_run is True


def test_no_dry_run_overrides_a_dry_run_config(tmp_path, monkeypatch):
    import json

    from alissa.tools.github.revloop.__main__ import resolve_config

    (tmp_path / CONFIG_FILENAME).write_text(json.dumps({"dry_run": True}))
    monkeypatch.chdir(tmp_path)

    assert resolve_config(cli("--no-dry-run")).dry_run is False
    assert resolve_config(cli()).dry_run is True


def test_dry_run_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli("--dry-run", "--no-dry-run")


def test_workspace_root_in_config_file_is_a_clear_error(tmp_path, monkeypatch):
    import json

    from alissa.tools.github.revloop.__main__ import main

    (tmp_path / CONFIG_FILENAME).write_text(
        json.dumps({"workspace_root": str(tmp_path)})
    )
    monkeypatch.chdir(tmp_path)

    assert main([]) == 2


def test_missing_explicit_config_exits_with_config_error(tmp_path, monkeypatch):
    from alissa.tools.github.revloop.__main__ import main

    monkeypatch.chdir(tmp_path)
    assert main(["--config-path", str(tmp_path / "nope.json")]) == 2


def test_task_ref_uses_task_number_not_seq(monkeypatch):
    """`TASK-<taskSeq>` 404s server-side; the resolvable ref is taskNumber."""
    from alissa.tools.github.revloop import alissa as alissa_mod

    row = {
        "taskSeq": 998,
        "taskNumber": 617115756,
        "title": f"Review PR {OWNER}/{REPO}#{NUMBER} (TASK-1874352953)",
        "status": "pending_validation",
    }
    monkeypatch.setattr(alissa_mod, "run_json", lambda *a, **k: [row])

    task = alissa_mod.Alissa().find_review_task(OWNER, REPO, NUMBER)
    assert task is not None
    assert task.ref == "TASK-617115756"


# -- CR6 verdict envelopes -------------------------------------------------


def envelope(verdict, round_, at, extra=""):
    """A real-shaped envelope. Note the em-dash and the hyphenated org name."""
    slug = "fahera-mx/studio.alissa.app#210"
    return {
        "title": f"Review verdict: {slug} — {verdict} (round {round_}{extra})",
        "markdownContent": (
            f"# Review verdict: {slug} — {verdict}\n\nRound {round_} findings.\n"
        ),
        "createdAt": at,
    }


def verdict_from(monkeypatch, payload, ref="TASK-500"):
    from alissa.tools.github.revloop import alissa as alissa_mod

    monkeypatch.setattr(alissa_mod, "run_json", lambda *a, **k: payload)
    return alissa_mod.Alissa().latest_verdict(ref)


def test_newest_verdict_envelope_wins(monkeypatch):
    payload = {
        "evidence": [
            envelope("request_changes", 1, "2026-07-18T18:31:00Z", ", revised"),
            envelope("approve", 3, "2026-07-18T20:20:00Z"),
            envelope("request_changes", 2, "2026-07-18T20:17:00Z"),
        ]
    }
    assert verdict_from(monkeypatch, payload) == "approve"


def test_request_changes_envelope_is_parsed(monkeypatch):
    payload = {
        "evidence": [
            envelope(
                "request_changes",
                3,
                "2026-07-18T20:20:00Z",
                ", bounced on triage — CAP REACHED, escalate",
            )
        ]
    }
    assert verdict_from(monkeypatch, payload) == "request_changes"


def test_verdict_is_read_from_the_body_when_the_title_is_bare(monkeypatch):
    item = envelope("approve", 1, "2026-07-18T20:20:00Z")
    item["title"] = "Round 1 verdict"
    assert verdict_from(monkeypatch, {"evidence": [item]}) == "approve"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"evidence": None},
        {"evidence": []},
        {"evidence": "not-a-list"},
        {"evidence": [None, 42, "nope"]},
        {"evidence": [{"title": "Unrelated deliverable", "createdAt": "x"}]},
        {"evidence": [{"title": None, "markdownContent": None}]},
        {"evidence": [{"title": "Review verdict: slug — maybe"}]},
        [],
        "garbage",
    ],
    ids=[
        "null",
        "empty-dict",
        "null-evidence",
        "empty-evidence",
        "evidence-not-a-list",
        "junk-items",
        "unrelated-evidence",
        "null-fields",
        "unknown-verdict-word",
        "top-level-list",
        "top-level-string",
    ],
)
def test_malformed_or_absent_evidence_degrades_to_no_verdict(monkeypatch, payload):
    """The daemon polls forever; this must never raise."""
    assert verdict_from(monkeypatch, payload) is None


def test_undated_envelope_loses_to_a_dated_one(monkeypatch):
    undated = envelope("request_changes", 2, "2026-07-18T20:17:00Z")
    del undated["createdAt"]
    payload = {"evidence": [undated, envelope("approve", 3, "2026-07-18T20:20:00Z")]}
    assert verdict_from(monkeypatch, payload) == "approve"


def test_cli_failure_is_not_fatal(monkeypatch):
    from alissa.tools.github.revloop import alissa as alissa_mod
    from alissa.tools.github.revloop.proc import CommandError

    def boom(*a, **k):
        raise CommandError(["alissa", "task", "get"], 1, "task not found")

    monkeypatch.setattr(alissa_mod, "run_json", boom)
    assert alissa_mod.Alissa().latest_verdict("TASK-500") is None


def test_task_without_a_number_is_skipped(monkeypatch):
    from alissa.tools.github.revloop import alissa as alissa_mod

    row = {
        "taskSeq": 998,
        "title": f"Review PR {OWNER}/{REPO}#{NUMBER}",
        "status": "committed",
    }
    monkeypatch.setattr(alissa_mod, "run_json", lambda *a, **k: [row])

    assert alissa_mod.Alissa().find_review_task(OWNER, REPO, NUMBER) is None


# -- the reap sweep (search-independent backstop) ---------------------------

def _record(w, pr, round_, task_ref="TASK-500"):
    """Record a spawn and return the (now nonce'd, unique) session name it used."""
    name = session_name(pr, round_)
    w.state.record_spawn(
        repo=f"{OWNER}/{REPO}", number=NUMBER, round_=round_, head_sha="abc123",
        session=name, task_ref=task_ref,
    )
    return name


def _live(al, name, status="idle", last_activity=0.0):
    # last_activity=0 means "quiet for ages" — past any grace period.
    al.sessions.append(
        ManagedSession(name=name, status=status, last_activity=last_activity)
    )


def test_sweep_reaps_converged_pr_absent_from_the_search(config):
    """THE starved case. Submitting a review CLEARS the review request, so a
    finished round's PR vanishes from the review-requested:@me search at
    exactly the moment its session becomes reapable — a reap living inside
    the search-fed evaluate() path is unreachable then. poll_once() must
    reap it with the search returning nothing at all."""
    pr = make_pr()
    w, gh, al = watcher(config, pr, [review("APPROVED")])
    gh.requests = []  # approved → the pending request is gone
    s1 = _record(w, pr, 1)
    _live(al, s1)

    w.poll_once()

    assert al.killed == [s1]
    assert w.state.is_reaped(s1)


def test_sweep_reaps_the_terminal_approved_round(config):
    """An approved round is terminal: the loop converged, no re-request will
    ever surface this PR again, so nothing but the sweep can free the slot."""
    pr = make_pr()
    w, _, al = watcher(config, pr, [review("APPROVED")], verdict="approve")
    s1 = _record(w, pr, 1)
    _live(al, s1)

    results = w.poll_once()

    assert al.killed == [s1]
    assert [d.action for _, d in results] == [Action.CONVERGED]


def test_sweep_reaps_sessions_of_closed_or_merged_prs(config):
    # Closed mid-round: no review was ever submitted, but the PR is over.
    pr = make_pr(state="closed", merged=True)
    w, gh, al = watcher(config, pr, [])
    gh.requests = []
    s1 = _record(w, pr, 1)
    _live(al, s1)

    w.poll_once()

    assert al.killed == [s1]


def test_sweep_reaps_every_completed_round(config):
    pr = make_pr()
    # two verdict envelopes on the task → rounds 1 and 2 are done
    reviews = [review(), review(at="2026-07-18T11:00:00Z")]
    w, _, al = watcher(config, pr, reviews)
    s1 = _record(w, pr, 1)
    s2 = _record(w, pr, 2)
    _live(al, s1)
    _live(al, s2)

    w.sweep_sessions()

    assert al.killed == [s1, s2]
    assert w.state.is_reaped(s1)
    assert w.state.is_reaped(s2)


def test_sweep_spares_the_in_flight_round(config):
    pr = make_pr()
    # zero completed rounds: round 1 is in flight, not done → not reaped
    w, _, al = watcher(config, pr, [])
    s1 = _record(w, pr, 1)
    _live(al, s1)

    w.sweep_sessions()

    assert al.killed == []
    assert not w.state.is_reaped(s1)


def test_sweep_never_yanks_a_busy_session(config):
    # Round 1's review has landed, but the session is still busy (recording
    # evidence, moving its task) — spare it until the worker reports idle.
    pr = make_pr()
    w, _, al = watcher(config, pr, [review()])
    s1 = _record(w, pr, 1)
    _live(al, s1, status="busy")

    w.sweep_sessions()

    assert al.killed == []


def test_sweep_spares_sessions_it_did_not_spawn(config):
    # A review-* session with no ledger row belongs to another workspace's
    # daemon (or a human) — not ours to judge.
    w, _, al = watcher(config, make_pr(), [review()])
    _live(al, "review-widgets-pr9-r1-abc123")

    w.sweep_sessions()

    assert al.killed == []


def test_sweep_dry_run_logs_only(config):
    from dataclasses import replace
    pr = make_pr()
    w, _, al = watcher(replace(config, dry_run=True), pr, [review()])
    s1 = _record(w, pr, 1)
    _live(al, s1)

    w.sweep_sessions()

    assert al.killed == []
    assert not w.state.is_reaped(s1)
    assert [s.name for s in al.sessions] == [s1], "dry-run must leave the session live"


def test_sweep_is_idempotent_across_polls(config):
    pr = make_pr()
    w, _, al = watcher(config, pr, [review()])
    s1 = _record(w, pr, 1)
    _live(al, s1)

    w.sweep_sessions()
    w.sweep_sessions()  # s1 is gone from the live list now — no second kill

    assert al.killed == [s1]


def test_sweep_survives_a_session_list_failure(config):
    from alissa.tools.github.revloop.proc import CommandError

    w, _, al = watcher(config, make_pr(), [review()])

    def boom():
        raise CommandError(["alissa", "tmux", "ls"], 1, "no tmux server")

    al.list_review_sessions = boom
    w.sweep_sessions()  # must not raise — retried next poll

    assert al.killed == []


def test_sweep_spares_when_github_is_undecidable(config):
    from alissa.tools.github.revloop.proc import CommandError

    pr = make_pr()
    w, gh, al = watcher(config, pr, [review()])
    s1 = _record(w, pr, 1)
    _live(al, s1)

    def boom(owner, repo, number):
        raise CommandError(["gh", "api"], 1, "boom")

    gh.pull_request = boom
    w.sweep_sessions()

    assert al.killed == []
    assert not w.state.is_reaped(s1)


def test_sweep_reaps_both_sessions_of_a_twice_spawned_round(config):
    """A stalled round gets re-enqueued with a fresh session name. The ledger
    must keep BOTH spawns (keyed by session, not round) — with the old
    (repo, number, round) key the re-spawn overwrote the row and the original
    still-live session was spared forever as 'not ours'."""
    pr = make_pr()
    w, _, al = watcher(config, pr, [review()])  # round 1 is done
    s_old = _record(w, pr, 1)
    s_new = _record(w, pr, 1)  # the re-enqueue of the same round
    _live(al, s_old)
    _live(al, s_new)

    w.sweep_sessions()

    assert sorted(al.killed) == sorted([s_old, s_new])
    assert w.state.is_reaped(s_old) and w.state.is_reaped(s_new)


def test_old_round_keyed_ledger_is_migrated_on_open(tmp_path):
    """Deployed daemons carry a state.db keyed by (repo, number, round); on
    open it must be re-keyed by session with every row preserved, and a
    second spawn of the same round must then be recordable."""
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE spawns (
            repo TEXT NOT NULL, number INTEGER NOT NULL, round INTEGER NOT NULL,
            head_sha TEXT NOT NULL, session TEXT NOT NULL, task_ref TEXT,
            spawned_at INTEGER NOT NULL, PRIMARY KEY (repo, number, round)
        );
        """
    )
    conn.execute(
        "INSERT INTO spawns VALUES (?,?,?,?,?,?,?)",
        (SLUG, NUMBER, 1, "abc123", "review-widgets-pr7-r1-old123", "TASK-500", 1000),
    )
    conn.commit()
    conn.close()

    st = State(db)
    assert st.find_spawn_by_session("review-widgets-pr7-r1-old123") is not None
    st.record_spawn(
        repo=SLUG, number=NUMBER, round_=1, head_sha="abc123",
        session="review-widgets-pr7-r1-new456", task_ref="TASK-500",
    )
    assert st.find_spawn_by_session("review-widgets-pr7-r1-old123") is not None
    assert st.find_spawn_by_session("review-widgets-pr7-r1-new456") is not None
    # get_spawn ages the NEWEST attempt, matching the in-flight semantics.
    assert st.get_spawn(SLUG, NUMBER, 1)["session"] == "review-widgets-pr7-r1-new456"


def test_sweep_waits_out_recent_activity(config):
    """The GitHub review count increments before the reviewer finishes its
    close-out (CR6 envelope, task move), and a claude session between turns
    reports 'idle' — so an idle-but-recently-active session is spared until
    it has been quiet for the configured grace period."""
    pr = make_pr()
    w, _, al = watcher(config, pr, [review()])
    s1 = _record(w, pr, 1)
    _live(al, s1, last_activity=time.time())  # just did something

    w.sweep_sessions()
    assert al.killed == []

    al.sessions = []
    _live(al, s1, last_activity=time.time() - config.reap_grace_seconds * 2)
    w.sweep_sessions()
    assert al.killed == [s1]


def test_sweep_counts_rounds_via_the_ledger_task_ref(config):
    """The sweep reads the task ref off the spawn row, not find_review_task:
    the live lookup fetches the whole task list, and its open-status filter
    drops a validated review task back onto the GitHub-count fallback."""
    pr = make_pr()
    # find_review_task would say None (task validated/gone), GitHub shows no
    # reviews — but the envelope on the ledger-referenced task closed round 1.
    w, _, al = watcher(config, pr, [], task=None, verdict_count=1)
    s1 = _record(w, pr, 1, task_ref="TASK-500")
    _live(al, s1)

    w.sweep_sessions()

    assert al.killed == [s1]


def test_sweep_fetches_each_pr_once_even_across_task_refs(config):
    """The docstring promises one PR fetch per distinct PR. Two live sessions
    of one PR whose rows disagree on task_ref (a pre-task round-1 spawn next
    to a later ref-carrying one) must share the fetch, even though the round
    count is keyed on the full (repo, number, task_ref) triple."""
    pr = make_pr()
    w, gh, al = watcher(config, pr, [review()], verdict_count=1)
    s1 = _record(w, pr, 1, task_ref=None)
    s2 = _record(w, pr, 2, task_ref="TASK-500")  # round 2 in flight (2 > 1)
    _live(al, s1)
    _live(al, s2)

    w.sweep_sessions()

    assert gh.pr_fetches == 1
    assert al.killed == [s1]


def test_interrupted_migration_leaves_the_old_ledger_migratable(tmp_path):
    """The migration is one transaction: if any statement fails, the
    round-keyed table must come back untouched (still detected as stale) and
    a retry must carry every row across — never a committed empty `spawns`
    with the rows stranded in spawns_v0."""
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE spawns (
            repo TEXT NOT NULL, number INTEGER NOT NULL, round INTEGER NOT NULL,
            head_sha TEXT NOT NULL, session TEXT NOT NULL, task_ref TEXT,
            spawned_at INTEGER NOT NULL, PRIMARY KEY (repo, number, round)
        );
        CREATE TABLE spawns_v0 (blocker INTEGER);  -- makes the RENAME fail
        """
    )
    conn.execute(
        "INSERT INTO spawns VALUES (?,?,?,?,?,?,?)",
        (SLUG, NUMBER, 1, "abc123", "review-widgets-pr7-r1-old123", "TASK-500", 1000),
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.OperationalError):
        State(db)

    # The failure rolled back: the old table is intact and still stale.
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) FROM spawns").fetchone()[0] == 1
    pk = [r["name"] for r in conn.execute("PRAGMA table_info(spawns)") if r["pk"]]
    assert pk == ["repo", "number", "round"]
    conn.execute("DROP TABLE spawns_v0")  # clear the obstruction
    conn.commit()
    conn.close()

    st = State(db)  # the retry migrates for real
    assert st.find_spawn_by_session("review-widgets-pr7-r1-old123") is not None


def test_sweep_falls_back_to_review_count_without_a_task_ref(config):
    # Spawn recorded before any review task existed: the GitHub substantive-
    # review count is the only signal left.
    pr = make_pr()
    w, _, al = watcher(config, pr, [review()], task=None)
    s1 = _record(w, pr, 1, task_ref=None)
    _live(al, s1)

    w.sweep_sessions()

    assert al.killed == [s1]


# -- the session-name grammar: what the sweep is allowed to touch -----------
#
# The sweep KILLS, and the worker container is shared with every other lane,
# so ownership is a parse and not a prefix. These pin both halves: the shapes
# the review loop produces, and the shapes it must keep its hands off.


def test_grammar_round_trips_the_names_this_daemon_spawns():
    """Producer and parser are one contract — a session_name() change that the
    matcher did not follow would make the daemon stop recognizing its own."""
    ref = parse_session_name(session_name(make_pr(), 3))
    assert ref == SessionRef(number=NUMBER, repo="widgets", round=3)


def test_grammar_accepts_the_skill_spawned_shapes():
    """The alissa-code-review procedures spawn `review-pr-<n>` (and
    `-r<k>` for later rounds) by hand. Those are rounds of the SAME loop, no
    ledger knows them, and nothing reaped them: every session in the
    2026-07-28 memory incident had one of these two names."""
    assert parse_session_name("review-pr-296") == SessionRef(number=296)
    assert parse_session_name("review-pr-302-r2") == SessionRef(
        number=302, round=2
    )


@pytest.mark.parametrize(
    "name",
    [
        # Other lanes live in the same container. Real names, from the
        # incident container's `alissa tmux ls`.
        "develop-fahera-mx-alissa-github-review-daemon-i46-a1",
        "fix-fahera-mx-studio-alissa-app-pr304-r1-a1",
        "maintain-fahera-mx-studio-alissa-app-pr293-a1",
        # Near misses in our own namespace.
        "review-",
        "review-pr-",
        "review-pr-x",
        "review-pr-296-r2-extra",
        "reviewer-pr-296",
        "review-widgets-pr7",  # no round, no nonce
        "review-widgets-pr7-r1",  # no nonce
        "review-widgets-pr7-r1-NOTHEX",
        # The raw tmux name, not the managed name the CLI reports.
        "ali-review-pr-296",
        "",
        None,
    ],
)
def test_grammar_rejects_everything_that_is_not_ours(name):
    assert parse_session_name(name) is None


def test_list_review_sessions_enumerates_only_the_grammar(monkeypatch):
    """The filter lives in the enumerator on purpose: a name that is not ours
    never reaches the sweep at all, so no later bug in it can kill one."""
    from alissa.tools.github.revloop import alissa as alissa_mod

    rows = [
        {"name": "review-pr-296", "status": "idle", "lastActivity": 12},
        {"name": "review-widgets-pr7-r1-abc123", "status": "busy"},
        {"name": "develop-fahera-mx-widgets-i46-a1", "status": "idle"},
        {"name": "fix-fahera-mx-widgets-pr304-r1-a1", "status": "idle"},
        {"name": None, "status": "idle"},
        "not-even-a-dict",
    ]
    monkeypatch.setattr(alissa_mod, "run_json", lambda *a, **k: rows)

    got = alissa_mod.Alissa().list_review_sessions()

    assert [s.name for s in got] == ["review-pr-296", "review-widgets-pr7-r1-abc123"]
    assert got[0].last_activity == 12
    assert got[1].last_activity == 0.0  # missing field = "quiet for ages"


def test_the_only_tmux_kill_the_package_can_run_is_per_session():
    """`kill-server` in a shared container takes every other lane's workers
    down with the finished reviewer. Pinned by walking every literal argv in
    the package rather than by grepping the call site, so a future 'tidy up
    all these sessions' anywhere in the tree still trips it."""
    import ast
    import pathlib

    import alissa.tools.github.revloop as pkg

    argvs = []
    for path in sorted(pathlib.Path(pkg.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not isinstance(node.args[0], (ast.List, ast.Tuple)):
                continue
            # Literal head of the argv; variables (the session name) drop out.
            literals = [
                el.value
                for el in node.args[0].elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]
            if literals[:1] == ["alissa"]:
                argvs.append(literals)

    assert argvs, "found no alissa argv at all — the walk is broken, not clean"
    assert not any("kill-server" in argv for argv in argvs)
    # Every kill the package can issue -- the sweep's and the console's
    # operator action -- is the same per-session verb, with the session name
    # as its (non literal) tail argument.
    kills = [argv for argv in argvs if "kill" in argv]
    assert kills and all(argv == ["alissa", "tmux", "kill"] for argv in kills)


# -- the terminal-PR reap edge (issue #46) ----------------------------------
#
# Sessions with no ledger row: the hand-spawned `review-pr-<n>` rounds, and
# any spawn whose ledger was lost. The name is the only evidence, so these
# reap on a TERMINAL PR only.


def _watched(config, *repos):
    return dataclasses.replace(config, repos=tuple(repos))


def _resolve_prs(gh, mapping):
    """Make the fake GitHub answer per (repo slug, number), 404ing otherwise —
    the multi-repo world the allowlist probe has to work in."""

    def pull_request(owner, repo, number):
        gh.pr_fetches += 1
        found = mapping.get((f"{owner}/{repo}", number))
        if found is None:
            raise CommandError(["gh", "api"], 1, "Not Found (HTTP 404)")
        return found

    gh.pull_request = pull_request


def test_sweep_reaps_a_ledgerless_session_of_a_merged_pr(config, caplog):
    """THE incident case: a skill-spawned reviewer, idle for hours, its PR
    merged, no spawn row anywhere — nothing in the daemon used to be able to
    reach it."""
    merged = make_pr(state="closed", merged=True)
    w, _, al = watcher(_watched(config, SLUG), merged, [])
    _live(al, "review-pr-7", last_activity=time.time() - 3600)

    with caplog.at_level(logging.INFO):
        assert w.sweep_sessions() == 1

    assert al.killed == ["review-pr-7"]
    # Evidence, per #46: the name, the PR state, and how long it sat idle.
    line = next(r.getMessage() for r in caplog.records if "reaped" in r.message)
    assert "review-pr-7" in line and "merged" in line and "60 min" in line


def test_sweep_never_reaps_a_ledgerless_session_of_an_open_pr(config):
    """v1 reaps on terminal PRs only. A superseded round (r1 while r2 runs) is
    exactly what the name cannot distinguish from an in-flight one, and an
    operator re-entry may still want that context — so an open PR spares every
    round of it."""
    w, _, al = watcher(_watched(config, SLUG), make_pr(), [review()])
    _live(al, "review-pr-7-r1")
    _live(al, "review-pr-7-r2")

    assert w.sweep_sessions() == 0
    assert al.killed == []


def test_sweep_never_reaps_a_busy_session_even_on_a_merged_pr(config):
    """Scoped post-merge re-reviews of fold commits are an established
    pattern — busy plus terminal is logged, never killed."""
    merged = make_pr(state="closed", merged=True)
    w, gh, al = watcher(_watched(config, SLUG), merged, [])
    _live(al, "review-pr-7", status="busy")

    assert w.sweep_sessions() == 0
    assert al.killed == []
    assert gh.pr_fetches == 0, "a session we will never kill must cost no fetch"


def test_sweep_holds_a_terminal_pr_session_through_the_grace_period(config):
    """The grace period is what lets a just-merged PR's reviewer finish its
    in-session close-out (CR6 envelope, task move) before the slot is freed."""
    cfg = dataclasses.replace(_watched(config, SLUG), reap_grace_seconds=600)
    merged = make_pr(state="closed", merged=True)
    w, _, al = watcher(cfg, merged, [])
    _live(al, "review-pr-7", last_activity=time.time() - 300)  # inside the grace

    assert w.sweep_sessions() == 0

    al.sessions = []
    _live(al, "review-pr-7", last_activity=time.time() - 900)  # past it
    assert w.sweep_sessions() == 1
    assert al.killed == ["review-pr-7"]


def test_sweep_spares_a_bare_name_when_nothing_bounds_the_search(config):
    """An empty allowlist means 'review whatever asks' — there is then no
    repo a bare `review-pr-<n>` could be resolved against, so it is spared
    without spending a single fetch."""
    w, gh, al = watcher(config, make_pr(state="closed", merged=True), [])
    _live(al, "review-pr-7")

    assert w.sweep_sessions() == 0
    assert al.killed == []
    assert gh.pr_fetches == 0


def test_sweep_spares_a_bare_name_two_watched_repos_could_own(config):
    """PR #7 exists in both watched repos: which session this is cannot be
    known, and a guess here kills somebody's reviewer."""
    merged = make_pr(state="closed", merged=True)
    other = dataclasses.replace(merged, repo="gadgets")
    w, gh, al = watcher(_watched(config, SLUG, "acme/gadgets"), merged, [])
    _resolve_prs(gh, {(SLUG, 7): merged, ("acme/gadgets", 7): other})
    _live(al, "review-pr-7")

    assert w.sweep_sessions() == 0
    assert al.killed == []


def test_sweep_resolves_a_bare_name_to_the_one_watched_repo_that_has_it(config):
    merged = make_pr(state="closed", merged=True)
    w, gh, al = watcher(_watched(config, "acme/gadgets", SLUG), merged, [])
    _resolve_prs(gh, {(SLUG, 7): merged})  # gadgets 404s on #7
    _live(al, "review-pr-7")

    assert w.sweep_sessions() == 1
    assert al.killed == ["review-pr-7"]


def test_sweep_matches_a_repo_bearing_name_against_the_allowlist(config):
    """A daemon-shaped name whose ledger row is gone still carries its repo —
    and the slug in the name ('studio-alissa-app') has to compare equal to the
    allowlist's real name ('studio.alissa.app')."""
    merged = dataclasses.replace(
        make_pr(state="closed", merged=True), owner="fahera-mx", repo="studio.alissa.app"
    )
    w, gh, al = watcher(
        _watched(config, "fahera-mx/studio.alissa.app", "acme/gadgets"), merged, []
    )
    _resolve_prs(gh, {("fahera-mx/studio.alissa.app", 7): merged})
    _live(al, "review-studio-alissa-app-pr7-r1-abc123")

    assert w.sweep_sessions() == 1
    assert al.killed == ["review-studio-alissa-app-pr7-r1-abc123"]
    assert gh.pr_fetches == 1, "the name names its repo — no allowlist probing"


def test_sweep_never_touches_another_lanes_sessions(config):
    """The other lanes' sessions are idle, long quiet, and share the container
    with a merged PR's — and are still invisible to the sweep."""
    merged = make_pr(state="closed", merged=True)
    w, gh, al = watcher(_watched(config, SLUG), merged, [])
    for name in (
        "develop-fahera-mx-alissa-github-review-daemon-i46-a1",
        "fix-fahera-mx-studio-alissa-app-pr304-r1-a1",
        "maintain-fahera-mx-studio-alissa-app-pr293-a1",
    ):
        _live(al, name)

    assert w.sweep_sessions() == 0
    assert al.killed == []
    assert gh.pr_fetches == 0
    assert len(al.sessions) == 3, "they must still be running afterwards"


def test_sweep_skips_a_ledgerless_session_whose_pr_cannot_be_fetched(config):
    """A GitHub failure is logged and skipped, never fatal to the walk: the
    session after it is still considered in the same pass."""
    merged = make_pr(state="closed", merged=True)
    w, gh, al = watcher(_watched(config, SLUG), merged, [])
    _resolve_prs(gh, {(SLUG, 9): merged})  # #7 blows up, #9 resolves
    _live(al, "review-pr-7")
    _live(al, "review-pr-9")

    assert w.sweep_sessions() == 1
    assert al.killed == ["review-pr-9"]


# -- the post-sweep cap check ----------------------------------------------


def test_cap_check_pages_when_the_sweep_cannot_keep_up(config, caplog):
    cfg = dataclasses.replace(_watched(config, SLUG), reap_session_cap=2)
    w, _, al = watcher(cfg, make_pr(), [review()])  # open PR: nothing reapable
    for number in (1, 2, 3):
        _live(al, f"review-pr-{number}")

    with caplog.at_level(logging.ERROR):
        assert w.sweep_sessions() == 0

    pages = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(pages) == 1
    assert "CAP EXCEEDED" in pages[0].message
    page = pages[0].getMessage()
    assert all(f"review-pr-{n} (" in page for n in (1, 2, 3))


def test_cap_check_counts_what_the_sweep_left_behind(config, caplog):
    """Reaped sessions are not 'live after the sweep' — a pass that cleared
    itself back under the cap must not page."""
    cfg = dataclasses.replace(_watched(config, SLUG), reap_session_cap=2)
    merged = make_pr(state="closed", merged=True)
    w, _, al = watcher(cfg, merged, [])
    for number in (1, 2, 3):
        _live(al, f"review-pr-{number}")
    _resolve_prs(w.github, {(SLUG, n): merged for n in (1, 2, 3)})

    with caplog.at_level(logging.ERROR):
        assert w.sweep_sessions() == 3

    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


# -- round 2: the probe must not be paid per poll ---------------------------


def _sweep_n(w, times):
    for _ in range(times):
        w.sweep_sessions()
    return w.github.pr_fetches


def test_the_allowlist_probe_is_paid_once_per_session_not_once_per_poll(config):
    """A bare-name session on an OPEN PR is spared by design and stays live for
    the life of the PR, so re-probing the whole allowlist each poll turned a
    documented cost into a budget problem (7 repos x 120 polls/h). The probe's
    answer is cached per session name; only the PR-state fetch, which is what
    can turn the session reapable, repeats."""
    open_pr = make_pr()
    repos = [f"acme/r{n}" for n in range(5)] + [SLUG]
    w, gh, al = watcher(_watched(config, *repos), open_pr, [review()])
    _resolve_prs(gh, {(SLUG, 7): open_pr})  # the other five 404 on #7
    _live(al, "review-pr-7")

    assert _sweep_n(w, 3) == 6 + 1 + 1, "6 probes once, then one state fetch/poll"
    assert al.killed == []


def test_an_unresolvable_name_is_probed_once_and_then_costs_nothing(config):
    """The sticky negative: two watched repos both have PR #7, so the session
    is spared forever. Re-probing it forever is exactly the cost that made the
    per-poll probe untenable."""
    merged = make_pr(state="closed", merged=True)
    other = dataclasses.replace(merged, repo="gadgets")
    w, gh, al = watcher(_watched(config, SLUG, "acme/gadgets"), merged, [])
    _resolve_prs(gh, {(SLUG, 7): merged, ("acme/gadgets", 7): other})
    _live(al, "review-pr-7")

    assert _sweep_n(w, 3) == 2, "both repos probed once; nothing after that"
    assert al.killed == []


def test_the_probe_cache_drops_sessions_that_left_the_live_list(config):
    """Names are unique per spawn and a session's PR never changes, so the only
    invalidation needed is forgetting names that are gone — otherwise the cache
    grows without bound in a daemon that polls forever."""
    merged = make_pr(state="closed", merged=True)
    w, gh, al = watcher(_watched(config, SLUG), merged, [])
    _resolve_prs(gh, {(SLUG, 7): merged, (SLUG, 9): merged})
    _live(al, "review-pr-7")
    w.sweep_sessions()
    assert "review-pr-7" in w._probe_cache

    al.sessions = []
    _live(al, "review-pr-9")
    w.sweep_sessions()

    assert list(w._probe_cache) == ["review-pr-9"]


# -- round 2: the cap alarm explains itself, once per episode ---------------


def test_the_cap_page_carries_why_each_survivor_was_spared(config, caplog):
    """The per-session holdout lines are debug and the container runs at INFO,
    so an alarm without the reasons inline is an alarm nobody can act on."""
    cfg = dataclasses.replace(_watched(config, SLUG), reap_session_cap=1)
    open_pr = make_pr()
    w, gh, al = watcher(cfg, open_pr, [review()])
    _resolve_prs(gh, {(SLUG, 7): open_pr})
    _live(al, "review-pr-7")  # idle, past grace, open PR, no ledger row
    _live(al, "review-pr-8", status="busy")

    with caplog.at_level(logging.ERROR):
        w.sweep_sessions()

    page = next(r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    assert "review-pr-7 (acme/widgets#7 is open and there is no ledger row" in page
    assert "review-pr-8 (busy — never reaped)" in page


def test_the_cap_page_fires_once_per_episode_not_once_per_poll(config, caplog):
    """Every 30s in the deployed config; 120 identical pages an hour is not a
    page. It must re-fire when the survivor set changes, and again after the
    count has fallen back inside the cap."""
    cfg = dataclasses.replace(_watched(config, SLUG), reap_session_cap=1)
    w, _, al = watcher(cfg, make_pr(), [review()])
    _live(al, "review-pr-7")
    _live(al, "review-pr-8")

    with caplog.at_level(logging.ERROR):
        w.sweep_sessions()
        w.sweep_sessions()  # same survivors — silent
        assert len(caplog.records) == 1

        _live(al, "review-pr-9")  # the set changed — page again
        w.sweep_sessions()
        assert len(caplog.records) == 2

        al.sessions = [al.sessions[0]]  # back inside the cap — clears
        w.sweep_sessions()
        assert len(caplog.records) == 2

        al.sessions = [ManagedSession(name=f"review-pr-{n}", status="idle") for n in (7, 8)]
        w.sweep_sessions()  # a NEW episode of the same set pages again
        assert len(caplog.records) == 3


# -- round 2: reap_grace_seconds is coupled to the stale window -------------


def test_a_grace_at_or_above_the_stale_window_is_rejected(tmp_path):
    """The same knob gates the stale-round liveness probe. At or above
    STALE_ROUND_SECONDS its 'idle-finished -> dead -> respawn' branch is
    unreachable: every stale round defers forever and only the operator ping
    fires. Loud at config time instead of silently wedged at runtime."""
    with pytest.raises(ValueError, match="stale-round window"):
        Config.build(tmp_path, {"reap_grace_seconds": STALE_ROUND_SECONDS})
    with pytest.raises(ValueError, match="stale-round window"):
        Config.build(tmp_path, {"reap_grace_seconds": 4 * 60 * 60})

    ok = Config.build(tmp_path, {"reap_grace_seconds": STALE_ROUND_SECONDS - 1})
    assert ok.reap_grace_seconds == STALE_ROUND_SECONDS - 1


def test_stale_round_constant_is_still_importable_from_loop():
    """It moved to `config` so the validation above can see it; `loop` re-exports
    it because webui and the tests import it from there."""
    from alissa.tools.github.revloop import config as config_mod
    from alissa.tools.github.revloop import loop as loop_mod

    assert loop_mod.STALE_ROUND_SECONDS is config_mod.STALE_ROUND_SECONDS


# -- AC2: reaping must not disturb round accounting or cap re-entry ---------


def _accounting_run(config, tmp_path, db_name, *, reap):
    """One identical scenario -- open PR, round 1 done, one operator re-entry
    grant on the ledger -- decided with and without a reap of round 1's
    session. Everything but the reap is byte-identical between the two."""
    pr = make_pr()
    st = State(tmp_path / db_name)
    w, _, al = watcher(config, pr, [review()], state=st, verdict_count=1)
    s1 = _record(w, pr, 1)
    w.state.record_grant(SLUG, NUMBER, 4242, "RHDZMOTA", 2)
    if reap:
        _live(al, s1)
        assert w.sweep_sessions() == 1, "the reap under test must actually happen"
    return w.evaluate(OWNER, REPO, NUMBER), w.state.granted_rounds(SLUG, NUMBER)


def test_round_accounting_and_re_entry_ignore_whether_sessions_were_reaped(
    config, tmp_path
):
    """AC2. Rounds are counted from CR6 verdict envelopes and the effective
    cap from the grants table; the reaps table is bookkeeping that nothing
    consults. So a PR whose earlier-round sessions were reaped decides
    exactly like one whose were not."""
    reaped_decision, reaped_grants = _accounting_run(
        config, tmp_path, "reaped.db", reap=True
    )
    kept_decision, kept_grants = _accounting_run(
        config, tmp_path, "kept.db", reap=False
    )

    assert reaped_decision.action is kept_decision.action is Action.SPAWNED
    assert reaped_decision.round == kept_decision.round == 2
    assert reaped_grants == kept_grants == 2


# -- stale rounds: two-signal staleness (timer + liveness) + floor -----------

def _backdate(st, seconds):
    """Age every spawn on the ledger so the stale timer has fired."""
    st._db.execute(
        "UPDATE spawns SET spawned_at=?", (int(time.time()) - int(seconds),)
    )
    st._db.commit()


PAST_STALE = STALE_ROUND_SECONDS + 60
PAST_FLOOR = STALLED_DEFER_MULTIPLE * STALE_ROUND_SECONDS + 60


def test_stale_round_with_busy_session_defers_not_respawns(config):
    """THE double-spend this exists to stop (double round-2 approves on
    devloop#11, double approves on #19 of this repo): the stale timer fired
    but the round's session is still busy reviewing. A timer-only re-enqueue
    spawns a second reviewer over the first; both submit. This test fails if
    the liveness probe is removed from the stale path."""
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    session = al.enqueued[0]["session"]
    _live(al, session, status="busy")
    _backdate(st, PAST_STALE)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.IN_FLIGHT
    assert len(al.enqueued) == 1, "must not spawn a second reviewer over a live one"
    assert session in d.reason


def test_stale_round_with_dead_session_respawns(config):
    """Timer fired AND the session is gone from the live list: both signals
    agree the round is dead — re-enqueue as before."""
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    _backdate(st, PAST_STALE)  # session never added to the live list

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert len(al.enqueued) == 2


def test_stale_round_with_idle_finished_session_respawns(config):
    """Idle past the quiet period without ever submitting = the session died
    at its prompt. That is not liveness; respawn."""
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    _live(al, al.enqueued[0]["session"],
          last_activity=time.time() - config.reap_grace_seconds * 2)
    _backdate(st, PAST_STALE)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert len(al.enqueued) == 2


def test_stale_round_with_recently_active_idle_session_defers(config):
    """Idle-but-recent is how a claude session looks between turns; the same
    quiet-period doctrine as the reap sweep applies before respawning over it."""
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    _live(al, al.enqueued[0]["session"], last_activity=time.time())
    _backdate(st, PAST_STALE)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.IN_FLIGHT
    assert len(al.enqueued) == 1


def test_unprobeable_session_list_defers_the_respawn(config):
    """No liveness evidence is not evidence of death: respawning blind is
    exactly the double-spend, so a failed `alissa tmux ls` defers one poll."""
    from alissa.tools.github.revloop.proc import CommandError

    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    _backdate(st, PAST_STALE)

    def boom():
        raise CommandError(["alissa", "tmux", "ls"], 1, "no tmux server")

    al.list_review_sessions = boom
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.IN_FLIGHT
    assert len(al.enqueued) == 1


def test_floor_pings_the_operator_once_per_episode(config):
    """Past STALLED_DEFER_MULTIPLE stale windows with the session still busy,
    the deferral gets a floor: one operator comment, then keep deferring —
    a second poll in the same episode must not comment again."""
    st = State(config.state_db)
    w, gh, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    session = al.enqueued[0]["session"]
    _live(al, session, status="busy")
    _backdate(st, PAST_FLOOR)

    first = w.evaluate(OWNER, REPO, NUMBER)
    second = w.evaluate(OWNER, REPO, NUMBER)

    assert first.action is Action.IN_FLIGHT
    assert second.action is Action.IN_FLIGHT
    assert len(al.enqueued) == 1, "the floor pings, it never respawns"
    assert len(operator_comments(gh)) == 1, "one ping per deferral episode"
    assert "stalled" in operator_comments(gh)[0].lower()
    assert session in operator_comments(gh)[0]
    assert st.pinged(f"{OWNER}/{REPO}", NUMBER, stalled_kind(session))


def test_deferral_below_the_floor_does_not_ping(config):
    st = State(config.state_db)
    w, gh, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    _live(al, al.enqueued[0]["session"], status="busy")
    _backdate(st, PAST_STALE)  # stale, but inside the first extra window

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.IN_FLIGHT
    assert operator_comments(gh) == []


def test_a_new_deferral_episode_pings_again(config):
    """Episode-keyed dedupe: after the wedged session is killed and the round
    re-enqueued, the NEW session stalling must ping again — keyed on the bare
    kind, the second episode would defer silently forever."""
    st = State(config.state_db)
    w, gh, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    s1 = al.enqueued[0]["session"]
    _live(al, s1, status="busy")
    _backdate(st, PAST_FLOOR)
    w.evaluate(OWNER, REPO, NUMBER)  # episode 1's ping
    assert len(operator_comments(gh)) == 1

    al.sessions = []  # the operator killed the wedged session
    w.evaluate(OWNER, REPO, NUMBER)  # stale + dead -> respawn (episode 2)
    s2 = al.enqueued[1]["session"]
    assert s2 != s1
    _live(al, s2, status="busy")
    _backdate(st, PAST_FLOOR)

    w.evaluate(OWNER, REPO, NUMBER)

    assert len(operator_comments(gh)) == 2, "a fresh episode must ping again"


def test_floor_ping_dry_run_is_silent(config):
    """Dry-run must neither comment nor burn the episode's ledger row (a
    later real run still owes the operator the ping)."""
    from dataclasses import replace

    cfg = replace(config, dry_run=True)
    st = State(cfg.state_db)
    w, gh, al = watcher(cfg, make_pr(), [], state=st)
    pr = make_pr()
    session = _record(w, pr, 1)  # a real run recorded this spawn earlier
    _live(al, session, status="busy")
    _backdate(st, PAST_FLOOR)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.IN_FLIGHT
    assert gh.comments == []
    assert not st.pinged(f"{OWNER}/{REPO}", NUMBER, stalled_kind(session))
    assert al.enqueued == []


def test_failed_ping_comment_retries_next_poll(config):
    """The ping is the operator's only signal for the episode: the ledger row
    lands only after the comment posts, so a transient failure retries."""
    from alissa.tools.github.revloop.proc import CommandError

    st = State(config.state_db)
    w, gh, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    _live(al, al.enqueued[0]["session"], status="busy")
    _backdate(st, PAST_FLOOR)

    calls = []

    def flaky(owner, repo, number, body):
        calls.append(body)
        if len(calls) == 1:
            raise CommandError(["gh", "api"], 1, "boom")

    gh.comment = flaky
    w.evaluate(OWNER, REPO, NUMBER)  # ping fails -> episode not recorded
    w.evaluate(OWNER, REPO, NUMBER)  # retried and lands
    w.evaluate(OWNER, REPO, NUMBER)  # now deduped

    assert len(calls) == 2


# -- the mechanical activity comment ----------------------------------------


def test_spawns_append_lines_to_one_activity_comment(config):
    """Round-k spawns across polls land as appended lines in a SINGLE
    marker-carrying issue comment: created on the first spawn, PATCHed ever
    after — never a second comment per round."""
    st = State(config.state_db)
    gh = FakeGitHub(make_pr(), [])
    al = FakeAlissa(FakeTask(), verdict_count=0)
    w = ReviewWatcher(config, github=gh, alissa=al, state=st)

    assert w.evaluate(OWNER, REPO, NUMBER).round == 1
    al.verdict_count = 1  # round 1's verdict envelope landed
    gh._reviews.append(review())  # ...and so did its native review
    assert w.evaluate(OWNER, REPO, NUMBER).round == 2

    acts = activity_comments(gh)
    assert len(acts) == 1, "exactly ONE activity comment per PR"
    body = acts[0].body
    assert "round 1 of 3" in body
    assert "round 2 of 3" in body
    assert al.enqueued[0]["session"] in body
    assert al.enqueued[1]["session"] in body
    assert "UTC" in body
    # The comment-create path ran once; round 2's line arrived via PATCH.
    assert len([c for c in gh.comments if ACTIVITY_MARKER in c]) == 1


def test_stale_reenqueue_appends_its_context_line(config):
    st = State(config.state_db)
    w, gh, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    _backdate(st, PAST_STALE)  # stale, session gone -> presumed dead

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    body = activity_comments(gh)[0].body
    assert "re-enqueued — previous session presumed dead" in body
    assert al.enqueued[1]["session"] in body


def test_liveness_deferral_appends_one_line_per_episode(config):
    """The deferral is re-decided every poll; appending per decision would
    grow the comment by a line a minute for hours. One line per deferral
    episode (keyed on the session, like the stalled ping) says the same
    thing without the spam."""
    st = State(config.state_db)
    w, gh, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    session = al.enqueued[0]["session"]
    _live(al, session, status="busy")
    _backdate(st, PAST_STALE)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.IN_FLIGHT
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.IN_FLIGHT

    body = activity_comments(gh)[0].body
    assert f"deferred — session `{session}` still busy" in body
    assert body.count("deferred") == 1, "one deferral line per episode"
    assert st.pinged(SLUG, NUMBER, deferral_activity_kind(session))


def test_spoofed_marker_from_another_author_is_never_patched(config):
    """Anyone can paste the hidden marker into their own comment; the
    find-or-create filter is own-author AND marker, so a spoof is ignored
    and the daemon still creates (and appends to) its OWN comment."""
    w, gh, al = watcher(config, make_pr(), [])
    spoof = f"{ACTIVITY_MARKER}\nnot the daemon's comment"
    gh.seed_comment("mallory", spoof)

    w.evaluate(OWNER, REPO, NUMBER)

    spoofed = [c for c in gh.issue_store if c.author == "mallory"]
    assert spoofed[0].body == spoof, "the spoofed comment must never be PATCHed"
    mine = [c for c in activity_comments(gh) if c.author == "alissa-app"]
    assert len(mine) == 1
    assert al.enqueued[0]["session"] in mine[0].body


def _raising(exc):
    def boom(*a, **k):
        raise exc

    return boom


@pytest.mark.parametrize("surface", ["list", "list-rate-limited", "create"])
def test_activity_failures_never_block_the_spawn(config, surface):
    """Best-effort by contract: list/create/PATCH failures — including a
    rate-limit, which _api surfaces as RateLimited, not CommandError — log a
    warning and the spawn still goes through."""
    from alissa.tools.github.revloop.ghclient import RateLimited
    from alissa.tools.github.revloop.proc import CommandError

    w, gh, al = watcher(config, make_pr(), [])
    if surface == "list":
        gh.issue_comments = _raising(CommandError(["gh", "api"], 1, "boom"))
    elif surface == "list-rate-limited":
        gh.issue_comments = _raising(RateLimited("limit"))
    else:  # create: listing worked, no activity comment exists yet
        gh.comment = _raising(CommandError(["gh", "api"], 1, "boom"))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert len(al.enqueued) == 1


def test_activity_dry_run_appends_nothing(config, caplog):
    import logging as _logging
    from dataclasses import replace

    cfg = replace(config, dry_run=True)
    w, gh, al = watcher(cfg, make_pr(), [], state=State(cfg.state_db))

    with caplog.at_level(_logging.INFO, logger="alissa.tools.github.revloop.loop"):
        d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert gh.issue_store == [] and gh.comments == []
    assert any(
        "[dry-run] would append" in r.getMessage() for r in caplog.records
    ), "dry-run must say what it would have appended"


def test_deferral_activity_dry_run_burns_no_episode(config):
    """Like the floor ping, dry-run must not record the deferral episode — a
    later real run still owes the PR its deferral line."""
    from dataclasses import replace

    cfg = replace(config, dry_run=True)
    st = State(cfg.state_db)
    w, gh, al = watcher(cfg, make_pr(), [], state=st)
    pr = make_pr()
    session = _record(w, pr, 1)  # a real run recorded this spawn earlier
    _live(al, session, status="busy")
    _backdate(st, PAST_STALE)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.IN_FLIGHT
    assert gh.issue_store == []
    assert not st.pinged(SLUG, NUMBER, deferral_activity_kind(session))


def test_escalation_comments_stay_separate_from_the_activity_comment(config):
    reviews = [review("CHANGES_REQUESTED", at=f"2026-07-18T1{i}:00:00Z") for i in range(3)]
    w, gh, _ = watcher(config, make_pr(), reviews)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.ESCALATED

    assert len(gh.comments) == 1
    assert ACTIVITY_MARKER not in gh.comments[0], "cap-out must not carry the marker"
    assert activity_comments(gh) == []


def test_activity_comment_creates_no_review_record(config):
    """PINNING: the activity comment is an ISSUE comment. It must never
    appear as a review record, so round counting — substantive submitted
    reviews — is provably unaffected by any number of activity appends.

    task=None pins the GitHub-count fallback, the path where a leaked
    record would corrupt the round math directly: if the activity comment
    counted as a review, `completed` would read 2 and the re-evaluation
    below would spawn round 3 instead of reporting round 2 in flight.
    """
    pr = make_pr()
    w, gh, al = watcher(config, pr, [review()], task=None)
    before = gh.my_reviews(OWNER, REPO, NUMBER)

    d = w.evaluate(OWNER, REPO, NUMBER)  # spawns round 2, appends the line

    assert d.round == 2
    assert len(activity_comments(gh)) == 1, "the line landed as an issue comment"
    assert gh.my_reviews(OWNER, REPO, NUMBER) == before, (
        "posting the activity comment must not create a review record"
    )
    d2 = w.evaluate(OWNER, REPO, NUMBER)
    assert d2.action is Action.IN_FLIGHT
    assert d2.round == 2, "round math unchanged by the activity comment"


# -- run_forever ------------------------------------------------------------

def test_run_forever_exits_cleanly_on_interrupt_during_sleep(config, monkeypatch):
    """The dominant real case: with a 60s poll interval (up to 900s backing
    off) the loop spends nearly all its wall-clock inside time.sleep, so
    Ctrl-C almost always lands there — it must hit the same clean-exit path,
    not traceback out of run_forever."""
    from alissa.tools.github.revloop import loop as loop_mod

    w, _, _ = watcher(config, make_pr(), [])
    polls = []
    w.poll_once = lambda: polls.append(1)

    def interrupt(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(loop_mod.time, "sleep", interrupt)
    w.run_forever()  # must return cleanly, not propagate KeyboardInterrupt

    assert polls == [1], "the interrupt landed in the sleep after one poll"


# -- round number from verdict envelopes, not GitHub body-presence ----------

def test_round_number_comes_from_envelopes_when_github_overcounts(config):
    # Two substantive GitHub reviews landed in one cycle (overcount), but the
    # task holds ONE verdict envelope -> this is round 2, not round 3, and the
    # session name is r2 (no reuse/collision).
    pr = make_pr()
    reviews = [review(), review(at="2026-07-18T10:03:00Z")]
    w, _, al = watcher(config, pr, reviews, verdict_count=1)
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.SPAWNED
    assert d.round == 2
    assert al.enqueued[-1]["session"].startswith("review-widgets-pr7-r2-")


def test_empty_body_round_still_counts_via_envelope(config, no_post_grace):
    # The prior round's GitHub review had an empty body (is_substantive False),
    # so github shows 0 countable reviews -- but its verdict envelope was
    # recorded. Two things follow, in order: that round has no verdict of
    # record, so the daemon posts one natively (issue #51); and the NEXT round
    # is numbered from the envelope, so it is round 2 and not a repeat of
    # round 1 (a repeat collides on the session name -> the worker wedges).
    pr = make_pr()
    w, gh, al = watcher(
        config, pr, [], verdict_count=1, verdict=VERDICT_REQUEST_CHANGES
    )

    posted = w.evaluate(OWNER, REPO, NUMBER)
    assert posted.action is Action.POSTED
    assert gh.submitted[0]["event"] == "REQUEST_CHANGES"

    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.SPAWNED
    assert d.round == 2


def test_falls_back_to_github_count_when_no_review_task(config):
    # Round 1, before any review task exists: fall back to the github count.
    pr = make_pr()
    w, _, al = watcher(config, pr, [], task=None, verdict_count=0)
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.round == 1


def test_count_verdicts_counts_only_envelope_evidence():
    from alissa.tools.github.revloop.alissa import Alissa
    payload = {
        "evidence": [
            {"title": "Review verdict: acme/widgets#7 — request_changes (round 1)"},
            {"markdownContent": "# Review verdict: acme/widgets#7 — approve\n\n..."},
            {"title": "Design note", "markdownContent": "not a verdict"},
            {"title": None, "markdownContent": None},
        ]
    }
    assert Alissa._count_verdicts(payload) == 2
    assert Alissa._count_verdicts({}) == 0
    assert Alissa._count_verdicts({"evidence": "nope"}) == 0
    assert Alissa._count_verdicts("garbage") == 0


# -- poll snapshots (the console sidecar's exhaust) ------------------------


def _stale_spawn(st, session):
    """Backdate a recorded spawn past the staleness threshold."""
    st._db.execute(
        "UPDATE spawns SET spawned_at=? WHERE session=?",
        (int(time.time()) - STALE_ROUND_SECONDS - 60, session),
    )
    st._db.commit()


def test_poll_writes_exactly_one_snapshot_per_pass(config):
    w, _, _ = watcher(config, make_pr(), [])
    assert w.state.read_snapshots() == []

    w.poll_once()
    w.poll_once()

    assert len(w.state.read_snapshots()) == 2, "one snapshot per poll pass"


def test_empty_pass_still_writes_a_snapshot(config):
    w, gh, _ = watcher(config, make_pr(), [])
    gh.requests = []  # nothing pending

    w.poll_once()

    snap = w.state.read_snapshots()[0]
    assert snap["candidates"] == 0
    assert snap["stages"] == []


def test_snapshot_records_a_spawn(config):
    w, _, al = watcher(config, make_pr(), [])

    w.poll_once()

    snap = w.state.read_snapshots()[0]
    assert snap["candidates"] == 1
    assert snap["spawned"] == 1
    assert snap["stale_reenqueued"] == 0
    stage = snap["stages"][0]
    assert stage["slug"] == f"{OWNER}/{REPO}#{NUMBER}"
    assert stage["number"] == NUMBER
    assert stage["round"] == 1
    assert stage["attempt"] is None
    assert stage["stage"] == "spawned"
    assert stage["session"] == al.enqueued[0]["session"]
    assert stage["task_ref"] == "TASK-500"


def test_snapshot_records_a_skip(config):
    w, _, _ = watcher(config, make_pr(draft=True), [])

    w.poll_once()

    snap = w.state.read_snapshots()[0]
    assert snap["skipped"] == 1
    assert snap["stages"][0]["stage"] == "skipped"


def test_snapshot_records_convergence(config):
    w, _, _ = watcher(config, make_pr(), [review("APPROVED")], verdict="approve")

    w.poll_once()

    snap = w.state.read_snapshots()[0]
    assert snap["converged"] == 1
    assert snap["stages"][0]["stage"] == "converged"


def test_snapshot_records_an_escalation(config):
    reviews = [
        review(at="2026-07-18T10:00:00Z"),
        review(at="2026-07-18T11:00:00Z"),
        review(at="2026-07-18T12:00:00Z"),
    ]
    w, _, _ = watcher(config, make_pr(), reviews)  # 3 rounds, cap 3, no approve

    w.poll_once()

    snap = w.state.read_snapshots()[0]
    assert snap["escalated"] == 1
    assert snap["stages"][0]["stage"] == "escalated"


def test_snapshot_distinguishes_a_liveness_deferral_from_in_flight(config):
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)  # round 1 spawn recorded
    name = al.enqueued[0]["session"]
    _stale_spawn(st, name)
    _live(al, name, status="busy")  # session still alive → defer, don't respawn

    w.poll_once()

    snap = st.read_snapshots()[0]
    assert snap["deferred"] == 1, "a liveness deferral is its own bucket"
    assert snap["in_flight"] == 0
    stage = snap["stages"][0]
    assert stage["stage"] == "deferred"
    assert stage["session"] == name


def test_snapshot_records_a_stale_reenqueue(config):
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)  # round 1 spawn recorded
    name = al.enqueued[0]["session"]
    _stale_spawn(st, name)
    # No live session for `name` → the round's reviewer is presumed dead, so
    # the pass respawns it: the "stale-re-enqueued" bucket, not a fresh spawn.

    w.poll_once()

    snap = st.read_snapshots()[0]
    assert snap["stale_reenqueued"] == 1
    assert snap["spawned"] == 0
    assert snap["stages"][0]["stage"] == "stale-re-enqueued"


def test_snapshot_records_the_reap_count(config):
    pr = make_pr()
    w, gh, al = watcher(config, pr, [review("APPROVED")], verdict="approve")
    gh.requests = []  # approved → the request is gone; only the sweep acts
    s1 = _record(w, pr, 1)
    _live(al, s1)  # idle and quiet → reapable

    w.poll_once()

    assert al.killed == [s1]
    assert w.state.read_snapshots()[0]["reaped"] == 1


def test_poll_captures_a_snapshot_in_dry_run(config):
    """AC: a dry-run pass OBSERVES the daemon's state — the snapshot is written
    even though the pass mutates nothing (no spawn ledger row, no reap)."""
    dry = dataclasses.replace(config, dry_run=True)
    w, _, _ = watcher(dry, make_pr(), [])
    assert w.state.read_snapshots() == []

    w.poll_once()

    snap = w.state.read_snapshots()[0]
    assert snap["candidates"] == 1
    assert snap["spawned"] == 1
    # The observation is recorded, but the spawn ledger was NOT touched.
    assert w.state.get_spawn(SLUG, NUMBER, 1) is None


def test_dry_run_snapshot_reaps_nothing(config):
    dry = dataclasses.replace(config, dry_run=True)
    pr = make_pr()
    w, gh, al = watcher(dry, pr, [review("APPROVED")], verdict="approve")
    gh.requests = []
    s1 = _record(w, pr, 1)
    _live(al, s1)  # reapable, but dry-run kills nothing

    w.poll_once()

    assert al.killed == []
    assert w.state.read_snapshots()[0]["reaped"] == 0


# -- operator re-entry after cap-out (issue #42) ---------------------------
#
# Live shape (studio PR #277): the loop ran the full cap, every round's
# findings were fixed, the last verdict even enumerated the path back to
# approve -- and the fixed head then sat unreviewable, because the only lever
# was raising round_cap globally and restarting the daemon. An explicit,
# allowlisted, bounded operator ack is that lever; everything below pins it.

OPERATOR = "operator-1"
ACK = "alissa-review: re-enter +1"


@pytest.fixture
def ack_config(config):
    """The cap-3 config, with one allowlisted operator."""
    return dataclasses.replace(config, operators=(OPERATOR,))


def capped_reviews(n=3, sha="abc123"):
    return [
        review("CHANGES_REQUESTED", sha=sha, at=f"2026-07-18T1{i}:00:00Z")
        for i in range(n)
    ]


def add_round(gh, al, *, sha="abc123", at="2026-07-18T20:00:00Z"):
    """A further round lands: one more substantive review, one more envelope."""
    gh._reviews.append(review("CHANGES_REQUESTED", sha=sha, at=at))
    al.verdict_count = sum(1 for r in gh._reviews if r.is_substantive)


# -- the ack grammar -------------------------------------------------------


@pytest.mark.parametrize(
    "body,rounds",
    [
        (ACK, 1),
        (f"`{ACK}`", 1),                                    # backticked
        ("ALISSA-REVIEW: RE-ENTER +2", 2),                  # logins shout
        (f"looks fixed to me\n{ACK}\nthanks", 1),           # prose around it
        (f"{ACK}\n{ACK}", 1),                               # one comment, one grant
        ("alissa-review: re-enter +5", 5),                  # the ceiling itself
    ],
)
def test_well_formed_acks_parse(body, rounds):
    assert parse_reentry_ack(body).rounds == rounds


@pytest.mark.parametrize(
    "body",
    [
        "",
        "lgtm, merging",
        f"> {ACK}",                                    # quoting the escalation
        f"just post `{ACK}` when you want another round",   # naming it in prose
    ],
)
def test_non_directives_are_not_acks(body):
    ack = parse_reentry_ack(body)
    assert not ack.is_directive
    assert ack.rounds is None


@pytest.mark.parametrize(
    "body,problem",
    [
        ("alissa-review: re-enter 1", "malformed"),          # no +
        ("alissa-review: reenter +1", "malformed"),          # no hyphen
        ("alissa-review: re-enter +", "malformed"),
        ("alissa-review: re-enter +0", "ceiling"),
        ("alissa-review: re-enter +6", "ceiling"),
        ("alissa-review: re-enter +99", "ceiling"),
        (f"{ACK}\nalissa-review: re-enter +2", "contradictory"),
    ],
)
def test_ill_formed_directives_are_refused_with_a_reason(body, problem):
    ack = parse_reentry_ack(body)
    assert ack.rounds is None
    assert ack.is_directive, "a directive that misses the grammar must be reported"
    assert problem in ack.problem


def test_the_re_entry_ceiling_is_pinned():
    """A bigger ack is two comments, not one bigger number — the ceiling is
    part of 'impossible to fire accidentally'."""
    assert MAX_REENTRY_ROUNDS == 5
    assert parse_reentry_ack(f"alissa-review: re-enter +{MAX_REENTRY_ROUNDS}").rounds == 5


# -- no ack, no rounds -----------------------------------------------------


def test_capped_pr_without_an_ack_is_unchanged(ack_config):
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    gh.seed_comment(OPERATOR, "nice work, I'll take a look tomorrow")

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.ESCALATED
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CAPPED
    assert al.enqueued == [], "no ack, no rounds"
    assert st.granted_rounds(SLUG, NUMBER) == 0
    assert len(operator_comments(gh)) == 1


def test_a_well_formed_ack_admits_exactly_n_more_rounds(ack_config):
    """+2 buys two rounds — and then the cap holds again."""
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)                      # cap-out page
    gh.seed_comment(OPERATOR, "alissa-review: re-enter +2")

    fourth = w.evaluate(OWNER, REPO, NUMBER)
    assert fourth.action is Action.SPAWNED and fourth.round == 4
    add_round(gh, al)                                    # round 4 lands, no approve

    fifth = w.evaluate(OWNER, REPO, NUMBER)
    assert fifth.action is Action.SPAWNED and fifth.round == 5
    add_round(gh, al, at="2026-07-18T21:00:00Z")         # round 5 lands, no approve

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.ESCALATED
    assert len(al.enqueued) == 2, "exactly the two rounds that were granted"
    assert st.granted_rounds(SLUG, NUMBER) == 2


def test_the_grant_is_counted_once_never_per_poll(ack_config):
    """The ack sits in the comment list forever; it must grant once."""
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, ACK)

    w.evaluate(OWNER, REPO, NUMBER)                      # round 4 spawns
    add_round(gh, al)                                    # round 4 lands
    w.evaluate(OWNER, REPO, NUMBER)                      # capped again, re-scans
    w.evaluate(OWNER, REPO, NUMBER)

    assert st.granted_rounds(SLUG, NUMBER) == 1
    assert len(st.read_grants(SLUG, NUMBER)) == 1
    assert len(al.enqueued) == 1, "one grant, one round — never per poll"


def test_a_second_grant_needs_a_second_comment(ack_config):
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, ACK)
    w.evaluate(OWNER, REPO, NUMBER)                      # round 4
    add_round(gh, al)
    w.evaluate(OWNER, REPO, NUMBER)                      # grant consumed → capped

    gh.seed_comment(OPERATOR, ACK)                       # a SECOND comment
    fifth = w.evaluate(OWNER, REPO, NUMBER)

    assert fifth.action is Action.SPAWNED and fifth.round == 5
    assert st.granted_rounds(SLUG, NUMBER) == 2
    assert len(st.read_grants(SLUG, NUMBER)) == 2


def test_the_grant_is_logged_loudly_and_appended_to_the_activity_comment(
    ack_config, caplog
):
    st = State(ack_config.state_db)
    w, gh, _ = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, "alissa-review: re-enter +2")

    with caplog.at_level(logging.WARNING):
        w.evaluate(OWNER, REPO, NUMBER)

    assert "RE-ENTRY GRANT" in caplog.text
    assert OPERATOR in caplog.text and "cap 3 → 5" in caplog.text
    activity = activity_comments(gh)[0].body
    assert "re-entry ack" in activity
    assert f"`{OPERATOR}`" in activity and "+2 round(s)" in activity
    assert "effective cap 3 → 5" in activity


def test_the_activity_line_is_appended_once_per_grant(ack_config):
    st = State(ack_config.state_db)
    w, gh, _ = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, ACK)
    w.evaluate(OWNER, REPO, NUMBER)
    w.evaluate(OWNER, REPO, NUMBER)

    body = activity_comments(gh)[0].body
    assert body.count("re-entry ack") == 1


# -- refusals --------------------------------------------------------------


def test_two_grants_each_report_their_own_cap_transition_in_order(ack_config):
    """The before/after cap is per grant, and an append-only log must not go
    out backwards — the retry path (a failed append landing beside a newer
    grant) has the same shape as two acks read in one pass."""
    st = State(ack_config.state_db)
    w, gh, _ = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, ACK)                        # +1, comment 1001
    gh.seed_comment(OPERATOR, "alissa-review: re-enter +2")  # +2, comment 1002

    w.evaluate(OWNER, REPO, NUMBER)

    lines = [ln for ln in activity_comments(gh)[0].body.splitlines()
             if "re-entry ack" in ln]
    assert len(lines) == 2
    assert "(comment 1001)" in lines[0] and "+1 round(s)" in lines[0]
    assert "effective cap 3 → 4" in lines[0]
    assert "(comment 1002)" in lines[1] and "+2 round(s)" in lines[1]
    assert "effective cap 4 → 6" in lines[1]


def test_an_ack_from_a_non_operator_is_ignored_with_a_log_line(ack_config, caplog):
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment("passer-by", ACK)

    with caplog.at_level(logging.WARNING):
        d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.CAPPED
    assert al.enqueued == []
    assert st.granted_rounds(SLUG, NUMBER) == 0
    assert "not an allowlisted operator" in caplog.text


def test_a_malformed_ack_from_an_operator_is_ignored_with_a_log_line(
    ack_config, caplog
):
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, "alissa-review: re-enter please")

    with caplog.at_level(logging.WARNING):
        d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.CAPPED
    assert al.enqueued == []
    assert st.granted_rounds(SLUG, NUMBER) == 0
    assert "malformed re-entry directive" in caplog.text


def test_an_out_of_range_ack_is_ignored_with_a_log_line(ack_config, caplog):
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, "alissa-review: re-enter +50")

    with caplog.at_level(logging.WARNING):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CAPPED

    assert al.enqueued == []
    assert "outside the re-entry ceiling" in caplog.text


def test_a_refused_ack_is_logged_once_not_every_poll(ack_config, caplog):
    st = State(ack_config.state_db)
    w, gh, _ = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment("passer-by", ACK)

    with caplog.at_level(logging.WARNING):
        w.evaluate(OWNER, REPO, NUMBER)
        w.evaluate(OWNER, REPO, NUMBER)
        w.evaluate(OWNER, REPO, NUMBER)

    assert caplog.text.count("ignoring re-entry directive") == 1


def test_the_daemon_can_never_ack_its_own_escalation(ack_config):
    """The cap-out comment carries the grammar, so the reviewer identity is
    refused even when an operator lists it — otherwise the daemon could lift
    CR9's cap with nobody in the loop."""
    cfg = dataclasses.replace(ack_config, operators=(OPERATOR, "alissa-app"))
    st = State(cfg.state_db)
    w, gh, al = watcher(cfg, make_pr(), capped_reviews(), state=st)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.ESCALATED
    gh.seed_comment("alissa-app", ACK)                   # our own login, verbatim ack

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CAPPED
    assert al.enqueued == []
    assert st.granted_rounds(SLUG, NUMBER) == 0


def test_no_ack_is_honoured_without_an_operator_allowlist(config):
    """The lever fails closed: an empty `operators` honours nothing."""
    st = State(config.state_db)
    w, gh, al = watcher(config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, ACK)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CAPPED
    assert al.enqueued == []


def test_an_empty_allowlist_costs_no_comment_fetch(config):
    """A capped PR keeps its review request pending, so it is re-decided every
    poll forever — scanning a thread whose every directive would be discarded
    would be up to COMMENT_PAGE_LIMIT requests a minute, per PR, for nothing."""
    st = State(config.state_db)
    w, gh, _ = watcher(config, make_pr(), capped_reviews(), state=st)
    fetches = []
    real = gh.issue_comments
    gh.issue_comments = lambda *a, **k: (fetches.append(1), real(*a, **k))[1]

    w.evaluate(OWNER, REPO, NUMBER)   # escalates (the page IS posted)
    w.evaluate(OWNER, REPO, NUMBER)   # capped
    w.evaluate(OWNER, REPO, NUMBER)   # capped

    assert fetches == [], "no allowlist, no ack, no fetch"


def test_unreadable_comments_leave_the_pr_capped(ack_config):
    """Withholding a round is the safe direction; the scan retries next poll."""
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, ACK)

    def boom(*a, **k):
        raise CommandError(["gh", "api"], 1, "502 Bad Gateway")

    gh.issue_comments = boom
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CAPPED
    assert al.enqueued == []


def test_a_rate_limited_ack_scan_propagates(ack_config):
    """The backoff in run_forever is the response to a rate limit — the scan
    must not swallow one into a quiet 'capped'."""
    st = State(ack_config.state_db)
    w, gh, _ = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)

    def limited(*a, **k):
        raise RateLimited("API rate limit exceeded")

    gh.issue_comments = limited
    with pytest.raises(RateLimited):
        w.evaluate(OWNER, REPO, NUMBER)


# -- escalation stays once-only -------------------------------------------


def test_a_consumed_grant_re_escalates_once_naming_the_ack(ack_config):
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)                      # page 1
    gh.seed_comment(OPERATOR, ACK)
    w.evaluate(OWNER, REPO, NUMBER)                      # round 4 spawns
    add_round(gh, al)                                    # round 4: still no approve

    consumed = w.evaluate(OWNER, REPO, NUMBER)
    assert consumed.action is Action.ESCALATED
    pages = operator_comments(gh)
    assert len(pages) == 2, "a consumed grant is a fresh decision, not a repeat"
    assert f"granted by @{OPERATOR}" in pages[1]
    assert "consumed without an approve" in pages[1]
    assert "effective cap 4" in pages[1]

    # ...and then it is capped again, silently, until the next ack.
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CAPPED
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CAPPED
    assert len(operator_comments(gh)) == 2
    assert len(al.enqueued) == 1, "never past the effective cap"


def test_an_approve_in_a_granted_round_converges_without_escalating(ack_config):
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, ACK)
    w.evaluate(OWNER, REPO, NUMBER)                      # round 4 spawns

    add_round(gh, al)
    al.verdict = "approve"                               # the verification round approves

    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.CONVERGED
    assert len(operator_comments(gh)) == 1, "no second page after an approve"


# -- what the cap-out page teaches ----------------------------------------


def test_the_escalation_teaches_the_ack_grammar(ack_config):
    w, gh, _ = watcher(ack_config, make_pr(), capped_reviews())
    w.evaluate(OWNER, REPO, NUMBER)

    page = operator_comments(gh)[0]
    assert REENTRY_GRAMMAR in page
    assert f"1 to {MAX_REENTRY_ROUNDS}" in page
    assert "allowlisted operator" in page
    assert "a further grant needs a further comment" in page
    assert "effective cap 3" in page


def test_the_escalation_recommends_a_verification_round_when_the_head_moved(
    ack_config,
):
    """The PR #277 shape: fixes pushed after the last verdict, sitting
    unreviewed. One round is exactly what that needs."""
    reviews = capped_reviews(sha="0ldhead0")
    w, gh, _ = watcher(ack_config, make_pr(sha="f1xedhead"), reviews)
    w.evaluate(OWNER, REPO, NUMBER)

    page = operator_comments(gh)[0]
    assert "head has moved" in page
    assert "one round is usually all this needs" in page
    assert "alissa-review: re-enter +1" in page
    assert "0ldhead0" in page and "f1xedhea" in page
    # The last-verdict line must not claim the verdict covers the CURRENT head
    # — that is the opposite of what the hint below it is saying.
    assert "Last verdict: `changes_requested` on `0ldhead0`" in page
    assert "the head is now `f1xedhea`" in page


def test_the_escalation_omits_the_verification_hint_on_an_unmoved_head(ack_config):
    w, gh, _ = watcher(ack_config, make_pr(sha="abc123"), capped_reviews(sha="abc123"))
    w.evaluate(OWNER, REPO, NUMBER)

    page = operator_comments(gh)[0]
    assert "head has moved" not in page
    assert "Last verdict: `changes_requested` at `abc123`." in page, (
        "one sha is enough when nothing moved"
    )
    assert REENTRY_GRAMMAR in page, "the grammar is offered on every cap-out"


# -- the effective cap is what everything downstream reports ---------------


def test_a_granted_round_reports_the_effective_cap(ack_config):
    st = State(ack_config.state_db)
    w, gh, al = watcher(ack_config, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, ACK)

    w.evaluate(OWNER, REPO, NUMBER)

    assert "round 4 of a review loop (cap 4)" in al.enqueued[0]["directive"]
    assert "round 4 of 4" in activity_comments(gh)[0].body


def test_the_round_1_directive_records_the_effective_cap(config):
    """Doc drift: the review-task template documents a cap of 3, the daemon's
    default has been 10 since CR9 was retuned. Round 1 is the round that
    creates that description, so it carries the real number."""
    cfg = dataclasses.replace(config, round_cap=10)
    w, _, al = watcher(cfg, make_pr(), [])
    w.evaluate(OWNER, REPO, NUMBER)

    directive = al.enqueued[0]["directive"]
    assert "EFFECTIVE round cap is 10" in directive
    assert "record THAT number in the review task description" in directive
    assert "stale template default" in directive


def test_the_round_k_directive_records_the_effective_cap_too(config):
    cfg = dataclasses.replace(config, round_cap=10)
    w, _, al = watcher(cfg, make_pr(sha="def456"), [review("CHANGES_REQUESTED")])
    w.evaluate(OWNER, REPO, NUMBER)

    assert "EFFECTIVE round cap is 10" in al.enqueued[0]["directive"]


# -- config ---------------------------------------------------------------


def test_operators_default_to_an_empty_allowlist(tmp_path):
    assert Config.build(tmp_path, {}).operators == ()


def test_operators_are_read_from_the_config_file(tmp_path):
    cfg = Config.build(tmp_path, {"operators": ["rhdzmota", " ops-bot "]})
    assert cfg.operators == ("rhdzmota", "ops-bot")


@pytest.mark.parametrize("key,value", [
    ("operators", "rhdzmota"),
    ("repos", "acme/widgets"),
])
def test_a_string_list_key_is_rejected(tmp_path, key, value):
    """A bare string iterates into single CHARACTERS: an allowlist of
    one-character names that matches nothing, silently."""
    with pytest.raises(ValueError, match=f"{key} must be a list"):
        Config.build(tmp_path, {key: value})


def test_the_dry_run_pass_never_records_a_grant(ack_config):
    dry = dataclasses.replace(ack_config, dry_run=True)
    st = State(dry.state_db)
    w, gh, al = watcher(dry, make_pr(), capped_reviews(), state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    gh.seed_comment(OPERATOR, ACK)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED, "a dry run still SHOWS what the ack would buy"
    assert al.enqueued[0]["dry_run"] is True, "...without really enqueuing it"
    assert st.get_spawn(SLUG, NUMBER, 4) is None
    assert st.granted_rounds(SLUG, NUMBER) == 0, "and records nothing"


# -- reviewer identity: the round's verdict of record (issue #51) -----------
#
# The defect these pin: a round's verdict reached GitHub only as a COMMENT
# review by the IMPLEMENTER identity (studio #298/#302 round 1). Nothing then
# expressed approve/request_changes on the PR, the pending review request was
# never consumed, and the daemon re-verified a closed round every poll. The
# rule is now flat: a round is not complete until the verdict exists as a
# native review submitted by the configured reviewer identity.


def envelope_ahead(config, verdict, *, reviews=None, state=None):
    """A PR whose review task carries a round-1 verdict envelope that no
    countable reviewer review backs — the #298 shape."""
    return watcher(
        config,
        make_pr(),
        list(reviews or []),
        state=state,
        verdict=verdict,
        verdict_count=1,
    )


def test_an_envelope_with_no_native_review_is_posted_as_one(config, no_post_grace):
    w, gh, _ = envelope_ahead(config, VERDICT_REQUEST_CHANGES)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.POSTED
    assert d.round == 1
    assert len(gh.submitted) == 1
    assert gh.submitted[0]["event"] == "REQUEST_CHANGES"
    assert gh.submitted[0]["commit_id"] == "abc123", "pinned to the head it judged"
    assert verdict_marker(1) in gh.submitted[0]["body"]
    assert "TASK-500" in gh.submitted[0]["body"]


def test_an_approve_envelope_posts_a_native_approve(config, no_post_grace):
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.POSTED
    assert gh.submitted[0]["event"] == "APPROVE"


def test_an_approve_envelope_does_not_converge_before_its_native_review(config):
    """The ordering that matters most. An approve envelope with no native
    APPROVE behind it must NOT close the loop: doing so leaves the PR with no
    verdict of record and its review request dangling forever — #298 exactly."""
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST, "not CONVERGED"
    assert gh.submitted == [], "still inside the grace window"


def test_the_round_is_not_closed_while_the_post_is_owed(config, no_post_grace):
    """No next round, either: round 2 cannot be owed while round 1 has no
    verdict of record."""
    st = State(config.state_db)
    w, gh, al = envelope_ahead(config, VERDICT_REQUEST_CHANGES, state=st)
    gh.submit_error = CommandError(["gh"], 1, "422 Unprocessable Entity")

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST
    assert al.enqueued == [], "round 2 must not be queued over an unclosed round 1"
    assert st.get_spawn(SLUG, NUMBER, 2) is None


def test_the_grace_window_lets_the_session_post_its_own_review_first(config):
    """A reviewer session writes its envelope and submits its own review
    moments apart. A poll landing between the two must not race it."""
    w, gh, _ = envelope_ahead(config, VERDICT_REQUEST_CHANGES)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST
    assert "grace" in d.reason
    assert gh.submitted == []


def test_the_post_refuses_under_a_foreign_login(config, no_post_grace):
    """The assertion is the whole point: a review posted under the wrong
    identity is not the round's verdict and does not consume the request, so
    refusing to post is strictly better than posting."""
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE)
    gh.identity_error = IdentityMismatch("resolves to 'RHDZMOTA', not 'alissa-app'")

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST
    assert gh.submitted == [], "refused, not posted under the wrong login"


def test_a_missing_reviewer_token_is_a_failed_post_not_a_silent_fallback(
    config, no_post_grace
):
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE)
    gh.submit_error = ReviewerTokenUnset("REVLOOP_REVIEWER_GH_TOKEN is unset")

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST
    assert "NOT closed" in d.reason


def test_a_failed_post_is_retried_and_then_escalated(config, no_post_grace):
    st = State(config.state_db)
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.submit_error = CommandError(["gh"], 1, "401 Bad credentials")

    for _ in range(MAX_VERDICT_POST_ATTEMPTS):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.AWAITING_POST

    pages = [c for c in operator_comments(gh) if "cannot be closed" in c]
    assert len(pages) == 1, "paged once, loudly"
    assert "alissa-app" in pages[0]
    assert st.pinged(SLUG, NUMBER, verdict_post_kind(1))
    assert st.get_verdict_post(SLUG, NUMBER, 1)["posted_at"] is None

    # ...and it keeps retrying without re-paging: the round stays OPEN.
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.AWAITING_POST
    assert len([c for c in operator_comments(gh) if "cannot be closed" in c]) == 1


def test_a_landed_post_is_recorded_and_announced(config, no_post_grace):
    st = State(config.state_db)
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)

    w.evaluate(OWNER, REPO, NUMBER)

    row = st.get_verdict_post(SLUG, NUMBER, 1)
    assert row["posted_at"] is not None and row["review_url"]
    assert "verdict of record" in activity_comments(gh)[0].body


def test_the_dry_run_pass_never_submits_a_review(config, no_post_grace):
    dry = dataclasses.replace(config, dry_run=True)
    w, gh, _ = envelope_ahead(dry, VERDICT_APPROVE)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST
    assert gh.submitted == []


def test_a_session_review_and_the_native_post_are_one_round(config, no_post_grace):
    """Requirement 5. Once a session's own review carries the reviewer
    identity, its record and the daemon's native post both describe round 1 —
    and counting two would spend a cap slot on a round that never ran."""
    st = State(config.state_db)
    w, gh, al = watcher(
        config, make_pr(), [review()], state=st,
        verdict=VERDICT_REQUEST_CHANGES, verdict_count=2,
    )
    # Round 2's envelope landed with no native review; the daemon posts it.
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.POSTED

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.round == 3, "two rounds are done, not three"
    assert countable_rounds(gh.my_reviews(OWNER, REPO, NUMBER)) == 2


def test_a_marked_post_and_an_unmarked_one_for_the_same_round_count_once():
    marked = review(body="verdict\n" + verdict_marker(1))
    unmarked = review(body="the session's own write-up")
    assert countable_rounds([unmarked, marked]) == 1
    assert countable_rounds([unmarked]) == 1, "legacy PRs count exactly as before"
    assert countable_rounds([]) == 0


def test_an_unreadable_verdict_is_never_guessed_onto_the_pr(config, no_post_grace):
    """`request_changes` and `approve` are not interchangeable. With an
    envelope counted but no verdict word parseable, the round stays open."""
    w, gh, al = envelope_ahead(config, None)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST
    assert gh.submitted == []
    assert al.enqueued == []


def test_the_post_only_happens_for_the_reviewer_identity_gap(config, no_post_grace):
    """No gap, no post: the healthy path is untouched, and every round the
    session closed itself stays closed by its own review."""
    w, gh, al = watcher(config, make_pr(), [review()], verdict=VERDICT_REQUEST_CHANGES)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert gh.submitted == []
    assert d.action is Action.SPAWNED and d.round == 2


def test_the_grace_window_is_pinned():
    """Long enough to lose a race, short enough that a genuinely missing post
    heals within one operator's attention span."""
    assert 60 <= VERDICT_POST_GRACE_SECONDS <= 15 * 60


def test_the_snapshot_counts_the_new_stages(config, no_post_grace):
    st = State(config.state_db)
    w, _, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)

    w.poll_once()

    snap = st.read_snapshots(1)[0]
    assert snap["posted"] == 1
    assert snap["awaiting_post"] == 0
    assert snap["stages"][0]["stage"] == "posted"


# -- the CI checks gate on APPROVE verdicts (issue #58) ---------------------
#
# An APPROVE by the reviewer identity is the operator's cue to merge, so it has
# to mean reviewed AND green. studio #323: the head's `test` check went red at
# 15:27Z and the round approved at 18:50Z, because nothing consulted the rollup.


def failing_check(name="test", url="https://github.com/acme/widgets/runs/1"):
    return CheckContext(name=name, conclusion="failure", url=url)


def running_check(name="test"):
    return CheckContext(name=name, conclusion="")


def test_a_red_rollup_never_approves_and_names_the_failing_checks(config, no_post_grace):
    st = State(config.state_db)
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.default_rollup = rollup_of([failing_check(), CheckContext("lint", "success")])

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.POSTED, "the round still closes — it just does not approve"
    assert len(gh.submitted) == 1
    assert gh.submitted[0]["event"] == "REQUEST_CHANGES"
    body = gh.submitted[0]["body"]
    assert "`test`" in body and "runs/1" in body, "leads with the check and its run URL"
    assert body.index("test") < body.index("Review round"), "the lead comes first"
    assert "red" in d.reason


def test_a_red_rollup_verdict_does_not_converge_the_loop(config, no_post_grace):
    """The envelope still reads approve. Converging on it would withdraw the
    review request and take the PR out of the loop on a verdict the daemon
    deliberately refused to post as an APPROVE — with no later green round."""
    st = State(config.state_db)
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.default_rollup = rollup_of([failing_check()])

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.POSTED

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is not Action.CONVERGED
    assert gh.removed == [], "the review request is not withdrawn"


def test_a_later_green_round_approves_the_same_code(config, no_post_grace):
    """The whole point of not converging: CI goes green, the next round runs,
    and it approves — no new commit required."""
    st = State(config.state_db)
    w, gh, al = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.default_rollup = rollup_of([failing_check()])
    w.evaluate(OWNER, REPO, NUMBER)  # round 1: red, REQUEST_CHANGES

    gh.default_rollup = rollup_of([CheckContext("test", "success")])
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED, "round 2 is owed"
    al.verdict_count = 2  # round 2's envelope lands, approve again

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.POSTED
    assert gh.submitted[-1]["event"] == "APPROVE"


def test_skipped_and_neutral_contexts_never_block_an_approve(config, no_post_grace):
    """Path-filtered matrix jobs (studio #323's api/cli/mcp/plugin) report
    `skipped`, and blocking on them would block every approve on every repo
    that filters by path."""
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE)
    gh.default_rollup = rollup_of(
        [
            CheckContext("test", "success"),
            CheckContext("api", "skipped"),
            CheckContext("cli", "neutral"),
        ]
    )

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.POSTED
    assert gh.submitted[0]["event"] == "APPROVE"
    assert "CI gate" not in gh.submitted[0]["body"], "the happy path is unchanged"


def test_a_commit_with_no_checks_at_all_approves_exactly_as_before(config, no_post_grace):
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE)
    gh.default_rollup = rollup_of([])

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.POSTED
    assert gh.submitted[0]["event"] == "APPROVE"


def test_the_rollup_is_read_for_the_head_the_verdict_is_pinned_to(config, no_post_grace):
    """Never "the PR's checks": approving commit A on commit B's rollup is the
    same error as stamping an old verdict onto a new head."""
    st = State(config.state_db)
    seed_round(st, "old111")  # the round was queued against an older head
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.rollups = {"old111": rollup_of([failing_check("test")])}  # judged head: red
    gh.default_rollup = rollup_of([CheckContext("test", "success")])  # current: green

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert gh.rollup_reads == ["old111"]
    assert gh.submitted[0]["event"] == "REQUEST_CHANGES"
    assert d.action is Action.POSTED


def test_a_pending_rollup_holds_the_round_open_without_posting(config, no_post_grace):
    st = State(config.state_db)
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.default_rollup = rollup_of([running_check(), CheckContext("lint", "success")])

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST
    assert gh.submitted == [], "no verdict at all while the checks run"
    assert "pending" in d.reason
    assert st.get_verdict_post(SLUG, NUMBER, 1)["checks_held_at"] is not None


def test_a_held_round_posts_normally_once_the_checks_go_green(config, no_post_grace):
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE)
    gh.default_rollup = rollup_of([running_check()])
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.AWAITING_POST

    gh.default_rollup = rollup_of([CheckContext("test", "success")])
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.POSTED
    assert gh.submitted[0]["event"] == "APPROVE"


def test_a_held_round_notes_the_wait_once_and_posts_no_comment(config, no_post_grace):
    """At most one waiting note per round, and it lands in the mechanical
    activity comment — a held round costs zero new comments."""
    st = State(config.state_db)
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.default_rollup = rollup_of([running_check()])

    for _ in range(4):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.AWAITING_POST

    assert operator_comments(gh) == [], "no waiting comment of its own"
    notes = [
        line
        for c in activity_comments(gh)
        for line in c.body.splitlines()
        if "verdict held" in line
    ]
    assert len(notes) == 1, notes
    assert st.pinged(SLUG, NUMBER, checks_hold_kind(1, "abc123"))


def test_a_rollup_that_never_concludes_degrades_to_a_comment(config, no_post_grace):
    """Past the bound the verdict is recorded, but never as an approve: the
    round leaves a readable record and stays re-enterable."""
    st = State(config.state_db)
    impatient = dataclasses.replace(config, checks_wait_seconds=0)
    w, gh, _ = envelope_ahead(impatient, VERDICT_APPROVE, state=st)
    gh.default_rollup = rollup_of([running_check("test")])

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.POSTED
    assert gh.submitted[0]["event"] == "COMMENT"
    assert "never concluded" in gh.submitted[0]["body"]
    assert "`test`" in gh.submitted[0]["body"]

    # ...and the comment verdict converges nothing: round 2 is owed.
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED


def test_an_unreadable_rollup_is_never_treated_as_green(config, no_post_grace):
    impatient = dataclasses.replace(config, checks_wait_seconds=0)
    w, gh, _ = envelope_ahead(impatient, VERDICT_APPROVE)
    gh.default_rollup = CheckRollup(CHECKS_UNKNOWN, unreadable="CommandError: 403")

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert gh.submitted[0]["event"] == "COMMENT"
    assert "403" in gh.submitted[0]["body"], "says why it could not be read"
    assert d.action is Action.POSTED


def test_a_missing_commit_is_left_to_the_pinned_head_handling(config, no_post_grace):
    """A commit that is gone from the repo is not a CI answer. Holding on it
    would stall a round the force-push path already knows how to release."""
    st = State(config.state_db)
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.default_rollup = CheckRollup(
        CHECKS_UNKNOWN, unreadable="CommandError: 404 Not Found", commit_missing=True
    )
    gh.commits = ["def456"]  # the judged head is not in the PR any more
    gh.submit_error = CommandError(["gh"], 1, "422 Unprocessable Entity: commit_id")

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.ABANDONED
    assert st.get_verdict_post(SLUG, NUMBER, 1)["checks_held_at"] is None


def test_a_request_changes_verdict_is_never_gated_on_checks(config, no_post_grace):
    """A request_changes on a red head is already a 'not ready' signal, and CI
    has nothing to add to it — so the gate does not even read the rollup."""
    w, gh, _ = envelope_ahead(config, VERDICT_REQUEST_CHANGES)
    gh.default_rollup = rollup_of([failing_check()])

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.POSTED
    assert gh.submitted[0]["event"] == "REQUEST_CHANGES"
    assert gh.rollup_reads == [], "no rollup read on the ungated path"


def test_the_gate_never_touches_labels(config, no_post_grace):
    """`alissa:maintain` and every other cross-daemon trigger stays an
    operator/devloop concern. The client has no label call at all — this pins
    that the gate did not grow one."""
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE)
    gh.default_rollup = rollup_of([failing_check()])

    w.evaluate(OWNER, REPO, NUMBER)

    assert not hasattr(gh, "labels"), "the fake never needed a label store"
    assert not any(
        hasattr(GitHub, name) for name in ("add_label", "set_labels", "remove_label")
    ), "the GitHub client has no label-mutating method"


def test_the_dry_run_pass_reports_the_gate_without_acting(config, no_post_grace):
    st = State(config.state_db)
    dry = dataclasses.replace(config, dry_run=True)
    w, gh, _ = envelope_ahead(dry, VERDICT_APPROVE, state=st)
    gh.default_rollup = rollup_of([failing_check()])

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST
    assert gh.submitted == []
    assert "red" in d.reason, "dry-run diagnoses the gate rather than staying silent"
    assert st.get_verdict_post(SLUG, NUMBER, 1) is None, "and writes no ledger row"


def test_the_wait_bound_default_is_pinned():
    """Longer than any check suite in this fleet, short enough that a stuck
    rollup surfaces within the working hour."""
    assert 10 * 60 <= DEFAULT_CHECKS_WAIT_SECONDS <= 60 * 60
    assert Config(workspace_root=".").checks_wait_seconds == DEFAULT_CHECKS_WAIT_SECONDS


def test_a_negative_wait_bound_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="checks_wait_seconds"):
        Config.build(tmp_path, {"checks_wait_seconds": -1})


# -- rollup reading: what counts as red, running, green ---------------------


def test_check_runs_and_commit_statuses_are_both_read(monkeypatch):
    gh = GitHub("alissa-app")
    calls: list[str] = []

    def api(*args, **kwargs):
        path = [a for a in args if a.startswith("repos/")][0]
        calls.append(path)
        if path.endswith("check-runs"):
            return {
                "check_runs": [
                    {"name": "test", "status": "completed", "conclusion": "SUCCESS"},
                    {"name": "api", "status": "completed", "conclusion": "skipped"},
                ]
            }
        return {
            "state": "failure",
            "total_count": 1,
            "statuses": [
                {"context": "buildkite", "state": "failure", "target_url": "http://ci"}
            ],
        }

    gh._api = api
    rollup = gh.check_rollup(OWNER, REPO, "abc123")

    assert [c.split("/")[-1] for c in calls] == ["check-runs", "status"]
    assert rollup.state == CHECKS_RED
    assert [c.name for c in rollup.failing] == ["buildkite"]
    assert rollup.total == 3


def test_an_empty_combined_status_is_not_read_as_pending(monkeypatch):
    """The combined-status endpoint answers `state: pending` for a commit with
    no statuses at all. Reading that as "something is running" would hold every
    approve on every Actions-only repo until the bound."""
    gh = GitHub("alissa-app")

    def api(*args, **kwargs):
        path = [a for a in args if a.startswith("repos/")][0]
        if path.endswith("check-runs"):
            return {
                "check_runs": [
                    {"name": "test", "status": "completed", "conclusion": "success"}
                ]
            }
        return {"state": "pending", "total_count": 0, "statuses": []}

    gh._api = api

    assert gh.check_rollup(OWNER, REPO, "abc123").state == CHECKS_GREEN


def test_an_unfinished_run_carries_no_conclusion_however_it_is_labelled():
    """queued / in_progress / waiting / whatever GitHub adds next: not
    completed means not a verdict."""
    for status in ("queued", "in_progress", "waiting", "requested"):
        gh = GitHub("alissa-app")

        def api(*args, status=status, **kwargs):
            path = [a for a in args if a.startswith("repos/")][0]
            if path.endswith("check-runs"):
                return {
                    "check_runs": [
                        {"name": "test", "status": status, "conclusion": None}
                    ]
                }
            return {"statuses": []}

        gh._api = api
        assert gh.check_rollup(OWNER, REPO, "abc123").state == CHECKS_PENDING


def test_an_unrecognised_conclusion_blocks_rather_than_approves():
    """Conservative on purpose: a gate whose job is to not approve a head it
    cannot vouch for must not read an unknown conclusion as success."""
    assert rollup_of([CheckContext("test", "some_future_conclusion")]).state == CHECKS_RED
    assert rollup_of([CheckContext("test", "cancelled")]).state == CHECKS_RED
    assert rollup_of([CheckContext("test", "timed_out")]).state == CHECKS_RED


def test_a_failure_outranks_a_still_running_check():
    """One job already red and three still going cannot be approved at all, and
    saying so now beats saying it half an hour later."""
    rollup = rollup_of([failing_check(), running_check("slow")])
    assert rollup.state == CHECKS_RED
    assert [c.name for c in rollup.failing] == ["test"]


def test_an_unreadable_rollup_reports_why_and_never_raises():
    gh = GitHub("alissa-app")
    gh._api = lambda *a, **k: (_ for _ in ()).throw(
        CommandError(["gh"], 1, "403 Resource not accessible")
    )

    rollup = gh.check_rollup(OWNER, REPO, "abc123")

    assert rollup.state == CHECKS_UNKNOWN
    assert "403" in rollup.unreadable
    assert not rollup.commit_missing


def test_a_404_rollup_says_the_commit_is_missing():
    gh = GitHub("alissa-app")
    gh._api = lambda *a, **k: (_ for _ in ()).throw(
        CommandError(["gh"], 1, "gh: Not Found (HTTP 404)")
    )

    assert gh.check_rollup(OWNER, REPO, "abc123").commit_missing


def test_a_rate_limited_rollup_still_reaches_the_backoff():
    gh = GitHub("alissa-app")
    gh._api = lambda *a, **k: (_ for _ in ()).throw(RateLimited("rate limit"))

    with pytest.raises(RateLimited):
        gh.check_rollup(OWNER, REPO, "abc123")


def test_too_many_check_runs_is_unknown_not_green():
    gh = GitHub("alissa-app")
    gh._api = lambda *a, **k: {
        "check_runs": [
            {"name": f"job{i}", "status": "completed", "conclusion": "success"}
            for i in range(PER_PAGE)
        ]
    }

    rollup = gh.check_rollup(OWNER, REPO, "abc123")

    assert rollup.state == CHECKS_UNKNOWN
    assert str(CHECK_RUN_PAGE_LIMIT * PER_PAGE) in rollup.unreadable


def test_a_comment_review_is_a_submitted_round_but_not_an_approval():
    """Why a degraded verdict is safe to mark: it closes its round (so the
    daemon does not post twice) and expresses no approval (so nothing
    converges on it)."""
    commented = Review(
        author="alissa-app",
        state="COMMENTED",
        commit_id="abc123",
        submitted_at="2026-07-20T01:00:00Z",
        url="u",
        body="verdict\n" + verdict_marker(1),
    )
    assert countable_rounds([commented]) == 1
    assert commented.verdict_round == 1


# -- credential routing: no inherited container default --------------------


def test_the_gh_env_carries_the_reviewer_token_and_nothing_inherited(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "the-implementer-token")
    monkeypatch.setenv("GITHUB_TOKEN", "the-implementer-token")
    monkeypatch.setenv("REV_TOKEN", "the-reviewer-token")

    env = GitHub("alissa-app", token_env="REV_TOKEN")._env()

    assert env["GH_TOKEN"] == "the-reviewer-token"
    assert env["GITHUB_TOKEN"] == "the-reviewer-token", "both, or gh picks the other"


def test_an_unset_reviewer_token_refuses_rather_than_falling_back(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "the-implementer-token")
    monkeypatch.delenv("REV_TOKEN", raising=False)

    with pytest.raises(ReviewerTokenUnset, match="REV_TOKEN"):
        GitHub("alissa-app", token_env="REV_TOKEN")._env()


def test_no_token_env_inherits_exactly_as_before():
    assert GitHub("alissa-app")._env() is None


def test_the_posting_gate_reads_the_login_fresh(monkeypatch):
    """Asserted once at boot, re-read at post time: the failure being defended
    against is a credential that was right at boot and is wrong now."""
    gh = GitHub("alissa-app")
    logins = iter(["alissa-app", "RHDZMOTA"])
    monkeypatch.setattr(GitHub, "token_login", lambda self: next(logins))

    assert gh.verify_identity() == "alissa-app"
    with pytest.raises(IdentityMismatch, match="RHDZMOTA"):
        gh.assert_review_identity()


def test_submit_review_refuses_an_event_that_is_not_a_verdict():
    """APPROVE, REQUEST_CHANGES and (for a checks-gated approve) COMMENT are
    the whole vocabulary; anything else is a caller bug, refused before the
    identity is even asserted."""
    with pytest.raises(ValueError, match="event must be one of"):
        GitHub("alissa-app").submit_review(
            OWNER, REPO, NUMBER, event="DISMISS", body="x"
        )


def test_the_directive_names_the_credential_variable_by_name(config):
    cfg = dataclasses.replace(config, reviewer_token_env="REVLOOP_REVIEWER_GH_TOKEN")
    w, _, al = watcher(cfg, make_pr(), [])

    w.evaluate(OWNER, REPO, NUMBER)

    directive = al.enqueued[0]["directive"]
    assert 'GH_TOKEN="$REVLOOP_REVIEWER_GH_TOKEN"' in directive
    assert "verdict of record" in directive


def test_the_directive_omits_the_clause_with_nothing_to_name(config):
    w, _, al = watcher(config, make_pr(), [])
    w.evaluate(OWNER, REPO, NUMBER)
    assert "CREDENTIAL" not in al.enqueued[0]["directive"]


def test_reviewer_token_env_must_be_a_name_not_a_token(tmp_path):
    with pytest.raises(ValueError, match="variable NAME"):
        Config.build(tmp_path, {"reviewer_token_env": "ghp_liveTokenPastedHere!"})
    assert Config.build(
        tmp_path, {"reviewer_token_env": "REVLOOP_REVIEWER_GH_TOKEN"}
    ).reviewer_token_env == "REVLOOP_REVIEWER_GH_TOKEN"


def test_preflight_logs_the_resolved_login_and_warns_on_an_inherited_credential(
    config, caplog
):
    w, gh, _ = watcher(config, make_pr(), [])
    gh.verify_identity = lambda: "alissa-app"
    gh.login = "alissa-app"

    with caplog.at_level(logging.INFO):
        warnings = w.preflight()

    assert "reviewing as GitHub user alissa-app" in caplog.text
    assert any("reviewer_token_env" in warning for warning in warnings)


def test_preflight_is_quiet_once_the_credential_is_routed(config):
    cfg = dataclasses.replace(config, reviewer_token_env="REV_TOKEN")
    w, gh, _ = watcher(cfg, make_pr(), [])
    gh.verify_identity = lambda: "alissa-app"

    assert not any("reviewer_token_env" in w_ for w_ in w.preflight())


# -- round-1 findings: the head a native verdict is ABOUT --------------------


def seed_round(st, head, round_=1):
    """The spawn ledger row the daemon writes for every round it queues — the
    record of which head the round was handed."""
    st.record_spawn(
        repo=SLUG, number=NUMBER, round_=round_, head_sha=head,
        session=f"review-widgets-pr{NUMBER}-r{round_}-seed", task_ref="TASK-500",
    )


def test_a_verdict_is_posted_against_the_head_its_round_judged(config, no_post_grace):
    """[blocker, round 1] The implementer pushes between the envelope and the
    daemon's post. Stamping the verdict onto the NEW head would restamp an
    approval onto code no reviewer has seen — the #227 latch, rebuilt inside
    the daemon's own post."""
    st = State(config.state_db)
    seed_round(st, "abc123")                       # round 1 judged abc123
    w, gh, _ = watcher(
        config, make_pr(sha="newhead"), [], state=st,   # ...the head is now newhead
        verdict=VERDICT_APPROVE, verdict_count=1,
    )

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.POSTED
    assert gh.submitted[0]["commit_id"] == "abc123", "pinned to the head it judged"
    assert "judged `abc123`" in gh.submitted[0]["body"], "and it says so in the open"


def test_a_verdict_on_an_older_head_does_not_converge_the_loop(config, no_post_grace):
    """The consequence that makes it a blocker: the very next pass must owe
    round 2, not read the daemon's own post as a current approval."""
    st = State(config.state_db)
    seed_round(st, "abc123")
    w, gh, al = watcher(
        config, make_pr(sha="newhead"), [], state=st,
        verdict=VERDICT_APPROVE, verdict_count=1,
    )
    w.evaluate(OWNER, REPO, NUMBER)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED, "an approve for old code owes another round"
    assert d.round == 2


def test_an_unmoved_head_still_converges(config, no_post_grace):
    """...and the ordinary case is untouched: nothing was pushed, so the
    daemon's APPROVE covers the current head and the loop is done."""
    st = State(config.state_db)
    seed_round(st, "abc123")
    w, gh, _ = watcher(
        config, make_pr(sha="abc123"), [], state=st,
        verdict=VERDICT_APPROVE, verdict_count=1,
    )
    w.evaluate(OWNER, REPO, NUMBER)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.CONVERGED
    assert "head has moved" not in gh.submitted[0]["body"]


def test_the_judged_head_is_frozen_at_first_observation(config):
    """With no ledger row the daemon cannot know the judged head, so it freezes
    the head it first saw the gap at. A push during the grace window then
    cannot drag the verdict forward onto it."""
    st = State(config.state_db)
    pr = make_pr(sha="firstseen")
    w, gh, _ = watcher(
        config, pr, [], state=st, verdict=VERDICT_APPROVE, verdict_count=1
    )
    w.evaluate(OWNER, REPO, NUMBER)                      # inside grace; freezes it
    assert st.get_verdict_post(SLUG, NUMBER, 1)["head_sha"] == "firstseen"

    gh._pr = make_pr(sha="pushed-since")                 # implementer pushes
    w.state.note_verdict_post_owed(SLUG, NUMBER, 1, "pushed-since")  # no-op by design
    assert st.get_verdict_post(SLUG, NUMBER, 1)["head_sha"] == "firstseen"


# -- round-1 findings: a 403 must not masquerade as a rate limit -------------


def _gh_raising(monkeypatch, stderr):
    """A real GitHub client whose `gh api` fails with `stderr` — so the mapping
    under test is `GitHub._api`'s, not the fake's."""
    gh = GitHub("alissa-app")
    monkeypatch.setattr(GitHub, "assert_review_identity", lambda self: "alissa-app")

    def boom(argv, *, timeout=60, env=None, stdin=None):
        raise CommandError(argv, 1, stderr)

    monkeypatch.setattr("alissa.tools.github.revloop.ghclient.run_json", boom)
    return gh


def test_an_authorization_403_on_the_review_post_is_not_a_rate_limit(monkeypatch):
    """[major, round 1] Collapsed into RateLimited it aborts the whole poll
    pass into a 900s backoff, and the round's retry-and-page path never runs —
    for exactly the failure the PR names as its main unverified risk."""
    gh = _gh_raising(
        monkeypatch, "Resource not accessible by personal access token (HTTP 403)"
    )
    with pytest.raises(CommandError):
        gh.submit_review(OWNER, REPO, NUMBER, event="APPROVE", body="x")


@pytest.mark.parametrize("stderr", [
    "API rate limit exceeded (HTTP 403)",
    "You have exceeded a secondary rate limit (HTTP 403)",
    "was submitted too quickly (HTTP 429)",
])
def test_a_genuine_throttle_on_the_review_post_still_backs_off(monkeypatch, stderr):
    gh = _gh_raising(monkeypatch, stderr)
    with pytest.raises(RateLimited):
        gh.submit_review(OWNER, REPO, NUMBER, event="APPROVE", body="x")


def test_the_read_path_still_treats_a_bare_403_as_throttling(monkeypatch):
    """Unchanged for reads: backing off is the right response there, and a
    skipped poll costs nothing."""
    gh = _gh_raising(monkeypatch, "HTTP 403: Forbidden")
    with pytest.raises(RateLimited):
        gh.reviews(OWNER, REPO, NUMBER)


def test_an_unpostable_repo_holds_its_round_open_instead_of_stalling_the_pass(
    config, no_post_grace, monkeypatch
):
    """End to end: the 403 lands on the retry path, so `poll_once` completes
    and every other watched PR in the pass still gets decided."""
    st = State(config.state_db)
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.submit_error = CommandError(["gh"], 1, "Resource not accessible (HTTP 403)")

    results = w.poll_once()

    assert [d.action for _, d in results] == [Action.AWAITING_POST]
    assert st.get_verdict_post(SLUG, NUMBER, 1)["attempts"] == 1


# -- round-1 findings: the retry backoff ------------------------------------


def test_the_retry_delay_grows_per_attempt_and_is_capped():
    assert loop_module._post_delay_after(1) == 120
    assert loop_module._post_delay_after(4) == 960
    assert loop_module._post_delay_after(99) == loop_module.MAX_VERDICT_POST_BACKOFF_SECONDS


def age_verdict_post(st, seconds, *, attempts=None):
    """Age a verdict_posts row: both clocks, so an aged row is aged for the
    grace window AND for the retry delay."""
    stamp = int(time.time()) - int(seconds)
    st._db.execute(
        "UPDATE verdict_posts SET first_seen_at=?, last_attempt_at=?", (stamp, stamp)
    )
    if attempts is not None:
        st._db.execute("UPDATE verdict_posts SET attempts=?", (attempts,))
    st._db.commit()


def test_a_failing_post_stops_retrying_every_poll(config, monkeypatch):
    """Deployed poll interval is 30s: unbounded retry is ~2,880 review POSTs a
    day per stuck PR. The round still never closes — only the cadence bounds."""
    st = State(config.state_db)
    monkeypatch.setattr(loop_module, "VERDICT_POST_GRACE_SECONDS", 0)
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.submit_error = CommandError(["gh"], 1, "401 Bad credentials")

    first = w.evaluate(OWNER, REPO, NUMBER)      # attempt 0 runs (grace is 0)
    second = w.evaluate(OWNER, REPO, NUMBER)     # attempt 1 is deferred

    assert first.action is second.action is Action.AWAITING_POST
    assert st.get_verdict_post(SLUG, NUMBER, 1)["attempts"] == 1, "no second POST"
    assert "backing off" in second.reason
    assert st.get_verdict_post(SLUG, NUMBER, 1)["posted_at"] is None


@pytest.mark.parametrize("elapsed", [2 * 3600, 24 * 3600, 10 * 24 * 3600])
def test_the_backoff_still_holds_long_past_its_cap(config, monkeypatch, elapsed):
    """[minor, reopened in round 2] A capped deadline measured from a FIXED
    origin stops bounding anything once the row outlives the cap — the hot loop
    returns, an hour late. The delay has to be per-attempt."""
    st = State(config.state_db)
    monkeypatch.setattr(loop_module, "VERDICT_POST_GRACE_SECONDS", 0)
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, state=st)
    gh.submit_error = CommandError(["gh"], 1, "401 Bad credentials")
    w.evaluate(OWNER, REPO, NUMBER)                       # one real attempt
    age_verdict_post(st, elapsed, attempts=20)            # ...now long stale

    due = w.evaluate(OWNER, REPO, NUMBER)                 # one attempt is due
    assert st.get_verdict_post(SLUG, NUMBER, 1)["attempts"] == 21

    # ...and the NEXT poll, 30s later, is deferred again — that is the whole
    # property. With a capped deadline off a fixed origin, every poll from here
    # attempted the post forever.
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert due.action is d.action is Action.AWAITING_POST
    assert st.get_verdict_post(SLUG, NUMBER, 1)["attempts"] == 21, "no hot loop"
    assert "backing off" in d.reason


# -- round-1 findings: a later session review closes its own round -----------


def test_a_correctly_posted_later_round_is_not_posted_over(config, no_post_grace):
    """[minor, round 1] Once round 1 is daemon-closed, a round-2 session that
    submits its own review correctly must not get a duplicate verdict stacked
    on top of it — every round, forever."""
    marked = review(body="round 1\n" + verdict_marker(1), at="2026-07-18T10:00:00Z")
    session_review = review(body="round 2 findings", at="2026-07-19T10:00:00Z")
    w, gh, al = watcher(
        config, make_pr(), [marked, session_review],
        verdict=VERDICT_REQUEST_CHANGES, verdict_count=2,
    )

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert gh.submitted == [], "round 2 already has a reviewer review"
    assert d.action is Action.SPAWNED and d.round == 3


def test_an_unmarked_review_older_than_the_marked_one_still_counts_once():
    """...while the same-round pair it was protecting against is unchanged: an
    unmarked review at or before the marked post is that round's, not a new one."""
    marked = review(body="v\n" + verdict_marker(1), at="2026-07-18T10:00:00Z")
    same_round = review(body="the session's own write-up", at="2026-07-18T09:00:00Z")
    assert countable_rounds([same_round, marked]) == 1


# -- round-1 findings: the config validator and its message ------------------


@pytest.mark.parametrize("token", [
    "ghp_" + "a" * 36,
    "gho_" + "b" * 36,
    "ghs_" + "c" * 36,
    "github_pat_" + "d" * 60,
    "R" * 80,
])
def test_credential_shaped_values_are_rejected_as_a_token_env_name(tmp_path, token):
    """Every real GitHub token is a valid POSIX identifier, so a name regex
    alone lets the one mistake this exists to catch straight through."""
    with pytest.raises(ValueError, match="not a value or a token"):
        Config.build(tmp_path, {"reviewer_token_env": token})


@pytest.mark.parametrize("secret", [
    "ghp-uTCXx2m1YGfWNid84iK2xxxxxxxxxxxxxxxx",
    "sk-proj-abc123def456ghi789",
    "xoxb-1234-abcd",
    "glpat-xxxxxxxxxxxxxxxxxxxx",
])
def test_the_rejection_never_reprints_the_rejected_value(tmp_path, secret):
    """`__main__` prints this as `config error: …` — into the container log.
    Punctuation-bearing secrets match no GitHub token shape, so the no-echo
    guarantee cannot rest on recognising them."""
    with pytest.raises(ValueError) as exc:
        Config.build(tmp_path, {"reviewer_token_env": secret})

    assert secret not in str(exc.value)
    assert f"{len(secret)}-character value" in str(exc.value)


def test_an_ordinary_variable_name_is_still_accepted(tmp_path):
    for name in ("REVLOOP_REVIEWER_GH_TOKEN", "_rev", "GH_TOKEN_REV2"):
        assert Config.build(
            tmp_path, {"reviewer_token_env": name}
        ).reviewer_token_env == name


# -- round-1 findings: the session must verify before it writes --------------


def test_the_credential_clause_tells_the_session_to_verify_first(config):
    """The daemon populates its own environment, not the worker's. An
    unexported variable expands to "", and `gh` reads an empty GH_TOKEN as no
    token — falling back to the container default, the identity the clause
    exists to avoid."""
    cfg = dataclasses.replace(config, reviewer_token_env="REV_TOKEN")
    w, _, al = watcher(cfg, make_pr(), [])

    w.evaluate(OWNER, REPO, NUMBER)

    directive = al.enqueued[0]["directive"]
    assert "non-empty" in directive
    assert 'GH_TOKEN="$REV_TOKEN" gh api user --jq .login' in directive
    assert "alissa-app" in directive, "the login to compare against"
    assert "do NOT post the review at all" in directive


# -- round-2 findings: a verdict pinned to a commit that is gone -------------
#
# Pinning the verdict to the head its round judged (round 1's blocker fix) made
# a new failure reachable: a force-push removes that commit, GitHub rejects the
# review, and no retry can ever succeed. Without an exit the round stays open,
# round k+1 is never spawned, and the PR leaves the loop until a human edits
# sqlite — a worse outcome than any verdict-of-record concern here.

REJECTED_COMMIT = CommandError(
    ["gh"], 1, "HTTP 422: commit_id is not part of the pull request"
)


def force_pushed(config, state):
    """Round 1 judged `abc123`; the implementer rebased and the PR now carries
    `rebased` alone."""
    seed_round(state, "abc123")
    w, gh, al = watcher(
        config, make_pr(sha="rebased"), [], state=state,
        verdict=VERDICT_APPROVE, verdict_count=1,
    )
    gh.commits = ["rebased"]                 # abc123 is gone
    gh.submit_error = REJECTED_COMMIT
    return w, gh, al


def test_an_unpostable_pinned_verdict_is_abandoned_not_retried_forever(
    config, no_post_grace
):
    st = State(config.state_db)
    w, gh, _ = force_pushed(config, st)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.ABANDONED, "not AWAITING_POST — it will never retry"
    assert "abandoned" in d.reason
    assert st.verdict_post_abandoned(SLUG, NUMBER, 1)
    assert "abandoned" in activity_comments(gh)[0].body, "never silent"


def test_an_abandoned_round_releases_the_loop(config, no_post_grace):
    """The point of abandoning: the PR rejoins the loop instead of stalling out
    of it, and the fresh round runs against the NEW head."""
    st = State(config.state_db)
    w, gh, al = force_pushed(config, st)
    w.evaluate(OWNER, REPO, NUMBER)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED and d.round == 2
    assert gh.submitted == [], "and never by posting against the new head"
    assert st.get_spawn(SLUG, NUMBER, 2)["head_sha"] == "rebased"


def test_a_rejection_is_not_abandoned_while_the_commit_is_still_there(
    config, no_post_grace
):
    """The 422 only starts the question; the PR's commit list answers it. A
    rejection for any other reason stays on the ordinary retry path —
    abandoning wrongly discards a real verdict."""
    st = State(config.state_db)
    seed_round(st, "abc123")
    w, gh, _ = watcher(
        config, make_pr(sha="abc123"), [], state=st,
        verdict=VERDICT_APPROVE, verdict_count=1,
    )
    gh.commits = ["abc123"]                  # the pin is fine
    gh.submit_error = CommandError(["gh"], 1, "HTTP 422: Unprocessable Entity")

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert not st.verdict_post_abandoned(SLUG, NUMBER, 1)
    assert st.get_verdict_post(SLUG, NUMBER, 1)["attempts"] == 1
    assert "NOT closed" in d.reason


def test_an_unreadable_commit_list_never_abandons(config, no_post_grace):
    """Conservative on missing evidence: retry, do not discard."""
    st = State(config.state_db)
    w, gh, _ = force_pushed(config, st)
    gh.commits_error = CommandError(["gh"], 1, "network is unreachable")

    w.evaluate(OWNER, REPO, NUMBER)

    assert not st.verdict_post_abandoned(SLUG, NUMBER, 1)


def test_a_non_commit_failure_never_probes_the_commit_list(config, no_post_grace):
    """A 401 is not about the pin, so it must not pay for a commit fetch."""
    st = State(config.state_db)
    w, gh, _ = force_pushed(config, st)
    gh.submit_error = CommandError(["gh"], 1, "401 Bad credentials")
    gh.commits_error = AssertionError("the commit list must not be read here")

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST
    assert not st.verdict_post_abandoned(SLUG, NUMBER, 1)


def test_a_truncated_commit_listing_cannot_prove_absence():
    """`pull_request_commits` raises rather than returning a short list: a SHA
    missing from a truncated read is not a SHA that is gone."""
    from alissa.tools.github.revloop.ghclient import TruncatedListing
    gh = GitHub("alissa-app")
    gh._api = lambda *a, **k: [{"sha": f"c{i}"} for i in range(PER_PAGE)]  # always full
    with pytest.raises(TruncatedListing):
        gh.pull_request_commits(OWNER, REPO, NUMBER)


# -- round-2 findings: the credential guard and its message ------------------


@pytest.mark.parametrize("secret", [
    "f1e2d3c4b5a67890abcdef0123456789abcdef01",   # legacy 40-char hex token
    "a" * 40,
    "s3cretValueNoPunctuation",                    # generic alnum API key
])
def test_a_punctuation_free_secret_is_rejected_too(tmp_path, secret):
    """Length alone is not the discriminator — an undifferentiated alphanumeric
    run is. Every plausible NAME carries an underscore or is short."""
    with pytest.raises(ValueError, match="not a value or a token"):
        Config.build(tmp_path, {"reviewer_token_env": secret})


def test_a_typo_is_diagnosed_by_character_without_echoing_the_value(tmp_path):
    """The operator needs to know WHICH characters were rejected, not to see
    the string back — that is what fixes the typo at a glance while keeping the
    no-echo guarantee for a value that happens to be a secret."""
    with pytest.raises(ValueError) as exc:
        Config.build(tmp_path, {"reviewer_token_env": "REVLOOP-REVIEWER-GH-TOKEN"})

    message = str(exc.value)
    assert "REVLOOP-REVIEWER-GH-TOKEN" not in message
    assert "'-' at 7, 16, 19" in message, "the offending characters and where"
    assert "rotate it" not in message, "not a credential, so no rotate advice"


def test_a_credential_is_still_redacted(tmp_path):
    secret = "ghp_" + "a" * 36
    with pytest.raises(ValueError) as exc:
        Config.build(tmp_path, {"reviewer_token_env": secret})

    assert secret not in str(exc.value)
    assert "rotate it" in str(exc.value)


# -- round-3 findings (follow-up to PR #52) ---------------------------------


def test_an_abandonment_does_not_leave_the_next_round_posted_twice(
    config, no_post_grace
):
    """[minor, round 3] An abandoned round's envelope stays on the task forever
    while its review record never exists, so the two counts are permanently off
    by one. Uncorrected, that hole reads on the NEXT round as "no native
    verdict" and stacks a duplicate APPROVE on a session that closed its own
    round correctly — the outcome `countable_rounds` calls the worse one."""
    st = State(config.state_db)
    w, gh, al = force_pushed(config, st)
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.ABANDONED
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED   # round 2

    # Round 2's session submits its own review, correctly, at the new head.
    gh.submit_error = None
    gh._reviews.append(review(sha="rebased", at="2026-07-21T10:00:00Z"))
    al.verdict_count = 2

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert gh.submitted == [], "round 2 already has its reviewer review"
    assert d.action is not Action.POSTED


def test_an_abandonment_still_lets_the_daemon_close_a_later_round(
    config, no_post_grace
):
    """...and the discount must not disable the post path outright: a round
    after an abandonment whose session did NOT submit still gets its verdict."""
    st = State(config.state_db)
    w, gh, al = force_pushed(config, st)
    w.evaluate(OWNER, REPO, NUMBER)                       # round 1 abandoned
    w.evaluate(OWNER, REPO, NUMBER)                       # round 2 spawned
    gh.submit_error = None
    al.verdict_count = 2                                  # round 2's envelope, no review

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.POSTED and d.round == 2
    assert gh.submitted[0]["event"] == "APPROVE"


def test_the_abandoned_round_is_countable_in_the_snapshot(config, no_post_grace):
    """The action is what the snapshot aggregates and the console renders; a
    round that gave up must not read as one that is still retrying."""
    st = State(config.state_db)
    w, _, _ = force_pushed(config, st)

    w.poll_once()

    snap = st.read_snapshots(1)[0]
    assert snap["abandoned"] == 1
    assert snap["awaiting_post"] == 0
    assert snap["stages"][0]["stage"] == "abandoned"


def test_the_commit_probe_refuses_to_prove_absence_at_githubs_own_cap(monkeypatch):
    """[minor, round 3] GitHub caps this endpoint at 250 commits and returns
    them OLDEST first, so on a longer PR a recent judged head is absent from
    every read — the probe would report every verdict's commit as gone and
    abandon real verdicts. 250 is where absence stops being provable."""
    from alissa.tools.github.revloop.ghclient import PR_COMMIT_CAP, TruncatedListing

    gh = GitHub("alissa-app")
    pages = {
        1: [{"sha": f"a{i}"} for i in range(PER_PAGE)],
        2: [{"sha": f"b{i}"} for i in range(PER_PAGE)],
        3: [{"sha": f"c{i}"} for i in range(PR_COMMIT_CAP - 2 * PER_PAGE)],
    }
    gh._api = lambda *a, **k: pages[int(a[-1].split("=")[1])]

    with pytest.raises(TruncatedListing, match=str(PR_COMMIT_CAP)):
        gh.pull_request_commits(OWNER, REPO, NUMBER)


def test_a_short_pr_still_reads_its_whole_commit_list(monkeypatch):
    gh = GitHub("alissa-app")
    gh._api = lambda *a, **k: [{"sha": "abc123"}, {"sha": "def456"}]
    assert gh.pull_request_commits(OWNER, REPO, NUMBER) == ["abc123", "def456"]


@pytest.mark.parametrize("name", [
    "REVLOOP_REVIEWER_GITHUB_TOKEN_ENV",            # 33 — the round-2 reply's own example
    "ALISSA_REVLOOP_REVIEWER_GITHUB_TOKEN",         # 36
])
def test_a_long_but_plausible_name_is_accepted(tmp_path, name):
    """[minor, round 3] A 32-char ceiling rejected names an operator can
    plausibly write — and, because length fed the credential heuristic, told
    them to rotate a variable name."""
    assert Config.build(
        tmp_path, {"reviewer_token_env": name}
    ).reviewer_token_env == name


def test_an_over_long_name_is_refused_without_being_accused(tmp_path):
    """Over-length is still refused, but on its own message: length alone is
    not evidence of a credential."""
    over_long = "_".join(
        ["ALISSA", "REVLOOP", "REVIEWER", "GITHUB", "TOKEN", "ENV", "NAME"]
    )
    assert len(over_long) > 40
    with pytest.raises(ValueError) as exc:
        Config.build(tmp_path, {"reviewer_token_env": over_long})

    assert "at most 40 characters" in str(exc.value)
    assert "rotate it" not in str(exc.value), "long is not evidence of a credential"


def test_the_length_ceiling_no_longer_drives_redaction():
    from alissa.tools.github.revloop.config import _is_credential_shaped
    assert _is_credential_shaped("A_LONG_BUT_CLEARLY_DELIMITED_VARIABLE_NAME") is False
    assert _is_credential_shaped("a" * 40) is True, "an undelimited alnum run is"
    assert _is_credential_shaped("ghp_" + "a" * 36) is True


# -- round close-out: the dangling self review request (issue #54) ----------
#
# A review request is normally consumed by the requested identity submitting a
# review. When it is not, the PR stays in review-requested:@me forever and
# every poll pays a full re-verification to reach the same no-op. Close-out
# withdraws the daemon's OWN request -- and only in the terminal branch.


def _converged(config, **pr_kwargs):
    """A PR whose round-1 approve stands at the current head."""
    pr = make_pr(**pr_kwargs)
    return watcher(config, pr, [review("APPROVED")])


def test_a_closed_round_withdraws_its_own_dangling_review_request(config):
    w, gh, _ = _converged(config, requested=("alissa-app",))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.CONVERGED
    assert gh.removed == ["alissa-app"]


def test_close_out_removes_only_the_daemons_own_login(config):
    """Human reviewers and any second bot are somebody else's pending work."""
    w, gh, _ = _converged(
        config, requested=("human-dev", "alissa-app", "other-bot")
    )

    w.evaluate(OWNER, REPO, NUMBER)

    assert gh.removed == ["alissa-app"]
    assert gh._pr.requested_reviewers == ("human-dev", "other-bot")


def test_a_closed_round_with_no_dangling_request_calls_nothing(config):
    """The normal case: the verdict post already consumed the request."""
    w, gh, _ = _converged(config)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CONVERGED
    assert gh.removed == []


def test_no_removal_once_the_head_moved_past_the_verdict(config):
    """Round k+1 is owed, and the request is what will surface it."""
    pr = make_pr(sha="def456", requested=("alissa-app",))
    w, gh, _ = watcher(config, pr, [review("APPROVED", sha="abc123")])

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert gh.removed == []


def test_no_removal_on_a_request_changes_round(config):
    w, gh, _ = watcher(
        config,
        make_pr(requested=("alissa-app",)),
        [review("COMMENTED")],
        verdict="request_changes",
    )

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert gh.removed == []


def test_no_removal_while_a_round_is_in_flight(config):
    pr = make_pr(requested=("alissa-app",))
    w, gh, _ = watcher(config, pr, [])
    _record(w, pr, 1)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.IN_FLIGHT
    assert gh.removed == []


def test_no_removal_on_a_capped_pr(config):
    """A capped PR is exactly where a re-entry ack can still open a round, and
    the ack scan only runs while the PR is in the search — withdrawing its
    request there would delete the operator's own way back in."""
    reviews = [review("COMMENTED", at=f"2026-07-18T1{i}:00:00Z") for i in range(3)]
    w, gh, _ = watcher(
        config, make_pr(requested=("alissa-app",)), reviews, verdict=None
    )

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.ESCALATED
    assert gh.removed == []
    # ...and again on the next poll, when the page is already delivered.
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CAPPED
    assert gh.removed == []


def test_no_removal_when_a_round_still_owes_its_native_verdict(config):
    """An envelope ahead of the native count means the round is not closed:
    the post that is owed is what consumes the request."""
    w, gh, _ = watcher(
        config,
        make_pr(requested=("alissa-app",)),
        [review("COMMENTED")],
        verdict="approve",
        verdict_count=2,
    )

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.AWAITING_POST
    assert gh.removed == []


def test_dry_run_withdraws_nothing(config, caplog):
    config = dataclasses.replace(config, dry_run=True)
    w, gh, _ = _converged(config, requested=("alissa-app",))

    with caplog.at_level(logging.INFO):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CONVERGED

    assert gh.removed == []
    assert "[dry-run] would withdraw" in caplog.text


def test_a_failed_delete_logs_and_never_blocks_the_walk(config, caplog):
    w, gh, _ = _converged(config, requested=("alissa-app",))
    gh.remove_error = CommandError(["gh"], 1, "422 Unprocessable Entity")

    with caplog.at_level(logging.WARNING):
        results = w.poll_once()

    assert [d.action for _, d in results] == [Action.CONVERGED]
    assert "could not withdraw the dangling review request" in caplog.text


def test_a_throttled_delete_does_not_abort_the_pass(config):
    """RateLimited reaches run_forever's backoff from the READ paths. Here the
    decision is already made and the removal is a side effect of it, so a
    throttle must not turn a no-op into an aborted pass — the next poll
    retries."""
    w, gh, _ = _converged(config, requested=("alissa-app",))
    gh.remove_error = RateLimited("secondary rate limit")

    results = w.poll_once()

    assert [d.action for _, d in results] == [Action.CONVERGED]
    assert gh.removed == []


def test_the_withdrawn_pr_leaves_the_per_poll_evaluation_set(config):
    """AC2: no repeated full re-verification passes for a closed round."""
    w, gh, _ = _converged(config, requested=("alissa-app",))

    first = w.poll_once()
    assert [d.action for _, d in first] == [Action.CONVERGED]
    assert gh.pr_fetches == 1

    second = w.poll_once()

    assert second == [], "the closed round must not be evaluated again"
    assert gh.pr_fetches == 1, "and must not be re-fetched"


def test_a_failed_removal_keeps_the_pr_in_the_set_for_the_retry(config):
    w, gh, _ = _converged(config, requested=("alissa-app",))
    gh.remove_error = CommandError(["gh"], 1, "403 Forbidden")

    w.poll_once()
    w.poll_once()

    assert gh.pr_fetches == 2, "still evaluated — nothing was withdrawn"


def test_identity_drift_warns_once_naming_both_identities(config, caplog):
    """The #298 shape: the request is held against one login, the round's
    newest review carries another, so GitHub can never consume it."""
    reviews = [
        review("APPROVED", at="2026-07-18T10:00:00Z"),
        dataclasses.replace(
            review("COMMENTED", at="2026-07-18T11:00:00Z"), author="RHDZMOTA"
        ),
    ]
    w, gh, _ = watcher(
        config, make_pr(requested=("alissa-app",)), reviews, verdict_count=1
    )
    # The removal keeps failing, so the drift path is walked twice.
    gh.remove_error = CommandError(["gh"], 1, "403 Forbidden")

    with caplog.at_level(logging.WARNING):
        w.evaluate(OWNER, REPO, NUMBER)
        w.evaluate(OWNER, REPO, NUMBER)

    drift = [r for r in caplog.records if "IDENTITY DRIFT" in r.getMessage()]
    assert len(drift) == 1, "a config alarm, not a per-poll line"
    assert "'alissa-app'" in drift[0].getMessage()
    assert "'RHDZMOTA'" in drift[0].getMessage()


def test_no_drift_warning_when_the_verdict_of_record_is_ours(config, caplog):
    w, gh, _ = _converged(config, requested=("alissa-app",))

    with caplog.at_level(logging.WARNING):
        w.evaluate(OWNER, REPO, NUMBER)

    assert "IDENTITY DRIFT" not in caplog.text
    assert gh.removed == ["alissa-app"]


def test_an_unreadable_review_list_does_not_block_the_removal(config):
    """The drift check is a diagnostic; close-out does not depend on it."""
    w, gh, _ = _converged(config, requested=("alissa-app",))
    gh.reviews_error = CommandError(["gh"], 1, "500")

    w.evaluate(OWNER, REPO, NUMBER)

    assert gh.removed == ["alissa-app"]


def test_the_removal_log_line_carries_its_evidence(config, caplog):
    """A full-length SHA, so the [:8] truncation is actually pinned — the
    6-character fixture made `sha[:8] in line` an assertion that could not
    fail."""
    sha = "5f702b61e5f9a06f19d813ca16ae360f13fbf4c4"
    pr = make_pr(sha=sha, requested=("alissa-app", "human-dev"))
    w, gh, _ = watcher(config, pr, [review("APPROVED", sha=sha)])

    with caplog.at_level(logging.INFO):
        w.evaluate(OWNER, REPO, NUMBER)

    line = next(
        r.getMessage() for r in caplog.records if "round close-out" in r.getMessage()
    )
    assert SLUG in line and str(NUMBER) in line       # which PR
    assert sha[:8] in line and sha not in line        # at which head, short
    assert "#r1" in line                              # the verdict of record
    assert "human-dev" in line                        # what was left alone
    # ...and the convergence reason, interpolated verbatim. This pins that the
    # log line carries `why`, not anything about GitHub's own review state.
    assert "last GitHub review state is APPROVED" in line


def test_the_drift_kind_is_keyed_on_both_identities():
    assert identity_drift_kind("alissa-app", "RHDZMOTA") != identity_drift_kind(
        "alissa-app", "someone-else"
    )


def test_the_delete_body_is_a_json_array_naming_one_login(monkeypatch):
    """The wire shape, pinned at the layer that reaches `gh`.

    Pinning the argv was not enough: `-f 'reviewers[]=x'` is encoded as the
    array `{"reviewers": ["x"]}` by modern gh and as a string field NAMED
    `reviewers[]` by the 2.4.0 this client targets, so an argv assertion passes
    identically in both worlds. The body is now built here and piped in, and
    this asserts the bytes.
    """
    seen: dict = {}

    def fake_run_json(argv, *, timeout=60, env=None, stdin=None):
        seen["argv"], seen["stdin"] = list(argv), stdin
        return None

    monkeypatch.setattr(ghclient_module, "run_json", fake_run_json)
    GitHub("alissa-app").remove_review_request(OWNER, REPO, NUMBER, "alissa-app")

    assert seen["argv"] == [
        "gh",
        "api",
        "-X",
        "DELETE",
        f"repos/{OWNER}/{REPO}/pulls/{NUMBER}/requested_reviewers",
        "--input",
        "-",
    ]
    assert "-f" not in seen["argv"], "no gh field syntax may decide the encoding"
    assert json.loads(seen["stdin"]) == {"reviewers": ["alissa-app"]}


def test_the_pr_carries_the_pending_review_requests():
    """Read off the PR payload the loop already fetches — knowing whether the
    request dangles costs no extra call. Users only: a requested TEAM is never
    something this daemon may withdraw."""
    gh = GitHub("alissa-app")
    gh._api = lambda *a, **k: {
        "title": "Add widget cache",
        "user": {"login": "teammate"},
        "head": {"sha": "abc123"},
        "requested_reviewers": [{"login": "alissa-app"}, {"login": "human-dev"}, {}],
        "requested_teams": [{"slug": "platform"}],
    }

    pr = gh.pull_request(OWNER, REPO, NUMBER)

    assert pr.requested_reviewers == ("alissa-app", "human-dev")


def test_a_pr_with_no_pending_requests_reads_as_empty():
    gh = GitHub("alissa-app")
    gh._api = lambda *a, **k: {"head": {"sha": "abc123"}}
    assert gh.pull_request(OWNER, REPO, NUMBER).requested_reviewers == ()


# -- round-1 findings (PR #55) ---------------------------------------------


def test_a_permanently_failing_withdrawal_warns_once_and_keeps_retrying(config, caplog):
    """[major] The common failure — an identity without `pull_requests: write`
    — is permanent and per-PR, so an undeduped warning is ~2,880 lines a day
    that never converge on anything. Retry yes, re-announce no."""
    w, gh, _ = _converged(config, requested=("alissa-app",))
    gh.remove_error = CommandError(["gh"], 1, "403 Forbidden")

    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            w.evaluate(OWNER, REPO, NUMBER)

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "could not withdraw" in r.getMessage()
    ]
    assert len(warnings) == 1, "one line per condition, not per poll"
    assert "pull_requests: write" in warnings[0].getMessage(), "name the likely cause"


def test_a_different_failure_class_announces_itself_once_more(config, caplog):
    """Keyed on the exception class: a 403 that becomes a 422 is a genuinely
    different condition."""
    w, gh, _ = _converged(config, requested=("alissa-app",))
    gh.remove_error = CommandError(["gh"], 1, "403 Forbidden")
    w.evaluate(OWNER, REPO, NUMBER)
    # caplog's handler is live for the whole test, so the first pass's warning
    # is already in `records` — without this the assertion below cannot fail.
    caplog.clear()

    gh.remove_error = RateLimited("secondary rate limit")
    with caplog.at_level(logging.WARNING):
        w.evaluate(OWNER, REPO, NUMBER)

    assert [
        r for r in caplog.records if "could not withdraw" in r.getMessage()
    ], "a new failure class is new information"


def test_the_drift_probe_is_paid_once_per_head_not_once_per_poll(config):
    """[major] The drift check's extra read runs on the same path a permanent
    403 walks every poll, so it needs its own bound."""
    w, gh, _ = _converged(config, requested=("alissa-app",))
    gh.remove_error = CommandError(["gh"], 1, "403 Forbidden")

    for _ in range(4):
        w.evaluate(OWNER, REPO, NUMBER)

    assert gh.reviews_calls == 1, "one unfiltered review read for this head"


def test_an_unreadable_review_list_leaves_the_probe_owed(config):
    """A read that failed settles nothing, so it must not record the bound."""
    w, gh, _ = _converged(config, requested=("alissa-app",))
    gh.remove_error = CommandError(["gh"], 1, "403 Forbidden")
    gh.reviews_error = CommandError(["gh"], 1, "500")

    w.evaluate(OWNER, REPO, NUMBER)
    gh.reviews_error = None
    w.evaluate(OWNER, REPO, NUMBER)

    assert gh.reviews_calls == 2, "the failed probe retried"


def test_dry_run_still_names_the_drifted_identities(config, caplog):
    """[minor] Dry-run is the mode an operator reaches for to DIAGNOSE the #298
    shape; the drift line is the only thing that names the root cause."""
    reviews = [
        review("APPROVED", at="2026-07-18T10:00:00Z"),
        dataclasses.replace(
            review("COMMENTED", at="2026-07-18T11:00:00Z"), author="RHDZMOTA"
        ),
    ]
    w, gh, _ = watcher(
        config=dataclasses.replace(config, dry_run=True),
        pr=make_pr(requested=("alissa-app",)),
        reviews=reviews,
        verdict_count=1,
    )

    with caplog.at_level(logging.INFO):
        w.evaluate(OWNER, REPO, NUMBER)

    assert "IDENTITY DRIFT" in caplog.text
    assert "[dry-run] would withdraw" in caplog.text
    assert gh.removed == [], "still takes nothing on GitHub"


def test_a_verdict_with_no_commit_id_is_not_withdrawn_on(config):
    """[question] `_convergence_reason` lets a review with no commit_id
    converge — that convergence is not head-bound, so it must not also take the
    PR out of the daemon's sight. Not producible today; the terminal-branch
    argument holds on its own terms anyway."""
    w, gh, _ = watcher(
        config,
        make_pr(requested=("alissa-app",)),
        [dataclasses.replace(review("APPROVED"), commit_id="")],
    )

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.CONVERGED
    assert gh.removed == [], "convergence was never bound to this head"


def test_a_request_that_comes_back_at_the_same_head_is_named_loudly(config, caplog):
    """[major] A human's explicit re-request being undone deserves more than
    the INFO line the first withdrawal gets."""
    w, gh, _ = _converged(config, requested=("alissa-app",))
    w.evaluate(OWNER, REPO, NUMBER)
    assert gh.removed == ["alissa-app"]

    # The operator re-requests at the unchanged head.
    gh._pr = dataclasses.replace(gh._pr, requested_reviewers=("alissa-app",))
    gh.requests = [(OWNER, REPO, NUMBER)]

    with caplog.at_level(logging.INFO):
        w.evaluate(OWNER, REPO, NUMBER)

    assert gh.removed == ["alissa-app", "alissa-app"]
    again = next(
        r for r in caplog.records if "withdrawn AGAIN" in r.getMessage()
    )
    assert again.levelno == logging.WARNING
    assert "only a new commit opens one" in again.getMessage()


def test_the_first_withdrawal_at_a_new_head_is_not_loud(config, caplog):
    """The louder line is about a request COMING BACK, not about withdrawing:
    a fresh head gets the ordinary INFO close-out."""
    w, gh, _ = _converged(config, requested=("alissa-app",))
    w.evaluate(OWNER, REPO, NUMBER)

    gh._pr = dataclasses.replace(
        gh._pr, head_sha="def456", requested_reviewers=("alissa-app",)
    )
    gh._reviews.append(review("APPROVED", sha="def456", at="2026-07-19T10:00:00Z"))
    gh.requests = [(OWNER, REPO, NUMBER)]

    with caplog.at_level(logging.INFO):
        w.evaluate(OWNER, REPO, NUMBER)

    assert "withdrawn AGAIN" not in caplog.text
    assert gh.removed == ["alissa-app", "alissa-app"]


def test_run_actually_feeds_stdin_to_the_child():
    """The one layer the monkeypatched wire test cannot reach: that `stdin`
    becomes the child's standard input for real, so `gh api --input -` gets the
    body rather than an empty pipe."""
    from alissa.tools.github.revloop.proc import run
    body = json.dumps({"reviewers": ["alissa-app"]})
    assert run(["cat"], stdin=body) == body


# -- round-2 findings (PR #55) ---------------------------------------------


def _drifted_reviews():
    """Our approve at head, and a NEWER write-up by another identity — the
    #298 shape the drift alarm exists for."""
    return [
        review("APPROVED", at="2026-07-18T10:00:00Z"),
        dataclasses.replace(
            review("COMMENTED", at="2026-07-18T11:00:00Z"), author="RHDZMOTA"
        ),
    ]


def _drift_watcher(config, state=None):
    return watcher(
        config=config,
        pr=make_pr(requested=("alissa-app",)),
        reviews=_drifted_reviews(),
        state=state,
        verdict_count=1,
    )


def test_a_dry_run_pass_leaves_the_ping_ledger_clean(config):
    """[major] `state_db` has no dry-run branch, so a ledger row written by a
    diagnostic pass lands in the file the production daemon reads."""
    w, _, _ = _drift_watcher(dataclasses.replace(config, dry_run=True))

    w.evaluate(OWNER, REPO, NUMBER)

    assert not w.state.pinged(SLUG, NUMBER, drift_probe_kind("abc123"))
    assert not w.state.pinged(
        SLUG, NUMBER, identity_drift_kind("alissa-app", "RHDZMOTA")
    )


def test_a_dry_run_does_not_silence_the_production_alarm(config, caplog):
    """The half that actually matters: an operator diagnosing with --dry-run
    must not make the daemon that runs next permanently quiet."""
    state = State(config.state_db)
    dry, _, _ = _drift_watcher(dataclasses.replace(config, dry_run=True), state=state)
    dry.evaluate(OWNER, REPO, NUMBER)

    live, gh, _ = _drift_watcher(config, state=state)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        live.evaluate(OWNER, REPO, NUMBER)

    assert gh.removed == ["alissa-app"]
    assert "IDENTITY DRIFT" in caplog.text


def test_a_dry_run_does_not_silence_itself_either(config, caplog):
    """Not reading the ledger matters as much as not writing it: a production
    pass that already probed this head must not make the operator's dry-run
    print nothing."""
    state = State(config.state_db)
    live, _, _ = _drift_watcher(config, state=state)
    live.evaluate(OWNER, REPO, NUMBER)
    assert state.pinged(SLUG, NUMBER, drift_probe_kind("abc123"))

    dry, gh, _ = _drift_watcher(dataclasses.replace(config, dry_run=True), state=state)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        dry.evaluate(OWNER, REPO, NUMBER)

    assert "IDENTITY DRIFT" in caplog.text
    assert gh.removed == [], "still takes nothing on GitHub"


# -- round-3 findings (follow-up to PR #55) --------------------------------


def _dry(config):
    return dataclasses.replace(config, dry_run=True)


def test_a_dry_run_daemon_announces_a_drift_once_per_process(config, caplog):
    """[minor] Skipping the durable gate left the block re-emitted every poll —
    the round-1 arithmetic (~2,880/day at a 30s interval) moved into the
    diagnostic lane. `dry_run` is a config key and composes with run_forever,
    so `--once` being the documented recipe does not bound it."""
    w, gh, _ = _drift_watcher(_dry(config))

    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            w.evaluate(OWNER, REPO, NUMBER)

    drift = [r for r in caplog.records if "IDENTITY DRIFT" in r.getMessage()]
    assert len(drift) == 1, "once per process, not once per poll"
    assert gh.reviews_calls == 1, "and the read it needs is bounded with it"


def test_a_fresh_dry_run_process_still_announces(config, caplog):
    """The bound must not become a suppression: the gate is process-lifetime
    precisely so a new run always says everything it has to say."""
    state = State(config.state_db)
    first, _, _ = _drift_watcher(_dry(config), state=state)
    first.evaluate(OWNER, REPO, NUMBER)

    second, _, _ = _drift_watcher(_dry(config), state=state)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        second.evaluate(OWNER, REPO, NUMBER)

    assert "IDENTITY DRIFT" in caplog.text


def test_the_dry_run_bound_is_not_written_to_the_ledger(config):
    """Bounding dry-run must not reintroduce the round-2 regression."""
    w, _, _ = _drift_watcher(_dry(config))

    for _ in range(3):
        w.evaluate(OWNER, REPO, NUMBER)

    assert not w.state.pinged(SLUG, NUMBER, drift_probe_kind("abc123"))
    assert not w.state.pinged(
        SLUG, NUMBER, identity_drift_kind("alissa-app", "RHDZMOTA")
    )


def test_a_production_pass_is_not_bounded_by_a_dry_run_one(config, caplog):
    """The two gates share a shape, not a store — neither can close the other."""
    state = State(config.state_db)
    dry, _, _ = _drift_watcher(_dry(config), state=state)
    dry.evaluate(OWNER, REPO, NUMBER)

    live, gh, _ = _drift_watcher(config, state=state)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        live.evaluate(OWNER, REPO, NUMBER)

    assert "IDENTITY DRIFT" in caplog.text
    assert gh.removed == ["alissa-app"]
