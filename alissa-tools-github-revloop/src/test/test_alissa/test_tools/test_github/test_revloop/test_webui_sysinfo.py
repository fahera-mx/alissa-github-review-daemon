"""/proc process-tree accounting: field parsing (incl. weird comm), tree walk,
CPU%/RSS math, tolerance of vanished PIDs -- plus the two container-wide
readers: the cgroup v2 memory split and the host-wide top-by-RSS list."""

from __future__ import annotations

from alissa.tools.github.revloop.webui import sysinfo


def write_stat(proc, pid, ppid, *, utime=0, stime=0, starttime=0, rss=0, comm="bash"):
    tail = ["S"] + ["0"] * 21
    tail[1] = str(ppid)
    tail[11] = str(utime)
    tail[12] = str(stime)
    tail[19] = str(starttime)
    tail[21] = str(rss)
    d = proc / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(tail) + "\n")


def write_uptime(proc, seconds):
    (proc / "uptime").write_text(f"{seconds} 0.0\n")


# -- read_stat -------------------------------------------------------------

def test_read_stat_basic(tmp_path):
    write_stat(tmp_path, 42, 7, utime=10, stime=5, starttime=100, rss=200)
    st = sysinfo.read_stat(tmp_path, 42)
    assert st == {
        "pid": 42, "comm": "bash", "ppid": 7, "cpu_ticks": 15,
        "starttime": 100, "rss_pages": 200,
    }


def test_read_stat_comm_with_parens_and_spaces(tmp_path):
    # comm can contain spaces and parentheses; the LAST ')' delimits the fields
    d = tmp_path / "42"
    d.mkdir()
    tail = ["S"] + ["0"] * 21
    tail[1] = "9"
    (d / "stat").write_text("42 (wei )rd (name) " + " ".join(tail))
    st = sysinfo.read_stat(tmp_path, 42)
    assert st is not None and st["ppid"] == 9
    # comm spans the FIRST '(' to that same last ')' -- the whole weird name,
    # not the fragment after the first ')'
    assert st["comm"] == "wei )rd (name"


def test_read_stat_without_an_opening_paren_returns_none(tmp_path):
    """The `(` guard, which no fixture reached before (round-1 [minor], caught
    by mutation and invisible to statement coverage -- all three conditions sit
    on one already-covered `if`). Without it `data[lparen + 1:rparen]` silently
    evaluates to `data[0:rparen]`, so `comm` comes back carrying the PID and
    the top-process panel renders that garbage as a process name."""
    d = tmp_path / "42"
    d.mkdir()
    tail = ["S"] + ["0"] * 21
    (d / "stat").write_text("42 no-parens-here) " + " ".join(tail))
    assert sysinfo.read_stat(tmp_path, 42) is None


def test_read_stat_with_a_close_paren_before_the_open_returns_none(tmp_path):
    """The `lparen > rparen` half of the same guard: a line whose only `(`
    follows its last `)` would slice backwards, not just wrongly."""
    d = tmp_path / "42"
    d.mkdir()
    tail = ["S"] + ["0"] * 21
    (d / "stat").write_text("42 )bash( " + " ".join(tail))
    assert sysinfo.read_stat(tmp_path, 42) is None


def test_read_stat_missing_returns_none(tmp_path):
    assert sysinfo.read_stat(tmp_path, 999) is None


def test_read_stat_no_paren_returns_none(tmp_path):
    d = tmp_path / "42"
    d.mkdir()
    (d / "stat").write_text("42 bash S 1 2 3")  # no ')' at all
    assert sysinfo.read_stat(tmp_path, 42) is None


def test_read_stat_truncated_returns_none(tmp_path):
    d = tmp_path / "42"
    d.mkdir()
    (d / "stat").write_text("42 (bash) S 1 2 3")  # far too few fields
    assert sysinfo.read_stat(tmp_path, 42) is None


def test_read_stat_non_numeric_field_returns_none(tmp_path):
    d = tmp_path / "42"
    d.mkdir()
    tail = ["S"] + ["0"] * 21
    tail[11] = "abc"  # utime is not an int
    (d / "stat").write_text("42 (bash) " + " ".join(tail))
    assert sysinfo.read_stat(tmp_path, 42) is None


# -- index + tree ----------------------------------------------------------

def test_build_index_skips_non_numeric_and_missing(tmp_path):
    write_stat(tmp_path, 10, 1)
    write_stat(tmp_path, 11, 10)
    (tmp_path / "cpuinfo").write_text("x")  # non-numeric entry
    (tmp_path / "77").mkdir()  # numeric dir, but NO stat file (vanished)
    children, stats = sysinfo.build_index(tmp_path)
    assert set(stats) == {10, 11}
    assert children[10] == [11]
    assert 77 not in stats


def test_build_index_unreadable_root_is_empty(tmp_path):
    children, stats = sysinfo.build_index(tmp_path / "no-such-proc")
    assert (children, stats) == ({}, {})


def test_tree_pids_collects_subtree():
    children = {1: [10], 10: [11, 12], 12: [13]}
    assert sysinfo.tree_pids(10, children) == {10, 11, 12, 13}


def test_tree_pids_cycle_guard():
    children = {1: [2], 2: [1]}  # a corrupt loop must not spin forever
    assert sysinfo.tree_pids(1, children) == {1, 2}


# -- tree_usage ------------------------------------------------------------

def test_tree_usage_sums_rss_and_cpu(tmp_path):
    clk = sysinfo._CLK_TCK
    write_uptime(tmp_path, 100.0)
    # root started at t=0 -> wall age 100s; 50s of CPU over the tree -> 50%
    write_stat(tmp_path, 10, 1, utime=25 * clk, stime=0, starttime=0, rss=100)
    write_stat(tmp_path, 11, 10, utime=25 * clk, stime=0, starttime=50 * clk, rss=50)
    usage = sysinfo.tree_usage(10, proc_root=tmp_path)
    assert usage["pids"] == 2
    assert usage["rss_bytes"] == 150 * sysinfo._PAGE_SIZE
    assert usage["cpu_percent"] == 50.0


def test_tree_usage_vanished_root_is_zero(tmp_path):
    write_uptime(tmp_path, 100.0)
    usage = sysinfo.tree_usage(999, proc_root=tmp_path)
    # "unknown", not 0 B: the same thing the sessions panel shows for a session
    # with no pane at all, so the column reads consistently.
    assert usage == {"pids": 0, "rss_bytes": None, "cpu_percent": None}


def test_tree_usage_child_vanishes_mid_walk(tmp_path):
    """A PID indexed as a child but whose stat is gone when summed contributes
    nothing -- it must not raise or corrupt the totals."""
    write_uptime(tmp_path, 100.0)
    write_stat(tmp_path, 10, 1, utime=0, starttime=0, rss=100)
    usage = sysinfo.tree_usage(10, proc_root=tmp_path)
    assert usage["pids"] == 1
    assert usage["rss_bytes"] == 100 * sysinfo._PAGE_SIZE


def test_tree_usage_none_root(tmp_path):
    usage = sysinfo.tree_usage(None, proc_root=tmp_path)
    # both "unknown" paths agree: no pane at all reads the same as a pane that
    # vanished -- neither may claim a measured 0 B
    assert usage == {"pids": 0, "rss_bytes": None, "cpu_percent": None}


def test_tree_usage_no_uptime_leaves_cpu_none(tmp_path):
    write_stat(tmp_path, 10, 1, utime=100, rss=10)
    usage = sysinfo.tree_usage(10, proc_root=tmp_path)  # no uptime file
    assert usage["cpu_percent"] is None
    assert usage["rss_bytes"] == 10 * sysinfo._PAGE_SIZE


# -- disk_usage ------------------------------------------------------------

def test_disk_usage_shape(tmp_path):
    u = sysinfo.disk_usage(tmp_path)
    assert u is not None
    assert set(u) == {"total_bytes", "used_bytes", "free_bytes", "percent"}
    assert 0 <= u["percent"] <= 100


def test_disk_usage_bad_path_none():
    assert sysinfo.disk_usage("/no/such/path/at/all/here") is None


def test_tree_usage_accepts_a_shared_index(tmp_path):
    """Several trees can be accounted from ONE /proc snapshot -- the dashboard
    builds the index once instead of once per session."""
    write_uptime(tmp_path, 100.0)
    write_stat(tmp_path, 10, 1, rss=100, starttime=0)
    write_stat(tmp_path, 20, 1, rss=7, starttime=0)
    index = sysinfo.build_index(tmp_path)

    calls = []
    real_build = sysinfo.build_index
    try:
        sysinfo.build_index = lambda root: calls.append(root) or real_build(root)
        a = sysinfo.tree_usage(10, proc_root=tmp_path, index=index)
        b = sysinfo.tree_usage(20, proc_root=tmp_path, index=index)
    finally:
        sysinfo.build_index = real_build
    assert calls == []  # the passed index is used verbatim -- no rescan
    assert a["rss_bytes"] == 100 * sysinfo._PAGE_SIZE
    assert b["rss_bytes"] == 7 * sysinfo._PAGE_SIZE


# -- top_procs -------------------------------------------------------------

def test_top_procs_ranks_the_whole_proc_by_rss(tmp_path):
    """Host-wide, largest first -- not a pane tree: the process holding a
    container's resident charge is routinely not a reviewer session."""
    write_stat(tmp_path, 10, 1, rss=100, comm="claude")
    write_stat(tmp_path, 11, 10, rss=900, comm="node")
    write_stat(tmp_path, 12, 1, rss=500, comm="python3")
    rows = sysinfo.top_procs(2, proc_root=tmp_path)
    assert rows == [
        {"pid": 11, "comm": "node", "rss_bytes": 900 * sysinfo._PAGE_SIZE},
        {"pid": 12, "comm": "python3", "rss_bytes": 500 * sysinfo._PAGE_SIZE},
    ]


def test_top_procs_returns_fewer_than_asked_when_proc_is_smaller(tmp_path):
    write_stat(tmp_path, 10, 1, rss=5, comm="bash")
    assert len(sysinfo.top_procs(5, proc_root=tmp_path)) == 1


def test_top_procs_ties_break_on_pid(tmp_path):
    """Equal RSS must give a TOTAL order: a list that reshuffles between polls
    reads as churn on a host where nothing is moving."""
    for pid in (30, 10, 20):
        write_stat(tmp_path, pid, 1, rss=64, comm="same")
    assert [r["pid"] for r in sysinfo.top_procs(3, proc_root=tmp_path)] == [10, 20, 30]


def test_top_procs_skips_a_vanished_pid(tmp_path):
    """A numeric /proc entry whose stat was deleted mid-walk contributes
    nothing and never raises -- the module's standing tolerance."""
    write_stat(tmp_path, 10, 1, rss=100, comm="claude")
    (tmp_path / "77").mkdir()  # indexed by the walk, no stat file
    rows = sysinfo.top_procs(5, proc_root=tmp_path)
    assert [r["pid"] for r in rows] == [10]


def test_top_procs_unreadable_proc_root_is_empty(tmp_path):
    assert sysinfo.top_procs(5, proc_root=tmp_path / "no-such-proc") == []


def test_top_procs_zero_or_negative_n_is_empty(tmp_path):
    write_stat(tmp_path, 10, 1, rss=100)
    assert sysinfo.top_procs(0, proc_root=tmp_path) == []
    assert sysinfo.top_procs(-1, proc_root=tmp_path) == []


def test_top_procs_reuses_a_shared_index(tmp_path):
    """The dashboard scans /proc ONCE: the session table and this list rank the
    same snapshot, so neither pays for a second walk."""
    write_stat(tmp_path, 10, 1, rss=100, comm="claude")
    write_stat(tmp_path, 11, 1, rss=7, comm="node")
    index = sysinfo.build_index(tmp_path)

    calls = []
    real_build = sysinfo.build_index
    try:
        sysinfo.build_index = lambda root: calls.append(root) or real_build(root)
        rows = sysinfo.top_procs(5, proc_root=tmp_path, index=index)
    finally:
        sysinfo.build_index = real_build
    assert calls == []  # the passed index is used verbatim -- no rescan
    assert [r["pid"] for r in rows] == [10, 11]


# -- cgroup_memory ---------------------------------------------------------

# A faithful cgroup v2 `memory.stat`: the keys the tile wants, in the kernel's
# own order, interleaved with the ~60 it does not (`slab` sits right next to
# `slab_reclaimable`, `anon_thp` right after `anon`). The numbers are the
# audited Railway plateau from issue #74 -- ~6 GB charged that is ~72 MB of
# real process memory and ~5.9 GB of reclaimable cache.
RAILWAY_CURRENT = 6420496384          # 5.98 GiB
RAILWAY_ANON = 75091968               # 71.6 MiB
RAILWAY_FILE = 2362232832             # 2.20 GiB
RAILWAY_SLAB_RECLAIMABLE = 3983993651  # 3.71 GiB


def write_cgroup(root, *, current=RAILWAY_CURRENT, anon=RAILWAY_ANON,
                 file=RAILWAY_FILE, slab_reclaimable=RAILWAY_SLAB_RECLAIMABLE):
    root.mkdir(parents=True, exist_ok=True)
    if current is not None:
        (root / "memory.current").write_text(f"{current}\n")
    (root / "memory.stat").write_text(
        f"anon {anon}\n"
        f"file {file}\n"
        "kernel 3990000000\n"
        "kernel_stack 1638400\n"
        "pagetables 0\n"
        "percpu 24400\n"
        "sock 0\n"
        "shmem 0\n"
        "file_mapped 1347584\n"
        "file_dirty 16384\n"
        "anon_thp 4194304\n"
        "inactive_anon 0\n"
        "active_anon 75091968\n"
        "inactive_file 2000000000\n"
        "active_file 362232832\n"
        "unevictable 0\n"
        f"slab_reclaimable {slab_reclaimable}\n"
        "slab_unreclaimable 2065528\n"
        "slab 3986059179\n"
        "workingset_refault_anon 0\n"
        "pgscan 37031\n"
    )
    return root


def test_cgroup_memory_splits_the_charge(tmp_path):
    """The whole point of the tile: a ~6 GB plateau reads as ~72 MB of real
    process memory plus ~5.9 GB the kernel would drop under pressure."""
    m = sysinfo.cgroup_memory(write_cgroup(tmp_path / "cg"))
    assert m["charged"] == RAILWAY_CURRENT
    assert m["resident"] == RAILWAY_ANON
    # the audited plateau has `shmem 0`, so dropping shmem changes nothing here
    assert m["shmem"] == 0
    assert m["reclaimable"] == RAILWAY_FILE + RAILWAY_SLAB_RECLAIMABLE
    # the raw keys ride along for an in-container memory.stat spot check
    assert m["anon"] == RAILWAY_ANON
    assert m["file"] == RAILWAY_FILE
    assert m["inactive_file"] == 2000000000
    assert m["shmem"] == 0
    assert m["slab_reclaimable"] == RAILWAY_SLAB_RECLAIMABLE
    assert m["slab_unreclaimable"] == 2065528
    # `slab` and `active_file` are NOT parsed -- a fixed, small key set
    assert "slab" not in m and "active_file" not in m
    # ...and the split really is the operator's answer: reclaimable dominates
    assert m["reclaimable"] > 50 * m["resident"]


def test_cgroup_memory_excludes_shmem_from_reclaimable(tmp_path):
    """Round-1 [minor]: `shmem` is a SUBSET of `file` and is swap-backed, so on
    a container with swap disabled it cannot be reclaimed under pressure. A
    tmpfs-heavy container must not read as "all cache, no leak" -- the one way
    this tile could be wrong in the reassuring direction."""
    root = tmp_path / "cg"
    root.mkdir()
    (root / "memory.current").write_text("10000\n")
    (root / "memory.stat").write_text(
        "anon 1000\nfile 6000\nshmem 5000\nslab_reclaimable 500\n"
    )
    m = sysinfo.cgroup_memory(root)
    # 6000 file - 5000 shmem = 1000 droppable, + 500 slab
    assert m["reclaimable"] == 1500
    # and shmem survives in the payload as its own magnitude, so the console
    # can account for the 5000 that is neither resident nor reclaimable
    assert m["shmem"] == 5000
    assert m["resident"] == 1000


def test_cgroup_memory_unknown_shmem_makes_reclaimable_unknown(tmp_path):
    """`file` whole would be the reassuring-direction error the split exists to
    prevent, so an unknown shmem propagates rather than being treated as 0."""
    root = tmp_path / "cg"
    root.mkdir()
    (root / "memory.stat").write_text("anon 1000\nfile 6000\nslab_reclaimable 500\n")
    m = sysinfo.cgroup_memory(root)
    assert m["file"] == 6000 and m["shmem"] is None
    assert m["reclaimable"] is None


def test_cgroup_memory_clamps_a_shmem_larger_than_file(tmp_path):
    """shmem cannot legitimately exceed file (one read of one file), but a
    negative "cache" number would be worse to render than a zero."""
    root = tmp_path / "cg"
    root.mkdir()
    (root / "memory.stat").write_text("file 100\nshmem 900\nslab_reclaimable 50\n")
    assert sysinfo.cgroup_memory(root)["reclaimable"] == 50


def test_cgroup_memory_missing_root_is_all_none(tmp_path):
    """A dev laptop or macOS has no cgroup v2 at all: every field is None and
    nothing raises, so the console still renders."""
    m = sysinfo.cgroup_memory(tmp_path / "no-such-cgroup")
    assert set(m) == {"charged", "resident", "reclaimable", "anon", "file",
                      "inactive_file", "shmem", "slab_reclaimable",
                      "slab_unreclaimable"}
    assert all(value is None for value in m.values())


def test_cgroup_memory_malformed_values_are_none(tmp_path):
    """cgroup v2 writes the literal `max` in some scalar files, and a key can
    carry anything: each bad value is None on its own, not an exception and
    not a zero (a measured 0 B is a claim we cannot make)."""
    root = tmp_path / "cg"
    root.mkdir()
    (root / "memory.current").write_text("max\n")
    (root / "memory.stat").write_text(
        "anon not-a-number\n"
        "file 1024\n"
        "slab_reclaimable 2048\n"
    )
    m = sysinfo.cgroup_memory(root)
    assert m["charged"] is None
    assert m["resident"] is None and m["anon"] is None
    assert m["file"] == 1024
    # keys the kernel never wrote are None, so the payload shape is stable
    assert m["shmem"] is None and m["inactive_file"] is None
    # ...and an unknown shmem propagates: the droppable part of `file` is not
    # knowable without it, so reclaimable stays unknown rather than counting
    # `file` whole (see test_cgroup_memory_unknown_shmem_makes_reclaimable_unknown)
    assert m["reclaimable"] is None


def test_cgroup_memory_reclaimable_needs_both_parts(tmp_path):
    """A partial sum would be published as a whole bucket and read as a
    SMALLER cache than the container holds -- exactly the wrong direction for
    "is that plateau a leak?". Unknown stays unknown."""
    root = tmp_path / "cg"
    root.mkdir()
    (root / "memory.current").write_text("100\n")
    (root / "memory.stat").write_text("anon 10\nfile 2000\n")  # no slab_reclaimable
    m = sysinfo.cgroup_memory(root)
    assert m["file"] == 2000 and m["slab_reclaimable"] is None
    assert m["reclaimable"] is None


def test_cgroup_memory_current_missing_keeps_the_stat_split(tmp_path):
    """Each file degrades on its own: an unreadable memory.current does not
    blank the breakdown.

    Data layer only -- that the CONSOLE still renders that breakdown is a
    separate property, pinned by
    `test_webui_page.test_dashboard_keeps_a_partial_cgroup_read`. Round-1
    [minor]: this docstring used to claim the rendering property while
    asserting the data one, and the tile did in fact throw the breakdown away.
    """
    root = write_cgroup(tmp_path / "cg", current=None)
    m = sysinfo.cgroup_memory(root)
    assert m["charged"] is None
    assert m["resident"] == RAILWAY_ANON
    assert m["reclaimable"] == RAILWAY_FILE + RAILWAY_SLAB_RECLAIMABLE


def test_cgroup_memory_accepts_a_str_root(tmp_path):
    """The root is a parameter (and a plain str is what Sources passes)."""
    root = write_cgroup(tmp_path / "cg")
    assert sysinfo.cgroup_memory(str(root))["charged"] == RAILWAY_CURRENT
