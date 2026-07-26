"""Per-session resource accounting off `/proc`, plus workspace disk usage.

The sessions panel wants CPU% and RSS for each managed tmux session. tmux hands
us a pane PID; the real work runs in that pane's child tree (a shell, the agent,
its subprocesses), so we sum over the whole tree rooted at the pane PID.

Two deliberate properties:

* **Sample-free CPU.** Instantaneous CPU% needs two `/proc` reads spaced apart;
  a dashboard endpoint polled every ~10s should not block sampling. Instead we
  report the *cumulative average* since the pane started:
  `100 * (utime+stime)/CLK_TCK / (uptime - starttime/CLK_TCK)`. It answers "how
  hot has this session run over its life", which is the operator question, and
  needs a single read.
* **Vanished-PID tolerant.** Between listing `/proc` and reading a PID's `stat`,
  the process can exit. Every read that fails (gone, permission, malformed) is
  skipped -- a disappearing reviewer contributes nothing and never raises.

The `/proc` root is a parameter so the parsing is testable against a crafted
fixture directory (including a PID that is indexed but whose `stat` was deleted
mid-walk -- the vanished-PID case the mutation bar pins).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

# Field offsets INTO the tail of /proc/<pid>/stat (everything after the
# "(comm)" field, which itself is field 3 == index 0 of the tail). See
# proc(5): utime=14, stime=15, starttime=22, rss=24 -> tail indices below.
_TAIL_PPID = 1
_TAIL_UTIME = 11
_TAIL_STIME = 12
_TAIL_STARTTIME = 19
_TAIL_RSS = 21
_TAIL_MIN_LEN = 22


def read_stat(proc_root: "str | os.PathLike[str]", pid: int) -> "dict | None":
    """Parse one `/proc/<pid>/stat`, or None if the PID has vanished or the
    line is malformed. The `comm` field can contain spaces and parentheses, so
    we split on the LAST ')' before tokenising -- the canonical safe parse."""
    try:
        data = (Path(proc_root) / str(pid) / "stat").read_text()
    except (OSError, ValueError):
        return None
    rparen = data.rfind(")")
    if rparen == -1:
        return None
    tail = data[rparen + 1:].split()
    if len(tail) < _TAIL_MIN_LEN:
        return None
    try:
        return {
            "pid": int(pid),
            "ppid": int(tail[_TAIL_PPID]),
            "cpu_ticks": int(tail[_TAIL_UTIME]) + int(tail[_TAIL_STIME]),
            "starttime": int(tail[_TAIL_STARTTIME]),
            "rss_pages": int(tail[_TAIL_RSS]),
        }
    except (ValueError, IndexError):
        return None


def read_uptime(proc_root: "str | os.PathLike[str]") -> "float | None":
    try:
        return float((Path(proc_root) / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def build_index(
    proc_root: "str | os.PathLike[str]",
) -> "tuple[dict[int, list[int]], dict[int, dict]]":
    """Scan `/proc` once: return (ppid -> [child pids], pid -> stat). Numeric
    entries only; anything that vanishes mid-scan is silently skipped."""
    children: dict[int, list[int]] = {}
    stats: dict[int, dict] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return children, stats
    for name in entries:
        if not name.isdigit():
            continue
        st = read_stat(proc_root, int(name))
        if st is None:
            continue
        stats[st["pid"]] = st
        children.setdefault(st["ppid"], []).append(st["pid"])
    return children, stats


def tree_pids(root_pid: int, children: "dict[int, list[int]]") -> "set[int]":
    """Every PID in the subtree rooted at `root_pid` (inclusive). Iterative and
    cycle-guarded -- a corrupt ppid loop can never spin forever."""
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, ()))
    return seen


def tree_usage(
    root_pid: "int | None",
    *,
    proc_root: "str | os.PathLike[str]" = "/proc",
    index: "tuple[dict[int, list[int]], dict[int, dict]] | None" = None,
) -> dict:
    """CPU% (cumulative average since the pane started) and summed RSS for the
    whole process tree under `root_pid`. Returns None/None when the root has
    already vanished -- "unknown", the same thing the caller shows for a
    session with no pane at all, so the column reads consistently.

    `index` is the (children, stats) pair from `build_index`. Pass it when
    accounting SEVERAL trees from one snapshot of `/proc`: the index is
    identical for every session in one dashboard build, and rebuilding it per
    session makes the walk O(sessions x processes) -- on a busy reviewer host
    thousands of `stat` reads every poll, stolen from the reviewer agents the
    console exists to watch. Omitted, it is built once for this call.
    """
    empty = {"pids": 0, "rss_bytes": None, "cpu_percent": None}
    if root_pid is None:
        return empty
    children, stats = index if index is not None else build_index(proc_root)
    if root_pid not in stats:
        return empty
    uptime = read_uptime(proc_root)
    rss_pages = 0
    cpu_ticks = 0
    counted = 0
    for pid in tree_pids(root_pid, children):
        st = stats.get(pid)
        if st is None:
            continue
        counted += 1
        rss_pages += st["rss_pages"]
        cpu_ticks += st["cpu_ticks"]
    cpu_percent = None
    if uptime is not None and _CLK_TCK:
        wall = uptime - stats[root_pid]["starttime"] / _CLK_TCK
        if wall > 0:
            cpu_seconds = cpu_ticks / _CLK_TCK
            cpu_percent = round(100.0 * cpu_seconds / wall, 1)
    return {
        "pids": counted,
        "rss_bytes": rss_pages * _PAGE_SIZE,
        "cpu_percent": cpu_percent,
    }


def disk_usage(path: "str | os.PathLike[str]") -> "dict | None":
    """Workspace volume usage for the stat tile, or None if the path is
    unreadable. Percent is used/total, rounded -- the meter the console fills."""
    try:
        usage = shutil.disk_usage(os.fspath(path))
    except OSError:
        return None
    percent = round(100.0 * usage.used / usage.total, 1) if usage.total else None
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "percent": percent,
    }
