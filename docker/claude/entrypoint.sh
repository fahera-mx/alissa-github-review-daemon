#!/usr/bin/env bash
# =============================================================================
# Container entrypoint for the Alissa GitHub review daemon.
#
#   0. as root: fix the volume-mount ownership (+ firewall), then drop to alissa
#   1. preflight the identities the loop depends on (gh / alissa / claude)
#   2. bootstrap the worktree-hub workspace + revloop config from a manifest
#      …then prune finished worktrees off it (3c-ii), and again on an interval
#      (`alissa code workspace prune`, capability-gated, bounded, best-effort)
#   3. start `alissa worker` (backgrounded) and wait until it is up
#   4. optionally start the `alissa-revloop-ui` console sidecar (ALISSA_UI_ENABLED)
#   5. run `alissa-revloop` in the foreground, stopping the worker on exit
#
# The daemon is a thin poller; the worker is what actually spawns reviewers, so
# the worker MUST be running first — the daemon only warns if it isn't.
#
# TWO ROLES, ONE IMAGE (CONTAINER_ROLE, issue #73)
# ------------------------------------------------
# `CONTAINER_ROLE=executor` boots this same image as an Alissa Studio queue
# EXECUTOR instead: it shares steps 0-3 (identities, workspace bootstrap, hub
# sync) and then `exec`s `alissa bridge start` — no worker, no console, no
# `alissa-revloop`. The executor is a SEPARATE SERVICE, not a sidecar of the
# daemon, and the split is the point:
#
#   * queue jobs are hours-long tmux sessions; a revloop redeploy would kill
#     them mid-run and burn a retry attempt each. Different restart domain.
#   * the executor resolves a job spec's env NAMES out of its OWN process env,
#     so every variable this service holds is nameable by any queue agent of the
#     token's user. A separate service is what keeps that set minimal — see
#     "Bridge executor role" in docker/claude/README.md.
#
# So the two roles never share a process chain: whichever one is selected, the
# other's supervisor is never started.
# =============================================================================
set -euo pipefail

log()  { printf '[entrypoint] %s\n' "$*" >&2; }
die()  { printf '[entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

# revloop.config.json renderer (pass-through-when-unset). Kept in a sibling
# script so it can be unit-tested standalone (tests-entrypoint-config.sh). It
# lives next to this file both in the image (/usr/local/bin) and in the repo.
ENTRYPOINT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=revloop-config.sh
. "${ENTRYPOINT_DIR}/revloop-config.sh"

WORKSPACE_ROOT="${ALISSA_WORKSPACE_ROOT:-/workspace}"
WORKSPACE_NAME="${ALISSA_WORKSPACE:-alissa-review}"
RUNTIME_USER=alissa

# Truthiness for the container's on/off flags: 1/true/yes/on (case-insensitive),
# anything else — including unset — is off. Shared by the console gate (2d) and
# the bridge-executor gate below, so those two cannot drift apart.
#
# NOT used by the ALISSA_ENABLE_FIREWALL gate in the root block, which still
# tests `= "1"` exactly — a pre-existing narrower contract, left alone here on
# purpose (TASK-112733143): widening it would make a deploy that today sets
# `true`, gets no firewall and therefore never passed --cap-add=NET_ADMIN start
# attempting the firewall init and die on it. That is a rollout, not a drive-by.
is_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *)             return 1 ;;
  esac
}

# -----------------------------------------------------------------------------
# Role selection + the executor's gates (issue #73).
#
# Resolved FIRST, before any bootstrap: a role/gate misconfiguration is a deploy
# mistake, and the cheapest place to say so is the top of the log. This block
# runs on the root pass too, so a refusal happens before the privilege drop.
# -----------------------------------------------------------------------------
CONTAINER_ROLE="$(printf '%s' "${CONTAINER_ROLE:-daemon}" | tr '[:upper:]' '[:lower:]')"
case "${CONTAINER_ROLE}" in
  daemon|executor) ;;
  *) die "CONTAINER_ROLE=${CONTAINER_ROLE} is not a role this image ships. Use 'daemon' (the review loop, the default) or 'executor' (an Alissa Studio queue executor running 'alissa bridge start')." ;;
esac

BRIDGE_EXECUTOR_ID=""
BRIDGE_HANDOFF=""
if [ "${CONTAINER_ROLE}" = "executor" ]; then
  # (a) The master gate. Default OFF: selecting the role is not consent to run
  #     one. An executor claims queued jobs from the whole user's queue and runs
  #     them as unattended agent sessions holding this service's credentials, so
  #     it takes TWO explicit settings to arm — and a service that only has the
  #     role set refuses cleanly instead of quietly registering itself.
  is_truthy "${ALISSA_BRIDGE_EXECUTOR:-0}" \
    || die "CONTAINER_ROLE=executor but ALISSA_BRIDGE_EXECUTOR=${ALISSA_BRIDGE_EXECUTOR:-0} (default off) — refusing to register an executor or claim any job. Set ALISSA_BRIDGE_EXECUTOR=1 on THIS service to arm it (see 'Bridge executor role' in docker/claude/README.md), or unset CONTAINER_ROLE to run the review daemon."

  # (b) The executor id, which is the identity half of the registry key
  #     (userId, executorId). Registration TAKES OVER an existing row with the
  #     same key, so two executors of the same user sharing an id evict each
  #     other in a loop and strand every sticky claim pinned to it. Hence:
  #     defaulted (never the CLI's slugified-hostname fallback, which on a
  #     platform is a random per-deploy string), required non-empty, and
  #     validated here so a bad slug fails at boot instead of at register time.
  #     `-` not `:-`: an explicitly EMPTY value is an error, not a request for
  #     the default — it is exactly how a deploy silently falls back to the
  #     hostname slug.
  BRIDGE_EXECUTOR_ID="${ALISSA_BRIDGE_EXECUTOR_ID-revloop-executor}"
  [ -n "${BRIDGE_EXECUTOR_ID}" ] \
    || die "ALISSA_BRIDGE_EXECUTOR_ID is set but EMPTY — an executor id is required. Leave it unset for the default 'revloop-executor', or give this service its own id; it MUST differ from every other executor of the same Alissa user (the devloop image's executor included), because registering the same id takes the other one over."
  #     The pattern is the CLI's own EXECUTOR_ID_RE, length bound included: an
  #     unbounded version would pass a 65-character id here and let it die
  #     inside `alissa bridge start` after the whole bootstrap had run — which
  #     is the register-time failure this gate exists to convert into a boot-time
  #     one.
  printf '%s' "${BRIDGE_EXECUTOR_ID}" | grep -qE '^[a-z0-9][a-z0-9-]{0,63}$' \
    || die "ALISSA_BRIDGE_EXECUTOR_ID=${BRIDGE_EXECUTOR_ID} is not a valid executor id — lowercase letters, digits and dashes, starting with a letter or digit, at most 64 characters (e.g. 'revloop-executor')."

  # (c) Which agent runs a job whose spec names none. STRUCTURAL, like
  #     agent_profile: it must name a profile the baked agents.yaml ships, and
  #     that file defines exactly `claude`. Drifting to the CLI's own default
  #     would select a profile this image may not have.
  BRIDGE_HANDOFF="${ALISSA_BRIDGE_HANDOFF:-claude}"

  # (d) Identity persistence. The CLI keeps the executor identity (its id and
  #     the fingerprint Studio shows) in ${ALISSA_CONFIG_DIR}/bridge-executor.json,
  #     which defaults to the EPHEMERAL home — so every redeploy would mint a new
  #     fingerprint and re-register as a changed machine. Point the whole config
  #     dir at the persisted volume instead (it also holds the verified API token,
  #     same posture as the claude credential already kept there), and EXPORT it
  #     so the CLI actually sees it.
  #
  #     Volume unavailability is handled, not crashed on: this is the same dir
  #     the auth preflight's writability probe (class 2 below) guards, so a mount
  #     that is missing or root-owned degrades to that capped backoff — log and
  #     retry forever, never a crash loop (2026-07-29 lesson).
  export ALISSA_CONFIG_DIR="${ALISSA_CONFIG_DIR:-${WORKSPACE_ROOT}/.alissa-config}"
  # Logged once, after the privilege drop — this block runs on both passes.
  ROLE_SUMMARY="EXECUTOR — id=${BRIDGE_EXECUTOR_ID}, handoff=${BRIDGE_HANDOFF}, identity file ${ALISSA_CONFIG_DIR}/bridge-executor.json"
else
  ROLE_SUMMARY="daemon (review loop)"
fi

# -----------------------------------------------------------------------------
# 0. Privilege bootstrap (runs only on the first pass, as root)
#
# The container starts as root purely so we can make a platform-provided volume
# writable: a persistent volume (e.g. Railway) mounts at WORKSPACE_ROOT owned by
# root, and the daemon runs as an unprivileged user, so without this it cannot
# even write the generated manifest. We chown the mount, raise the optional
# firewall (which needs root anyway), then re-exec this script as `alissa`.
#
# claude refuses --dangerously-skip-permissions as root, so everything past this
# point MUST run unprivileged — that is exactly what the drop guarantees.
#
# THE WALK IS GUARDED (issue #78). `chown -R` over the persistent volume stats
# every inode of every worktree hub, and the dentry/inode slab it fills is
# charged to the container's cgroup: a 2026-08-09 audit of the Railway service
# found `memory.current` at ~5.98 GB with 176 MB of actual RSS — 3.71 GB of it
# `slab_reclaimable` from this walk, the rest page cache from the hub sync in
# step 3c. The kernel has no pressure to drop any of it below the cgroup limit,
# so the metric plateaus flat at ~6 GB from the moment of deploy and stops being
# usable for spotting a real leak. On a warm restart that whole cost buys
# nothing: the volume is already `alissa`-owned from the previous boot.
#
# So the walk now runs only when a cheap probe says it has to. The probe is
# O(top-level entries) BY CONSTRUCTION — one `stat` over the mount point plus
# its depth-1 children, testing owner AND group, because `alissa:alissa` is what
# the walk asserts and a probe that tested only the owner would read `alissa:root`
# as clean and never repair it. It deliberately does NOT `find … ! -user alissa`:
# find stats every inode too, which is the exact slab storm this guard exists to
# remove.
#
# WHAT THE PROBE CANNOT SEE, precisely: anything DEEPER than the mount point's
# immediate children. A platform console shell running as root is the realistic
# way to get there — hence ALISSA_FORCE_CHOWN=1, which forces the full walk for
# one boot.
#
# AND THE INVARIANT THIS LEANS ON, which is easy to break by accident:
# `chown -R` walks POST-ORDER, so the mount point itself is chowned LAST. That is
# the only reason an interrupted first boot self-heals — a partial walk leaves
# depth 0 still root-owned, so the next boot's probe trips and the walk resumes.
# An "optimisation" that chowned the mount point first would convert every killed
# first boot into a permanently latched half-owned volume. Do not reorder it.
# The same post-order property is why a walk that FAILS deep still leaves depths
# 0 and 1 owned by `alissa` — i.e. looking clean to this probe forever — which is
# why the failure is logged loudly below instead of being swallowed.
#
# Reclaiming after the fact is not an option here: `/sys/fs/cgroup` is mounted
# read-only inside a Railway container, so `echo … > memory.reclaim` fails with
# EROFS even as root (verified 2026-08-09). Avoidance is the only lever, which
# is why there is no reclaim call in this block.
# -----------------------------------------------------------------------------

# Does <path> need the recursive chown? Returns 0 (yes) when the probe finds any
# entry that is not ${RUNTIME_USER}:${RUNTIME_USER} — or when it cannot tell,
# because a walk we did not need is cheaper than a volume the daemon cannot write.
#
# Scope is the path itself plus its immediate children, nothing deeper: that is
# what makes the probe O(top-level entries) instead of O(inodes). One `stat`
# call for the lot, so a hub-per-repo mount costs a single process.
needs_recursive_chown() {
  local path="$1"
  local entry owners owner
  local -a entries=()

  [ -e "${path}" ] || return 0
  entries+=("${path}")
  # Dotfiles included; `..?*` catches `..foo` without ever matching `..`.
  for entry in "${path}"/* "${path}"/.[!.]* "${path}"/..?*; do
    [ -e "${entry}" ] || [ -L "${entry}" ] || continue   # unmatched glob / dangling link
    entries+=("${entry}")
  done

  # `%U:%G`, not `%U`: the repair this guards sets owner AND group, so testing
  # only the owner would leave `alissa:root` looking clean with no repair path
  # left (the unconditional every-boot walk used to be that path). Same single
  # stat call either way.
  #
  # GNU stat does not dereference symlinks without -L, so a dangling link
  # reports its own ownership rather than failing the whole call.
  owners="$(stat -c '%U:%G' -- "${entries[@]}" 2>/dev/null)" || return 0
  [ -n "${owners}" ] || return 0
  while IFS= read -r owner; do
    # An uid/gid with no passwd/group entry prints as UNKNOWN, which is correctly
    # "not ours".
    [ "${owner}" = "${RUNTIME_USER}:${RUNTIME_USER}" ] || return 0
  done <<<"${owners}"
  return 1
}

if [ "$(id -u)" = "0" ]; then
  TMUX_DIR="${TMUX_TMPDIR:-/home/${RUNTIME_USER}/.tmux}"
  mkdir -p "${WORKSPACE_ROOT}" "${TMUX_DIR}"

  # Fix ownership so the unprivileged user can write. -R because a first boot
  # finds a root-owned mount, and a root console shell can leave root-owned
  # files behind — but only when the probe (or the operator) says so, per the
  # block comment above. Each target is decided on its own: the tmux dir is a
  # handful of sockets, the volume is millions of inodes.
  FORCE_CHOWN=0
  if is_truthy "${ALISSA_FORCE_CHOWN:-0}"; then
    FORCE_CHOWN=1
    log "ALISSA_FORCE_CHOWN=${ALISSA_FORCE_CHOWN} — forcing the full recursive chown (the depth-1 probe is skipped; use this once after a root shell has written deeper into the volume)"
  fi
  for CHOWN_TARGET in "${WORKSPACE_ROOT}" "${TMUX_DIR}"; do
    if [ "${FORCE_CHOWN}" = "1" ] || needs_recursive_chown "${CHOWN_TARGET}"; then
      # `|| true` stays — step 0 must not die here — but the STATUS is not
      # discarded any more. `chown -R` continues past per-entry errors and still
      # processes the parent directories (post-order), so a walk that fails deep
      # finishes with depths 0 and 1 owned by ${RUNTIME_USER}: the probe reads
      # clean on every later boot and the unrepaired subtree latches silently.
      # Under the old unconditional walk that failure was retried next boot; now
      # the warning is the operator's only signal, so it must exist. stderr stays
      # suppressed so a per-entry error storm cannot flood the boot log.
      if chown -R "${RUNTIME_USER}:${RUNTIME_USER}" "${CHOWN_TARGET}" 2>/dev/null; then
        CHOWN_OK=1
      else
        CHOWN_OK=0
      fi
      if [ "${FORCE_CHOWN}" = "1" ]; then
        log "chown -R ${RUNTIME_USER}:${RUNTIME_USER} ${CHOWN_TARGET} (forced)"
      else
        log "chown -R ${RUNTIME_USER}:${RUNTIME_USER} ${CHOWN_TARGET} (probe found entries that are not ${RUNTIME_USER}:${RUNTIME_USER})"
      fi
      [ "${CHOWN_OK}" = "1" ] || log "WARNING: chown -R ${CHOWN_TARGET} reported errors — entries below depth 1 may still be foreign-owned, and the depth-1 probe will read this tree as clean on every later boot. Re-deploy once with ALISSA_FORCE_CHOWN=1 if the daemon cannot write."
    else
      log "${CHOWN_TARGET} already owned by ${RUNTIME_USER}:${RUNTIME_USER} — skipping the recursive chown (set ALISSA_FORCE_CHOWN=1 to force it)"
    fi
  done
  log "workspace mount ${WORKSPACE_ROOT} owned by ${RUNTIME_USER}"

  if [ "${ALISSA_ENABLE_FIREWALL:-0}" = "1" ]; then
    log "raising egress firewall (ALISSA_ENABLE_FIREWALL=1)"
    /usr/local/bin/init-firewall.sh \
      || die "firewall init failed — did you pass --cap-add=NET_ADMIN?"
  fi

  log "dropping to ${RUNTIME_USER}"
  exec gosu "${RUNTIME_USER}" "$0" "$@"
fi

log "role: ${ROLE_SUMMARY}"

# -----------------------------------------------------------------------------
# 1. Preflight the three identities
#
# The daemon warns that an identity MISMATCH between the gh token and
# reviewer_login is fatal — but that check lives in the daemon itself. Here we
# only guarantee all three are present and authenticated, then let the daemon
# enforce the mismatch guard at its own startup.
# -----------------------------------------------------------------------------

# 2a. claude / Anthropic — the reviewer agent. NOT fatal: the daemon itself
#     never calls claude (only the worker-spawned reviewer does). Auth can come
#     from a persisted `claude /login` (the preferred, auto-renewing credential
#     at $CLAUDE_CONFIG_DIR/.credentials.json on the volume), or from the env
#     (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY — note a static setup-token
#     expires and 401s). Warn only if NONE of these is present.
CLAUDE_CRED_FILE="${CLAUDE_CONFIG_DIR:-/home/${RUNTIME_USER}/.claude}/.credentials.json"
if [ -s "${CLAUDE_CRED_FILE}" ]; then
  log "claude credential present (persisted login: ${CLAUDE_CRED_FILE})"
elif [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  log "claude credential present (from env)"
else
  log "WARN: no persisted claude login and no ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN — run 'claude /login' once (see README); reviewers will 401 until then"
fi

# 2a-ii. Executor role: the claude credential has to OUTLIVE a redeploy, because
#        a queue job is an hours-long agent session and a re-login is manual. The
#        image points CLAUDE_CONFIG_DIR at the volume already; verify it rather
#        than assume it, and WARN (never die) if a deploy has moved it off — the
#        executor still runs, it just loses the login on the next restart.
if [ "${CONTAINER_ROLE}" = "executor" ]; then
  case "${CLAUDE_CONFIG_DIR:-}" in
    "${WORKSPACE_ROOT}"/*|"${WORKSPACE_ROOT}")
      log "claude config persisted on the volume: ${CLAUDE_CONFIG_DIR}" ;;
    "")
      log "WARN: CLAUDE_CONFIG_DIR is unset — a 'claude /login' lands in the ephemeral home and is gone on the next redeploy. Point it at a path under ${WORKSPACE_ROOT}." ;;
    *)
      log "WARN: CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR} is not under ${WORKSPACE_ROOT} — if that path is not a persisted mount, a 'claude /login' is gone on the next redeploy and job sessions 401 until someone logs in again." ;;
  esac
fi

# 2b. gh — the review queue, round counting, PR comments. gh reads GH_TOKEN /
#     GITHUB_TOKEN from the environment automatically.
if [ -z "${GH_TOKEN:-}" ] && [ -z "${GITHUB_TOKEN:-}" ]; then
  die "no GH_TOKEN (or GITHUB_TOKEN) — cannot watch the review queue"
fi
GH_LOGIN="$(gh api user -q .login 2>/dev/null)" \
  || die "gh token rejected by GitHub (gh api user failed)"
log "gh authenticated as: ${GH_LOGIN}"

# 2b-ii. The REVIEWER identity, explicitly (issue #51).
#
# This container holds more than one GitHub identity, and the default gh
# credential above belongs to whichever one was injected as GH_TOKEN — which on
# studio was the IMPLEMENTER's, so review verdicts posted from here landed under
# the wrong login and never consumed the PR's review request. Setting
# ALISSA_REVIEWER_TOKEN_ENV to the NAME of the variable carrying the reviewer's
# token routes every daemon `gh` call through it explicitly. We resolve it here
# so a bad credential fails at boot with a login in the log, not silently at the
# first verdict; the daemon re-asserts the same identity before every post.
if [ "${CONTAINER_ROLE}" = "executor" ]; then
  # An executor posts no verdicts, so it needs no reviewer identity — and it
  # must not HOLD one. `resolveJobEnv` resolves a job spec's env names out of
  # this process's environment, so any credential parked on this service is
  # nameable by any queue agent of the token's user. Say so rather than
  # silently ignoring the variable.
  if [ -n "${ALISSA_REVIEWER_TOKEN_ENV:-}" ]; then
    log "WARN: ALISSA_REVIEWER_TOKEN_ENV=${ALISSA_REVIEWER_TOKEN_ENV} is set on an EXECUTOR service. The executor posts no reviews, so it is unused — and a job spec can name any variable this service holds. Drop the reviewer credential from this service's env."
  fi
elif [ -n "${ALISSA_REVIEWER_TOKEN_ENV:-}" ]; then
  # Indirect expansion: read the variable whose NAME is in
  # ALISSA_REVIEWER_TOKEN_ENV. The `:-` keeps `set -u` from aborting when that
  # variable does not exist, so the empty check below is what reports it.
  REVIEWER_TOKEN="${!ALISSA_REVIEWER_TOKEN_ENV:-}"
  [ -n "${REVIEWER_TOKEN}" ] \
    || die "ALISSA_REVIEWER_TOKEN_ENV=${ALISSA_REVIEWER_TOKEN_ENV} but that variable is empty — inject the reviewer identity's token there, or unset ALISSA_REVIEWER_TOKEN_ENV to inherit the default credential (and accept that verdicts may post under the wrong login)"
  REVIEWER_LOGIN="$(GH_TOKEN="${REVIEWER_TOKEN}" GITHUB_TOKEN="${REVIEWER_TOKEN}" gh api user -q .login 2>/dev/null)" \
    || die "the reviewer token in ${ALISSA_REVIEWER_TOKEN_ENV} was rejected by GitHub"
  log "reviewer identity: ${REVIEWER_LOGIN} (from \$${ALISSA_REVIEWER_TOKEN_ENV})"
  if [ -n "${ALISSA_REVIEWER_LOGIN:-}" ] && [ "${ALISSA_REVIEWER_LOGIN}" != "${REVIEWER_LOGIN}" ]; then
    die "ALISSA_REVIEWER_LOGIN=${ALISSA_REVIEWER_LOGIN} but \$${ALISSA_REVIEWER_TOKEN_ENV} belongs to ${REVIEWER_LOGIN} — a review posted under the wrong identity is not a verdict of record"
  fi
  [ "${REVIEWER_LOGIN}" = "${GH_LOGIN}" ] \
    && log "NOTE: the reviewer token resolves to the same login as the default gh credential (${GH_LOGIN})" \
    || log "reviewer (${REVIEWER_LOGIN}) and default gh credential (${GH_LOGIN}) are DIFFERENT identities — as intended"
else
  log "WARN: ALISSA_REVIEWER_TOKEN_ENV unset — the daemon will post reviews with the inherited credential (${GH_LOGIN}). In a container holding several identities that is how a round's verdict lands under the wrong login (issue #51)."
fi

# The API token above is enough for `gh api` calls, but NOT for git itself:
# hub-ifying a repo (on_missing_hub:add) does a `git clone`, which needs a git
# credential helper. Wire gh in as that helper so https clones/fetches of
# private repos authenticate with the same token. Non-fatal: an SSH-based or
# public-only setup does not need it.
gh auth setup-git 2>/dev/null \
  && log "git credential helper configured (gh)" \
  || log "WARN: gh auth setup-git failed — private-repo clone/fetch may not authenticate"

# -----------------------------------------------------------------------------
# 2c. alissa — tasks, session queue, verdicts. The CLI reads ALISSA_API_TOKEN,
#     but `auth login` also stores + verifies it, which is the real preflight.
#
# TRIAGE BEFORE INTERPRETING (issue #62). On 2026-07-29 this gate crash-looped a
# Railway deploy through two multi-hour outages while reporting
# "ALISSA_API_TOKEN rejected" — twice, and the token was valid both times. The
# CLI binary (an image-layer file at ~/.local/bin/alissa) had vanished mid-run,
# so `auth login` never reached a server at all; the old line muted stderr and
# called every non-zero exit a rejection. A wrong diagnosis that also exits
# FATAL is the worst of both: it needs a human, and it points the human at the
# wrong thing.
#
# Four classes, only ONE of which a human can fix:
#
#   1. CLI missing / not executable  -> re-bootstrap from the official installer
#                                       and retry (covers the image-layer loss)
#   2. config dir unwritable         -> retry with capped backoff, forever
#   3. API unreachable (transport)   -> retry with capped backoff, forever
#   4. server-side rejection (401/403, login RAN and got an answer)
#                                    -> FATAL, fast, naming token rotation
#
# The default for an UNRECOGNISED failure is retry, not FATAL: every outage this
# gate has actually caused was a platform blip mislabelled as a rejection, and a
# blip must self-heal with zero human action. An unrecognised failure that never
# heals is not silent either — the retry log escalates once it outlasts
# AUTH_ESCALATE_SECONDS and names token rotation as the thing to check.
#
# POSIX shell, no new dependencies: `command -v`, `curl` (already required to
# install the CLI), and the CLI itself.
# -----------------------------------------------------------------------------
[ -n "${ALISSA_API_TOKEN:-}" ] \
  || die "no ALISSA_API_TOKEN — cannot reach tasks / session queue"

# The official installer, and the CLI's own two location knobs, with the same
# defaults the CLI itself applies.
#
# ALISSA_INSTALL_URL is overridable for tests-entrypoint-auth.sh ONLY. It is
# NOT a supported production lever: pointing it elsewhere makes this entrypoint
# execute remote code from wherever it points. A deploy leaves it unset. (No
# checksum is pinned against the default deliberately — a pin in this repo goes
# stale on every installer release, and a stale pin turns the self-healing
# re-bootstrap into a hard failure during exactly the incident it exists for.)
ALISSA_INSTALL_URL="${ALISSA_INSTALL_URL:-https://share.alissa.app/install}"
ALISSA_CONFIG_DIR="${ALISSA_CONFIG_DIR:-${HOME}/.config/alissa}"
ALISSA_API_BASE="${ALISSA_API_BASE:-https://api.alissa.app}"
# Capped exponential backoff for the self-healing classes: 30s doubling to a
# 10m ceiling, per issue #62. Overridable so the entrypoint test suite can
# exercise the retry loop without waiting out real minutes; production sets
# neither.
AUTH_RETRY_SECONDS="${ALISSA_AUTH_RETRY_SECONDS:-30}"
AUTH_RETRY_CAP_SECONDS="${ALISSA_AUTH_RETRY_CAP_SECONDS:-600}"
# How long the retry loop stays quiet-ish before every further attempt also
# names the one thing a human could act on. Retrying forever is the contract;
# retrying forever without ever saying so is how a silent outage happens.
AUTH_ESCALATE_SECONDS="${ALISSA_AUTH_ESCALATE_SECONDS:-600}"

# Strip the token out of anything captured from a child before it is logged.
# The entrypoint hands the token on the command line (`--token "${…}"`), so
# whether it comes back in an error string is the CLI's choice, not ours — and
# the retry loop below logs its capture on EVERY iteration, forever, so a token
# echoed once would be echoed into the platform's log retention indefinitely.
# Redacting at capture covers every consumer (the retry line and the `die`) by
# construction. Bash pattern replacement: no subprocess, no new dependency.
redact_token() {
  printf '%s' "${1//"${ALISSA_API_TOKEN}"/***REDACTED***}"
}

# A genuine server-side rejection: `auth login` RAN, reached the API and was
# told no. Matched on the CLI's own error text, which carries the HTTP status.
# Deliberately narrow — this is the only class that pages a human, so anything
# it does not recognise falls through to the retry path.
auth_rejected() {
  printf '%s' "$1" | grep -qiE '\b(401|403)\b|unauthori[sz]ed|forbidden|invalid token|token (is )?(invalid|rejected|expired)'
}

# Reachability at TRANSPORT level only. curl exit 22 means the server answered
# with an HTTP error — that is a REACHABLE API (the base URL 404s on plenty of
# healthy deployments), and treating it as unreachable would retry forever
# against a server that is up. Only a DNS/connect/TLS/timeout failure counts.
# The probe's own stderr is kept (API_PROBE_ERR) so the retry line can say what
# curl actually reported, not just that "something" was unreachable.
API_PROBE_ERR=""
api_reachable() {
  API_PROBE_RC=0
  API_PROBE_ERR="$(curl -fsS -o /dev/null --max-time 10 "${ALISSA_API_BASE}" 2>&1)" \
    || API_PROBE_RC=$?
  [ "${API_PROBE_RC}" = "0" ] || [ "${API_PROBE_RC}" = "22" ]
}

# The CLI stores the verified token in its config dir, so an unwritable dir
# fails the login with an error that looks nothing like a token problem.
config_dir_writable() {
  mkdir -p "${ALISSA_CONFIG_DIR}" 2>/dev/null || return 1
  ( : > "${ALISSA_CONFIG_DIR}/.write-probe" ) 2>/dev/null || return 1
  rm -f "${ALISSA_CONFIG_DIR}/.write-probe" 2>/dev/null || true
}

AUTH_WAITED=0
AUTH_DELAY="${AUTH_RETRY_SECONDS}"
# Sleep out one backoff step and double it, capped. Also the single place the
# "this is not healing" escalation is emitted, so every retry class gets it.
auth_backoff() {
  log "retrying alissa auth in ${AUTH_DELAY}s (waited ${AUTH_WAITED}s so far; retrying forever — this class of failure needs no human)"
  if [ "${AUTH_WAITED}" -ge "${AUTH_ESCALATE_SECONDS}" ]; then
    log "ERROR: alissa preflight has been retrying for ${AUTH_WAITED}s without succeeding. The daemon is NOT up. If the platform is healthy, check that ALISSA_API_TOKEN in the Railway env is current."
  fi
  sleep "${AUTH_DELAY}"
  AUTH_WAITED=$((AUTH_WAITED + AUTH_DELAY))
  AUTH_DELAY=$((AUTH_DELAY * 2))
  [ "${AUTH_DELAY}" -le "${AUTH_RETRY_CAP_SECONDS}" ] || AUTH_DELAY="${AUTH_RETRY_CAP_SECONDS}"
}

while :; do
  # (1) CLI present and executable? An image-layer file that disappeared
  #     mid-run is not a credential problem — reinstall it and carry on.
  if ! command -v alissa >/dev/null 2>&1; then
    log "alissa CLI missing from PATH (image-layer loss) — re-bootstrapping from ${ALISSA_INSTALL_URL}"
    # Both halves' stderr is captured (nothing muted): a proxy blocking the
    # download and an installer that ran and failed are different problems.
    if BOOTSTRAP_ERR="$({ curl -fsSL "${ALISSA_INSTALL_URL}" | bash; } 2>&1 >/dev/null)"; then
      log "alissa CLI re-bootstrapped"
    else
      # Redacted like the login capture: the token is not passed to the
      # installer, but this is an unconstrained shell pipeline's output and the
      # boundary is the only part of that we control.
      log "WARN: alissa CLI re-bootstrap failed: $(redact_token "${BOOTSTRAP_ERR:-<no output>}")"
    fi
    # The installer drops the launcher into a directory already on PATH, but
    # bash caches lookups — clear it before deciding whether this worked.
    hash -r 2>/dev/null || true
    if ! command -v alissa >/dev/null 2>&1; then
      log "WARN: alissa CLI still missing after re-bootstrap"
      auth_backoff
      continue
    fi
  fi

  # (2) Can the CLI store the login it is about to verify?
  if ! config_dir_writable; then
    log "WARN: alissa config dir ${ALISSA_CONFIG_DIR} is not writable — the verified token cannot be stored (volume not yet mounted / wrong ownership?)"
    auth_backoff
    continue
  fi

  # (3) Is the API reachable at all? Asked BEFORE the login so a network blip
  #     can never be read as a credential verdict.
  if ! api_reachable; then
    log "WARN: ${ALISSA_API_BASE} is unreachable at transport level (DNS/connect/TLS, curl exit ${API_PROBE_RC}) — not a token problem. curl said: ${API_PROBE_ERR:-<no output>}"
    auth_backoff
    continue
  fi

  # (4) The real preflight. stderr is CAPTURED, never muted: two multi-hour
  #     false diagnoses on 2026-07-29 were bought by `2>&1` into /dev/null.
  if LOGIN_ERR="$(alissa auth login --token "${ALISSA_API_TOKEN}" 2>&1 >/dev/null)"; then
    log "alissa authenticated"
    break
  fi
  LOGIN_ERR="$(redact_token "${LOGIN_ERR:-<no output>}")"
  if auth_rejected "${LOGIN_ERR}"; then
    die "ALISSA_API_TOKEN rejected by ${ALISSA_API_BASE} — the API answered and refused this token. Rotate ALISSA_API_TOKEN in the Railway service env (Variables -> ALISSA_API_TOKEN) and redeploy; retrying cannot fix a credential. alissa auth login said: ${LOGIN_ERR}"
  fi
  log "WARN: alissa auth login failed, and the error is NOT a server-side rejection — treating it as transient. alissa auth login said: ${LOGIN_ERR}"
  auth_backoff
done

# -----------------------------------------------------------------------------
# 2d. Reviewer console (sidecar) gate — preflighted HERE, launched at step 4b.
#
# The console (alissa-revloop-ui) is OPT-IN via ALISSA_UI_ENABLED (default off).
# We resolve the flag and validate its one hard requirement now — before any
# bootstrap work — so a misconfigured deploy fails FAST with a clear message,
# consistent with the identity gates above (die at boot, not after the worker is
# up). ALISSA_UI_PASSCODE is the ONLY gate on the console (and, once the platform
# routes a public URL to it, on the whole operator surface — including its kill
# and retry-now actions), so ENABLED without a passcode is fatal, exactly as the
# sidecar itself is fail-closed on the passcode.
#
# Truthiness matches the sidecar's own _env_flag (1/true/yes/on, case-insensitive)
# so `ALISSA_UI_ENABLED=true` behaves like `=1`; anything else (incl. unset) is off.
# -----------------------------------------------------------------------------
#
# The console renders the review loop's own panels (spawn ledger, review-*
# sessions), of which an executor has none — so the executor role never starts
# it, and says so if a deploy asked for one.
UI_ENABLED=0
if is_truthy "${ALISSA_UI_ENABLED:-0}"; then UI_ENABLED=1; fi
UI_PORT="${PORT:-8080}"
if [ "${CONTAINER_ROLE}" = "executor" ]; then
  [ "${UI_ENABLED}" = "1" ] \
    && log "WARN: ALISSA_UI_ENABLED is set on an EXECUTOR service — the reviewer console is a review-loop surface and is NOT started here; no listener." \
    || log "reviewer console not applicable in the executor role — no listener"
  UI_ENABLED=0
elif [ "${UI_ENABLED}" = "1" ]; then
  [ -n "${ALISSA_UI_PASSCODE:-}" ] \
    || die "ALISSA_UI_ENABLED is set but ALISSA_UI_PASSCODE is empty — the console is fail-closed on the passcode (it is the ONLY gate, and it rides the public URL once you enable networking). Set ALISSA_UI_PASSCODE, or unset ALISSA_UI_ENABLED."
  log "reviewer console ENABLED (ALISSA_UI_ENABLED) — will serve on 0.0.0.0:${UI_PORT} (passcode required)"
else
  log "reviewer console disabled (ALISSA_UI_ENABLED unset/off) — no listener"
fi

# -----------------------------------------------------------------------------
# 2e. Resolve the reviewer model into the baked agents.yaml.
#
# The reviewer is the pipeline's quality gate, but the baked claude profile pins
# no model — so it inherits the persisted /login account's default, which can
# silently fall back to a smaller model when a plan hits its usage threshold. We
# pin it explicitly at boot: ALISSA_AGENT_MODEL (default `claude-fable-5-1`, the
# latest and most capable generally-available model, one tier above Opus) is
# appended to the profile's `command:` as `--model <value>`. The value passes
# through verbatim — aliases (`opus`, `sonnet`) and full ids (`claude-opus-4-8`)
# are both valid, no allowlist. `default` or an empty value omits the flag
# entirely, restoring the account-default behavior.
#
# Precedence: we only rewrite the BAKED default (identified by its `alissa-managed:`
# marker). A custom agents.yaml mounted over this path carries no marker, so it is
# left verbatim and ALISSA_AGENT_MODEL is ignored for it — the mounted command
# always wins. The baked file lives in the ephemeral home (not on the volume), so
# it is pristine on every boot and this rewrite is idempotent.
# -----------------------------------------------------------------------------
AGENTS_YAML="${HOME}/.config/alissa/agents.yaml"
# Default `claude-fable-5-1` only when UNSET (use `-`, not `:-`): an explicitly
# empty value is a valid opt-out and must NOT be re-defaulted back to the pin.
AGENT_MODEL="${ALISSA_AGENT_MODEL-claude-fable-5-1}"
if [ ! -f "${AGENTS_YAML}" ]; then
  log "WARN: ${AGENTS_YAML} not found — skipping model pin (worker will fall back to a bare claude)"
elif ! grep -q 'alissa-managed:' "${AGENTS_YAML}"; then
  log "custom agents.yaml in effect (no alissa-managed marker) — using it verbatim, ALISSA_AGENT_MODEL ignored"
else
  BASE_CMD="claude --dangerously-skip-permissions --permission-mode acceptEdits"
  if [ -z "${AGENT_MODEL}" ] || [ "${AGENT_MODEL}" = "default" ]; then
    CLAUDE_CMD="${BASE_CMD}"
    log "reviewer model: account default (ALISSA_AGENT_MODEL='${AGENT_MODEL}') — no --model flag"
  else
    # The pin is appended VERBATIM (no model allowlist, by design), which makes
    # it a route for smuggling extra flags into the reviewer spawn: a value of
    # `opus --permission-mode acceptEdits` renders a command whose explicit mode
    # overrides --dangerously-skip-permissions and re-enables the hard prompts
    # that wedge headless sessions (issue #116, PR #117 round-1 finding). A
    # model alias or id is one bare token, so refuse anything with whitespace
    # or a leading dash — same posture as the executor-id validation above:
    # a mis-set knob is fatal at boot with a message that names the fix.
    case "${AGENT_MODEL}" in
      -*|*[[:space:]]*)
        die "ALISSA_AGENT_MODEL='${AGENT_MODEL}' is not a model alias or id — it must be one bare token (no whitespace, no leading dash). The value is appended verbatim to the claude command as '--model <value>', so anything else smuggles extra flags into the reviewer spawn; an explicit --permission-mode there overrides --dangerously-skip-permissions and re-enables the permission prompts that wedge headless sessions (issue #116). Use an alias or id such as 'opus' or 'claude-fable-5-1', or 'default' / empty to inherit the account default." ;;
    esac
    CLAUDE_CMD="${BASE_CMD} --model ${AGENT_MODEL}"
    log "reviewer model: ${AGENT_MODEL} (ALISSA_AGENT_MODEL)"
  fi
  # Rewrite the profile's `command:` line. python (not sed) so an arbitrary
  # passed-through model value can't collide with a substitution metacharacter.
  python3 - "${AGENTS_YAML}" "${CLAUDE_CMD}" <<'PY' || die "failed to render agents.yaml"
import re, sys
path, cmd = sys.argv[1], sys.argv[2]
out, seen = [], False
for ln in open(path).read().splitlines(keepends=True):
    m = re.match(r'^(\s*)command:\s', ln)
    if m and not seen:
        out.append(f"{m.group(1)}command: {cmd}\n")
        seen = True
    else:
        out.append(ln)
if not seen:
    sys.exit("no `command:` line found in agents.yaml")
open(path, "w").writelines(out)
PY
  log "effective reviewer command: ${CLAUDE_CMD}"
fi

# 2e-ii. Executor role: put the resolved agents.yaml where the CLI will look.
#
# The CLI reads agent profiles from ${ALISSA_CONFIG_DIR}/agents.yaml, and the
# role block at the top moved that dir onto the volume — so the baked profile
# in the ephemeral home would be invisible and `alissa bridge start` would reject
# every job with "no agent profile named claude". Copy the just-resolved file
# across on EVERY boot (never generate-if-absent): the image is the source of
# truth for the profile, and a copy left on the volume by an older image must not
# outlive it. Both locations stay populated, so whichever one a job session
# resolves finds the same profile.
#
# The profile deliberately carries no `disable_alissa_code`, which is what makes
# the CLI launch it via `alissa code -y --handoff claude` — that is the wrapper
# registering the codeSession and its 10-minute log checkpoints. Adding the flag
# would launch a bare claude and lose both.
if [ "${CONTAINER_ROLE}" = "executor" ] && [ -f "${AGENTS_YAML}" ]; then
  EXECUTOR_AGENTS_YAML="${ALISSA_CONFIG_DIR}/agents.yaml"
  if [ "${EXECUTOR_AGENTS_YAML}" = "${AGENTS_YAML}" ]; then
    log "executor agent profiles: ${AGENTS_YAML} (config dir is the baked one)"
  elif mkdir -p "${ALISSA_CONFIG_DIR}" 2>/dev/null \
       && cp "${AGENTS_YAML}" "${EXECUTOR_AGENTS_YAML}" 2>/dev/null; then
    log "executor agent profiles: copied ${AGENTS_YAML} -> ${EXECUTOR_AGENTS_YAML}"
  else
    # Non-fatal by design. The auth preflight above already proved this exact
    # directory writable, so reaching here means the mount changed under us —
    # and a mount blip must not become a crash loop (2026-07-29). The job that
    # would have used the profile fails with the CLI's own "no agent profile"
    # rejection, which is a per-job failure, not a dead service.
    log "WARN: could not copy agents.yaml to ${EXECUTOR_AGENTS_YAML} (volume not writable?) — jobs will be rejected with 'no agent profile' until the next boot"
  fi
fi

# -----------------------------------------------------------------------------
# 3. Bootstrap the workspace (bootstrap-from-manifest model)
#
# Reviewers cd into {root}/{repo}/main worktree hubs. With on_missing_hub:add
# the daemon hub-ifies each repo itself on first review request, so we do NOT
# pre-clone anything — we only guarantee a manifest and a revloop config
# exist. Either may be mounted; otherwise we generate them from env.
# -----------------------------------------------------------------------------
mkdir -p "${WORKSPACE_ROOT}"

MANIFEST="${WORKSPACE_ROOT}/alissa-workspace.yaml"
# ALISSA_REVIEW_REPOS: "|"-separated owner/repo allowlist ("|" because repo
# slugs contain "/"); a single repo needs no separator. Whitespace around
# entries is stripped. This helper prints one repo per line.
repos_lines() {
  printf '%s' "${ALISSA_REVIEW_REPOS:-}" \
    | tr '|' '\n' \
    | sed 's/[[:space:]]//g' \
    | grep -v '^$'
}

# ALISSA_REVIEW_OPERATORS: "|"-separated GitHub logins allowed to ack a
# review-loop re-entry on a capped PR (`alissa-review: re-enter +N`). Same
# convention as the repo allowlist; unset means no ack is ever honoured.
operators_lines() {
  printf '%s' "${ALISSA_REVIEW_OPERATORS:-}" \
    | tr '|' '\n' \
    | sed 's/[[:space:]]//g' \
    | grep -v '^$'
}

# Skills installed into every reviewer session (manifest `skills:`). Same
# "|"-separated convention. Defaults to the workspace + review skills; override
# with ALISSA_REVIEW_SKILLS. alissa-session / alissa-skills-usage are installed
# by `alissa code` automatically, so they need not be listed.
skills_lines() {
  printf '%s' "${ALISSA_REVIEW_SKILLS:-alissa-code-workspace|alissa-code-review}" \
    | tr '|' '\n' \
    | sed 's/[[:space:]]//g' \
    | grep -v '^$'
}

CONFIG="${WORKSPACE_ROOT}/revloop.config.json"

if [ -n "$(repos_lines)" ]; then
  # ENV-DRIVEN MODE: ALISSA_REVIEW_REPOS is authoritative, so (re)generate the
  # manifest + config on EVERY boot. The files persist on the /workspace volume,
  # so "generate only if absent" would pin them to the first boot's value and a
  # later Railway env change would silently never apply. Regenerating is safe:
  # the allowlist is the full set of repos the daemon may touch (on_missing_hub
  # only hub-ifies repos already in it), and the cloned hub dirs on the volume
  # are untouched by rewriting this text.
  #
  # The EXECUTOR role shares this exact flow — a queue job runs inside the same
  # worktree hubs a reviewer does, so the manifest is what makes those hubs
  # exist. It skips only revloop.config.json, which configures a daemon this
  # service never starts.
  if [ "${CONTAINER_ROLE}" = "executor" ]; then
    log "generating ${MANIFEST} from ALISSA_REVIEW_REPOS (executor role: no revloop.config.json — no daemon here)"
  else
    log "generating ${MANIFEST} + revloop.config.json from ALISSA_REVIEW_REPOS"
  fi
  {
    printf 'name: %s\n' "${WORKSPACE_NAME}"
    printf 'description: Containerized Alissa review daemon workspace\n'
    printf 'repos:\n'
    repos_lines | while IFS= read -r r; do
      printf '  - repo: %s\n' "${r}"
    done
    printf 'reviewers: []\n'
    printf 'skills:\n'
    skills_lines | while IFS= read -r s; do
      printf '  - %s\n' "${s}"
    done
    printf 'attributes: {}\n'
  } > "${MANIFEST}"

  # The generated config PASSES THROUGH optional tuning keys: an unset
  # ALISSA_POLL_INTERVAL / ALISSA_ROUND_CAP / ALISSA_STABILITY_ROUNDS is omitted
  # so the daemon library's
  # own default applies (env var > library default, no shadowing entrypoint
  # layer). Structural keys (on_missing_hub, agent_profile) are always emitted.
  # See revloop-config.sh for the precedence contract and per-key rationale.
  if [ "${CONTAINER_ROLE}" != "executor" ]; then
    repos_json="$(repos_lines | jq -R . | jq -s -c .)"
    # `|| true` because an EMPTY operator list is the normal case: the last
    # filter in operators_lines is a grep, which exits 1 when it matches nothing,
    # and under `set -e -o pipefail` that non-zero status would kill the
    # entrypoint mid-bootstrap. jq's own status still propagates.
    operators_json="$({ operators_lines || true; } | jq -R . | jq -s -c .)"
    render_revloop_config "${repos_json}" "${operators_json}" > "${CONFIG}"
  fi
else
  # MOUNTED MODE: no allowlist in the env — respect a mounted workspace as-is.
  [ -f "${MANIFEST}" ] \
    || die "no alissa-workspace.yaml mounted and ALISSA_REVIEW_REPOS is empty — nothing to work on (the manifest is what materializes the worktree hubs a reviewer, or a queue job, runs inside)"
  log "using mounted workspace at ${WORKSPACE_ROOT} (ALISSA_REVIEW_REPOS unset)"
fi

# -----------------------------------------------------------------------------
# 3a. Seed claude's first-run config so reviewers start headless.
#
# A fresh user hangs on claude's first-run gates (welcome/theme, the one-time
# --dangerously-skip-permissions warning, and a per-directory "trust this
# folder?" prompt that the flag does NOT suppress). We pre-set the flags so the
# TUI comes up ready. Auth is separate — the persisted `claude /login` credential
# lives in $CLAUDE_CONFIG_DIR/.credentials.json and is never touched here.
#
# CLAUDE_CONFIG_DIR reliably relocates only .credentials.json; whether it also
# moves the state/settings files is undocumented, so we seed BOTH $HOME and
# $CLAUDE_CONFIG_DIR — whichever claude reads, the flags are there. Merges are
# load-then-update, so a persisted login (oauthAccount etc.) is preserved.
# -----------------------------------------------------------------------------
python3 - "${WORKSPACE_ROOT}" <<'PY' || true
import glob, json, os, sys
root = sys.argv[1]
home = os.path.expanduser("~")
ccdir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()

# Reviewer working dirs to pre-trust: allowlisted repos (basename of owner/repo,
# even before hub-ified) plus any hub main/ already on disk.
paths = set()
for r in os.environ.get("ALISSA_REVIEW_REPOS", "").replace("|", "\n").split():
    r = r.strip()
    if "/" in r:
        paths.add(os.path.join(root, r.split("/")[-1], "main"))
paths.update(glob.glob(os.path.join(root, "*", "main")))

def merge(path, apply):
    try:
        d = json.load(open(path))
    except Exception:
        d = {}
    apply(d)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(d, open(path, "w"), indent=2)

def state(d):  # ~/.claude.json equivalent: onboarding + per-project trust
    d["hasCompletedOnboarding"] = True
    d["hasSeenAutoModeEntryWarning"] = True
    d.setdefault("lastOnboardingVersion", "2.1.215")
    pr = d.setdefault("projects", {})
    for p in paths:
        pr.setdefault(p, {})["hasTrustDialogAccepted"] = True

def settings(d):  # settings.json: skip the bypass-mode prompt, theme, TUI
    d["skipDangerousModePermissionPrompt"] = True
    d.setdefault("theme", "dark")
    d.setdefault("tui", "fullscreen")

state_targets = [os.path.join(home, ".claude.json")]
settings_targets = [os.path.join(home, ".claude", "settings.json")]
if ccdir:
    state_targets.append(os.path.join(ccdir, ".claude.json"))
    settings_targets.append(os.path.join(ccdir, "settings.json"))
for t in state_targets:
    merge(t, state)
for t in settings_targets:
    merge(t, settings)
print(f"[entrypoint] seeded claude first-run config; pre-trusted {len(paths)} reviewer dir(s)")
PY

# -----------------------------------------------------------------------------
# 3b. Reset the stale in-flight ledger.
#
# The daemon's spawn ledger persists on the /workspace volume, but the tmux
# server, its sessions, and the worker queue all live in the ephemeral home and
# are gone on every (re)start. So after a redeploy the ledger still says round N
# is "in-flight" for sessions that no longer exist, and the daemon waits the full
# 90-min stall before re-enqueuing — nothing reviews in the meantime.
#
# A fresh container has no reviewer running by definition (tmux server is down),
# so every `spawns` row is stale: clear them and the daemon re-enqueues on its
# first poll. `escalations` is kept, so capped-out PRs are not re-escalated.
#
# Daemon-only: the ledger belongs to `alissa-revloop`, which the executor role
# never starts. An executor's own in-flight jobs are reconciled server-side
# (`reconcileResumed`), not from this file.
STATE_DB="${WORKSPACE_ROOT}/.revloop/state.db"
if [ "${CONTAINER_ROLE}" != "executor" ] && [ -f "${STATE_DB}" ]; then
  python3 - "${STATE_DB}" <<'PY' || true
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
try:
    n = db.execute("DELETE FROM spawns").rowcount
    db.commit()
    print(f"[entrypoint] cleared {n} stale in-flight spawn record(s) from the ledger")
except sqlite3.OperationalError:
    pass  # table not created yet — nothing to clear
finally:
    db.close()
PY
fi

# -----------------------------------------------------------------------------
# 3c. Materialize the worktree hubs the manifest declares.
#
# The manifest lists every allowlisted repo, but the hub directories
# (bare clone + main/ worktree) don't exist until something creates them. The
# daemon's on_missing_hub:add uses `alissa code workspace add`, which is
# idempotent BY MANIFEST ENTRY -- for a repo already listed it no-ops, leaving an
# empty folder and no main/, and the daemon then loops forever hub-ifying a hub
# that never completes. `workspace sync` is the reconcile operation: it creates
# the missing/half-built hubs (and fetches existing ones) to match the manifest.
# Run it here (auth is wired, the manifest exists) so every hub is real before
# the daemon polls -- exactly the manual `sync` an operator would otherwise run.
if [ -f "${MANIFEST}" ]; then
  log "syncing worktree hubs to the manifest (alissa code workspace sync)"
  ( cd "${WORKSPACE_ROOT}" && alissa code workspace sync ) \
    || log "WARN: workspace sync did not fully complete — the daemon will retry per-repo, but check for clone/auth errors above"
fi

# -----------------------------------------------------------------------------
# 3c-ii. Volume hygiene — `alissa code workspace prune` (issue #81).
#
# Nothing in this container ever removed anything from the volume. Every review
# round materializes a per-PR worktree in a hub (plus whatever a session's build
# leaves behind), merged or closed PRs leave theirs in place forever, and the
# bare `.source` object store only grows. `alissa code workspace prune` is the
# remediation primitive: it removes finished-branch worktrees behind the CLI's
# own safety rails (never `main/`, never a dirty tree without --force, an age
# gate, a live-tmux-session guard, never a worktree whose PR is still open, and
# a lookup failure KEEPS the worktree) and then runs the hub-level
# `git worktree prune` / `git remote prune origin` / `git gc --auto` sequence.
#
# Three properties this wiring must have, in order of how much they matter:
#
#   * it can never take the container down. Prune is an optimization; a failing
#     one leaves exactly the disk usage we already had. So: best-effort at boot,
#     and an interval loop that warns and waits for the next tick.
#   * `--force` is NEVER passed. In a review container a dirty worktree is
#     evidence — a session that died mid-round, with uncommitted work someone
#     may still want. The rail that keeps it is the one this entrypoint most
#     needs, so the flag that defeats it is not exposed at all.
#   * it is CAPABILITY-GATED, not version-pinned. The image installs the alissa
#     CLI from an install channel that may predate the subcommand, and a hard
#     pin here would go stale on every CLI release. An older CLI gets exactly
#     one loud warning and both hooks skip.
#
# CONCURRENCY WITH LIVE REVIEWER SESSIONS, since the interval loop fires into
# hubs that may be mid-round: the CLI's live-session guard covers WORKTREE
# REMOVAL, and for the hub-level tail we rely on git's own guarantee rather than
# on the sweep being session-aware. `git gc --auto` never deletes a reachable
# object, keeps unreachable ones for `gc.pruneExpire` (2 weeks by default),
# publishes rewritten packs by atomic rename, and drops loose objects only after
# that — so a concurrent checkout/commit in a sibling worktree sees a consistent
# store throughout. What would NOT be safe is a pruning `gc --prune=now`, which
# this entrypoint never asks for and has no knob to ask for, exactly like
# `--force`.
#
# The interval loop is DAEMON-ONLY, and deliberately so: the executor role
# `exec`s `alissa bridge start` a few lines below, which replaces this shell —
# a loop started here would outlive the only code that knows how to stop it and
# would keep pruning against jobs it cannot see being torn down. The executor
# still gets the boot pass, because it grows the same volume the same way.
# -----------------------------------------------------------------------------
PRUNE_ENABLED=0
PRUNE_INTERVAL_MINUTES=360
PRUNE_INTERVAL_SECONDS=$(( 360 * 60 ))
PRUNE_TIMEOUT_SECONDS=240
PRUNE_KILL_AFTER_SECONDS=30
# Never contains `--force`. See the block comment above; the entrypoint has no
# knob that adds it, which is the point.
PRUNE_ARGS=()

# Does this CLI ship `alissa code workspace prune`?
#
# NOT "does `--help` exit 0". Commander answers an UNKNOWN subcommand by
# printing the PARENT command's help and exiting 0 — verified against alissa
# CLI 0.1.0, where `alissa code workspace prune --help` prints the `workspace`
# help and exits 0 without a word about prune. A probe on the exit status alone
# would therefore report every old CLI as capable and we would discover the
# truth one failing prune at a time. So the probe reads the OUTPUT: a real
# subcommand prints its own `Usage: … workspace prune …`, and a help that lists
# a `prune` command counts too.
workspace_prune_supported() {
  local out
  out="$(alissa code workspace prune --help 2>&1)" || return 1
  printf '%s\n' "${out}" \
    | grep -qE '^[[:space:]]*(Usage:[[:space:]].*workspace[[:space:]]+prune|prune([[:space:]]|$))'
}

# One prune pass. ALWAYS returns 0: both callers treat a failure as "warn and
# carry on", and the interval loop runs under this script's `set -e`, where a
# non-zero return would silently kill the loop instead of the pass.
#
# BOUNDED AND STDIN-CLOSED, and that is the boot pass's whole safety story. A
# failing pass is harmless — the volume simply keeps what it holds — but a pass
# that BLOCKS would hold the boot open, and this hook runs before the worker
# starts. Two ways that happens, both closed here rather than at the call site
# so the interval pass inherits them too:
#
#   * the first pass after the CLI lands runs `git gc --auto` across hubs on a
#     volume that, by this feature's own diagnosis, has never had anything
#     reclaimed. On a multi-GB object store that is minutes — and on a platform
#     with a startup health-check window, a slow enough boot IS a dead
#     container, which is the one outcome this wiring must never cause.
#   * command substitution inherits stdin, so a CLI that ever prompts would
#     wait forever on a container that has no tty.
#
# `-k` IS PART OF THE BOUND, not a refinement of it. Plain `timeout` sends TERM
# and then waits indefinitely, so a child that traps TERM is unbounded again —
# the same boot-held-open failure, one step further along. That is not a
# hypothetical for this subcommand: `workspace prune` is the destructive one, and
# a handler that finishes the in-flight git operation before exiting is exactly
# what a careful implementation would install, so the bound would go missing at
# the moment the CLI lands and the feature stops being a no-op. `-k` gives such a
# handler PRUNE_KILL_AFTER_SECONDS to finish and then KILLs it — and that path
# reports 137 (128+9), NOT 124, which is why the WARN below has a branch of its
# own for it. Do not "simplify" the two into one: `timeout` normalises nothing
# here (measured on coreutils 9.1), so a 124-only branch files the very case `-k`
# exists for under the generic "exited N" message.
#
# A timed-out pass is just another non-zero exit into the WARN path below (124,
# named there so the log says "timed out" instead of a bare number). This is
# deliberately NOT how `workspace sync` above is treated: sync is a
# precondition — no hubs, no reviews — while prune is an optimization, and only
# one of those may hold a boot open.
#
# The CLI's per-worktree verdicts are captured and re-emitted through log(), so
# they carry the entrypoint prefix and are greppable in the platform's boot log
# next to everything else this boot did. They go through redact_token() first:
# every other captured-CLI-output path in this file does (see the auth triage),
# prune shells out to git across every hub, and an https hub cloned by the
# daemon's own on_missing_hub:add path has a credential helper wired in by
# `gh auth setup-git` at step 1 — a failing `git remote prune origin` is one of
# the few places that could echo a URL.
workspace_prune_run() {
  local label="$1" out line rc=0
  # When the pass started, so the 137 branch below can tell OUR kill from someone
  # else's. `timeout` reports 128+signal for ANY signal death of the child, so
  # 137 alone does not mean "we killed it after the grace" — an OOM kill looks
  # identical, and on a memory-capped container a `git gc --auto` sweep over a
  # never-pruned object store is the best OOM candidate the boot has (issue #74).
  local started=${SECONDS}
  # Run from the workspace root: the workspace binding is cwd-based, exactly as
  # for the `workspace sync` above.
  out="$( cd "${WORKSPACE_ROOT}" \
          && timeout -k "${PRUNE_KILL_AFTER_SECONDS}" "${PRUNE_TIMEOUT_SECONDS}" \
             alissa code workspace prune ${PRUNE_ARGS[@]+"${PRUNE_ARGS[@]}"} </dev/null 2>&1 )" \
    || rc=$?
  if [ -n "${out}" ]; then
    while IFS= read -r line; do log "prune[${label}]: $(redact_token "${line}")"; done <<<"${out}"
  fi
  if [ "${rc}" = "0" ]; then
    log "workspace prune (${label}) finished"
  elif [ "${rc}" = "124" ]; then
    log "WARN: workspace prune (${label}) TIMED OUT after ${PRUNE_TIMEOUT_SECONDS}s and was terminated — the boot was not held open, and nothing was removed. A first pass over a never-pruned volume can legitimately need longer: raise ALISSA_WORKSPACE_PRUNE_TIMEOUT_SECONDS, but keep it (plus the grace) inside your platform's health-check window."
  elif [ "${rc}" = "137" ] && [ $(( SECONDS - started )) -ge "${PRUNE_TIMEOUT_SECONDS}" ]; then
    # 124 is `timeout`'s own "I terminated it" and needs no corroboration. 137 is
    # NOT ours to claim: it is 128+9, which `timeout` reports for any SIGKILL
    # death of the child — `timeout 100 bash -c 'kill -9 $$'` returns 137 with no
    # `-k` involved at all. The elapsed check is the disambiguator: only a pass
    # that actually ran out its bound can have been killed by our grace.
    #
    # An early 137 therefore falls through to the generic branch below, which
    # says only what it knows. That matters because the realistic early cause is
    # the OOM killer, and this branch's remedy — raise the timeout — would make
    # an OOM strictly worse (the next pass runs longer and allocates more before
    # dying), while pointing the investigation at the CLI instead of the memory
    # ceiling.
    log "WARN: workspace prune (${label}) TIMED OUT after ${PRUNE_TIMEOUT_SECONDS}s and did NOT exit on SIGTERM, so it was KILLED after the ${PRUNE_KILL_AFTER_SECONDS}s grace — the boot was not held open, and nothing was removed. Raise ALISSA_WORKSPACE_PRUNE_TIMEOUT_SECONDS (keeping it plus the grace inside your health-check window); if this recurs, the CLI is trapping SIGTERM and the grace may need to be longer."
  else
    log "WARN: workspace prune (${label}) exited ${rc} — nothing was removed by this pass; the volume keeps what it holds until the next one. Check the verdict lines above."
  fi
  return 0
}

# Default ON: this container is the one whose volume is growing, so the knob is
# an opt-OUT. Same truthiness as every other gate here.
if ! is_truthy "${ALISSA_WORKSPACE_PRUNE:-1}"; then
  log "workspace prune DISABLED (ALISSA_WORKSPACE_PRUNE=${ALISSA_WORKSPACE_PRUNE:-1}) — nothing in this container reclaims finished worktrees, so the volume grows until someone prunes it by hand"
elif ! workspace_prune_supported; then
  # Exactly one warning, and it is loud: this is a silent-growth condition, and
  # the only signal an operator gets that the hygiene they think is running is
  # not. Both hooks stay off — see the capability-gate rationale above.
  log "WARN: this alissa CLI predates 'alissa code workspace prune' — workspace prune is SKIPPED at boot AND on the interval, and the volume WILL grow. Upgrade the CLI in the image's install channel; nothing about this entrypoint needs to change when it lands."
else
  PRUNE_ENABLED=1

  # `--min-age-hours` is a SAFETY knob: a larger value keeps MORE worktrees. A
  # value the CLI would reject therefore must not fall back to the CLI's own
  # default — that would prune worktrees younger than the operator asked to
  # keep, which is the one direction of error that destroys something. Fail
  # closed instead: skip the whole feature for this boot and say why.
  if [ -n "${ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS:-}" ]; then
    if printf '%s' "${ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS}" | grep -qE '^[0-9]+$'; then
      PRUNE_ARGS+=( --min-age-hours "${ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS}" )
    else
      PRUNE_ENABLED=0
      log "WARN: ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS=${ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS} is not a whole number of hours — workspace prune is SKIPPED entirely rather than run with the CLI's default age gate, because that gate could be SHORTER than the one you asked for. Fix the value (or unset it to accept the CLI's default) and redeploy."
    fi
  fi
fi

if [ "${PRUNE_ENABLED}" = "1" ]; then
  # How long ONE pass may run before it is killed — a bound, not a budget.
  #
  # 240s, and the number is chosen against a specific window rather than being
  # "generously large": this pass runs at 3c-ii, while the console that serves
  # the documented `/healthz` deployment health check does not start until 4b —
  # so the pass sits IN FRONT of the endpoint the platform probes, and Railway's
  # healthcheck timeout defaults to 300s.
  #
  # BE PRECISE ABOUT WHAT THIS BUYS, because 240+30 < 300 is easy to misread as a
  # guarantee that the boot fits the window. It is not one. This bounds the step
  # this file's prune hook owns; the boot's TOTAL is bounded by nothing here —
  # `3c` (`workspace sync`) is deliberately unbounded, for the reason argued 80
  # lines up, and `2c`'s auth retry ceiling is 600s on its own. 240 is prune's
  # SHARE of a window those steps also draw on, and it is chosen because 900 gave
  # this one step more than the whole window on the platform the README walks
  # through.
  #
  # The trade runs one way: a timed-out pass costs a warning and nothing else —
  # the volume simply keeps what it holds until the next tick — while an overrun
  # boot costs the container. So the default protects the boot, and the known
  # slow case (a first sweep over a never-pruned volume) is served by RAISING the
  # knob, which is exactly what the timeout WARN tells the operator to do.
  #
  # Both roles resolve it, because both run the boot pass. Garbage falls back to
  # the default with a warning rather than failing closed: a bound that is merely
  # wrong is still a bound, and disabling the hygiene over a typo would trade the
  # smaller problem for the larger one.
  PRUNE_TIMEOUT_SECONDS="${ALISSA_WORKSPACE_PRUNE_TIMEOUT_SECONDS:-240}"
  if ! printf '%s' "${PRUNE_TIMEOUT_SECONDS}" | grep -qE '^[1-9][0-9]*$'; then
    log "WARN: ALISSA_WORKSPACE_PRUNE_TIMEOUT_SECONDS=${PRUNE_TIMEOUT_SECONDS} is not a positive whole number of seconds — falling back to 240"
    PRUNE_TIMEOUT_SECONDS=240
  fi
  # Grace for a CLI that traps TERM, before KILL. Operator-tunable; see the env
  # table in docker/claude/README.md (this is NOT one of the test-only overrides).
  # 30s is long enough for an in-flight git operation to finish, and the pair —
  # timeout PLUS grace — is what has to fit whatever window sits in front of this
  # container, not the timeout alone.
  PRUNE_KILL_AFTER_SECONDS="${ALISSA_WORKSPACE_PRUNE_KILL_AFTER_SECONDS:-30}"
  if ! printf '%s' "${PRUNE_KILL_AFTER_SECONDS}" | grep -qE '^[1-9][0-9]*$'; then
    log "WARN: ALISSA_WORKSPACE_PRUNE_KILL_AFTER_SECONDS=${PRUNE_KILL_AFTER_SECONDS} is not a positive whole number of seconds — falling back to 30"
    PRUNE_KILL_AFTER_SECONDS=30
  fi

  if [ "${CONTAINER_ROLE}" = "executor" ]; then
    # No interval resolution at all in this role: it never reaches 4c, and
    # validating, defaulting and ANNOUNCING a cadence it will not run would put
    # two contradicting lines in its boot log.
    log "workspace prune ENABLED — boot pass only (executor role; min-age: ${ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS:-CLI default}, timeout ${PRUNE_TIMEOUT_SECONDS}s+${PRUNE_KILL_AFTER_SECONDS}s, --force never passed). The interval loop belongs to the daemon's process chain: this role 'exec's the bridge, so there would be nothing left here to tear a loop down."
  else
    # The interval is not a safety knob — a bad one only changes how often a
    # best-effort pass runs — so a garbage value falls back to the default with
    # a warning instead of disabling the feature.
    PRUNE_INTERVAL_MINUTES="${ALISSA_WORKSPACE_PRUNE_INTERVAL_MINUTES:-360}"
    if ! printf '%s' "${PRUNE_INTERVAL_MINUTES}" | grep -qE '^[1-9][0-9]*$'; then
      log "WARN: ALISSA_WORKSPACE_PRUNE_INTERVAL_MINUTES=${PRUNE_INTERVAL_MINUTES} is not a positive whole number of minutes — falling back to 360"
      PRUNE_INTERVAL_MINUTES=360
    fi
    PRUNE_INTERVAL_SECONDS=$(( PRUNE_INTERVAL_MINUTES * 60 ))

    # ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS is TEST-ONLY, on the same footing
    # as ALISSA_AUTH_RETRY_SECONDS and ALISSA_INSTALL_URL — and, like them, it
    # is documented as test-only in docker/claude/README.md rather than only
    # here: an override a shipped entrypoint honours but no operator can find is
    # a footgun. tests-entrypoint-prune.sh has to watch the loop tick more than
    # once, and the smallest cadence the supported knob can express is a minute.
    # A deploy leaves it unset.
    if [ -n "${ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS:-}" ]; then
      if printf '%s' "${ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS}" | grep -qE '^[1-9][0-9]*$'; then
        PRUNE_INTERVAL_SECONDS="${ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS}"
        log "workspace prune: interval overridden to ${PRUNE_INTERVAL_SECONDS}s (ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS — test-only, not for a deploy)"
      else
        log "WARN: ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS=${ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS} is not a positive whole number of seconds — ignoring it"
      fi
    fi

    log "workspace prune ENABLED — boot pass now, then every ${PRUNE_INTERVAL_MINUTES} min (min-age: ${ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS:-CLI default}, timeout ${PRUNE_TIMEOUT_SECONDS}s+${PRUNE_KILL_AFTER_SECONDS}s, --force never passed)"
  fi

  # Best-effort, exactly like the other boot steps: prune is an optimization,
  # never a boot precondition. workspace_prune_run already swallows the status;
  # the `|| true` is the belt to that suspenders, and says so at the call site.
  workspace_prune_run boot || true
fi

# -----------------------------------------------------------------------------
# 3d. EXECUTOR ROLE — hand the container over to `alissa bridge start`.
#
# This is the end of the shared path. `exec` replaces this shell, so tini's only
# child is the executor itself: no worker, no console, no `alissa-revloop`, and
# nothing of this entrypoint left to be torn down with them. That is the "not in
# the daemon's process chain" property, enforced by construction rather than by
# a flag someone has to remember.
#
# Flag mapping, on this repo's usual pass-through contract (env > CLI default,
# no hidden entrypoint layer):
#
#   ALISSA_BRIDGE_EXECUTOR_ID   -> --executor-id     STRUCTURAL, always passed
#   ALISSA_BRIDGE_HANDOFF       -> --handoff         STRUCTURAL, always passed
#   ALISSA_BRIDGE_LABEL         -> --label           pass-through (unset: the CLI
#                                                    falls back to the hostname)
#   ALISSA_BRIDGE_MAX_CONCURRENT-> --max-concurrent  pass-through (CLI clamps 1-16)
#   ALISSA_BRIDGE_POLL_SECONDS  -> --interval        pass-through (CLI default 15s)
#
# The two structural ones are pinned for the same reason `agent_profile` is: the
# CLI's own fallbacks (slugified hostname, its default handoff) are wrong for a
# platform container — a per-deploy hostname is an identity that takes over its
# own registry row every redeploy, and a handoff this image has no profile for
# rejects every job.
#
# Extra CMD args still pass through, so `docker run … --once` works for a smoke
# test exactly as the daemon's flags do.
# -----------------------------------------------------------------------------
if [ "${CONTAINER_ROLE}" = "executor" ]; then
  mkdir -p "${TMUX_TMPDIR:-/home/${RUNTIME_USER}/.tmux}"

  # Drop this executor's stale lockfile before starting.
  #
  # The CLI guards against two executors of the same id on one machine with
  # ${ALISSA_CONFIG_DIR}/bridge/executor-<id>.lock, holding the owner's pid, and
  # decides staleness with a bare `process.kill(pid, 0)` — no boot id, no start
  # time, no cmdline. That was safe while the config dir was the ephemeral home,
  # because a lock could not outlive its container. It is NOT safe now that the
  # dir is on the volume: any ungraceful stop (OOM, platform hard-restart,
  # SIGKILL after the grace period) leaves the file behind, and the next boot
  # evaluates that pid against a fresh pid namespace that restarts at 1 and
  # replays the same deterministic boot — so it can easily be live, and can even
  # be this container's own executor (`exec` hands the CLI this shell's pid).
  # The CLI then refuses to start, exits non-zero, the platform restarts it, and
  # it repeats: a crash loop, which is the one thing this entrypoint must never
  # cause (2026-07-29).
  #
  # Same argument step 3b makes for the spawn ledger: a fresh container has no
  # executor running, by definition, so a lock found here is stale by
  # construction. Scoped to OUR id and NOT globbed over `executor-*.lock`: if a
  # config dir is ever shared with another container, a glob would delete a LIVE
  # peer's lock. A stale lock under some other id is never consulted for ours,
  # so leaving it is harmless.
  rm -f "${ALISSA_CONFIG_DIR}/bridge/executor-${BRIDGE_EXECUTOR_ID}.lock" 2>/dev/null || true

  BRIDGE_ARGS=( --executor-id "${BRIDGE_EXECUTOR_ID}"
                --workspace-root "${WORKSPACE_ROOT}"
                --handoff "${BRIDGE_HANDOFF}" )
  if [ -n "${ALISSA_BRIDGE_LABEL:-}" ]; then
    BRIDGE_ARGS+=( --label "${ALISSA_BRIDGE_LABEL}" )
  fi
  if [ -n "${ALISSA_BRIDGE_MAX_CONCURRENT:-}" ]; then
    BRIDGE_ARGS+=( --max-concurrent "${ALISSA_BRIDGE_MAX_CONCURRENT}" )
  fi
  if [ -n "${ALISSA_BRIDGE_POLL_SECONDS:-}" ]; then
    BRIDGE_ARGS+=( --interval "${ALISSA_BRIDGE_POLL_SECONDS}" )
  fi
  log "starting alissa bridge start (executor ${BRIDGE_EXECUTOR_ID}) over ${WORKSPACE_ROOT}"
  exec alissa bridge start "${BRIDGE_ARGS[@]}" "$@"
fi

# -----------------------------------------------------------------------------
# 4. Start the worker, wait until it reports running.  (daemon role only)
# -----------------------------------------------------------------------------
mkdir -p "${TMUX_TMPDIR:-/home/alissa/.tmux}"

log "starting alissa worker (detached)"
alissa worker start --daemon --interval "${ALISSA_WORKER_INTERVAL:-2}" \
  || die "alissa worker failed to start"

# Poll status until it is up (the daemon only warns if the worker is absent).
worker_up=0
for _ in $(seq 1 15); do
  if alissa worker status 2>/dev/null | grep -qiv 'not running\|no worker' \
     && alissa worker status 2>/dev/null | grep -qi 'running'; then
    worker_up=1; break
  fi
  sleep 1
done
[ "${worker_up}" = "1" ] || die "alissa worker did not come up within 15s"
log "alissa worker is running"

# -----------------------------------------------------------------------------
# 4b. Optionally start the reviewer console sidecar (alissa-revloop-ui).
#
# Opt-in via ALISSA_UI_ENABLED (resolved + passcode-preflighted at 2d). It runs
# as a backgrounded child of this entrypoint, so tini (PID 1) reaps it with the
# rest of the tmux/node/claude fan-out. It binds 0.0.0.0:${PORT:-8080} — not the
# sidecar's own 127.0.0.1:8788 default — so a platform (Railway) can route its
# public URL to the container; the passcode is the ONLY gate (see the README
# warning). ALISSA_UI_PASSCODE is read straight from the inherited env (already
# validated present), and it watches the same WORKSPACE_ROOT the daemon does —
# the state.db written below it and the review-* sessions — so its panels are
# truthful. It is started AFTER the worker so the process list it renders is the
# real one from its first paint.
#
# Fail-VISIBLE supervision: the console is a sidecar, not the primary function
# (the daemon + worker are), so its death does NOT tear the container down — but
# a silent disappearance would strand an operator at a dead URL. A tiny monitor
# subshell polls the pid and logs LOUDLY if it exits. (It cannot `wait` on the
# sidecar — a subshell can only wait on its own children — so it polls instead.)
# -----------------------------------------------------------------------------
UI_PID=""
UI_MONITOR_PID=""
if [ "${UI_ENABLED}" = "1" ]; then
  log "starting reviewer console (alissa-revloop-ui) on 0.0.0.0:${UI_PORT}"
  alissa-revloop-ui --host 0.0.0.0 --port "${UI_PORT}" \
    --workspace-root "${WORKSPACE_ROOT}" &
  UI_PID=$!
  (
    while kill -0 "${UI_PID}" 2>/dev/null; do sleep 30; done
    log "WARN: reviewer console (alissa-revloop-ui, pid ${UI_PID}) EXITED — the console URL is dead until the next redeploy; the daemon and worker keep running"
  ) &
  UI_MONITOR_PID=$!
fi

# -----------------------------------------------------------------------------
# 4c. The periodic workspace prune (daemon role only — see 3c-ii).
#
# Same in-container background pattern as the console monitor above: a subshell
# child of this entrypoint, so tini reaps it, and a pid the shutdown handler
# kills. `sleep` FIRST, so the interval measures from the boot pass that 3c-ii
# already ran rather than pruning twice in a row on every restart.
#
# The loop cannot die of a prune failure: workspace_prune_run returns 0 always
# (a non-zero return under `set -e` would end the subshell, turning one failed
# pass into no more passes until the next redeploy).
# -----------------------------------------------------------------------------
PRUNE_PID=""
if [ "${PRUNE_ENABLED}" = "1" ]; then
  log "starting the workspace prune loop (every ${PRUNE_INTERVAL_MINUTES} min = ${PRUNE_INTERVAL_SECONDS}s)"
  (
    while true; do
      sleep "${PRUNE_INTERVAL_SECONDS}"
      workspace_prune_run interval
    done
  ) &
  PRUNE_PID=$!
fi

# -----------------------------------------------------------------------------
# 5. Run the daemon in the foreground; stop the worker on shutdown.
# -----------------------------------------------------------------------------
DAEMON_PID=""
shutdown() {
  log "shutting down"
  # Silence the console monitor FIRST so tearing the sidecar down below does not
  # trip its "EXITED" alarm during an orderly shutdown.
  [ -n "${UI_MONITOR_PID}" ] && kill "${UI_MONITOR_PID}" 2>/dev/null || true
  # The prune loop goes down with it, and for the same reason: an orderly
  # shutdown must not start a pass into a container that is being torn down
  # (the CLI walks hubs and can run `git gc` — work worth not interrupting).
  [ -n "${PRUNE_PID}" ] && kill "${PRUNE_PID}" 2>/dev/null || true
  [ -n "${UI_PID}" ] && kill "${UI_PID}" 2>/dev/null || true
  [ -n "${DAEMON_PID}" ] && kill "${DAEMON_PID}" 2>/dev/null || true
  alissa worker stop >/dev/null 2>&1 || true
  wait "${DAEMON_PID}" 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

# Extra daemon flags (e.g. -v, --once, --dry-run) pass through as CMD args.
log "starting alissa-revloop over ${WORKSPACE_ROOT}"
alissa-revloop --workspace-root "${WORKSPACE_ROOT}" "$@" &
DAEMON_PID=$!
wait "${DAEMON_PID}"
