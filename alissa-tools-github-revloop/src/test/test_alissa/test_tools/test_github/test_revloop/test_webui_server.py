"""The HTTP surface: pinned action argv, audit lines, auth/CSRF gating end to
end over a real socket, and the fail-closed CLI boot."""

from __future__ import annotations

import http.cookies
import io
import json
import re
import threading
import types
import urllib.error
import urllib.request

import pytest

from alissa.tools.github.revloop.config import Config
from alissa.tools.github.revloop.loop import STALE_ROUND_SECONDS
from alissa.tools.github.revloop.proc import CommandError
from alissa.tools.github.revloop.state import State
from alissa.tools.github.revloop.webui import __main__ as ui_main
from alissa.tools.github.revloop.webui.auth import SESSION_COOKIE, Auth
from alissa.tools.github.revloop.webui.server import App, Handler, _MAX_BODY, make_server
from alissa.tools.github.revloop.webui.sources import Sources

SESSION = "review-widgets-pr16-r1-ab12cd"


def seed(db_path):
    with State(db_path) as st:
        st.record_spawn(repo="acme/widgets", number=16, round_=1,
                        head_sha="cafe1234", session=SESSION, task_ref="TASK-9")


# -- App.kill_session: the argv is pinned -----------------------------------

def make_app(tmp_path, runner=None, audit=None):
    config = Config.build(tmp_path, {"repos": ["acme/widgets"]}, {})
    seed(config.state_db)
    src = Sources(config=config, running_version="0.14.0",
                  run=lambda a, **k: "", http_get=lambda u, t: None,
                  wall_clock=lambda: 5000.0)
    return App(auth=Auth("pw", boot_nonce="n"), sources=src, version="0.14.0",
               run=runner or (lambda a, **k: ""),
               audit=audit or (lambda action, detail: None))


def test_kill_argv_is_exactly_alissa_tmux_kill(tmp_path):
    seen = []
    app = make_app(tmp_path, runner=lambda argv, **kw: seen.append(list(argv)))
    ok, _ = app.kill_session(SESSION)
    assert ok is True
    assert seen == [["alissa", "tmux", "kill", SESSION]]
    # never the server, never a raw tmux command
    assert seen[0][1:3] == ["tmux", "kill"]
    assert "kill-server" not in seen[0]


def test_kill_rejects_unsafe_names_without_running(tmp_path):
    seen = []
    app = make_app(tmp_path, runner=lambda argv, **kw: seen.append(list(argv)))
    for bad in ("", None, "has space", "-rf", "a;b", "x/../y"):
        ok, msg = app.kill_session(bad)
        assert ok is False and msg == "invalid session name"
    assert seen == []  # nothing was ever executed


def test_kill_reports_command_error(tmp_path):
    def boom(argv, **kw):
        raise CommandError(argv, 1, "no such session")

    app = make_app(tmp_path, runner=boom)
    ok, msg = app.kill_session(SESSION)
    assert ok is False and "no such session" in msg


def test_kill_audits(tmp_path):
    lines = []
    app = make_app(tmp_path, audit=lambda action, detail: lines.append((action, detail)))
    app.kill_session(SESSION)
    assert lines[0][0] == "kill"
    assert lines[0][1]["ok"] is True
    # the trail records WHOSE process tree was killed -- this daemon's reviewer
    # session, or another daemon's worker (both are killable by design)
    assert lines[0][1]["managed"] is True
    app.kill_session("develop-acme-widgets-i7-a1")
    assert lines[1][1]["managed"] is False
    app.kill_session("-rf")
    assert lines[2][1]["ok"] is False


# -- App.retry: an UPDATE, delegated ---------------------------------------

def test_retry_ages_ledger_row(tmp_path):
    app = make_app(tmp_path)
    ok, msg = app.retry("acme/widgets", 16, 1)
    assert ok is True and msg == "retried"
    with State(app.sources.config.state_db) as st:
        rows = st.read_spawns()
        assert len(rows) == 1  # row kept (UPDATE, not DELETE)
        assert rows[0]["spawned_at"] == 5000 - STALE_ROUND_SECONDS - 60


def test_retry_bad_shape_rejected(tmp_path):
    app = make_app(tmp_path)
    assert app.retry(None, 16, 1)[0] is False
    assert app.retry("not-a-slug", 16, 1)[0] is False
    assert app.retry("acme/widgets", "notanumber", 1)[0] is False
    assert app.retry("acme/widgets", 16, None)[0] is False


def test_retry_audits_with_the_ledger_outcome(tmp_path):
    lines = []
    app = make_app(tmp_path, audit=lambda action, detail: lines.append((action, detail)))
    app.retry("acme/widgets", 16, 1)
    app.retry("acme/widgets", 999, 1)  # no ledger row
    assert lines[0] == ("retry", {"ok": True, "outcome": "retried",
                                  "repo_slug": "acme/widgets",
                                  "number": 16, "round": 1})
    # a genuinely absent row is audited as such -- distinct from a lost write
    assert lines[1][1]["ok"] is False
    assert lines[1][1]["outcome"] == "no ledger row"


# -- live server: end-to-end auth + CSRF gating ----------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture
def live(tmp_path):
    config = Config.build(tmp_path, {"repos": ["acme/widgets"]}, {})
    seed(config.state_db)
    with State(config.state_db) as st:
        st.record_snapshot(duration_ms=1, candidates=0, stages=[])

    def runner(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return "[]"
        return ""

    src = Sources(config=config, running_version="0.14.0",
                  run=runner, http_get=lambda u, t: None, wall_clock=lambda: 5000.0)
    app = App(auth=Auth("letmein", boot_nonce="fixed"), sources=src,
              version="0.14.0", run=runner, audit=lambda a, d: None)
    server = make_server(app, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", app
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _req(base, path, method="GET", data=None, headers=None):
    opener = urllib.request.build_opener(_NoRedirect)
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers=headers or {})
    try:
        resp = opener.open(r, timeout=5)
        return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def _login(base, passcode="letmein"):
    status, _, hdrs = _req(base, "/login", "POST",
                           data=f"passcode={passcode}".encode())
    cookie = None
    if "Set-Cookie" in hdrs:
        jar = http.cookies.SimpleCookie(hdrs["Set-Cookie"])
        if SESSION_COOKIE in jar:
            cookie = f"{SESSION_COOKIE}=" + jar[SESSION_COOKIE].value
    return status, cookie


def test_healthz_open(live):
    base, _ = live
    status, body, _ = _req(base, "/healthz")
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_api_requires_session(live):
    base, _ = live
    status, _, _ = _req(base, "/api/state")
    assert status == 401


def test_root_serves_the_gate_when_unauthenticated(live):
    base, _ = live
    status, body, _ = _req(base, "/")
    assert status == 200
    assert b'name="passcode"' in body
    assert b"csrf-token" not in body


def test_login_bad_then_good(live):
    base, _ = live
    bad_status, bad_cookie = _login(base, "wrong")
    assert bad_status == 401 and bad_cookie is None
    good_status, cookie = _login(base)
    assert good_status == 303 and cookie is not None


def test_login_throttle_locks_out(live):
    """Repeated bad passcodes lock the console: 429 + Retry-After, and the
    CORRECT passcode is refused while locked."""
    base, app = live
    for _ in range(5):
        _login(base, "wrong")
    status, _, hdrs = _req(base, "/login", "POST", data=b"passcode=wrong")
    assert status == 429
    assert int(hdrs["Retry-After"]) > 0
    status, cookie = _login(base)  # the right passcode, still locked
    assert status == 429 and cookie is None


def test_authed_flow_and_csrf_gate(live):
    base, app = live
    _, cookie = _login(base)
    assert cookie is not None

    # dashboard carries the csrf token
    status, body, _ = _req(base, "/", headers={"Cookie": cookie})
    assert status == 200
    csrf = re.search(rb'csrf-token" content="([0-9a-f]+)"', body).group(1).decode()

    # data endpoint now authorised
    status, body, _ = _req(base, "/api/state", headers={"Cookie": cookie})
    assert status == 200 and "tiles" in json.loads(body)

    payload = json.dumps({"session": SESSION}).encode()
    # action without CSRF -> 403 even WITH a valid session cookie
    status, _, _ = _req(base, "/action/kill", "POST", payload,
                        {"Cookie": cookie, "Content-Type": "application/json"})
    assert status == 403

    # action without session -> 401
    status, _, _ = _req(base, "/action/kill", "POST", payload,
                        {"Content-Type": "application/json"})
    assert status == 401

    # action with session + CSRF -> 200
    status, body, _ = _req(base, "/action/kill", "POST", payload,
                           {"Cookie": cookie, "X-CSRF-Token": csrf,
                            "Content-Type": "application/json"})
    assert status == 200 and json.loads(body)["ok"] is True


def test_retry_action_gated_and_performed(live):
    base, app = live
    _, cookie = _login(base)
    _, body, _ = _req(base, "/", headers={"Cookie": cookie})
    csrf = re.search(rb'csrf-token" content="([0-9a-f]+)"', body).group(1).decode()
    payload = json.dumps({"repo_slug": "acme/widgets", "number": 16,
                          "round": 1}).encode()

    # no CSRF -> rejected, and the ledger is untouched
    status, _, _ = _req(base, "/action/retry", "POST", payload, {"Cookie": cookie})
    assert status == 403
    with State(app.sources.config.state_db) as st:
        assert st.read_spawns()[0]["spawned_at"] != 5000 - STALE_ROUND_SECONDS - 60

    status, body, _ = _req(base, "/action/retry", "POST", payload,
                           {"Cookie": cookie, "X-CSRF-Token": csrf,
                            "Content-Type": "application/json"})
    assert status == 200 and json.loads(body)["ok"] is True
    with State(app.sources.config.state_db) as st:
        assert st.read_spawns()[0]["spawned_at"] == 5000 - STALE_ROUND_SECONDS - 60


def test_action_failure_is_400(live):
    base, _ = live
    _, cookie = _login(base)
    _, body, _ = _req(base, "/", headers={"Cookie": cookie})
    csrf = re.search(rb'csrf-token" content="([0-9a-f]+)"', body).group(1).decode()
    payload = json.dumps({"repo_slug": "acme/widgets", "number": 999,
                          "round": 1}).encode()
    status, body, _ = _req(base, "/action/retry", "POST", payload,
                           {"Cookie": cookie, "X-CSRF-Token": csrf,
                            "Content-Type": "application/json"})
    assert status == 400 and json.loads(body)["message"] == "no ledger row"


def test_logout_requires_csrf(live):
    """`/logout` mutates (expires the cookie), so it is CSRF-gated like the
    actions -- a cross-site POST cannot force-logout the operator."""
    base, _ = live
    _, cookie = _login(base)
    status, body, _ = _req(base, "/", headers={"Cookie": cookie})
    csrf = re.search(rb'csrf-token" content="([0-9a-f]+)"', body).group(1).decode()

    # logout WITHOUT a CSRF token -> 403, even with a valid session cookie
    status, _, hdrs = _req(base, "/logout", "POST", b"", {"Cookie": cookie})
    assert status == 403
    assert "Set-Cookie" not in hdrs  # cookie was NOT expired

    # logout WITH the CSRF token -> 200 and the cookie is expired
    status, _, hdrs = _req(base, "/logout", "POST", b"",
                           {"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200
    assert "Max-Age=0" in hdrs.get("Set-Cookie", "")


def test_security_headers_present(live):
    base, _ = live
    _, _, hdrs = _req(base, "/healthz")
    assert hdrs["X-Frame-Options"] == "DENY"
    assert hdrs["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in hdrs["Content-Security-Policy"]


def test_unknown_route_404(live):
    base, _ = live
    assert _req(base, "/nope")[0] == 404
    assert _req(base, "/action/nope", "POST", b"{}")[0] == 404


# -- request-body cap (unauth allocation guard) ----------------------------

def test_read_body_rejects_oversized():
    """An oversized Content-Length is refused unread (no big allocation before
    auth), and keep-alive is dropped since the body is left on the wire."""
    h = Handler.__new__(Handler)
    h.headers = {"Content-Length": str(_MAX_BODY + 1)}
    h.rfile = io.BytesIO(b"x" * (_MAX_BODY + 1))
    h.close_connection = False
    assert h._read_body() == b""
    assert h.close_connection is True

    # a normal small body is still read exactly
    h2 = Handler.__new__(Handler)
    h2.headers = {"Content-Length": "5"}
    h2.rfile = io.BytesIO(b"hello")
    h2.close_connection = False
    assert h2._read_body() == b"hello"
    assert h2.close_connection is False


def test_json_body_tolerates_junk():
    h = Handler.__new__(Handler)
    h.headers = {"Content-Length": "3"}
    h.rfile = io.BytesIO(b"{[}")
    h.close_connection = False
    assert h._json_body() == {}

    h2 = Handler.__new__(Handler)
    h2.headers = {"Content-Length": "2"}
    h2.rfile = io.BytesIO(b"[]")  # valid JSON, wrong shape
    h2.close_connection = False
    assert h2._json_body() == {}


# -- conditional Secure cookie ---------------------------------------------

def test_cookie_secure_flag_conditional():
    """`Secure` is off by default (localhost HTTP) and on when the app is
    configured for a TLS-terminated posture."""
    h = Handler.__new__(Handler)
    h.server = types.SimpleNamespace(app=types.SimpleNamespace(secure_cookie=False))
    assert h._cookie_secure() == ""
    assert "Secure" not in h._expire_cookie()

    h.server.app.secure_cookie = True
    assert h._cookie_secure() == "; Secure"
    assert "; Secure" in h._expire_cookie()


def test_login_cookie_secure_when_configured(tmp_path):
    """End to end: with secure_cookie the login Set-Cookie carries Secure."""
    config = Config.build(tmp_path, {"repos": ["acme/widgets"]}, {})
    src = Sources(config=config, running_version="0.14.0",
                  run=lambda a, **k: "[]", http_get=lambda u, t: None,
                  wall_clock=lambda: 5000.0)
    app = App(auth=Auth("letmein", boot_nonce="fixed"), sources=src,
              version="0.14.0", run=lambda a, **k: "", audit=lambda a, d: None,
              secure_cookie=True)
    server = make_server(app, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, _, hdrs = _req(f"http://127.0.0.1:{port}", "/login", "POST",
                          data=b"passcode=letmein")
        assert "Secure" in hdrs.get("Set-Cookie", "")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# -- fail-closed CLI boot --------------------------------------------------

def test_main_refuses_without_passcode(monkeypatch, capsys):
    monkeypatch.delenv("ALISSA_UI_PASSCODE", raising=False)
    rc = ui_main.main(["--port", "0"])
    assert rc == 2
    assert "refusing to start" in capsys.readouterr().err


def test_main_refuses_on_blank_passcode(monkeypatch, capsys):
    monkeypatch.setenv("ALISSA_UI_PASSCODE", "   ")
    rc = ui_main.main(["--port", "0"])
    assert rc == 2
    assert "refusing to start" in capsys.readouterr().err


def test_resolve_config_no_network(tmp_path, monkeypatch):
    monkeypatch.setenv("ALISSA_UI_PASSCODE", "x")
    import argparse
    ns = argparse.Namespace(workspace_root=tmp_path, config_path=None,
                            state_path=None)
    config = ui_main.resolve_config(ns)
    assert config.workspace_root == tmp_path.resolve()


def test_resolve_config_reads_the_daemons_file(tmp_path, monkeypatch):
    import argparse
    (tmp_path / "revloop.config.json").write_text(
        '{"round_cap": 4, "repos": ["acme/widgets"]}')
    ns = argparse.Namespace(workspace_root=tmp_path, config_path=None,
                            state_path=None)
    config = ui_main.resolve_config(ns)
    assert config.round_cap == 4 and config.repos == ("acme/widgets",)


def test_main_bad_config_returns_2(tmp_path, monkeypatch):
    monkeypatch.setenv("ALISSA_UI_PASSCODE", "x")
    # an unknown key is a config error -> exit 2, never a traceback
    (tmp_path / "revloop.config.json").write_text('{"nope": 1}')
    rc = ui_main.main(["--workspace-root", str(tmp_path)])
    assert rc == 2


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    (None, False), ("", False), ("0", False), ("nope", False),
])
def test_env_flag(value, expected):
    assert ui_main._env_flag(value) is expected


def test_main_secure_cookie_from_env(tmp_path, monkeypatch):
    """ALISSA_UI_SECURE_COOKIE flips the app's secure_cookie; unset is False."""
    monkeypatch.setenv("ALISSA_UI_PASSCODE", "letmein")
    monkeypatch.setenv("ALISSA_UI_SECURE_COOKIE", "1")

    captured = {}

    class FakeServer:
        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

        def server_close(self):
            pass

    def fake_make_server(app, host, port):
        captured["secure_cookie"] = app.secure_cookie
        return FakeServer()

    monkeypatch.setattr(ui_main, "make_server", fake_make_server)
    rc = ui_main.main(["--workspace-root", str(tmp_path), "--port", "0"])
    assert rc == 0
    assert captured["secure_cookie"] is True


def test_main_happy_path_wires_and_serves(tmp_path, monkeypatch, capsys):
    """main() resolves config, builds the app, and serves; a Ctrl-C unwinds
    cleanly through shutdown/close. make_server is stubbed so nothing binds."""
    monkeypatch.setenv("ALISSA_UI_PASSCODE", "letmein")
    monkeypatch.setenv(ui_main.ENV_LOG, str(tmp_path / "daemon.log"))
    monkeypatch.delenv("ALISSA_UI_SECURE_COOKIE", raising=False)

    events = []

    class FakeServer:
        def serve_forever(self):
            events.append("serve")
            raise KeyboardInterrupt

        def shutdown(self):
            events.append("shutdown")

        def server_close(self):
            events.append("close")

    def fake_make_server(app, host, port):
        events.append(("make", host, port))
        assert app.sources.log_path == tmp_path / "daemon.log"
        assert app.secure_cookie is False
        return FakeServer()

    monkeypatch.setattr(ui_main, "make_server", fake_make_server)
    rc = ui_main.main(["--workspace-root", str(tmp_path),
                       "--host", "0.0.0.0", "--port", "9999"])
    assert rc == 0
    assert ("make", "0.0.0.0", 9999) in events
    assert events[-2:] == ["shutdown", "close"]
    assert "serving on" in capsys.readouterr().out


def test_default_port_does_not_collide_with_the_devloop_console(tmp_path, monkeypatch):
    """Both daemons run on one machine; two sidecars on 8787 would be a boot
    failure, so the reviewer console defaults elsewhere."""
    monkeypatch.setenv("ALISSA_UI_PASSCODE", "letmein")
    seen = {}

    class FakeServer:
        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

        def server_close(self):
            pass

    def fake_make_server(app, host, port):
        seen["port"] = port
        return FakeServer()

    monkeypatch.setattr(ui_main, "make_server", fake_make_server)
    ui_main.main(["--workspace-root", str(tmp_path)])
    assert seen["port"] == ui_main.DEFAULT_PORT != 8787
