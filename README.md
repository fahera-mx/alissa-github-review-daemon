# Alissa — GitHub Review Daemon

A GitHub watcher that drives the [`alissa-code-review`](https://skills.alissa.app/)
adversarial review loop (CR1-CR9) to convergence.

The skill lists trigger automation as a planned tier ("a CI job on
`pull_request.ready_for_review` ... is not part of this skill's contract"). This
is that tier, as a polling daemon instead of a webhook.

Shipped as the module `alissa.tools.github.revloop`, in the distribution
`alissa-tools-github-revloop/`. `alissa.tools.github` is a PEP 420 namespace
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
alissa task list                →  find the review task (CR2 dedupe) — cached,
                                   memoized and negative-cached, so this is the
                                   rare path (Bounding the task-list read)
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
pip install -e ./alissa-tools-github-revloop

cd <your-workspace>
alissa-revloop --once --dry-run -v      # runs on defaults; no config needed
```

`alissa worker` must be running or queued sessions never spawn — the daemon warns
at startup if it isn't.

```sh
alissa-revloop         # foreground; tip: run it in its own tmux session
```

### In a container

To run the whole loop unattended in Docker — the poller, an `alissa worker`, and
the `claude` reviewer it spawns, bundled in one image — see
[`docker/claude/`](./docker/claude/README.md).

That image also serves a second, separate role: `CONTAINER_ROLE=executor` runs
`alissa bridge start` (an Alissa Studio queue executor) instead of the review
loop, deployed as its own service so hours-long queue jobs never share a restart
domain with the daemon. See
[Bridge executor role](./docker/claude/README.md#bridge-executor-role-a-second-service-from-this-same-image).

### Settings

Three layers, each winning over the one before: **defaults → config file → CLI**.

`workspace_root` is **not** a config key — it is a property of the running
process, given by `--workspace-root` and defaulting to the current directory.
That is what lets one config file drive several daemons over different
workspaces on the same machine:

```sh
alissa-revloop --workspace-root ~/ws/alpha --repo org/alpha-api &
alissa-revloop --workspace-root ~/ws/beta  --repo org/beta-web &
```

Every key below also exists as a CLI flag (`--poll-interval`, `--repo`, …), and
the flag wins. `--repo` is repeatable and *replaces* the config list rather than
extending it. `--dry-run` / `--no-dry-run` override the config in both directions.
Two keys have a third layer above both, the environment: `task_list_bow_id`
(`ALISSA_REVIEW_TASK_BOW`; see *Naming the review BOW* for why) and
`loop_events_enabled` (`ALISSA_REV_LOOP_EVENTS_ENABLED`; see *Loop telemetry*).
The environment wins over the file **and** the flags for both.

| key / flag | default | meaning |
| --- | --- | --- |
| `--workspace-root` | cwd | root of the worktree-hub workspace (**CLI only**) |
| `hub_template` | `{root}/{repo}/main` | reviewer cwd — the pristine `main/` mirror (CR6: reviewers never write) |
| `poll_interval` | `60` | seconds; must be ≥10 |
| `round_cap` | `10` | CR9 cap; never queues round cap+1 |
| `stability_rounds` | `3` | **product-stability guard** (CR9 converged-by-stability): once the shipped-product diff between the head judged this many `request_changes` rounds ago and the current head is *empty*, the next round is queued carrying a **PRODUCT-STABILITY NOTICE**; if that grace round comes back `request_changes` with the product still unmoved, no further round is queued, the operator is paged once per head, and the same `alissa-review: re-enter +N` ack lifts it. A push that moves a shipped file clears the hold by itself. `0` disables the guard entirely — no comparison call, no notice, no hold |
| `stability_nonshipped_globs` | `tests/**`, `test/**`, `**/*.test.*`, `**/*.spec.*`, `**/*.md`, `docs/**`, `**/__snapshots__/**`, `**/_generated/**` | what the guard above does **not** count as product movement. `**` crosses directory separators (so `**/*.test.*` matches `src/a/b/x.test.ts`, and `**/*.md` matches a top-level `README.md`); every other segment is an ordinary `fnmatch` pattern that cannot. Anything unmatched is **shipped** — a shipped file that changed *at all* is movement, comment-only hunks included |
| `repos` | `[]` | allowlist of `owner/repo`; empty = all |
| `authors` | `[]` | allowlist of GitHub logins whose PRs are reviewed; empty = all. A **scope filter, not the security boundary** — see *Who the loop serves* |
| `operators` | `[]` | GitHub logins whose re-entry ack may re-open a capped PR; empty = none |
| `agent_profile` | `claude` | agent the worker launches for reviewer sessions |
| `reviewer_login` | `null` | the identity every verdict is posted under; resolved from `gh api user` when null |
| `reviewer_token_env` | `null` | **name** of the env var holding the reviewer's GitHub token (never the token). Set it and every `gh` call runs under that credential explicitly; leave it null and the daemon inherits — and warns |
| `state_path` | `<workspace-root>/.revloop/state.db` | spawn ledger; per-workspace by default so parallel daemons never share one |
| `on_missing_review_task` | `spawn_anyway` | `spawn_anyway` \| `warn_and_spawn` \| `skip` |
| `on_missing_hub` | `skip` | `skip` \| `add` — see *Provisioning new repos* |
| `reap_grace_seconds` | `1800` | how long a reviewer session must be idle **and** quiet before the sweep reaps it (and before a stale round reads it as dead); must be **well under** the 90-minute stale-round window, which the loader enforces |
| `reap_session_cap` | `6` | more live reviewer sessions than this after a sweep and the daemon logs page-worthy; an alarm threshold, not a capacity limit |
| `max_concurrent_sessions` | `4` | **spawn gate**: at this many live reviewer sessions of this daemon's own grammar, an owed round *waits* instead of spawning and is retried next poll. Deferral burns no round number and no attempt, trips no stale-round respawn, and pages nobody. Must be **≤ `reap_session_cap`**, which the loader enforces — the cap is the alarm, this is the limit |
| `checks_wait_seconds` | `1800` | how long a round holds its **approve** while the judged head's CI rollup is still running (or unreadable) before recording the verdict as a `COMMENT` instead. Applies **per condition waited on**: an unreadable hold that becomes a genuine *pending* one restarts the clock once, so the worst-case hold is **twice** this. A **red** rollup never waits and never approves — see *Never approve a red head* |
| `checks_spawn_wait_seconds` | `900` | **pre-spawn CI gate**: how long an owed round waits for the head's checks to *conclude* before its reviewer is queued at all. The key above gates the verdict the *daemon* posts; this is the only structural gate on the verdict a reviewer *session* posts. Past the bound the round is queued anyway, told it may not approve. `0` disables the *hold* and relies on the directive alone. **It also bounds the reviewer session's own in-round wait**: the same number is written into every directive as how long a session may wait for a running check before submitting, floored at 5 minutes so a `0` here cannot read as "do not wait at all" — see *Never approve a red head* |
| `review_task_miss_ttl_polls` | `10` | how many polls a PR with **no** review task is taken on trust before the task corpus is searched for one again. Trades **latency** for reads: a review task created mid-window is picked up at the re-arm rather than the next poll. Floor `1` — there is no value that turns it off — see *Bounding the task-list read* |
| `task_list_self_scope` | `false` | narrow `alissa task list` to this actor's own rows (`--self`). **Off by default on evidence**: a small minority of review tasks on the live fleet are owned by another actor, and a review task the list cannot see is a round the daemon cannot count — see *Bounding the task-list read* |
| `task_list_bow_id` | `null` | scope `alissa task list` to one body of work (`--bow`), so candidates come from that BOW's junction rows instead of the operator's whole involvement index. The **only key the environment can set** (`ALISSA_REVIEW_TASK_BOW`, which wins over both the file and `--task-list-bow`). Off by default: a review task **outside** the configured BOW is invisible to the daemon, which on the default `on_missing_review_task` means a round spawned *untethered from its task* — see *Bounding the task-list read* for the id's contract, the two ways to get it wrong, and what `--bow` does to the other narrowing flags |
| `loop_events_enabled` | `false` | push loop telemetry (rounds spawned, verdicts posted, cap-outs, stability holds, stalls, checks holds, grants, reaps) to Studio's `POST /v1/loop-events` **once per poll pass** — one idempotent, ledger-derived batch, best-effort and never fatal. Settable by the environment (`ALISSA_REV_LOOP_EVENTS_ENABLED`, which wins over the file and `--loop-events`/`--no-loop-events`) — see *Loop telemetry (Studio ingest)* |
| `alissa_endpoint` | `https://api.alissa.app` | the Alissa API base the loop-events client posts to; the token is the CLI's own `ALISSA_API_TOKEN` from the environment |

#### Who the loop serves

`repos` and `authors` are both **scope filters**, and both read the same way:
**empty means everything**, so a config with neither key reviews every PR that
requests this reviewer. `operators` is the odd one out and deliberately so — it
is a *grant* (whose ack may lift CR9's cap), and empty there means **nobody**.

`authors` is a **policy knob, not the security boundary**. Summoning this loop
already costs repo write access — someone has to request the reviewer — plus a
place on the `repos` allowlist. What `authors` decides is which of those
already-authorised PRs are worth spending rounds on: only the devloop
identity's, say, or everything except Dependabot and renovate.

- Matching is **case-insensitive** (GitHub logins are).
- A filtered PR is skipped **before any round bookkeeping** — no reviewer, no
  round number, no attempt record, and **no comment on the PR**. Silence, like
  an unwatched repo: an author this loop does not serve should not be handed an
  interaction surface.
- It gates *starting* rounds only, and sits below the branches that *close* a
  round for exactly that reason. Drop an author from the list mid-round and the
  round already in flight still finishes: its session is not reaped, its verdict
  still becomes a review of record, and a converged PR still has its dangling
  review request withdrawn. What it costs is that a filtered PR is not the
  cheapest possible skip — it pays one review-list fetch and the (cached)
  review-task lookup per poll before the gate is reached.
- No entry in it can override the **self-review** skip: listing the reviewer's
  own login narrows the loop to a PR GitHub then forbids it to review.
- `--author` is repeatable and *replaces* the config list, exactly like
  `--repo`.

### Config file discovery

`--config-path PATH`, else `./revloop.config.json`, else
`<workspace-root>/revloop.config.json`. If none exists the daemon runs on
defaults plus CLI arguments — a config file is optional. An explicit
`--config-path` that does not exist is an error rather than a silent fallback.

Copy `revloop.config.example.json` to start from a documented template.

## Identity

Everything is **relative to the gh token**. `review-requested:@me` resolves
server-side from whoever `gh` is authenticated as, and `reviewer_login` defaults
to `gh api user`. Re-authenticating gh, or setting `GH_TOKEN` in the daemon's
environment, silently changes whose review queue is watched.

Your two identities are independent and nothing keeps them in sync:

| | identity | used for |
| --- | --- | --- |
| `gh` | `alissa-app` | the review queue, PR comments, the GitHub-side convergence signal |
| `alissa` | Alissa by Fahera | tasks, session queue, verdicts |

Because of that, a `reviewer_login` that disagrees with the token is **fatal at
startup**, not a warning: the search would follow the token while round counting
followed the config, so every round would look like round 1 and respawn forever.

### The verdict of record, and whose credential writes it

A round is **not complete until its verdict exists as a native GitHub review
submitted by the reviewer identity** — `approve` → `APPROVE`,
`request_changes` → `REQUEST_CHANGES`. Anything else a session writes is a
transcript artifact, however complete: only a review by the *requested*
reviewer expresses the verdict on GitHub and consumes the pending review
request.

That is not a formality. On studio #298 the round's only artifact was a
`COMMENTED` review by the **implementer** identity, because the reviewer
session's `gh` inherited the container's default credential. Consequences, all
silent: the PR showed no verdict, the ready-flip review request was never
consumed, and the daemon re-verified the closed round on every poll.

So the daemon owns that post itself. When a review task carries more verdict
envelopes than the PR has reviewer reviews, the daemon submits the missing one
after a short grace window (long enough for the session to submit its own
review first, if it is going to):

- the posting path re-reads `GET /user` and **refuses** to submit under any
  login other than the configured reviewer — a wrong-identity post is worse
  than a late one;
- a post that fails is retried on a growing backoff, and pages the PR after
  five attempts. The round stays **open** throughout: no convergence, no next
  round, no cap-out — a missing verdict of record stalls the loop visibly
  rather than closing it invisibly. The one exception is a post that can never
  succeed: if a force-push removed the commit the verdict judged, the post is
  **abandoned** (recorded, and logged in the activity comment) and the round is
  released, so a fresh round runs against the new head instead of the PR
  stalling out of the loop forever;
- each daemon post carries a hidden `round=k` marker, so a session's own review
  and the daemon's post for the same round count as **one** round against the
  cap.

`reviewer_token_env` is the other half. It names the environment variable
holding the reviewer's token, and with it set every `gh` call the daemon makes
runs under an environment built from that variable with the inherited
`GH_TOKEN`/`GITHUB_TOKEN` stripped — no ordering by which a container default
can win. Reviewer *sessions* are told the variable's name in their directive
(`GH_TOKEN="$VAR" gh …`), since `alissa tmux queue add` has no env-injection
flag and a session otherwise inherits the worker's environment; the daemon's own
post is the guarantee either way.

In the container, set `ALISSA_REVIEWER_TOKEN_ENV` to the variable name and
inject the token at runtime like any other secret. The entrypoint resolves the
login at boot and refuses to start if the variable is empty, the token is
rejected, or it belongs to someone other than `ALISSA_REVIEWER_LOGIN`.

### Never approve a red head: the CI checks gate

An `APPROVE` from the reviewer identity is the operator's cue to merge, so it
has to mean **reviewed AND green**. On studio #323 it did not: the head's `test`
check failed at 15:27Z and the round approved at 18:50Z — 3.4 hours later —
because nothing consulted the checks. The operator's merge gate received an
"approved, ready" PR that was unmergeable-red, and the failure sat unaddressed
until a human noticed.

So before a round concludes with an approve, the daemon reads the rollup **of
the commit the verdict is pinned to** (`GET /commits/<sha>/check-runs` plus
`/status` — never "the PR's checks", because approving commit A on commit B's
rollup is the same error as stamping an old verdict onto a new head):

| rollup at the judged head | what the round does |
| --- | --- |
| success — including `skipped`/`neutral` contexts (path-filtered matrix jobs) and commits with no checks at all | approves exactly as before |
| still running, or unreadable | **holds** the round open, re-checking each poll, up to `checks_wait_seconds`; one log line per poll and **one** note in the activity comment, no comment of its own |
| still unsettled at that bound | records the verdict as a `COMMENT` saying the checks never concluded — never an approve on an unverified head — **and pages the operator once**, see below |
| failure/error | posts `REQUEST_CHANGES` leading with the failing check names and run URLs, and no approve |

> **The bound is per condition, so the worst case is 2×.** An unreadable rollup
> and a genuinely pending one are different waits: a transient read error must
> not eat the bound a real check suite is entitled to (that was a review finding
> against the first version), so the clock restarts **once** when an `unknown`
> hold is promoted to a `pending` one — and never again, because a reader
> flapping between the two would otherwise push the bound out forever. With the
> 30-minute default, a rollup that is unreadable for 29 minutes and then starts
> reporting a running suite is held ~59 minutes, not ~30. The daemon reports both
> numbers (this condition, and total held) in its log line and in the degraded
> verdict body, so the record never claims the shorter one.

Three consequences worth stating:

- a non-approve verdict the daemon posted at the current head **outranks the
  approve envelope behind it** for convergence, so a gated round does not
  converge the loop: **on a re-request**, a later round approves the same code
  once CI is green (no new commit required);
- **a degraded `COMMENT` cannot re-enter the loop by itself, so it pages.**
  GitHub consumes a pending review request when the requested identity submits
  *any* review, comment-mode included — that is the same edge-trigger the whole
  loop is built on — so the PR leaves `review-requested:@me` the moment the
  degraded verdict lands, and a `COMMENT` is not what the DEV fix flow keys on.
  The daemon therefore posts one operator comment per (round, head) naming the
  rollup, the fact that nothing further is queued, and the two ways back in
  (conclude/fix the checks and re-request review, or merge with a recorded
  waiver). The red path needs none of this: `REQUEST_CHANGES` *is* the signal
  that flow consumes;
- the gate only ever shapes revloop's **own verdict**. It adds and removes no
  labels — `alissa:maintain` and every other cross-daemon trigger stays an
  operator/devloop concern — and `REQUEST_CHANGES` verdicts are not gated at
  all, since they are already a "not ready" signal.

#### The other half: the round that has not started yet

Everything above gates the verdict **the daemon posts** — and the daemon posts
one only for a round whose reviewer session did *not* submit its own. On the
ordinary round the session submits, and that approve travels from an agent
straight to the reviews API with no daemon-side check between it and GitHub.

studio #560, 2026-08-15, is what that costs: the worker pushed at 20:04:53, CI
started six seconds later, the round **approved** at 20:07:22, and that same
head's `test` job **failed** at 20:07:51. Nothing was wrong with the review — the
verdict simply preceded the evidence, and the red PR carried a green approval for
two hours. (The failure happened to be an unrelated flake. Irrelevant: the
approve was written before any outcome existed.)

A session that has not been queued cannot do that, so the wait moves to the
spawn. Before an owed round is enqueued the daemon reads the rollup **of the PR's
current head** — the commit that round will actually review:

| rollup at the head about to be reviewed | what the daemon does |
| --- | --- |
| still running | **does not queue the round**, re-checking each poll up to `checks_spawn_wait_seconds`. It writes no ledger row: the round burns no round number, no attempt, and the stale-round probe cannot see it |
| green | queues it, and the directive says what was seen — a round told "the head was green" still has to re-read it, which makes that a comparison rather than a chore |
| red | queues it **now**, with the failing jobs and their run URLs and a directive that forbids the approve. Waiting would be pointless (only a push or a re-run can change the answer) and the failure is real review material — it belongs in the round as a blocking finding |
| unreadable | queues it, saying so. Deliberately **not** a hold, which is where this gate parts company with the verdict one: an unreadable rollup there blocks one finished verdict, while here it would delay *every* round of *every* PR for as long as a credential lacks `checks: read` |
| still running at the bound | queues it anyway, with a directive that forbids the approve and names the checks that never settled — a CI system that never reports must delay a review, never cancel it |

A push mid-wait starts a fresh wait against the new commit: the old commit's
checks say nothing about the code the reviewer will now open.

The bound does double duty, and deliberately: the same
`checks_spawn_wait_seconds` is written into every directive as how long a
*session* may wait for a running check before it submits (floored at 5 minutes,
so the `0` that disables the hold cannot read as "do not wait at all"). One
number answers one question — how long is this loop willing to wait for this
head's CI — on both sides of the spawn. It is deliberately **not**
`checks_wait_seconds`: that one bounds the daemon holding a verdict it has
already finished, while a session waiting is holding one of
`max_concurrent_sessions` worker slots.

And because the daemon cannot intercept what a session submits, every directive
also carries the rule itself: **confirm the head you reviewed is still the head**
(`.head.sha`), then read that sha's rollup, and approve only once every context
has concluded and none failed — running, red or never-settled is
`request_changes`, naming the job and linking the run. The gate makes the common
case impossible to get wrong; the clause covers a check that goes red *while* the
review is running.

> **Two waiting states, one column.** A round waiting for a session slot and a
> round waiting for CI are both `queued` in the poll snapshot — owed, nothing
> started, no session consumed — but the console's per-PR stage tells them apart
> (`queued` vs `checks-held`), because "free a session" and "look at CI" are
> opposite actions. The spawn gate's own summary line and its stall escalation
> ignore CI holds entirely: a fleet with slow CI must not page as a review
> outage.

> **Version floor.** `ALISSA_REVIEWER_LOGIN` and `ALISSA_REVIEWER_TOKEN_ENV`
> need `REVLOOP_VERSION >= 0.16.3` in the image. The entrypoint renders them
> into `revloop.config.json` and the daemon rejects unknown config keys, so
> setting either against an older pin fails config load with `unknown config
> key(s): reviewer_token_env` — *after* a successful reviewer-identity
> preflight, which makes a version skew read like a library bug. Bump the pin
> first.

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
| a round is owed but `max_concurrent_sessions` reviewer sessions are live | **deferred** — the round waits for a slot and is retried next poll; it burns no round number and no attempt — see *Spawn back-pressure* |
| a round is owed but the head's CI has not concluded | **held** — the reviewer is not queued until the checks settle, bounded by `checks_spawn_wait_seconds`; a session that has not started cannot approve ahead of its evidence — see *Never approve a red head* |
| a round is owed and the head's CI is red | queued **now**, with the failing jobs and run URLs in its directive and no approve permitted |
| round enqueued >90 min, still no review | reviewer presumed stalled, re-enqueue |
| a round's review has landed | its reviewer session is reaped (freed) — see below |
| a round's verdict envelope exists but no reviewer-identity review does | the daemon submits it natively after a short grace; the round is **not** closed until it lands |
| that post keeps failing | retried with a growing backoff, paged after 5 attempts — the round stays open |
| the head that round judged is gone (force-push) | the post is abandoned and the round released — a fresh round is owed against the new head |
| an approve verdict, but the judged head's CI rollup is red | `REQUEST_CHANGES` leading with the failing checks — never an approve on a red head |
| an approve verdict, but the checks are still running | the round is held open (bounded by `checks_wait_seconds` **per condition waited on** — up to 2× it if an unreadable hold is promoted), then recorded as a `COMMENT` if they never conclude — plus one operator page, since a comment-mode verdict consumes the review request and nothing re-enters on its own |
| approve (GitHub state or verdict envelope) **for the current head** | converged, no-op |
| approve, but new commits landed since it was written | **not** converged — the approval is head-bound, so the next round is owed |
| converged, and the daemon's own review request is *still* pending | that request is withdrawn, so the closed round leaves the poll set — see below |
| the shipped diff has been empty for `stability_rounds` `request_changes` rounds | the round still spawns, carrying the **product-stability notice**: approve, or name the shipped `file:line` that is still wrong |
| …and that grace round comes back `request_changes`, product still unmoved | **held** — no further round, one operator page per head, `stability-held` in the console; a shipped-file push or a re-entry ack lifts it |
| `round_cap` reviews, no approve | comment cap-out on the PR, escalate, stop |
| new commits after a cap-out | re-escalate (head moved, decision is about the new state) |
| operator ack on a capped PR | grant N more rounds, log it, append to the activity comment |
| a granted re-entry consumed without approve | one fresh cap-out naming the ack, then capped again |
| PR is a draft | skip (CR1) |
| PR authored by the reviewer identity | skip — GitHub forbids self-review |
| PR author not on a non-empty `authors` list | skip, silently (log-only; no round, no comment) |
| repo has no worktree hub | skip, or hub-ify first if `on_missing_hub: "add"` |

`COMMENTED` reviews close a round, not just `APPROVED`/`CHANGES_REQUESTED` —
single-operator workspaces post comment-mode reviews per CR5, and the loop must
still advance.

### Round close-out: withdrawing a dangling self review request

The poll's attention set *is* `review-requested:@me`, and GitHub clears a
review request when the **requested** login submits a review. So the normal
closed round drops out of the search by itself.

It does not when the round's review was posted under some other login — studio
#298 again. Nothing ever consumes the request, the PR stays in the search
forever, and every poll pays a full re-verification (PR fetch, reviews, review
task, envelope count) to reach the same no-op.

So close-out withdraws it. Strictly in the **converged** branch — a verdict of
record standing at the current head, where no further round can be owed — and
never from any branch that could still open one: a moved head, a
`request_changes` round, a round still owing its native verdict, an in-flight
round, or a **capped** PR (a capped PR is precisely where an operator ack can
still grant rounds, and the ack scan only runs while the PR is in the search).

- **Only the daemon's own login is ever removed.** The DELETE names one
  reviewer, so a human reviewer or a second bot in the same
  `requested_reviewers` array is untouched.
- One `INFO` line records the removal with its evidence: the PR, the head, the
  verdict review's URL, why the round is closed, and who was left in place.
- A failed DELETE is logged and dropped — never raised, not even on a
  throttle. The decision is already made; the PR simply stays in the set and
  the next poll retries. Withdrawing a request needs write/triage permission on
  the repo, which is more than reviewing needs: a reviewer identity that only
  has read access will see the DELETE 403 every poll and keep the pre-0.16.5
  behaviour, with the failure named in the log rather than silent.
- Round accounting, verdict envelopes, the cap and re-entry semantics are all
  untouched. A re-request **against a new head** surfaces the PR and opens a
  round normally — the head move is what makes the approve stale.
- A re-request at the **unchanged** head does not open one, and the request is
  withdrawn again: the approve still stands at that commit, and re-reviewing
  code that already carries one is the stale-approve latch pointed the other
  way. That second withdrawal logs at `WARNING` rather than `INFO`, naming the
  head and saying the approve still stands, so an operator clicking the button
  by hand can see why their request keeps vanishing. Push a commit to reopen
  the loop.

**Identity drift.** If the round's newest review carries a different login than
the one the request is held against, the daemon logs one loud warning naming
both. That mismatch means GitHub-native request consumption can never work for
that deployment's configuration — every closed round will leave a dangling
request behind. It is deduped once per PR per pair of logins (durable, so a
restart does not re-announce it, and a *changed* drift does), and the read it
needs runs at most once per PR per head. It is emitted in `--dry-run` too —
that is the mode an operator reaches for to diagnose exactly this — where
**neither bound is durable**: both are held for the life of the process
instead, so a diagnostic pass cannot silence production and a production pass
cannot silence the diagnostic, while a daemon left running in dry-run still
says it only once.

### Operator re-entry after a cap-out

A cap-out is deliberately terminal: the loop never runs past the cap and never
silently merges. But the interesting case is a PR whose *fixes are already
pushed* — the loop capped out, the implementer then landed the very changes the
last verdict asked for, and that head now sits unreviewable. Raising `round_cap`
would raise it for every PR, and only after a daemon restart.

So one lever exists, per PR, and only for an operator:

```
alissa-review: re-enter +1
```

Posted as a comment on the PR (its own line; backticks optional, prose around it
is fine), by a login listed in `operators`, it raises **that PR's** effective cap
by N. `N` runs from 1 to 5 — a bigger re-entry is a second comment, not a bigger
number.

- **Counted, never inferred.** One grant per ack comment, keyed by comment id:
  the ack sits on the PR forever and grants exactly once. A second grant needs a
  second comment.
- **Auditable.** The grant is logged loudly (`RE-ENTRY GRANT …`) and appended to
  the review-loop activity comment with the author, the comment id, and the
  before/after cap.
- **Fails closed.** No `operators` allowlist, no honoured acks. The reviewer
  identity is never an operator, however it is configured — the cap-out comment
  itself quotes the grammar, and a daemon that could ack its own page would lift
  CR9's cap with nobody in the loop. The PR *author* is not excluded — an
  operator who opened the PR by hand is the ordinary case — so putting an agent
  identity on the allowlist is a deliberate choice, not something the daemon
  does for you. Malformed directives, out-of-range `N`,
  contradictory lines in one comment, quoted (`>`) lines and non-operator
  authors are all ignored, each with one log line.
- **Escalation stays once-only.** When the granted rounds are consumed without
  an `approve`, exactly one fresh cap-out fires — naming the ack that granted
  them — and the PR is capped again until the next ack.

The cap-out comment teaches all of this at the moment it is needed, and when the
head has moved past the head the last verdict was written against it says so and
recommends a single verification round: the reviewer re-checks the fix against
its own final findings and flips to `approve` (or re-requests, consuming the
grant).

### Reaping finished reviewer sessions

Reviewers are one-shot per round (CR3), but a finished `claude` sits idle at its
prompt — the session is not *empty*, so `alissa tmux cleanup` (which only reaps
empty sessions after a long idle) never frees it, and slots pile up. Three things
prevent that:

- **Fast path — the reviewer self-kills.** Its directive's final action, once the
  round is fully closed, is `alissa tmux kill <its own session>`.
- **Backstop — the daemon reaps its own finished rounds.** On each poll it kills
  the session of any round whose verdict has already landed (round ≤ completed
  rounds), read off the spawn ledger, idempotently (a per-session `reaps`
  ledger), skipped in `--dry-run`. This covers the case where the reviewer
  forgets to self-kill.
- **Terminal-PR reaper — the daemon reaps rounds it never spawned.** The
  alissa-code-review procedures also spawn reviewers *by hand*
  (`review-pr-<n>`, `review-pr-<n>-r<k>`); no ledger knows them, and until
  0.16.2 nothing reaped them. Those are reaped once their **PR is merged or
  closed**, which ends every round on it without needing any round accounting.

`enqueue_reviewer` sets the reviewer queue's `respawn off`, so a kill (from either
path) can never trigger a respawn loop.

**What the sweep may touch.** Only sessions whose *name parses* as one of the two
reviewer shapes above — the worker container is shared with other lanes
(`develop-*`, `fix-*`, `maintain-*`, …) and a prefix is not a strong enough claim
of ownership to kill on. Names that do not parse are never even enumerated. For a
session with no ledger row the number in the name is resolved against the `repos`
allowlist: a name carrying a repo picks its entry, a bare `review-pr-<n>` is
probed across the allowlist and must hit **exactly one** PR — zero or several is
a guess, and the sweep spares rather than guesses. An empty allowlist can never
resolve a bare name at all. The probe is paid once per session name, not once per
poll (its answer, including "unresolvable", is cached for the session's lifetime).

**Known v1 limits of bare-name resolution.** The allowlist *bounds the search*; it
does not prove ownership, because a bare `review-pr-<n>` carries no repo. Two
consequences, both worth knowing before you widen `repos`:

- **Over-reap.** A session reviewing a PR in a repo that is *not* watched is
  reapable if exactly one watched repo happens to have a terminal PR of the same
  number. Requiring a unique hit bounds the blast radius; it does not remove it.
- **Under-reap, and this is the one you will hit.** Once two watched repos both
  have a PR `#n` — inevitable as a newer repo's numbering catches up with an
  older one's — every bare `review-pr-<n>` in the overlap resolves to two hits
  and is spared *forever*. The symptom is the reviewer-session count quietly
  ceasing to fall while the cap alarm keeps firing.

The durable fix is out of scope for the daemon: it needs the repo *in the session
name* (a skill-side change to `spawn-a-reviewer-session.md`) or a ledger row for
hand-spawned sessions. Sessions this daemon spawns are unaffected — they carry
both a repo and a ledger row.

**Guards, all of which must hold before anything is killed.**

| guard | why |
| --- | --- |
| the session is **idle** | a busy reviewer is never killed, *even on a merged PR* — scoped post-merge re-reviews of fold commits are an established pattern; busy is logged, not reaped |
| quiet for `reap_grace_seconds` | a claude session between turns also reports "idle"; the grace period leaves a just-merged PR's reviewer time to finish its close-out (CR6 envelope, task move) |
| terminal PR, for a ledger-less session | the name's `-r<k>` cannot tell a superseded round from an in-flight one, and an operator re-entry may still want the earlier context — superseded-round reaping on an *open* PR is out of scope |

Kills are always **per session** (`alissa tmux kill <name>`), never a server-wide
kill: the container is shared, and a test pins that no other kill verb exists
anywhere in the package. Reaps are logged with evidence (name, PR state, idle
duration) at `INFO`; a failed fetch or kill is logged and skipped, never fatal to
the walk. Per-session *holdout* lines are `debug` — they repeat every poll for the
whole life of a spared session, and the container runs at `INFO`, so they are not
the operator's channel; the cap alarm below is, and it carries each survivor's
spare reason inline.
Reaping is bookkeeping-only as far as the loop is concerned — rounds are counted
from CR6 verdict envelopes and the effective cap from the grants table, so a PR
whose earlier sessions were reaped decides exactly like one whose were not.

**When the sweep cannot keep up**, i.e. more than `reap_session_cap` reviewer
sessions are still live after a pass, the daemon logs page-worthy (`ERROR`) with
each surviving session and *why* it was spared. Deduped in-process on the set of
survivors, so a standing over-cap condition pages once per episode rather than
once per poll; it re-fires when the set changes, and clears when the count falls
back inside the cap. Each idle agent session holds hundreds of MB forever; the 2026-07-28
incident was this drift, past 10 GB, with every review session idle and its PR
long merged.

### Spawn back-pressure: `max_concurrent_sessions`

Nothing used to bound how many reviewer sessions ran at once. `round_cap` bounds
*rounds per PR*; `reap_session_cap` is an alarm the daemon logs and cannot act
on. So a merge wave spawned one interactive agent per PR, simultaneously, against
a fixed container: on 2026-07-29 the 18:45–19:00Z burst pegged the deployment's
2 vCPU ceiling with 4+ concurrent reviewers plus the poll loop. Throttled
sessions review *slower*, hold their round slots longer, and widen the very burst
that is starving them.

The gate closes that. Immediately before spawning — past every branch that
decides whether a round is owed — the daemon counts the live sessions matching
**its own grammar** and, at or above `max_concurrent_sessions`, defers:

- **Deferral is not failure.** No spawn-ledger row is written, so the round
  number is untouched (the next poll computes the same one), no attempt is
  spent, and the stale-round probe cannot see the round at all — a round can sit
  gated for hours without ever being "in flight 90 minutes". Nothing is posted:
  no PR comment, no operator page, no escalation.
- **Oldest waiter first.** `review_requests` is a `search/issues` query sorted by
  *relevance* and not stable between calls, so the walk is re-ordered: everything
  already deferred goes first, in the order it was first deferred, then everything
  else in the search's own order. A round that has waited two passes takes the
  next free slot ahead of one that has waited none. The queue is in memory — it
  is a fairness order, not a decision the daemon must remember, and a restart
  costs it one pass of ordering and nothing else.
- **Own grammar only.** Both shapes count: this daemon's
  `review-<repo>-pr<n>-r<k>-<nonce>` spawns *and* the skill's hand-spawned
  `review-pr-<n>` — they burn the same CPU, and a gate blind to them would let a
  hand-driven round and a daemon round each think it held the last slot. Other
  lanes' sessions in the shared container are invisible, exactly as they are to
  the reaper.
- **One line per pass**, at `INFO`, summarizing every deferral
  (`spawn gate: 2 round(s) deferred — 4/4 reviewer sessions live …`),
  streak-limited like the poll firewall. A queue that keeps *moving* never
  escalates however long it holds: a full container handing out freed slots is
  the gate working, and paging on it would make normal load look like a leak.
- **A gate that spawns nothing does escalate.** Deferrals across consecutive
  passes with **no spawn at all**, for longer than the 30-minute
  `POLL_ESCALATE_SECONDS` window, switch the line to `WARNING` naming how long
  nothing has started. That is a different subject from a deferred round (which
  still pages nobody): it is a review outage, and the reap alarm can miss it
  entirely — a **busy** session is never reaped whatever its PR's state, a
  hand-spawned `review-pr-<n>` on an **open** PR is outside the reaper's scope
  by design, and an undecidable session is spared every poll. Four such sessions
  against the shipped defaults is a permanently shut gate under a silent alarm
  (`reap_session_cap` 6 > 4), and the gated rounds write no ledger row, so the
  stale-round probe cannot see them either. Any single spawn clears it.
- **Alarm coherence.** `Config.build` refuses a config whose `reap_session_cap`
  sits below `max_concurrent_sessions` — an alarm under the limit pages on
  healthy load. **Upgrade note:** a config that already sets `reap_session_cap`
  below `4` now fails to load; lower `max_concurrent_sessions` with it.
- **Failure mode: open.** If `alissa tmux ls` cannot be read the spawn proceeds,
  logged once per pass. That list is also the reaper's only input, so a CLI that
  cannot answer it means nothing is being reaped either — refusing every spawn
  would turn one broken subprocess into a review outage with the container idle.

The gate bounds what the *daemon* starts. Hand-spawned sessions can still push
the live count past `reap_session_cap`, and that is exactly when the alarm should
fire.

### Bounding the task-list read

`alissa task list` is the widest query this daemon issues — the calling actor's
entire live task corpus, sponsor-union scoped, several hundred rows — and it is
the *only* way to FIND a review task, because the mapping is by title. Between
2026-08-12 and 08-16 this loop was the largest single contributor to the Alissa
deployment's top database-I/O endpoint. Four mechanisms bound it, innermost
first:

| mechanism | what it removes |
| --- | --- |
| the PR → review-task **mapping** (`review_tasks`) | the fetch, for every PR whose review task is known: the task is read back by ref instead |
| the per-pass **memo** | duplicate fetches inside one pass — every PR that misses the cache shares one list |
| the **negative cache** (`review_task_misses`) | the fetch, for a PR that has *no* review task, for `review_task_miss_ttl_polls` polls |
| the **narrowed call** | rows and columns on the wire, wherever the installed CLI can filter server-side |

The negative cache is the one that closed the hole. The mapping table can only
remember an answer that exists, so a PR with no review task — a third-party PR,
or one whose task was validated or retitled — missed every pass and re-fetched
the whole corpus for the same answer, 1,440 times a day at a 60-second poll,
indefinitely. Now a search that *completes and finds nothing* is recorded, and
the next `review_task_miss_ttl_polls` polls of that PR skip the fetch and answer
from the ledger.

What that trades is latency, not correctness:

- a review task created mid-window is picked up at the **re-arm**, not the next
  poll (10 polls ≈ 10 minutes at the default cadence);
- recording a mapping clears the negative row immediately, so the two can never
  both speak for one PR;
- only a search that **ran** may record one — a search that raised propagates,
  and the pass turns it into that PR's `skipped` decision as before;
- a ledger that cannot record or spend the answer suppresses **nothing**: the
  daemon searches every pass, exactly as it did before the table existed. A
  suppression is granted only when the countdown's decrement actually persisted,
  which is what keeps a volume that flips read-only from answering "no review
  task" forever;
- `alissa-revloop --pr OWNER/REPO#N` re-arms the PR before deciding, so the
  one-shot diagnostic always reports what a search finds.

The **narrowing** is probe-gated. At boot the daemon reads `alissa task list
--help` and sends only what that output advertises as an option — an issue's (or
a docs page's) claim about a flag is not evidence, and a non-zero `alissa` exit
becomes a skipped review, not a slower one. Of the four narrowings below, the
CLI installed on 2026-08-28 offers `--self` and `--bow` — and still reports
itself as version `0.1.0`, the same string it reported on 08-16 when it offered
neither `--bow` nor `--digest`. So `alissa --version` is not a capability signal
for this CLI, and the help output is re-read rather than remembered. Both of the
flags it does offer are opt-in per deployment, so a deployment that sets neither
key still makes exactly the call the daemon always made:

- **status filter** — sent as exactly the daemon's own open-status set, so it
  cannot change which task resolves; adopted automatically when the CLI grows a
  `--status`. That set holds only *canonical* Alissa statuses, because the CLI
  validates `--status` against exactly the seven of them (`draft`, `committed`,
  `in_progress`, `blocked`, `pending_validation`, `validated`, `cancelled`) and
  refuses the whole call over one unknown value — losing the digest view and
  `--self` with it. So a non-canonical status in the open set drops the status
  filter rather than being sent: wide but complete, the direction everything
  else on this path degrades in;
- **`--view digest`** — a lean projection of each row; the daemon keeps only
  `taskNumber`/`title`/`status`, so it is adopted automatically too, with no key.
  (0.1.0 does ship a boolean `--digest` spelling of the same projection. The
  probe looks for `--view`, so that spelling is not adopted today — a narrowing
  question, not a correctness one, and untouched here.)
- **`--self`** — off unless `task_list_self_scope` says otherwise. On the live
  fleet corpus it removes 4% of the payload, and 3 of the 371 review tasks in
  that corpus are among the rows it removes: they are owned by another actor, not
  by the agent actor whose sessions write the rest. A review task the list cannot
  see is a round the daemon cannot count. Turn it on only where every review task
  is created by this daemon's own reviewer sessions.
- **`--bow <id>`** — off unless `task_list_bow_id` names a body of work. The only
  one of the four that changes the **candidate set** rather than filtering it, and
  the only one worth more than bytes: candidates come from that BOW's junction
  rows, which carry a denormalized status, so a non-matching row never costs a
  task-document read — and a BOW-scoped query's cache is invalidated only by
  writes *inside* that BOW, so the operator's unrelated task churn stops
  re-billing every 30-second poll. It does **not** compose the way the other
  three do — see below.

A narrowed call that fails, or that answers with an *empty* corpus, is retried
once unnarrowed — both are how a CLI that advertises a flag its API does not
serve would present, and either would otherwise read as "this actor has no review
tasks". A **failure** then drops the narrowing for the rest of the process. An
**empty answer** drops it too, *except* when the call was BOW-scoped: there only
`--bow` is dropped and the other three are kept, because an empty answer is a
legitimate result for a flag that replaces the corpus rather than filtering it —
see *Naming the review BOW* below for the whole of that carve-out.

#### What `--bow` actually does to the other flags

Measured against the installed CLI on **2026-08-28**, because the wire semantics
are the CLI's and not this daemon's, and the docs previously asserted the
convenient reading of them:

- **It replaces the actor's corpus; it does not intersect it.** Across five real
  bodies of work, *no* BOW-scoped answer was a subset of the plain 1335-row one,
  and every row outside it was non-terminal. So a BOW-scoped list can surface
  tasks the actor's own involvement index does not contain.
- **`--self` is ignored under `--bow`.** `--bow X --self` returns exactly what
  `--bow X` returns. A deployment running `task_list_self_scope` *and* a BOW gets
  the body of work's whole membership, actor-owned or not — **wider** than
  `--self`, never narrower.
- **`--include-terminal` is ignored too.** A BOW holding 52 tasks, 17 of them
  `validated`, answers 35 rows either way: the default non-terminal filter is
  applied server-side, but the flag that lifts it is not.
- **`--digest` / `--view` still applies** — it is a projection, not a filter.
- **`--status` could not be probed**, because the installed CLI has none. On the
  `--include-terminal` evidence, do not assume it composes.

None of that is a correctness risk, which is why the flags are still sent: a
filter the server ignores makes the answer *wider*, the daemon's own `is_open`
predicate still runs client-side, and the argv is one the CLI accepts. It costs
bytes, not reviews. Treat the list as a **dated snapshot rather than a
contract** — this CLI has now grown flags twice without moving off version
`0.1.0`.

#### Naming the review BOW

`task_list_bow_id` is opt-in per deployment, and the reason is sharper than the
one behind `--self`: every other narrowing on this page trades **wire bytes**,
while this one trades **visibility**. A review task that is not attached to the
configured BOW is invisible to the daemon — and what that costs depends on
`on_missing_review_task`, so it is worth being exact:

- `skip` — the PR is passed over: a genuinely **missed** round;
- `spawn_anyway` (the **default**) and `warn_and_spawn` — the round is spawned
  **untethered from its task**. The reviewer session has no task to hydrate and
  nowhere to record its verdict envelope; the second mode at least logs loudly.

Round *counting* survives either way — `completed` is derived from the PR's own
review records, not from the task — so the CR9 cap still holds.

Nothing creates review tasks into a BOW until the operator's review protocol says
to (upstream studio `TASK-703031741` gives `create_downstream_task` a create-time
`bodyOfWorkId`), so a deployment that sets this key before that is true lists an
empty corpus.

The id itself has a contract, and two ways to get it wrong:

- it must be the **Convex `_id`** of the BOW review tasks are created into. A
  BOW's `_id` is **not** its `mirrorInstanceId`, and passing the latter is a
  well-formed id that resolves to nothing;
- it must **not** be a repo's `autodev:` feed BOW. That one carries the *issue*
  feed, not this daemon's review tasks, so it lists rows and still finds none.

Both mistakes present identically — an empty answer — and both are caught rather
than believed: an empty *narrowed* corpus is never taken at face value, so the
call is retried plain and the daemon degrades to seeing every review task, not
none. That is a safety net for a mistyped id, not a licence to guess one.

**A BOW that is merely empty costs the BOW narrowing until the next restart, and
that is deliberate.** A wrong id and a correct-but-empty BOW are indistinguishable
— a swap has no smaller answer to compare against — so the first poll pass that
gets an empty BOW answer drops `--bow` for the life of the process. The other
three narrowings are **kept**: they filter the same corpus, so an empty answer
from them really is anomalous, while for `--bow` it is a legitimate answer, and
condemning them on that evidence would drop the whole optimization on precisely
the state a rollout starts in. Holding the BOW instead would mean paying two list
calls every pass, for ever, on a typo. So: set the key *after* the review
protocol starts attaching tasks, and if you set it earlier, restart the daemon
once the body of work is populated. The warning in the log says so at the time.

Three layers set it, later winning over earlier: the config file, the
`--task-list-bow` flag, and the environment variable `ALISSA_REVIEW_TASK_BOW`.
It is the only key with that third layer, and the reason is `alissa-pr-review`:
the implementer-side driver lists tasks too, and it runs in a task worktree with
neither the daemon's config file nor its argv, so the variable is the only
channel that reaches both call sites. An **empty** variable means unset — it does
not blank an id the file or the flag already set.

### Poll snapshots (console exhaust)

Every poll pass persists one row to a `poll_snapshots` table in the same SQLite
state DB — a self-contained record of what that pass *observed*, so the
console sidecar below can render live daemon state without spending any GitHub API
budget of its own (the UI-1 pattern ported from the devloop). Each row carries
the timestamp, the pass duration in ms, the candidate count, the decision-summary
counts (`spawned`, `stale_reenqueued`, `in_flight`, `deferred`, `queued`,
`converged`, `capped`, `escalated`, `skipped`) and the reap count, plus a JSON
column of the pass's per-item stages (PR slug and number, round, session name,
current stage, reason, task ref). It is built entirely from the per-PR decisions
already in hand — **no extra GitHub calls** — and the table is self-bounding:
the newest 1,000 rows are kept and older ones pruned on every write.

A snapshot **observes** a pass; it is not an action the daemon takes, so it is
written in `--dry-run` too (where the counts and stages reflect what *would* have
happened, and the reap count is `0`). `State.read_snapshots()` is the reader the
console consumes — newest first, with the `stages` JSON decoded back to a
list. Nothing in the decision logic reads a snapshot, so persisting it can never
change which reviewers spawn.

### Loop telemetry (Studio ingest)

With `loop_events_enabled` on, the end of every poll pass derives **loop
events** from the local ledger and pushes them to the Alissa API's
`POST /v1/loop-events` (`seat: "revloop"`) — the Factory console's source for
rounds-to-approve, verdict mix, cap-outs and holds over time. One batch per
pass, split at the ingest cap of 200; authenticated with the same
`ALISSA_API_TOKEN` the `alissa` CLI reads (the CLI itself has no loop-events
command, so this is the package's one direct REST write — `alissa_client.py`).

The kinds, each carrying the **ledger's** timestamp as `at` and a dedupe key
built from the ledger's own keys:

| kind | ledger source | payload |
| --- | --- | --- |
| `round.spawned` | `spawns` | `session`, `round`, `data.headSha`, `data.taskRef` |
| `round.verdict` | `verdict_posts` once `posted_at` is set | `round`, `data.verdict` (`approve` \| `request_changes`), `data.headSha`, `data.reviewUrl`, `data.attempts`, `data.checksHeldMs` when the round was held. **Coverage bound**: `verdict_posts` is the per-round *obligation* record — a row exists only when the daemon had to post the native fallback verdict itself, i.e. when the reviewer session did not submit its own review. On a fleet whose sessions post every review (0 of the last 58 rounds on this repo took the fallback path), this series is **expected to be empty**; covering session-posted rounds is TASK-1086576582 |
| `round.abandoned` | `verdict_posts` once `abandoned_at` is set | `round`, `reason` (= `last_error`), `data.headSha`. Same coverage bound as `round.verdict` |
| `round.capped` | `escalations` (CR9 cap-outs) | `round` (the newest spawn **at or before** `escalated_at` — the round in flight when the cap was recorded; omitted when no spawn row qualifies), `data.headSha` |
| `stability.hold` | `stability:` pings, enriched from `stability_notices` | `data.headSha`, `data.rcRounds`, `data.grantsSeen` |
| `stalled` | `stalled:<session>` pings | `session` |
| `checks.held` | `checks-unsettled:` pings **and** `spawn_checks_holds` | `round`, `data.headSha`, `data.gate` (`verdict` \| `spawn`) |
| `grant` | `grants` (operator re-entry acks) | `data.author`, `data.rounds` |
| `reap` | `reaps` | `session` |

**Best-effort, never fatal, no retry queue.** A failed push is one WARN and the
pass completes. The emitter keeps an in-memory watermark of the newest ledger
stamp it sent: a failure leaves it put, so the next pass re-derives and
re-sends, and the deterministic dedupe keys make the overlap land as silent
server-side duplicates (the ingest is idempotent on `(user, dedupeKey)`). A
daemon restart resets the watermark, so the first enabled pass re-sends the
whole ledger — that is the **backfill**, batched and deduped, not a bug. No
events are pushed in `--dry-run` (an outbound POST is an act), and none are
derived from vitals.

## Reviewer console (`alissa-revloop-ui`)

A second console script ships in the same distribution: a **stdlib-only operator
dashboard sidecar** that renders live reviewer-daemon state and offers two
actions. It runs as its own process alongside the daemon and shares nothing but
the daemon's own local exhaust — so it spends **zero** GitHub API budget of its
own beyond two cached checks. (Ported from the devloop's worker console and
adapted to reviewer semantics; the two are deliberate copies, not a shared
package — a shared-webui refactor is a separate lane.)

```sh
export ALISSA_UI_PASSCODE='…'          # required — no passcode, no boot (fail-closed)
alissa-revloop-ui --workspace-root /path/to/workspace   # serves 127.0.0.1:8788
```

- **Read-only, and it says so when there is nothing to read.** Reads open the
  ledger through sqlite's `mode=ro`, so the console can never create or migrate
  the daemon's `state.db`; a workspace that has never run the daemon renders a
  banner naming the path instead of an empty dashboard that looks like an idle
  daemon (`--workspace-root` defaults to the cwd, so that is the easy mistake).
- **Data, no polling of GitHub.** Every panel reads the daemon's `poll_snapshots`
  table (the per-pass exhaust — pipeline board, sparklines, review-queue depth),
  the spawn ledger (which session is on which PR round), the escalation table and
  the ping ledger (the operator inbox), all read-only; plus local
  `alissa tmux ls` + a `/proc` walk of each session's pane PID for CPU%/RSS. The
  only network calls are `gh api rate_limit` (60s cache) and the PyPI version
  JSON (10m cache, for the running-vs-latest drift chip).
- **Reviewer semantics.** The pipeline board is PR-centric — PR ref → **round k
  of the cap** → session → stage (`spawned` / `in-flight` / `deferred` /
  `queued` / `checks-held` / `stale-re-enqueued` / `converged` / `capped` /
  `escalated` / `skipped`). The
  inbox pages the three things the daemon pages a human about: CR9 **cap-outs**
  (from `escalations`), **stalled** deferral episodes and **stability holds**
  (both from `pings`, under their own kind prefixes), all linking to the PR.
  There is no worker-tasks panel — reviewers create no tasks — and no
  maintenance edge.
- **Settled pages are filed away, not deleted.** `escalations` and `pings` are
  dedupe key stores the daemon must keep, so they are never pruned — and an
  inbox bounded only by row count therefore fills with pages whose PR merged
  days ago, until an operator learns to skim past it and misses the live rows
  too. The console splits them at read time, from the daemon's own exhaust and
  still without a single GitHub call: a page is **live** while its PR is in the
  newest poll snapshot's items, and **settled** once that pass no longer lists
  it — ordinarily because the trigger cleared (merged, closed, or the review
  request withdrawn), so nothing the console offers can act on it any more. A
  capped or stability-held PR that is still open keeps its review request, so
  its page stays live: that is exactly the one owed a re-entry ack. Settled rows
  move behind a collapsed `N settled — show` footer — which stays open across
  the ten-second refresh once an operator expands it, since the half exists to
  be audited — and `Inbox clear.` is the empty state of the live half only.
  Four guards keep the split from hiding real work: a page raised within the
  last two poll intervals is live whatever the snapshot says (it may simply
  predate the pass); when there is no snapshot at all — a fresh boot, an
  unreadable `state.db` — every row is live; a row the console cannot key at
  all is live, because that is missing evidence about the row; and the ledgers
  are read several windows deeper than the panel renders, so a run of settled
  rows cannot squeeze the live half out. When a read does come back at its
  bound the payload says so, and the panel both drops the `Inbox clear.` claim
  — which must mean *no live pages* and never *none among the rows I read* —
  and marks a non-empty list as short, which would otherwise read as complete.
  The footer's own count is what expanding it reveals, with anything the
  per-half cap left out named beside it (`+N not shown`) rather than folded
  into it. Missing evidence never hides a page. One caveat the split inherits
  rather than creates: the daemon's own candidate listing is a single
  unpaginated search, so past its page size a pass sees an arbitrary subset —
  TASK-1796886433 covers closing that ceiling.
- **Fail-closed auth.** `ALISSA_UI_PASSCODE` unset ⇒ refuse to start. Login is a
  constant-time compare behind a throttle; the session cookie is HMAC-signed with
  a key derived from the passcode **and** a per-boot nonce (so a restart logs
  everyone out), and every action POST additionally needs a CSRF token bound to
  that cookie.
- **Actions (audit-logged to stdout).** *Kill* runs exactly
  `alissa tmux kill <session>` (never `kill-server`); *Retry-now* ages the round's
  newest spawn row past the stale window — an `UPDATE`, reusing the daemon's own
  retry semantics, never a new retry path. Aging is necessary but not sufficient:
  the daemon still defers a respawn behind a session that shows life (that
  liveness signal is what stops a round being double-spent), so kill the wedged
  session first, then retry.
- **Studio design system**, both themes (parchment/ink light + glass-dark), one
  gold accent on the drift chip, status colours kept separate.

The daemon is a **read-only consumer's** data source here — `alissa-revloop-ui`
never drives the loop, and the daemon is unaware of it. The log-tail panel reads
`--log-file` (or `$ALISSA_REVLOOP_LOG`). The default port is **8788**, not the
devloop console's 8787: the two daemons routinely run on one machine.
`ALISSA_UI_SECURE_COOKIE=1` adds `Secure` to the cookie for a TLS-terminated
(reverse-proxied) posture.

**In the container.** The reviewer image wires the sidecar in: set
`ALISSA_UI_ENABLED=1` **and** `ALISSA_UI_PASSCODE`, and the entrypoint starts the
console alongside the worker and daemon on `0.0.0.0:${PORT:-8080}` (off by
default; enabled-without-a-passcode dies at boot; `/healthz` is the platform
healthcheck path). Enabling it behind a public URL puts the dashboard — kill and
retry-now included — on the internet behind that one passcode. See
[`docker/claude/README.md`](./docker/claude/README.md#reviewer-console-runtime-env-only--alissa_ui_enabled-alissa_ui_passcode-port).

## Scope

The daemon (`alissa-revloop`) is the **reviewer side**. It reacts to review
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

## Migration: `reviewloop` → `revloop`

This repo adopted the three-letter loop-family naming (devloop / orcloop /
revloop). The rename is name-only — no behaviour changed:

| Was | Now |
| --- | --- |
| module `alissa.tools.github.reviewloop` | `alissa.tools.github.revloop` |
| dist `alissa-tools-github-reviewloop` | `alissa-tools-github-revloop` |
| CLI `alissa-reviewloop` | `alissa-revloop` |
| Dockerfile ARG `REVIEWLOOP_VERSION` | `REVLOOP_VERSION` |
| config file `reviewloop.config.json` | `revloop.config.json` |
| state dir `<workspace>/.reviewloop/` | `<workspace>/.revloop/` |

The loop-closer console script `alissa-pr-review` keeps its name — it names the
action, not the loop — only its home module path moved to `…github.revloop`.

**Old dist is frozen.** `alissa-tools-github-reviewloop` stays on PyPI at its
last published version (**0.11.0**) and receives no further releases. Install
`alissa-tools-github-revloop` instead; the new line continues monotonically from
**0.12.0** (its first release, i.e. `0.12.0 > 0.11.0`).

**Operators must migrate a mounted config.** In env-driven mode the container
entrypoint regenerates `revloop.config.json` on every boot, so nothing to do.
If you *mount* a config file, rename `reviewloop.config.json` →
`revloop.config.json`; the old `.reviewloop/` spawn ledger is simply re-created
as `.revloop/` on first poll (a fresh container clears the ledger anyway).

**On-merge operator follow-up (not performed on this branch):** the Railway
reviewer service exposes the ARG-matched build variable `REVIEWLOOP_VERSION`.
Rename it to `REVLOOP_VERSION` with the new release value (0.12.0) and rebuild in
the same step, so the from-Dockerfile build resolves the ARG.

**Cross-repo consumer follow-up (separate, in the OTHER repo):** the devloop
image installs this project's *old* dist purely for the `alissa-pr-review`
loop-closer. Repointing it to `alissa-tools-github-revloop` is a follow-up owned
by the devloop repo — it is **not** touched from this branch.

## Environment notes

- `gh` 2.4.0 (Ubuntu 2022 build) predates `gh search`, so all queries go through
  `gh api`. Nothing else here depends on a newer gh.
- Search API allows 30 req/min; one pass costs 1 search + 2 core calls per PR.
  Rate limits trigger exponential backoff to a 15 min ceiling.
- No third-party dependencies; `pytest` only for tests.

## Tests

```sh
bash tests-unit.sh alissa-tools-github-revloop
bash tests-coverage.sh alissa-tools-github-revloop
bash check-style.sh alissa-tools-github-revloop
bash check-types.sh alissa-tools-github-revloop
```

The container entrypoint has its own shell suites (no docker needed — the CLIs it
shells out to are stubbed, and the entrypoint under test is the real one):

```sh
bash docker/claude/tests-entrypoint-config.sh    # config renderer pass-through
bash docker/claude/tests-entrypoint-identity.sh  # reviewer-identity preflight
bash docker/claude/tests-entrypoint-auth.sh      # alissa auth failure triage
bash docker/claude/tests-entrypoint-executor.sh  # bridge-executor role + gates
bash docker/claude/tests-entrypoint-ui.sh        # reviewer-console wiring
```

815 tests cover the decision state machine, the config layering (including the
`authors` scope filter: default-empty, replace-not-extend, the string guard,
case-insensitive matching, the pre-bookkeeping skip and the self-review guard's
precedence over it), the two CI
gates (pre-spawn hold, verdict hold — pending→green, pending→red, both
timeouts, a head that moves under either), the task-list bounds (the negative
cache's window, its re-arm, its per-PR countdown and every way it fails open;
the CLI flag probe against the real help text of each CLI generation, and the
runtime disproof of a flag the API does not serve), the
`poll_snapshots` exhaust buffer (record/read round-trip, retention pruning,
in-place migration, one-snapshot-per-poll, dry-run capture), the
`alissa-pr-review` round/verdict/timeout logic, and the reviewer console (auth
matrix, endpoint payload shapes off a seeded state.db, `/proc` parsing with
vanished PIDs, pinned action argv, HTML token presence), with GitHub, Alissa,
tmux and `/proc` faked.

**Verified live:** the search query, login resolution, PR/review fetching,
`alissa task list` parsing, review-task title matching, worker detection, the
identity-mismatch guard, and the workspace preflight.

**Not verified live:** the trigger firing on a real review request, and the spawn
actually reaching a tmux session. Both need a PR authored by an account *other*
than the reviewer identity — GitHub forbids requesting review from the PR author,
so a self-authored PR never fires the trigger at all. Run `--once --dry-run -v`
against the first real request before letting it run unattended.

## License

Licensed under the [Apache License 2.0](LICENSE) — the same license applies to
the published `alissa-tools-github-revloop` distribution, which ships `LICENSE`
and [`NOTICE`](NOTICE) alongside the code.

"Alissa" and "Fahera" are trademarks of CORE FAHERA ENTERPRISE HOLDINGS S. DE
R.L.; the license does not grant permission to use them.
