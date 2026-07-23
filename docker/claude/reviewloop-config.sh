#!/usr/bin/env bash
# =============================================================================
# Render reviewloop.config.json from the environment.
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
#     the authority: poll_interval, round_cap.
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
# Usage:  reviewloop-config.sh '<repos-json-array>'   # prints config JSON
# Or source it and call render_reviewloop_config '<repos-json-array>'.
# =============================================================================
set -euo pipefail

render_reviewloop_config() {
  local repos_json="$1"
  # --arg (string) + tonumber for the numeric pass-through keys: an unset/empty
  # env var yields "" and the key is dropped, so the library default wins.
  jq -n \
    --argjson repos "${repos_json}" \
    --arg     hub    "${ALISSA_ON_MISSING_HUB:-add}" \
    --arg     agent  "${ALISSA_AGENT_PROFILE:-claude}" \
    --arg     poll   "${ALISSA_POLL_INTERVAL:-}" \
    --arg     cap    "${ALISSA_ROUND_CAP:-}" \
    '{ repos: $repos, on_missing_hub: $hub, agent_profile: $agent }
     + (if $poll == "" then {} else { poll_interval: ($poll | tonumber) } end)
     + (if $cap  == "" then {} else { round_cap:     ($cap  | tonumber) } end)'
}

# Direct execution renders to stdout; sourcing just defines the function.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  render_reviewloop_config "${1:?usage: reviewloop-config.sh <repos-json-array>}"
fi
