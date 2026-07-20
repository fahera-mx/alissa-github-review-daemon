"""Tests for the implementer-side driver `alissa-pr-review`.

GitHub, Alissa, and the gh subprocess calls are faked; what is under test is
round-completion detection (a new substantive reviewer review), reading the
verdict from the review task envelope, the self-review identity guard, and the
timeout path.
"""

from __future__ import annotations

import pytest

from alissa.tools.github.reviewloop import prreview
from alissa.tools.github.reviewloop.alissa import (
    VERDICT_APPROVE,
    VERDICT_REQUEST_CHANGES,
    Task,
)
from alissa.tools.github.reviewloop.ghclient import Review

OWNER, REPO, NUMBER = "acme", "widgets", 7
REVIEWER, DEV = "alissa-app", "dev-account"
PR_URL = f"https://github.com/{OWNER}/{REPO}/pull/{NUMBER}"


def review(author: str, body: str = "a real review") -> Review:
    return Review(
        author=author, state="COMMENTED", commit_id="", submitted_at="2026-01-01T00:00:00Z",
        url="", body=body,
    )


def review_task(status: str = "pending_validation") -> Task:
    return Task(ref="TASK-99", title=f"Review PR {OWNER}/{REPO}#{NUMBER} (TASK-1)", status=status)


# -- pure helpers -------------------------------------------------------------

def test_review_task_ref_matches_cr2_title():
    class A:
        def list_tasks(self):
            return [
                Task(ref="TASK-1", title="Do the thing", status="in_progress"),
                review_task(),
            ]

    assert prreview._review_task_ref(A(), OWNER, REPO, NUMBER) == "TASK-99"


def test_review_task_ref_none_when_absent():
    class A:
        def list_tasks(self):
            return [Task(ref="TASK-1", title="Review PR other/repo#1", status="todo")]

    assert prreview._review_task_ref(A(), OWNER, REPO, NUMBER) is None


def test_reviewer_review_count_counts_only_substantive_reviewer_reviews():
    class G:
        def reviews(self, o, r, n):
            return [
                review(REVIEWER, "round 1 verdict writeup"),  # counts
                review(REVIEWER, ""),                         # empty artifact — ignored
                review(DEV, "a dev self-note"),               # not the reviewer — ignored
            ]

    assert prreview._reviewer_review_count(G(), OWNER, REPO, NUMBER, REVIEWER) == 1


# -- main() flow --------------------------------------------------------------

@pytest.fixture
def wiring(monkeypatch):
    """Fake out GitHub/Alissa, the gh subprocess calls, and sleep.

    `review_seq` is a list of review-lists returned on successive `.reviews()`
    calls (last value sticks), so a round can 'land' partway through the poll.
    """
    state = {"review_seq": [[]], "i": 0, "verdict": None, "tasks": []}

    class FakeGH:
        def token_login(self):
            return DEV

        def reviews(self, o, r, n):
            seq = state["review_seq"]
            val = seq[min(state["i"], len(seq) - 1)]
            state["i"] += 1
            return val

    class FakeAlissa:
        def list_tasks(self):
            return state["tasks"]

        def latest_verdict(self, ref):
            return state["verdict"]

    monkeypatch.setattr(prreview, "GitHub", lambda *a, **k: FakeGH())
    monkeypatch.setattr(prreview, "Alissa", lambda *a, **k: FakeAlissa())
    monkeypatch.setattr(prreview, "run", lambda *a, **k: "")
    monkeypatch.setattr(
        prreview, "run_json",
        lambda *a, **k: {"url": PR_URL, "number": NUMBER, "isDraft": True, "state": "OPEN"},
    )
    monkeypatch.setattr(prreview.time, "sleep", lambda *_: None)
    return state


def test_identity_guard_rejects_self_review(wiring, capsys):
    # dev token == reviewer → GitHub would forbid the request.
    rc = prreview.main(["--reviewer", DEV])
    assert rc == 3
    assert "forbids requesting review from the PR author" in capsys.readouterr().err


def test_approve_returns_zero(wiring):
    # snapshot sees 0 reviews; then a reviewer review lands, envelope = approve.
    wiring["review_seq"] = [[], [review(REVIEWER)]]
    wiring["tasks"] = [review_task()]
    wiring["verdict"] = VERDICT_APPROVE
    assert prreview.main(["--reviewer", REVIEWER, "--poll-interval", "0"]) == 0


def test_request_changes_returns_one(wiring):
    wiring["review_seq"] = [[], [review(REVIEWER)]]
    wiring["tasks"] = [review_task()]
    wiring["verdict"] = VERDICT_REQUEST_CHANGES
    assert prreview.main(["--reviewer", REVIEWER, "--poll-interval", "0"]) == 1


def test_timeout_returns_two_when_no_new_review(wiring, monkeypatch):
    # No reviewer review ever lands; a fake clock jumps past the deadline.
    clock = {"t": 1000.0}
    monkeypatch.setattr(prreview.time, "time", lambda: clock["t"])

    def sleep(_):
        clock["t"] += 60  # each poll advances the clock a minute

    monkeypatch.setattr(prreview.time, "sleep", sleep)
    wiring["review_seq"] = [[]]  # snapshot sees 0, and it never increases
    rc = prreview.main(["--reviewer", REVIEWER, "--timeout", "120", "--poll-interval", "30"])
    assert rc == 2
