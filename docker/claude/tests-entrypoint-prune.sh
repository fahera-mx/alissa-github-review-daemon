#!/usr/bin/env bash
# =============================================================================
# Tests for the entrypoint's workspace-prune wiring (issue #81).
#
# Nothing in this container used to remove anything from the volume, so the
# entrypoint now runs `alissa code workspace prune` at boot and on a supervised
# interval. This boots the REAL entrypoint.sh six times with the CLIs it shells
# out to replaced by stubs (no docker needed — CI has no docker-in-docker, and
# the image adds layers, not entrypoint behaviour). The `alissa` stub records
# every prune invocation with its argv AND its cwd, which is what makes the
# safety properties assertions rather than claims:
#
#   1. default ON, capable CLI   -> boot pass runs from the workspace root, the
#                                   interval loop keeps ticking (a FAILED tick
#                                   warns and the next one still runs), no
#                                   --force and no --min-age-hours ever, and
#                                   SIGTERM stops the loop for good
#   2. ALISSA_WORKSPACE_PRUNE=0  -> zero invocations, no loop, boot unaffected
#   3. CLI without the subcommand-> EXACTLY ONE loud warning, zero invocations,
#                                   and the container still boots. The stub
#                                   reproduces commander's real behaviour here:
#                                   an unknown subcommand's `--help` prints the
#                                   PARENT help and exits 0 (alissa CLI 0.1.0),
#                                   so a probe reading only the exit status
#                                   would call this CLI capable
#   4. min-age + garbage interval-> --min-age-hours passed through verbatim; a
#                                   non-numeric interval falls back to 360
#   5. garbage min-age           -> fails CLOSED (prune skipped, loud warning):
#                                   the CLI's default age gate may be SHORTER
#                                   than the one that was asked for
#   6. executor role             -> boot pass yes, interval loop no (the role
#                                   `exec`s the bridge; a loop would outlive the
#                                   only code that stops it)
#   7. a HANGING prune           -> `timeout` cuts it, the boot continues to the
#                                   worker, and the warning names the knob. The
#                                   boot hook runs BEFORE the worker starts, so
#                                   an unbounded pass delays every boot
#
# Usage: bash docker/claude/tests-entrypoint-prune.sh
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
assert_eq() { if [ "$1" = "$2" ]; then pass "$3"; else bad "$3 (expected '$2', got '$1')"; fi; }

TMPROOT="$(mktemp -d)"
BIN="${TMPROOT}/bin"; mkdir -p "${BIN}"
FAKE_HOME="${TMPROOT}/home"; mkdir -p "${FAKE_HOME}/.config/alissa"
WORKSPACE="${TMPROOT}/workspace"; mkdir -p "${WORKSPACE}"
SPY="${TMPROOT}/spy"; mkdir -p "${SPY}"
cp "${HERE}/agents.yaml" "${FAKE_HOME}/.config/alissa/agents.yaml"
cleanup() { rm -rf "${TMPROOT}"; }
trap cleanup EXIT

cat > "${BIN}/gh" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  "api user -q .login") echo alissa-app ;;
esac
exit 0
STUB

# The API reachability probe. Always up: the transport classes have their own
# suite (tests-entrypoint-auth.sh).
cat > "${BIN}/curl" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

# The alissa CLI stub.
#
# PRUNE_CAPABLE=0 turns it into a CLI that predates the subcommand — and it
# models that faithfully, which is the whole point of case 3: commander prints
# the PARENT command's help and exits 0 for an unknown subcommand, so `prune
# --help` SUCCEEDS on a CLI that has never heard of prune.
#
# PRUNE_FAIL_ON=<n> makes the n-th prune invocation fail, so the interval loop's
# tolerance of a failing pass is exercised rather than assumed. PRUNE_HANG makes
# it block forever, which is what the boot pass's `timeout` bound exists for.
cat > "${BIN}/alissa" <<'STUB'
#!/usr/bin/env bash
case "${1:-} ${2:-} ${3:-}" in
  "code workspace prune")
    shift 3
    if [ "${1:-}" = "--help" ]; then
      if [ "${PRUNE_CAPABLE:-1}" = "1" ]; then
        cat <<'HELP'
Usage: alissa code workspace prune [options]

Remove worktrees of finished branches from this workspace's hubs

Options:
  --min-age-hours <n>  only prune worktrees older than <n> hours
  --force              prune even a dirty worktree
  -h, --help           display help for command
HELP
        exit 0
      fi
      # alissa CLI 0.1.0, verbatim in shape: the `workspace` help, exit 0, no
      # mention of prune anywhere.
      cat <<'HELP'
Usage: alissa code workspace [options] [command]

Create and maintain an Alissa Code Workspace (multi-repo worktree hubs)

Options:
  -h, --help            display help for command

Commands:
  init [options] [dir]  Initialize a workspace here (or in <dir>)
  add [options] <repo>  Hub-ify a repo into this workspace
  sync [options]        Reconcile the workspace against its manifest
  status                Show the workspace binding, hubs, and active worktrees
  doctor                Check workspace invariants
  help [command]        display help for command
HELP
      exit 0
    fi
    printf 'cwd=%s argv=%s\n' "${PWD}" "$*" >> "${SPY_DIR}/prune-argv"
    # PRUNE_HANG models the slow first sweep (or a prompt on a tty-less
    # container): the entrypoint's `timeout` is the only thing that ends it.
    [ -n "${PRUNE_HANG:-}" ] && sleep 120
    n="$(wc -l < "${SPY_DIR}/prune-argv" | tr -d ' ')"
    echo "keep  main (never pruned)"
    echo "prune TASK-1-EXAMPLE (branch merged, PR closed)"
    if [ -n "${PRUNE_FAIL_ON:-}" ] && [ "${n}" = "${PRUNE_FAIL_ON}" ]; then
      echo "error: hub sweep failed" >&2
      exit 3
    fi
    exit 0
    ;;
esac
case "${1:-} ${2:-}" in
  "auth login")     echo "Authenticated." ;;
  "worker start")   : > "${SPY_DIR}/worker-started" ;;
  "worker status")  [ -f "${SPY_DIR}/worker-started" ] && echo "worker is running" || echo "worker not running" ;;
  "worker stop")    : > "${SPY_DIR}/worker-stopped" ;;
  "code workspace") ;;
  "bridge start")
    : > "${SPY_DIR}/bridge-started"
    trap 'exit 0' TERM INT
    sleep 600 &
    wait $!
    ;;
esac
exit 0
STUB

# The review daemon: a foreground process the entrypoint backgrounds.
cat > "${BIN}/alissa-revloop" <<'STUB'
#!/usr/bin/env bash
trap 'exit 0' TERM INT
sleep 600 &
wait $!
STUB
chmod 0755 "${BIN}/gh" "${BIN}/curl" "${BIN}/alissa" "${BIN}/alissa-revloop"

reset_spies() {
  rm -f "${SPY}"/prune-argv "${SPY}"/worker-started "${SPY}"/worker-stopped \
        "${SPY}"/bridge-started
}

prune_count() { [ -f "${SPY}/prune-argv" ] && wc -l < "${SPY}/prune-argv" | tr -d ' ' || echo 0; }

EP_PID=""
run_entrypoint() {
  local log="$1"; shift
  env -i \
    PATH="${BIN}:/usr/local/bin:/usr/bin:/bin" \
    HOME="${FAKE_HOME}" \
    TMUX_TMPDIR="${TMPROOT}/tmux" \
    ALISSA_WORKSPACE_ROOT="${WORKSPACE}" \
    GH_TOKEN=stub-gh-token \
    ALISSA_API_TOKEN=stub-alissa-token \
    ALISSA_REVIEW_REPOS="fahera-mx/example-repo" \
    SPY_DIR="${SPY}" \
    "$@" \
    bash "${ENTRYPOINT}" > "${log}" 2>&1 &
  EP_PID=$!
}

wait_for_log() {
  local i
  for i in $(seq 1 "$3"); do
    grep -qF -- "$2" "$1" && return 0
    kill -0 "${EP_PID}" 2>/dev/null || { grep -qF -- "$2" "$1" && return 0; return 1; }
    sleep 1
  done
  return 1
}

# wait_for_prune_count <n> <seconds>
wait_for_prune_count() {
  local i
  for i in $(seq 1 "$2"); do
    [ "$(prune_count)" -ge "$1" ] && return 0
    sleep 1
  done
  return 1
}

stop_entrypoint() {
  local pid="$1" i
  kill -TERM "${pid}" 2>/dev/null || true
  for i in $(seq 1 15); do
    kill -0 "${pid}" 2>/dev/null || return 0
    sleep 1
  done
  kill -KILL "${pid}" 2>/dev/null || true
}

DAEMON_UP="alissa worker is running"

# -----------------------------------------------------------------------------
info "1. default ON with a capable CLI -> boot pass + a supervised interval loop"
# -----------------------------------------------------------------------------
reset_spies
LOG1="${TMPROOT}/on.log"
# 3s ticks via the test-only seconds override, and the SECOND pass fails, so the
# assertions below cover both "the loop ticks" and "a failed tick is survivable".
run_entrypoint "${LOG1}" \
  ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS=3 \
  PRUNE_FAIL_ON=2
PID1="${EP_PID}"
wait_for_log "${LOG1}" "${DAEMON_UP}" 40 || bad "entrypoint never reached the worker-up milestone (see ${LOG1})"

assert_contains "${LOG1}" "workspace prune ENABLED" "prune is on by default (no env set)"
assert_contains "${LOG1}" "prune[boot]: prune TASK-1-EXAMPLE" "the CLI's per-worktree verdicts land in the boot log"
assert_contains "${LOG1}" "workspace prune (boot) finished" "the boot pass reports its outcome"
assert_contains "${LOG1}" "starting the workspace prune loop" "the interval loop is started"

if [ "$(prune_count)" -ge 1 ]; then
  pass "boot pass invoked the CLI"
  assert_eq "$(head -1 "${SPY}/prune-argv" | sed 's/ argv=.*//')" "cwd=${WORKSPACE}" \
    "prune runs from the workspace root (the binding is cwd-based)"
  assert_eq "$(head -1 "${SPY}/prune-argv" | sed 's/^.* argv=//')" "" \
    "boot pass passes NO flags when neither knob is set (CLI defaults apply)"
else
  bad "boot pass never invoked the CLI"
fi

# The loop: three passes at 3s ticks, the second of which fails.
if wait_for_prune_count 3 40; then
  pass "the interval loop ticks (>=3 passes)"
  assert_contains "${LOG1}" "workspace prune (interval) exited 3" "a failed interval pass warns"
  assert_contains "${LOG1}" "prune[interval]:" "interval verdicts are logged too"
  kill -0 "${PID1}" 2>/dev/null \
    && pass "the container survives a failed prune" \
    || bad "the entrypoint died on a failed prune"
else
  bad "the interval loop did not tick 3 times within 40s (count=$(prune_count))"
fi

assert_not_contains "${SPY}/prune-argv" "--force" "--force is NEVER passed"

# Teardown: the loop must die with the container, not keep pruning a volume
# nothing is using.
stop_entrypoint "${PID1}"
assert_contains "${LOG1}" "shutting down" "shuts down on SIGTERM"
AFTER="$(prune_count)"
sleep 8   # >2 ticks at the 3s override
assert_eq "$(prune_count)" "${AFTER}" "no prune pass runs after shutdown (the loop is torn down)"

# -----------------------------------------------------------------------------
info ""
info "2. ALISSA_WORKSPACE_PRUNE=0 -> the opt-out really is one"
# -----------------------------------------------------------------------------
reset_spies
LOG2="${TMPROOT}/off.log"
run_entrypoint "${LOG2}" ALISSA_WORKSPACE_PRUNE=0 ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS=3
PID2="${EP_PID}"
wait_for_log "${LOG2}" "${DAEMON_UP}" 40 || bad "entrypoint never reached the worker-up milestone (see ${LOG2})"
assert_contains "${LOG2}" "workspace prune DISABLED" "logs the opt-out"
assert_not_contains "${LOG2}" "starting the workspace prune loop" "no interval loop when opted out"
sleep 6
assert_eq "$(prune_count)" "0" "zero prune invocations when opted out"
stop_entrypoint "${PID2}"

# -----------------------------------------------------------------------------
info ""
info "3. a CLI that predates the subcommand -> ONE loud warning, both hooks skip"
# -----------------------------------------------------------------------------
reset_spies
LOG3="${TMPROOT}/old-cli.log"
run_entrypoint "${LOG3}" PRUNE_CAPABLE=0 ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS=3
PID3="${EP_PID}"
wait_for_log "${LOG3}" "${DAEMON_UP}" 40 || bad "entrypoint never reached the worker-up milestone (see ${LOG3})"
assert_eq "$(grep -c "predates 'alissa code workspace prune'" "${LOG3}")" "1" \
  "exactly ONE warning about the missing subcommand"
assert_contains "${LOG3}" "WARN: this alissa CLI predates" "and it is a WARN, not a whisper"
assert_not_contains "${LOG3}" "starting the workspace prune loop" "no interval loop on an old CLI"
assert_not_contains "${LOG3}" "workspace prune ENABLED" "the feature is not reported as enabled"
sleep 6
assert_eq "$(prune_count)" "0" "zero prune invocations on an old CLI"
kill -0 "${PID3}" 2>/dev/null \
  && pass "the container boots normally on an old CLI" \
  || bad "an old CLI broke the boot"
stop_entrypoint "${PID3}"

# -----------------------------------------------------------------------------
info ""
info "4. min-age pass-through + a garbage interval"
# -----------------------------------------------------------------------------
reset_spies
LOG4="${TMPROOT}/knobs.log"
run_entrypoint "${LOG4}" \
  ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS=48 \
  ALISSA_WORKSPACE_PRUNE_INTERVAL_MINUTES=soon \
  ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS=3
PID4="${EP_PID}"
wait_for_log "${LOG4}" "${DAEMON_UP}" 40 || bad "entrypoint never reached the worker-up milestone (see ${LOG4})"
if [ "$(prune_count)" -ge 1 ]; then
  assert_eq "$(head -1 "${SPY}/prune-argv" | sed 's/^.* argv=//')" "--min-age-hours 48" \
    "ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS is passed through as --min-age-hours"
else
  bad "no prune invocation to inspect"
fi
assert_contains "${LOG4}" "ALISSA_WORKSPACE_PRUNE_INTERVAL_MINUTES=soon is not a positive whole number" \
  "a garbage interval warns"
assert_contains "${LOG4}" "starting the workspace prune loop (every 360 min" \
  "a garbage interval falls back to 360 (an interval is not a safety knob)"
# The INTERVAL pass must carry the same flags as the boot pass — PRUNE_ARGS is a
# global the loop's subshell inherits, and nothing else pins that.
if wait_for_prune_count 2 30; then
  assert_eq "$(sed -n 2p "${SPY}/prune-argv" | sed 's/^.* argv=//')" "--min-age-hours 48" \
    "the interval pass inherits --min-age-hours too, not just the boot pass"
else
  bad "no interval pass observed within 30s (count=$(prune_count))"
fi
stop_entrypoint "${PID4}"

# -----------------------------------------------------------------------------
info ""
info "5. a garbage min-age fails CLOSED (the age gate is a safety knob)"
# -----------------------------------------------------------------------------
reset_spies
LOG5="${TMPROOT}/bad-age.log"
run_entrypoint "${LOG5}" \
  ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS=48h \
  ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS=3
PID5="${EP_PID}"
wait_for_log "${LOG5}" "${DAEMON_UP}" 40 || bad "entrypoint never reached the worker-up milestone (see ${LOG5})"
assert_contains "${LOG5}" "ALISSA_WORKSPACE_PRUNE_MIN_AGE_HOURS=48h is not a whole number of hours" \
  "a garbage min-age warns"
assert_not_contains "${LOG5}" "starting the workspace prune loop" "and disables the loop"
sleep 6
assert_eq "$(prune_count)" "0" "and runs no pass at all rather than one with a shorter age gate"
kill -0 "${PID5}" 2>/dev/null \
  && pass "the container still boots" \
  || bad "a garbage min-age broke the boot"
stop_entrypoint "${PID5}"

# -----------------------------------------------------------------------------
info ""
info "6. executor role -> boot pass, but no interval loop"
# -----------------------------------------------------------------------------
reset_spies
LOG6="${TMPROOT}/executor.log"
run_entrypoint "${LOG6}" \
  CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1 \
  ALISSA_WORKSPACE_PRUNE_INTERVAL_SECONDS=3
PID6="${EP_PID}"
wait_for_log "${LOG6}" "starting alissa bridge start" 40 \
  || bad "the executor never reached the bridge (see ${LOG6})"
assert_eq "$(prune_count)" "1" "the executor prunes once at boot (it grows the same volume)"
assert_contains "${LOG6}" "boot pass only (executor role" "the ENABLED line says boot-pass-only for this role"
assert_contains "${LOG6}" "nothing left here to tear a loop down" "and says why there is no loop"
assert_not_contains "${LOG6}" "starting the workspace prune loop" "no interval loop in the executor role"
assert_not_contains "${LOG6}" "then every" "the executor's log announces no cadence it will not run"
sleep 8   # >2 ticks at the 3s override: an orphaned loop would show up here
assert_eq "$(prune_count)" "1" "no loop survived the exec into the bridge"
stop_entrypoint "${PID6}"

# -----------------------------------------------------------------------------
info ""
info "7. a HANGING prune cannot hold the boot open"
# -----------------------------------------------------------------------------
reset_spies
LOG7="${TMPROOT}/hang.log"
# The boot hook runs before the worker starts, so an unbounded pass would delay
# every boot by however long the CLI takes — and on a never-pruned volume the
# first `git gc --auto` sweep is exactly that case. PRUNE_HANG makes the stub
# sleep for two minutes; a 2s timeout must cut it and let the boot continue.
run_entrypoint "${LOG7}" PRUNE_HANG=1 ALISSA_WORKSPACE_PRUNE_TIMEOUT_SECONDS=2
PID7="${EP_PID}"
if wait_for_log "${LOG7}" "${DAEMON_UP}" 40; then
  pass "the boot reaches the worker even though the prune pass hangs"
else
  bad "a hanging prune pass held the boot open (see ${LOG7})"
fi
assert_contains "${LOG7}" "workspace prune (boot) TIMED OUT after 2s" \
  "the timeout is reported as a timeout, not as a bare exit code"
assert_contains "${LOG7}" "ALISSA_WORKSPACE_PRUNE_TIMEOUT_SECONDS" \
  "and names the knob that raises the bound"
kill -0 "${PID7}" 2>/dev/null \
  && pass "the container is alive after a timed-out pass" \
  || bad "a timed-out prune killed the container"
stop_entrypoint "${PID7}"

info ""
if [ "${fail}" = "0" ]; then
  info "All workspace-prune entrypoint checks passed."
else
  info "Some workspace-prune entrypoint checks FAILED." >&2
fi
exit "${fail}"
