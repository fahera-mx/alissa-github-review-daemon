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
from alissa.tools.github.revloop.config import (
    TASK_LIST_BOW_ENV,
    Config,
    env_task_list_bow_id,
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

# The help of the CLI installed TODAY, captured verbatim on 2026-08-28 -- still
# self-reporting version 0.1.0, and carrying three options the 2026-08-16
# capture above does not: `--digest`, `--project` and `--bow`.
#
# It is here because it is the evidence for issue #100. `--bow` is a flag the
# fleet's own `alissa` actually offers, so the probe gate has a real listing to
# answer against rather than an invented one -- and it doubles as the standing
# proof that `alissa --version` is not a capability signal for this CLI: the
# flag set moved and the version string did not.
HELP_LIVE_2026_08_28 = """Usage: alissa task list [options]

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
  --project <projectId>  Only tasks in this project's bodies of work (Convex
                         project id)
  --bow <bowId>          Only tasks in this body of work (Convex BOW id)
  -h, --help             display help for command
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


def test_the_2026_08_16_cli_advertised_only_self(cli):
    """The measured baseline. Anything else the daemon can ask for is absent
    from this CLI, so the probe must say so rather than assume."""
    flags = Alissa().probe_task_list()

    assert flags == TaskListFlags(status=False, self_scope=True, digest=False, bow=False)


def test_todays_cli_advertises_self_and_bow(cli):
    """And what the SAME self-reported version offers twelve days later.

    `--bow` is found; `--status` and `--view` still are not (the live listing
    ships a boolean `--digest`, which is a different option and deliberately not
    what the probe looks for). `--project` is advertised too and stays unread --
    the daemon has no project to scope to, and probing for a flag it would never
    send is how a probe grows answers nothing consumes.
    """
    cli.help = HELP_LIVE_2026_08_28

    flags = Alissa().probe_task_list()

    assert flags == TaskListFlags(status=False, self_scope=True, digest=False, bow=True)


def test_an_older_cli_advertises_nothing(cli):
    cli.help = HELP_OLD

    assert Alissa().probe_task_list() == TaskListFlags()


def test_cli_0_2_0_advertises_everything_the_daemon_asks_for(cli):
    cli.help = HELP_0_2_0

    assert Alissa().probe_task_list() == TaskListFlags(
        status=True, self_scope=True, digest=True, bow=True
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


def test_a_non_canonical_status_added_to_the_open_set_disables_the_filter():
    """What stops a value like `todo` re-entering through `is_open`.

    The answer is deliberately NOT to intersect the filter with the canonical
    set: that would keep narrowing on a filter narrower than `is_open`, so a
    review task in the dropped status would never cross the wire and the daemon
    would read its absence as "this PR has no review task". Dropping the status
    filter entirely is wide but complete.
    """
    assert _status_filter(OPEN_STATUSES | {"todo"}) == ""
    assert _status_filter(set()) == "", "and an empty set is not a filter either"
    assert _status_filter(OPEN_STATUSES) == TASK_LIST_STATUS_FILTER


def test_the_dropped_filter_is_reported_once_from_the_call_that_loses_it(
    cli, caplog, monkeypatch
):
    """Where the diagnostic is emitted, which is the whole of PR #98 round 1.

    It used to be emitted while this module was being IMPORTED -- which
    `__main__` does (through `.loop`) long before it calls
    `logging.basicConfig`, so the one warning the guard produces fell through to
    `logging.lastResort`: bare on stderr, outside the deployment's handlers.
    """
    cli.help = HELP_0_2_0
    monkeypatch.setattr(alissa_module, "TASK_LIST_STATUS_FILTER", "")
    monkeypatch.setattr(alissa_module, "NON_CANONICAL_OPEN_STATUSES", ("todo",))
    client = Alissa()

    with caplog.at_level(logging.WARNING):
        assert client.task_list_argv() == PLAIN + ["--view", "digest"]
        client.task_list_argv()

    assert caplog.text.count("todo") == 1, "once per client, not once per poll pass"


def test_a_cli_that_does_not_offer_status_is_not_warned_about(cli, caplog, monkeypatch):
    """And the message is only emitted when it is true: on a CLI without
    `--status` the filter costs nothing, so there is nothing lost to report."""
    cli.help = HELP_0_1_0
    monkeypatch.setattr(alissa_module, "TASK_LIST_STATUS_FILTER", "")
    monkeypatch.setattr(alissa_module, "NON_CANONICAL_OPEN_STATUSES", ("todo",))

    with caplog.at_level(logging.WARNING):
        assert Alissa().task_list_argv() == PLAIN

    assert "todo" not in caplog.text


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


def test_the_whole_narrowed_call_a_self_scoped_deployment_makes_is_accepted(cli):
    """Every narrowing at once -- the one argv a real deployment can emit that
    the acceptance harness never saw (PR #98 round 1). `--self` is a plain
    boolean on both sides, so the risk was low; the file's claim is that the
    argv is one the CLI ACCEPTS, and this is the shape that claim missed."""
    cli.help = HELP_0_2_0
    client = Alissa(task_list_self_scope=True)

    argv = client.task_list_argv()

    assert argv == PLAIN + [
        "--status", "committed,in_progress,pending_validation",
        "--self", "--view", "digest",
    ]
    assert cli_0_2_0_task_list(argv) == {
        "scope": "self",
        "view": "digest",
        "status": ["committed", "in_progress", "pending_validation"],
    }
    assert cli_0_2_0_task_list(client.task_list_argv(narrow_status=False)) == {
        "scope": "self",
        "view": "digest",
    }, "and the shape `alissa-pr-review` opts into is accepted too"


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


# -- the BOW scope (issue #100) ---------------------------------------------
#
# The one narrowing that chooses a candidate set instead of filtering one, and
# so the one whose failure mode is a review the daemon never sees rather than a
# list that is merely large. Everything below is about the two gates in front of
# it -- the CLI advertises `--bow`, AND the deployment named a BOW -- and about
# it composing with, rather than replacing, the flags that were already there.

BOW = "kt7c9m2q4x8n1v5b3z6w0y9r7s4d2f8g"


def test_a_bow_needs_both_the_flag_and_a_configured_id(cli):
    """Neither gate alone. An id on a CLI without `--bow` is an argv the CLI
    exits 1 on -- which this daemon turns into a SKIPPED decision -- and a CLI
    with `--bow` and no id has nothing to scope to."""
    cli.help = HELP_LIVE_2026_08_28
    assert Alissa().task_list_argv() == PLAIN, "advertised, but no id configured"
    assert Alissa(task_list_bow_id=BOW).task_list_argv() == PLAIN + ["--bow", BOW]

    cli.help = HELP_0_1_0
    assert Alissa(task_list_bow_id=BOW).task_list_argv() == PLAIN, "id set, flag absent"


def test_an_unset_bow_leaves_todays_call_byte_identical(cli):
    """The acceptance criterion the whole opt-in exists to protect: a deployment
    that says nothing gets the argv it got before this key existed, on every CLI
    -- including the one that would happily serve the flag."""
    for helptext, expected in (
        (HELP_OLD, PLAIN),
        (HELP_0_1_0, PLAIN),
        (HELP_LIVE_2026_08_28, PLAIN),
        (HELP_0_2_0, PLAIN + ["--status", TASK_LIST_STATUS_FILTER, "--view", "digest"]),
    ):
        cli.help = helptext
        for unset in (None, "", "   "):
            assert Alissa(task_list_bow_id=unset).task_list_argv() == expected, helptext


def test_the_bow_composes_with_every_other_narrowing_and_is_accepted(cli):
    """The maximally-narrowed call: BOW first because it picks the candidate
    set, then the filters that narrow within it. Put through the CLI's own parse
    so the claim is 'a call 0.2.0 takes', not 'the argv we meant to build'."""
    cli.help = HELP_0_2_0
    client = Alissa(task_list_self_scope=True, task_list_bow_id=BOW)

    argv = client.task_list_argv()

    assert argv == PLAIN + [
        "--bow", BOW,
        "--status", "committed,in_progress,pending_validation",
        "--self", "--view", "digest",
    ]
    assert cli_0_2_0_task_list(argv) == {
        "bodyOfWorkId": BOW,
        "scope": "self",
        "view": "digest",
        "status": ["committed", "in_progress", "pending_validation"],
    }
    assert cli_0_2_0_task_list(client.task_list_argv(narrow_status=False)) == {
        "bodyOfWorkId": BOW,
        "scope": "self",
        "view": "digest",
    }, "and `alissa-pr-review`'s shape keeps the BOW, dropping only the status filter"


def test_a_bow_on_todays_cli_is_the_only_narrowing_it_can_send(cli):
    """What the fleet would actually emit if the key were set right now: today's
    CLI offers `--self` and `--bow` and neither of the other two."""
    cli.help = HELP_LIVE_2026_08_28

    argv = Alissa(task_list_bow_id=BOW).task_list_argv()

    assert argv == PLAIN + ["--bow", BOW]
    assert cli_0_2_0_task_list(argv) == {"bodyOfWorkId": BOW}


def test_a_disproved_narrowing_drops_the_bow_with_everything_else(cli):
    """A BOW that lists EMPTY is the shape a mistyped id takes -- and it is
    caught by the machinery that was already there, because an empty narrowed
    answer is never taken at face value. The retry is the plain call, so the
    daemon degrades to seeing every review task rather than none."""
    cli.help = HELP_LIVE_2026_08_28
    narrowed = tuple(PLAIN + ["--bow", BOW])
    cli.answers[narrowed] = []
    client = Alissa(task_list_bow_id=BOW)

    assert [t.ref for t in client.list_tasks()] == ["TASK-500"]
    assert cli.calls == [list(narrowed), PLAIN]

    assert client.task_list_argv() == PLAIN, "and the BOW is not sent again"


# -- where the id comes from: file < CLI < env ------------------------------


def test_the_bow_id_defaults_to_unset(tmp_path):
    assert Config.build(tmp_path, {}, {}, {}).task_list_bow_id is None
    assert Config.build(tmp_path, environ={}).task_list_bow_id is None


def test_a_cli_flag_beats_the_config_file(tmp_path):
    cfg = Config.build(tmp_path, {"task_list_bow_id": "from-file"}, {}, {})
    assert cfg.task_list_bow_id == "from-file"

    cfg = Config.build(
        tmp_path, {"task_list_bow_id": "from-file"}, {"task_list_bow_id": BOW}, {}
    )
    assert cfg.task_list_bow_id == BOW


def test_the_environment_beats_both(tmp_path):
    """The layer this key has and the others do not. It is what reaches
    `alissa-pr-review`, which has neither of the other two."""
    cfg = Config.build(
        tmp_path,
        {"task_list_bow_id": "from-file"},
        {"task_list_bow_id": "from-cli"},
        {TASK_LIST_BOW_ENV: BOW},
    )

    assert cfg.task_list_bow_id == BOW


def test_an_empty_value_means_unset_in_every_layer(tmp_path):
    """`--bow ""` is a call the CLI takes and answers with nobody's tasks, and
    an exported-but-empty variable is how a container spells "not configured"."""
    assert env_task_list_bow_id({TASK_LIST_BOW_ENV: ""}) is None
    assert env_task_list_bow_id({TASK_LIST_BOW_ENV: "  "}) is None
    assert env_task_list_bow_id({}) is None

    assert Config.build(tmp_path, {"task_list_bow_id": "  "}, {}, {}).task_list_bow_id is None
    cfg = Config.build(
        tmp_path, {"task_list_bow_id": BOW}, {}, {TASK_LIST_BOW_ENV: "  "}
    )
    assert cfg.task_list_bow_id == BOW, "an empty variable does not blank a configured id"


def test_the_id_is_stripped_wherever_it_comes_from(tmp_path):
    assert env_task_list_bow_id({TASK_LIST_BOW_ENV: f"  {BOW}\n"}) == BOW
    assert Config.build(tmp_path, {"task_list_bow_id": f" {BOW} "}, {}, {}).task_list_bow_id == BOW
    assert Alissa(task_list_bow_id=f" {BOW} ").task_list_bow_id == BOW


def test_an_unknown_config_key_is_still_refused(tmp_path):
    """The new key is in CONFIG_KEYS, so its near-misses are not."""
    Config.build(tmp_path, {"task_list_bow_id": BOW}, {}, {})
    with pytest.raises(ValueError, match="unknown config key"):
        Config.build(tmp_path, {"task_list_bow": BOW}, {}, {})
