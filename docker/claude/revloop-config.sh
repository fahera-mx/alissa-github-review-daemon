#!/usr/bin/env bash
# =============================================================================
# Render revloop.config.json from the environment.
#
# Precedence contract:  env var  >  daemon library default.  There is NO hidden
# entrypoint layer in between for optional tuning knobs — the entrypoint used to
# inject EVERY key with its own hardcoded fallback (e.g. `round_cap: 3`), which
# SHADOWED the library's own default: a library that raised its default (say to
# 10) could never take effect in the container, because the entrypoint always
# wrote the old value unless the operator happened to set ALISSA_ROUND_CAP.
#
# So keys fall into two classes:
#
#   * PASS-THROUGH (optional tuning knobs) — emitted ONLY when the env var is
#     set. When unset the key is omitted entirely and the daemon library applies
#     its own current default. These are pure tuning values where the library is
#     the authority: poll_interval, round_cap, checks_wait_seconds (how long a
#     round holds its approve for an unsettled CI rollup, per condition waited on
#     -- so up to 2x it if an unreadable hold becomes a pending one; a property
#     of the watched repos' CI, not of the image), operators (an EMPTY operator
#     allowlist is the library's fail-closed default -- emitting `[]` would say
#     the same thing, but omitting it keeps "unset means the library decides"
#     true for every optional key without exception).
#
#   * STRUCTURAL (container constants) — always emitted with an explicit value
#     the container requires, INDEPENDENT of the library default. Pass-through is
#     unsafe here (see the per-key rationale below), so the value is byte-pinned
#     and covered by tests-entrypoint-config.sh:
#       - on_missing_hub = add     the container's whole model is self-contained
#                                  hub-ify on demand; the library default is
#                                  `skip`, which would make a fresh volume review
#                                  nothing. (Bounded: `add` requires a non-empty
#                                  repos allowlist, which env-driven mode always
#                                  has.)
#       - agent_profile  = claude  must name a profile that exists in the baked
#                                  agents.yaml, which defines exactly `claude`;
#                                  drifting to some future library default would
#                                  select a profile the image does not ship.
#
# `repos` is required (env-driven mode only calls this with a non-empty
# allowlist) and always emitted.
#
# The two reviewer-identity keys are pass-through for the same reason as the
# tuning knobs -- unset means "the library decides" -- but they are worth
# calling out because the container is where their absence bites (issue #51):
#   - reviewer_login      the identity every review MUST be posted under.
#                          Unset, the daemon adopts whatever the gh credential
#                          resolves to at boot.
#   - reviewer_token_env  the NAME of the variable carrying that identity's
#                          token. Unset, every `gh` call inherits the
#                          container's default credential -- and this container
#                          holds more than one identity, which is exactly how a
#                          round's verdict landed under the implementer's login.
#
# Usage:  revloop-config.sh '<repos-json-array>' ['<operators-json-array>']
# Or source it and call render_revloop_config '<repos-json>' '<operators-json>'.
# =============================================================================
set -euo pipefail

render_revloop_config() {
  local repos_json="$1"
  # Operator logins allowed to re-open a capped PR with an
  # `alissa-review: re-enter +N` comment. Absent/empty -> the key is omitted and
  # the daemon honours no ack at all (see the daemon README).
  local operators_json="${2:-[]}"
  # --arg (string) + tonumber for the numeric pass-through keys: an unset/empty
  # env var yields "" and the key is dropped, so the library default wins.
  jq -n \
    --argjson repos     "${repos_json}" \
    --argjson operators "${operators_json}" \
    --arg     hub    "${ALISSA_ON_MISSING_HUB:-add}" \
    --arg     agent  "${ALISSA_AGENT_PROFILE:-claude}" \
    --arg     poll   "${ALISSA_POLL_INTERVAL:-}" \
    --arg     cap    "${ALISSA_ROUND_CAP:-}" \
    --arg     grace  "${ALISSA_REAP_GRACE_SECONDS:-}" \
    --arg     scap   "${ALISSA_REAP_SESSION_CAP:-}" \
    --arg     maxses "${ALISSA_MAX_CONCURRENT_SESSIONS:-}" \
    --arg     cwait  "${ALISSA_CHECKS_WAIT_SECONDS:-}" \
    --arg     rlogin "${ALISSA_REVIEWER_LOGIN:-}" \
    --arg     rtoken "${ALISSA_REVIEWER_TOKEN_ENV:-}" \
    '{ repos: $repos, on_missing_hub: $hub, agent_profile: $agent }
     + (if $poll  == "" then {} else { poll_interval:      ($poll  | tonumber) } end)
     + (if $cap   == "" then {} else { round_cap:          ($cap   | tonumber) } end)
     + (if $grace == "" then {} else { reap_grace_seconds: ($grace | tonumber) } end)
     + (if $scap  == "" then {} else { reap_session_cap:   ($scap  | tonumber) } end)
     + (if $maxses == "" then {} else { max_concurrent_sessions: ($maxses | tonumber) } end)
     + (if $cwait == "" then {} else { checks_wait_seconds: ($cwait | tonumber) } end)
     + (if $rlogin == "" then {} else { reviewer_login:     $rlogin } end)
     + (if $rtoken == "" then {} else { reviewer_token_env: $rtoken } end)
     + (if ($operators | length) == 0 then {} else { operators: $operators } end)'
}

# Direct execution renders to stdout; sourcing just defines the function.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  render_revloop_config "${1:?usage: revloop-config.sh <repos-json-array>}"
fi
