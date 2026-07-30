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

| key / flag | default | meaning |
| --- | --- | --- |
| `--workspace-root` | cwd | root of the worktree-hub workspace (**CLI only**) |
| `hub_template` | `{root}/{repo}/main` | reviewer cwd — the pristine `main/` mirror (CR6: reviewers never write) |
| `poll_interval` | `60` | seconds; must be ≥10 |
| `round_cap` | `10` | CR9 cap; never queues round cap+1 |
| `repos` | `[]` | allowlist of `owner/repo`; empty = all |
| `operators` | `[]` | GitHub logins whose re-entry ack may re-open a capped PR; empty = none |
| `agent_profile` | `claude` | agent the worker launches for reviewer sessions |
| `reviewer_login` | `null` | the identity every verdict is posted under; resolved from `gh api user` when null |
| `reviewer_token_env` | `null` | **name** of the env var holding the reviewer's GitHub token (never the token). Set it and every `gh` call runs under that credential explicitly; leave it null and the daemon inherits — and warns |
| `state_path` | `<workspace-root>/.revloop/state.db` | spawn ledger; per-workspace by default so parallel daemons never share one |
| `on_missing_review_task` | `spawn_anyway` | `spawn_anyway` \| `warn_and_spawn` \| `skip` |
| `on_missing_hub` | `skip` | `skip` \| `add` — see *Provisioning new repos* |
| `reap_grace_seconds` | `1800` | how long a reviewer session must be idle **and** quiet before the sweep reaps it (and before a stale round reads it as dead); must be **well under** the 90-minute stale-round window, which the loader enforces |
| `reap_session_cap` | `6` | more live reviewer sessions than this after a sweep and the daemon logs page-worthy; an alarm threshold, not a capacity limit |
| `checks_wait_seconds` | `1800` | how long a round holds its **approve** while the judged head's CI rollup is still running (or unreadable) before recording the verdict as a `COMMENT` instead; a **red** rollup never waits and never approves — see *Never approve a red head* |

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
| round enqueued >90 min, still no review | reviewer presumed stalled, re-enqueue |
| a round's review has landed | its reviewer session is reaped (freed) — see below |
| a round's verdict envelope exists but no reviewer-identity review does | the daemon submits it natively after a short grace; the round is **not** closed until it lands |
| that post keeps failing | retried with a growing backoff, paged after 5 attempts — the round stays open |
| the head that round judged is gone (force-push) | the post is abandoned and the round released — a fresh round is owed against the new head |
| an approve verdict, but the judged head's CI rollup is red | `REQUEST_CHANGES` leading with the failing checks — never an approve on a red head |
| an approve verdict, but the checks are still running | the round is held open (bounded by `checks_wait_seconds`), then recorded as a `COMMENT` if they never conclude — plus one operator page, since a comment-mode verdict consumes the review request and nothing re-enters on its own |
| approve (GitHub state or verdict envelope) **for the current head** | converged, no-op |
| approve, but new commits landed since it was written | **not** converged — the approval is head-bound, so the next round is owed |
| converged, and the daemon's own review request is *still* pending | that request is withdrawn, so the closed round leaves the poll set — see below |
| `round_cap` reviews, no approve | comment cap-out on the PR, escalate, stop |
| new commits after a cap-out | re-escalate (head moved, decision is about the new state) |
| operator ack on a capped PR | grant N more rounds, log it, append to the activity comment |
| a granted re-entry consumed without approve | one fresh cap-out naming the ack, then capped again |
| PR is a draft | skip (CR1) |
| PR authored by the reviewer identity | skip — GitHub forbids self-review |
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

### Poll snapshots (console exhaust)

Every poll pass persists one row to a `poll_snapshots` table in the same SQLite
state DB — a self-contained record of what that pass *observed*, so the
console sidecar below can render live daemon state without spending any GitHub API
budget of its own (the UI-1 pattern ported from the devloop). Each row carries
the timestamp, the pass duration in ms, the candidate count, the decision-summary
counts (`spawned`, `stale_reenqueued`, `in_flight`, `deferred`, `converged`,
`capped`, `escalated`, `skipped`) and the reap count, plus a JSON column of the
pass's per-item stages (PR slug and number, round, session name, current stage,
reason, task ref). It is built entirely from the per-PR decisions already in hand
— **no extra GitHub calls** — and the table is self-bounding: the newest 1,000
rows are kept and older ones pruned on every write.

A snapshot **observes** a pass; it is not an action the daemon takes, so it is
written in `--dry-run` too (where the counts and stages reflect what *would* have
happened, and the reap count is `0`). `State.read_snapshots()` is the reader the
console consumes — newest first, with the `stages` JSON decoded back to a
list. Nothing in the decision logic reads a snapshot, so persisting it can never
change which reviewers spawn.

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
  `stale-re-enqueued` / `converged` / `capped` / `escalated` / `skipped`). The
  inbox pages the two things the daemon pages a human about: CR9 **cap-outs**
  (from `escalations`) and **stalled** deferral episodes (from `pings`), both
  linking to the PR. There is no worker-tasks panel — reviewers create no tasks —
  and no maintenance edge.
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

The container entrypoint has its own two shell suites (no docker needed — the
CLIs it shells out to are stubbed):

```sh
bash docker/claude/tests-entrypoint-config.sh   # config renderer pass-through
bash docker/claude/tests-entrypoint-ui.sh       # reviewer-console wiring
```

282 tests cover the decision state machine, the config layering, the
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
