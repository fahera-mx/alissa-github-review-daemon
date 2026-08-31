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
# Only the FIRST of those three is installed here. This is a thin LEAF on the
# shared loopwork base image, which owns the runtime substrate the review and
# develop daemons had copy-pasted between them:
#
#   https://github.com/ali-fhr/alissa-loopwork   (README -> "leaf contract")
#
# In the BASE (never re-do any of it here): python 3.12 + Node 22, git/tmux/gh/
# tini/gosu/jq + iptables/ipset, claude-code with its first-run gates pre-seeded,
# the alissa CLI on the `alissa` user's PATH, the non-root `alissa` user (uid
# 1000) + /workspace mount point, the system-wide GitHub SSH->HTTPS rewrite with
# gh as git's credential helper, the workspace ENV skeleton
# (ALISSA_WORKSPACE_ROOT / TMUX_TMPDIR / CLAUDE_CONFIG_DIR), EXPOSE 8080, and the
# tini ENTRYPOINT hook at the fixed path /usr/local/bin/entrypoint.sh. Since base
# 0.2.0 it is also MULTI-AGENT: the `codex` and `pi` CLIs ship alongside
# claude-code. This daemon spawns `claude` only, so they are inert here — do not
# add knobs or config for them.
#
# In this LEAF (below): the pip-installed daemon, the entrypoint + its helper
# scripts (COPYed OVER the base's failing stub at that fixed path), the
# agents.yaml profile, the git author identity, and the whole ARG->ENV knob block.
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
#   docker build --platform linux/amd64 -t alissa-review-daemon docker/claude
#
# `--platform` is explicit because the base image publishes linux/amd64 ONLY (see
# "Base image" in README.md); on an amd64 host it is a no-op, and off one it is
# the difference between an emulated build and a manifest-match failure.
#
# The image contract this build has to preserve is checked mechanically by
# ./tests-image-contract.sh, which builds and then asserts inside the result.
#
# Run: see docker/claude/README.md and ../../README.md.
# =============================================================================

# The shared loopwork base image. It is PUBLIC on GHCR, so the pull is anonymous
# and no registry credential is needed anywhere (Railway included).
#
# Pinned TWICE, and the two halves do different jobs.
#
#   * the exact semver (never `:latest`) is the READABLE half — a base bump is a
#     deliberate, reviewable change to this line, and that is the ONLY thing that
#     advances the shared infrastructure layers;
#   * the `@sha256:` is the ENFORCING half — a tag is mutable, so without it a
#     re-push of `0.2.0` is substituted into every build with no diff to review.
#     That matters more here than for an ordinary base: this pin is now the whole
#     review surface for claude-code, the `curl … | bash` CLI install and the apt
#     layer, none of which this repo builds any more.
#
# A bump changes BOTH values together.
#
# The digest is the INDEX digest — what the registry returns as
# `Docker-Content-Digest` for the tag — not the amd64 child manifest's. That keeps
# platform selection a build-time choice (`--platform`) instead of freezing it
# into the reference, so an arm64 base, once published, needs only the ordinary
# two-value bump. Read the current one back with:
#
#   docker buildx imagetools inspect ghcr.io/ali-fhr/alissa-loopwork-base:0.2.0
#
# The base is linux/amd64-ONLY today; see "Base image" in README.md.
FROM ghcr.io/ali-fhr/alissa-loopwork-base:0.2.0@sha256:f46fd1431462392993da6dd209e23c15c3dc8495540469eaa06b40ca5bafda1c

# Version of the daemon to install from PyPI. Bump per release: 0.16.0 is the
# release this repo publishes on merge (it must match the dist's version file,
# or the image ships a library that cannot read the config this image renders).
#
# The FLOOR is separate from the current pin and moves far less often: never
# below 0.15.0, the first version shipping the `alissa-revloop-ui` console
# script the entrypoint starts. Naming the two apart keeps the next release a
# one-line edit.
ARG REVLOOP_VERSION=0.16.14

# --- The review daemon (from PyPI) -------------------------------------------
# Installs the `alissa-revloop` console script into /usr/local/bin.
RUN pip install "alissa-tools-github-revloop==${REVLOOP_VERSION}"

# --- Entrypoint + optional firewall ------------------------------------------
# /usr/local/bin/entrypoint.sh is the fixed path the base's ENTRYPOINT (tini ->
# this script) points at; the base ships a stub there that exits 1, and this
# COPY is what replaces it with the real one. Nothing here re-declares
# ENTRYPOINT — it is inherited.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY revloop-config.sh /usr/local/bin/revloop-config.sh
COPY init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/revloop-config.sh /usr/local/bin/init-firewall.sh

# Agent profile the worker uses to launch reviewers non-interactively. Without
# this, worker-spawned claude sessions stop on the first-run trust/permission
# prompt. Mount your own over this path at runtime to override.
COPY --chown=alissa:alissa agents.yaml /home/alissa/.config/alissa/agents.yaml

# A git identity so any git operation (e.g. the clone in hub-ify) has an author
# and never drops into git's interactive "who are you" prompt. Reviewers are
# read-only (CR6), so this is a safety default, not a committing identity.
#
# This is the one piece of user-level git config that is per-daemon, which is
# why it stays in the leaf: the rewrites, the credential helper and
# advice.detachedHead are all system-level in the base. `--global` writes
# /home/alissa/.gitconfig, so it has to run AS that user; the base left the
# image on USER root (for the leaf's pip/COPY layers and the entrypoint's
# root-phase chown), so hop over and back.
USER alissa
RUN git config --global user.name  "alissa-review-daemon" \
    && git config --global user.email "support@alissa.app"
USER root

# --- Build-time configuration (Railway-friendly) -----------------------------
# Railway's Dockerfile builds only expose service variables that are declared as
# ARG here — runtime ENV set in the dashboard does NOT reach a from-Dockerfile
# build's config. So every non-secret knob is an ARG whose value is baked into
# an ENV of the same name. A runtime `-e VAR=...` still overrides the baked
# default, so both paths work.
#
# Kept LAST on purpose: changing any of these rebuilds only these tiny trailing
# layers, never the pip/COPY layers above (and never the base image at all).
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
# The base's EXPOSE 8080 documents the console port (metadata only); the console
# is disabled by default.
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
# ALISSA_STABILITY_ROUNDS is pass-through for the same reason and by the same
# mechanism: how many consecutive request_changes rounds with an EMPTY
# shipped-product diff stop the loop (0 disables the guard entirely; the library
# default is 3). It REQUIRES REVLOOP_VERSION >= 0.26.0 — an older library
# refuses the unknown config key outright and the daemon will not start.
ARG ALISSA_STABILITY_ROUNDS=""
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
# usage-throttled account). Default `claude-fable-5`: the most capable generally
# available Claude model, one tier above Opus (operator decision 2026-08-30).
# Alias (`opus`/`sonnet`) or full id (`claude-opus-4-8`) pass through verbatim;
# `default` or empty omits the --model flag. See the entrypoint (step 2e) + README.
ARG ALISSA_AGENT_MODEL="claude-fable-5"
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
    ALISSA_STABILITY_ROUNDS=${ALISSA_STABILITY_ROUNDS} \
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
