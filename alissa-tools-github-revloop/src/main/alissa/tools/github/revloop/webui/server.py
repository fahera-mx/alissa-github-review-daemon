"""The console's HTTP surface: ThreadingHTTPServer, routing, and auth/CSRF
gating around the two actions.

Threading matters for correctness, not just latency: a `/proc` walk or a `gh`
call can take a beat, and a single-threaded server would serialise the ~10s
client poll behind it. Each request opens its own short-lived sqlite connection
(Sources does this per call), so nothing is shared across handler threads.

Route map:
  GET  /            -> login page, or the dashboard when the session verifies
  GET  /healthz     -> liveness, unauthenticated (for a container healthcheck)
  GET  /api/state   -> the dashboard JSON (session cookie required)
  POST /login       -> passcode -> throttle/verify -> set signed session cookie
  POST /logout      -> clear the session cookie
  POST /action/kill -> `alissa tmux kill <session>` (session + CSRF required)
  POST /action/retry-> age a round's ledger row for retry (session + CSRF)

Every action POST is gated on BOTH the signed session cookie AND a CSRF token
bound to it, and every action is audit-logged to stdout as a JSON line.
"""

from __future__ import annotations

import json
import re
import sys
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs

from ..proc import CommandError, run as proc_run
from .auth import SESSION_COOKIE, Auth
from .page import dashboard_page, login_page
from .sources import RETRY_OK, Sources, is_managed

# A managed reviewer session is `review-<repo>-pr<n>-r<k>-<nonce>` and friends:
# it starts with a letter and carries only these characters. Validating against
# it makes the kill argv impossible to weaponise -- the argv is already pinned
# to `alissa tmux kill <name>` (a name arg, never a raw `tmux kill-server`), and
# the leading-char rule additionally forbids a `-flag`-shaped name.
_SAFE_SESSION = re.compile(r"\A[A-Za-z0-9._][A-Za-z0-9._-]{0,199}\Z")

# A `<owner>/<repo>` slug, validated before it reaches a SQL parameter or an
# audit line. Parameterised queries already make injection a non-issue; this
# keeps junk out of the ledger lookup and the audit trail.
_SAFE_SLUG = re.compile(r"\A[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}\Z")

# Upper bound on a request body. Every body this server reads is tiny -- a
# passcode form or a small JSON action payload -- and `/login` reads its body
# UNAUTHENTICATED, so an oversized `Content-Length` must not force a large
# allocation before any auth runs. Bodies over the cap are refused unread.
_MAX_BODY = 64 * 1024

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    # Nothing this server serves is cacheable: /api/state carries session
    # names, the config echo and the daemon log tail, and the authed page
    # carries the CSRF token. Applied uniformly (the login page and /healthz
    # lose nothing by it) so no authenticated response can be stored by the
    # browser or by an intermediary in the reverse-proxied posture.
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


def _default_audit(action: str, detail: dict) -> None:
    """One JSON line per action, to stdout. stdout (not the logger) on purpose:
    the audit trail is a first-class output stream a container can ship to its
    log sink, separate from the daemon's diagnostic logging on stderr."""
    line = json.dumps({"audit": action, **detail}, separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class App:
    """The wiring the request handler dispatches into. Holds auth + the data
    layer and owns the two side-effecting actions, so the whole request surface
    is testable without a socket (see the handler's `dispatch_*` seams)."""

    def __init__(
        self,
        *,
        auth: Auth,
        sources: Sources,
        version: str,
        run: "Callable[..., str]" = proc_run,
        audit: "Callable[[str, dict], None]" = _default_audit,
        secure_cookie: bool = False,
    ) -> None:
        self.auth = auth
        self.sources = sources
        self.version = version
        self._run = run
        self._audit = audit
        # Add `Secure` to the session cookie. Off by default (the localhost-HTTP
        # posture, where Secure would break the cookie), switched on for the
        # reverse-proxied-under-TLS posture auth.py contemplates so the cookie
        # never rides plain HTTP.
        self.secure_cookie = secure_cookie

    def kill_session(self, name: "str | None") -> "tuple[bool, str]":
        """Kill exactly ONE session, by name. The argv is pinned to
        `alissa tmux kill <name>` -- never `kill-server`, never a flag -- and
        the name is validated for SHAPE first, so an action POST can only ever
        kill a well-formed session name.

        Deliberately not restricted to this daemon's `review-*` namespace: the
        sessions table lists every session that holds a worker slot (including
        another daemon's), and seeing what holds a slot is only half useful if
        the operator cannot free it. The unmanaged rows are marked as such in
        the table and named in the confirm prompt -- the gate is the operator's
        informed click, not a namespace filter. The audit line records
        `managed` alongside the name, so the trail says whose process tree was
        killed: this daemon's reviewer, or someone else's worker.
        """
        if not name or not _SAFE_SESSION.match(name):
            self._audit("kill", {"session": name, "ok": False, "error": "invalid name"})
            return False, "invalid session name"
        managed = is_managed(name)
        try:
            self._run(["alissa", "tmux", "kill", name], timeout=30)
        except CommandError as exc:
            self._audit("kill", {"session": name, "managed": managed,
                                 "ok": False, "error": str(exc)})
            return False, str(exc)
        self._audit("kill", {"session": name, "managed": managed, "ok": True})
        return True, "killed"

    def retry(
        self, repo_slug: "str | None", number: Any, round_: Any
    ) -> "tuple[bool, str]":
        """Retry-now: age the round's newest ledger row past the stale window
        (an UPDATE via Sources.retry_now) so the daemon can respawn it.
        Validates the shape, then delegates -- the console adds no retry logic
        of its own."""
        if not repo_slug or not _SAFE_SLUG.match(str(repo_slug)):
            self._audit("retry", {"ok": False, "error": "bad repo", "repo_slug": repo_slug})
            return False, "bad request"
        try:
            num = int(number)
            rnd = int(round_)
        except (TypeError, ValueError):
            self._audit("retry", {"ok": False, "error": "bad number", "repo_slug": repo_slug})
            return False, "bad number"
        outcome = self.sources.retry_now(str(repo_slug), num, rnd)
        ok = outcome == RETRY_OK
        self._audit("retry", {
            "ok": ok, "outcome": outcome, "repo_slug": repo_slug,
            "number": num, "round": rnd,
        })
        return ok, outcome


class Handler(BaseHTTPRequestHandler):
    server_version = "alissa-revloop-ui"
    protocol_version = "HTTP/1.1"
    # HTTP/1.1 keeps connections alive and ThreadingHTTPServer spawns one
    # (uncapped) thread per connection, so without a socket timeout a client
    # that connects and says nothing pins a thread forever. 30s is far longer
    # than any request this server serves.
    timeout = 30

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter access log
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- request helpers ---------------------------------------------------

    def _session_token(self) -> "str | None":
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except Exception:  # pragma: no cover - defensive
            return None
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _authed(self) -> bool:
        return self.app.auth.verify_session(self._session_token())

    def _csrf_ok(self) -> bool:
        token = self._session_token()
        provided = self.headers.get("X-CSRF-Token")
        return self.app.auth.verify_csrf(token, provided)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return b""
        if length <= 0:
            return b""
        if length > _MAX_BODY:
            # Refuse before reading/allocating. We deliberately leave the body
            # unread on the wire, so drop keep-alive on this connection.
            self.close_connection = True
            return b""
        return self.rfile.read(length)

    def _send(self, status: int, body: bytes, content_type: str,
              extra: "dict[str, str] | None" = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200,
                   extra: "dict[str, str] | None" = None) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8", extra)

    def _send_json(self, obj: Any, status: int = 200,
                   extra: "dict[str, str] | None" = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json({"ok": True, "version": self.app.version})
            return
        if path == "/":
            if self._authed():
                csrf = self.app.auth.csrf_token(self._session_token() or "")
                self._send_html(dashboard_page(csrf, self.app.version))
            else:
                self._send_html(login_page(), status=200)
            return
        if path == "/api/state":
            if not self._authed():
                self._send_json({"error": "unauthorized"},
                                status=HTTPStatus.UNAUTHORIZED)
                return
            self._send_json(self.app.sources.dashboard())
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/login":
            self._handle_login()
            return
        if path == "/logout":
            # Expiring the cookie is a state change, so it is CSRF-gated like
            # the actions below -- otherwise a cross-site POST could force-logout
            # the operator. verify_csrf requires a valid session token to derive
            # the token, so this implies a live session too.
            if not self._csrf_ok():
                self._send_json({"error": "csrf"}, status=HTTPStatus.FORBIDDEN)
                return
            self._send_html(
                login_page(),
                extra={"Set-Cookie": self._expire_cookie()},
            )
            return
        # Everything below is a state-changing action: session + CSRF required.
        if path in ("/action/kill", "/action/retry"):
            if not self._authed():
                self._send_json({"error": "unauthorized"},
                                status=HTTPStatus.UNAUTHORIZED)
                return
            if not self._csrf_ok():
                self._send_json({"error": "csrf"}, status=HTTPStatus.FORBIDDEN)
                return
            payload = self._json_body()
            if path == "/action/kill":
                ok, msg = self.app.kill_session(payload.get("session"))
            else:
                ok, msg = self.app.retry(
                    payload.get("repo_slug"), payload.get("number"),
                    payload.get("round"),
                )
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def _json_body(self) -> dict:
        try:
            data = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _handle_login(self) -> None:
        form = parse_qs(self._read_body().decode("utf-8", "replace"))
        submitted = (form.get("passcode") or [""])[0]
        ok, reason = self.app.auth.attempt_login(submitted)
        if ok:
            token = self.app.auth.issue_session()
            cookie = (
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; "
                f"SameSite=Strict{self._cookie_secure()}"
            )
            self._send_html(
                _REDIRECT_HOME, status=303,
                extra={"Set-Cookie": cookie, "Location": "/"},
            )
            return
        if reason == "locked":
            wait = int(self.app.auth.throttle.retry_after()) + 1
            self._send_html(
                login_page(f"Too many attempts -- locked for {wait}s."),
                status=429, extra={"Retry-After": str(wait)},
            )
            return
        self._send_html(login_page("Incorrect passcode."), status=401)

    def _cookie_secure(self) -> str:
        """`; Secure` when the app is configured for a TLS-terminated posture,
        else empty. Kept off by default so the cookie still works over the
        localhost-HTTP default (a Secure cookie is dropped on plain HTTP)."""
        return "; Secure" if self.app.secure_cookie else ""

    def _expire_cookie(self) -> str:
        return (
            f"{SESSION_COOKIE}=; Path=/; HttpOnly; "
            f"SameSite=Strict{self._cookie_secure()}; Max-Age=0"
        )


_REDIRECT_HOME = (
    '<!doctype html><meta http-equiv="refresh" content="0; url=/">'
    "<a href=\"/\">continue</a>"
)


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server
