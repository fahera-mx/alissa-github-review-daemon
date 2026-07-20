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
ARG REVIEWLOOP_VERSION=0.2.0
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
# sudo   : only used by the optional firewall init (dropped priv otherwise)
# iptables/ipset : only used by the optional firewall init
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git tmux tini sudo \
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
# Agents run unattended with three live tokens; don't run them as root. The
# firewall init (optional) is the only thing that needs root, via a narrow
# sudoers entry.
RUN useradd --create-home --shell /bin/bash --uid 1000 alissa \
    && echo 'alissa ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh' \
        > /etc/sudoers.d/alissa-firewall \
    && chmod 0440 /etc/sudoers.d/alissa-firewall \
    && mkdir -p /workspace \
    && chown alissa:alissa /workspace

# --- Entrypoint + optional firewall ------------------------------------------
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/init-firewall.sh

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

# A git identity so any git operation (e.g. the clone in hub-ify) has an author
# and never drops into git's interactive "who are you" prompt. Reviewers are
# read-only (CR6), so this is a safety default, not a committing identity.
RUN git config --global user.name  "alissa-review-daemon" \
    && git config --global user.email "support@alissa.app" \
    && git config --global advice.detachedHead false

# Workspace root that the daemon and reviewers operate over. Mount a manifest
# here, or let the entrypoint generate one from the config below (see README).
# These are fixed internal paths, not meant to be reconfigured.
ENV ALISSA_WORKSPACE_ROOT=/workspace \
    TMUX_TMPDIR=/home/alissa/.tmux
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
# Daemon knobs (see the daemon README's settings table).
ARG ALISSA_POLL_INTERVAL="60"
ARG ALISSA_ROUND_CAP="3"
ARG ALISSA_AGENT_PROFILE="claude"
ARG ALISSA_ON_MISSING_HUB="add"
# Worker reconcile tick, seconds.
ARG ALISSA_WORKER_INTERVAL="2"
# Optional egress firewall (needs --cap-add=NET_ADMIN at runtime).
ARG ALISSA_ENABLE_FIREWALL="0"
ARG ALISSA_FIREWALL_EXTRA=""

ENV ALISSA_REVIEW_REPOS=${ALISSA_REVIEW_REPOS} \
    ALISSA_WORKSPACE=${ALISSA_WORKSPACE} \
    ALISSA_POLL_INTERVAL=${ALISSA_POLL_INTERVAL} \
    ALISSA_ROUND_CAP=${ALISSA_ROUND_CAP} \
    ALISSA_AGENT_PROFILE=${ALISSA_AGENT_PROFILE} \
    ALISSA_ON_MISSING_HUB=${ALISSA_ON_MISSING_HUB} \
    ALISSA_WORKER_INTERVAL=${ALISSA_WORKER_INTERVAL} \
    ALISSA_ENABLE_FIREWALL=${ALISSA_ENABLE_FIREWALL} \
    ALISSA_FIREWALL_EXTRA=${ALISSA_FIREWALL_EXTRA}

# tini reaps the worker/tmux/claude child fan-out; entrypoint does the rest.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
