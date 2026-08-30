#!/usr/bin/env bash
# =============================================================================
# Tests for the entrypoint's revloop.config.json renderer (#30).
#
# Proves the pass-through-when-unset contract:
#   * unset optional knobs  -> key OMITTED (library default applies)
#   * set optional knobs    -> key present with the given value (override wins)
#   * structural keys        -> always present with their pinned container value
#
# Pure shell + jq for the structural assertions (runs anywhere jq exists). A
# final cross-check, run only when the daemon package is importable, boots the
# omitted-key config through the real Config.build and asserts the effective
# value equals the library default — the acceptance criterion's "effective
# daemon config equals library defaults", verified against the actual library.
#
# Usage: bash docker/claude/tests-entrypoint-config.sh
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=revloop-config.sh
. "${HERE}/revloop-config.sh"

REPOS='["fahera-mx/studio.alissa.app"]'
fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1" >&2; fail=1; }

# assert_key_absent <json> <key> <label>
assert_key_absent() {
  if printf '%s' "$1" | jq -e "has(\"$2\")" >/dev/null; then
    bad "$3 (expected key '$2' absent, but present)"
  else
    pass "$3"
  fi
}
# assert_eq <json> <jq-filter> <expected> <label>
assert_eq() {
  local got; got="$(printf '%s' "$1" | jq -c "$2")"
  if [ "${got}" = "$3" ]; then pass "$4"; else bad "$4 (got ${got}, want $3)"; fi
}

echo "== pass-through: optional knobs omitted when env unset =="
out="$(env -u ALISSA_POLL_INTERVAL -u ALISSA_ROUND_CAP \
        -u ALISSA_STABILITY_ROUNDS \
        -u ALISSA_REAP_GRACE_SECONDS -u ALISSA_REAP_SESSION_CAP \
        -u ALISSA_MAX_CONCURRENT_SESSIONS \
        -u ALISSA_CHECKS_WAIT_SECONDS -u ALISSA_CHECKS_SPAWN_WAIT_SECONDS \
        -u ALISSA_REVIEW_TASK_MISS_TTL_POLLS -u ALISSA_TASK_LIST_SELF_SCOPE \
        bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
assert_key_absent "${out}" poll_interval "poll_interval omitted when ALISSA_POLL_INTERVAL unset"
assert_key_absent "${out}" round_cap     "round_cap omitted when ALISSA_ROUND_CAP unset"
assert_key_absent "${out}" stability_rounds \
  "stability_rounds omitted when ALISSA_STABILITY_ROUNDS unset"
assert_key_absent "${out}" reap_grace_seconds "reap_grace_seconds omitted when ALISSA_REAP_GRACE_SECONDS unset"
assert_key_absent "${out}" reap_session_cap   "reap_session_cap omitted when ALISSA_REAP_SESSION_CAP unset"
assert_key_absent "${out}" checks_wait_seconds \
  "checks_wait_seconds omitted when ALISSA_CHECKS_WAIT_SECONDS unset"
assert_key_absent "${out}" checks_spawn_wait_seconds \
  "checks_spawn_wait_seconds omitted when ALISSA_CHECKS_SPAWN_WAIT_SECONDS unset"
assert_key_absent "${out}" max_concurrent_sessions \
  "max_concurrent_sessions omitted when ALISSA_MAX_CONCURRENT_SESSIONS unset"
assert_key_absent "${out}" review_task_miss_ttl_polls \
  "review_task_miss_ttl_polls omitted when ALISSA_REVIEW_TASK_MISS_TTL_POLLS unset"
assert_key_absent "${out}" task_list_self_scope \
  "task_list_self_scope omitted when ALISSA_TASK_LIST_SELF_SCOPE unset"
assert_eq "${out}" '.on_missing_hub' '"add"'    "on_missing_hub always emitted (structural: add)"
assert_eq "${out}" '.agent_profile'  '"claude"' "agent_profile always emitted (structural: claude)"
assert_eq "${out}" '.repos'          "${REPOS}" "repos emitted from allowlist"
assert_key_absent "${out}" operators "operators omitted when no allowlist is passed"

echo "== operators: pass-through list (empty omitted, set emitted verbatim) =="
out="$(render_revloop_config "${REPOS}" '[]')"
assert_key_absent "${out}" operators "operators omitted when the list is empty"
out="$(render_revloop_config "${REPOS}" '["RHDZMOTA","ops-bot"]')"
assert_eq "${out}" '.operators' '["RHDZMOTA","ops-bot"]' "operators emitted from allowlist"

echo "== reviewer identity: pass-through, and the NAME never the token (#51) =="
out="$(env -u ALISSA_REVIEWER_LOGIN -u ALISSA_REVIEWER_TOKEN_ENV \
        bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
assert_key_absent "${out}" reviewer_login     "reviewer_login omitted when unset"
assert_key_absent "${out}" reviewer_token_env "reviewer_token_env omitted when unset"
out="$(ALISSA_REVIEWER_LOGIN=alissa-app \
       ALISSA_REVIEWER_TOKEN_ENV=REVLOOP_REVIEWER_GH_TOKEN \
       render_revloop_config "${REPOS}")"
assert_eq "${out}" '.reviewer_login'     '"alissa-app"' "reviewer_login emitted verbatim"
assert_eq "${out}" '.reviewer_token_env' '"REVLOOP_REVIEWER_GH_TOKEN"' \
  "reviewer_token_env carries the variable NAME"

echo "== empty-string env is treated as unset (Dockerfile bakes empty ENV) =="
out="$(ALISSA_ROUND_CAP="" ALISSA_POLL_INTERVAL="" ALISSA_STABILITY_ROUNDS="" \
       render_revloop_config "${REPOS}")"
assert_key_absent "${out}" round_cap     "round_cap omitted when ALISSA_ROUND_CAP is empty"
assert_key_absent "${out}" stability_rounds \
  "stability_rounds omitted when ALISSA_STABILITY_ROUNDS is empty"
assert_key_absent "${out}" poll_interval "poll_interval omitted when ALISSA_POLL_INTERVAL is empty"

echo "== override: set env still wins, emitted as a JSON number =="
out="$(ALISSA_ROUND_CAP=7 ALISSA_POLL_INTERVAL=90 ALISSA_STABILITY_ROUNDS=5 \
       render_revloop_config "${REPOS}")"
assert_eq "${out}" '.round_cap'     '7'  "round_cap override present as number"
assert_eq "${out}" '.stability_rounds' '5' \
  "stability_rounds override present as number"
assert_eq "${out}" '.poll_interval' '90' "poll_interval override present as number"
out="$(ALISSA_STABILITY_ROUNDS=0 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.stability_rounds' '0' \
  "stability_rounds=0 (guard OFF) is emitted, not treated as unset"
out="$(ALISSA_REAP_GRACE_SECONDS=900 ALISSA_REAP_SESSION_CAP=3 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.reap_grace_seconds' '900' "reap_grace_seconds override present as number"
assert_eq "${out}" '.reap_session_cap'   '3'   "reap_session_cap override present as number"
out="$(ALISSA_CHECKS_WAIT_SECONDS=600 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.checks_wait_seconds' '600' "checks_wait_seconds override present as number"
# The PRE-SPAWN bound (issue #84) is a separate knob from the verdict-side one
# above: it is paid as latency on every round whose head is still building,
# while that one is paid only by a round that has finished reviewing. Both are
# properties of the watched repos' CI, so both pass through.
out="$(ALISSA_CHECKS_SPAWN_WAIT_SECONDS=300 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.checks_spawn_wait_seconds' '300' \
  "checks_spawn_wait_seconds override present as number"
# The spawn gate. Rendered alongside a coherent reap_session_cap on purpose: the
# daemon refuses a config whose ALARM sits below its spawn LIMIT, so an operator
# lowering one has to look at the other, and the pair is what the container ships.
out="$(ALISSA_MAX_CONCURRENT_SESSIONS=2 ALISSA_REAP_SESSION_CAP=4 \
       render_revloop_config "${REPOS}")"
assert_eq "${out}" '.max_concurrent_sessions' '2' \
  "max_concurrent_sessions override present as number"
assert_eq "${out}" '.reap_session_cap' '4' "and the alarm it must not exceed"

# The task-list bounds (issue #87). The TTL is an ordinary numeric pass-through;
# the self-scope is this renderer's only BOOLEAN one, so its accepted spellings
# and -- more importantly -- its refusal of anything else are pinned here: a
# typo that quietly rendered `false` would be indistinguishable from the default
# it was trying to change.
out="$(ALISSA_REVIEW_TASK_MISS_TTL_POLLS=4 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.review_task_miss_ttl_polls' '4' \
  "review_task_miss_ttl_polls override present as number"
for truthy in 1 true TRUE yes on; do
  out="$(ALISSA_TASK_LIST_SELF_SCOPE="${truthy}" render_revloop_config "${REPOS}")"
  assert_eq "${out}" '.task_list_self_scope' 'true' \
    "task_list_self_scope=${truthy} renders JSON true"
done
for falsy in 0 false FALSE no off; do
  out="$(ALISSA_TASK_LIST_SELF_SCOPE="${falsy}" render_revloop_config "${REPOS}")"
  assert_eq "${out}" '.task_list_self_scope' 'false' \
    "task_list_self_scope=${falsy} renders JSON false"
done
if ALISSA_TASK_LIST_SELF_SCOPE=ture render_revloop_config "${REPOS}" >/dev/null 2>&1; then
  bad "a non-boolean ALISSA_TASK_LIST_SELF_SCOPE is refused, not silently false"
else
  pass "a non-boolean ALISSA_TASK_LIST_SELF_SCOPE is refused, not silently false"
fi

echo "== override: structural keys still overridable =="
out="$(ALISSA_ON_MISSING_HUB=skip ALISSA_AGENT_PROFILE=custom render_revloop_config "${REPOS}")"
assert_eq "${out}" '.on_missing_hub' '"skip"'   "on_missing_hub override wins"
assert_eq "${out}" '.agent_profile'  '"custom"' "agent_profile override wins"

echo "== cross-check: omitted keys resolve to the LIBRARY default =="
if python3 -c 'import alissa.tools.github.revloop.config' 2>/dev/null; then
  # ALISSA_STABILITY_ROUNDS, ALISSA_CHECKS_WAIT_SECONDS,
  # ALISSA_CHECKS_SPAWN_WAIT_SECONDS,
  # ALISSA_MAX_CONCURRENT_SESSIONS, ALISSA_REVIEW_TASK_MISS_TTL_POLLS and
  # ALISSA_TASK_LIST_SELF_SCOPE are unset here
  # too: the library this cross-check imports is the Dockerfile-PINNED release,
  # which predates those keys and would reject them as unknown. Rendering either
  # into the config would then fail the cross-check for a version skew rather
  # than for a default drift.
  out="$(env -u ALISSA_POLL_INTERVAL -u ALISSA_ROUND_CAP \
          -u ALISSA_STABILITY_ROUNDS \
          -u ALISSA_CHECKS_WAIT_SECONDS -u ALISSA_CHECKS_SPAWN_WAIT_SECONDS \
          -u ALISSA_MAX_CONCURRENT_SESSIONS \
          -u ALISSA_REVIEW_TASK_MISS_TTL_POLLS -u ALISSA_TASK_LIST_SELF_SCOPE \
          bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
  # Pass the rendered JSON via an env var (not a pipe) so the heredoc can own
  # stdin as the python program.
  if CONFIG_JSON="${out}" python3 <<'PY'
import json, os
from alissa.tools.github.revloop.config import Config
data = json.loads(os.environ["CONFIG_JSON"])
built = Config.build(workspace_root=".", file_data=data)
ref = Config(workspace_root=".")  # library defaults (dataclass fields)
assert "round_cap" not in data and "poll_interval" not in data, data
assert built.round_cap == ref.round_cap, (built.round_cap, ref.round_cap)
assert built.poll_interval == ref.poll_interval, (built.poll_interval, ref.poll_interval)
print(f"  ok   effective round_cap={built.round_cap} poll_interval={built.poll_interval} "
      f"== library defaults")
PY
  then :; else fail=1; fi
else
  echo "  skip (revloop package not importable — structural checks above still ran)"
fi

echo
[ "${fail}" = "0" ] && { echo "ALL PASS"; exit 0; } || { echo "FAILURES"; exit 1; }
