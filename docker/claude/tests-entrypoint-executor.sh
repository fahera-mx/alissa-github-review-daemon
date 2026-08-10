#!/usr/bin/env bash
# =============================================================================
# Tests for the entrypoint's BRIDGE EXECUTOR role (issue #73).
#
# The image serves two services from one artifact: the review daemon
# (CONTAINER_ROLE unset/daemon) and an Alissa Studio queue executor
# (CONTAINER_ROLE=executor, running `alissa bridge start`). The executor is a
# separate SERVICE on purpose — queue jobs are hours-long tmux sessions and a
# revloop redeploy would kill them mid-run — so the property that matters most
# is that the two roles never share a process chain.
#
# This boots the REAL entrypoint.sh (CLIs stubbed, no docker needed — CI has no
# docker-in-docker, and the image adds layers, not entrypoint behaviour):
#
#   1. role=executor, gate off (default)  -> refuses cleanly, starts NOTHING
#   2. role=executor, gate on             -> execs `alissa bridge start` with the
#                                            structural flags; no worker, no
#                                            daemon, no revloop.config.json;
#                                            identity + agent profile on the volume
#   3. executor id required + validated   -> empty dies, bad slug dies
#   4. the pass-through knobs             -> label / concurrency / interval only
#                                            appear when they are set
#   5. daemon role, bridge env set        -> still the daemon; bridge never runs
#   6. unknown CONTAINER_ROLE             -> dies naming the two it ships
#   4b. the id length bound              -> matches the CLI's own 64, so a long
#                                            id is refused at boot, not later
#   7. volume unwritable                  -> capped backoff, NOT a crash loop,
#                                            and it self-heals mid-flight
#   7b. a stale executor lock             -> swept, so persisting the config dir
#                                            cannot wedge the next boot
#   8. CLAUDE_CONFIG_DIR off the volume   -> WARNs, still boots
#   9. firewall + agents.yaml             -> the allowlist really does admit what
#                                            a job session needs; the profile
#                                            really does still route via alissa code
#
# Usage: bash docker/claude/tests-entrypoint-executor.sh
# Needs: bash, python3, jq (the entrypoint's own bootstrap uses both).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENTRYPOINT="${HERE}/entrypoint.sh"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1" >&2; fail=1; }
info() { printf '%s\n' "$*"; }

assert_contains() {
  if grep -qF -- "$2" "$1"; then pass "$3"; else bad "$3 (not in $1: $2)"; fi
}
assert_not_contains() {
  if grep -qF -- "$2" "$1"; then bad "$3 (unexpected in $1: $2)"; else pass "$3"; fi
}
assert_file()     { if [ -e "$1" ]; then pass "$2"; else bad "$2 (missing: $1)"; fi; }
assert_no_file()  { if [ -e "$1" ]; then bad "$2 (unexpected file: $1)"; else pass "$2"; fi; }
assert_eq()       { if [ "$1" = "$2" ]; then pass "$3"; else bad "$3 (expected '$2', got '$1')"; fi; }

TMPROOT="$(mktemp -d)"
BIN="${TMPROOT}/bin"; mkdir -p "${BIN}"
FAKE_HOME="${TMPROOT}/home"; mkdir -p "${FAKE_HOME}/.config/alissa"
WORKSPACE="${TMPROOT}/workspace"; mkdir -p "${WORKSPACE}"
SPY="${TMPROOT}/spy"; mkdir -p "${SPY}"
cp "${HERE}/agents.yaml" "${FAKE_HOME}/.config/alissa/agents.yaml"
cleanup() {
  chmod -R u+rwX "${TMPROOT}" 2>/dev/null || true
  rm -rf "${TMPROOT}"
}
trap cleanup EXIT

cat > "${BIN}/gh" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  "api user -q .login") echo alissa-app ;;
esac
exit 0
STUB

# The alissa CLI stub. Every surface the entrypoint touches records that it was
# reached, so a test can assert what the role did AND what it never did.
# `bridge start` records its full argv and then blocks like the real daemon,
# because the entrypoint `exec`s it and never comes back.
cat > "${BIN}/alissa" <<'STUB'
#!/usr/bin/env bash
case "$1 $2" in
  "auth login")
    echo "Authenticated."
    exit 0
    ;;
  "worker start")
    echo "start" >> "${SPY_DIR}/worker-start"
    exit 0
    ;;
  "worker status")
    echo "worker is running"
    exit 0
    ;;
  "code workspace")
    echo "sync" >> "${SPY_DIR}/workspace-sync"
    exit 0
    ;;
  "bridge start")
    shift 2
    # Model the CLI's own single-daemon lock, because the entrypoint's stale-lock
    # sweep is only meaningful against it. Faithful to `acquireExecutorLock` /
    # `runningExecutorPid`: the lock is ${ALISSA_CONFIG_DIR}/bridge/executor-<id>.lock
    # holding a pid, and staleness is decided by a bare liveness check with no
    # boot id and no start time — so a leftover file whose pid happens to be live
    # in the new namespace refuses the start and exits non-zero.
    eid="revloop-executor"
    prev=""
    for arg in "$@"; do
      [ "${prev}" = "--executor-id" ] && eid="${arg}"
      prev="${arg}"
    done
    lock="${ALISSA_CONFIG_DIR:-${HOME}/.config/alissa}/bridge/executor-${eid}.lock"
    if [ -f "${lock}" ] && kill -0 "$(cat "${lock}" 2>/dev/null)" 2>/dev/null; then
      echo "A bridge executor \"${eid}\" is already running on this machine (pid $(cat "${lock}"))." >&2
      exit 1
    fi
    mkdir -p "$(dirname "${lock}")"
    printf '%s\n' "$$" > "${lock}"
    printf '%s\n' "$*" > "${SPY_DIR}/bridge-argv"
    # Deliberately NO lock cleanup on the way out: an ungraceful stop is exactly
    # the condition the sweep exists for.
    trap 'exit 0' TERM INT
    sleep 600 &
    wait $!
    exit 0
    ;;
esac
exit 0
STUB

# The review daemon. It must never be reached in the executor role — the spy
# file is the assertion, not the sleep.
cat > "${BIN}/alissa-revloop" <<'STUB'
#!/usr/bin/env bash
echo "start" >> "${SPY_DIR}/revloop-start"
trap 'exit 0' TERM INT
sleep 600 &
wait $!
STUB

# The API reachability probe. Always up: this suite is about the role, and the
# transport classes have their own suite (tests-entrypoint-auth.sh).
cat > "${BIN}/curl" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod 0755 "${BIN}/gh" "${BIN}/alissa" "${BIN}/alissa-revloop" "${BIN}/curl"

reset_spies() {
  rm -f "${SPY}"/bridge-argv "${SPY}"/worker-start "${SPY}"/revloop-start \
        "${SPY}"/workspace-sync
  rm -rf "${WORKSPACE:?}"/* "${WORKSPACE:?}"/.alissa-config
  BOOT_ARGS=()
}

BOOT_PID=""
BOOT_STATUS=0
# Extra CMD args handed to the entrypoint itself (not env assignments) — the
# `docker run … --once` path. Reset by reset_spies; `${…[@]+…}` so an empty
# array is safe under `set -u`.
BOOT_ARGS=()
start_boot() {
  local log="$1"; shift
  env -i \
    PATH="${BIN}:/usr/local/bin:/usr/bin:/bin" \
    HOME="${FAKE_HOME}" \
    TMUX_TMPDIR="${TMPROOT}/tmux" \
    ALISSA_WORKSPACE_ROOT="${WORKSPACE}" \
    GH_TOKEN=dev-default-token \
    ALISSA_API_TOKEN=stub-alissa-token \
    ALISSA_REVIEW_REPOS="fahera-mx/example-repo" \
    SPY_DIR="${SPY}" \
    ALISSA_AUTH_RETRY_SECONDS=1 \
    ALISSA_AUTH_RETRY_CAP_SECONDS=4 \
    "$@" \
    bash "${ENTRYPOINT}" ${BOOT_ARGS[@]+"${BOOT_ARGS[@]}"} > "${log}" 2>&1 &
  BOOT_PID=$!
}

# wait_boot <logfile> <max seconds> <marker> — until the marker appears in the
# log (the role's "I am up" milestone) or the boot dies. Then stop it.
wait_boot() {
  local log="$1" limit="$2" marker="$3" i
  BOOT_STATUS=0
  for i in $(seq 1 "${limit}"); do
    grep -qF "${marker}" "${log}" && break
    kill -0 "${BOOT_PID}" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "${BOOT_PID}" 2>/dev/null; then
    kill -TERM "${BOOT_PID}" 2>/dev/null || true
    # The executor role `exec`s the CLI, so SIGTERM reaches the stub directly.
    BOOT_STATUS=0
  fi
  wait "${BOOT_PID}" 2>/dev/null || BOOT_STATUS=$?
}

EXECUTOR_UP="starting alissa bridge start"
DAEMON_UP="alissa worker is running"

# -----------------------------------------------------------------------------
info "1. role=executor with the gate OFF (default) -> refuses, starts nothing"
# -----------------------------------------------------------------------------
reset_spies
LOG1="${TMPROOT}/gate-off.log"
start_boot "${LOG1}" CONTAINER_ROLE=executor
wait_boot "${LOG1}" 20 "${EXECUTOR_UP}"

if [ "${BOOT_STATUS}" != "0" ]; then
  pass "the boot exits non-zero"
else
  bad "an unarmed executor should not have kept running"
fi
assert_contains "${LOG1}" "ALISSA_BRIDGE_EXECUTOR=0 (default off)" \
  "the refusal names the gate and says it defaults off"
assert_contains "${LOG1}" "refusing to register an executor or claim any job" \
  "...and says what it refused to do"
assert_no_file "${SPY}/bridge-argv" "alissa bridge start is NEVER invoked"
assert_no_file "${SPY}/worker-start" "and neither is the worker"
assert_no_file "${SPY}/revloop-start" "and neither is the review daemon"

# -----------------------------------------------------------------------------
info ""
info "2. role=executor, gate ON -> execs the bridge, and only the bridge"
# -----------------------------------------------------------------------------
reset_spies
LOG2="${TMPROOT}/executor.log"
start_boot "${LOG2}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1
wait_boot "${LOG2}" 30 "${EXECUTOR_UP}"

assert_file "${SPY}/bridge-argv" "alissa bridge start is invoked"
ARGV="$(cat "${SPY}/bridge-argv" 2>/dev/null || true)"
assert_contains "${SPY}/bridge-argv" "--executor-id revloop-executor" \
  "the executor id defaults to revloop-executor (distinct from the devloop's)"
assert_contains "${SPY}/bridge-argv" "--workspace-root ${WORKSPACE}" \
  "it serves the container's workspace root"
assert_contains "${SPY}/bridge-argv" "--handoff claude" \
  "the handoff is pinned to the profile the image bakes"
case "${ARGV}" in
  *--label*|*--max-concurrent*|*--interval*)
    bad "unset knobs must be OMITTED so the CLI default applies (got: ${ARGV})" ;;
  *) pass "unset label/concurrency/interval are omitted (CLI default applies)" ;;
esac

# The process-chain property, which is the whole reason this is a second service.
assert_no_file "${SPY}/worker-start" "the alissa worker is NOT started in the executor role"
assert_no_file "${SPY}/revloop-start" "the review daemon is NOT started in the executor role"
assert_contains "${LOG2}" "reviewer console not applicable in the executor role" \
  "and neither is the console"

# Bootstrap reuse: the manifest and the hubs, but no daemon config.
assert_file "${WORKSPACE}/alissa-workspace.yaml" "the workspace manifest is generated"
assert_file "${SPY}/workspace-sync" "the worktree hubs are synced, as in the daemon role"
assert_no_file "${WORKSPACE}/revloop.config.json" \
  "revloop.config.json is NOT written (there is no daemon here to read it)"
assert_contains "${LOG2}" "executor role: no revloop.config.json" \
  "...and the log says so"

# Identity + agent profile on the persisted volume.
assert_contains "${LOG2}" "identity file ${WORKSPACE}/.alissa-config/bridge-executor.json" \
  "the executor identity file is placed on the volume"
assert_file "${WORKSPACE}/.alissa-config/agents.yaml" \
  "the resolved agents.yaml is copied to the executor's config dir"
assert_contains "${WORKSPACE}/.alissa-config/agents.yaml" "--model opus" \
  "...with the model pin applied, exactly as the daemon role gets it"
assert_not_contains "${WORKSPACE}/.alissa-config/agents.yaml" "disable_alissa_code" \
  "...and WITHOUT disable_alissa_code, so job sessions still launch via alissa code"

# -----------------------------------------------------------------------------
info ""
info "3. the executor id is required and validated"
# -----------------------------------------------------------------------------
reset_spies
LOG3A="${TMPROOT}/id-empty.log"
start_boot "${LOG3A}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1 \
                      ALISSA_BRIDGE_EXECUTOR_ID=
wait_boot "${LOG3A}" 20 "${EXECUTOR_UP}"
[ "${BOOT_STATUS}" != "0" ] && pass "an EMPTY executor id is fatal" \
  || bad "an empty executor id should not have booted"
assert_contains "${LOG3A}" "an executor id is required" "the reason says it is required"
assert_contains "${LOG3A}" "takes the other one over" \
  "...and why a shared id is the hazard"
assert_no_file "${SPY}/bridge-argv" "nothing registers"

reset_spies
LOG3B="${TMPROOT}/id-bad.log"
start_boot "${LOG3B}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1 \
                      ALISSA_BRIDGE_EXECUTOR_ID="Revloop Executor"
wait_boot "${LOG3B}" 20 "${EXECUTOR_UP}"
[ "${BOOT_STATUS}" != "0" ] && pass "a malformed executor id is fatal at BOOT" \
  || bad "a malformed executor id should not have booted"
assert_contains "${LOG3B}" "is not a valid executor id" "the reason names the slug shape"
assert_no_file "${SPY}/bridge-argv" "nothing registers"

# -----------------------------------------------------------------------------
info ""
info "4. the pass-through knobs reach the CLI when they are set"
# -----------------------------------------------------------------------------
reset_spies
LOG4="${TMPROOT}/knobs.log"
start_boot "${LOG4}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1 \
                     ALISSA_BRIDGE_EXECUTOR_ID=revloop-executor-2 \
                     ALISSA_BRIDGE_LABEL="Revloop executor (staging)" \
                     ALISSA_BRIDGE_MAX_CONCURRENT=3 \
                     ALISSA_BRIDGE_POLL_SECONDS=45 \
                     ALISSA_BRIDGE_HANDOFF=claude
wait_boot "${LOG4}" 30 "${EXECUTOR_UP}"
assert_contains "${SPY}/bridge-argv" "--executor-id revloop-executor-2" "a custom id wins"
assert_contains "${SPY}/bridge-argv" "--label Revloop executor (staging)" \
  "the Studio label is passed through"
assert_contains "${SPY}/bridge-argv" "--max-concurrent 3" "the concurrency cap is passed through"
assert_contains "${SPY}/bridge-argv" "--interval 45" \
  "ALISSA_BRIDGE_POLL_SECONDS maps to the CLI's --interval"

# Extra CMD args, which the README promotes as the one-poll smoke test
# (`docker run … --once`). Without the trailing "$@" on the exec line every
# assertion above still passes, so this is what pins it — position included,
# since a flag ahead of the structural ones would be a different command.
reset_spies
BOOT_ARGS=( --once )
LOG4B="${TMPROOT}/cmd-args.log"
start_boot "${LOG4B}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1
wait_boot "${LOG4B}" 30 "${EXECUTOR_UP}"
assert_contains "${SPY}/bridge-argv" "--once" "extra CMD args reach alissa bridge start"
case "$(cat "${SPY}/bridge-argv" 2>/dev/null || true)" in
  *"--handoff claude --once") pass "...after the structural flags, not ahead of them" ;;
  *) bad "extra args must come LAST (got: $(cat "${SPY}/bridge-argv" 2>/dev/null || true))" ;;
esac

info ""
info "4b. the executor id boundary matches the CLI's own (64 characters)"
# The CLI's EXECUTOR_ID_RE caps the id at 64. An unbounded boot check would let a
# 65-character id through and kill it INSIDE `alissa bridge start`, after the
# whole bootstrap — the register-time failure this gate exists to prevent.
ID64="$(printf 'a%.0s' $(seq 1 64))"
ID65="$(printf 'a%.0s' $(seq 1 65))"
reset_spies
LOG4C="${TMPROOT}/id-64.log"
start_boot "${LOG4C}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1 \
                      ALISSA_BRIDGE_EXECUTOR_ID="${ID64}"
wait_boot "${LOG4C}" 30 "${EXECUTOR_UP}"
assert_contains "${SPY}/bridge-argv" "--executor-id ${ID64}" \
  "a 64-character id is accepted (the CLI's own bound)"
reset_spies
LOG4D="${TMPROOT}/id-65.log"
start_boot "${LOG4D}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1 \
                      ALISSA_BRIDGE_EXECUTOR_ID="${ID65}"
wait_boot "${LOG4D}" 20 "${EXECUTOR_UP}"
[ "${BOOT_STATUS}" != "0" ] && pass "a 65-character id is refused at BOOT, not at register time" \
  || bad "a 65-character id should not have booted"
assert_no_file "${SPY}/bridge-argv" "...so the bootstrap never runs for it"

# -----------------------------------------------------------------------------
info ""
info "5. the daemon role is unaffected, even with the bridge env set"
# -----------------------------------------------------------------------------
reset_spies
LOG5="${TMPROOT}/daemon.log"
start_boot "${LOG5}" ALISSA_BRIDGE_EXECUTOR=1 ALISSA_BRIDGE_EXECUTOR_ID=revloop-executor
wait_boot "${LOG5}" 30 "${DAEMON_UP}"
assert_contains "${LOG5}" "role: daemon (review loop)" "the default role is the daemon"
assert_no_file "${SPY}/bridge-argv" \
  "the gate alone never starts an executor — the ROLE selects it"
assert_file "${SPY}/worker-start" "the worker still starts"
assert_file "${WORKSPACE}/revloop.config.json" "and revloop.config.json is still written"
assert_contains "${LOG5}" "starting alissa-revloop" "the daemon still runs"

# -----------------------------------------------------------------------------
info ""
info "6. an unknown role is refused, naming the two the image ships"
# -----------------------------------------------------------------------------
reset_spies
LOG6="${TMPROOT}/role-unknown.log"
start_boot "${LOG6}" CONTAINER_ROLE=sidecar
wait_boot "${LOG6}" 20 "${DAEMON_UP}"
[ "${BOOT_STATUS}" != "0" ] && pass "an unknown role is fatal" \
  || bad "an unknown role should not have booted"
assert_contains "${LOG6}" "is not a role this image ships" "the reason is explicit"
assert_no_file "${SPY}/worker-start" "and nothing is started meanwhile"

# -----------------------------------------------------------------------------
info ""
info "7. volume unwritable -> capped backoff, no crash loop, heals mid-flight"
# -----------------------------------------------------------------------------
# The executor's identity lives in ALISSA_CONFIG_DIR, so an unavailable volume
# is exactly the auth preflight's "config dir unwritable" class: log and retry
# forever (2026-07-29 lesson), never exit and let the platform restart-loop.
reset_spies
LOG7="${TMPROOT}/volume.log"
UNWRITABLE="${TMPROOT}/novolume"
mkdir -p "${UNWRITABLE}"
chmod 0555 "${UNWRITABLE}"
start_boot "${LOG7}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1 \
                     ALISSA_CONFIG_DIR="${UNWRITABLE}/alissa"
for _ in $(seq 1 20); do
  [ "$(grep -c 'is not writable' "${LOG7}" 2>/dev/null || true)" -ge 2 ] && break
  kill -0 "${BOOT_PID}" 2>/dev/null || break
  sleep 1
done
assert_contains "${LOG7}" "is not writable" "an unavailable volume is diagnosed as such"
assert_not_contains "${LOG7}" "FATAL" "it does NOT crash-loop the service"
assert_contains "${LOG7}" "retrying alissa auth" "it backs off and retries instead"
assert_no_file "${SPY}/bridge-argv" "and no executor registers while the volume is gone"
chmod 0755 "${UNWRITABLE}"
wait_boot "${LOG7}" 30 "${EXECUTOR_UP}"
assert_file "${SPY}/bridge-argv" "restoring the volume is enough — it proceeds, no restart"
assert_contains "${LOG7}" "identity file ${UNWRITABLE}/alissa/bridge-executor.json" \
  "an explicit ALISSA_CONFIG_DIR is honoured for the identity file"

# -----------------------------------------------------------------------------
info ""
info "7b. a stale executor lock on the volume does not wedge the next boot"
# -----------------------------------------------------------------------------
# Persisting ALISSA_CONFIG_DIR also persists the CLI's executor lockfile, and the
# CLI decides that lock is stale with a bare `kill(pid, 0)` — no boot id, no
# start time. A container that died ungracefully leaves the file behind, and the
# next boot's fresh PID namespace can make that dead PID look alive, so the CLI
# refuses to start and the platform restart-loops it.
#
# The lock is planted with a PID that is definitely LIVE (this test's own), which
# is the case the CLI would reject — so this fails if the sweep is removed,
# rather than passing beside it.
reset_spies
LOG7B="${TMPROOT}/stale-lock.log"
LOCK_DIR="${WORKSPACE}/.alissa-config/bridge"
mkdir -p "${LOCK_DIR}"
printf '%s\n' "$$" > "${LOCK_DIR}/executor-revloop-executor.lock"
# A second executor's lock, which this container does not own.
printf '%s\n' "$$" > "${LOCK_DIR}/executor-someone-else.lock"
start_boot "${LOG7B}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1
wait_boot "${LOG7B}" 30 "${EXECUTOR_UP}"
assert_file "${SPY}/bridge-argv" \
  "a stale lock holding a LIVE pid does not stop the boot"
assert_not_contains "${LOG7B}" "is already running on this machine" \
  "...and the CLI never refuses to start (the crash-loop condition)"
assert_eq "${BOOT_STATUS}" "0" "...with a live container, not a non-zero exit"
assert_file "${LOCK_DIR}/executor-someone-else.lock" \
  "...and another id's lock is left alone (never a blanket executor-*.lock glob)"

# -----------------------------------------------------------------------------
info ""
info "8. CLAUDE_CONFIG_DIR off the volume -> warns, still boots"
# -----------------------------------------------------------------------------
reset_spies
LOG8="${TMPROOT}/claudecfg.log"
start_boot "${LOG8}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1 \
                     CLAUDE_CONFIG_DIR="${FAKE_HOME}/.claude"
wait_boot "${LOG8}" 30 "${EXECUTOR_UP}"
assert_contains "${LOG8}" "is not under ${WORKSPACE}" \
  "a claude config dir off the volume is called out"
assert_file "${SPY}/bridge-argv" "...but it is a WARNing, not a refusal"

reset_spies
LOG8B="${TMPROOT}/claudecfg-ok.log"
start_boot "${LOG8B}" CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1 \
                      CLAUDE_CONFIG_DIR="${WORKSPACE}/.claude-config"
wait_boot "${LOG8B}" 30 "${EXECUTOR_UP}"
assert_contains "${LOG8B}" "claude config persisted on the volume" \
  "the image's own CLAUDE_CONFIG_DIR (under the volume) is confirmed, not assumed"

# -----------------------------------------------------------------------------
info ""
info "9. the firewall allowlist really does admit what a job session needs"
# -----------------------------------------------------------------------------
# Asserted against the shipped list itself: init-firewall.sh is sourceable, so
# this reads the same array the root bootstrap resolves — no root, no NET_ADMIN,
# and no second copy of the list to drift.
# shellcheck source=init-firewall.sh
DOMAINS="$(bash -c '. "'"${HERE}"'/init-firewall.sh"; firewall_domains')"
for host in api.alissa.app api.anthropic.com github.com api.github.com \
            share.alissa.app skills.alissa.app; do
  if printf '%s\n' "${DOMAINS}" | grep -qx "${host}"; then
    pass "the egress allowlist admits ${host}"
  else
    bad "a queue job session needs ${host}, and the firewall allowlist omits it"
  fi
done
EXTRA_OUT="$(ALISSA_FIREWALL_EXTRA="ghe.internal" \
  bash -c '. "'"${HERE}"'/init-firewall.sh"; firewall_domains' | tail -n 1)"
assert_eq "${EXTRA_OUT}" "ghe.internal" "ALISSA_FIREWALL_EXTRA still extends the list"

echo
if [ "${fail}" = "0" ]; then
  echo "entrypoint executor-role tests: PASS"
else
  echo "entrypoint executor-role tests: FAIL" >&2
fi
exit "${fail}"
