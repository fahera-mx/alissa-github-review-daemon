# Containerized review daemon (`docker/claude`)

Runs the GitHub review loop unattended in a container: the `alissa-reviewloop`
poller, an `alissa worker`, and the `claude` reviewer agent it spawns — all in
one image.

This is **not** a thin Python-daemon container. The daemon only watches GitHub
and enqueues sessions; the worker is what drains the queue and spawns reviewers,
so the image bundles all three tiers (see the top-of-file comment in
[`Dockerfile`](./Dockerfile)).

## Build

```sh
docker build -t alissa-review-daemon docker/claude

# with configuration baked in (see the Configuration table):
docker build \
  --build-arg REVIEWLOOP_VERSION=0.2.0 \
  --build-arg ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app|fahera-mx/blog.alissa.app" \
  --build-arg ALISSA_POLL_INTERVAL=90 \
  --build-arg ALISSA_ROUND_CAP=3 \
  -t alissa-review-daemon docker/claude
```

The image installs the daemon from PyPI, so the build context is just this
directory — no repo source is copied in.

### On Railway

Set the config values (`ALISSA_REVIEW_REPOS`, `ALISSA_POLL_INTERVAL`, …) as
**service variables** — Railway passes any variable matching a declared `ARG`
into the Dockerfile build, which is why these are ARGs and not plain runtime
ENV. Set the three **secrets** (`GH_TOKEN`, `ALISSA_API_TOKEN`,
`ANTHROPIC_API_KEY`) as service variables too; those are read at runtime and
must NOT be baked in.

## The three identities (self-onboarding)

The loop depends on three independent identities. Provide all three as **runtime
env** (secrets — never baked into the image); the entrypoint does the rest of the
onboarding automatically, so you only supply tokens:

| env var | identity | required? | what the entrypoint does |
| --- | --- | --- | --- |
| `GH_TOKEN` | `gh` (the `alissa-app` GitHub user) | **yes** — fatal if missing | validates via `gh api user`; the image rewrites GitHub SSH URLs to HTTPS + wires gh as the git credential helper, so hub-ify's `git clone` authenticates with the token (no SSH key needed) |
| `ALISSA_API_TOKEN` (`alissa_…`) | Alissa by Fahera | **yes** — fatal if missing | `alissa auth login --token` (stores + verifies) |
| a persisted `claude /login` *(recommended)*, or `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` in env | claude | no — warns, continues | credential persists on the volume via `CLAUDE_CONFIG_DIR`; the baked [`agents.yaml`](./agents.yaml) launches claude headless and the first-run config is pre-seeded (see below) |

`GH_TOKEN` and `ALISSA_API_TOKEN` are hard requirements — the daemon can't poll
GitHub or reach the task queue without them. The **claude credential is not**: the
daemon never calls claude directly (only the worker-spawned reviewer does), and
claude can authenticate by other means — a mounted `~/.claude` credential or
Bedrock/Vertex env. If none is present the entrypoint just warns, and a reviewer
that genuinely has no credential fails on its own later.

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

The reviewer's claude launch command lives in [`agents.yaml`](./agents.yaml)
(pin a model or change flags there, or mount your own over
`/home/alissa/.config/alissa/agents.yaml`). The image runs as a non-root user
because claude refuses `--dangerously-skip-permissions` as root.

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
| `ALISSA_WORKSPACE` | `alissa-review` | workspace name in the generated manifest |
| `ALISSA_REVIEW_SKILLS` | `alissa-code-workspace\|alissa-code-review` | skills installed into every reviewer session (manifest `skills:`), `\|`-separated |
| `ALISSA_POLL_INTERVAL` | `60` | seconds between polls (≥10) |
| `ALISSA_ROUND_CAP` | `3` | CR9 round cap |
| `ALISSA_AGENT_PROFILE` | `claude` | agent the worker launches |
| `ALISSA_ON_MISSING_HUB` | `add` | `add` hub-ifies on demand; `skip` to require a mounted workspace |
| `ALISSA_WORKER_INTERVAL` | `2` | worker reconcile tick (seconds) |
| `ALISSA_ENABLE_FIREWALL` | `0` | `1` raises the egress firewall (needs `--cap-add=NET_ADMIN`) |
| `ALISSA_FIREWALL_EXTRA` | *(empty)* | extra firewall allowlist hosts, space-separated |

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
guarantees a manifest and a `reviewloop.config.json` exist under
`ALISSA_WORKSPACE_ROOT` (`/workspace`, fixed).

**When `ALISSA_REVIEW_REPOS` is set it is authoritative**: the entrypoint
regenerates both files from it on **every boot**, so changing the allowlist (or
`ALISSA_POLL_INTERVAL`, `ALISSA_ROUND_CAP`, …) and redeploying just applies — the
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

Everything after the image name is passed straight to `alissa-reviewloop`, so
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

- `alissa-workspace.yaml` + `reviewloop.config.json` (regenerated from
  `ALISSA_REVIEW_REPOS` each boot when it's set, else whatever you mounted);
- the cloned worktree hubs `<owner>/<repo>/main` — persisting them means a
  restart does **not** re-clone every repo;
- `.claude-config/.credentials.json` — the persisted `claude /login` (see the
  claude-auth section); this is why the login survives restarts;
- `.reviewloop/state.db` — the spawn ledger. Its `escalations` (cap-out memory)
  are worth keeping so a restart doesn't re-escalate a capped PR.

The ledger's **in-flight `spawns` are cleared on every boot** by the entrypoint:
the tmux server, its reviewer sessions, and the worker queue all live in the
ephemeral home and are gone on restart, so a persisted "round N in-flight" would
otherwise be stale and stall re-enqueue for 90 min. A fresh container has no
reviewer running by definition, so clearing them is safe and the daemon
re-enqueues any still-pending round on its first poll.

Nothing else needs a volume: the gh/alissa/claude auth is re-established from the
env tokens on every boot, and tmux sockets are deliberately ephemeral.

On Railway, set the volume's mount path to `/workspace`. Persistent platform
volumes typically mount **root-owned**, so the container starts as root, the
entrypoint `chown`s the mount to `alissa` (uid 1000), and then drops to that user
(via `gosu`) for everything else — so a root-owned mount just works, no manual
`chown` or init container needed. (claude still runs unprivileged, as it must.)

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

0. As root: `chown` the `/workspace` mount to `alissa`, (optionally) raise the
   egress firewall, then drop to `alissa` via `gosu`. Everything below runs
   unprivileged.
1. Preflight + onboard the identities: validate `gh` (fatal if missing) and run
   `gh auth setup-git`; `alissa auth login` (fatal if missing); check the claude
   credential (warn-only — the baked `agents.yaml` handles headless launch).
2. Ensure a manifest + `reviewloop.config.json` exist (mount or generate).
3. **`alissa code workspace sync`** — materialize the worktree hubs the manifest
   declares (create missing/half-built ones, fetch existing). Without this the
   daemon's on-demand `alissa code workspace add` no-ops on a repo already listed
   in the manifest, leaving an empty folder and looping forever hub-ifying a hub
   that never completes.
4. Start `alissa worker --daemon`, wait until it reports running (the daemon only
   *warns* if the worker is absent, so ordering matters).
5. Run `alissa-reviewloop` in the foreground; stop the worker on `SIGTERM`/`SIGINT`.

`tini` is PID 1 to reap the tmux/node/claude child fan-out.
