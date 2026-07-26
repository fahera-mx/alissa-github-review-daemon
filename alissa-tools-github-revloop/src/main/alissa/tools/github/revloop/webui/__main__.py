"""CLI entry point: `alissa-revloop-ui` (or `python -m ...revloop.webui`).

The sidecar reads the SAME config the daemon resolved (so its echo panel is
truthful) but never runs the daemon's preflight -- it makes no `gh api user`
identity call, holds no GitHub token of its own, and spends GitHub budget only
through the two cached checks in `sources`. It is fail-closed on the passcode:
`ALISSA_UI_PASSCODE` unset -> exit before the socket ever binds.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..config import Config, load_config_file, resolve_config_path
from ..version import version
from .auth import Auth, PasscodeUnset, require_passcode
from .server import App, make_server
from .sources import Sources

# The default bind port. Deliberately NOT the devloop console's 8787: the two
# daemons routinely run on one machine (they are the two halves of the same
# loop), and two sidecars fighting over one port is a boot failure an operator
# would have to debug.
DEFAULT_PORT = 8788

# The log file to tail when `--log-file` is not given.
ENV_LOG = "ALISSA_REVLOOP_LOG"


def _env_flag(value: "str | None") -> bool:
    """A truthy env flag: 1/true/yes/on (case-insensitive). Unset or anything
    else is False -- fail-safe, so a stray value never silently flips a knob."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alissa-revloop-ui",
        description="Reviewer console: a stdlib-only operator dashboard sidecar "
        "for the alissa-revloop daemon. Reads the daemon's local state (poll "
        "snapshots, spawn ledger, escalations, pings), tmux/proc, and two cached "
        "checks; renders live state and offers kill/retry-now actions.",
        epilog="Requires ALISSA_UI_PASSCODE (fail-closed). Reads the same "
        "config file the daemon uses so its echo panel is truthful.",
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {version.value}")
    p.add_argument("--host", default="127.0.0.1", metavar="ADDR",
                   help="bind address (default: 127.0.0.1 -- localhost only)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, metavar="PORT",
                   help=f"bind port (default: {DEFAULT_PORT})")
    p.add_argument("--workspace-root", type=Path, default=None, metavar="PATH",
                   help="the workspace whose daemon state to render "
                   "(default: current directory)")
    p.add_argument("-c", "--config-path", "--config", dest="config_path",
                   type=Path, default=None, metavar="PATH",
                   help="config file; without it, ./revloop.config.json then "
                   "<workspace-root>/revloop.config.json, else defaults only")
    p.add_argument("--state-path", type=Path, default=None, metavar="PATH",
                   help="override the state.db location (default: from config)")
    p.add_argument("--log-file", type=Path, default=None, metavar="PATH",
                   help="daemon log file to tail in the console "
                   f"(default: ${ENV_LOG}, else none)")
    return p


def resolve_config(args: argparse.Namespace) -> Config:
    """Config exactly as the daemon resolves it (discovery + layers), minus the
    preflight -- no network, no gh identity."""
    workspace_root = args.workspace_root or Path.cwd()
    path = resolve_config_path(args.config_path, workspace_root)
    file_data = load_config_file(path) if path else {}
    overrides = {"state_path": args.state_path}
    return Config.build(workspace_root, file_data, overrides)


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        passcode = require_passcode(os.environ)
    except PasscodeUnset as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    try:
        config = resolve_config(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    log_file = args.log_file
    if log_file is None:
        env_log = os.environ.get(ENV_LOG)
        log_file = Path(env_log) if env_log else None

    auth = Auth(passcode)
    sources = Sources(
        config=config, running_version=version.value, log_path=log_file
    )
    # Add `Secure` to the session cookie when the console sits behind TLS
    # termination (reverse proxy). Off by default for the localhost-HTTP posture.
    secure_cookie = _env_flag(os.environ.get("ALISSA_UI_SECURE_COOKIE"))
    app = App(
        auth=auth, sources=sources, version=version.value,
        secure_cookie=secure_cookie,
    )
    server = make_server(app, args.host, args.port)

    print(
        f"alissa-revloop-ui {version.value} -- serving on "
        f"http://{args.host}:{args.port} (passcode required); "
        f"watching {config.workspace_root}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
