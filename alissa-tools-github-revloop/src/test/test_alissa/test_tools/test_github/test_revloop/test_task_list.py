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

Since issue #97 it also pins the one thing the probe cannot check: that the argv
is a call the CLI on the other side ACCEPTS. The status filter is validated
client-side against seven canonical statuses, and one unknown value costs not a
coarser filter but every narrowing on the call -- so the filter's vocabulary is
pinned against that canonical set, and the assembled argv is put through a
transcription of the CLI's own parse (`cli_0_2_0_task_list`).
"""

from __future__ import annotations

import logging

import pytest

from alissa.tools.github.revloop import alissa as alissa_module
from alissa.tools.github.revloop.alissa import (
    CANONICAL_TASK_STATUSES,
    OPEN_STATUSES,
    TASK_LIST_STATUS_FILTER,
    Alissa,
    TaskListFlags,
    _status_filter,
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

# The help of CLI 0.2.0 -- the version that ships the two flags this daemon has
# been probing for (fahera-mx/studio.alissa.app PR #830).
#
# Not hand-written: rendered by the commander bundled in the installed `alissa`
# CLI, with #830's own `.option()` calls for `--status` / `--view` spliced into
# `task list`, and captured verbatim on 2026-08-28. The probe is a text matcher
# over a listing whose column widths move with the longest option name, so an
# invented layout tests a shape the fleet never emits -- and this real one
# carries a trap an invented one would not have thought of: `--view`'s
# description names `--digest`, which is a real option two rows above it.
HELP_0_2_0 = """Usage: alissa task list [options]

List the actionable tasks owned by your actor (validated/cancelled hidden)

Options:
  --json                 Output raw JSON
  --include-terminal     Also list validated and cancelled tasks
  --self                 Only your own actor's rows — skip the sponsor's corpus
                         (agent tokens). Pair with --include-shared
  --include-shared       Also list tasks shared with you
                         (contributor/reviewer/observer)
  --digest               Ask for the digest row shape: id, number, title,
                         status, priority, updatedAt
  --status <statuses>    Comma-separated statuses to keep — any of draft,
                         committed, in_progress, blocked, pending_validation,
                         validated, cancelled
  --view <view>          Row shape asked of the API: digest (same as --digest)
                         or full (whole task documents)
  --project <projectId>  Only tasks in this project's bodies of work (Convex
                         project id)
  --bow <bowId>          Only tasks in this body of work (Convex BOW id)
  -h, --help             display help for command
"""


class CliError(Exception):
    """The studio CLI's own failure type: a message and exit 1, no request."""


# The seven statuses CLI 0.2.0 validates `--status` against -- `TASK_STATUSES`
# in `cli/src/commands/task.ts`, transcribed rather than imported from the
# daemon's own CANONICAL_TASK_STATUSES: a harness that reads the constant under
# test cannot fail when that constant is wrong, which is the only failure this
# file exists to catch.
CLI_0_2_0_STATUSES = [
    "draft",
    "committed",
    "in_progress",
    "blocked",
    "pending_validation",
    "validated",
    "cancelled",
]


def cli_0_2_0_task_list(argv: "list[str]") -> dict:
    """What CLI 0.2.0 does with `argv` BEFORE it issues a request.

    A transcription of `parseStatusFilter` / `parseListView` and the option
    table around them (fahera-mx/studio.alissa.app PR #830). Raises `CliError`
    exactly where that CLI raises one -- unknown option, unknown status, unknown
    view, or the `--digest --view full` contradiction -- and otherwise returns
    the `TaskListQuery` it would send.

    It is here because the acceptance criterion is not "the daemon builds the
    argv it means to", which a self-referential assertion satisfies with any
    filter at all: it is that the CLI on the other side ACCEPTS that argv. The
    real CLI cannot be run from these tests (it is a node bundle, and the
    version that has these flags is not the one installed), so the validation it
    would apply is written out here, with the exact vocabulary it validates
    against, and checked against the argv the daemon actually assembles.
    """
    assert argv[:3] == ["alissa", "task", "list"]
    booleans = {"--json", "--include-terminal", "--self", "--include-shared", "--digest"}
    valued = {"--status", "--view", "--project", "--bow"}

    opts: dict = {}
    rest, i = argv[3:], 0
    while i < len(rest):
        token = rest[i]
        if token in booleans:
            opts[token] = True
            i += 1
        elif token in valued:
            if i + 1 >= len(rest):
                raise CliError(f"option '{token} <value>' argument missing")
            opts[token] = rest[i + 1]
            i += 2
        else:
            raise CliError(f"unknown option '{token}'")

    view = None
    if "--view" in opts:
        view = opts["--view"].strip()
        if view not in ("digest", "full"):
            raise CliError(f"Unknown --view value: {opts['--view']}. Valid: digest, full")
    if opts.get("--digest") and view == "full":
        raise CliError(
            "--digest and --view full ask for different row shapes. Pass one of them."
        )

    query: dict = {}
    if opts.get("--include-terminal"):
        query["includeTerminal"] = True
    if opts.get("--self"):
        query["scope"] = "self"
    if opts.get("--include-shared"):
        query["includeShared"] = True
    if opts.get("--digest"):
        query["view"] = "digest"
    if view:
        query["view"] = view
    if "--status" in opts:
        raw = opts["--status"]
        values = [v.strip() for v in raw.split(",") if v.strip()]
        valid = ", ".join(CLI_0_2_0_STATUSES)
        if not values:
            raise CliError(f'--status needs at least one status (got "{raw}"). Valid: {valid}')
        unknown = [v for v in values if v not in CLI_0_2_0_STATUSES]
        if unknown:
            plural = "s" if len(unknown) > 1 else ""
            raise CliError(f"Unknown --status value{plural}: {', '.join(unknown)}. Valid: {valid}")
        query["status"] = list(dict.fromkeys(values))
    for flag, key in (("--project", "projectId"), ("--bow", "bodyOfWorkId")):
        if opts.get(flag):
            query[key] = opts[flag]
    return query


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


def test_cli_0_2_0_advertises_everything_the_daemon_asks_for(cli):
    cli.help = HELP_0_2_0

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
    cli.help = HELP_0_2_0.replace("  --status", "   --status")

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
    cli.help = HELP_0_2_0

    argv = Alissa().task_list_argv()

    assert argv == PLAIN + ["--status", TASK_LIST_STATUS_FILTER, "--view", "digest"]
    assert set(TASK_LIST_STATUS_FILTER.split(",")) == OPEN_STATUSES


# -- and the filter has to be a vocabulary the CLI speaks (issue #97) --------


def test_the_open_set_and_its_filter_hold_only_canonical_alissa_statuses():
    """The pin the acceptance criterion asks for: against the CANONICAL set, not
    against OPEN_STATUSES.

    `is_open` is a client-side `in` over a set of strings and accepts anything;
    `--status` is validated against seven names. Asserting the filter equals the
    open set (above) therefore says nothing about whether the CLI would take it
    -- until the open set itself is pinned, which is what re-admitted `todo` for
    four releases.
    """
    assert OPEN_STATUSES <= CANONICAL_TASK_STATUSES, sorted(
        OPEN_STATUSES - CANONICAL_TASK_STATUSES
    )
    assert set(TASK_LIST_STATUS_FILTER.split(",")) <= CANONICAL_TASK_STATUSES
    assert CANONICAL_TASK_STATUSES == set(CLI_0_2_0_STATUSES), (
        "the daemon's canonical set and the vocabulary CLI 0.2.0 validates "
        "against are the same seven names"
    )


def test_a_non_canonical_status_added_to_the_open_set_disables_the_filter(caplog):
    """What stops a value like `todo` re-entering through `is_open`.

    The answer is deliberately NOT to intersect the filter with the canonical
    set: that would keep narrowing on a filter narrower than `is_open`, so a
    review task in the dropped status would never cross the wire and the daemon
    would read its absence as "this PR has no review task". Dropping the status
    filter entirely is wide but complete.
    """
    with caplog.at_level(logging.WARNING):
        assert _status_filter(OPEN_STATUSES | {"todo"}) == ""

    assert "todo" in caplog.text, "and it says which value cost the narrowing"
    assert _status_filter(OPEN_STATUSES) == TASK_LIST_STATUS_FILTER


def test_the_narrowed_call_is_one_cli_0_2_0_accepts(cli):
    """The Definition of Done, end to end: what the daemon assembles against a
    0.2.0 help is a call 0.2.0 takes -- so `list_tasks` never sees the
    `CommandError` that turns the narrowing off for the process."""
    cli.help = HELP_0_2_0

    argv = Alissa().task_list_argv()

    assert argv == PLAIN + [
        "--status", "committed,in_progress,pending_validation", "--view", "digest",
    ]
    assert cli_0_2_0_task_list(argv) == {
        "view": "digest",
        "status": ["committed", "in_progress", "pending_validation"],
    }


def test_the_narrowed_call_this_daemon_used_to_build_is_rejected(cli, monkeypatch):
    """The counterfactual, without which the test above passes on any filter.

    This is the argv of revloop 0.21.0 and earlier -- the open set with `todo`
    in it -- put through the same acceptance harness. CLI 0.2.0 refuses it
    before issuing a request, which is issue #97: not a coarser filter, no
    filter, no digest view, and one wasted subprocess per daemon process.
    """
    cli.help = HELP_0_2_0
    monkeypatch.setattr(
        alissa_module,
        "TASK_LIST_STATUS_FILTER",
        "committed,in_progress,pending_validation,todo",
    )

    argv = Alissa().task_list_argv()

    with pytest.raises(CliError) as excinfo:
        cli_0_2_0_task_list(argv)
    assert "Unknown --status value: todo" in str(excinfo.value)


def test_a_filter_the_cli_would_refuse_is_not_sent_at_all(cli, monkeypatch):
    """`--status ""` is the rejected call with an extra step, and the rest of
    the narrowing is worth keeping on its own."""
    cli.help = HELP_0_2_0
    monkeypatch.setattr(alissa_module, "TASK_LIST_STATUS_FILTER", "")

    argv = Alissa().task_list_argv()

    assert argv == PLAIN + ["--view", "digest"]
    assert cli_0_2_0_task_list(argv) == {"view": "digest"}


def test_the_digest_view_needs_no_knob(cli):
    """The daemon keeps taskNumber/title/status and nothing else, so a leaner
    projection is pure saving with no semantics to get wrong."""
    cli.help = HELP_0_2_0.replace(
        "  --self                 Only your own actor's rows — skip the sponsor's corpus\n"
        "                         (agent tokens). Pair with --include-shared\n",
        "",
    )

    assert Alissa().task_list_argv() == PLAIN + [
        "--status", TASK_LIST_STATUS_FILTER, "--view", "digest",
    ]


def test_narrow_status_false_drops_only_the_status_filter(cli):
    """`alissa-pr-review` reads a review task whose status `is_open` rejects, so
    it opts out of that one narrowing and keeps every other."""
    cli.help = HELP_0_2_0

    argv = Alissa(task_list_self_scope=True).task_list_argv(narrow_status=False)

    assert argv == PLAIN + ["--self", "--view", "digest"]


def test_list_tasks_makes_the_narrowed_call_and_parses_it(cli):
    cli.help = HELP_0_2_0
    narrowed = tuple(PLAIN + ["--status", TASK_LIST_STATUS_FILTER, "--view", "digest"])
    cli.answers[narrowed] = ROWS

    tasks = Alissa().list_tasks()

    assert [t.ref for t in tasks] == ["TASK-500"]
    assert cli.calls == [list(narrowed)]


# -- runtime disproof: a flag the CLI advertises but the API does not serve --


def test_a_narrowed_call_that_fails_retries_plain_and_stops_narrowing(cli):
    cli.help = HELP_0_2_0
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
    cli.help = HELP_0_2_0
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
    cli.help = HELP_0_2_0
    narrowed = tuple(PLAIN + ["--status", TASK_LIST_STATUS_FILTER, "--view", "digest"])
    cli.answers[narrowed] = []
    client = Alissa()

    assert [t.ref for t in client.list_tasks()] == ["TASK-500"]
    assert cli.calls == [list(narrowed), PLAIN]

    client.list_tasks()
    assert cli.calls[-1] == PLAIN, "the narrowing is disproved for this process"


def test_a_genuinely_empty_corpus_costs_one_extra_list_and_keeps_narrowing(cli):
    cli.help = HELP_0_2_0
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
