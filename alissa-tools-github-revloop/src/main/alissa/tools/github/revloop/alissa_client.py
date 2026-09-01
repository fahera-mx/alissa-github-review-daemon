"""Alissa REST access — the one write loop telemetry needs (issue #112).

This is the SECOND Alissa adapter in the package, and the split is deliberate.
`alissa.py` shells out to the `alissa` CLI, which is the daemon's established
Alissa idiom for everything it does today (the review-task search, the tmux
queue, the CR6 envelope reads). Loop telemetry needs a thing that idiom cannot
supply: the CLI has no loop-events command, and it cannot attach the actor
identity `POST /v1/loop-events` keys its rows by — the API stores events under
the token's principal user, which is exactly the credential this daemon's
`ALISSA_API_TOKEN` already carries.

So the one write goes over the REST API directly, in the shape devloop's
`alissa_client.py` adopted (PR #91 there): stdlib `urllib` only — the
distribution ships no third-party runtime dependency and this must not be the
change that adds one — a bearer token from the environment, bounded timeouts,
and errors classified into a small taxonomy instead of leaking raw urllib
exceptions. The CLI adapter stays untouched for everything else.

The caller here is a BEST-EFFORT emitter (`loop_events`): its answer to every
bucket is the same — warn once and let the pass complete — so the taxonomy
exists for the log line, which should say "your token is wrong" (permanent,
operator-fixable) differently from "the API blinked".
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://api.alissa.app"

# The env var the `alissa` CLI itself reads, so a daemon whose CLI is already
# authenticated needs no second secret.
ENV_TOKEN = "ALISSA_API_TOKEN"

# The ingest cap: POST /v1/loop-events takes 1-200 events per call. The
# EMITTER splits batches at this bound; the client refuses an oversized one
# rather than silently posting a request the API will 400.
MAX_EVENTS_PER_POST = 200


class AlissaError(Exception):
    """Base of the taxonomy. `status` is 0 for a transport failure (there was
    no HTTP response to carry one)."""

    def __init__(self, status: int, detail: object, code: "str | None" = None):
        super().__init__(f"HTTP {status}: {detail}" if status else str(detail))
        self.status = status
        self.detail = detail
        self.code = code


class AlissaAuthError(AlissaError):
    """401/403, or no token at all. Permanent and operator-fixable — retrying
    it every pass only writes the same warning again."""


class AlissaTransient(AlissaError):
    """408/429/5xx and every transport failure (DNS, refused, timeout). The
    'the API blinked' bucket — re-emission next pass is the retry, and the
    deterministic dedupe keys are what make it harmless."""


class AlissaClient:
    """The one write, with the transport hidden behind the taxonomy.

    Reads `ALISSA_API_TOKEN` from the environment; `base` defaults to the
    public API. Both are constructor arguments so a test never needs the
    network and an operator can point a daemon at another deployment
    (`alissa_endpoint` in the config)."""

    def __init__(
        self,
        token: "str | None" = None,
        base: "str | None" = None,
        *,
        timeout: int = 30,
    ):
        self.base = (base or DEFAULT_ENDPOINT).rstrip("/")
        self._token = token if token is not None else os.environ.get(ENV_TOKEN)
        self._timeout = timeout

    def _request(self, path: str, payload: dict) -> object:
        """One POST. Every failure leaves as a taxonomy exception — the caller
        never sees a raw urllib error or an HTTP status."""
        if not self._token:
            # No token at all is an auth condition, not a transport one: the
            # operator must set the env var. Fail the way a 401 would, so the
            # emitter's warning reads as permanent rather than transient.
            raise AlissaAuthError(0, f"{ENV_TOKEN} is not set")

        req = urllib.request.Request(
            f"{self.base}{path}",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise self._classify(exc) from None
        except urllib.error.URLError as exc:
            raise AlissaTransient(0, str(exc.reason)) from None
        except (TimeoutError, OSError) as exc:  # pragma: no cover - defence
            # A socket timeout on the READ does not arrive as URLError.
            raise AlissaTransient(0, str(exc)) from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            # A 2xx that is not JSON is a contract violation, not a retry
            # signal -- but it must not escape as a bare ValueError either,
            # because the emitter catches AlissaError and nothing else.
            raise AlissaError(200, f"response was not JSON ({exc})") from None

    @staticmethod
    def _classify(exc: "urllib.error.HTTPError") -> AlissaError:
        """Map an HTTP error onto the taxonomy. The API sends JSON error
        bodies (`{"error": CODE, "message": ...}`); the code rides along when
        present, but classification keys on the STATUS — codes are advisory,
        statuses are the contract."""
        detail: object = exc.read().decode("utf-8", "replace")
        code: "str | None" = None
        try:
            parsed = json.loads(detail)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            detail = parsed
            raw_code = parsed.get("error")
            code = raw_code if isinstance(raw_code, str) else None

        status = exc.code
        if status in (401, 403):
            return AlissaAuthError(status, detail, code)
        if status in (408, 429) or 500 <= status <= 599:
            return AlissaTransient(status, detail, code)
        return AlissaError(status, detail, code)

    def post_loop_events(self, events: "list[dict]") -> dict:
        """Ingest one batch of loop events (`POST /v1/loop-events`).

        The API is idempotent on `(user, dedupeKey)`, so re-posting a batch —
        which is exactly what the emitter does after a failed pass — lands as
        silent duplicates, never as errors or overwrites. Returns the API's
        `{"accepted": N, "duplicates": M}` payload (empty dict when the body
        was empty), for the caller's debug line.

        An oversized batch is refused HERE, loudly: the API fails the whole
        call at >200 events, and the emitter owns the splitting, so reaching
        this guard is a code defect rather than an operational condition.
        """
        if len(events) > MAX_EVENTS_PER_POST:
            raise ValueError(
                f"post_loop_events takes at most {MAX_EVENTS_PER_POST} events "
                f"per call, got {len(events)} — the emitter must split"
            )
        payload = self._request("/v1/loop-events", {"events": events})
        return payload if isinstance(payload, dict) else {}
