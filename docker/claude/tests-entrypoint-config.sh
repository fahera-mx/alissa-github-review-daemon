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
        bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
assert_key_absent "${out}" poll_interval "poll_interval omitted when ALISSA_POLL_INTERVAL unset"
assert_key_absent "${out}" round_cap     "round_cap omitted when ALISSA_ROUND_CAP unset"
assert_eq "${out}" '.on_missing_hub' '"add"'    "on_missing_hub always emitted (structural: add)"
assert_eq "${out}" '.agent_profile'  '"claude"' "agent_profile always emitted (structural: claude)"
assert_eq "${out}" '.repos'          "${REPOS}" "repos emitted from allowlist"

echo "== empty-string env is treated as unset (Dockerfile bakes empty ENV) =="
out="$(ALISSA_ROUND_CAP="" ALISSA_POLL_INTERVAL="" render_revloop_config "${REPOS}")"
assert_key_absent "${out}" round_cap     "round_cap omitted when ALISSA_ROUND_CAP is empty"
assert_key_absent "${out}" poll_interval "poll_interval omitted when ALISSA_POLL_INTERVAL is empty"

echo "== override: set env still wins, emitted as a JSON number =="
out="$(ALISSA_ROUND_CAP=7 ALISSA_POLL_INTERVAL=90 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.round_cap'     '7'  "round_cap override present as number"
assert_eq "${out}" '.poll_interval' '90' "poll_interval override present as number"

echo "== override: structural keys still overridable =="
out="$(ALISSA_ON_MISSING_HUB=skip ALISSA_AGENT_PROFILE=custom render_revloop_config "${REPOS}")"
assert_eq "${out}" '.on_missing_hub' '"skip"'   "on_missing_hub override wins"
assert_eq "${out}" '.agent_profile'  '"custom"' "agent_profile override wins"

echo "== cross-check: omitted keys resolve to the LIBRARY default =="
if python3 -c 'import alissa.tools.github.revloop.config' 2>/dev/null; then
  out="$(env -u ALISSA_POLL_INTERVAL -u ALISSA_ROUND_CAP \
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
