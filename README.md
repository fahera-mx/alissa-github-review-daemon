# Alissa — GitHub Review Daemon

A GitHub watcher that drives the [`alissa-code-review`](https://skills.alissa.app/)
adversarial review loop (CR1-CR9) to convergence.

The skill lists trigger automation as a planned tier ("a CI job on
`pull_request.ready_for_review` ... is not part of this skill's contract"). This
is that tier, as a polling daemon instead of a webhook.

Shipped as the module `alissa.tools.github.reviewloop`, in the distribution
`alissa-tools-github-reviewloop/`. `alissa.tools.github` is a PEP 420 namespace
other repos can extend — see that package's README.

## What it does

One poll pass:

1. Ask GitHub for PRs with a review pending from you.
2. For each, work out which round is owed.
3. Enqueue a **fresh** reviewer session for that round via `alissa tmux queue add`.
4. Stop at `approve`, or escalate at the round cap.

```
gh api search/issues            →  PRs awaiting my review (draft:false → CR1)
  ↓
alissa task list                →  find the review task (CR2 dedupe)
  ↓
alissa task get  (its verdicts) →  how many rounds are done? → round k
  ↓
alissa tmux queue add           →  fresh reviewer, round k (CR3)
```

## The key design decision: GitHub triggers, the task counts

GitHub **clears** a pending review request the moment you submit a review, and
**re-adds** it when the implementer re-requests after fixes. So
`review-requested:@me` is already an edge-trigger for CR9 rounds — no webhook and
no diffing needed. That is what fires a round.

The round *number* is derived from the **review task's verdict envelopes** — one
append-only envelope per round (CR7), the authoritative round record:
`round = (verdict envelopes on the review task) + 1`. Before the review task
exists (round 1) it falls back to the GitHub substantive-review count.

> Earlier this counted GitHub reviews directly (`round = substantive reviews + 1`).
> That is a fragile proxy: a round whose review has an empty top-level body
> undercounts (the round number *repeats* → the session name collides → the worker
> wedges), and two reviews in one cycle overcount. The verdict envelope is exactly
> one-per-round, so counting it can't drift.

The local SQLite ledger holds only what neither can: which round is currently *in
flight* (so a 60s poll doesn't spawn the same reviewer twice), which cap-outs were
escalated, and which finished sessions were reaped.

CR3's "fresh instance per round" falls out for free — each trigger spawns a new
`ali-*` session, named `review-<repo>-pr<n>-r<k>-<nonce>`. The `<nonce>` makes the
name unique per spawn, so even a miscounted round can never collide with a
still-live session.

## Setup

```sh
python -m venv venv && source venv/bin/activate
pip install -r requirements-develop.txt
pip install -e ./alissa-tools-github-reviewloop

cd <your-workspace>
alissa-reviewloop --once --dry-run -v      # runs on defaults; no config needed
```

`alissa worker` must be running or queued sessions never spawn — the daemon warns
at startup if it isn't.

```sh
alissa-reviewloop         # foreground; tip: run it in its own tmux session
```

### In a container

To run the whole loop unattended in Docker — the poller, an `alissa worker`, and
the `claude` reviewer it spawns, bundled in one image — see
[`docker/claude/`](./docker/claude/README.md).

### Settings

Three layers, each winning over the one before: **defaults → config file → CLI**.

`workspace_root` is **not** a config key — it is a property of the running
process, given by `--workspace-root` and defaulting to the current directory.
That is what lets one config file drive several daemons over different
workspaces on the same machine:

```sh
alissa-reviewloop --workspace-root ~/ws/alpha --repo org/alpha-api &
alissa-reviewloop --workspace-root ~/ws/beta  --repo org/beta-web &
```

Every key below also exists as a CLI flag (`--poll-interval`, `--repo`, …), and
the flag wins. `--repo` is repeatable and *replaces* the config list rather than
extending it. `--dry-run` / `--no-dry-run` override the config in both directions.

| key / flag | default | meaning |
| --- | --- | --- |
| `--workspace-root` | cwd | root of the worktree-hub workspace (**CLI only**) |
| `hub_template` | `{root}/{repo}/main` | reviewer cwd — the pristine `main/` mirror (CR6: reviewers never write) |
| `poll_interval` | `60` | seconds; must be ≥10 |
| `round_cap` | `3` | CR9 cap; never queues round cap+1 |
| `repos` | `[]` | allowlist of `owner/repo`; empty = all |
| `agent_profile` | `claude` | agent the worker launches for reviewer sessions |
| `reviewer_login` | `null` | resolved from `gh api user` when null |
| `state_path` | `<workspace-root>/.reviewloop/state.db` | spawn ledger; per-workspace by default so parallel daemons never share one |
| `on_missing_review_task` | `spawn_anyway` | `spawn_anyway` \| `warn_and_spawn` \| `skip` |
| `on_missing_hub` | `skip` | `skip` \| `add` — see *Provisioning new repos* |

### Config file discovery

`--config-path PATH`, else `./reviewloop.config.json`, else
`<workspace-root>/reviewloop.config.json`. If none exists the daemon runs on
defaults plus CLI arguments — a config file is optional. An explicit
`--config-path` that does not exist is an error rather than a silent fallback.

Copy `reviewloop.config.example.json` to start from a documented template.

## Identity

Everything is **relative to the gh token**. `review-requested:@me` resolves
server-side from whoever `gh` is authenticated as, and `reviewer_login` defaults
to `gh api user`. Re-authenticating gh, or setting `GH_TOKEN` in the daemon's
environment, silently changes whose review queue is watched.

Your two identities are independent and nothing keeps them in sync:

| | identity | used for |
| --- | --- | --- |
| `gh` | `alissa-app` | the review queue, PR comments, round counting |
| `alissa` | Alissa by Fahera | tasks, session queue, verdicts |

Because of that, a `reviewer_login` that disagrees with the token is **fatal at
startup**, not a warning: the search would follow the token while round counting
followed the config, so every round would look like round 1 and respawn forever.

## Provisioning new repos

By default a review for a repo with no worktree hub is **skipped**, with the
`alissa code workspace add` command to fix it in the log.

Set `on_missing_hub: "add"` and the daemon hub-ifies the repo itself (bare clone
+ `main/` worktree + manifest entry) before spawning the reviewer. This is
deliberately gated, because hub-ifying clones code onto the machine and opens it
as an agent's working directory — and the trigger is an *inbound* request from
someone else:

- it requires a non-empty `repos` allowlist (config load fails otherwise);
- it refuses to run outside a real workspace (no `alissa-workspace.yaml`);
- if the CLI reports success but the hub still isn't there, it reports that
  rather than spawning an agent into a missing directory.

Leave it on `skip` unless you want unattended clones.

## Behaviour

| situation | action |
| --- | --- |
| pending request, no prior review | spawn round 1 |
| pending request, k−1 reviews submitted | spawn round k (round-k directive: verify triage, verify fixes, sweep delta) |
| round already enqueued | in-flight, no-op |
| round enqueued >90 min, still no review | reviewer presumed stalled, re-enqueue |
| a round's review has landed | its reviewer session is reaped (freed) — see below |
| approve (GitHub state or verdict envelope) **for the current head** | converged, no-op |
| approve, but new commits landed since it was written | **not** converged — the approval is head-bound, so the next round is owed |
| `round_cap` reviews, no approve | comment cap-out on the PR, escalate, stop |
| new commits after a cap-out | re-escalate (head moved, decision is about the new state) |
| PR is a draft | skip (CR1) |
| PR authored by the reviewer identity | skip — GitHub forbids self-review |
| repo has no worktree hub | skip, or hub-ify first if `on_missing_hub: "add"` |

`COMMENTED` reviews close a round, not just `APPROVED`/`CHANGES_REQUESTED` —
single-operator workspaces post comment-mode reviews per CR5, and the loop must
still advance.

### Reaping finished reviewer sessions

Reviewers are one-shot per round (CR3), but a finished `claude` sits idle at its
prompt — the session is not *empty*, so `alissa tmux cleanup` (which only reaps
empty sessions after a long idle) never frees it, and slots pile up. Two things
prevent that:

- **Fast path — the reviewer self-kills.** Its directive's final action, once the
  round is fully closed, is `alissa tmux kill <its own session>`.
- **Backstop — the daemon reaps.** On each poll it kills the session of any round
  whose review has already landed (round ≤ submitted-review count), idempotently
  (a per-session `reaps` ledger), skipped in `--dry-run`. This covers the case
  where the reviewer forgets to self-kill.

`enqueue_reviewer` sets the reviewer queue's `respawn off`, so a kill (from either
path) can never trigger a respawn loop.

## Scope

The daemon (`alissa-reviewloop`) is the **reviewer side**. It reacts to review
requests and spawns reviewers; it never pushes, merges, or changes PR state — it
only enqueues reviewers and, on cap-out, comments. Reviewer posture (CR6) is
enforced in every directive.

The **implementer side** — triaging findings (CR8), fixing, re-requesting — stays
with the implementer per the `alissa-code-review` skill's
`procedures/run-the-review-loop.md`. The `alissa-pr-review` command below is a
thin driver for it.

## Closing the loop: `alissa-pr-review` (implementer side)

The daemon closes the *reviewer* half autonomously, but nothing tells the **dev**
when a review lands. `alissa-pr-review` is the counterpart the dev session runs
after finishing the work: it fires the trigger and blocks on the verdict, so the
loop closes without a second always-on daemon.

```sh
alissa-pr-review --reviewer alissa-app --branch TASK-123-FIX-THING --timeout 2700
```

One invocation = **one round**:

1. resolve the PR from the branch (or the current branch);
2. flip it **ready-for-review** (from draft);
3. **request the reviewer** — this is exactly the daemon's `review-requested:@me`
   edge-trigger, so the reviewer daemon takes it from here;
4. block until a new review round lands, then read the verdict — from the **review
   task envelope**, never GitHub's review state (reviewers comment-mode, so the
   state is always `COMMENTED`). It reuses the daemon's `latest_verdict` /
   round-counting, so the two halves can't disagree.

Exit codes drive the loop: **`0` approve** (converged), **`1` request_changes**,
**`2` timeout / no verdict**, **`3` usage or setup error** (including the
self-review guard — GitHub forbids requesting review from the PR author, so the
dev's `gh` account must differ from `--reviewer`).

### The loop (HOW-TO)

The command is one round; the loop and the cap live around it:

```sh
CAP=3
for round in $(seq 1 "$CAP"); do
  alissa-pr-review --reviewer alissa-app --branch "$(git branch --show-current)"
  case $? in
    0) echo "converged (approve)"; break ;;
    1) # triage every finding on its PR thread ([triage:pursue|ignore|later|answer],
       # reasoning mandatory), fix the pursued ones, commit, push — then loop.
       echo "round $round: request_changes — triage, fix, push, re-enter" ;;
    2) echo "no verdict yet (timeout) — reviewer may be slow; re-run or check the worker"; break ;;
    *) echo "setup error"; break ;;
  esac
done
```

The `2700`s (45 min) timeout is shorter than the daemon's 90-min stall
re-enqueue, so a timeout means *"no verdict yet,"* not *"failed."* The triage
taxonomy and cap-out escalation are defined in the `alissa-code-review` skill.

The daemon never pushes, merges, or changes PR state; it only enqueues reviewers
and, on cap-out, comments. Reviewer posture (CR6) is enforced in every directive.

## Environment notes

- `gh` 2.4.0 (Ubuntu 2022 build) predates `gh search`, so all queries go through
  `gh api`. Nothing else here depends on a newer gh.
- Search API allows 30 req/min; one pass costs 1 search + 2 core calls per PR.
  Rate limits trigger exponential backoff to a 15 min ceiling.
- No third-party dependencies; `pytest` only for tests.

## Tests

```sh
bash tests-unit.sh alissa-tools-github-reviewloop
bash tests-coverage.sh alissa-tools-github-reviewloop
bash check-style.sh alissa-tools-github-reviewloop
bash check-types.sh alissa-tools-github-reviewloop
```

96 tests cover the decision state machine, the config layering, and the
`alissa-pr-review` round/verdict/timeout logic, with GitHub and Alissa faked.

**Verified live:** the search query, login resolution, PR/review fetching,
`alissa task list` parsing, review-task title matching, worker detection, the
identity-mismatch guard, and the workspace preflight.

**Not verified live:** the trigger firing on a real review request, and the spawn
actually reaching a tmux session. Both need a PR authored by an account *other*
than the reviewer identity — GitHub forbids requesting review from the PR author,
so a self-authored PR never fires the trigger at all. Run `--once --dry-run -v`
against the first real request before letting it run unattended.
