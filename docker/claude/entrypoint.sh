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
#     never calls claude (only the worker-spawned reviewer does), and claude may
#     be authenticated by other means — a mounted ~/.claude credential, a token
#     from `claude setup-token`, or Bedrock/Vertex env. So warn and continue; a
#     reviewer with truly no credential fails loudly on its own later.
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  log "WARN: no ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN in env — relying on claude's own stored auth; reviewers will fail if none is configured"
else
  log "claude credential present"
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

if [ ! -f "${MANIFEST}" ]; then
  # Required to generate a manifest, and required by on_missing_hub:add anyway
  # (the daemon refuses "add" with an empty allowlist).
  [ -n "$(repos_lines)" ] \
    || die "no alissa-workspace.yaml mounted and ALISSA_REVIEW_REPOS is empty — nothing to review"
  log "generating ${MANIFEST} from ALISSA_REVIEW_REPOS"
  {
    printf 'name: %s\n' "${WORKSPACE_NAME}"
    printf 'description: Containerized Alissa review daemon workspace\n'
    printf 'repos:\n'
    repos_lines | while IFS= read -r r; do
      printf '  - repo: %s\n' "${r}"
    done
    printf 'reviewers: []\n'
    printf 'skills:\n  - alissa-code-workspace\n'
    printf 'attributes: {}\n'
  } > "${MANIFEST}"
fi

CONFIG="${WORKSPACE_ROOT}/reviewloop.config.json"
if [ ! -f "${CONFIG}" ]; then
  log "generating ${CONFIG} (on_missing_hub: add)"
  # Build the repos JSON array from the same allowlist.
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
fi

# -----------------------------------------------------------------------------
# 3a. Pre-trust the reviewer working directories.
#
# claude shows a per-directory "Is this a project you trust?" prompt the first
# time it opens a folder, and --dangerously-skip-permissions does NOT suppress
# it — so the reviewer TUI hangs there ("stuck — waiting at a prompt"). The
# accept flag lives per-project in ~/.claude.json. Pre-set it for every hub
# main/ the reviewer will cd into: each allowlisted repo (may not be cloned yet)
# plus any hub already on disk. This runs before the worker, so the flag is in
# place before any reviewer session starts.
# -----------------------------------------------------------------------------
python3 - "${WORKSPACE_ROOT}" <<'PY' || true
import glob, json, os, sys
root = sys.argv[1]
cfg = os.path.expanduser("~/.claude.json")
try:
    data = json.load(open(cfg))
except Exception:
    data = {}
projects = data.setdefault("projects", {})
paths = set()
# Allowlisted repos (basename of owner/repo), even before they are hub-ified.
for r in os.environ.get("ALISSA_REVIEW_REPOS", "").replace("|", "\n").split():
    r = r.strip()
    if "/" in r:
        paths.add(os.path.join(root, r.split("/")[-1], "main"))
# Any hub main/ already present (covers --dir overrides and mounted workspaces).
paths.update(glob.glob(os.path.join(root, "*", "main")))
for p in sorted(paths):
    projects.setdefault(p, {})["hasTrustDialogAccepted"] = True
json.dump(data, open(cfg, "w"), indent=2)
print(f"[entrypoint] pre-trusted {len(paths)} reviewer dir(s) in ~/.claude.json")
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
