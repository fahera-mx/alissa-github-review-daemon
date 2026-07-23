# syntax=docker/dockerfile:1
# =============================================================================
# Alissa — GitHub Review Daemon (claude reviewer runtime)
#
# This image is NOT just the poller. The daemon (`alissa-reviewloop`) is thin: it
# watches GitHub and enqueues reviewer sessions via `alissa tmux queue add`. The
# real review work is done by `alissa worker`, which drains that queue and spawns
# `claude` reviewer agents inside worktree-hub directories. So the image bundles
# all three tiers:
#
#   * python 3.12 + alissa-tools-github-reviewloop  (the poller, from PyPI)
#   * node + the alissa CLI                          (worker, tmux queue, tasks)
#   * claude-code                                    (the agent the worker spawns)
#
# Build context is this directory (docker/claude) — the image pulls the daemon
# from PyPI, so no repo source is copied in.
#
#   docker build -t alissa-review-daemon docker/claude
#
# Run: see docker/claude/README.md and ../../README.md.
# =============================================================================

# Base is pinned to python 3.12 (matches the repo's .python-version); Node 22 is
# layered on via NodeSource. The daemon needs python >= 3.11; the alissa CLI and
# claude-code need Node >= 18 — both satisfied.
FROM python:3.12-slim-bookworm

# Version of the daemon to install from PyPI. Bump per release.
ARG REVIEWLOOP_VERSION=0.8.0
# Node major version (must be >= 18 for the alissa CLI and claude-code).
ARG NODE_MAJOR=22

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --- System dependencies ------------------------------------------------------
# git   : reviewers operate over git worktree hubs
# tmux  : the alissa worker manages tmux (ali-*) sessions
# gh     : the daemon shells out to `gh api` for the review queue
# tini   : PID 1 init — reaps the tmux/node/claude child fan-out
# gosu   : drop root -> alissa after the entrypoint fixes the volume mount
# iptables/ipset : only used by the optional firewall init
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git tmux tini gosu \
        iptables ipset procps jq less; \
    # GitHub CLI apt repo
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    # Node.js via NodeSource
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -; \
    apt-get install -y --no-install-recommends gh nodejs; \
    rm -rf /var/lib/apt/lists/*

# --- claude-code (the reviewer agent) ----------------------------------------
RUN npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

# --- The review daemon (from PyPI) -------------------------------------------
# Installs the `alissa-reviewloop` console script into /usr/local/bin.
RUN pip install "alissa-tools-github-reviewloop==${REVIEWLOOP_VERSION}"

# --- Non-root runtime user ----------------------------------------------------
# Agents run unattended with three live tokens, and claude refuses
# --dangerously-skip-permissions as root, so the worker/daemon run as this user.
# The container still STARTS as root (see the USER root before ENTRYPOINT): the
# entrypoint fixes the /workspace mount ownership, then drops to `alissa` via
# gosu. This is what makes a root-owned platform volume (e.g. Railway) writable.
RUN useradd --create-home --shell /bin/bash --uid 1000 alissa \
    && mkdir -p /workspace \
    && chown alissa:alissa /workspace

# --- git: force GitHub over HTTPS with the gh token (no SSH key in a container)
# `alissa code workspace add` clones over SSH by default (git@github.com:…), and
# the daemon's on_missing_hub:add path calls it WITHOUT --https — so there is no
# flag to flip. Rewrite every GitHub SSH URL to HTTPS system-wide and wire gh in
# as the credential helper (gh reads GH_TOKEN from the env). This makes the clone
# authenticate for both the unprivileged daemon and any manual shell (root).
# NB: --add on the second insteadOf — a plain `git config` replaces the single
# value, so without it only the last rewrite survives (and git@github.com: is the
# form the alissa CLI actually emits).
RUN git config --system url."https://github.com/".insteadOf "git@github.com:" \
    && git config --system --add url."https://github.com/".insteadOf "ssh://git@github.com/" \
    && git config --system credential."https://github.com".helper "" \
    && git config --system --add credential."https://github.com".helper "!gh auth git-credential"

# --- Entrypoint + optional firewall ------------------------------------------
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY reviewloop-config.sh /usr/local/bin/reviewloop-config.sh
COPY init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/reviewloop-config.sh /usr/local/bin/init-firewall.sh

USER alissa
WORKDIR /home/alissa

# The alissa CLI installer is npm-free: it drops a `node cli.mjs` launcher into
# ~/.local/bin. Run it as the target user so it lands in the user's home.
ENV PATH="/home/alissa/.local/bin:${PATH}"
RUN curl -fsSL https://share.alissa.app/install | bash

# Agent profile the worker uses to launch reviewers non-interactively. Without
# this, worker-spawned claude sessions stop on the first-run trust/permission
# prompt. Mount your own over this path at runtime to override.
COPY --chown=alissa:alissa agents.yaml /home/alissa/.config/alissa/agents.yaml

# --- claude first-run gates: pre-seed so the TUI starts READY, no human ------
# A brand-new user hits claude's first-run dialogs (welcome/onboarding, theme
# picker, and the one-time --dangerously-skip-permissions "bypass mode" warning)
# and the worker hangs forever: "agent UI not ready — a first-run/trust dialog
# needs a human". These are the exact keys the working host carries, split across
# the two files claude reads:
#   ~/.claude.json          — user state: onboarding completed, warnings seen
#   ~/.claude/settings.json — settings: skip the bypass-mode prompt, theme, TUI
# skipDangerousModePermissionPrompt is the key the public docs omit; it is what
# lets `claude --dangerously-skip-permissions` come up without a keypress.
# Auth is still required and stays in the env (CLAUDE_CODE_OAUTH_TOKEN preferred
# — an interactive TUI would prompt to approve a bare ANTHROPIC_API_KEY).
RUN mkdir -p /home/alissa/.claude \
    && CLAUDE_VER="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" \
    && printf '{\n  "hasCompletedOnboarding": true,\n  "hasSeenAutoModeEntryWarning": true,\n  "lastOnboardingVersion": "%s"\n}\n' "${CLAUDE_VER:-2.1.215}" \
        > /home/alissa/.claude.json \
    && printf '{\n  "skipDangerousModePermissionPrompt": true,\n  "theme": "dark",\n  "tui": "fullscreen"\n}\n' \
        > /home/alissa/.claude/settings.json

# A git identity so any git operation (e.g. the clone in hub-ify) has an author
# and never drops into git's interactive "who are you" prompt. Reviewers are
# read-only (CR6), so this is a safety default, not a committing identity.
RUN git config --global user.name  "alissa-review-daemon" \
    && git config --global user.email "support@alissa.app" \
    && git config --global advice.detachedHead false

# Workspace root that the daemon and reviewers operate over. Mount a manifest
# here, or let the entrypoint generate one from the config below (see README).
# These are fixed internal paths, not meant to be reconfigured.
#
# CLAUDE_CONFIG_DIR points claude's config — crucially its OAuth credential file
# `.credentials.json` — at a folder ON the /workspace volume, so a one-time
# `claude /login` survives restarts and auto-renews (a static CLAUDE_CODE_OAUTH_
# TOKEN expires and 401s). If you relocate the volume, move this with it.
ENV ALISSA_WORKSPACE_ROOT=/workspace \
    TMUX_TMPDIR=/home/alissa/.tmux \
    CLAUDE_CONFIG_DIR=/workspace/.claude-config
RUN mkdir -p /home/alissa/.tmux
WORKDIR /workspace

# --- Build-time configuration (Railway-friendly) -----------------------------
# Railway's Dockerfile builds only expose service variables that are declared as
# ARG here — runtime ENV set in the dashboard does NOT reach a from-Dockerfile
# build's config. So every non-secret knob is an ARG whose value is baked into
# an ENV of the same name. A runtime `-e VAR=...` still overrides the baked
# default, so both paths work.
#
# Kept LAST on purpose: changing any of these rebuilds only these tiny trailing
# layers, never the apt/npm/pip layers above.
#
# NOT here (by design): GH_TOKEN, ALISSA_API_TOKEN, ANTHROPIC_API_KEY /
# CLAUDE_CODE_OAUTH_TOKEN. Secrets baked via ARG leak into `docker history` and
# every image layer — inject them as runtime env instead (Railway provides
# service variables to the container at runtime).

# Allowlist of repos to watch, as a single string separated by "|" (a single
# repo needs no separator). "|" is used because repo slugs contain "/".
#   e.g. "fahera-mx/studio.alissa.app|fahera-mx/blog.alissa.app"
ARG ALISSA_REVIEW_REPOS=""
# Workspace name recorded in the generated manifest.
ARG ALISSA_WORKSPACE="alissa-review"
# Skills installed into every reviewer session (manifest `skills:`), "|"-separated.
# alissa-code-review is the review protocol the reviewer directive loads; the
# workspace skill is for navigating the hubs. alissa-session/-skills-usage are
# auto-installed by `alissa code`, so they need not be listed. Deliberately NOT
# alissa-code-orchestration (reviewers must not spawn ali-* sessions) or
# alissa-code-git (read-only reviewers never commit).
ARG ALISSA_REVIEW_SKILLS="alissa-code-workspace|alissa-code-review"
# Daemon knobs (see the daemon README's settings table).
#
# ALISSA_POLL_INTERVAL / ALISSA_ROUND_CAP are PASS-THROUGH: their ARG default is
# empty ON PURPOSE, so when the operator does not set them the entrypoint OMITS
# the key from the generated config and the daemon library's own current default
# applies (env var > library default). Baking a concrete value here would shadow
# the library default — the exact defect this repo's #30 removed. The library
# defaults at the pinned REVIEWLOOP_VERSION are poll_interval=60, round_cap=3;
# they are documented in the README, not duplicated here.
ARG ALISSA_POLL_INTERVAL=""
ARG ALISSA_ROUND_CAP=""
# STRUCTURAL container constants (not pass-through): agent_profile must name a
# profile the baked agents.yaml ships (`claude`); on_missing_hub must be `add`
# for the self-contained hub-ify-on-demand model (the library default `skip`
# would review nothing on a fresh volume). Kept explicit and pinned by
# tests-entrypoint-config.sh.
ARG ALISSA_AGENT_PROFILE="claude"
# Reviewer model pinned into the agents.yaml claude command at boot (the reviewer
# is the quality gate, so it must not silently fall back to a smaller model on a
# usage-throttled account). Alias (`opus`/`sonnet`) or full id (`claude-opus-4-8`);
# `default` or empty omits the --model flag. See the entrypoint (step 2d) + README.
ARG ALISSA_AGENT_MODEL="opus"
ARG ALISSA_ON_MISSING_HUB="add"
# Worker reconcile tick, seconds.
ARG ALISSA_WORKER_INTERVAL="2"
# Optional egress firewall (needs --cap-add=NET_ADMIN at runtime).
ARG ALISSA_ENABLE_FIREWALL="0"
ARG ALISSA_FIREWALL_EXTRA=""

ENV ALISSA_REVIEW_REPOS=${ALISSA_REVIEW_REPOS} \
    ALISSA_WORKSPACE=${ALISSA_WORKSPACE} \
    ALISSA_REVIEW_SKILLS=${ALISSA_REVIEW_SKILLS} \
    ALISSA_POLL_INTERVAL=${ALISSA_POLL_INTERVAL} \
    ALISSA_ROUND_CAP=${ALISSA_ROUND_CAP} \
    ALISSA_AGENT_PROFILE=${ALISSA_AGENT_PROFILE} \
    ALISSA_AGENT_MODEL=${ALISSA_AGENT_MODEL} \
    ALISSA_ON_MISSING_HUB=${ALISSA_ON_MISSING_HUB} \
    ALISSA_WORKER_INTERVAL=${ALISSA_WORKER_INTERVAL} \
    ALISSA_ENABLE_FIREWALL=${ALISSA_ENABLE_FIREWALL} \
    ALISSA_FIREWALL_EXTRA=${ALISSA_FIREWALL_EXTRA}

# Start as root so the entrypoint can chown a root-owned volume mount; it drops
# to `alissa` (via gosu) before running anything else. All the build steps above
# ran as `alissa`, so the CLI, agents.yaml and git config are already in place.
USER root

# tini reaps the worker/tmux/claude child fan-out; entrypoint does the rest.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
