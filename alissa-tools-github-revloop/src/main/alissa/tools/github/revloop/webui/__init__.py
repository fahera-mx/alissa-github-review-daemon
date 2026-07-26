"""The reviewer console: a stdlib-only sidecar (`alissa-revloop-ui`) that
renders live review-daemon state for an operator, spending **zero** GitHub API
budget of its own beyond two cached checks.

Ported from the devloop's worker console (`alissa.tools.github.devloop.webui`,
its PR #38) and adapted to reviewer semantics: the unit of work is a PR round,
not an issue attempt, so the pipeline board is PR-centric (PR ref → round k of
the cap → session → stage), the operator inbox pages cap-outs and stalled
deferrals, and there is no worker-tasks panel (reviewers create no tasks) and
no maintenance edge. Module shapes are copied deliberately -- family precedent
is copy-adapt per repo; a shared-webui package is a separate, deferred lane.

The daemon (loop.py) already persists everything the console needs: every poll
pass writes one `poll_snapshots` row (UI-1, PR #35) carrying the pass timing,
the candidate count, the decision-summary counts, and the compact per-item
stage list. The sidecar reads that table through `State.read_snapshots`, plus
the spawn ledger, the escalation table and the ping ledger (the operator
inbox), all read-only. Its only live signals are local (`alissa tmux ls`, a
`/proc` walk of each session's pane-PID tree) or cached (`gh api rate_limit`,
60s; the PyPI version JSON, 10m) -- so a fleet of operators refreshing the
dashboard never moves the daemon's rate budget.

Layout:
  auth.py     -- fail-closed passcode, HMAC-signed sessions, CSRF, login throttle
  sysinfo.py  -- /proc process-tree CPU%/RSS (sample-free, vanished-PID tolerant)
  sources.py  -- the read-only data layer + the retry-now UPDATE, cached checks
  page.py     -- the single static HTML page (studio design system, both themes)
  server.py   -- ThreadingHTTPServer wiring, routing, auth/CSRF gating, actions
  __main__.py -- the `alissa-revloop-ui` console entry point
"""

from __future__ import annotations

from .auth import Auth, LoginThrottle, PasscodeUnset, require_passcode

__all__ = ["Auth", "LoginThrottle", "PasscodeUnset", "require_passcode"]
