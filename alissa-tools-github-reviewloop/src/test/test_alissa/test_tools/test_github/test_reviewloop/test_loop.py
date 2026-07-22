"""Decision-logic tests for the review loop state machine.

GitHub and Alissa are faked; what is under test is when a round is owed, when
it is in flight, when the loop has converged, and when CR9 caps out.
"""

from __future__ import annotations

import time

import pytest

from alissa.tools.github.reviewloop.config import (
    CONFIG_FILENAME,
    HUB_ADD,
    ON_MISSING_SKIP,
    Config,
    resolve_config_path,
)
from alissa.tools.github.reviewloop.alissa import ManagedSession
from alissa.tools.github.reviewloop.ghclient import GitHub, IdentityMismatch, PullRequest, Review
from alissa.tools.github.reviewloop.loop import STALE_ROUND_SECONDS, Action, ReviewWatcher, session_name
from alissa.tools.github.reviewloop.state import State

OWNER, REPO, NUMBER = "acme", "widgets", 7
SLUG = f"{OWNER}/{REPO}"


class FakeGitHub:
    def __init__(self, pr: PullRequest, reviews: list[Review], login: str = "alissa-app"):
        self.login = login
        self._pr = pr
        self._reviews = reviews
        self.comments: list[str] = []
        self.requests = [(OWNER, REPO, NUMBER)]

    def pull_request(self, owner, repo, number):
        return self._pr

    def my_reviews(self, owner, repo, number):
        # Mirrors GitHub.my_reviews: mine, substantive, oldest first.
        mine = [
            r for r in self._reviews if r.author == self.login and r.is_substantive
        ]
        return sorted(mine, key=lambda r: r.submitted_at)

    def comment(self, owner, repo, number, body):
        self.comments.append(body)

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
    assert gh.comments == [], "must not escalate on artifact count"


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
    assert cfg.round_cap == 3


def test_underscore_keys_are_treated_as_comments(tmp_path):
    cfg = Config.build(tmp_path, {"_note": "json has no comments", "round_cap": 2})
    assert cfg.round_cap == 2


def test_state_path_defaults_inside_the_workspace(tmp_path):
    """Two daemons over different workspaces must not share a spawn ledger."""
    one = Config.build(tmp_path / "ws-one")
    two = Config.build(tmp_path / "ws-two")

    assert one.state_db == (tmp_path / "ws-one" / ".reviewloop" / "state.db")
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
    from alissa.tools.github.reviewloop.__main__ import build_parser

    return build_parser().parse_args(list(argv))


def test_workspace_root_defaults_to_cwd(tmp_path, monkeypatch):
    from alissa.tools.github.reviewloop.__main__ import resolve_config

    monkeypatch.chdir(tmp_path)
    assert resolve_config(cli()).workspace_root == tmp_path.resolve()


def test_workspace_root_flag_beats_cwd(tmp_path, monkeypatch):
    from alissa.tools.github.reviewloop.__main__ import resolve_config

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(tmp_path)

    cfg = resolve_config(cli("--workspace-root", str(ws)))
    assert cfg.workspace_root == ws.resolve()


def test_repeated_repo_flags_accumulate(tmp_path, monkeypatch):
    from alissa.tools.github.reviewloop.__main__ import resolve_config

    monkeypatch.chdir(tmp_path)
    cfg = resolve_config(cli("--repo", "a/one", "--repo", "a/two"))
    assert cfg.repos == ("a/one", "a/two")


def test_cli_fills_in_over_a_discovered_config_file(tmp_path, monkeypatch):
    import json

    from alissa.tools.github.reviewloop.__main__ import resolve_config

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

    from alissa.tools.github.reviewloop.__main__ import resolve_config

    (tmp_path / CONFIG_FILENAME).write_text(json.dumps({"dry_run": True}))
    monkeypatch.chdir(tmp_path)

    assert resolve_config(cli("--no-dry-run")).dry_run is False
    assert resolve_config(cli()).dry_run is True


def test_dry_run_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli("--dry-run", "--no-dry-run")


def test_workspace_root_in_config_file_is_a_clear_error(tmp_path, monkeypatch):
    import json

    from alissa.tools.github.reviewloop.__main__ import main

    (tmp_path / CONFIG_FILENAME).write_text(
        json.dumps({"workspace_root": str(tmp_path)})
    )
    monkeypatch.chdir(tmp_path)

    assert main([]) == 2


def test_missing_explicit_config_exits_with_config_error(tmp_path, monkeypatch):
    from alissa.tools.github.reviewloop.__main__ import main

    monkeypatch.chdir(tmp_path)
    assert main(["--config-path", str(tmp_path / "nope.json")]) == 2


def test_task_ref_uses_task_number_not_seq(monkeypatch):
    """`TASK-<taskSeq>` 404s server-side; the resolvable ref is taskNumber."""
    from alissa.tools.github.reviewloop import alissa as alissa_mod

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
    from alissa.tools.github.reviewloop import alissa as alissa_mod

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
    from alissa.tools.github.reviewloop import alissa as alissa_mod
    from alissa.tools.github.reviewloop.proc import CommandError

    def boom(*a, **k):
        raise CommandError(["alissa", "task", "get"], 1, "task not found")

    monkeypatch.setattr(alissa_mod, "run_json", boom)
    assert alissa_mod.Alissa().latest_verdict("TASK-500") is None


def test_task_without_a_number_is_skipped(monkeypatch):
    from alissa.tools.github.reviewloop import alissa as alissa_mod

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
    from alissa.tools.github.reviewloop.proc import CommandError

    w, _, al = watcher(config, make_pr(), [review()])

    def boom():
        raise CommandError(["alissa", "tmux", "ls"], 1, "no tmux server")

    al.list_review_sessions = boom
    w.sweep_sessions()  # must not raise — retried next poll

    assert al.killed == []


def test_sweep_spares_when_github_is_undecidable(config):
    from alissa.tools.github.reviewloop.proc import CommandError

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
    _live(al, s1, last_activity=time.time() - STALE_ROUND_SECONDS)  # long quiet
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


def test_sweep_falls_back_to_review_count_without_a_task_ref(config):
    # Spawn recorded before any review task existed: the GitHub substantive-
    # review count is the only signal left.
    pr = make_pr()
    w, _, al = watcher(config, pr, [review()], task=None)
    s1 = _record(w, pr, 1, task_ref=None)
    _live(al, s1)

    w.sweep_sessions()

    assert al.killed == [s1]


# -- run_forever ------------------------------------------------------------

def test_run_forever_exits_cleanly_on_interrupt_during_sleep(config, monkeypatch):
    """The dominant real case: with a 60s poll interval (up to 900s backing
    off) the loop spends nearly all its wall-clock inside time.sleep, so
    Ctrl-C almost always lands there — it must hit the same clean-exit path,
    not traceback out of run_forever."""
    from alissa.tools.github.reviewloop import loop as loop_mod

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
    from alissa.tools.github.reviewloop.alissa import Alissa
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
