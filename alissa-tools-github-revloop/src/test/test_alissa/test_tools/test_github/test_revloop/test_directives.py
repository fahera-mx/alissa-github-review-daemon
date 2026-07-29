"""The reviewer directives must spell out the closing contract in every round.

These are the reviewer's most-skipped steps: on re-review, sessions produce
findings but never register the review on the PR, or stop without a verdict. The
directive is the literal prompt, so the requirements live here explicitly — this
guards them against being edited away.
"""

from __future__ import annotations

import pytest

from alissa.tools.github.revloop.loop import (
    ROUND_1_DIRECTIVE,
    ROUND_K_DIRECTIVE,
    _POST_AS_REVIEWER,
)


@pytest.mark.parametrize("template", [ROUND_1_DIRECTIVE, ROUND_K_DIRECTIVE])
def test_directive_demands_registered_review_and_a_verdict(template):
    text = template.format(
        assignment="You've been assigned TASK-1.", round=2, cap=3,
        session="review-widgets-pr7-r2", credential="",
    ).lower()
    # (1) the review must actually register on the PR
    assert "submit" in text and "review record" in text
    assert "session do not exist" in text
    # (2) always close with a decisive verdict, never comment-only
    assert "approve or request_changes" in text
    assert "never neither" in text
    # (3) read-only posture reinforced
    assert "never commit or fix" in text
    # (4) self-kill as the final action, using the injected session name
    assert "alissa tmux kill review-widgets-pr7-r2" in text
    assert text.rstrip().endswith("do nothing after it.")


@pytest.mark.parametrize("template", [ROUND_1_DIRECTIVE, ROUND_K_DIRECTIVE])
def test_directive_formats_without_stray_braces(template):
    # The closing clause must not introduce unescaped {…} that break .format().
    out = template.format(
        assignment="a.", round=1, cap=3, session="review-x-pr1-r1",
        credential=_POST_AS_REVIEWER.format(env_var="REV_TOKEN"),
    )
    assert "{" not in out and "}" not in out


@pytest.mark.parametrize("template", [ROUND_1_DIRECTIVE, ROUND_K_DIRECTIVE])
def test_directive_routes_gh_writes_through_the_reviewer_credential(template):
    """A session inherits the container's default gh credential, which belongs
    to the IMPLEMENTER identity — the leak that put round-1 verdicts under the
    wrong login. The clause names the variable holding the right token; it
    never carries the token itself."""
    out = template.format(
        assignment="a.", round=1, cap=3, session="review-x-pr1-r1",
        credential=_POST_AS_REVIEWER.format(env_var="REVLOOP_REVIEWER_GH_TOKEN"),
    )
    assert 'GH_TOKEN="$REVLOOP_REVIEWER_GH_TOKEN" gh' in out
    assert "not the round's verdict of record" in out
