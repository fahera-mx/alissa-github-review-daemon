"""The `alissa task list` narrowing probe and the call it assembles (issue #87).

`list_tasks` is the widest query this daemon issues, and every narrowing it can
apply depends on a flag the installed CLI may or may not have. So what is under
test here is the PROBE and the ARGV it produces: that a flag is sent only when
the CLI advertises it as an option (not merely mentions it in prose), that
`--self` additionally needs the deployment to have said its review tasks are
actor-owned, that an older CLI produces byte-for-byte the call the daemon has
always made, and -- the part that protects reviews rather than bytes -- that a
narrowed call which fails or comes back empty is disproved at runtime instead of
being read as "this actor has no review tasks".
"""

from __future__ import annotations

import pytest

from alissa.tools.github.revloop import alissa as alissa_module
from alissa.tools.github.revloop.alissa import (
    OPEN_STATUSES,
    TASK_LIST_STATUS_FILTER,
    Alissa,
    TaskListFlags,
)
from alissa.tools.github.revloop.proc import CommandError

PLAIN = ["alissa", "task", "list", "--json"]

# The real help text of the CLI this daemon ships against, captured verbatim on
# 2026-08-16 (CLI 0.1.0). Kept whole rather than trimmed to the lines under
# test: an invented help shape is how a probe stays green against output it
# would never see, and this one carries the trap the anchoring exists for --
# `--include-shared` appears BOTH as a real option and inside another option's
# prose.
HELP_0_1_0 = """Usage: alissa task list [options]

List the actionable tasks owned by your actor (validated/cancelled hidden)

Options:
  --json              Output raw JSON
  --include-terminal  Also list validated and cancelled tasks
  --self              Only your own actor's rows — skip the sponsor's corpus
                      (agent tokens). Pair with --include-shared
  --include-shared    Also list tasks shared with you
                      (contributor/reviewer/observer)
  -h, --help          display help for command
"""

# The same CLI as it was BEFORE `--self` shipped: no narrowing at all.
HELP_OLD = """Usage: alissa task list [options]

List the actionable tasks owned by your actor (validated/cancelled hidden)

Options:
  --json              Output raw JSON
  --include-terminal  Also list validated and cancelled tasks
  -h, --help          display help for command
"""

# A hypothetical future CLI carrying everything the daemon knows how to ask for.
HELP_FULL = """Usage: alissa task list [options]

List the actionable tasks owned by your actor (validated/cancelled hidden)

Options:
  --json                 Output raw JSON
  --include-terminal     Also list validated and cancelled tasks
  --self                 Only your own actor's rows
  --status <statuses>    Comma-separated statuses to include
  --view <view>          Projection: full (default) or digest
  -h, --help             display help for command
"""


def _row(number, title, status="committed"):
    return {"taskNumber": number, "title": title, "status": status}


ROWS = [_row(500, "Review PR acme/widgets#7 (TASK-499)")]


@pytest.fixture
def cli(monkeypatch):
    """A stand-in `alissa` CLI: scripted help text and scripted list payloads.

    Records every argv so a test can assert on the call that was MADE, which is
    the whole contract here -- a fake that answered whatever it was asked would
    let the argv builder be deleted.
    """

    class CLI:
        def __init__(self):
            self.help = HELP_0_1_0
            self.help_calls = 0
            self.help_error: "CommandError | None" = None
            self.calls: list[list[str]] = []
            # argv (as a tuple) -> payload or CommandError. Anything unscripted
            # answers `ROWS`.
            self.answers: dict = {}

        def run(self, argv, *, timeout=60, **kw):
            assert argv[:4] == ["alissa", "task", "list", "--help"][:4]
            self.help_calls += 1
            if self.help_error is not None:
                raise self.help_error
            return self.help

        def run_json(self, argv, *, timeout=60, **kw):
            self.calls.append(list(argv))
            answer = self.answers.get(tuple(argv), ROWS)
            if isinstance(answer, Exception):
                raise answer
            return answer

    fake = CLI()
    monkeypatch.setattr(alissa_module, "run", fake.run)
    monkeypatch.setattr(alissa_module, "run_json", fake.run_json)
    return fake


# -- the probe -------------------------------------------------------------


def test_todays_cli_advertises_only_self(cli):
    """The measured baseline. Anything else the daemon can ask for is absent
    from this CLI, so the probe must say so rather than assume."""
    flags = Alissa().probe_task_list()

    assert flags == TaskListFlags(status=False, self_scope=True, digest=False)


def test_an_older_cli_advertises_nothing(cli):
    cli.help = HELP_OLD

    assert Alissa().probe_task_list() == TaskListFlags()


def test_a_future_cli_advertises_everything(cli):
    cli.help = HELP_FULL

    assert Alissa().probe_task_list() == TaskListFlags(
        status=True, self_scope=True, digest=True
    )


def test_a_flag_named_only_in_prose_is_not_an_offer(cli):
    """`--status` mentioned in another option's description is not the CLI
    offering `--status`. Reading it as one sends a flag the CLI does not have,
    and this daemon turns a non-zero `alissa` exit into a skipped review."""
    cli.help = HELP_0_1_0.replace(
        "Also list tasks shared with you",
        "Also list tasks shared with you; combine with --status",
    )

    assert Alissa().probe_task_list().status is False


def test_a_flag_starting_a_wrapped_description_line_is_not_an_offer(cli):
    """The boundary the line-start anchor alone did not hold (PR #88 round 1).

    Commander wraps a long description onto continuation lines indented to the
    DESCRIPTION column, so a wrapped line can BEGIN with something that looks
    like an option. 0.1.0's own help already wraps `--self`'s description, so
    this is the shipped shape rather than a hypothetical one -- and the flag
    being wrongly offered here is `--status`, the one whose argument syntax the
    daemon would be guessing at.
    """
    cli.help = HELP_0_1_0.replace(
        "  --include-shared    Also list tasks shared with you\n",
        "  --include-shared    Also list tasks shared with you; combine\n"
        "                      --status with it to filter\n",
    )

    flags = Alissa().probe_task_list()

    assert flags.status is False, "a wrapped description line is not an offer"
    assert flags.self_scope is True, "and the real options are still found"


def test_an_option_is_found_however_the_listing_indents_its_column(cli):
    """The guard is the option COLUMN, not one hardcoded width: a listing whose
    longest option name pushes the description column out must still work."""
    cli.help = HELP_FULL.replace("  --status", "   --status")

    assert Alissa().probe_task_list().status is True


def test_a_longer_flag_starting_with_the_same_letters_is_not_the_flag(cli):
    cli.help = HELP_OLD.replace(
        "  --json              Output raw JSON",
        "  --json              Output raw JSON\n  --self-review       Something else",
    )

    assert Alissa().probe_task_list().self_scope is False


def test_a_probe_that_answers_is_paid_once_per_process(cli):
    client = Alissa()

    client.probe_task_list()
    client.probe_task_list()
    client.list_tasks()

    assert cli.help_calls == 1, "the CLI cannot change under a running daemon"


def test_a_probe_that_fails_degrades_this_pass_only(cli):
    """A transient `alissa` failure must not pin the daemon to the widest call
    until someone restarts it."""
    cli.help_error = CommandError(["alissa"], 1, "boom")
    client = Alissa()

    assert client.probe_task_list() == TaskListFlags()
    assert client.task_list_argv() == PLAIN

    cli.help_error = None
    assert client.probe_task_list().self_scope is True


# -- the argv it assembles -------------------------------------------------


def test_an_old_cli_gets_exactly_the_call_the_daemon_always_made(cli):
    cli.help = HELP_OLD

    assert Alissa(task_list_self_scope=True).task_list_argv() == PLAIN


def test_self_needs_both_the_flag_and_the_deployments_word(cli):
    """Ownership is a property of a deployment, not of the CLI: a review task
    the list cannot see is a round the daemon cannot count."""
    assert Alissa().task_list_argv() == PLAIN, "advertised, but not opted into"
    assert Alissa(task_list_self_scope=True).task_list_argv() == PLAIN + ["--self"]

    cli.help = HELP_OLD
    assert Alissa(task_list_self_scope=True).task_list_argv() == PLAIN, "opted in, absent"


def test_the_status_filter_is_the_daemons_own_open_set(cli):
    """Server-side filtering by exactly the statuses `is_open` accepts cannot
    change which task resolves -- only how much of the corpus crosses the wire.
    Deriving it from OPEN_STATUSES is what keeps that true as the set grows."""
    cli.help = HELP_FULL

    argv = Alissa().task_list_argv()

    assert argv == PLAIN + ["--status", TASK_LIST_STATUS_FILTER, "--view", "digest"]
    assert set(TASK_LIST_STATUS_FILTER.split(",")) == OPEN_STATUSES


def test_the_digest_view_needs_no_knob(cli):
    """The daemon keeps taskNumber/title/status and nothing else, so a leaner
    projection is pure saving with no semantics to get wrong."""
    cli.help = HELP_FULL.replace("  --self                 Only your own actor's rows\n", "")

    assert Alissa().task_list_argv() == PLAIN + [
        "--status", TASK_LIST_STATUS_FILTER, "--view", "digest",
    ]


def test_narrow_status_false_drops_only_the_status_filter(cli):
    """`alissa-pr-review` reads a review task whose status `is_open` rejects, so
    it opts out of that one narrowing and keeps every other."""
    cli.help = HELP_FULL

    argv = Alissa(task_list_self_scope=True).task_list_argv(narrow_status=False)

    assert argv == PLAIN + ["--self", "--view", "digest"]


def test_list_tasks_makes_the_narrowed_call_and_parses_it(cli):
    cli.help = HELP_FULL
    narrowed = tuple(PLAIN + ["--status", TASK_LIST_STATUS_FILTER, "--view", "digest"])
    cli.answers[narrowed] = ROWS

    tasks = Alissa().list_tasks()

    assert [t.ref for t in tasks] == ["TASK-500"]
    assert cli.calls == [list(narrowed)]


# -- runtime disproof: a flag the CLI advertises but the API does not serve --


def test_a_narrowed_call_that_fails_retries_plain_and_stops_narrowing(cli):
    cli.help = HELP_FULL
    narrowed = tuple(PLAIN + ["--status", TASK_LIST_STATUS_FILTER, "--view", "digest"])
    cli.answers[narrowed] = CommandError(list(narrowed), 1, "unknown option")
    client = Alissa()

    assert [t.ref for t in client.list_tasks()] == ["TASK-500"]
    assert cli.calls == [list(narrowed), PLAIN], "one retry, unnarrowed"

    client.list_tasks()
    assert cli.calls[-1] == PLAIN, "and this process does not narrow again"


def test_a_failed_narrowed_call_does_not_also_pay_the_empty_cross_check(cli):
    """The retry above already made the plain call. If it legitimately answers an
    EMPTY corpus, the empty-answer cross-check must not fire a THIRD list --
    inside the change whose purpose is removing whole-corpus fetches (PR #88
    round 1)."""
    cli.help = HELP_FULL
    narrowed = tuple(PLAIN + ["--status", TASK_LIST_STATUS_FILTER, "--view", "digest"])
    cli.answers[narrowed] = CommandError(list(narrowed), 1, "unknown option")
    cli.answers[tuple(PLAIN)] = []

    assert Alissa().list_tasks() == []
    assert cli.calls == [list(narrowed), PLAIN], "two calls, not three"


def test_a_plain_call_that_fails_still_raises(cli):
    """The retry exists to survive a bad FLAG. A CLI that is simply down must
    still reach the caller, which turns it into one PR's SKIPPED decision."""
    cli.help = HELP_OLD
    cli.answers[tuple(PLAIN)] = CommandError(PLAIN, 1, "network")

    with pytest.raises(CommandError):
        Alissa().list_tasks()

    assert cli.calls == [PLAIN], "and it is not retried in a loop"


def test_a_narrowed_call_that_answers_empty_is_checked_against_the_plain_one(cli):
    """The dangerous failure: a filter the API accepts and ignores, or serves as
    nothing. `find_review_task` reads an empty corpus as 'this PR has no review
    task', which is a skipped review -- so an empty narrowed answer is never
    taken at face value."""
    cli.help = HELP_FULL
    narrowed = tuple(PLAIN + ["--status", TASK_LIST_STATUS_FILTER, "--view", "digest"])
    cli.answers[narrowed] = []
    client = Alissa()

    assert [t.ref for t in client.list_tasks()] == ["TASK-500"]
    assert cli.calls == [list(narrowed), PLAIN]

    client.list_tasks()
    assert cli.calls[-1] == PLAIN, "the narrowing is disproved for this process"


def test_a_genuinely_empty_corpus_costs_one_extra_list_and_keeps_narrowing(cli):
    cli.help = HELP_FULL
    narrowed = tuple(PLAIN + ["--status", TASK_LIST_STATUS_FILTER, "--view", "digest"])
    cli.answers[narrowed] = []
    cli.answers[tuple(PLAIN)] = []
    client = Alissa()

    assert client.list_tasks() == []
    assert cli.calls == [list(narrowed), PLAIN]

    client.list_tasks()
    assert cli.calls == [list(narrowed), PLAIN] * 2, (
        "nothing was disproved, so the narrowed call is still made -- and the "
        "cross-check costs one extra EMPTY list each pass, which is the price "
        "of never mistaking a broken filter for an empty corpus"
    )
