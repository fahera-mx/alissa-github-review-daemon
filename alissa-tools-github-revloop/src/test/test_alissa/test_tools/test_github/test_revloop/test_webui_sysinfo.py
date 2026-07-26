"""/proc process-tree accounting: field parsing (incl. weird comm), tree walk,
CPU%/RSS math, and tolerance of vanished PIDs."""

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
        "pid": 42, "ppid": 7, "cpu_ticks": 15, "starttime": 100, "rss_pages": 200,
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
    assert usage == {"pids": 0, "rss_bytes": 0, "cpu_percent": None}


def test_tree_usage_child_vanishes_mid_walk(tmp_path):
    """A PID indexed as a child but whose stat is gone when summed contributes
    nothing -- it must not raise or corrupt the totals."""
    write_uptime(tmp_path, 100.0)
    write_stat(tmp_path, 10, 1, utime=0, starttime=0, rss=100)
    usage = sysinfo.tree_usage(10, proc_root=tmp_path)
    assert usage["pids"] == 1
    assert usage["rss_bytes"] == 100 * sysinfo._PAGE_SIZE


def test_tree_usage_none_root(tmp_path):
    assert sysinfo.tree_usage(None, proc_root=tmp_path)["pids"] == 0


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
