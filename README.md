# Alissa — GitHub Review Daemon

A GitHub watcher that drives the [`alissa-code-review`](https://skills.alissa.app/)
adversarial review loop (CR1-CR9) to convergence.

The skill lists trigger automation as a planned tier ("a CI job on
`pull_request.ready_for_review` ... is not part of this skill's contract"). This
is that tier, as a polling daemon instead of a webhook.

Shipped as `alissa.tools.github.reviewloop` in the `alissa.tools` distribution
(`alissa-tools/`); see that package's README for the layout conventions.

## What it does

One poll pass:

1. Ask GitHub for PRs with a review pending from you.
2. For each, work out which round is owed.
3. Enqueue a **fresh** reviewer session for that round via `alissa tmux queue add`.
4. Stop at `approve`, or escalate at the round cap.

```
gh api search/issues            →  PRs awaiting my review (draft:false → CR1)
  ↓
gh api …/pulls/N/reviews        →  how many rounds have I completed?
  ↓
alissa task list                →  find the review task (CR2 dedupe)
  ↓
alissa tmux queue add           →  fresh reviewer, round k (CR3)
```

## The key design decision: rounds come from GitHub, not local state

GitHub **clears** a pending review request the moment you submit a review, and
**re-adds** it when the implementer re-requests after fixes. So
`review-requested:@me` is already an edge-trigger for CR9 rounds — no webhook and
no diffing needed.

Round number is derived: `round = (my submitted reviews on this PR) + 1`. That
makes GitHub the source of truth. The local SQLite ledger holds only two things
it cannot: which round is currently *in flight* (so a 60s poll doesn't spawn the
same reviewer twice), and which cap-outs were already escalated.

CR3's "fresh instance per round" falls out for free — each trigger spawns a new
`ali-*` session, named `review-<repo>-pr<n>-r<k>`.

## Setup

```sh
python -m venv venv && source venv/bin/activate
pip install -r requirements-develop.txt
pip install -e ./alissa-tools

cp reviewloop.config.example.json reviewloop.config.json
$EDITOR reviewloop.config.json          # set workspace_root at minimum
alissa-reviewloop --once --dry-run -v
```

`alissa worker` must be running or queued sessions never spawn — the daemon warns
at startup if it isn't.

```sh
alissa-reviewloop         # foreground; tip: run it in its own tmux session
```

### Config

| key | default | meaning |
| --- | --- | --- |
| `workspace_root` | *(required)* | root of the worktree-hub workspace |
| `hub_template` | `{root}/{repo}/main` | reviewer cwd — the pristine `main/` mirror (CR6: reviewers never write) |
| `poll_interval` | `60` | seconds; must be ≥10 |
| `round_cap` | `3` | CR9 cap; never queues round cap+1 |
| `repos` | `[]` | allowlist of `owner/repo`; empty = all |
| `reviewer_login` | `null` | resolved from `gh api user` when null |
| `on_missing_review_task` | `spawn_anyway` | `spawn_anyway` \| `warn_and_spawn` \| `skip` |
| `on_missing_hub` | `skip` | `skip` \| `add` — see *Provisioning new repos* |

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
| last review is `APPROVED` | converged, no-op |
| `round_cap` reviews, no approve | comment cap-out on the PR, escalate, stop |
| new commits after a cap-out | re-escalate (head moved, decision is about the new state) |
| PR is a draft | skip (CR1) |
| PR authored by the reviewer identity | skip — GitHub forbids self-review |
| repo has no worktree hub | skip, or hub-ify first if `on_missing_hub: "add"` |

`COMMENTED` reviews close a round, not just `APPROVED`/`CHANGES_REQUESTED` —
single-operator workspaces post comment-mode reviews per CR5, and the loop must
still advance.

## Scope

This is the **reviewer side**. It reacts to review requests and spawns reviewers.
The implementer side of the loop — triaging findings (CR8), fixing, re-requesting
— stays with the implementer per `procedures/run-the-review-loop.md`. From here,
each re-request is simply the next round.

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
bash tests-unit.sh alissa-tools
bash tests-coverage.sh alissa-tools
bash check-style.sh alissa-tools
bash check-types.sh alissa-tools
```

26 tests cover the decision state machine with GitHub and Alissa faked.

**Verified live:** the search query, login resolution, PR/review fetching,
`alissa task list` parsing, review-task title matching, worker detection, the
identity-mismatch guard, and the workspace preflight.

**Not verified live:** the trigger firing on a real review request, and the spawn
actually reaching a tmux session. Both need a PR authored by an account *other*
than the reviewer identity — GitHub forbids requesting review from the PR author,
so a self-authored PR never fires the trigger at all. Run `--once --dry-run -v`
against the first real request before letting it run unattended.
