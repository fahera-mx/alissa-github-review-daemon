"""Per-session resource accounting off `/proc`, plus workspace disk and
container-memory usage.

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

Per-session accounting cannot answer the question a platform memory graph
asks. A Railway-style flat multi-GB plateau with zero reviewer sessions is
CONTAINER-wide, and the per-pane sums (tens of MB) say nothing about it, so
`cgroup_memory` reads the container's own cgroup v2 charge and splits it into
the three numbers that settle "is that a leak?": what the container is charged
for, what is really process memory, and what is page cache the kernel would
drop the moment anything asked for it. `top_procs` is the same question one
level down -- the whole `/proc`, not one pane tree -- so a charge that IS
resident can be attributed to a process without shelling into the container.
Both follow the module's conventions: roots are parameters, every read
degrades to None instead of raising (a dev laptop or macOS has no cgroup v2
and must still render the console), and neither samples.
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

# The cgroup v2 files the memory tile reads, and the `memory.stat` keys it
# keeps. Deliberately a fixed, small set: `memory.stat` carries ~40 keys and
# the tile answers one question, so parsing the rest would be payload we never
# render. `anon` is process memory, `file` is page cache, `slab_reclaimable` is
# kernel cache the shrinkers can free -- together they are the split between a
# real footprint and a charge the kernel would give back under pressure.
_CGROUP_CURRENT = "memory.current"
_CGROUP_STAT = "memory.stat"
_CGROUP_STAT_KEYS = (
    "anon",
    "file",
    "inactive_file",
    "shmem",
    "slab_reclaimable",
    "slab_unreclaimable",
)


def read_stat(proc_root: "str | os.PathLike[str]", pid: int) -> "dict | None":
    """Parse one `/proc/<pid>/stat`, or None if the PID has vanished or the
    line is malformed. The `comm` field can contain spaces and parentheses, so
    we split on the LAST ')' before tokenising -- the canonical safe parse.

    `comm` is carried in the row (between the FIRST '(' and that last ')', the
    mirror of the same parse) purely for the top-process list: the tree sums
    need only the numbers, but a top-5 by RSS that shows bare PIDs tells an
    operator nothing about what is holding the memory.
    """
    try:
        data = (Path(proc_root) / str(pid) / "stat").read_text()
    except (OSError, ValueError):
        return None
    rparen = data.rfind(")")
    lparen = data.find("(")
    if rparen == -1 or lparen == -1 or lparen > rparen:
        return None
    tail = data[rparen + 1:].split()
    if len(tail) < _TAIL_MIN_LEN:
        return None
    try:
        return {
            "pid": int(pid),
            "comm": data[lparen + 1:rparen],
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


def top_procs(
    n: int = 5,
    *,
    proc_root: "str | os.PathLike[str]" = "/proc",
    index: "tuple[dict[int, list[int]], dict[int, dict]] | None" = None,
) -> "list[dict]":
    """The n biggest processes on the HOST by RSS: [{pid, comm, rss_bytes}],
    largest first.

    Deliberately the whole `/proc` and not a pane tree: the memory tile's
    question is container-wide, so when the charge really is resident the
    operator needs to see whatever is holding it -- which is routinely not a
    reviewer session at all (the daemon itself, a stray build, the shell that
    started everything).

    `index` is the same (children, stats) pair `tree_usage` takes, so a
    dashboard build that already scanned `/proc` for its session table ranks
    processes off that ONE snapshot instead of walking every PID twice.
    Vanished PIDs never appear: `build_index` has already dropped them, and a
    process that exits after the scan simply ranks with its last-known RSS.

    Ties break on PID so the order is total -- an equal-RSS pair must not
    reshuffle between polls and make a still list look like it is churning.
    """
    if n <= 0:
        return []
    _, stats = index if index is not None else build_index(proc_root)
    ranked = sorted(stats.values(), key=lambda st: (-st["rss_pages"], st["pid"]))
    return [
        {
            "pid": st["pid"],
            "comm": st.get("comm") or "?",
            "rss_bytes": st["rss_pages"] * _PAGE_SIZE,
        }
        for st in ranked[:n]
    ]


def _read_int_file(path: Path) -> "int | None":
    """One cgroup scalar file as an int, or None when it is absent, unreadable
    or not a number. cgroup v2 writes the literal `max` in some files, which is
    exactly the malformed case this returns None for."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _read_stat_keys(path: Path, keys: "tuple[str, ...]") -> "dict[str, int | None]":
    """The requested `key value` lines of a cgroup `*.stat` file. Every key is
    present in the result whether or not the file had it, so the payload shape
    never depends on the kernel version -- a missing or unparseable key is
    None, the same thing the whole-file failure produces."""
    out: "dict[str, int | None]" = {key: None for key in keys}
    try:
        text = path.read_text()
    except (OSError, ValueError):
        return out
    wanted = set(keys)
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name not in wanted:
            continue
        try:
            out[name] = int(value.strip())
        except ValueError:
            out[name] = None
    return out


def _sum_or_none(*values: "int | None") -> "int | None":
    """Sum only when EVERY part is known. A partial sum would be reported as a
    whole bucket and read as a smaller cache than the container really holds --
    an unknown number must stay unknown rather than become a wrong one."""
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def cgroup_memory(
    cgroup_root: "str | os.PathLike[str]" = "/sys/fs/cgroup",
) -> dict:
    """The container's own memory charge, split for the operator.

    Answers the question a platform memory graph raises and cannot settle: a
    flat multi-GB plateau is `charged` (cgroup v2 `memory.current`), but most
    of it is routinely cache the kernel would drop under pressure, not a leak.
    So we also report:

    * `resident` -- `anon`, the process memory that is really in use.
    * `reclaimable` -- `file` + `slab_reclaimable`, the page cache plus the
      shrinkable kernel caches: the "not really in use" bucket.

    The raw keys ride along for a spot check against an in-container
    `memory.stat`. Note the three summary numbers do NOT partition the charge
    (kernel stacks, pagetables, sockets are charged too, and `shmem` is counted
    inside `file` while behaving like anon memory) -- they are the three
    magnitudes an operator compares, not a balance sheet.

    Never raises. On a host without cgroup v2 -- a dev laptop, macOS, a v1
    cgroup tree -- every field is None and the console renders "unavailable";
    the root is a parameter so all of that is testable off a fixture dir.
    """
    root = Path(cgroup_root)
    charged = _read_int_file(root / _CGROUP_CURRENT)
    stat = _read_stat_keys(root / _CGROUP_STAT, _CGROUP_STAT_KEYS)
    out: "dict[str, int | None]" = {
        "charged": charged,
        "resident": stat["anon"],
        "reclaimable": _sum_or_none(stat["file"], stat["slab_reclaimable"]),
    }
    out.update(stat)
    return out


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
