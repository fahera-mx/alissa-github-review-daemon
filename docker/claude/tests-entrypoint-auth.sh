#!/usr/bin/env bash
# =============================================================================
# Tests for the entrypoint's `alissa auth login` TRIAGE (issue #62).
#
# The defect: every non-zero exit from `alissa auth login` was reported as
# "ALISSA_API_TOKEN rejected" and exited FATAL, with the command's stderr muted.
# On 2026-07-29 that crash-looped a Railway deploy twice for hours while the
# token was valid both times — the CLI binary (an image-layer file) had vanished
# mid-run, so the login never reached a server at all.
#
# Four classes, and only ONE of them needs a human. This boots the REAL
# entrypoint.sh (CLIs stubbed, no docker needed — CI has no docker-in-docker,
# and the image adds layers, not entrypoint behaviour) and proves each:
#
#   1. CLI absent from PATH  -> re-bootstraps from the installer, logs in, boots
#   2. API unreachable       -> capped exponential backoff, real stderr logged,
#                               proceeds when it comes back, no restart
#   3. config dir unwritable -> same self-healing retry; restoring the mode
#                               mid-flight is enough
#   4. token rejected (401)  -> FATAL, fast, ONE login attempt, names the real
#                               stderr and ALISSA_API_TOKEN rotation
#
# Usage: bash docker/claude/tests-entrypoint-auth.sh
# Needs: bash, python3, jq (the entrypoint's own config rendering uses both).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENTRYPOINT="${HERE}/entrypoint.sh"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1" >&2; fail=1; }
info() { printf '%s\n' "$*"; }

assert_contains() {
  if grep -qF -- "$2" "$1"; then pass "$3"; else bad "$3 (not in log: $2)"; fi
}
assert_not_contains() {
  if grep -qF -- "$2" "$1"; then bad "$3 (unexpected in log: $2)"; else pass "$3"; fi
}
assert_eq() {
  if [ "$1" = "$2" ]; then pass "$3"; else bad "$3 (expected '$2', got '$1')"; fi
}

TMPROOT="$(mktemp -d)"
BIN="${TMPROOT}/bin"; mkdir -p "${BIN}"
FAKE_HOME="${TMPROOT}/home"; mkdir -p "${FAKE_HOME}/.config/alissa"
WORKSPACE="${TMPROOT}/workspace"; mkdir -p "${WORKSPACE}"
# Where the stubs record what they were asked to do, so a test can assert that
# the entrypoint retried (or, for the FATAL class, that it did NOT).
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

# The alissa CLI stub. `auth login` is the component under test's counterpart:
# it counts its invocations and answers according to ALISSA_STUB_LOGIN —
# `ok` (default) or `reject` (a real 401 on stderr, exit 1, stdout untouched,
# exactly as the CLI behaves).
cat > "${BIN}/alissa.real" <<'STUB'
#!/usr/bin/env bash
case "$1 $2" in
  "auth login")
    echo "login" >> "${SPY_DIR}/login-attempts"
    if [ "${ALISSA_STUB_LOGIN:-ok}" = "reject" ]; then
      echo "Error: request failed with HTTP 401 Unauthorized (token is invalid or revoked)" >&2
      exit 1
    fi
    echo "Authenticated."
    exit 0
    ;;
  "worker status") echo "worker is running" ;;
esac
exit 0
STUB
chmod 0755 "${BIN}/gh" "${BIN}/alissa.real"

# The daemon never has to do anything: every assertion here is about boot.
cat > "${BIN}/alissa-revloop" <<'STUB'
#!/usr/bin/env bash
trap 'exit 0' TERM INT
sleep 600 &
wait $!
STUB
chmod 0755 "${BIN}/alissa-revloop"

# curl stub: it serves BOTH surfaces the triage uses, and each is scriptable.
#
#  * the API reachability probe -> exit code from ${SPY_DIR}/api-rc if that file
#    exists (7 = could not connect), consuming one line per call so a test can
#    script "unreachable, unreachable, then up";
#  * the official installer -> prints a script that installs the alissa stub,
#    which is exactly what the real one-liner does (`curl … | bash`).
cat > "${BIN}/curl" <<'STUB'
#!/usr/bin/env bash
url="${@: -1}"
case "${url}" in
  *"/install")
    echo "cp \"${STUB_BIN}/alissa.real\" \"${STUB_BIN}/alissa\"; chmod 0755 \"${STUB_BIN}/alissa\""
    exit 0
    ;;
esac
# Reachability probe.
echo "probe ${url}" >> "${SPY_DIR}/api-probes"
if [ -s "${SPY_DIR}/api-rc" ]; then
  rc="$(head -n 1 "${SPY_DIR}/api-rc")"
  sed -i '1d' "${SPY_DIR}/api-rc"
  if [ "${rc}" != "0" ]; then
    echo "curl: (${rc}) Could not resolve host" >&2
    exit "${rc}"
  fi
fi
exit 0
STUB
chmod 0755 "${BIN}/curl"

# run_boot <logfile> [env assignments...] -> runs to the worker-up milestone (or
# to its death), then stops it. Sets BOOT_STATUS.
BOOT_PID=""
BOOT_STATUS=0
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
    STUB_BIN="${BIN}" \
    ALISSA_AUTH_RETRY_SECONDS=1 \
    ALISSA_AUTH_RETRY_CAP_SECONDS=4 \
    "$@" \
    bash "${ENTRYPOINT}" > "${log}" 2>&1 &
  BOOT_PID=$!
}

# wait_boot <logfile> <max seconds> — until the worker milestone or death.
wait_boot() {
  local log="$1" limit="$2" i
  for i in $(seq 1 "${limit}"); do
    grep -qF "alissa worker is running" "${log}" && break
    kill -0 "${BOOT_PID}" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "${BOOT_PID}" 2>/dev/null; then
    kill -TERM "${BOOT_PID}" 2>/dev/null || true
    BOOT_STATUS=0
  fi
  wait "${BOOT_PID}" 2>/dev/null || BOOT_STATUS=$?
}

reset_spies() {
  rm -f "${SPY}/login-attempts" "${SPY}/api-probes" "${SPY}/api-rc"
  rm -f "${BIN}/alissa"
  # Every case but #1 starts with the CLI present, as a healthy image does.
  if [ "${1:-present}" = "present" ]; then
    cp "${BIN}/alissa.real" "${BIN}/alissa"
    chmod 0755 "${BIN}/alissa"
  fi
}

# -----------------------------------------------------------------------------
info "1. alissa CLI absent -> re-bootstrap from the installer, then log in"
# -----------------------------------------------------------------------------
reset_spies absent
LOG1="${TMPROOT}/missing-cli.log"
start_boot "${LOG1}"
wait_boot "${LOG1}" 40

assert_contains "${LOG1}" "alissa CLI missing from PATH (image-layer loss)" \
  "the missing CLI is named as an image-layer loss, not a token problem"
assert_contains "${LOG1}" "re-bootstrapping from https://share.alissa.app/install" \
  "it re-bootstraps from the official installer"
assert_contains "${LOG1}" "alissa CLI re-bootstrapped" "the re-bootstrap is confirmed"
assert_contains "${LOG1}" "alissa authenticated" "and the login then SUCCEEDS"
assert_not_contains "${LOG1}" "FATAL" "it does NOT exit FATAL (the 2026-07-29 defect)"
assert_contains "${LOG1}" "alissa worker is running" "the boot completes"
assert_eq "${BOOT_STATUS}" "0" "the entrypoint is still alive at the worker milestone"

# -----------------------------------------------------------------------------
info "2. API unreachable -> capped exponential backoff, then proceeds"
# -----------------------------------------------------------------------------
reset_spies
LOG2="${TMPROOT}/unreachable.log"
# Three transport failures (curl 7), then reachable.
printf '7\n7\n7\n0\n' > "${SPY}/api-rc"
start_boot "${LOG2}"
wait_boot "${LOG2}" 60

assert_contains "${LOG2}" "is unreachable at transport level" \
  "an unreachable API is diagnosed as unreachable"
assert_contains "${LOG2}" "Could not resolve host" \
  "curl's REAL stderr is logged, not muted"
assert_not_contains "${LOG2}" "rejected" "it is never called a token rejection"
assert_contains "${LOG2}" "retrying alissa auth in 1s" "the first retry waits the base delay"
assert_contains "${LOG2}" "retrying alissa auth in 2s" "and the delay DOUBLES"
assert_contains "${LOG2}" "retrying alissa auth in 4s" "up to the configured cap"
assert_contains "${LOG2}" "alissa authenticated" "it proceeds once the API comes back"
assert_contains "${LOG2}" "alissa worker is running" "with no container restart"
assert_eq "${BOOT_STATUS}" "0" "and a live entrypoint throughout"
# The timestamps the backoff is claimed from: each retry line reports the total
# waited so far, and it must be monotonically increasing.
waited="$(grep -o 'waited [0-9]*s so far' "${LOG2}" | grep -o '[0-9]*' | tr '\n' ' ')"
assert_eq "${waited}" "0 1 3 " "the cumulative wait is stamped on every retry (0s, 1s, 3s)"

# -----------------------------------------------------------------------------
info "3. config dir unwritable -> retries; restoring it mid-flight is enough"
# -----------------------------------------------------------------------------
reset_spies
LOG3="${TMPROOT}/unwritable.log"
chmod 0555 "${FAKE_HOME}/.config/alissa"
start_boot "${LOG3}"
# Let it fail the probe at least twice, then heal the volume under it.
for _ in $(seq 1 20); do
  # grep -c already prints 0 when it matches nothing; the `|| true` is only
  # there to keep its exit status from tripping `set -e`.
  [ "$(grep -c 'is not writable' "${LOG3}" 2>/dev/null || true)" -ge 2 ] && break
  sleep 1
done
chmod 0755 "${FAKE_HOME}/.config/alissa"
wait_boot "${LOG3}" 40

assert_contains "${LOG3}" "is not writable" "an unwritable config dir is diagnosed as such"
assert_not_contains "${LOG3}" "rejected" "and never as a token rejection"
assert_contains "${LOG3}" "alissa authenticated" "it self-heals once the dir is writable"
assert_eq "${BOOT_STATUS}" "0" "with no restart and no human action"

# -----------------------------------------------------------------------------
info "4. genuine 401 -> FATAL, fast, exactly one attempt, names the remedy"
# -----------------------------------------------------------------------------
reset_spies
LOG4="${TMPROOT}/rejected.log"
start_boot "${LOG4}" ALISSA_STUB_LOGIN=reject
wait_boot "${LOG4}" 30

assert_contains "${LOG4}" "FATAL: ALISSA_API_TOKEN rejected" "a real rejection IS fatal"
assert_contains "${LOG4}" "HTTP 401 Unauthorized" \
  "the login command's ACTUAL stderr is in the message"
assert_contains "${LOG4}" "Rotate ALISSA_API_TOKEN in the Railway service env" \
  "and it names the one remedy a human can apply"
assert_not_contains "${LOG4}" "retrying alissa auth" \
  "no infinite retry for the one class retrying cannot fix"
assert_eq "$(wc -l < "${SPY}/login-attempts")" "1" "the login is attempted exactly once"
if [ "${BOOT_STATUS}" != "0" ]; then
  pass "the entrypoint exited non-zero"
else
  bad "the entrypoint should have exited non-zero on a rejected token"
fi

echo
if [ "${fail}" = "0" ]; then
  echo "entrypoint auth-triage tests: PASS"
else
  echo "entrypoint auth-triage tests: FAIL" >&2
fi
exit "${fail}"
