#!/usr/bin/env bash
# =============================================================================
# Container entrypoint for the Alissa GitHub review daemon.
#
#   0. as root: fix the volume-mount ownership (+ firewall), then drop to alissa
#   1. preflight the identities the loop depends on (gh / alissa / claude)
#   2. bootstrap the worktree-hub workspace + revloop config from a manifest
#   3. start `alissa worker` (backgrounded) and wait until it is up
#   4. optionally start the `alissa-revloop-ui` console sidecar (ALISSA_UI_ENABLED)
#   5. run `alissa-revloop` in the foreground, stopping the worker on exit
#
# The daemon is a thin poller; the worker is what actually spawns reviewers, so
# the worker MUST be running first — the daemon only warns if it isn't.
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
# -----------------------------------------------------------------------------
if [ "$(id -u)" = "0" ]; then
  mkdir -p "${WORKSPACE_ROOT}" "${TMUX_TMPDIR:-/home/${RUNTIME_USER}/.tmux}"
  # Fix ownership so the unprivileged user can write. -R because a restart may
  # find files a previous root-mounted run left behind.
  chown -R "${RUNTIME_USER}:${RUNTIME_USER}" \
    "${WORKSPACE_ROOT}" "${TMUX_TMPDIR:-/home/${RUNTIME_USER}/.tmux}" 2>/dev/null || true
  log "workspace mount ${WORKSPACE_ROOT} owned by ${RUNTIME_USER}"

  if [ "${ALISSA_ENABLE_FIREWALL:-0}" = "1" ]; then
    log "raising egress firewall (ALISSA_ENABLE_FIREWALL=1)"
    /usr/local/bin/init-firewall.sh \
      || die "firewall init failed — did you pass --cap-add=NET_ADMIN?"
  fi

  log "dropping to ${RUNTIME_USER}"
  exec gosu "${RUNTIME_USER}" "$0" "$@"
fi

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
if [ -n "${ALISSA_REVIEWER_TOKEN_ENV:-}" ]; then
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
UI_ENABLED=0
case "$(printf '%s' "${ALISSA_UI_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) UI_ENABLED=1 ;;
esac
UI_PORT="${PORT:-8080}"
if [ "${UI_ENABLED}" = "1" ]; then
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
# pin it explicitly at boot: ALISSA_AGENT_MODEL (default `opus`) is appended to
# the profile's `command:` as `--model <value>`. The value passes through
# verbatim — aliases (`opus`, `sonnet`) and full ids (`claude-opus-4-8`) are both
# valid, no allowlist. `default` or an empty value omits the flag entirely,
# restoring the account-default behavior.
#
# Precedence: we only rewrite the BAKED default (identified by its `alissa-managed:`
# marker). A custom agents.yaml mounted over this path carries no marker, so it is
# left verbatim and ALISSA_AGENT_MODEL is ignored for it — the mounted command
# always wins. The baked file lives in the ephemeral home (not on the volume), so
# it is pristine on every boot and this rewrite is idempotent.
# -----------------------------------------------------------------------------
AGENTS_YAML="${HOME}/.config/alissa/agents.yaml"
# Default `opus` only when UNSET (use `-`, not `:-`): an explicitly empty value is
# a valid opt-out and must NOT be re-defaulted back to opus.
AGENT_MODEL="${ALISSA_AGENT_MODEL-opus}"
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
  log "generating ${MANIFEST} + revloop.config.json from ALISSA_REVIEW_REPOS"
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
  # ALISSA_POLL_INTERVAL / ALISSA_ROUND_CAP is omitted so the daemon library's
  # own default applies (env var > library default, no shadowing entrypoint
  # layer). Structural keys (on_missing_hub, agent_profile) are always emitted.
  # See revloop-config.sh for the precedence contract and per-key rationale.
  repos_json="$(repos_lines | jq -R . | jq -s -c .)"
  # `|| true` because an EMPTY operator list is the normal case: the last
  # filter in operators_lines is a grep, which exits 1 when it matches nothing,
  # and under `set -e -o pipefail` that non-zero status would kill the
  # entrypoint mid-bootstrap. jq's own status still propagates.
  operators_json="$({ operators_lines || true; } | jq -R . | jq -s -c .)"
  render_revloop_config "${repos_json}" "${operators_json}" > "${CONFIG}"
else
  # MOUNTED MODE: no allowlist in the env — respect a mounted workspace as-is.
  [ -f "${MANIFEST}" ] \
    || die "no alissa-workspace.yaml mounted and ALISSA_REVIEW_REPOS is empty — nothing to review"
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
STATE_DB="${WORKSPACE_ROOT}/.revloop/state.db"
if [ -f "${STATE_DB}" ]; then
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
# 4. Start the worker, wait until it reports running.
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
# 5. Run the daemon in the foreground; stop the worker on shutdown.
# -----------------------------------------------------------------------------
DAEMON_PID=""
shutdown() {
  log "shutting down"
  # Silence the console monitor FIRST so tearing the sidecar down below does not
  # trip its "EXITED" alarm during an orderly shutdown.
  [ -n "${UI_MONITOR_PID}" ] && kill "${UI_MONITOR_PID}" 2>/dev/null || true
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
