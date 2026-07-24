"""Decision-logic tests for the review loop state machine.

GitHub and Alissa are faked; what is under test is when a round is owed, when
it is in flight, when the loop has converged, and when CR9 caps out.
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from alissa.tools.github.revloop.config import (
    CONFIG_FILENAME,
    HUB_ADD,
    ON_MISSING_SKIP,
    Config,
    resolve_config_path,
)
from alissa.tools.github.revloop.alissa import ManagedSession
from alissa.tools.github.revloop.ghclient import (
    GitHub,
    IdentityMismatch,
    IssueComment,
    PullRequest,
    Review,
)
from alissa.tools.github.revloop.loop import (
    ACTIVITY_MARKER,
    REAP_QUIET_SECONDS,
    STALE_ROUND_SECONDS,
    STALLED_DEFER_MULTIPLE,
    Action,
    ReviewWatcher,
    deferral_activity_kind,
    session_name,
    stalled_kind,
)
from alissa.tools.github.revloop.state import State

OWNER, REPO, NUMBER = "acme", "widgets", 7
SLUG = f"{OWNER}/{REPO}"


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

    def pull_request(self, owner, repo, number):
        self.pr_fetches += 1
        return self._pr

    def my_reviews(self, owner, repo, number):
        # Mirrors GitHub.my_reviews: mine, substantive, oldest first.
        mine = [
            r for r in self._reviews if r.author == self.login and r.is_substantive
        ]
        return sorted(mine, key=lambda r: r.submitted_at)

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
        return [s for s in self.sessions if s.name.startswith("review-")]

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


def make_pr(*, draft=False, author="teammate", sha="abc123", state="open", merged=False) -> PullRequest:
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
    # last_activity=0 means "quiet for ages" — past REAP_QUIET_SECONDS.
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
    it has been quiet for REAP_QUIET_SECONDS."""
    pr = make_pr()
    w, _, al = watcher(config, pr, [review()])
    s1 = _record(w, pr, 1)
    _live(al, s1, last_activity=time.time())  # just did something

    w.sweep_sessions()
    assert al.killed == []

    al.sessions = []
    _live(al, s1, last_activity=time.time() - REAP_QUIET_SECONDS * 2)  # long quiet
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
          last_activity=time.time() - REAP_QUIET_SECONDS * 2)
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


def test_empty_body_round_still_counts_via_envelope(config):
    # The prior round's GitHub review had an empty body (is_substantive False),
    # so github shows 0 reviews -- but its verdict envelope was recorded. Without
    # the envelope count the daemon would repeat round 1 (name collision -> stuck);
    # with it, the next round is correctly round 2.
    pr = make_pr()
    w, _, al = watcher(config, pr, [], verdict_count=1)
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
