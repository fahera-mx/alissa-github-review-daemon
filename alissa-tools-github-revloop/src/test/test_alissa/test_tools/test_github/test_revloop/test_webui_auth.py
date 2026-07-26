"""Auth: fail-closed passcode gate, constant-time login, HMAC-signed sessions,
CSRF binding, and the login throttle."""

from __future__ import annotations

import pytest

from alissa.tools.github.revloop.webui.auth import (
    SESSION_COOKIE,
    Auth,
    LoginThrottle,
    PasscodeUnset,
    require_passcode,
)


class Clock:
    """A hand-cranked monotonic clock for deterministic throttle/ttl tests."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


# -- fail-closed passcode gate --------------------------------------------

def test_require_passcode_missing_refuses():
    with pytest.raises(PasscodeUnset):
        require_passcode({})


def test_require_passcode_blank_refuses():
    with pytest.raises(PasscodeUnset):
        require_passcode({"ALISSA_UI_PASSCODE": "   "})


def test_require_passcode_present_returns_value():
    assert require_passcode({"ALISSA_UI_PASSCODE": "s3cret"}) == "s3cret"


def test_cookie_name_is_console_specific():
    """Cookies are not port-scoped, so the reviewer console must not reuse the
    devloop console's cookie name -- one would clobber the other's session on a
    host running both sidecars."""
    assert SESSION_COOKIE == "alissa_revloop_ui"


# -- passcode compare ------------------------------------------------------

def test_verify_passcode():
    auth = Auth("hunter2", boot_nonce="n")
    assert auth.verify_passcode("hunter2") is True
    assert auth.verify_passcode("hunter3") is False
    assert auth.verify_passcode("") is False


# -- signed sessions -------------------------------------------------------

def test_session_roundtrip():
    auth = Auth("pw", boot_nonce="n")
    token = auth.issue_session()
    assert auth.verify_session(token) is True


def test_tampered_session_rejected():
    auth = Auth("pw", boot_nonce="n")
    token = auth.issue_session()
    assert auth.verify_session(token[:-1] + ("0" if token[-1] != "0" else "1")) is False
    assert auth.verify_session("garbage") is False
    assert auth.verify_session("") is False
    assert auth.verify_session(None) is False


def test_session_bound_to_nonce_and_passcode():
    token = Auth("pw", boot_nonce="n1").issue_session()
    # different boot nonce -> different key -> old cookie invalid (reboot logout)
    assert Auth("pw", boot_nonce="n2").verify_session(token) is False
    # different passcode -> invalid too
    assert Auth("other", boot_nonce="n1").verify_session(token) is False


def test_session_expires():
    clock = Clock()
    auth = Auth("pw", boot_nonce="n", session_ttl=100, clock=clock)
    token = auth.issue_session()
    clock.tick(50)
    assert auth.verify_session(token) is True
    clock.tick(100)  # now 150s old, ttl 100
    assert auth.verify_session(token) is False


def test_session_with_unparseable_issue_time_rejected():
    auth = Auth("pw", boot_nonce="n")
    forged = auth._sign("sid.notanumber")
    assert auth.verify_session(forged) is False


# -- CSRF binding ----------------------------------------------------------

def test_csrf_bound_to_session():
    auth = Auth("pw", boot_nonce="n")
    a = auth.issue_session()
    b = auth.issue_session()
    csrf_a = auth.csrf_token(a)
    assert auth.verify_csrf(a, csrf_a) is True
    assert auth.verify_csrf(b, csrf_a) is False  # other session's token
    assert auth.verify_csrf(a, "nope") is False
    assert auth.verify_csrf(None, csrf_a) is False
    assert auth.verify_csrf(a, None) is False


# -- login throttle --------------------------------------------------------

def test_throttle_locks_after_max():
    clock = Clock()
    thr = LoginThrottle(max_attempts=3, lockout_seconds=60, clock=clock)
    assert thr.locked() is False
    for _ in range(3):
        thr.record_failure()
    assert thr.locked() is True
    assert thr.retry_after() == pytest.approx(60)
    clock.tick(61)
    assert thr.locked() is False


def test_throttle_window_forgets_old_failures():
    clock = Clock()
    thr = LoginThrottle(max_attempts=3, window_seconds=100, clock=clock)
    thr.record_failure()
    thr.record_failure()
    clock.tick(200)  # first two age out of the window
    thr.record_failure()
    assert thr.locked() is False  # only one failure inside the window


def test_throttle_success_resets():
    thr = LoginThrottle(max_attempts=3)
    thr.record_failure()
    thr.record_failure()
    thr.record_success()
    thr.record_failure()
    assert thr.locked() is False


def test_attempt_login_flow():
    clock = Clock()
    thr = LoginThrottle(max_attempts=2, lockout_seconds=30, clock=clock)
    auth = Auth("pw", boot_nonce="n", throttle=thr)
    assert auth.attempt_login("pw") == (True, "ok")
    assert auth.attempt_login("bad") == (False, "bad")
    assert auth.attempt_login("bad") == (False, "bad")
    # now locked -- even the CORRECT passcode is refused while locked
    ok, reason = auth.attempt_login("pw")
    assert ok is False and reason == "locked"
