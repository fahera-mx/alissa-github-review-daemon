#!/usr/bin/env bash
# =============================================================================
# Container entrypoint for the Alissa GitHub review daemon.
#
#   0. as root: fix the volume-mount ownership (+ firewall), then drop to alissa
#   1. preflight the identities the loop depends on (gh / alissa / claude)
#   2. bootstrap the worktree-hub workspace + reviewloop config from a manifest
#   3. start `alissa worker` (backgrounded) and wait until it is up
#   4. run `alissa-reviewloop` in the foreground, stopping the worker on exit
#
# The daemon is a thin poller; the worker is what actually spawns reviewers, so
# the worker MUST be running first — the daemon only warns if it isn't.
# =============================================================================
set -euo pipefail

log()  { printf '[entrypoint] %s\n' "$*" >&2; }
die()  { printf '[entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

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

# The API token above is enough for `gh api` calls, but NOT for git itself:
# hub-ifying a repo (on_missing_hub:add) does a `git clone`, which needs a git
# credential helper. Wire gh in as that helper so https clones/fetches of
# private repos authenticate with the same token. Non-fatal: an SSH-based or
# public-only setup does not need it.
gh auth setup-git 2>/dev/null \
  && log "git credential helper configured (gh)" \
  || log "WARN: gh auth setup-git failed — private-repo clone/fetch may not authenticate"

# 2c. alissa — tasks, session queue, verdicts. The CLI reads ALISSA_API_TOKEN,
#     but `auth login` also stores + verifies it, which is the real preflight.
[ -n "${ALISSA_API_TOKEN:-}" ] \
  || die "no ALISSA_API_TOKEN — cannot reach tasks / session queue"
alissa auth login --token "${ALISSA_API_TOKEN}" >/dev/null 2>&1 \
  || die "ALISSA_API_TOKEN rejected (alissa auth login failed)"
log "alissa authenticated"

# -----------------------------------------------------------------------------
# 2d. Resolve the reviewer model into the baked agents.yaml.
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
# pre-clone anything — we only guarantee a manifest and a reviewloop config
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

CONFIG="${WORKSPACE_ROOT}/reviewloop.config.json"

if [ -n "$(repos_lines)" ]; then
  # ENV-DRIVEN MODE: ALISSA_REVIEW_REPOS is authoritative, so (re)generate the
  # manifest + config on EVERY boot. The files persist on the /workspace volume,
  # so "generate only if absent" would pin them to the first boot's value and a
  # later Railway env change would silently never apply. Regenerating is safe:
  # the allowlist is the full set of repos the daemon may touch (on_missing_hub
  # only hub-ifies repos already in it), and the cloned hub dirs on the volume
  # are untouched by rewriting this text.
  log "generating ${MANIFEST} + reviewloop.config.json from ALISSA_REVIEW_REPOS"
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

  repos_json="$(repos_lines | jq -R . | jq -s -c .)"
  jq -n \
    --argjson repos "${repos_json}" \
    --argjson poll   "${ALISSA_POLL_INTERVAL:-60}" \
    --argjson cap    "${ALISSA_ROUND_CAP:-3}" \
    --arg     agent  "${ALISSA_AGENT_PROFILE:-claude}" \
    --arg     hub    "${ALISSA_ON_MISSING_HUB:-add}" \
    '{
       repos: $repos,
       poll_interval: $poll,
       round_cap: $cap,
       agent_profile: $agent,
       on_missing_hub: $hub
     }' > "${CONFIG}"
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
STATE_DB="${WORKSPACE_ROOT}/.reviewloop/state.db"
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
# 5. Run the daemon in the foreground; stop the worker on shutdown.
# -----------------------------------------------------------------------------
DAEMON_PID=""
shutdown() {
  log "shutting down"
  [ -n "${DAEMON_PID}" ] && kill "${DAEMON_PID}" 2>/dev/null || true
  alissa worker stop >/dev/null 2>&1 || true
  wait "${DAEMON_PID}" 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

# Extra daemon flags (e.g. -v, --once, --dry-run) pass through as CMD args.
log "starting alissa-reviewloop over ${WORKSPACE_ROOT}"
alissa-reviewloop --workspace-root "${WORKSPACE_ROOT}" "$@" &
DAEMON_PID=$!
wait "${DAEMON_PID}"
