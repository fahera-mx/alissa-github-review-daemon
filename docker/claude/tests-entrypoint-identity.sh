#!/usr/bin/env bash
# =============================================================================
# Tests for the entrypoint's REVIEWER-identity preflight (#51).
#
# The defect: this container holds more than one GitHub identity, and `gh` uses
# whichever token it inherited — so a review verdict posted from here landed
# under the IMPLEMENTER's login (studio #298/#302 round 1). It was not the
# round's verdict of record, it did not consume the PR's review request, and
# nothing said so.
#
# ALISSA_REVIEWER_TOKEN_ENV names the variable carrying the REVIEWER's token.
# This boots the REAL entrypoint.sh (CLIs stubbed, no docker needed — CI has no
# docker-in-docker, and the image adds layers, not entrypoint behaviour) and
# proves the boot gate:
#
#   1. token env set, distinct identity -> resolves and LOGS the reviewer login,
#      and says the two identities differ
#   2. token env names an EMPTY variable -> dies at boot (never falls back to the
#      inherited credential)
#   3. ALISSA_REVIEWER_LOGIN disagrees with the token -> dies at boot
#   4. token env unset                   -> boots, but WARNS that verdicts will
#      use the inherited credential
#
# Usage: bash docker/claude/tests-entrypoint-identity.sh
# Needs: bash, python3 (the entrypoint's own config seeding uses it).
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

TMPROOT="$(mktemp -d)"
BIN="${TMPROOT}/bin"; mkdir -p "${BIN}"
FAKE_HOME="${TMPROOT}/home"; mkdir -p "${FAKE_HOME}/.config/alissa"
WORKSPACE="${TMPROOT}/workspace"; mkdir -p "${WORKSPACE}"
cp "${HERE}/agents.yaml" "${FAKE_HOME}/.config/alissa/agents.yaml"
cleanup() { rm -rf "${TMPROOT}"; }
trap cleanup EXIT

# The stub that makes this a real test: `gh api user` answers with the login
# that OWNS the token it was called with, exactly as GitHub does. So a call
# carrying the inherited credential and a call carrying the reviewer's token
# return DIFFERENT logins, and the entrypoint has to route the right one.
cat > "${BIN}/gh" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  "api user -q .login")
    case "${GH_TOKEN:-}" in
      reviewer-token) echo alissa-app ;;
      "")             exit 1 ;;
      *)              echo RHDZMOTA ;;   # the container's default = the DEV identity
    esac
    ;;
esac
exit 0
STUB

cat > "${BIN}/alissa" <<'STUB'
#!/usr/bin/env bash
case "$1 $2" in
  "worker status") echo "worker is running" ;;
esac
exit 0
STUB

# The daemon never has to do anything: every assertion here is about boot.
cat > "${BIN}/alissa-revloop" <<'STUB'
#!/usr/bin/env bash
trap 'exit 0' TERM INT
sleep 600 &
wait $!
STUB
chmod 0755 "${BIN}/gh" "${BIN}/alissa" "${BIN}/alissa-revloop"

# run_boot <logfile> [env assignments...] -> runs to the worker-up milestone (or
# to its death), then stops it. Sets BOOT_STATUS.
BOOT_STATUS=0
run_boot() {
  local log="$1"; shift
  env -i \
    PATH="${BIN}:/usr/local/bin:/usr/bin:/bin" \
    HOME="${FAKE_HOME}" \
    TMUX_TMPDIR="${TMPROOT}/tmux" \
    ALISSA_WORKSPACE_ROOT="${WORKSPACE}" \
    GH_TOKEN=dev-default-token \
    ALISSA_API_TOKEN=stub-alissa-token \
    ALISSA_REVIEW_REPOS="fahera-mx/example-repo" \
    "$@" \
    bash "${ENTRYPOINT}" > "${log}" 2>&1 &
  local pid=$!
  local i
  for i in $(seq 1 30); do
    grep -qF "alissa worker is running" "${log}" && break
    kill -0 "${pid}" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "${pid}" 2>/dev/null; then
    kill -TERM "${pid}" 2>/dev/null || true
    BOOT_STATUS=0
  fi
  wait "${pid}" 2>/dev/null || BOOT_STATUS=$?
}

info "1. reviewer token env set, distinct from the container default"
LOG1="${TMPROOT}/routed.log"
run_boot "${LOG1}" ALISSA_REVIEWER_TOKEN_ENV=REVLOOP_REVIEWER_GH_TOKEN \
                   REVLOOP_REVIEWER_GH_TOKEN=reviewer-token
assert_contains "${LOG1}" "gh authenticated as: RHDZMOTA" \
  "the inherited credential is still the DEV identity"
assert_contains "${LOG1}" "reviewer identity: alissa-app" \
  "the reviewer login is resolved from the named variable and LOGGED"
assert_contains "${LOG1}" "are DIFFERENT identities" \
  "the boot log says the two identities differ"
assert_contains "${WORKSPACE}/revloop.config.json" \
  '"reviewer_token_env": "REVLOOP_REVIEWER_GH_TOKEN"' \
  "the generated config carries the variable NAME"
assert_not_contains "${WORKSPACE}/revloop.config.json" "reviewer-token" \
  "...and never the token value"
assert_not_contains "${LOG1}" "reviewer-token" \
  "the token VALUE never reaches the log"

info ""
info "2. the named variable is empty -> die, never fall back"
LOG2="${TMPROOT}/empty.log"
run_boot "${LOG2}" ALISSA_REVIEWER_TOKEN_ENV=REVLOOP_REVIEWER_GH_TOKEN
[ "${BOOT_STATUS}" != "0" ] && pass "boot failed" || bad "boot should have failed"
assert_contains "${LOG2}" "but that variable is empty" "the reason names the variable"

info ""
info "3. ALISSA_REVIEWER_LOGIN disagrees with the token -> die"
LOG3="${TMPROOT}/mismatch.log"
run_boot "${LOG3}" ALISSA_REVIEWER_TOKEN_ENV=REVLOOP_REVIEWER_GH_TOKEN \
                   REVLOOP_REVIEWER_GH_TOKEN=reviewer-token \
                   ALISSA_REVIEWER_LOGIN=someone-else
[ "${BOOT_STATUS}" != "0" ] && pass "boot failed" || bad "boot should have failed"
assert_contains "${LOG3}" "not a verdict of record" "the reason says why it matters"

info ""
info "4. no reviewer token env -> boots, but warns"
LOG4="${TMPROOT}/inherited.log"
run_boot "${LOG4}"
assert_contains "${LOG4}" "ALISSA_REVIEWER_TOKEN_ENV unset" "the warning fires"
assert_contains "${LOG4}" "alissa worker is running" "...and the daemon still boots"

info ""
[ "${fail}" = "0" ] && { echo "ALL PASS"; exit 0; } || { echo "FAILURES"; exit 1; }
