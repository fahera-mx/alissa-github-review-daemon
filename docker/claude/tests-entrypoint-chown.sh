#!/usr/bin/env bash
# =============================================================================
# Tests for the entrypoint's GUARDED boot-time chown (#78).
#
# The defect: step 0 ran `chown -R alissa:alissa ${WORKSPACE_ROOT}` on EVERY
# boot. The walk stats every inode of every worktree hub on the persistent
# volume, and the dentry/inode slab it fills is charged to the container cgroup
# — a 2026-08-09 audit of the Railway service found memory.current at ~5.98 GB
# against 176 MB of real RSS, 3.71 GB of it slab_reclaimable from this walk.
# On a warm restart the walk fixes nothing: the volume is already alissa-owned.
#
# The contract this pins:
#
#   1. warm boot (probe finds everything alissa-owned) -> NO recursive chown,
#      and the probe stays O(top-level entries): one stat per target, never a
#      `find`, never a depth-2 path
#   2. a root-owned entry among the mount's depth-1 CHILDREN -> full chown runs
#   3. the mount point ITSELF root-owned (the first-boot case) -> full chown runs
#   4. ALISSA_FORCE_CHOWN=1 -> full chown runs with everything alissa-owned, and
#      the probe is skipped entirely (the escape hatch for root-owned files
#      deeper than the probe can see)
#   5. ALISSA_FORCE_CHOWN=0 -> back to the probe (the flag is off by default)
#   6. the probe against the REAL filesystem and the REAL stat, with the outcome
#      derived from the tree's actual owner — so a stub that lies about stat's
#      interface cannot make cases 1-5 pass on their own
#
# HOW (no docker, no root — CI has neither): this boots the REAL entrypoint.sh
# with the commands its root phase shells out to replaced by stubs.
#   * `id -u` answers 0 ONCE, so the root phase runs; the re-exec then sees the
#     real uid, exactly like the post-gosu pass in the container
#   * `gosu` drops its user argument and execs, standing in for the privilege drop
#   * `chown` and `find` record their arguments instead of running (a non-root
#     test cannot chown, and `find` must never be called at all)
#   * `stat` answers scripted ownership per path (cases 1-5) or is the real stat
#     (case 6), and records every invocation so the probe's COST is assertable
#
# Usage: bash docker/claude/tests-entrypoint-chown.sh
# Needs: bash, awk, coreutils.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENTRYPOINT="${HERE}/entrypoint.sh"
REAL_STAT="$(command -v stat)"

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

TMPROOT="$(mktemp -d)"
BIN="${TMPROOT}/bin"; mkdir -p "${BIN}"
FAKE_HOME="${TMPROOT}/home"; mkdir -p "${FAKE_HOME}/.config/alissa"
cp "${HERE}/agents.yaml" "${FAKE_HOME}/.config/alissa/agents.yaml"
cleanup() { rm -rf "${TMPROOT}"; }
trap cleanup EXIT

# --- stubs -------------------------------------------------------------------

# `id -u` is the root gate. Answer 0 exactly once (the pre-drop pass) and defer
# to the real id afterwards, so the script takes the root branch and then
# behaves like the unprivileged re-exec.
cat > "${BIN}/id" <<STUB
#!/usr/bin/env bash
if [ "\${1:-}" = "-u" ] && [ ! -e "\${ROOT_PASS_DONE}" ]; then
  : > "\${ROOT_PASS_DONE}"
  echo 0
  exit 0
fi
exec $(command -v id) "\$@"
STUB

# The privilege drop: `gosu <user> <cmd...>` -> run <cmd...> as who we already are.
cat > "${BIN}/gosu" <<'STUB'
#!/usr/bin/env bash
shift
exec "$@"
STUB

# chown / find never really run here; what matters is WHETHER they were called.
cat > "${BIN}/chown" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${CHOWN_LOG}"
exit 0
STUB
cat > "${BIN}/find" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FIND_LOG}"
exit 0
STUB

# Scripted ownership. FAKE_OWNERS names a TSV of "<path>\t<owner>" lines; any
# path not listed is owned by `alissa`. Unset FAKE_OWNERS => the real stat.
# Every invocation is appended to STAT_LOG, which is how the probe's cost (one
# call per target, depth-1 paths only) becomes an assertion instead of a claim.
cat > "${BIN}/stat" <<STUB
#!/usr/bin/env bash
if [ -z "\${FAKE_OWNERS:-}" ]; then
  exec ${REAL_STAT} "\$@"
fi
printf '%s\n' "\$*" >> "\${STAT_LOG}"
for arg in "\$@"; do
  case "\${arg}" in
    -*|'%U') continue ;;
  esac
  owner="\$(awk -F'\t' -v p="\${arg}" '\$1==p{print \$2; exit}' "\${FAKE_OWNERS}")"
  [ -n "\${owner}" ] || owner=alissa
  printf '%s\n' "\${owner}"
done
exit 0
STUB

# The identities the boot preflights, and the daemon it ends up running. None of
# them is under test here — every assertion is about the root phase — so they
# only have to be credible enough to get past their gates.
cat > "${BIN}/gh" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  "api user -q .login") echo alissa-app ;;
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
cat > "${BIN}/alissa-revloop" <<'STUB'
#!/usr/bin/env bash
trap 'exit 0' TERM INT
sleep 600 &
wait $!
STUB
chmod 0755 "${BIN}"/*

# --- harness -----------------------------------------------------------------

# run_boot <case-name> [env assignments...]
#
# Builds a fresh workspace tree for the case, boots the entrypoint through the
# fake root pass, and stops it once the drop has happened (the marker line
# "role: " is the first thing the unprivileged pass logs, so it proves the root
# phase completed AND that we are past it). Exports, per case:
#   WORKSPACE  the mount point       LOG        the boot log
#   CHOWN_LOG  chown invocations     STAT_LOG   stat invocations
#   FIND_LOG   find invocations
case_workspace() { printf '%s\n' "${TMPROOT}/$1/workspace"; }

run_boot() {
  local name="$1"; shift
  local case_dir="${TMPROOT}/${name}"
  WORKSPACE="$(case_workspace "${name}")"
  LOG="${case_dir}/boot.log"
  CHOWN_LOG="${case_dir}/chown.log"
  STAT_LOG="${case_dir}/stat.log"
  FIND_LOG="${case_dir}/find.log"
  mkdir -p "${WORKSPACE}/fahera-mx" "${WORKSPACE}/.revloop" "${case_dir}/tmux"
  : > "${CHOWN_LOG}"; : > "${STAT_LOG}"; : > "${FIND_LOG}"

  env -i \
    PATH="${BIN}:/usr/local/bin:/usr/bin:/bin" \
    HOME="${FAKE_HOME}" \
    TMUX_TMPDIR="${case_dir}/tmux" \
    ALISSA_WORKSPACE_ROOT="${WORKSPACE}" \
    ROOT_PASS_DONE="${case_dir}/root-pass-done" \
    CHOWN_LOG="${CHOWN_LOG}" \
    STAT_LOG="${STAT_LOG}" \
    FIND_LOG="${FIND_LOG}" \
    GH_TOKEN=stub-gh-token \
    ALISSA_API_TOKEN=stub-alissa-token \
    ALISSA_REVIEW_REPOS="fahera-mx/example-repo" \
    "$@" \
    bash "${ENTRYPOINT}" > "${LOG}" 2>&1 &
  local pid=$! i
  for i in $(seq 1 30); do
    grep -qF "[entrypoint] role: " "${LOG}" && break
    kill -0 "${pid}" 2>/dev/null || break
    sleep 1
  done
  kill -TERM "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
}

# Did the recursive chown of the workspace mount run?
chowned_workspace() { grep -qF -- "-R alissa:alissa ${WORKSPACE}" "${CHOWN_LOG}"; }

# --- 1. warm boot: the whole volume is already alissa-owned ------------------
info "1. warm boot (everything alissa-owned) -> no recursive walk"
: > "${TMPROOT}/owners-warm.tsv"
run_boot warm FAKE_OWNERS="${TMPROOT}/owners-warm.tsv"
if chowned_workspace; then bad "the recursive chown must NOT run on a warm boot"
else pass "no recursive chown of the workspace mount"; fi
assert_contains "${LOG}" "already owned by alissa — skipping the recursive chown" \
  "the log says the walk was skipped, and how to force it"
if [ ! -s "${FIND_LOG}" ]; then pass "no \`find\` sweep (it would stat every inode too)"
else bad "the probe shelled out to find: $(cat "${FIND_LOG}")"; fi
# O(top-level entries): one stat call per target, and nothing below depth 1.
if [ "$(wc -l < "${STAT_LOG}")" = "2" ]; then
  pass "the probe cost exactly one stat call per target (workspace + tmux dir)"
else
  bad "expected 2 stat calls, got $(wc -l < "${STAT_LOG}"): $(cat "${STAT_LOG}")"
fi
if grep -qF -- "${WORKSPACE}/fahera-mx/" "${STAT_LOG}"; then
  bad "the probe descended past depth 1 (that is the walk it exists to avoid)"
else
  pass "the probe never looked below the mount's immediate children"
fi
assert_contains "${STAT_LOG}" "${WORKSPACE}/.revloop" \
  "...but it DID probe the depth-1 children, dotted ones included"

# --- 2. a root-owned child of the mount --------------------------------------
info ""
info "2. a depth-1 child is root-owned -> the full chown runs"
printf '%s\t%s\n' "$(case_workspace child-root)/fahera-mx" root > "${TMPROOT}/owners-child.tsv"
run_boot child-root FAKE_OWNERS="${TMPROOT}/owners-child.tsv"
if chowned_workspace; then pass "the recursive chown of the mount ran"
else bad "a root-owned child must trigger the full chown"; fi
assert_contains "${LOG}" "probe found entries not owned by alissa" \
  "the log says why the walk ran"

# --- 3. the mount point itself is root-owned (first boot) --------------------
info ""
info "3. the mount point itself is root-owned (fresh volume) -> the full chown runs"
printf '%s\t%s\n' "$(case_workspace mount-root)" root > "${TMPROOT}/owners-mount.tsv"
run_boot mount-root FAKE_OWNERS="${TMPROOT}/owners-mount.tsv"
if chowned_workspace; then pass "first-boot behaviour is unchanged: the full chown still runs"
else bad "a root-owned mount point must trigger the full chown"; fi

# --- 4. the escape hatch -----------------------------------------------------
info ""
info "4. ALISSA_FORCE_CHOWN=1 -> the walk runs even though the probe would skip it"
: > "${TMPROOT}/owners-force.tsv"
run_boot force FAKE_OWNERS="${TMPROOT}/owners-force.tsv" ALISSA_FORCE_CHOWN=1
if chowned_workspace; then pass "the recursive chown ran on an alissa-owned tree"
else bad "ALISSA_FORCE_CHOWN=1 must force the walk"; fi
assert_contains "${LOG}" "ALISSA_FORCE_CHOWN=1 — forcing the full recursive chown" \
  "the log announces the forced walk"
assert_contains "${LOG}" "(forced)" "...and attributes the walk to the flag, not the probe"
if [ ! -s "${STAT_LOG}" ]; then pass "the probe is skipped entirely when forced"
else bad "forced boot still probed: $(cat "${STAT_LOG}")"; fi

# --- 5. the flag is off by default -------------------------------------------
info ""
info "5. ALISSA_FORCE_CHOWN=0 -> back to the probe"
: > "${TMPROOT}/owners-off.tsv"
run_boot force-off FAKE_OWNERS="${TMPROOT}/owners-off.tsv" ALISSA_FORCE_CHOWN=0
if chowned_workspace; then bad "ALISSA_FORCE_CHOWN=0 must not force the walk"
else pass "no recursive chown"; fi
assert_not_contains "${LOG}" "forcing the full recursive chown" "no forced-walk banner"

# --- 6. the real stat, against the real filesystem ---------------------------
info ""
info "6. real stat, real tree -> the outcome follows the tree's actual owner"
run_boot real-stat
REAL_OWNER="$(${REAL_STAT} -c '%U' "${WORKSPACE}")"
if [ "${REAL_OWNER}" = "alissa" ]; then
  # The container this repo builds runs as `alissa`, so a local run lands here.
  if chowned_workspace; then bad "a genuinely alissa-owned tree must skip the walk"
  else pass "tree really owned by alissa -> skipped"; fi
else
  # CI runs as `runner`; every entry is foreign, which is the first-boot shape.
  if chowned_workspace; then pass "tree really owned by ${REAL_OWNER} (not alissa) -> walked"
  else bad "a tree owned by ${REAL_OWNER} must trigger the walk"; fi
fi

info ""
[ "${fail}" = "0" ] && { echo "ALL PASS"; exit 0; } || { echo "FAILURES"; exit 1; }
