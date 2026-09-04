# Containerized review daemon (`docker/claude`)

Runs the GitHub review loop unattended in a container: the `alissa-revloop`
poller, an `alissa worker`, and the `claude` reviewer agent it spawns — all in
one image.

This is **not** a thin Python-daemon container. The daemon only watches GitHub
and enqueues sessions; the worker is what drains the queue and spawns reviewers,
so the image bundles all three tiers (see the top-of-file comment in
[`Dockerfile`](./Dockerfile)). Two of those three now come from a shared base
image rather than from layers built here — see [Base image](#base-image).

The same image also serves a **second, separate service**: with
`CONTAINER_ROLE=executor` it runs `alissa bridge start` as an Alissa Studio queue
executor instead of the review loop. See
[Bridge executor role](#bridge-executor-role-a-second-service-from-this-same-image).

## Build

```sh
docker build --platform linux/amd64 -t alissa-review-daemon docker/claude

# with configuration baked in (see the Configuration table):
docker build --platform linux/amd64 \
  --build-arg ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app|fahera-mx/blog.alissa.app" \
  --build-arg ALISSA_POLL_INTERVAL=90 \
  --build-arg ALISSA_ROUND_CAP=3 \
  -t alissa-review-daemon docker/claude
```

`--platform linux/amd64` is not optional boilerplate: the base image is published
for that platform only. It is a no-op on an amd64 host and the difference between
an emulated build and a hard failure anywhere else — see
[Platform: amd64 only](#platform-amd64-only).

To check the built image against its contract rather than by eye, run
[`tests-image-contract.sh`](./tests-image-contract.sh), which does the build and
then asserts inside the result:

```sh
docker/claude/tests-image-contract.sh
```

The image installs the daemon from PyPI, so the build context is just this
directory — no repo source is copied in.

`REVLOOP_VERSION` is deliberately absent from the example: its `ARG` default in
the Dockerfile is the release this repo publishes on merge, and it is the value
CI reads back out of the Dockerfile, so a second copy here could only ever go
stale — and a stale one is load-bearing once a config key has a version floor
(pinning below `ALISSA_REVIEW_OPERATORS`' floor makes the daemon reject the key
and the container exit at boot). Pass `--build-arg REVLOOP_VERSION=…` only when
you deliberately want a version other than the pinned one.

### Base image

This Dockerfile is a thin **leaf** on the shared loopwork base image, pinned by
both an exact tag and a digest:

```dockerfile
FROM ghcr.io/ali-fhr/alissa-loopwork-base:0.2.0@sha256:f46fd1431462392993da6dd209e23c15c3dc8495540469eaa06b40ca5bafda1c
```

The base is **public on GHCR**, so the pull is anonymous — no registry
credential is needed anywhere, Railway included.

It owns the runtime substrate this image used to build for itself, and which was
copy-pasted between this repo and the develop daemon. **Do not re-add any of it
to the leaf:**

- python 3.12 + Node 22, and `git` / `tmux` / `gh` / `tini` / `gosu` / `jq`
  (+ `iptables`/`ipset` for the optional egress firewall)
- **claude-code**, with its first-run gates pre-seeded (`~/.claude.json`,
  `~/.claude/settings.json`) so worker-spawned reviewers start headless
- since base `0.2.0`, two further agent CLIs — **`codex`** and **`pi`** — which
  make the base multi-agent for its other leaves. This daemon spawns `claude`
  only, so both are present and unused here; that is expected, not a defect, and
  it is not a reason to add knobs or config for them
- the **alissa CLI** on the `alissa` user's `PATH`
- the non-root **`alissa` user (uid 1000)** and the `/workspace` mount point
- the system-wide GitHub **SSH→HTTPS rewrite** with `gh` as git's credential
  helper, plus `advice.detachedHead=false`
- the workspace ENV skeleton (`ALISSA_WORKSPACE_ROOT`, `TMUX_TMPDIR`,
  `CLAUDE_CONFIG_DIR`), `EXPOSE 8080`, and the `tini` **ENTRYPOINT** hook at the
  fixed path `/usr/local/bin/entrypoint.sh`

What stays in this leaf: the pip-installed daemon (`REVLOOP_VERSION`), the
entrypoint plus `revloop-config.sh` / `init-firewall.sh` — COPYed **over** the
base's deliberately-failing stub at that fixed entrypoint path — `agents.yaml`,
the `alissa-review-daemon` git author identity, and the whole ARG→ENV knob block
below.

**Bumping the base is a one-line `FROM` pin change**, reviewed like any other
PR. That is how claude-code, the alissa CLI and the system packages advance for
this image — never by re-adding a layer here. Base repo and its leaf contract:
<https://github.com/ali-fhr/alissa-loopwork>.

The bump to `0.2.0` (base PR `ali-fhr/alissa-loopwork#2`) is the multi-agent one:
it adds `codex` and `pi` next to claude-code, and that is the whole of it — the
claude-code version is unchanged across the bump, and so is everything this leaf
builds on top. What it costs is **size**: the base's compressed amd64 layers go
from ~270 MB to ~430 MB (≈840 MB → ≈1.31 GB unpacked), and this image inherits
all of it. Expect a slower cold pull; nothing else about the runtime changes.

#### How the pin is written

Never `:latest`, and never a bare tag either. The reference carries **two values
that do different jobs**, and a bump changes both together:

| half | job |
| --- | --- |
| `:0.2.0` — exact semver | the **readable** half. It is what makes a bump a reviewable one-line change and what tells a reader which base this is. |
| `@sha256:f46fd143…` — digest | the **enforcing** half. A tag is mutable; without the digest, a re-push of `0.2.0` is substituted into every build with no diff to review. |

The digest matters more here than for an ordinary base image. This one line is now
the *entire* review surface for claude-code, a `curl … | bash` CLI install and
the whole apt layer — none of which this repo builds, or sees, any more. A silent
substitution should be a build failure, not a successful build of something else.

The pinned digest is the **index** digest (what the registry returns as
`Docker-Content-Digest` for the tag `0.2.0`), not the digest of the amd64 child
manifest it currently selects (`sha256:ba2f9b7b…`). Pinning the index keeps
platform selection a build-time choice, so when the base gains arm64 this stays an
ordinary two-value bump instead of a reference that can only ever resolve to
amd64. Read the current values back with:

```sh
docker buildx imagetools inspect ghcr.io/ali-fhr/alissa-loopwork-base:0.2.0
```

#### Platform: amd64 only

**The base publishes `linux/amd64` and nothing else.** Its `0.2.0` index contains
exactly one platform manifest plus an attestation manifest — no `arm64`, no
`arm/v7`. The `python:3.12-slim-bookworm` this image used to build from shipped
five architectures, so this is a real narrowing and it is worth knowing before you
build.

It is an upstream **omission rather than a decision**: the base repo's
`publish.yml` calls `docker/build-push-action@v6` with no `platforms:` key, so the
single architecture is inherited from the amd64 GitHub runner.

What it costs, and what it does not:

- **Production is unaffected.** Railway builds amd64.
- **Local builds off amd64 are affected** — Apple Silicon in particular. Without
  `--platform linux/amd64` the build fails at the `FROM` with
  `no match for platform in manifest`, an error that points at the base rather
  than at anything you did. With the flag it builds under emulation: slower, but
  correct.

That is why every `docker build` command documented above passes
`--platform linux/amd64` explicitly, and why
[`tests-image-contract.sh`](./tests-image-contract.sh) passes it too.

The fix is upstream and cheap: add `platforms: linux/amd64,linux/arm64` to the
base's publish workflow, then re-pin tag **and** digest here. Nothing in this leaf
needs to change for it.

### On Railway

Set the config values (`ALISSA_REVIEW_REPOS`, `ALISSA_POLL_INTERVAL`, …) as
**service variables** — Railway passes any variable matching a declared `ARG`
into the Dockerfile build, which is why these are ARGs and not plain runtime
ENV. Set the three **secrets** (`GH_TOKEN`, `ALISSA_API_TOKEN`,
`ANTHROPIC_API_KEY`) as service variables too; those are read at runtime and
must NOT be baked in.

The **reviewer console** knobs (`ALISSA_UI_ENABLED`, `ALISSA_UI_PASSCODE`,
`PORT`, `ALISSA_UI_SECURE_COOKIE`) are deliberately **not** ARGs — they are
runtime-only service variables. See
[Reviewer console](#reviewer-console-runtime-env-only--alissa_ui_enabled-alissa_ui_passcode-port)
and [The console on Railway](#the-console-on-railway).

## The three identities (self-onboarding)

The loop depends on three independent identities. Provide all three as **runtime
env** (secrets — never baked into the image); the entrypoint does the rest of the
onboarding automatically, so you only supply tokens:

| env var | identity | required? | what the entrypoint does |
| --- | --- | --- | --- |
| `GH_TOKEN` | `gh` (the `alissa-app` GitHub user) | **yes** — fatal if missing | validates via `gh api user`; the image rewrites GitHub SSH URLs to HTTPS + wires gh as the git credential helper, so hub-ify's `git clone` authenticates with the token (no SSH key needed) |
| `ALISSA_API_TOKEN` (`alissa_…`) | Alissa by Fahera | **yes** — fatal only when the *API rejects it* | `alissa auth login --token` (stores + verifies), after [triaging what a failure actually is](#alissa-auth-failures-are-triaged-not-guessed) |
| a persisted `claude /login` *(recommended)*, or `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` in env | claude | no — warns, continues | credential persists on the volume via `CLAUDE_CONFIG_DIR`; the baked [`agents.yaml`](./agents.yaml) launches claude headless and the first-run config is pre-seeded (see below) |

`GH_TOKEN` and `ALISSA_API_TOKEN` are hard requirements — the daemon can't poll
GitHub or reach the task queue without them. The **claude credential is not**: the
daemon never calls claude directly (only the worker-spawned reviewer does), and
claude can authenticate by other means — a mounted `~/.claude` credential or
Bedrock/Vertex env. If none is present the entrypoint just warns, and a reviewer
that genuinely has no credential fails on its own later.

### `alissa auth` failures are triaged, not guessed

A failing `alissa auth login` used to mean one thing to the entrypoint —
"`ALISSA_API_TOKEN` rejected" — and it exited FATAL, so the platform restarted
the container and it failed again, forever. On 2026-07-29 that crash-looped the
Railway service through two multi-hour outages **with a perfectly valid token**:
the `alissa` CLI binary (an image-layer file at `~/.local/bin/alissa`) had
vanished mid-run, so the login never reached a server at all, and its stderr was
muted so nothing said so.

The gate now triages before it interprets, and **logs the command's real
stderr** in every case:

| what actually happened | how it is detected | what the entrypoint does |
| --- | --- | --- |
| CLI missing / not executable | `command -v alissa` fails | re-bootstraps from the official installer (`ALISSA_INSTALL_URL`), then retries — no human needed |
| config dir unwritable | write probe in `ALISSA_CONFIG_DIR` | retries with capped backoff, **forever** (a volume that mounts late self-heals) |
| API unreachable | `curl` transport failure against `ALISSA_API_BASE` — an HTTP error response counts as *reachable* | retries with capped backoff, **forever** |
| token genuinely refused | the login ran, reached the server, and got `401`/`403` | **FATAL, fast**, naming `ALISSA_API_TOKEN` rotation — the only class a human can fix |

An unrecognised failure retries rather than dying: every outage this gate has
actually caused was a platform blip mislabelled as a rejection. It is not silent
about it either — once the retries outlast `ALISSA_AUTH_ESCALATE_SECONDS` every
further attempt also logs an `ERROR` naming token rotation as the thing to check.

| runtime env var | default | meaning |
| --- | --- | --- |
| `ALISSA_AUTH_RETRY_SECONDS` | `30` | first backoff step for the self-healing classes |
| `ALISSA_AUTH_RETRY_CAP_SECONDS` | `600` | ceiling the doubling stops at |
| `ALISSA_AUTH_ESCALATE_SECONDS` | `600` | after this much total waiting, each retry also logs page-worthy `ERROR` |

The defaults are the intended production values; these three knobs exist so
`tests-entrypoint-auth.sh` can exercise the retry loop without waiting out real
minutes.

> **`ALISSA_INSTALL_URL` is test-only — do not set it in a deploy.** The
> re-bootstrap runs `curl -fsSL "$ALISSA_INSTALL_URL" | bash`, so overriding it
> makes the entrypoint execute remote code from wherever it points. It exists
> for `tests-entrypoint-auth.sh` and is **not** a supported production lever;
> leave it unset and the official installer
> (`https://share.alissa.app/install`) is used. No checksum is pinned against
> that default on purpose: a pin in this repo goes stale on every installer
> release, and a stale pin would turn the self-healing re-bootstrap into a hard
> failure during exactly the incident it exists for.

The daemon itself has the matching property in-process — see
[Crash resilience](#crash-resilience-the-daemon-survives-its-substrate).

### claude auth: log in once, persisted on the volume (recommended)

The worker spawns each reviewer as the **`alissa`** user, so the credential has to
be visible to that user *and* survive restarts. The durable answer is a one-time
interactive `claude /login`: it stores a **refresh-token credential that
auto-renews**, unlike a static `CLAUDE_CODE_OAUTH_TOKEN` (a `setup-token` is a
fixed 1-year token that eventually returns `401 Invalid bearer token`).

The image sets **`CLAUDE_CONFIG_DIR=/workspace/.claude-config`** (on the persistent
volume), which relocates claude's `.credentials.json` there — so the login
survives restarts and redeploys. Log in once, as `alissa`, inside the container:

```sh
gosu alissa bash -lc 'claude /login'      # follow the URL, paste the code back
```

That writes `/workspace/.claude-config/.credentials.json`; every reviewer (and
every future container) reuses and auto-renews it. claude ≥ 2.1.211 coordinates
renewal across parallel sessions, so concurrent reviewers won't corrupt it.

Two footguns:

- **Don't `claude /login` as `root`** — it writes `/root/.claude/…`, which the
  `alissa` reviewer never reads. Always `gosu alissa`.
- **A static token in the env wins over the file and will keep 401'ing.** If you
  set `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` and it's expired/invalid,
  claude uses it instead of the good persisted login. Once you've done `/login`,
  **remove those env vars** in Railway. (A valid env token still works if you
  prefer it — but prefer `CLAUDE_CODE_OAUTH_TOKEN` over `ANTHROPIC_API_KEY`, since
  a bare API key triggers claude's own "approve this key?" prompt in the TUI.)

**First-run dialogs are pre-seeded** so the TUI never blocks: the image bakes the
onboarding/settings flags (`hasCompletedOnboarding`, `hasSeenAutoModeEntryWarning`,
`skipDangerousModePermissionPrompt`, theme) and the entrypoint sets
`projects["<hub>/main"].hasTrustDialogAccepted` for every reviewer dir — into both
`$HOME` and `$CLAUDE_CONFIG_DIR`. Without these a fresh user hangs at claude's
welcome / theme / bypass-mode / **"trust this folder?"** dialog (`"stuck — waiting
at a prompt"`); the trust dialog in particular is **not** suppressed by
`--dangerously-skip-permissions`.

So the setup is: two tokens in the env (gh + alissa), one `claude /login` on the
volume, and the container self-configures git-over-HTTPS, the alissa session, and
a headless claude. No `gh auth login`, no first-run prompts, no manual git config.

A `reviewer_login` that disagrees with the `GH_TOKEN` is **fatal at the daemon's
own startup** (every round would look like round 1 and respawn forever) — so keep
the token and any configured login in sync.

The reviewer's claude launch command lives in [`agents.yaml`](./agents.yaml). To
pin the model, set `ALISSA_AGENT_MODEL` (see [Pinning the reviewer model](#pinning-the-reviewer-model))
rather than editing the file; to change flags, mount your own file over
`/home/alissa/.config/alissa/agents.yaml`. The image runs as a non-root user
because claude refuses `--dangerously-skip-permissions` as root. That flag is the
whole unattended contract: do **not** pair it with an explicit `--permission-mode`
(a mounted file included) — an explicit mode overrides the bypass and re-enables
the hard prompts (dangerous `rm`, un-allowlisted Bash) that nothing in a headless
container answers, so the session wedges at the dialog until it is killed.

### Pinning the reviewer model

The reviewer is the pipeline's **quality gate**: if its recall degrades nothing
downstream notices. The baked [`agents.yaml`](./agents.yaml) pins no model, so a
reviewer inherits whatever the persisted `claude /login` account defaults to —
and on plan-based accounts that default can **silently fall back to a smaller
model** once a usage threshold is hit. `ALISSA_AGENT_MODEL` makes the model an
explicit boot-time decision instead of a rebuild-time one. The unset default is
`claude-fable-5-1` — the latest and most capable generally-available Claude
model, one tier above Opus — because the reviewer is the pipeline's quality
gate.

At container boot the entrypoint appends `--model "$ALISSA_AGENT_MODEL"` to the
claude profile's `command:` and logs the effective command (grep the startup log
for `effective reviewer command:`).

| `ALISSA_AGENT_MODEL` | reviewer `command:` becomes |
| --- | --- |
| *(unset)* → default `claude-fable-5-1` | `claude … --model claude-fable-5-1` |
| `claude-opus-4-8` (any alias or full id) | `claude … --model claude-opus-4-8` |
| `default` *or* empty | `claude …` (no `--model` — restores account default) |

The value passes through **verbatim** — both aliases (`opus`, `sonnet`) and full
ids (`claude-opus-4-8`) are valid; there is no allowlist.

**Precedence.** The entrypoint only rewrites the **baked default** profile, which
it recognizes by an `alissa-managed:` marker comment. A custom `agents.yaml`
mounted over `/home/alissa/.config/alissa/agents.yaml` carries no such marker, so
it is used **verbatim** and `ALISSA_AGENT_MODEL` is ignored for it — the mounted
command (including any `--model` you set there) always wins. This is unchanged for
every other field: flags, `mode`, `quietSeconds`, and `promptPatterns` are
untouched, and reviewer posture (CR6: reviewers never write) stays enforced by the
round directive, independent of the model.

## Configuration (build ARGs — Railway-friendly)

Every non-secret knob is a build `ARG` baked into an `ENV` of the same name.
This is deliberate: **Railway's Dockerfile builds only expose service variables
that are declared as `ARG`** — runtime `ENV` set in the dashboard does not reach
a from-Dockerfile build's config. A runtime `-e VAR=...` still overrides the
baked default, so local `docker run -e ...` works too.

Set them at build time (Railway populates matching service variables
automatically; locally pass `--build-arg`):

| ARG / env | default | meaning |
| --- | --- | --- |
| `ALISSA_REVIEW_REPOS` | *(required if no manifest mounted)* | allowlist as one `\|`-separated string (see below) |
| `ALISSA_REVIEW_OPERATORS` | *(empty — no ack honoured)* | logins allowed to re-open a capped PR with `alissa-review: re-enter +N`, one `\|`-separated string; **pass-through** |
| `ALISSA_WORKSPACE` | `alissa-review` | workspace name in the generated manifest |
| `ALISSA_REVIEW_SKILLS` | `alissa-code-workspace\|alissa-code-review` | skills installed into every reviewer session (manifest `skills:`), `\|`-separated |
| `ALISSA_POLL_INTERVAL` | *daemon default* (currently 60) | seconds between polls (≥10); **pass-through** — unset ⇒ library default |
| `ALISSA_ROUND_CAP` | *daemon default* (currently 10) | CR9 round cap; **pass-through** — unset ⇒ library default |
| `ALISSA_STABILITY_ROUNDS` | *daemon default* (currently 3) | **product-stability guard**: how many consecutive `request_changes` rounds with an *empty* shipped-product diff stop the loop. The first stable round is still queued, carrying a notice that tells the reviewer to approve or name the shipped `file:line` that is wrong; a `request_changes` on that round with the product still unmoved queues nothing further and pages the operator once per head, lifted by the same `alissa-review: re-enter +N` ack a cap-out uses. `0` disables the guard entirely. **pass-through** — unset ⇒ library default. Needs `REVLOOP_VERSION >= 0.26.0` |
| `ALISSA_REAP_GRACE_SECONDS` | *daemon default* (currently 1800) | how long a reviewer session must be idle **and** quiet before the reap sweep kills it; **pass-through** — unset ⇒ library default |
| `ALISSA_REAP_SESSION_CAP` | *daemon default* (currently 6) | live reviewer sessions after a sweep above which the daemon logs page-worthy; **pass-through** — unset ⇒ library default |
| `ALISSA_MAX_CONCURRENT_SESSIONS` | *daemon default* (currently 4) | spawn gate: at this many live reviewer sessions of the daemon's own grammar, an owed round waits for a slot instead of spawning (it burns no round number and no attempt, and pages nobody). Must be **≤ `ALISSA_REAP_SESSION_CAP`** — the daemon refuses a config whose alarm sits below its spawn limit. **pass-through** — unset ⇒ library default. Needs `REVLOOP_VERSION >= 0.16.13` |
| `ALISSA_CHECKS_WAIT_SECONDS` | *daemon default* (currently 1800) | how long a round holds its approve while the judged head's CI rollup is still running before recording the verdict as a comment, **per condition waited on** (an unreadable hold promoted to a pending one restarts the clock once, so the worst case is 2×); a **red** rollup never waits and never approves; **pass-through** — unset ⇒ library default. Needs `REVLOOP_VERSION >= 0.16.7` |
| `ALISSA_CHECKS_SPAWN_WAIT_SECONDS` | *daemon default* (currently 900) | how long an owed round waits for the head's checks to **conclude** before its reviewer is queued at all — the only structural gate on the verdict a reviewer *session* posts (the knob above gates the one the daemon posts). A **red** or unreadable rollup never waits: the round is queued at once and forbidden to approve; past the bound it is queued anyway, told the checks never settled. The same number also bounds a reviewer session's own in-round wait (floored at 5 min), so tuning it moves both halves of the gate together. **pass-through** — unset ⇒ library default. Needs `REVLOOP_VERSION >= 0.17.0` |
| `ALISSA_REVIEW_TASK_MISS_TTL_POLLS` | *daemon default* (currently 10) | how many polls a PR with **no** Alissa review task is taken on trust before the daemon searches the actor's task corpus for one again. That search is the widest read the daemon makes and the PR→task mapping can only cache an answer that exists, so an unmapped PR used to pay the whole corpus every poll, indefinitely. Trades **latency** for reads: a review task created inside the window is picked up at the re-arm, not the next poll. Floor `1` — there is no value that disables it, and `0` is refused at config load. **pass-through** — unset ⇒ library default. Needs `REVLOOP_VERSION >= 0.18.0` |
| `ALISSA_TASK_LIST_SELF_SCOPE` | *(unset ⇒ library default `false`)* | `1`/`true`/`yes`/`on` narrows `alissa task list` to this actor's own rows (`--self`), dropping the sponsor's corpus; `0`/`false`/`no`/`off` is the explicit opposite and **anything else is refused** rather than rendered as `false`. Off by default on measured evidence: it saves ~4% of the payload, and a small minority of review tasks on the live fleet are owned by another actor — one the list cannot see is a round the daemon cannot count. Set it only where every review task is created by this daemon's own reviewer sessions. The other narrowings (status filter, digest view) are probed from the installed CLI and need no variable. **pass-through** — unset ⇒ library default. Needs `REVLOOP_VERSION >= 0.18.0` |
| `ALISSA_REV_LOOP_EVENTS_ENABLED` | *(unset ⇒ library default `false`)* | `1`/`true`/`yes`/`on` makes the daemon push loop telemetry (rounds spawned, verdicts posted, cap-outs, stability holds, stalls, checks holds, grants, reaps) to Studio's `POST /v1/loop-events` once per poll pass — one idempotent, ledger-derived batch, best-effort and never fatal, authenticated with the container's existing `ALISSA_API_TOKEN`; `0`/`false`/`no`/`off` is the explicit opposite and **anything else is refused** rather than rendered as `false`. The daemon library also reads this exact variable directly and it **wins over the rendered config and the CLI flags**, so the render can never contradict the env. Off by default: telemetry is an outbound write and an image upgrade must not start posting on its own. **pass-through** — unset ⇒ library default. Needs `REVLOOP_VERSION >= 0.28.0` |
| `ALISSA_AGENT_PROFILE` | `claude` | agent the worker launches (must name a profile in `agents.yaml`) |
| `ALISSA_AGENT_MODEL` | `claude-fable-5-1` | model pinned into the reviewer's claude command (see [Pinning the reviewer model](#pinning-the-reviewer-model)); `default` or empty omits the pin |
| `ALISSA_ON_MISSING_HUB` | `add` | `add` hub-ifies on demand; `skip` to require a mounted workspace |
| `ALISSA_WORKER_INTERVAL` | `2` | worker reconcile tick (seconds) |
| `ALISSA_ENABLE_FIREWALL` | `0` | `1` raises the egress firewall (needs `--cap-add=NET_ADMIN`) |
| `ALISSA_FIREWALL_EXTRA` | *(empty)* | extra firewall allowlist hosts, space-separated |
| `ALISSA_FORCE_CHOWN` | `0` | `1`/`true`/`yes`/`on` forces the root phase's full `chown -R` of the volume for that boot; anything else (incl. unset) leaves the probe in charge. Off by default: the entrypoint probes ownership at depth 1 and walks only when it finds a foreign owner (see [Boot-time ownership](#boot-time-ownership-the-volume-walk-is-guarded)). Set it for **one** boot after a root shell wrote files deeper in the tree |
| `ALISSA_WORKSPACE_PRUNE` | `1` (**on**) | volume hygiene: `1`/`true`/`yes`/`on` runs `alissa code workspace prune` at boot **and** on an interval; `0` opts out, after which nothing in the container reclaims anything. Default-on because this is the container whose volume grows (see [Volume hygiene](#volume-hygiene-finished-worktrees-are-pruned)). Skipped, with one loud warning, if the installed CLI predates the subcommand |
| `ALISSA_WORKSPACE_PRUNE_INTERVAL_MINUTES` | `360` | minutes between prune passes. Daemon role only — the executor gets the boot pass and no loop. A non-numeric or non-positive value warns and falls back to `360` |
| `ALISSA_WORKSPACE_PRUNE_TIMEOUT_SECONDS` | `240` | how long ONE prune pass may run before it is killed, so a slow first sweep over a never-pruned volume cannot hold the boot open. The default is chosen to sit inside a 300s health-check window — the pass runs *before* `/healthz` exists (see below). A timed-out pass warns (exit 124, or 137 if it had to be killed) and changes nothing. Garbage falls back to `240` |
| `ALISSA_WORKSPACE_PRUNE_KILL_AFTER_SECONDS` | `30` | grace a `SIGTERM`-trapping prune gets before `SIGKILL` (`timeout -k`). Without it a CLI that traps `TERM` is unbounded again. **Timeout + grace** is what must fit your health-check window |
| `ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS` | *(CLI default)* | passed straight through as `--min-age-hours`; **pass-through** — unset ⇒ the CLI's own age gate. A value that is not a whole number of hours **disables prune for that boot** instead of falling back (the CLI's default gate may be *shorter* than the one you asked for) |

#### Config precedence: env var > daemon library default

The optional tuning knobs `ALISSA_POLL_INTERVAL`, `ALISSA_ROUND_CAP`,
`ALISSA_STABILITY_ROUNDS`,
`ALISSA_REAP_GRACE_SECONDS`, `ALISSA_REAP_SESSION_CAP`,
`ALISSA_MAX_CONCURRENT_SESSIONS`, `ALISSA_CHECKS_WAIT_SECONDS`,
`ALISSA_CHECKS_SPAWN_WAIT_SECONDS`, `ALISSA_REVIEW_TASK_MISS_TTL_POLLS`,
`ALISSA_TASK_LIST_SELF_SCOPE`, `ALISSA_REV_LOOP_EVENTS_ENABLED` and
`ALISSA_REVIEW_OPERATORS` are
**pass-through**: their build `ARG` default is empty, and when they are unset the
entrypoint **omits the key entirely** from the generated `revloop.config.json`
so the daemon library applies its own current default. There is no hidden
entrypoint fallback layer that would shadow it — set the env var to override,
leave it unset to inherit the library default (which is why the "default" column
above says *daemon default* rather than a baked number, and why upgrading the
daemon can change these without a Dockerfile edit). The parenthetical values are
the library defaults at the pinned `REVLOOP_VERSION`, informational only.

`ALISSA_AGENT_PROFILE` and `ALISSA_ON_MISSING_HUB` are **not** pass-through: they
are container constants the image requires. `agent_profile` must name a profile
that the baked [`agents.yaml`](./agents.yaml) ships (`claude`), and
`on_missing_hub` must be `add` for the self-contained hub-ify-on-demand model —
the library default `skip` would make a fresh volume review nothing. Both are
still overridable via their env var.

#### Boot-time ownership: the volume walk is guarded

The container starts as root only to make a root-owned volume mount writable
(see [Persistence](#persistence--mount-the-volume-at-workspace)). That `chown -R`
used to run on **every** boot, and on a warm volume it is both a no-op and
expensive: the walk stats every inode of every worktree hub, and the resulting
dentry/inode slab is charged to the container's cgroup. An audit of the Railway
service on 2026-08-09 found `memory.current` at **~5.98 GB** against **176 MB**
of actual process RSS — 3.71 GB of it `slab_reclaimable` from the walk, the rest
page cache from the hub sync — with zero reviewer sessions running. It is
reclaimable cache, not a leak (the kernel drops it under real pressure at the
cgroup limit), but it plateaus flat from the moment of deploy and makes the
memory graph useless for spotting a real problem.

So step 0 now **probes before it walks**: one `stat` over the mount point and its
immediate children, and the `chown -R` runs only if something there is not
`alissa:alissa`. Owner **and** group, because that is what the walk asserts — a
probe testing only the owner would read an `alissa:root` entry as clean, and the
unconditional every-boot walk that used to repair the group is exactly what this
change removes. The probe is O(top-level entries) by construction — deliberately
not `find … ! -user alissa`, which stats every inode and would recreate the same
slab storm. A first boot on a fresh root-owned volume is unaffected: the mount
point itself is root-owned, so the probe trips and the full walk runs exactly as
before.

The blind spot is precisely one thing: **depth**. Anything below the mount point's
immediate children is invisible to the probe — realistically, a platform console
shell running as root. `ALISSA_FORCE_CHOWN=1` is the escape hatch: it skips the
probe and forces the full walk for that boot. Set it, redeploy, then unset it.

Two failure modes are worth knowing about, because the guard changed their
consequences. A walk that **fails** partway (a permission error deep in the tree)
still processes the mount point and its children — `chown -R` is post-order and
continues past per-entry errors — so the probe would read the tree as clean
forever after. The entrypoint therefore logs `WARNING: chown -R … reported errors`
and names `ALISSA_FORCE_CHOWN=1` as the remedy; that warning is the only signal
there is, so it is worth alerting on. A walk that is **interrupted** (the container
is killed mid-boot) is self-healing for the same post-order reason: the mount point
is chowned last, so a partial walk leaves the probe tripping on the next boot.

Reclaiming the cache after the fact is not an option on Railway: `/sys/fs/cgroup`
is mounted read-only inside the container, so writing `memory.reclaim` fails with
`EROFS` even as root (verified 2026-08-09). Avoidance is the only lever, which is
why the entrypoint has no reclaim call.

#### Volume hygiene: finished worktrees are pruned

**The diagnosis.** Until issue #81 this container removed *nothing*, ever. Every
review round materializes a per-PR worktree inside a hub (`<repo>/TASK-…`), plus
whatever a session's build or test run leaves in it; a merged or closed PR left
its worktree exactly where it was; and the bare `.source` object store behind
each hub only ever grew, because nothing pruned the remote-tracking refs the
deleted branches left behind. There was no cleanup path anywhere in the image —
not at boot, not on merge, not on a timer — so the only bound on the volume was
the volume's size, and the only remedy was a human with a shell.

**The remediation** is the CLI's own primitive, `alissa code workspace prune`,
which removes finished-branch worktrees behind its safety rails (never `main/`,
never a dirty tree without `--force`, an age gate, a live-tmux-session guard,
never a worktree whose PR is still open, and a lookup failure *keeps* the
worktree) and then runs the hub-level `git worktree prune` /
`git remote prune origin` / `git gc --auto` sequence. The entrypoint calls it in
two places:

- **at boot**, right after the hub sync, best-effort — a failure logs a warning
  and the boot continues, exactly like the other best-effort boot steps. Prune
  is an optimization, never a boot precondition;
- **on an interval** (`ALISSA_WORKSPACE_PRUNE_INTERVAL_MINUTES`, default 360), as
  a supervised background loop that the shutdown handler kills alongside the
  console sidecar. A failing pass warns and the loop waits for the next tick; it
  never takes the container down.

Three properties are worth stating explicitly, because they are what make this
safe to leave on by default:

- **`--force` is never passed, and there is no env knob that adds it.** In a
  review container a dirty worktree is *evidence* — a session that died
  mid-round with work someone may still want. The rail that keeps it is the one
  this container needs most.
- **It is capability-gated, not version-pinned.** The alissa CLI is not installed
  by this image at all — it arrives prebuilt in the [base image](#base-image), and
  therefore advances only when someone bumps the `FROM` pin, not on every rebuild
  here. So the CLI in any given image may predate the subcommand, and may stay
  that way across many builds of this repo — which makes capability-gating more
  necessary than it was when this Dockerfile installed the CLI itself, not less: a
  version pin written here would go stale against a layer this file no longer
  owns. The entrypoint probes once at boot and, if the subcommand is missing, logs
  exactly one loud
  `WARN: this alissa CLI predates 'alissa code workspace prune' …` and skips
  *both* hooks. Nothing here needs to change when a base bump brings a newer CLI.
  (The probe reads the help *output*, not its exit status: commander answers an
  unknown subcommand by printing the parent command's help and exiting `0`, so an
  exit-status probe would call every old CLI capable.)
- **The interval loop is daemon-only.** The [executor role](#bridge-executor-role-a-second-service-from-this-same-image)
  gets the boot pass — it grows the same volume the same way — but not the loop:
  that role `exec`s `alissa bridge start`, which replaces the shell, so a loop
  started there would outlive the only code that knows how to stop it.

**A pass cannot hold the boot open.** The boot hook runs before `alissa worker`
starts, and the first pass after the CLI lands sweeps `git gc --auto` across hubs
on a volume that has never had anything reclaimed — minutes of work on a large
object store, and on a platform with a startup health-check window a slow enough
boot is a *dead* boot. So a pass runs under `timeout` with **stdin closed**, and
a pass that hits either bound lands in the same warning path as any other
failure.

The default of **240s** is picked against a window, not chosen for roundness. The
prune pass runs at step `3c-ii`; the console that serves `/healthz` — the health
check [the Railway walkthrough above](#the-console-on-railway) tells you to
configure — does not start until `4b`. The pass therefore sits *in front of* the
endpoint the platform probes, and Railway's healthcheck timeout defaults to 300s.
`ALISSA_WORKSPACE_PRUNE_KILL_AFTER_SECONDS` (30) is the grace a `SIGTERM`-trapping
CLI gets before `SIGKILL`; **it is the sum that must fit**, not the timeout alone.

**This bounds the prune step, not the boot.** 240 + 30 < 300 says prune alone fits
the window; it does not say the boot does, and nothing here bounds the total. Two
unbounded steps run in front of it: `alissa code workspace sync` at `3c` is
deliberately unbounded (hubs are a precondition — without them the daemon reviews
nothing, so blocking on it is correct), and the auth retry loop at `2c` backs off
to a 600s ceiling, which exceeds the whole window on its own. So read 240 as
prune's *share* of a window other steps also draw on — the reason to prefer it
over 900 is that 900 gave this one step more than the entire window. If your
deployment has a health check in front of it, budget across all three, and when
you raise the timeout for a known-slow first sweep keep timeout + grace inside
whatever is left. The neighbouring `alissa code workspace sync` is deliberately *not*
bounded this way: hubs are a precondition for reviewing anything, while prune is
an optimization, and only one of those may delay a boot.

> **`ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS` is test-only — do not set it in a
> deploy.** `docker/claude/tests-entrypoint-prune.sh` has to watch the loop tick
> more than once, and the smallest cadence the supported knob can express is a
> minute. It silently overrides
> `ALISSA_WORKSPACE_PRUNE_INTERVAL_MINUTES`, and the boot log then reads
> `starting the workspace prune loop (every 360 min = 3s)` — which is what you
> will find first if it is ever set by accident.

If you turn this off (`ALISSA_WORKSPACE_PRUNE=0`), nothing else reclaims the
volume; plan on pruning by hand.

### Reviewer console (runtime env only — `ALISSA_UI_ENABLED`, `ALISSA_UI_PASSCODE`, `PORT`)

The image can also serve the **reviewer console**
([`alissa-revloop-ui`](../../README.md#reviewer-console-alissa-revloop-ui)) — a
read-only operator dashboard sidecar — alongside the worker and daemon. It
renders the daemon's own local exhaust from this container's volume
(`/workspace/.revloop/state.db`: poll snapshots, the spawn ledger, escalations,
pings) plus the live `review-*` tmux sessions, so it costs **no** GitHub API
budget beyond two cached checks. It is **opt-in and off by default**, and its
knobs are **runtime-only** (not build `ARG`s), for the reasons in each row:

| variable | default | meaning |
| --- | --- | --- |
| `ALISSA_UI_ENABLED` | *(unset ⇒ off)* | `1`/`true`/`yes`/`on` starts the console sidecar; anything else (incl. unset) leaves **no listener**. Runtime-only so it pairs with the two below; the entrypoint already defaults it off, so no ARG default is needed |
| `ALISSA_UI_PASSCODE` | *(none)* | **the console's only gate** — a secret, so never a build ARG (an ARG leaks into `docker history`, like the tokens). With `ALISSA_UI_ENABLED` set, an **empty passcode dies at boot** (fail-closed, consistent with the identity gates) |
| `PORT` | `8080` | bind port for the console. Platform-injected at runtime (Railway sets it); the entrypoint binds `0.0.0.0:${PORT:-8080}` so the platform can route a public URL to it. `EXPOSE 8080` in the Dockerfile documents the default |
| `ALISSA_UI_SECURE_COOKIE` | *(unset ⇒ off)* | `1` adds `Secure` to the session cookie — set it whenever the console rides a TLS-terminated public URL |

When enabled the console binds **`0.0.0.0`** (not the sidecar's own
`127.0.0.1:8788` default) so the platform's router can reach it, and exposes an
unauthenticated **`/healthz`** liveness endpoint (`{"ok": true, "version": …}`) —
use it as the deployment healthcheck. The console is a **sidecar**: if it exits
the daemon and worker keep running, but the entrypoint logs the exit loudly (the
URL is dead until the next redeploy). It is started *after* the worker, so its
first paint already shows the real process list. See [Railway](#the-console-on-railway)
for the public-networking warning.

```
ALISSA_UI_ENABLED=1
ALISSA_UI_PASSCODE=<a long random secret>
# PORT is set by the platform; locally it defaults to 8080
```

```sh
docker run -d --name alissa-review \
  -e GH_TOKEN -e ALISSA_API_TOKEN \
  -e ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app" \
  -e ALISSA_UI_ENABLED=1 -e ALISSA_UI_PASSCODE="$(openssl rand -hex 24)" \
  -p 8080:8080 \
  -v alissa-review-workspace:/workspace \
  alissa-review-daemon -v
# then: curl -fsS localhost:8080/healthz  ->  {"ok": true, "version": "…"}
```

The wiring has its own suite — [`tests-entrypoint-ui.sh`](./tests-entrypoint-ui.sh)
boots this entrypoint with the console off, enabled-without-a-passcode, enabled,
and with the sidecar killed under it (CLIs stubbed, sidecar real, no docker
required); it runs in CI beside the config-renderer suite.

> ⚠️ Enabling the console and turning on the service's public networking puts the
> operator dashboard — including its **kill** and **retry-now** actions — on the
> public internet, gated **only** by `ALISSA_UI_PASSCODE`. Use a long, random
> passcode, or leave the console disabled and reach it over a private network /
> port-forward instead.

#### The console on Railway

1. Set `ALISSA_UI_ENABLED=1` and a **long, random** `ALISSA_UI_PASSCODE` as
   service variables (both runtime-only — the passcode is a secret, so it must
   not be an ARG). Enabling without a passcode makes the container **die at
   boot** with a clear message, before the worker or daemon start.
2. Under the service's **Settings → Networking**, enable a public domain (or a
   TCP proxy). Railway injects `PORT` and routes the public URL to it; the
   entrypoint already binds `0.0.0.0:${PORT:-8080}`.
3. Set the **Healthcheck Path** to **`/healthz`** — the console's unauthenticated
   liveness endpoint (`{"ok": true, "version": …}`). It reports up without
   exposing any data or needing the passcode. (Leave the healthcheck unset while
   the console is disabled: with no listener there is nothing to probe.)
4. When the console rides a TLS-terminated public URL, also set
   `ALISSA_UI_SECURE_COOKIE=1` so the session cookie carries `Secure`.

> ⚠️ **The console then rides the public URL, gated ONLY by the passcode.** A
> public domain + `ALISSA_UI_ENABLED=1` exposes the dashboard — and its **kill**
> and **retry-now** actions, which can end a running reviewer — to anyone who
> reaches the URL; `ALISSA_UI_PASSCODE` is the sole gate (behind a login throttle
> and CSRF, but still one shared secret). Prefer a private network / port-forward,
> or keep the passcode long and random. Leave `ALISSA_UI_ENABLED` unset to serve
> nothing at all.

### The repos allowlist string

`ALISSA_REVIEW_REPOS` is a single string, entries separated by **`|`**. `|` is
used because repo slugs already contain `/` (so `/` can't be the delimiter, and
`;`/`:` are noisier). A single repo needs no separator; whitespace around entries
is stripped.

```
ALISSA_REVIEW_REPOS=fahera-mx/studio.alissa.app|fahera-mx/blog.alissa.app
ALISSA_REVIEW_REPOS=fahera-mx/studio.alissa.app          # one repo
```

A non-empty allowlist is required whenever `on_missing_hub` is `add` — the daemon
refuses to hub-ify unattended without one.

## Workspace: bootstrap-from-manifest

Reviewers `cd` into `{root}/{repo}/main` worktree hubs. This image is
self-contained: with `on_missing_hub: add` the daemon hub-ifies each repo itself
on the first review request, so **you do not pre-clone anything**. The entrypoint
guarantees a manifest and a `revloop.config.json` exist under
`ALISSA_WORKSPACE_ROOT` (`/workspace`, fixed).

**When `ALISSA_REVIEW_REPOS` is set it is authoritative**: the entrypoint
regenerates both files from it on **every boot**, so changing the allowlist (or
`ALISSA_POLL_INTERVAL`, `ALISSA_ROUND_CAP`, `ALISSA_STABILITY_ROUNDS`, …) and
redeploying just applies — the
files persist on the volume, so a "generate only if absent" rule would otherwise
pin them to the first boot's values forever. Leave `ALISSA_REVIEW_REPOS` **unset**
to instead run against a workspace you've mounted at `/workspace` as-is.

## Run

Start with a dry run against the first real pending request before letting it run
unattended (mirrors the daemon's own "not verified live" caveat):

```sh
docker run --rm -it \
  -e GH_TOKEN \
  -e ALISSA_API_TOKEN \
  -e ANTHROPIC_API_KEY \
  -e ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app" \
  alissa-review-daemon --once --dry-run -v
```

Everything after the image name is passed straight to `alissa-revloop`, so
`--once`, `--dry-run`, `-v` all work.

Unattended, persisting the workspace (the cloned hubs) across restarts:

```sh
docker run -d --name alissa-review \
  --restart unless-stopped \
  -e GH_TOKEN \
  -e ALISSA_API_TOKEN \
  -e ANTHROPIC_API_KEY \
  -e ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app|fahera-mx/blog.alissa.app" \
  -v alissa-review-workspace:/workspace \
  alissa-review-daemon -v
```

### Persistence — mount the volume at `/workspace`

Mount your volume at **`/workspace`** (the value of `ALISSA_WORKSPACE_ROOT`).
Everything worth surviving a restart lives there:

- `alissa-workspace.yaml` + `revloop.config.json` (regenerated from
  `ALISSA_REVIEW_REPOS` each boot when it's set, else whatever you mounted);
- the cloned worktree hubs `<owner>/<repo>/main` — persisting them means a
  restart does **not** re-clone every repo;
- `.claude-config/.credentials.json` — the persisted `claude /login` (see the
  claude-auth section); this is why the login survives restarts;
- `.revloop/state.db` — the spawn ledger. Its `escalations` (cap-out memory)
  are worth keeping so a restart doesn't re-escalate a capped PR.

The ledger's **in-flight `spawns` are cleared on every boot** by the entrypoint:
the tmux server, its reviewer sessions, and the worker queue all live in the
ephemeral home and are gone on restart, so a persisted "round N in-flight" would
otherwise be stale and stall re-enqueue for 90 min. A fresh container has no
reviewer running by definition, so clearing them is safe and the daemon
re-enqueues any still-pending round on its first poll.

The volume is also the thing that **grows**: reviewer worktrees accumulate in
the hubs and nothing used to remove them. The entrypoint now prunes finished-branch
worktrees at boot and every `ALISSA_WORKSPACE_PRUNE_INTERVAL_MINUTES` (default
360) — see [Volume hygiene](#volume-hygiene-finished-worktrees-are-pruned) for the
rails it runs behind and `ALISSA_WORKSPACE_PRUNE=0` for the opt-out.

Nothing else needs a volume: the gh/alissa/claude auth is re-established from the
env tokens on every boot, and tmux sockets are deliberately ephemeral.

On Railway, set the volume's mount path to `/workspace`. Persistent platform
volumes typically mount **root-owned**, so the container starts as root, the
entrypoint `chown`s the mount to `alissa` (uid 1000), and then drops to that user
(via `gosu`) for everything else — so a root-owned mount just works, no manual
`chown` or init container needed. (claude still runs unprivileged, as it must.)

That `chown` is **guarded**: on a warm volume it is skipped, because walking it
charges GBs of reclaimable kernel cache to the container's memory metric for no
benefit. See [Boot-time ownership](#boot-time-ownership-the-volume-walk-is-guarded)
for the numbers and for `ALISSA_FORCE_CHOWN`, the escape hatch to use after a root
console shell has written into the volume.

### Optional egress firewall

For unattended runs, lock egress to the hosts the loop needs (GitHub, Anthropic,
Alissa, the package registries). Needs `NET_ADMIN`:

```sh
docker run -d --name alissa-review \
  --cap-add=NET_ADMIN \
  -e ALISSA_ENABLE_FIREWALL=1 \
  -e ALISSA_FIREWALL_EXTRA="ghe.example.com" \
  ... \
  alissa-review-daemon -v
```

See [`init-firewall.sh`](./init-firewall.sh) for the allowlist.

## Bridge executor role (a SECOND service from this same image)

`CONTAINER_ROLE=executor` boots this image as an **Alissa Studio queue
executor** instead of the review daemon. The executor is `alissa bridge start`:
it registers a `bridgeExecutors` row, polls `/v1/bridge/jobs`, and runs each
claimed job as an `alissa code --handoff claude` session in its own tmux session
inside the workspace hubs.

### Why a separate service, and not a sidecar of the daemon

This is a pinned decision, recorded here so it survives the next round of
refactoring:

1. **Different restart domains.** A queue job is a tmux session that runs for
   hours (6h default budget). Every revloop redeploy or restart would kill the
   in-flight ones, and each hand-back costs the job a retry attempt
   (`reconcileResumed`, attempt + 1). The review daemon redeploys often; the
   executor must not.
2. **A separate service is a separate environment, and that is a security
   control.** The executor resolves a job spec's env **names** out of its own
   process environment (`resolveJobEnv`), so *every variable this service holds
   is nameable by any queue agent belonging to the token's user*. Running it as
   its own service is what lets you give it a minimal credential set instead of
   the review daemon's full one. See [Exactly what a job spec can
   name](#exactly-what-a-job-spec-can-name) below.
3. **An in-container background sidecar was considered and rejected.** That
   pattern (the devloop repo's UI console) is fine for a stateless console; it is
   wrong for hour-long stateful jobs, for reason 1.

The entrypoint enforces the split by construction rather than by convention: the
executor role `exec`s the CLI, so the container's only process is the executor —
no `alissa worker`, no `alissa-revloop`, no console. And the daemon role never
starts a bridge, however the `ALISSA_BRIDGE_*` variables are set. Both directions
are asserted in
[`tests-entrypoint-executor.sh`](./tests-entrypoint-executor.sh).

### Deploying it

Same image, same Dockerfile, no second build. On Railway: add a second service
from this repo, attach **its own** volume at `/workspace`, and set the runtime
variables below. Locally:

```sh
docker run -d --name alissa-bridge-executor \
  -e CONTAINER_ROLE=executor \
  -e ALISSA_BRIDGE_EXECUTOR=1 \
  -e ALISSA_BRIDGE_EXECUTOR_ID=revloop-executor \
  -e ALISSA_BRIDGE_LABEL="Revloop executor" \
  -e ALISSA_API_TOKEN=alissa_… \
  -e GH_TOKEN=ghp_… \
  -e ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app" \
  -v alissa-executor-workspace:/workspace \
  alissa-review-daemon
```

Extra CMD args pass through to `alissa bridge start`, so
`docker run … alissa-review-daemon --once` is a one-poll smoke test.

### Configuration (runtime env only — no build ARGs)

Unlike the daemon knobs, none of these is a build ARG. `CONTAINER_ROLE` is what
makes one image serve two services, so baking it would make the artifact
role-specific; `ALISSA_BRIDGE_EXECUTOR` is an arming gate, so it ships unset; and
the rest only mean anything on a service that already set those two.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONTAINER_ROLE` | `daemon` | `executor` selects this role. Any other value **dies at boot** naming the two the image ships |
| `ALISSA_BRIDGE_EXECUTOR` | *(unset ⇒ off)* | the arming gate: `1`/`true`/`yes`/`on`. With the role set and this off, the container **refuses cleanly** and registers nothing — selecting the role is not consent to claim jobs |
| `ALISSA_BRIDGE_EXECUTOR_ID` | `revloop-executor` | **required** (an explicitly empty value is fatal), validated as a slug at boot. It **must differ from every other executor of the same Alissa user** — the devloop image's executor above all. Identity is `(userId, executorId)` and registration *takes over* an existing row, so a shared id means two services evicting each other in a loop and stranding every sticky claim pinned to it. Never left to the CLI's own fallback (a slugified hostname, which on a platform is a fresh string per deploy) |
| `ALISSA_BRIDGE_HANDOFF` | `claude` | agent used when a job spec names none. Structural, like `ALISSA_AGENT_PROFILE`: it must name a profile in the baked [`agents.yaml`](./agents.yaml) |
| `ALISSA_BRIDGE_LABEL` | *(CLI default: the hostname)* | human name shown in Studio; **pass-through** — unset ⇒ the CLI decides |
| `ALISSA_BRIDGE_MAX_CONCURRENT` | *(CLI default)* | jobs to run at once (the CLI clamps to 1–16); **pass-through** |
| `ALISSA_BRIDGE_POLL_SECONDS` | *(CLI default: 15)* | seconds between queue polls; maps to the CLI's `--interval`; **pass-through** |

The model pin works exactly as it does for the daemon: `ALISSA_AGENT_MODEL`
(default `claude-fable-5-1`) is rewritten into the `claude` profile's `command:`
at boot, and job sessions inherit it. The profile deliberately carries **no**
`disable_alissa_code`, which is what makes the CLI launch it via `alissa code -y
--handoff claude` — that wrapper is what registers the codeSession and its
10-minute log checkpoints. Adding the flag would launch a bare `claude` and lose
both.

### Exactly what a job spec can name

A job spec carries environment variable **names**, never values; the executor
resolves each name against its own process environment and fails the job
(`spec_rejected`) if one is missing. So the set of variables a queue agent can
pull into a job session is *exactly the set this service holds*. Keep it minimal.

What the executor genuinely needs:

| Variable | Why |
| --- | --- |
| `ALISSA_API_TOKEN` | polls the job queue and reports results — without it there is no executor |
| `GH_TOKEN` (or `GITHUB_TOKEN`) | the workspace bootstrap clones the hubs, and job sessions do the repo work |
| `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` | only if you are not using the persisted `claude /login` on the volume (which is preferred — see [claude auth](#claude-auth-log-in-once-persisted-on-the-volume-recommended)) |
| `ALISSA_REVIEW_REPOS` | the repo allowlist the manifest (and therefore the hubs) is built from |

What it must **not** carry:

* **the reviewer's GitHub token.** An executor posts no verdicts. If
  `ALISSA_REVIEWER_TOKEN_ENV` is set on an executor service the entrypoint warns
  and ignores it — but the *token itself* being present is the problem, because a
  job spec can name it.
* **`ALISSA_UI_PASSCODE`.** The console is a review-loop surface and is never
  started in this role.
* anything else the review daemon happens to hold. Copying the daemon service's
  variable set across is the mistake this whole split exists to prevent.

### Persistence, and what happens when the volume is not there

Two things have to outlive a redeploy, and both live on the `/workspace` volume —
plus a third that lands there as a consequence, and that you should know about:

* **the executor identity** — `${ALISSA_CONFIG_DIR}/bridge-executor.json`, the id
  plus the fingerprint Studio displays. In this role `ALISSA_CONFIG_DIR` defaults
  to `/workspace/.alissa-config` (the CLI's own default is the *ephemeral* home,
  which would mint a new fingerprint on every boot and re-register as a changed
  machine). Set it explicitly only if you move the volume.
* **the claude credential** — `CLAUDE_CONFIG_DIR`, already `/workspace/.claude-config`
  in this image. The entrypoint verifies it is under the workspace root and warns
  if a deploy moved it off; a queue job is long and unattended, and re-logging in
  is manual.

* **the Alissa API token — a consequence, not a goal.** `alissa auth login` (the
  same preflight the daemon role runs) writes the *verified* `ALISSA_API_TOKEN`
  in cleartext to `${ALISSA_CONFIG_DIR}/config.json` with default permissions.
  Relocating that directory onto the volume therefore moves that secret from the
  ephemeral home to persistent storage. Nothing about the executor needs it
  persisted — it is the price of persisting the identity file that sits beside
  it — but the volume now holds a live credential, so give it the same handling
  you would give any credential store: no snapshots into shared buckets, no
  attaching it to a second service to "have a look", and rotate the token if the
  volume is ever exposed. This is the same class of exposure as
  [Exactly what a job spec can name](#exactly-what-a-job-spec-can-name) above,
  one layer down: that section is about what a job can *read from the process*,
  this is about what sits *on the disk*.

The resolved `agents.yaml` is copied into the executor's config dir on **every**
boot, so the image — not a file left behind by an older image — is always the
source of truth for the profile.

The CLI's executor **lockfile** (`…/bridge/executor-<id>.lock`) also lands in
that directory, and it is deliberately *not* treated as persistent state: the
entrypoint deletes this executor's lock immediately before starting. A lock is a
claim that a process on this machine is already running, decided by a bare
`kill(pid, 0)`; a container that died ungracefully leaves one behind, and the
next boot's fresh PID namespace can easily make that dead PID look alive —
refusing to start, forever. A fresh container has no executor running by
definition, so the lock is stale by construction. Only *this* executor's lock is
removed, never every `executor-*.lock`, so a lock belonging to some other id is
left alone rather than pulled out from under whoever owns it.

Volume unavailability is **not** fatal. Because the identity dir is the same
directory the `alissa auth login` preflight probes, a missing or root-owned mount
lands in that gate's "config dir unwritable" class: it logs what it saw and
retries with capped backoff, forever, and proceeds the moment the mount appears —
[the same posture](#alissa-auth-failures-are-triaged-not-guessed) the rest of the
entrypoint takes, and deliberately not a crash loop.

### Firewall

The egress allowlist is shared by both roles and **verified**, not assumed: a job
session needs the Alissa API (`api.alissa.app`), `api.anthropic.com`, GitHub
(`github.com`, `api.github.com`, `codeload.github.com`,
`objects.githubusercontent.com`) and the skill/installer hosts
(`skills.alissa.app`, `share.alissa.app`), and every one of those is already in
[`init-firewall.sh`](./init-firewall.sh). Nothing had to be added for this role.
`firewall_domains` there is sourceable precisely so
[`tests-entrypoint-executor.sh`](./tests-entrypoint-executor.sh) asserts that
membership against the shipped list instead of a second copy of it. Package
registries (`registry.npmjs.org`, `pypi.org`, `files.pythonhosted.org`) are
allowed too, which a job that installs dependencies will want; extend with
`ALISSA_FIREWALL_EXTRA` for anything else.

## docker-compose

```yaml
services:
  review-daemon:
    build:
      context: ./docker/claude
      # Non-secret config is baked at build time (matches the Railway/ARG model).
      args:
        ALISSA_REVIEW_REPOS: "fahera-mx/studio.alissa.app|fahera-mx/blog.alissa.app"
        ALISSA_POLL_INTERVAL: "90"
    restart: unless-stopped
    environment:
      # Secrets ride runtime env — never baked into the image.
      GH_TOKEN: ${GH_TOKEN}
      ALISSA_API_TOKEN: ${ALISSA_API_TOKEN}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    volumes:
      - alissa-review-workspace:/workspace
    # For the egress firewall:
    # cap_add: ["NET_ADMIN"]
    # environment: { ALISSA_ENABLE_FIREWALL: "1" }
volumes:
  alissa-review-workspace:
```

## What the entrypoint does

0. Resolve `CONTAINER_ROLE` (`daemon` by default) and, in the `executor` role,
   its gates — the arming flag, the executor id, and where the identity is
   persisted. A bad role or an unarmed executor **dies here**, before any
   bootstrap. See [Bridge executor role](#bridge-executor-role-a-second-service-from-this-same-image).
0b. As root: make the `/workspace` mount writable by `alissa` — probing
   ownership (owner **and** group) at depth 1 and running the full `chown -R` only
   when the probe finds something foreign or `ALISSA_FORCE_CHOWN=1` says so, and
   **warning** if that walk reports errors
   ([why](#boot-time-ownership-the-volume-walk-is-guarded)) — then (optionally)
   raise the egress firewall and drop to `alissa` via `gosu`. Everything below
   runs unprivileged.
1. Preflight + onboard the identities: validate `gh` (fatal if missing) and run
   `gh auth setup-git`; `alissa auth login` — fatal only when the API **rejects**
   the token, otherwise triaged and retried (see
   [`alissa auth` failures are triaged](#alissa-auth-failures-are-triaged-not-guessed));
   check the claude credential (warn-only — the baked `agents.yaml` handles
   headless launch).
   Also resolve the `ALISSA_UI_ENABLED` console gate and, when enabled, **die
   here** if `ALISSA_UI_PASSCODE` is empty (fail-closed, fail-fast); then resolve
   `ALISSA_AGENT_MODEL` into `agents.yaml` and log the effective command.
2. Ensure a manifest + `revloop.config.json` exist (mount or generate).
3. **`alissa code workspace sync`** — materialize the worktree hubs the manifest
   declares (create missing/half-built ones, fetch existing). Without this the
   daemon's on-demand `alissa code workspace add` no-ops on a repo already listed
   in the manifest, leaving an empty folder and looping forever hub-ifying a hub
   that never completes.
3c-ii. **`alissa code workspace prune`** — best-effort volume hygiene, on the path
   both roles share: remove finished-branch worktrees (never `main/`, never
   dirty, never an open PR, never `--force`) and run the hub-level git prune/gc.
   Skipped with one loud warning if the CLI predates the subcommand, or if
   `ALISSA_WORKSPACE_PRUNE=0`, and bounded by `timeout -k` with stdin closed so a
   slow, prompting, or `SIGTERM`-trapping pass cannot hold the boot open. A failure here is a warning, never a dead boot
   ([why](#volume-hygiene-finished-worktrees-are-pruned)).
3d. **Executor role only, and the end of the shared path**: drop this executor's
   stale lockfile, then `exec alissa bridge
   start …`. `exec`, so the container's only process is the executor — steps 4
   and 5 below never run in this role, and the daemon role never reaches this
   step. Nothing after this point applies to an executor service.
4. Start `alissa worker --daemon`, wait until it reports running (the daemon only
   *warns* if the worker is absent, so ordering matters).
4b. When `ALISSA_UI_ENABLED` is set, start the reviewer console
   (`alissa-revloop-ui`) backgrounded on `0.0.0.0:${PORT:-8080}` — a sidecar,
   not the primary function, so a monitor logs loudly if it exits but does not
   tear the container down.
5. Run `alissa-revloop` in the foreground; stop the worker (and the console
   sidecar) on `SIGTERM`/`SIGINT`.

`tini` is PID 1 to reap the tmux/node/claude child fan-out (the console sidecar
included).

## Crash resilience: the daemon survives its substrate

Three outages on 2026-07-29 all had the same shape — the container's filesystem
misbehaved for a moment, and the daemon died of it instead of reporting it. The
container is not assumed to be well-behaved any more:

* **One bad poll is one bad poll.** `run_forever` firewalls every exception a
  poll pass can raise (a subprocess `ENOENT`, a parse error, an `OSError`, an
  sqlite error, anything). It logs with streak limiting, backs off by doubling
  the poll interval up to 15 minutes, and keeps polling. `Ctrl-C` and
  `SystemExit` still stop it, and a **startup** config error still exits `2`
  fast — a bad config file is not something retrying fixes.
* **A stuck daemon is loud, not silent.** If the *same* exception class fires on
  every poll for 30 minutes, the log level escalates to page-worthy `ERROR`
  ("the daemon is alive and still retrying… but this is no longer transient").
  When it heals, that is logged too, so the last line about a recovered daemon
  is not its failure.
* **Telemetry never outranks the loop.** The `poll_snapshots` write — pure
  observability for the console — is best-effort: a database error is absorbed,
  retried once through a reconnect, and reported as a streak-limited `WARN`.
* **The daemon never takes an action it cannot record.** Before each pass it
  probes whether the ledger can be written. If it cannot, that pass decides
  *nothing at all* — no reviewer spawned, no operator paged, no cap granted, no
  verdict posted, and no session reaped — and the daemon keeps probing until the
  volume comes back, then resumes by itself.

  This is the part that is easy to get wrong, so it is worth being precise about:
  the ledger's other writes stay **strict** (they raise), but strictness is not
  what protects you. Raising only aborts the pass that failed; the firewall then
  hands the loop straight back to the same code, with the side effect already
  taken. Without the gate, a read-only volume turned "queue a reviewer, then fail
  to record it" into a *fresh reviewer agent every poll, forever*. Strictness
  makes an unrecordable action visible; the gate is what stops it repeating.
  (`--dry-run` is exempt — it takes no action to protect — so it still answers
  "what would you do right now" during an incident.)

What this means operationally — three states, and the third is the one that
needs a runbook line:

| log line | what it means | what to do |
| --- | --- | --- |
| `WARN: poll failed … retrying in 120s` | riding out a blip; still deciding | nothing |
| `ERROR: poll has failed with … on every attempt` | same fault every poll for 30 min; still deciding, still retrying | look at it — this one is not healing |
| `ERROR: ledger at … cannot be written` | **up, polling, and deliberately deciding nothing.** No review will be queued | fix the volume mount or its ownership; the daemon resumes on its own, no restart |

A container that is up is therefore not the same as a container that is working
— the third row is the case where everything looks healthy and no review is
being queued at all.
