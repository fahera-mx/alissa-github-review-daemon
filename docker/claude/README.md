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

## The three identities

The loop depends on three independent identities. All are injected at runtime as
env vars — **never bake them into the image**. The entrypoint preflights all
three and fails fast if any is missing or rejected.

| env var | identity | used for |
| --- | --- | --- |
| `GH_TOKEN` | `gh` (the `alissa-app` GitHub user) | review queue, round counting, PR comments |
| `ALISSA_API_TOKEN` (`alissa_…`) | Alissa by Fahera | tasks, session queue, verdicts |
| `ANTHROPIC_API_KEY` *or* `CLAUDE_CODE_OAUTH_TOKEN` | claude | the reviewer agent |

A `reviewer_login` that disagrees with the `GH_TOKEN` is **fatal at the daemon's
own startup** (every round would look like round 1 and respawn forever) — so keep
the token and any configured login in sync.

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
only guarantees a manifest and a `reviewloop.config.json` exist under
`ALISSA_WORKSPACE_ROOT` (`/workspace`, fixed). Either can be mounted; otherwise
both are generated from the config above.

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

Unattended, persisting the workspace (hubs + the spawn ledger) across restarts:

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

1. (optional) raise the egress firewall.
2. Preflight `gh`, `alissa`, and `claude` — fail fast if any is missing/rejected.
3. Ensure a manifest + `reviewloop.config.json` exist (mount or generate).
4. Start `alissa worker --daemon`, wait until it reports running (the daemon only
   *warns* if the worker is absent, so ordering matters).
5. Run `alissa-reviewloop` in the foreground; stop the worker on `SIGTERM`/`SIGINT`.

`tini` is PID 1 to reap the tmux/node/claude child fan-out.
