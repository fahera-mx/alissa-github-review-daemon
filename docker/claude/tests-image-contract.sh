#!/usr/bin/env bash
# =============================================================================
# Image-contract check for docker/claude — builds the image, then asserts the
# contract INSIDE the result.
#
# WHY THIS EXISTS. This Dockerfile is a thin leaf on a shared base image
# (ghcr.io/ali-fhr/alissa-loopwork-base). Almost everything the container needs
# at runtime — python, node, claude-code, the alissa CLI, gh, tmux, the `alissa`
# user, the system git rewrites, the tini ENTRYPOINT hook — is INHERITED, and
# inherited things break silently: nothing in this repo changes, no test goes
# red, and the first symptom is a container that will not boot in production.
# The single `FROM` line is the only review surface those layers have left, so
# the contract they satisfy has to be asserted somewhere that runs.
#
# The `tests-entrypoint-*.sh` suites cannot do it. They deliberately boot the
# entrypoint against STUBBED CLIs in a plain shell, because CI has no docker;
# that is the right trade for testing entrypoint LOGIC, and it is exactly why
# they say nothing about whether the image the logic runs in was assembled
# correctly. This file is the other half: it makes no claim about behaviour, only
# about the artifact.
#
# WHAT IT PINS — the Acceptance list of issue #95, mechanically rather than by
# eye:
#
#   1. the build itself succeeds
#   2. `alissa-revloop`, `alissa-revloop-ui`, `claude`, `alissa`, `gh`, `tmux`,
#      `codex`, `pi` all resolvable on PATH. The last two are base-contract only:
#      this daemon never spawns them (base 0.2.0 made the base multi-agent), and
#      they are asserted precisely BECAUSE nothing here would notice them going
#      missing — which is the same reason every other inherited item is listed.
#   3. `alissa` is uid 1000 / gid 1000
#   4. BOTH GitHub SSH->HTTPS rewrites, the gh credential helper, and
#      advice.detachedHead=false are present system-wide
#   5. claude's first-run gates are pre-seeded
#   6. the per-daemon git author identity resolves FOR THE alissa USER
#   7. the baked ARG->ENV knob defaults are unchanged
#   8. /usr/local/bin/entrypoint.sh is THIS repo's file and not the base's stub
#   9. the inherited image config (ENTRYPOINT / USER / WORKDIR / EXPOSE) is what
#      the leaf assumes when it declines to re-declare any of it
#  10. the installed daemon is the version ARG REVLOOP_VERSION pins
#
# Two of those deserve a note on HOW they are checked, because the obvious way
# passes vacuously:
#
#   * (6) `git config --global` is HOME-derived. The image ends on USER root, so
#     reading it as root reads /root/.gitconfig, finds nothing, and an
#     exit-status check would call that a pass. The identity is written under
#     `USER alissa` into /home/alissa/.gitconfig, so it is read back with
#     `--user alissa` and compared to an expected VALUE, never just "is set".
#   * (8) the leaf COPYs its entrypoint over a base stub that lives at the same
#     path. "The file exists" is true either way. So the check is sha256
#     equality against the file in this repo, with the stub's banner asserted
#     ABSENT as a second, independent signal — a dropped COPY fails both.
#
# HOW TO RUN
#
#   docker/claude/tests-image-contract.sh
#
# Needs a working docker daemon. Without one it SKIPS loudly and exits 0, so it
# stays runnable from environments that have no daemon (the agent runners that
# implement this repo do not). Set REQUIRE_DOCKER=1 to turn that skip into a
# failure — CI does, so a runner that silently lost docker cannot look green.
#
# `--platform linux/amd64` is passed explicitly: the base publishes that
# platform only (see "Base image" in README.md).
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE_TAG:-alissa-review-daemon:contract-test}"
PLATFORM="${BUILD_PLATFORM:-linux/amd64}"

fail=0
info() { printf '%s\n' "$*"; }
pass() { printf '  ok   %s\n' "$*"; }
bad()  { printf '  FAIL %s\n' "$*"; fail=1; }

# --- 0. is there a daemon? ---------------------------------------------------
if ! command -v docker >/dev/null 2>&1 || ! docker version >/dev/null 2>&1; then
  info "SKIPPED: no usable docker daemon (\`docker version\` failed)."
  info "         This check builds a real image; there is nothing to substitute"
  info "         for that. Run it where a daemon exists, or read the CI job"
  info "         'Image contract' on the pull request."
  if [ "${REQUIRE_DOCKER:-0}" = "1" ]; then
    info "REQUIRE_DOCKER=1 -> treating the missing daemon as a FAILURE."
    exit 1
  fi
  exit 0
fi

# --- 1. build ----------------------------------------------------------------
info "1. docker build --platform ${PLATFORM} ${SCRIPT_DIR}"
if ! docker build --platform "${PLATFORM}" -t "${IMAGE}" "${SCRIPT_DIR}"; then
  info ""
  info "FAILURES: the build itself did not succeed — nothing below could run."
  exit 1
fi
pass "build succeeded -> ${IMAGE}"

# Expected values the container cannot know on its own.
EXPECT_EP_SHA="$(sha256sum "${SCRIPT_DIR}/entrypoint.sh"      | cut -d' ' -f1)"
EXPECT_RC_SHA="$(sha256sum "${SCRIPT_DIR}/revloop-config.sh"  | cut -d' ' -f1)"
EXPECT_FW_SHA="$(sha256sum "${SCRIPT_DIR}/init-firewall.sh"   | cut -d' ' -f1)"
EXPECT_AGENTS_SHA="$(sha256sum "${SCRIPT_DIR}/agents.yaml"    | cut -d' ' -f1)"
# The pin, read out of the Dockerfile the same way check-entrypoint.yaml reads it.
EXPECT_REVLOOP="$(sed -n 's/^ARG REVLOOP_VERSION=\([0-9.]*\).*/\1/p' "${SCRIPT_DIR}/Dockerfile")"

# --- 2-8, 10: assertions inside the image, as root ---------------------------
info ""
info "2. contract inside the image (as root)"
if ! docker run --rm -i --platform "${PLATFORM}" --entrypoint bash \
  -e "EXPECT_EP_SHA=${EXPECT_EP_SHA}" \
  -e "EXPECT_RC_SHA=${EXPECT_RC_SHA}" \
  -e "EXPECT_FW_SHA=${EXPECT_FW_SHA}" \
  -e "EXPECT_AGENTS_SHA=${EXPECT_AGENTS_SHA}" \
  -e "EXPECT_REVLOOP=${EXPECT_REVLOOP}" \
  "${IMAGE}" -s <<'PROBE'
set -uo pipefail
rc=0
ok()  { printf '  ok   %s\n' "$*"; }
no()  { printf '  FAIL %s\n' "$*"; rc=1; }
eq()  { # eq <label> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1 = $2"; else no "$1: expected '$2', got '$3'"; fi
}

# --- console scripts + inherited CLIs ---
# `codex` and `pi` arrived with base 0.2.0 and are unused by this daemon; they
# are here as BASE-CONTRACT assertions, not because anything in the leaf calls
# them.
for b in alissa-revloop alissa-revloop-ui claude alissa gh tmux codex pi; do
  p="$(command -v "$b" 2>/dev/null)"
  if [ -n "$p" ]; then ok "command -v $b -> $p"; else no "command -v $b -> NOT FOUND"; fi
done

# --- the non-root user ---
eq "id -u alissa" 1000 "$(id -u alissa 2>/dev/null)"
eq "id -g alissa" 1000 "$(id -g alissa 2>/dev/null)"

# --- system git config (base-owned; the leaf must not have shadowed it) ---
rew="$(git config --system --get-all 'url.https://github.com/.insteadOf' 2>/dev/null)"
case "${rew}" in
  *"git@github.com:"*) ok "system rewrite: git@github.com:" ;;
  *) no "system rewrite git@github.com: missing (got: ${rew:-<none>})" ;;
esac
case "${rew}" in
  *"ssh://git@github.com/"*) ok "system rewrite: ssh://git@github.com/" ;;
  *) no "system rewrite ssh://git@github.com/ missing (got: ${rew:-<none>})" ;;
esac
# NOT `credential.helper`: the base scopes it to the host, so /etc/gitconfig has
# a [credential "https://github.com"] section and the bare key is genuinely
# empty. Asking for the unscoped key reports "missing" against a correct image —
# this check did exactly that on its first CI run. Ask for the scoped key, and
# accept ANY credential.* helper as a fallback so a future base that widens the
# scope does not read as a regression.
helpers="$(git config --system --get-all 'credential.https://github.com.helper' 2>/dev/null)"
[ -n "${helpers}" ] || helpers="$(git config --system --get-regexp '^credential\.' 2>/dev/null)"
case "${helpers}" in
  *"gh auth git-credential"*) ok "credential helper for https://github.com -> gh auth git-credential" ;;
  *) no "gh credential helper missing (system credential.* = ${helpers:-<none>})" ;;
esac
eq "advice.detachedHead" "false" "$(git config --system advice.detachedHead 2>/dev/null)"

# --- claude first-run gates ---
for f in /home/alissa/.claude.json /home/alissa/.claude/settings.json; do
  if [ -s "$f" ]; then ok "first-run seeded: $f"; else no "first-run file missing/empty: $f"; fi
done
if grep -q 'hasCompletedOnboarding' /home/alissa/.claude.json 2>/dev/null; then
  ok "~/.claude.json carries hasCompletedOnboarding"
else
  no "~/.claude.json has no hasCompletedOnboarding key"
fi

# --- baked ARG->ENV knob defaults ---
eq "ALISSA_AGENT_PROFILE"   "claude"     "${ALISSA_AGENT_PROFILE-<unset>}"
eq "ALISSA_AGENT_MODEL"     "claude-fable-5-1" "${ALISSA_AGENT_MODEL-<unset>}"
eq "ALISSA_ON_MISSING_HUB"  "add"        "${ALISSA_ON_MISSING_HUB-<unset>}"
eq "ALISSA_WORKER_INTERVAL" "2"          "${ALISSA_WORKER_INTERVAL-<unset>}"
eq "ALISSA_WORKSPACE_ROOT"  "/workspace" "${ALISSA_WORKSPACE_ROOT-<unset>}"
# Pass-through knobs must stay EMPTY, not merely defined: a baked value here
# would shadow the daemon library's own default (the defect #30 removed).
for k in ALISSA_POLL_INTERVAL ALISSA_ROUND_CAP; do
  v="$(printenv "$k")"
  if [ -z "${v}" ]; then ok "$k is empty (pass-through preserved)"; else no "$k baked to '${v}' — shadows the library default"; fi
done

# --- the entrypoint is OURS, not the base's stub ---
eq "sha256 /usr/local/bin/entrypoint.sh"     "${EXPECT_EP_SHA}" "$(sha256sum /usr/local/bin/entrypoint.sh     | cut -d' ' -f1)"
eq "sha256 /usr/local/bin/revloop-config.sh" "${EXPECT_RC_SHA}" "$(sha256sum /usr/local/bin/revloop-config.sh | cut -d' ' -f1)"
eq "sha256 /usr/local/bin/init-firewall.sh"  "${EXPECT_FW_SHA}" "$(sha256sum /usr/local/bin/init-firewall.sh  | cut -d' ' -f1)"
if grep -q 'runs no daemon' /usr/local/bin/entrypoint.sh 2>/dev/null; then
  no "entrypoint.sh is still the BASE STUB — the leaf's COPY did not land"
else
  ok "base-stub banner absent from entrypoint.sh"
fi
for f in entrypoint.sh revloop-config.sh init-firewall.sh; do
  if [ -x "/usr/local/bin/$f" ]; then ok "executable: /usr/local/bin/$f"; else no "not executable: /usr/local/bin/$f"; fi
done
eq "sha256 agents.yaml" "${EXPECT_AGENTS_SHA}" "$(sha256sum /home/alissa/.config/alissa/agents.yaml | cut -d' ' -f1)"
eq "agents.yaml owner" "alissa:alissa" "$(stat -c '%U:%G' /home/alissa/.config/alissa/agents.yaml 2>/dev/null)"

# --- the installed daemon is the pinned one ---
got="$(python3 -c 'import importlib.metadata as m; print(m.version("alissa-tools-github-revloop"))' 2>/dev/null)"
eq "installed alissa-tools-github-revloop" "${EXPECT_REVLOOP}" "${got}"

exit "${rc}"
PROBE
then fail=1; fi

# --- 6. the git identity, read as the user that owns it ----------------------
info ""
info "3. git author identity, read AS alissa (--global is HOME-derived)"
if ! docker run --rm -i --platform "${PLATFORM}" --user alissa --entrypoint bash "${IMAGE}" -s <<'PROBE'
set -uo pipefail
rc=0
eq() { if [ "$2" = "$3" ]; then printf '  ok   %s = %s\n' "$1" "$2"; else printf '  FAIL %s: expected %s, got %s\n' "$1" "$2" "${3:-<unset>}"; rc=1; fi; }
eq "git config --global user.name"  "alissa-review-daemon" "$(git config --global user.name  2>/dev/null)"
eq "git config --global user.email" "support@alissa.app"   "$(git config --global user.email 2>/dev/null)"
exit "${rc}"
PROBE
then fail=1; fi

# --- 9. inherited image config ------------------------------------------------
info ""
info "4. inherited image config (the leaf re-declares none of this)"
cfg() { docker image inspect --format "$1" "${IMAGE}" 2>/dev/null; }
ceq() { if [ "$2" = "$3" ]; then pass "$1 = $2"; else bad "$1: expected '$2', got '${3:-<empty>}'"; fi; }
ceq "Entrypoint" "[/usr/bin/tini -- /usr/local/bin/entrypoint.sh]" "$(cfg '{{.Config.Entrypoint}}')"
ceq "Cmd"        "[]"          "$(cfg '{{.Config.Cmd}}')"
ceq "User"       "root"        "$(cfg '{{.Config.User}}')"
ceq "WorkingDir" "/workspace"  "$(cfg '{{.Config.WorkingDir}}')"
ceq "ExposedPorts" "map[8080/tcp:{}]" "$(cfg '{{.Config.ExposedPorts}}')"

info ""
if [ "${fail}" = "0" ]; then echo "ALL PASS"; exit 0; else echo "FAILURES"; exit 1; fi
