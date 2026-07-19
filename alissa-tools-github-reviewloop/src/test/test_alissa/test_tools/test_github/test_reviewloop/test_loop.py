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

    def pull_request(self, owner, repo, number):
        return self._pr

    def my_reviews(self, owner, repo, number):
        return [r for r in self._reviews if r.author == self.login]

    def comment(self, owner, repo, number, body):
        self.comments.append(body)

    def review_requests(self, repos=()):
        return [(OWNER, REPO, NUMBER)]


class FakeAlissa:
    def __init__(self, task=None):
        self.task = task
        self.enqueued: list[dict] = []
        self.added: list[tuple] = []
        self.on_add = None  # optional side effect: actually create the hub

    def find_review_task(self, owner, repo, number):
        return self.task

    def enqueue_reviewer(self, **kwargs):
        self.enqueued.append(kwargs)

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


def make_pr(*, draft=False, author="teammate", sha="abc123") -> PullRequest:
    return PullRequest(
        owner=OWNER,
        repo=REPO,
        number=NUMBER,
        title="Add widget cache",
        author=author,
        head_sha=sha,
        draft=draft,
        url=f"https://github.com/{SLUG}/pull/{NUMBER}",
    )


def review(state="CHANGES_REQUESTED", sha="abc123", at="2026-07-18T10:00:00Z"):
    return Review(
        author="alissa-app",
        state=state,
        commit_id=sha,
        submitted_at=at,
        url=f"https://github.com/{SLUG}/pull/{NUMBER}#r1",
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


def watcher(config, pr, reviews, task=FakeTask(), state=None):
    gh = FakeGitHub(pr, reviews)
    al = FakeAlissa(task)
    w = ReviewWatcher(config, github=gh, alissa=al, state=state or State(config.state_db))
    return w, gh, al


# -- round 1 ---------------------------------------------------------------


def test_pending_request_with_no_prior_review_spawns_round_1(config):
    w, _, al = watcher(config, make_pr(), [])
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert d.round == 1
    assert al.enqueued[0]["session"] == "review-widgets-pr7-r1"
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
    assert al.enqueued[0]["session"] == "review-widgets-pr7-r2"
    directive = al.enqueued[0]["directive"]
    assert "round 2 of a review loop (cap 3)" in directive
    assert "verify the triage of every prior finding" in directive


def test_comment_only_review_still_closes_a_round(config):
    """Single-operator workspaces post comment-mode reviews (CR5); they must
    still count as a completed round or the loop would never advance."""
    w, _, _ = watcher(config, make_pr(sha="def456"), [review("COMMENTED")])
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.round == 2


# -- convergence and cap-out ----------------------------------------------


def test_approved_pr_is_converged(config):
    w, _, al = watcher(
        config, make_pr(), [review("CHANGES_REQUESTED"), review("APPROVED")]
    )
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.CONVERGED
    assert al.enqueued == []


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


def test_session_names_are_tmux_safe_and_unique():
    pr = make_pr()
    assert session_name(pr, 1) == "review-widgets-pr7-r1"
    assert session_name(pr, 2) == "review-widgets-pr7-r2"

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
    assert name == "review-widgets-app-pr7-r1"
    assert ":" not in name and "." not in name


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
