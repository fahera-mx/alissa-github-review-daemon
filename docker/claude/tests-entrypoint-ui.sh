#!/usr/bin/env bash
# =============================================================================
# Tests for the entrypoint's reviewer-console wiring (#38).
#
# Boots the REAL entrypoint.sh three times — once per console posture — with the
# three CLIs it shells out to (gh / alissa / alissa-revloop) replaced by stubs,
# and the console sidecar left REAL. So this exercises the actual gate, launch,
# supervision and shutdown code paths end to end, without needing a docker
# daemon (CI has no docker-in-docker; the container itself only adds the image
# layers, not different entrypoint behaviour):
#
#   1. console OFF (ALISSA_UI_ENABLED unset) -> boots, NO listener on ${PORT}
#      (and the pass-through-when-unset config contract still holds end to end)
#   2. console ON, no passcode               -> dies at boot, BEFORE the worker
#   3. console ON with a passcode            -> serves 0.0.0.0:${PORT}:
#      /healthz 200 unauthenticated, /api/state 401 without a session, wrong
#      passcode refused, right passcode -> session -> /api/state 200; orderly
#      SIGTERM takes the sidecar down without tripping its "EXITED" alarm.
#   4. console ON, sidecar killed under it -> the monitor logs the death LOUDLY
#      and the daemon + worker keep running (fail-visible, not fail-fatal)
#
# Usage: bash docker/claude/tests-entrypoint-ui.sh
# Needs: curl, jq, python3, and `alissa-revloop-ui` available (installed, or
# runnable from this repo's source tree — see the sidecar resolution below).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
ENTRYPOINT="${HERE}/entrypoint.sh"
SRC_TREE="${REPO_ROOT}/alissa-tools-github-revloop/src/main"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1" >&2; fail=1; }
info() { printf '%s\n' "$*"; }

# assert_contains <file> <pattern> <label>
assert_contains() {
  if grep -qF -- "$2" "$1"; then pass "$3"; else bad "$3 (pattern not in log: $2)"; fi
}
# assert_not_contains <file> <pattern> <label>
assert_not_contains() {
  if grep -qF -- "$2" "$1"; then bad "$3 (unexpected in log: $2)"; else pass "$3"; fi
}
# assert_eq <got> <want> <label>
assert_eq() {
  if [ "$1" = "$2" ]; then pass "$3"; else bad "$3 (got '$1', want '$2')"; fi
}

# -----------------------------------------------------------------------------
# Sandbox: a throwaway HOME + workspace, and stubs for the three CLIs. The stubs
# are only as smart as the entrypoint's own checks (identity preflight, worker
# up/down, a foreground daemon); everything about the console is the real thing.
# -----------------------------------------------------------------------------
TMPROOT="$(mktemp -d)"
BIN="${TMPROOT}/bin"; mkdir -p "${BIN}"
FAKE_HOME="${TMPROOT}/home"; mkdir -p "${FAKE_HOME}/.config/alissa"
WORKSPACE="${TMPROOT}/workspace"; mkdir -p "${WORKSPACE}"
MARKERS="${TMPROOT}/markers"; mkdir -p "${MARKERS}"
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

cat > "${BIN}/alissa" <<STUB
#!/usr/bin/env bash
case "\$1 \$2" in
  "auth login")     ;;
  "worker start")   : > "${MARKERS}/worker-started" ;;
  "worker status")  [ -f "${MARKERS}/worker-started" ] && echo "worker is running" || echo "worker not running" ;;
  "worker stop")    : > "${MARKERS}/worker-stopped" ;;
  "code workspace") ;;
esac
exit 0
STUB

# The daemon: a foreground process the entrypoint backgrounds and kills on TERM.
cat > "${BIN}/alissa-revloop" <<'STUB'
#!/usr/bin/env bash
trap 'exit 0' TERM INT
sleep 600 &
wait $!
STUB

# The console: prefer the installed console script; fall back to running the
# module out of this repo's source tree so the harness works in a checkout that
# has not pip-installed the dist. Either way the sidecar under test is the real
# webui, invoked under the name the entrypoint calls.
# It is linked INTO the stub dir rather than left on PATH: the entrypoint below
# runs with a hermetic env (env -i), so an installation under e.g. ~/.local/bin
# would otherwise be invisible to it and the sidecar would die instantly. For the
# same reason the package's import root is carried over as PYTHONPATH — a
# per-user install resolves through $HOME, which the sandbox replaces.
UI_PYTHONPATH=""
if UI_BIN="$(command -v alissa-revloop-ui 2>/dev/null)"; then
  UI_ORIGIN="installed console script (${UI_BIN})"
  ln -s "${UI_BIN}" "${BIN}/alissa-revloop-ui"
  UI_PYTHONPATH="$(python3 -c 'import os, alissa
print(os.path.dirname(list(alissa.__path__)[0]))' 2>/dev/null || true)"
elif [ -d "${SRC_TREE}" ]; then
  UI_ORIGIN="repo source tree (${SRC_TREE})"
  UI_PYTHONPATH="${SRC_TREE}"
  cat > "${BIN}/alissa-revloop-ui" <<'STUB'
#!/usr/bin/env bash
exec python3 -m alissa.tools.github.revloop.webui "$@"
STUB
else
  printf 'FATAL: no alissa-revloop-ui (install the dist or run from a checkout)\n' >&2
  exit 1
fi
chmod 0755 "${BIN}/gh" "${BIN}/alissa" "${BIN}/alissa-revloop"
[ -L "${BIN}/alissa-revloop-ui" ] || chmod 0755 "${BIN}/alissa-revloop-ui"

PORT_FREE="$(python3 -c 'import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"

# run_entrypoint <logfile> [env assignments...] -> backgrounds it, sets EP_PID.
# Sets a global rather than printing the pid: a pid captured through a command
# substitution belongs to that subshell, and `wait` (scenario 2 needs the exit
# status) only works on a child of THIS shell.
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
    PORT="${PORT_FREE}" \
    ${UI_PYTHONPATH:+PYTHONPATH="${UI_PYTHONPATH}"} \
    "$@" \
    bash "${ENTRYPOINT}" > "${log}" 2>&1 &
  EP_PID=$!
}

# wait_for_log <logfile> <pattern> <seconds>
wait_for_log() {
  local i
  for i in $(seq 1 "$3"); do
    grep -qF -- "$2" "$1" && return 0
    sleep 1
  done
  return 1
}

# stop_entrypoint <pid>: orderly SIGTERM, then wait for it to leave.
stop_entrypoint() {
  local pid="$1" i
  kill -TERM "${pid}" 2>/dev/null || true
  for i in $(seq 1 15); do
    kill -0 "${pid}" 2>/dev/null || return 0
    sleep 1
  done
  kill -KILL "${pid}" 2>/dev/null || true
}

info "reviewer console entrypoint wiring — sidecar from: ${UI_ORIGIN}"
info "using port ${PORT_FREE}"

# -----------------------------------------------------------------------------
info ""
info "1. console OFF by default (ALISSA_UI_ENABLED unset)"
# -----------------------------------------------------------------------------
LOG1="${TMPROOT}/off.log"
rm -f "${MARKERS}"/*
run_entrypoint "${LOG1}"; PID1="${EP_PID}"
wait_for_log "${LOG1}" "alissa worker is running" 30 \
  || bad "entrypoint did not reach the worker-up milestone (see ${LOG1})"
assert_contains "${LOG1}" "reviewer console disabled" "logs the console as disabled"
assert_not_contains "${LOG1}" "starting reviewer console" "does not start the sidecar"
if curl -fsS --max-time 3 "http://127.0.0.1:${PORT_FREE}/healthz" >/dev/null 2>&1; then
  bad "NO listener on \${PORT} when disabled"
else
  pass "no listener on \${PORT} when disabled"
fi
# The pass-through-when-unset contract, end to end through a real boot (the
# renderer's own unit tests are in tests-entrypoint-config.sh).
CFG="${WORKSPACE}/revloop.config.json"
if [ -f "${CFG}" ]; then
  assert_eq "$(jq -r 'has("poll_interval")' "${CFG}")" "false" \
    "generated config omits unset poll_interval (pass-through intact)"
  assert_eq "$(jq -r 'has("round_cap")' "${CFG}")" "false" \
    "generated config omits unset round_cap (pass-through intact)"
  assert_eq "$(jq -r '.on_missing_hub' "${CFG}")" "add" \
    "generated config keeps the structural on_missing_hub"
  assert_eq "$(jq -r 'has("operators")' "${CFG}")" "false" \
    "generated config omits operators when ALISSA_REVIEW_OPERATORS is unset (fails closed)"
else
  bad "entrypoint generated no revloop.config.json"
fi
stop_entrypoint "${PID1}"
assert_contains "${LOG1}" "shutting down" "shuts down on SIGTERM"

# -----------------------------------------------------------------------------
info ""
info "2. console ENABLED without a passcode -> dies at boot"
# -----------------------------------------------------------------------------
LOG2="${TMPROOT}/nopass.log"
rm -f "${MARKERS}"/*
run_entrypoint "${LOG2}" ALISSA_UI_ENABLED=1; PID2="${EP_PID}"
set +e
wait "${PID2}"; rc2=$?
set -e
[ "${rc2}" -ne 0 ] && pass "exits non-zero (${rc2})" || bad "expected a non-zero exit, got 0"
assert_contains "${LOG2}" "FATAL: ALISSA_UI_ENABLED is set but ALISSA_UI_PASSCODE is empty" \
  "fails with the fail-closed passcode message"
if [ -f "${MARKERS}/worker-started" ]; then
  bad "died AFTER starting the worker (the gate must be a preflight)"
else
  pass "dies before any worker/bootstrap work (fail-fast)"
fi

# -----------------------------------------------------------------------------
info ""
info "3. console ENABLED with a passcode -> serves on 0.0.0.0:\${PORT}"
# -----------------------------------------------------------------------------
LOG3="${TMPROOT}/on.log"
JAR="${TMPROOT}/cookies.txt"
PASSCODE='harness-passcode-not-a-secret'
rm -f "${MARKERS}"/*
run_entrypoint "${LOG3}" ALISSA_UI_ENABLED=true ALISSA_UI_PASSCODE="${PASSCODE}"; PID3="${EP_PID}"
wait_for_log "${LOG3}" "starting reviewer console" 30 \
  || bad "entrypoint never started the console (see ${LOG3})"
assert_contains "${LOG3}" "reviewer console ENABLED" "logs the console as enabled"
assert_contains "${LOG3}" "0.0.0.0:${PORT_FREE}" "binds the platform \${PORT} on 0.0.0.0"

up=0
for _ in $(seq 1 30); do
  curl -fsS --max-time 3 "http://127.0.0.1:${PORT_FREE}/healthz" >/dev/null 2>&1 && { up=1; break; }
  sleep 1
done
[ "${up}" = "1" ] && pass "console answers on \${PORT}" || bad "console never came up on \${PORT}"

if [ "${up}" = "1" ]; then
  # /healthz: unauthenticated liveness, the documented healthcheck path.
  code="$(curl -s -o "${TMPROOT}/healthz.json" -w '%{http_code}' \
    "http://127.0.0.1:${PORT_FREE}/healthz")"
  assert_eq "${code}" "200" "GET /healthz -> 200 (no passcode needed)"
  assert_eq "$(jq -r '.ok' "${TMPROOT}/healthz.json")" "true" "/healthz payload is {\"ok\": true, …}"
  assert_eq "$(jq -r 'has("version")' "${TMPROOT}/healthz.json")" "true" "/healthz reports the running version"

  # The passcode really is the gate.
  assert_eq "$(curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:${PORT_FREE}/api/state")" "401" "GET /api/state without a session -> 401"
  assert_eq "$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    --data-urlencode "passcode=wrong-${PASSCODE}" \
    "http://127.0.0.1:${PORT_FREE}/login")" "401" "POST /login with the wrong passcode -> 401 (no session)"
  rm -f "${JAR}"
  assert_eq "$(curl -s -o /dev/null -w '%{http_code}' -X POST -c "${JAR}" \
    --data-urlencode "passcode=${PASSCODE}" \
    "http://127.0.0.1:${PORT_FREE}/login")" "303" "POST /login with the right passcode -> 303 + session cookie"
  assert_eq "$(curl -s -o /dev/null -w '%{http_code}' -b "${JAR}" \
    "http://127.0.0.1:${PORT_FREE}/api/state")" "200" "GET /api/state with the session -> 200"

  # 0.0.0.0, not 127.0.0.1: reachable on a non-loopback address (that is what
  # lets a platform router reach it). Skipped when the host has none.
  hostip="$(python3 -c 'import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("192.0.2.1", 9))
    print(s.getsockname()[0]); s.close()
except Exception:
    print("")')"
  if [ -n "${hostip}" ] && [ "${hostip}" != "127.0.0.1" ]; then
    assert_eq "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
      "http://${hostip}:${PORT_FREE}/healthz")" "200" "reachable off-loopback (${hostip}) -> 0.0.0.0 bind"
  else
    info "  skip non-loopback reachability (no routable address on this host)"
  fi
fi

# Orderly shutdown: the sidecar goes down with the container and the monitor
# does NOT cry "EXITED" (that alarm is for an unexpected death).
stop_entrypoint "${PID3}"
assert_contains "${LOG3}" "shutting down" "shuts down on SIGTERM"
assert_not_contains "${LOG3}" "EXITED" "orderly shutdown does not trip the sidecar alarm"
if curl -fsS --max-time 3 "http://127.0.0.1:${PORT_FREE}/healthz" >/dev/null 2>&1; then
  bad "console still listening after shutdown"
else
  pass "console listener is gone after shutdown"
fi

# -----------------------------------------------------------------------------
info ""
info "4. sidecar killed under the container -> fail-VISIBLE, not fail-fatal"
# -----------------------------------------------------------------------------
LOG4="${TMPROOT}/died.log"
rm -f "${MARKERS}"/*
# ALISSA_REVIEW_OPERATORS rides on THIS boot rather than paying for a fifth one,
# and specifically on a boot that starts a REAL sidecar — that makes it an
# end-to-end pin/library-skew check rather than a renderer check. The daemon is
# stubbed here, so the sidecar is the only component that actually LOADS the
# generated config, and `Config.build` rejects unknown keys fatally: an image
# whose `REVLOOP_VERSION` predates a config key it renders dies at boot, and
# this assertion is what fails instead. Hence the rule for the next person
# adding a config key — bump `ARG REVLOOP_VERSION` in the SAME PR (CI installs
# that pin, falling back to the repo source tree while it is unpublished), and
# this case proves the two moved together. Asserting only that the file was
# rendered would not: `jq` reading back what the entrypoint just wrote says
# nothing about whether the daemon can read it.
run_entrypoint "${LOG4}" ALISSA_UI_ENABLED=on ALISSA_UI_PASSCODE="${PASSCODE}" \
  ALISSA_REVIEW_OPERATORS="RHDZMOTA| ops-bot "; PID4="${EP_PID}"
if wait_for_log "${LOG4}" "starting reviewer console" 30; then
  assert_eq "$(jq -c '.operators' "${WORKSPACE}/revloop.config.json")" \
    '["RHDZMOTA","ops-bot"]' \
    "generated config carries ALISSA_REVIEW_OPERATORS (split, trimmed)"
  ui_pid=""
  for _ in $(seq 1 30); do
    # Match on the argv the entrypoint passes, not the program name: the
    # sidecar may be the installed console script or (in a source checkout) a
    # python -m invocation of the same module.
    ui_pid="$(pgrep -f -- "--host 0.0.0.0 --port ${PORT_FREE}" | head -1 || true)"
    [ -n "${ui_pid}" ] && break
    sleep 1
  done
  if [ -n "${ui_pid}" ]; then
    kill -KILL "${ui_pid}" 2>/dev/null || true
    # The monitor polls the pid every 30s, so allow two poll windows.
    if wait_for_log "${LOG4}" "EXITED" 70; then
      pass "monitor logs the sidecar death loudly"
    else
      bad "sidecar died silently (no EXITED warning within 70s)"
    fi
    assert_not_contains "${LOG4}" "shutting down" "container did NOT tear down with the sidecar"
    kill -0 "${PID4}" 2>/dev/null \
      && pass "entrypoint (daemon + worker) still running" \
      || bad "entrypoint exited when the sidecar died"
    [ -f "${MARKERS}/worker-stopped" ] \
      && bad "worker was stopped when the sidecar died" \
      || pass "worker was left running"
  else
    bad "could not find the sidecar process to kill"
  fi
else
  bad "entrypoint never started the console (see ${LOG4})"
fi
stop_entrypoint "${PID4}"

info ""
if [ "${fail}" = "0" ]; then
  info "All reviewer-console entrypoint checks passed."
else
  info "Some reviewer-console entrypoint checks FAILED." >&2
fi
exit "${fail}"
