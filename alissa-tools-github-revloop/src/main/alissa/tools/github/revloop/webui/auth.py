"""Fail-closed authentication for the reviewer console.

The whole surface is a single operator passcode (`ALISSA_UI_PASSCODE`). There
is no user store: the sidecar is a personal operator tool, not a multi-tenant
service. The security posture (identical to the devloop console's contract --
one operator learns one console):

* **Fail-closed.** No passcode in the environment -> the server refuses to
  start (`require_passcode` raises). There is deliberately no "unauthenticated"
  fallback -- an operator console with kill/retry actions must never boot open.
* **Constant-time passcode compare.** `hmac.compare_digest`, so a wrong guess
  leaks no timing signal about how many leading characters matched.
* **HMAC-signed session cookie.** The signing key is derived from the passcode
  AND a per-boot nonce, so (a) a stolen cookie cannot be forged without the
  passcode, and (b) every restart invalidates all outstanding sessions (the
  nonce changes) -- a cheap, deliberate "reboot logs everyone out".
* **CSRF token bound to the session.** Every state-changing POST must present a
  token that is itself an HMAC over the session cookie, so a cross-site form
  post (which cannot read the cookie to derive the token) is rejected even
  though the browser would attach the cookie.
* **Login throttle.** A small sliding-window lockout blunts online guessing.

Nothing here is a substitute for network isolation -- the sidecar is meant to
sit behind localhost / a private network -- but it means an exposed port is not
an instant takeover.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Callable, Mapping

# The environment variable that carries the operator passcode. Unset or
# whitespace-only means "no passcode" -> fail-closed (see require_passcode).
# Deliberately the same name the devloop console uses: an operator running both
# sidecars on one machine sets one secret, and the two are separate processes
# with separate boot nonces regardless.
ENV_PASSCODE = "ALISSA_UI_PASSCODE"

# The session cookie name. Namespaced per console so a browser holding both
# sidecars open (same host, different ports -- cookies are NOT port-scoped)
# does not have one console's cookie overwrite the other's.
SESSION_COOKIE = "alissa_revloop_ui"

# Sessions live half a day by default; a signed cookie past its issue age is
# rejected even though its signature still verifies.
DEFAULT_SESSION_TTL = 12 * 60 * 60


class PasscodeUnset(RuntimeError):
    """Raised when ALISSA_UI_PASSCODE is missing or whitespace-only. The server
    turns this into a refusal to start (fail-closed), never a warning."""


def require_passcode(environ: Mapping[str, str]) -> str:
    """The passcode, or raise PasscodeUnset. `environ` is passed in (not read
    from os.environ here) so the boot check is a pure function the tests drive
    directly. Whitespace-only counts as unset: a stray `ALISSA_UI_PASSCODE=` in
    a compose file must fail closed, not boot with an empty secret."""
    raw = environ.get(ENV_PASSCODE)
    if raw is None or not raw.strip():
        raise PasscodeUnset(
            f"{ENV_PASSCODE} is unset -- the reviewer console refuses to start "
            f"without a passcode (fail-closed). Set {ENV_PASSCODE} and retry."
        )
    return raw


class LoginThrottle:
    """A sliding-window login-attempt throttle. After `max_attempts` failures
    inside `window_seconds`, logins are locked for `lockout_seconds`. A success
    clears the record. Deliberately global (one operator, one console), so it
    is a lockout, not a per-IP heuristic that a proxy could smear."""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: float = 300.0,
        lockout_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._clock = clock
        self._failures: list[float] = []
        self._locked_until = 0.0

    def locked(self) -> bool:
        return self._clock() < self._locked_until

    def retry_after(self) -> float:
        """Seconds until logins are accepted again (0.0 when not locked)."""
        return max(0.0, self._locked_until - self._clock())

    def record_failure(self) -> None:
        now = self._clock()
        self._failures = [t for t in self._failures if now - t < self._window]
        self._failures.append(now)
        if len(self._failures) >= self._max:
            self._locked_until = now + self._lockout
            self._failures = []

    def record_success(self) -> None:
        self._failures = []
        self._locked_until = 0.0


class Auth:
    """Passcode verification, session issue/verify, and CSRF binding.

    The signing key is `sha256(boot_nonce : passcode)`; changing either
    invalidates every outstanding session, which is exactly the reboot/rotate
    behaviour we want. All comparisons are constant-time.
    """

    def __init__(
        self,
        passcode: str,
        *,
        boot_nonce: str | None = None,
        session_ttl: float = DEFAULT_SESSION_TTL,
        clock: Callable[[], float] = time.time,
        throttle: LoginThrottle | None = None,
    ) -> None:
        self._passcode = passcode
        self.boot_nonce = boot_nonce or secrets.token_hex(16)
        self._key = hashlib.sha256(
            f"{self.boot_nonce}:{passcode}".encode()
        ).digest()
        self._ttl = session_ttl
        self._clock = clock
        self.throttle = throttle if throttle is not None else LoginThrottle()

    # -- passcode / login --------------------------------------------------

    def verify_passcode(self, submitted: str) -> bool:
        return hmac.compare_digest(
            (submitted or "").encode(), self._passcode.encode()
        )

    def attempt_login(self, submitted: str) -> tuple[bool, str]:
        """Throttle-aware login. Returns (ok, reason) where reason is one of
        "ok", "locked", "bad". A locked console never even compares the
        passcode, so a lockout is not a timing oracle either."""
        if self.throttle.locked():
            return False, "locked"
        if self.verify_passcode(submitted):
            self.throttle.record_success()
            return True, "ok"
        self.throttle.record_failure()
        return False, "bad"

    # -- session cookie ----------------------------------------------------

    def _mac(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue_session(self) -> str:
        """A fresh signed session token: `<sid>.<issued_at>.<mac>`."""
        sid = secrets.token_hex(16)
        issued = int(self._clock())
        return self._sign(f"{sid}.{issued}")

    def _sign(self, payload: str) -> str:
        return f"{payload}.{self._mac(payload)}"

    def verify_session(self, token: str | None) -> bool:
        """True iff `token` is a well-formed, correctly-signed, unexpired
        session cookie. Any malformed input returns False, never raises."""
        if not token:
            return False
        payload, _, mac = token.rpartition(".")
        if not payload or not mac:
            return False
        if not hmac.compare_digest(self._mac(payload), mac):
            return False
        _, _, issued_str = payload.rpartition(".")
        try:
            issued = int(issued_str)
        except ValueError:
            return False
        return 0 <= (self._clock() - issued) <= self._ttl

    # -- CSRF binding ------------------------------------------------------

    def csrf_token(self, session_token: str) -> str:
        """A CSRF token bound to a specific session cookie. A cross-site form
        cannot read the cookie, so it cannot compute this -- the token is the
        proof that the request originated from a page WE served."""
        return self._mac(f"csrf:{session_token}")

    def verify_csrf(self, session_token: str | None, csrf: str | None) -> bool:
        if not session_token or not csrf:
            return False
        return hmac.compare_digest(self.csrf_token(session_token), csrf)
