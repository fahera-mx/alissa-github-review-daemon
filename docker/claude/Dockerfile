# syntax=docker/dockerfile:1
# =============================================================================
# Alissa — GitHub Review Daemon (claude reviewer runtime)
#
# This image is NOT just the poller. The daemon (`alissa-revloop`) is thin: it
# watches GitHub and enqueues reviewer sessions via `alissa tmux queue add`. The
# real review work is done by `alissa worker`, which drains that queue and spawns
# `claude` reviewer agents inside worktree-hub directories. So the image bundles
# all three tiers:
#
#   * python 3.12 + alissa-tools-github-revloop  (the poller, from PyPI)
#   * node + the alissa CLI                          (worker, tmux queue, tasks)
#   * claude-code                                    (the agent the worker spawns)
#
# ONE IMAGE, TWO ROLES (issue #73). `CONTAINER_ROLE=executor` boots the same
# image as an Alissa Studio queue EXECUTOR (`alissa bridge start`) instead of the
# review daemon — deployed as a SECOND service, never as a sidecar of the first.
# Nothing below changes for it: the executor uses the same CLI, the same claude,
# the same agents.yaml and the same entrypoint, which simply takes a different
# branch. See "Bridge executor role" in README.md.
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

# Version of the daemon to install from PyPI. Bump per release: 0.16.0 is the
# release this repo publishes on merge (it must match the dist's version file,
# or the image ships a library that cannot read the config this image renders).
#
# The FLOOR is separate from the current pin and moves far less often: never
# below 0.15.0, the first version shipping the `alissa-revloop-ui` console
# script the entrypoint starts. Naming the two apart keeps the next release a
# one-line edit.
ARG REVLOOP_VERSION=0.16.14
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
# Installs the `alissa-revloop` console script into /usr/local/bin.
RUN pip install "alissa-tools-github-revloop==${REVLOOP_VERSION}"

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
COPY revloop-config.sh /usr/local/bin/revloop-config.sh
COPY init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/revloop-config.sh /usr/local/bin/init-firewall.sh

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
#
# ALSO deliberately not here (runtime-only): the reviewer console knobs.
#   * ALISSA_UI_PASSCODE — a SECRET (the console's only gate); baking it via ARG
#     would leak it into `docker history`, exactly like the tokens above.
#   * PORT — platform-injected at runtime (Railway sets it); the entrypoint
#     binds 0.0.0.0:${PORT:-8080}, so there is nothing to bake.
#   * ALISSA_UI_ENABLED — opt-in flag; the entrypoint already defaults it OFF
#     (`${ALISSA_UI_ENABLED:-0}`), so no ARG default is needed. Kept runtime-only
#     so it pairs with the passcode it is useless without.
#   * ALISSA_UI_SECURE_COOKIE — TLS-posture flag read by the sidecar itself.
# EXPOSE below documents the console port; the console is disabled by default.
#
# AND deliberately not here (runtime-only): every bridge-executor knob —
# CONTAINER_ROLE, ALISSA_BRIDGE_EXECUTOR, ALISSA_BRIDGE_EXECUTOR_ID,
# ALISSA_BRIDGE_LABEL, ALISSA_BRIDGE_MAX_CONCURRENT, ALISSA_BRIDGE_POLL_SECONDS,
# ALISSA_BRIDGE_HANDOFF. Same reasoning as the console flags, plus one that is
# specific to the role:
#   * CONTAINER_ROLE is what makes ONE image serve TWO services. Baking a default
#     other than `daemon` would make the artifact role-specific — the opposite of
#     the "same image, no second Dockerfile" decision this role exists under.
#   * ALISSA_BRIDGE_EXECUTOR is the arming gate, and an ARG default is a value
#     every deploy inherits. It stays unset so the image ships DISARMED and the
#     entrypoint's own `${…:-0}` is the only default there is.
#   * the id / label / concurrency / interval / handoff knobs only mean anything
#     on a service that already set the two above, and the entrypoint supplies
#     their defaults (`revloop-executor`, `claude`, and CLI pass-through for the
#     rest), so an ARG here would just be a second place for them to drift.

# Allowlist of repos to watch, as a single string separated by "|" (a single
# repo needs no separator). "|" is used because repo slugs contain "/".
#   e.g. "fahera-mx/studio.alissa.app|fahera-mx/blog.alissa.app"
ARG ALISSA_REVIEW_REPOS=""
# GitHub logins allowed to re-open a capped PR with an
# `alissa-review: re-enter +N` comment, same "|"-separated convention. Empty ON
# PURPOSE: the lever fails closed, and an operator opts in explicitly.
#   e.g. "RHDZMOTA"
ARG ALISSA_REVIEW_OPERATORS=""
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
# defaults at the pinned REVLOOP_VERSION are poll_interval=60, round_cap=10;
# they are documented in the README, not duplicated here.
ARG ALISSA_POLL_INTERVAL=""
ARG ALISSA_ROUND_CAP=""
# Reviewer IDENTITY, and where its credential comes from (issue #51). Both are
# pass-through (empty ARG default -> key omitted -> library default).
#
# ALISSA_REVIEWER_LOGIN pins the GitHub login every verdict must be posted
# under; the daemon refuses to submit a review as anyone else. Unset, it adopts
# whatever the gh credential resolved to at boot.
#
# ALISSA_REVIEWER_TOKEN_ENV is the NAME of the variable holding that identity's
# token — never the token, which stays a runtime secret like GH_TOKEN (see the
# "NOT here (by design)" note above). Set it and the daemon builds an explicit
# environment for every `gh` call instead of inheriting the container default;
# leave it empty and the daemon warns at startup, because this container holds
# several identities and inheritance is how a verdict lands under the wrong one.
#
# BOTH REQUIRE REVLOOP_VERSION >= 0.16.3 (pinned at the top of this file). The
# entrypoint renders them into revloop.config.json, and `Config.build` rejects
# unknown keys — so setting either against an older pin fails config load with
# `unknown config key(s): reviewer_token_env`, AFTER the entrypoint has already
# logged a successful reviewer-identity preflight. That reads as a library bug
# rather than the version skew it is. Bump the pin before setting these.
ARG ALISSA_REVIEWER_LOGIN=""
ARG ALISSA_REVIEWER_TOKEN_ENV=""
# Reviewer-session reaper knobs, pass-through on the same contract: how long a
# session must be idle AND quiet before the sweep reaps it, and the page-worthy
# threshold for live reviewer sessions left after a sweep.
ARG ALISSA_REAP_GRACE_SECONDS=""
ARG ALISSA_REAP_SESSION_CAP=""
# Spawn back-pressure: how many of THIS daemon's reviewer sessions may be live
# before an owed round waits for a slot instead of spawning. A reviewer session
# is a full interactive agent, and nothing bounded how many ran at once --
# `round_cap` is per PR and the reap cap only alarms -- so a merge wave pegged
# the container's vCPU ceiling (2026-07-29, 18:45-19:00Z). Deferral costs the
# round nothing: no round number, no attempt, no escalation. Must be <=
# ALISSA_REAP_SESSION_CAP, which the daemon enforces at config load. Pass-through,
# and it REQUIRES REVLOOP_VERSION >= 0.16.13 for the same reason the keys above
# do: an older library rejects the unknown config key at load.
ARG ALISSA_MAX_CONCURRENT_SESSIONS=""
# How long a round holds its APPROVE while the judged head's CI rollup is still
# running (or unreadable) before recording the verdict as a comment instead --
# PER CONDITION waited on, so an unreadable hold promoted to a pending one
# restarts the clock once and the worst case is twice this. A RED rollup never
# waits and never approves, whatever this says. Pass-through,
# and it REQUIRES REVLOOP_VERSION >= 0.16.7 for the same reason the two keys
# above it do: an older library rejects the unknown config key at load.
ARG ALISSA_CHECKS_WAIT_SECONDS=""
# How long an owed round waits for the head's checks to CONCLUDE before its
# reviewer is queued at all (issue #84). The knob above gates the verdict the
# DAEMON posts; this one is the only structural gate on the verdict a reviewer
# SESSION posts, which is the ordinary path -- a session that has not started
# cannot approve ahead of its evidence. A RED or unreadable rollup never waits:
# the round is queued at once, forbidden to approve. Pass-through, and it
# REQUIRES REVLOOP_VERSION >= 0.17.0 for the same reason the keys above it do:
# an older library rejects the unknown config key at load.
ARG ALISSA_CHECKS_SPAWN_WAIT_SECONDS=""
# How many polls a PR with NO Alissa review task is taken on trust before the
# daemon searches the actor's task corpus for one again (issue #87). That search
# is the widest read this daemon makes, and the PR -> review-task mapping can
# only cache an answer that EXISTS -- so an unmapped PR paid the whole corpus on
# every poll, forever. Trades LATENCY for reads: a review task created inside the
# window is picked up at the re-arm, not the next poll. Floor 1; there is no
# value that disables it. Pass-through, and it REQUIRES REVLOOP_VERSION >= 0.18.0
# for the same reason the keys above it do: an older library rejects the unknown
# config key at load.
ARG ALISSA_REVIEW_TASK_MISS_TTL_POLLS=""
# Narrow `alissa task list` to this actor's OWN rows (`--self`), dropping the
# sponsor's corpus. Off unless set, on measured evidence rather than caution: on
# the live fleet corpus it saves 4% of the payload, and a small minority of review
# tasks are owned by another actor -- one the list could not see would be a round
# the daemon could not count. Set it to 1 only where every review task is created
# by this daemon's own reviewer sessions. The other narrowings (status filter,
# digest view) are adopted automatically wherever the installed CLI offers them
# and need no variable. Pass-through, REQUIRES REVLOOP_VERSION >= 0.18.0.
ARG ALISSA_TASK_LIST_SELF_SCOPE=""
# STRUCTURAL container constants (not pass-through): agent_profile must name a
# profile the baked agents.yaml ships (`claude`); on_missing_hub must be `add`
# for the self-contained hub-ify-on-demand model (the library default `skip`
# would review nothing on a fresh volume). Kept explicit and pinned by
# tests-entrypoint-config.sh.
ARG ALISSA_AGENT_PROFILE="claude"
# Reviewer model pinned into the agents.yaml claude command at boot (the reviewer
# is the quality gate, so it must not silently fall back to a smaller model on a
# usage-throttled account). Alias (`opus`/`sonnet`) or full id (`claude-opus-4-8`);
# `default` or empty omits the --model flag. See the entrypoint (step 2e) + README.
ARG ALISSA_AGENT_MODEL="opus"
ARG ALISSA_ON_MISSING_HUB="add"
# Worker reconcile tick, seconds.
ARG ALISSA_WORKER_INTERVAL="2"
# Optional egress firewall (needs --cap-add=NET_ADMIN at runtime).
ARG ALISSA_ENABLE_FIREWALL="0"
ARG ALISSA_FIREWALL_EXTRA=""
# Force the root phase's full `chown -R` of the volume (issue #78). Default OFF:
# the entrypoint probes ownership at depth 1 and only walks when it finds
# something not owned by `alissa`, because the walk charges GBs of reclaimable
# dentry/inode slab to the container cgroup and a warm boot gains nothing from
# it. Set this to 1 for ONE boot after a root shell has written files DEEPER in
# the tree than the probe can see — that is the probe's only blind spot.
ARG ALISSA_FORCE_CHOWN="0"

ENV ALISSA_REVIEW_REPOS=${ALISSA_REVIEW_REPOS} \
    ALISSA_REVIEW_OPERATORS=${ALISSA_REVIEW_OPERATORS} \
    ALISSA_WORKSPACE=${ALISSA_WORKSPACE} \
    ALISSA_REVIEW_SKILLS=${ALISSA_REVIEW_SKILLS} \
    ALISSA_POLL_INTERVAL=${ALISSA_POLL_INTERVAL} \
    ALISSA_ROUND_CAP=${ALISSA_ROUND_CAP} \
    ALISSA_REAP_GRACE_SECONDS=${ALISSA_REAP_GRACE_SECONDS} \
    ALISSA_REAP_SESSION_CAP=${ALISSA_REAP_SESSION_CAP} \
    ALISSA_MAX_CONCURRENT_SESSIONS=${ALISSA_MAX_CONCURRENT_SESSIONS} \
    ALISSA_CHECKS_WAIT_SECONDS=${ALISSA_CHECKS_WAIT_SECONDS} \
    ALISSA_CHECKS_SPAWN_WAIT_SECONDS=${ALISSA_CHECKS_SPAWN_WAIT_SECONDS} \
    ALISSA_REVIEW_TASK_MISS_TTL_POLLS=${ALISSA_REVIEW_TASK_MISS_TTL_POLLS} \
    ALISSA_TASK_LIST_SELF_SCOPE=${ALISSA_TASK_LIST_SELF_SCOPE} \
    ALISSA_REVIEWER_LOGIN=${ALISSA_REVIEWER_LOGIN} \
    ALISSA_REVIEWER_TOKEN_ENV=${ALISSA_REVIEWER_TOKEN_ENV} \
    ALISSA_AGENT_PROFILE=${ALISSA_AGENT_PROFILE} \
    ALISSA_AGENT_MODEL=${ALISSA_AGENT_MODEL} \
    ALISSA_ON_MISSING_HUB=${ALISSA_ON_MISSING_HUB} \
    ALISSA_WORKER_INTERVAL=${ALISSA_WORKER_INTERVAL} \
    ALISSA_ENABLE_FIREWALL=${ALISSA_ENABLE_FIREWALL} \
    ALISSA_FIREWALL_EXTRA=${ALISSA_FIREWALL_EXTRA} \
    ALISSA_FORCE_CHOWN=${ALISSA_FORCE_CHOWN}

# The reviewer console (alissa-revloop-ui) listens here when ALISSA_UI_ENABLED is
# set. EXPOSE is documentation/metadata only — it opens no port by itself; the
# entrypoint binds 0.0.0.0:${PORT:-8080}, and 8080 is the default the console
# falls back to when the platform injects no PORT (the sidecar's own default,
# 127.0.0.1:8788, is for a local run — the container always overrides both).
# The console is DISABLED by default (opt-in) and fail-closed on
# ALISSA_UI_PASSCODE. Liveness (a platform healthcheck path): GET /healthz.
EXPOSE 8080

# Start as root so the entrypoint can chown a root-owned volume mount; it drops
# to `alissa` (via gosu) before running anything else. All the build steps above
# ran as `alissa`, so the CLI, agents.yaml and git config are already in place.
USER root

# tini reaps the worker/tmux/claude child fan-out; entrypoint does the rest.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
