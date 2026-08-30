"""HTML smoke tests: the login gate and the dashboard shell carry the design
tokens, the CSRF token, and every panel container the client fills."""

from __future__ import annotations

from alissa.tools.github.revloop.webui.page import dashboard_page, login_page


def test_login_page_is_the_gate():
    html = login_page()
    assert 'name="passcode"' in html
    assert 'action="/login"' in html
    # the gate carries no data script and no CSRF (there is no session yet)
    assert "/api/state" not in html
    assert "csrf-token" not in html


def test_login_page_error_banner():
    assert "Incorrect passcode" in login_page("Incorrect passcode.")
    assert '<p class="banner">x</p>' in login_page("x")
    assert '<p class="banner">' not in login_page()


def test_dashboard_carries_csrf_and_version():
    html = dashboard_page("deadbeefcsrf", "1.2.3")
    assert 'name="csrf-token" content="deadbeefcsrf"' in html
    assert "v1.2.3" in html


def test_dashboard_has_design_tokens_both_themes():
    html = dashboard_page("c", "1.0.0")
    # parchment/ink light theme (default) + glass-dark
    assert "--bg-primary: #F4ECE1" in html          # parchment light
    assert "#141C1C" in html                         # glass-dark base
    assert "prefers-color-scheme: dark" in html      # media default
    assert ':root[data-theme="dark"]' in html        # manual dark wins over media
    assert '[data-theme="light"]' in html            # manual light wins over dark media
    # mono overline + tabular nums + the ONE gold accent on the drift chip
    assert "letter-spacing: 0.12em" in html
    assert "tabular-nums" in html
    assert "--accent-primary: #94743B" in html       # gold, light
    assert "chip drift" in html


def test_gold_accent_is_spent_only_on_the_drift_chip():
    """Colour is rationed: the accent token may only be referenced by the chip
    (and the login field focus ring); status colours stay semantic."""
    html = dashboard_page("c", "1.0.0")
    uses = html.count("var(--accent-primary)")
    assert uses == 1, f"gold accent used {uses}x -- it belongs to the drift chip"


def test_dashboard_has_all_panel_containers():
    html = dashboard_page("c", "1.0.0")
    for panel_id in ("tiles", "pipeline", "sessions", "topprocs", "inbox", "log",
                     "spark-duration", "spark-active", "drift", "state-banner"):
        assert f'id="{panel_id}"' in html


def test_dashboard_shows_reviewer_semantics():
    """Reviewer console, not the devloop worker console: rounds against a cap,
    a reviewer identity, and no worker-tasks panel."""
    html = dashboard_page("c", "1.0.0")
    assert "Reviewer Console" in html
    assert 'id="h-cap"' in html and "cap <span" in html
    assert 'id="h-login"' in html
    assert 'id="tasks"' not in html
    assert "round ' + esc(round) + ' of '" in html  # round k of the cap


def test_dashboard_wires_actions_and_polling():
    html = dashboard_page("c", "1.0.0")
    assert "/api/state" in html
    assert "/action/kill" in html
    assert "/action/retry" in html
    assert "X-CSRF-Token" in html
    assert "setInterval(load, 10000)" in html  # ~10s client poll


def test_dashboard_theme_key_is_console_specific():
    """localStorage is per-origin, not per-port: a shared theme key would let
    the two consoles fight over the toggle."""
    assert "revloop-ui-theme" in dashboard_page("c", "1.0.0")


def test_dashboard_warns_when_there_is_no_daemon_state():
    """An empty dashboard means two different things; the page says which."""
    html = dashboard_page("c", "1.0.0")
    assert "h.state_present" in html
    assert "No revloop state at" in html
    assert "--workspace-root" in html


def test_kill_button_is_wired_by_index_not_by_name():
    """Session names in this table are arbitrary (unmanaged rows included), so
    a name-built selector could silently lose the button or break outright."""
    html = dashboard_page("c", "1.0.0")
    assert "data-actions=" in html
    assert "data-name=" not in html
    # and an unmanaged session is called out in the confirm prompt
    assert "NOT managed by this daemon" in html


def test_dashboard_reads_a_container_memory_plateau_at_a_glance():
    """The tile issue #74 exists for: the headline is what the container is
    CHARGED, and the sub says how much of it is real vs cache the kernel would
    drop -- otherwise a platform memory graph cannot be answered in-console."""
    html = dashboard_page("c", "1.0.0")
    assert "Container Memory" in html
    assert "' resident \u00b7 '" in html
    assert "' reclaimable'" in html
    # shmem is charged, is NOT droppable, and is excluded from reclaimable --
    # so it must be visible or a tmpfs-heavy container shows a charge that
    # neither of the other two numbers accounts for
    assert "bytes(mem.shmem) + ' shmem'" in html
    # ...and a host that cannot be read says so instead of erroring. Round-1
    # [nit]: 'unavailable' (the word the acceptance detail and the neighbouring
    # tiles use), not the diagnosis 'no cgroup v2' the console cannot make.
    assert "tile('Container Memory', '--', 'unavailable')" in html
    assert "'no cgroup v2'" not in html  # the JS string literal is gone
    # the meter is the RESIDENT share of the charge -- the part a limit kills
    # for -- so warn/crit mean the same thing here as on the other tiles
    assert "100 * mem.resident / mem.charged" in html


def test_dashboard_top_process_panel_is_read_only():
    """Host-wide PIDs are unmanaged: the panel names what holds the memory,
    and the sessions table above stays the only place to kill anything."""
    html = dashboard_page("c", "1.0.0")
    assert "by RSS, host-wide" in html
    assert "renderTopProcs(d.top_procs)" in html
    body = html.split("function renderTopProcs")[1].split("function renderLog")[0]
    assert "action/kill" not in body and "btn" not in body


def test_dashboard_tiles_grid_fits_five_tiles():
    """Five tiles now; the narrow breakpoint must still win over the mid one."""
    html = dashboard_page("c", "1.0.0")
    assert "grid-template-columns: repeat(5, 1fr)" in html
    assert html.index("max-width: 1100px") < html.index("max-width: 900px")


def test_dashboard_keeps_a_partial_cgroup_read():
    """Round-1 [minor]: `cgroup_memory` reads memory.current and memory.stat
    through separate helpers so each degrades on its own, and the tile used to
    throw the whole breakdown away when only the headline was missing. This is
    the rendering half of the property that
    `test_webui_sysinfo.test_cgroup_memory_current_missing_keeps_the_stat_split`
    asserts at the data layer -- it lives here because the console, not the
    reader, is what has to still show it."""
    html = dashboard_page("c", "1.0.0")
    assert ("mem.charged != null || mem.resident != null || mem.reclaimable != null"
            in html)


def test_inbox_settled_rows_ride_behind_a_collapsed_footer():
    """Settled pages -- the PR has left the poll's candidate set -- are exhaust,
    not backlog (issue #108). They must not sit in the list that means "you owe
    this", and must not displace its empty state."""
    html = dashboard_page("c", "1.0.0")
    # both halves of the payload reach the renderer
    assert "renderInbox(d.inbox, d.inbox_settled)" in html
    assert "function renderInbox(items, settled)" in html
    # the footer is emitted when there are settled rows -- and only then, so an
    # inbox with nothing filed away renders exactly as it does today
    assert "if (settled.length) {" in html
    # collapsed by default: a <details> with no `open` attribute
    assert '<details class="inbox-settled"><summary>' in html
    assert "inbox-settled\" open" not in html
    # the footer counts the settled rows it is hiding
    assert "settled.length +" in html
    assert "' settled — show</summary>'" in html
    # and they are visibly filed away, not just tucked under a heading
    assert ".inbox-settled .inbox-item { opacity:" in html


def test_inbox_clear_is_the_empty_state_of_the_live_half_only():
    """A hundred settled pages behind the fold must still read as "clear" --
    that is the whole point of the split."""
    html = dashboard_page("c", "1.0.0")
    assert "items.length ? items.map(inboxRow).join('')" in html
    assert '<div class="empty">Inbox clear.</div>' in html
    # the empty state is chosen off `items`, never off a merged list
    assert "if (!items.length)" not in html


def test_live_and_settled_rows_share_one_row_builder():
    """A settled row is the same row, filed away: one builder, so the live list
    cannot drift from the settled one (and the live markup is unchanged)."""
    html = dashboard_page("c", "1.0.0")
    assert html.count("function inboxRow(it)") == 1
    assert html.count("map(inboxRow)") == 2
    assert '<div class="inbox-item"><span>' in html
