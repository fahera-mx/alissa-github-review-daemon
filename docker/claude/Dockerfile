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

# Workspace root that the daemon and reviewers operate over. Mount a manifest
# here, or let the entrypoint generate one from env (see README).
ENV ALISSA_WORKSPACE_ROOT=/workspace \
    ALISSA_WORKSPACE=alissa-review \
    TMUX_TMPDIR=/home/alissa/.tmux
RUN mkdir -p /home/alissa/.tmux
WORKDIR /workspace

# tini reaps the worker/tmux/claude child fan-out; entrypoint does the rest.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
