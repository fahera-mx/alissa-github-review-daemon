"""The console's single static HTML page, in the Alissa studio design system.

One page, server-rendered shell + client-side fill: `dashboard_page` returns a
static document whose panels are empty containers, and the inline script polls
`/api/state` every ~10s to fill them. That keeps the server a pure JSON source
and the markup a pure view.

Design-system fidelity (from `fahera-mx/studio.alissa.app` DESIGN-SYSTEM.md +
src/index.css), exactly as the devloop console applied it: the parchment/ink
**light** theme is the default; **glass-dark** is the `prefers-color-scheme:
dark` (and manual-toggle) counterpart; both are first-class. Colour is rationed
-- exactly ONE gold accent, spent on the drift chip; every other status colour
is a semantic `--status-*` token, kept separate. Overlines are mono, .6875rem,
letter-spacing .12em, uppercase, muted. Numbers are tabular-nums. Buttons are
ghost (transparent, 1px border, opacity/colour hover). Icons are stroke-only
SVG. Whitespace is the structure -- no shadows, seamless 1px-gap grids.

Both `__CSRF__` and `__VERSION__` are substituted at render time (plain string
replace, so the CSS/JS braces need no escaping).
"""

from __future__ import annotations

_CSS = """
:root {
  color-scheme: light dark;
  --bg-primary: #F4ECE1;
  --bg-secondary: #F8F1E6;
  --bg-elevated: #FDFAF3;
  --surface-border: rgba(24, 36, 36, 0.12);
  --accent-primary: #94743B;   /* the ONE gold accent -- drift chip only */
  --accent-secondary: #C8A969;
  --text-primary: #182424;
  --text-secondary: #46544F;
  --text-tertiary: #67746E;
  --text-muted: #8A958E;
  --status-committed: #3568A8;
  --status-committed-bg: rgba(53, 104, 168, 0.15);
  --status-in-progress: #1E7A55;
  --status-in-progress-bg: rgba(30, 122, 85, 0.15);
  --status-blocked: #9C6708;
  --status-blocked-bg: rgba(156, 103, 8, 0.15);
  --status-pending: #6B4FAE;
  --status-pending-bg: rgba(107, 79, 174, 0.15);
  --status-cancelled: #B54141;
  --status-cancelled-bg: rgba(181, 65, 65, 0.15);
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
  --transition-fast: 150ms cubic-bezier(0.4, 0, 0.2, 1);
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg-primary: #141C1C;
    --bg-secondary: #1A2525;
    --bg-elevated: #243333;
    --surface-border: rgba(255, 255, 255, 0.07);
    --accent-primary: #CC9E52;
    --accent-secondary: #DDB76A;
    --text-primary: #E6ECEC;
    --text-secondary: #9FB3B3;
    --text-tertiary: #768C8C;
    --text-muted: #5A7373;
    --status-committed: #5C7FA3;
    --status-committed-bg: rgba(92, 127, 163, 0.15);
    --status-in-progress: #6FA58F;
    --status-in-progress-bg: rgba(111, 165, 143, 0.15);
    --status-blocked: #D1A054;
    --status-blocked-bg: rgba(209, 160, 84, 0.15);
    --status-pending: #7C6BA8;
    --status-pending-bg: rgba(124, 107, 168, 0.15);
    --status-cancelled: #B76E6E;
    --status-cancelled-bg: rgba(183, 110, 110, 0.15);
  }
}
:root[data-theme="dark"] {
  --bg-primary: #141C1C;
  --bg-secondary: #1A2525;
  --bg-elevated: #243333;
  --surface-border: rgba(255, 255, 255, 0.07);
  --accent-primary: #CC9E52;
  --accent-secondary: #DDB76A;
  --text-primary: #E6ECEC;
  --text-secondary: #9FB3B3;
  --text-tertiary: #768C8C;
  --text-muted: #5A7373;
  --status-committed: #5C7FA3;
  --status-committed-bg: rgba(92, 127, 163, 0.15);
  --status-in-progress: #6FA58F;
  --status-in-progress-bg: rgba(111, 165, 143, 0.15);
  --status-blocked: #D1A054;
  --status-blocked-bg: rgba(209, 160, 84, 0.15);
  --status-pending: #7C6BA8;
  --status-pending-bg: rgba(124, 107, 168, 0.15);
  --status-cancelled: #B76E6E;
  --status-cancelled-bg: rgba(183, 110, 110, 0.15);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.tabular-nums, .num { font-variant-numeric: tabular-nums; }
.wrap { max-width: 1200px; margin: 0 auto; padding: 2rem 1.5rem 4rem; }
.overline {
  font-family: var(--mono);
  font-size: 0.6875rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-muted);
  margin: 0 0 0.75rem;
}
a { color: var(--text-secondary); text-decoration: none; }
a:hover { color: var(--text-primary); }

/* header */
header.top {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 1rem; flex-wrap: wrap;
  border-bottom: 1px solid var(--surface-border);
  padding-bottom: 1.5rem; margin-bottom: 2rem;
}
header.top h1 {
  font-size: 1.4rem; font-weight: 700; letter-spacing: -0.03em; margin: 0.25rem 0;
}
.ident-meta { color: var(--text-tertiary); font-size: 0.8125rem; }
.ident-meta code { font-family: var(--mono); color: var(--text-secondary); }
.head-right { display: flex; align-items: center; gap: 0.75rem; }

/* the ONE gold accent: the drift chip */
.chip {
  display: inline-flex; align-items: center; gap: 0.4rem;
  font-family: var(--mono); font-size: 0.6875rem; letter-spacing: 0.08em;
  text-transform: uppercase; padding: 0.3rem 0.6rem;
  border-radius: var(--radius-sm);
}
.chip.drift {
  color: var(--accent-primary);
  background: rgba(148, 116, 59, 0.10);
  border: 1px solid rgba(148, 116, 59, 0.30);
}
.chip.drift.current { opacity: 0.7; }

/* ghost button */
.btn {
  background: transparent; border: 1px solid var(--surface-border);
  color: var(--text-secondary); font: inherit; font-size: 0.8125rem;
  padding: 0.35rem 0.7rem; border-radius: var(--radius-md); cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}
.btn:hover { color: var(--text-primary); border-color: var(--text-muted); }
.btn.danger { color: var(--status-cancelled); }
.btn.danger:hover { border-color: var(--status-cancelled); }
.btn:disabled { opacity: 0.4; cursor: default; }
.btn.sm { font-size: 0.75rem; padding: 0.2rem 0.5rem; }

.icon { width: 15px; height: 15px; stroke: currentColor; fill: none;
  stroke-width: 1.5; vertical-align: -2px; }

/* stat tiles: seamless 1px-gap grid */
.tiles {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px;
  background: var(--surface-border); border: 1px solid var(--surface-border);
  border-radius: var(--radius-lg); overflow: hidden; margin-bottom: 2rem;
}
.tile { background: var(--bg-primary); padding: 1.1rem 1.25rem; }
.tile .label { font-family: var(--mono); font-size: 0.6875rem;
  letter-spacing: 0.12em; text-transform: uppercase; color: var(--text-muted); }
.tile .value { font-size: 1.75rem; font-weight: 700; letter-spacing: -0.02em;
  margin-top: 0.35rem; font-variant-numeric: tabular-nums; }
.tile .sub { color: var(--text-tertiary); font-size: 0.75rem; margin-top: 0.2rem; }
.meter { height: 4px; border-radius: 9999px; background: var(--surface-border);
  margin-top: 0.6rem; overflow: hidden; }
.meter > span { display: block; height: 100%; background: var(--status-in-progress); }
.meter.warn > span { background: var(--status-blocked); }
.meter.crit > span { background: var(--status-cancelled); }

.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
@media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; }
  .tiles { grid-template-columns: repeat(2, 1fr); } }

section.panel {
  border: 1px solid var(--surface-border); border-radius: var(--radius-lg);
  padding: 1.25rem 1.35rem; margin-bottom: 1.5rem; background: var(--bg-secondary);
}
.spark { width: 100%; height: 44px; display: block; }
.spark path { fill: none; stroke: var(--text-tertiary); stroke-width: 1.5; }
.spark .fillband { fill: var(--surface-border); stroke: none; }

/* pipeline board (PR-centric: PR ref -> round k of cap -> session -> stage) */
.pill {
  display: inline-flex; align-items: center; gap: 0.35rem;
  font-family: var(--mono); font-size: 0.625rem; letter-spacing: 0.06em;
  text-transform: uppercase; padding: 0.15rem 0.5rem; border-radius: var(--radius-sm);
  border: 1px solid var(--surface-border); color: var(--text-secondary);
}
.pill.spawned, .pill.stale-re-enqueued { color: var(--status-in-progress);
  border-color: var(--status-in-progress); background: var(--status-in-progress-bg); }
.pill.in-flight { color: var(--status-committed);
  border-color: var(--status-committed); background: var(--status-committed-bg); }
.pill.deferred { color: var(--status-pending);
  border-color: var(--status-pending); background: var(--status-pending-bg); }
.pill.converged { color: var(--status-in-progress);
  border-color: var(--status-in-progress); background: var(--status-in-progress-bg); }
.pill.capped { color: var(--status-cancelled);
  border-color: var(--status-cancelled); background: var(--status-cancelled-bg); }
.pill.escalated { color: var(--status-blocked);
  border-color: var(--status-blocked); background: var(--status-blocked-bg); }
.pipe-row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  padding: 0.5rem 0; border-top: 1px solid var(--surface-border); font-size: 0.8125rem; }
.pipe-row .arrow { color: var(--text-muted); }
.pipe-row .trigger { font-family: var(--mono); color: var(--text-secondary); }
.pipe-row .spacer { flex: 1; }

table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
th { text-align: left; font-family: var(--mono); font-size: 0.625rem;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--text-muted);
  font-weight: 500; padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--surface-border); }
td { padding: 0.5rem 0.5rem; border-bottom: 1px solid var(--surface-border);
  color: var(--text-secondary); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 9999px;
  margin-right: 0.4rem; vertical-align: 1px; }
.dot.busy { background: var(--status-in-progress); }
.dot.idle { background: var(--text-muted); }
.dot.gone { background: var(--status-cancelled); }
.mono { font-family: var(--mono); }
.muted { color: var(--text-muted); }
.inbox-item { display: flex; align-items: baseline; justify-content: space-between;
  gap: 0.75rem; padding: 0.55rem 0; border-top: 1px solid var(--surface-border); }
.inbox-kind { font-family: var(--mono); font-size: 0.75rem; color: var(--status-blocked); }
.inbox-kind.cap-out { color: var(--status-cancelled); }
.log {
  font-family: var(--mono); font-size: 0.75rem; line-height: 1.55;
  color: var(--text-tertiary); background: var(--bg-primary);
  border: 1px solid var(--surface-border); border-radius: var(--radius-md);
  padding: 0.85rem 1rem; max-height: 320px; overflow: auto; white-space: pre-wrap;
}
.empty { color: var(--text-muted); font-size: 0.8125rem; padding: 0.75rem 0; }
.stale { color: var(--text-muted); font-size: 0.75rem; margin-left: 0.5rem; }
"""

# The login page: a single passcode field. Deliberately minimal -- no data,
# no scripts, just the fail-closed gate. `__ERROR__` is replaced with a banner
# or the empty string.
_LOGIN = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reviewer Console -- sign in</title>
<style>__CSS__
.login-wrap { max-width: 360px; margin: 12vh auto 0; padding: 0 1.5rem; }
.login-card { border: 1px solid var(--surface-border); border-radius: var(--radius-lg);
  padding: 2rem 1.75rem; background: var(--bg-secondary); }
.login-card h1 { font-size: 1.15rem; letter-spacing: -0.02em; margin: 0.25rem 0 1.25rem; }
input[type=password] { width: 100%; padding: 0.6rem 0.75rem; font: inherit;
  background: var(--bg-elevated); color: var(--text-primary);
  border: 1px solid var(--surface-border); border-radius: var(--radius-md); }
input[type=password]:focus { outline: 2px solid var(--accent-primary); outline-offset: 1px; }
.login-card .btn { width: 100%; margin-top: 1rem; padding: 0.6rem; text-align: center; }
.banner { color: var(--status-cancelled); font-size: 0.8125rem; margin-bottom: 0.75rem; }
</style></head>
<body><div class="login-wrap"><div class="login-card">
<p class="overline">Alissa &middot; PR Reviewer</p>
<h1>Reviewer Console</h1>
__ERROR__
<form method="post" action="/login">
<input type="password" name="passcode" placeholder="Operator passcode" autofocus autocomplete="current-password">
<button class="btn" type="submit">Sign in</button>
</form>
</div></div></body></html>"""

# The dashboard shell. Panels are empty containers filled by the inline script.
_DASHBOARD = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="__CSRF__">
<title>Reviewer Console</title>
<style>__CSS__</style></head>
<body><div class="wrap">
<header class="top">
  <div>
    <p class="overline">Alissa &middot; Review Daemon</p>
    <h1>Reviewer Console</h1>
    <div class="ident-meta">
      reviewing <code id="h-repos">--</code> as <code id="h-login">--</code>
      &middot; poll <span id="h-poll" class="num">--</span>s
      &middot; cap <span id="h-cap" class="num">--</span> rounds
      &middot; up <span id="h-uptime">--</span>
      <span id="h-dryrun"></span>
    </div>
  </div>
  <div class="head-right">
    <span id="drift" class="chip drift" title="running vs latest on PyPI">v__VERSION__</span>
    <button class="btn" id="theme-toggle" title="Toggle theme" aria-label="Toggle theme">
      <svg class="icon" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/></svg>
    </button>
  </div>
</header>

<div class="tiles" id="tiles"></div>

<div class="grid2">
  <section class="panel"><p class="overline">Poll Duration</p>
    <svg class="spark" id="spark-duration" preserveAspectRatio="none"></svg></section>
  <section class="panel"><p class="overline">Active Sessions</p>
    <svg class="spark" id="spark-active" preserveAspectRatio="none"></svg></section>
</div>

<section class="panel"><p class="overline">Pipeline &middot; latest poll</p><div id="pipeline"></div></section>

<section class="panel"><p class="overline">Operator Inbox</p><div id="inbox"></div></section>

<section class="panel"><p class="overline">Sessions</p><div id="sessions"></div></section>

<section class="panel">
  <p class="overline">Daemon Log &middot; <span id="log-path" class="mono muted"></span></p>
  <div class="log" id="log"></div>
</section>

<p class="ident-meta" style="margin-top:2rem">
  <span id="status-line" class="muted">loading&hellip;</span>
  &middot; <a href="#" id="logout">sign out</a>
</p>
</div>
<script>__JS__</script>
</body></html>"""

_JS = r"""
(function () {
  var CSRF = document.querySelector('meta[name=csrf-token]').content;
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  };
  var bytes = function (n) {
    if (n == null) return '--';
    var u = ['B','KB','MB','GB','TB'], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + ' ' + u[i];
  };
  var dur = function (s) {
    if (s == null) return '--';
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    return Math.floor(s / 86400) + 'd';
  };
  var el = function (id) { return document.getElementById(id); };

  // theme toggle (persisted; wins over the media default in both directions)
  var saved = localStorage.getItem('revloop-ui-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  el('theme-toggle').addEventListener('click', function () {
    var cur = document.documentElement.getAttribute('data-theme');
    var media = window.matchMedia('(prefers-color-scheme: dark)').matches;
    var next = (cur ? cur === 'dark' : media) ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('revloop-ui-theme', next);
  });

  el('logout').addEventListener('click', function (e) {
    e.preventDefault();
    fetch('/logout', {method: 'POST', headers: {'X-CSRF-Token': CSRF}})
      .then(function () { location.reload(); });
  });

  function sparkline(node, series) {
    var w = 300, h = 44, n = series.length;
    if (!n) { node.innerHTML = ''; return; }
    var max = Math.max.apply(null, series), min = Math.min.apply(null, series);
    var span = (max - min) || 1;
    var pts = series.map(function (v, i) {
      var x = n === 1 ? w : (i / (n - 1)) * w;
      var y = h - 3 - ((v - min) / span) * (h - 6);
      return x.toFixed(1) + ',' + y.toFixed(1);
    });
    var line = 'M' + pts.join(' L');
    var band = line + ' L' + w + ',' + h + ' L0,' + h + ' Z';
    node.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
    node.innerHTML = '<path class="fillband" d="' + band + '"/><path d="' + line + '"/>';
  }

  function tile(label, value, sub, meter) {
    var m = '';
    if (meter != null) {
      var cls = meter >= 90 ? ' crit' : meter >= 70 ? ' warn' : '';
      m = '<div class="meter' + cls + '"><span style="width:' + Math.min(100, meter) + '%"></span></div>';
    }
    return '<div class="tile"><div class="label">' + esc(label) + '</div>' +
      '<div class="value">' + esc(value) + '</div>' +
      (sub ? '<div class="sub">' + esc(sub) + '</div>' : '') + m + '</div>';
  }

  function renderTiles(d) {
    var t = d.tiles, out = '';
    out += tile('Reviewer Sessions', t.active_sessions, t.live_sessions + ' live on host');
    if (t.rate) {
      var pct = t.rate.limit ? Math.round(100 * (t.rate.limit - t.rate.remaining) / t.rate.limit) : 0;
      out += tile('GH Rate', t.rate.remaining + ' / ' + t.rate.limit, 'used ' + pct + '%', pct);
    } else { out += tile('GH Rate', '--', 'unavailable'); }
    if (t.volume) {
      var vsub = bytes(t.volume.used_bytes) + ' / ' + bytes(t.volume.total_bytes);
      out += tile('Volume', t.volume.percent + '%', vsub, t.volume.percent);
    } else { out += tile('Volume', '--', 'unavailable'); }
    out += tile('Review Queue', t.queue_depth, 'PRs awaiting me, last poll');
    el('tiles').innerHTML = out;
  }

  // one row per PR the latest poll saw: PR ref -> round k of cap -> session -> stage
  function renderPipeline(p) {
    var rows = p.items || [];
    if (!rows.length) {
      el('pipeline').innerHTML = '<div class="empty">No PRs awaiting review in the latest poll.</div>';
      return;
    }
    el('pipeline').innerHTML = rows.map(function (r, i) {
      var stage = (r.stage || '').replace(/_/g, '-');
      var round = r.round == null ? '--' : r.round;
      return '<div class="pipe-row">' +
        '<span class="trigger">' +
        (r.url ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.slug) + '</a>' : esc(r.slug)) +
        '</span>' +
        '<span class="arrow">&rsaquo;</span>' +
        '<span class="muted mono">round ' + esc(round) + ' of ' + esc(r.round_cap) + '</span>' +
        '<span class="arrow">&rsaquo;</span>' +
        '<span class="trigger">' + esc(r.session || 'no session') + '</span>' +
        '<span class="arrow">&rsaquo;</span>' +
        '<span class="pill ' + esc(stage) + '">' + esc(stage) + '</span>' +
        '<span class="muted mono">' + esc(r.task_ref || 'no task') + '</span>' +
        (r.reason ? '<span class="stale">' + esc(r.reason) + '</span>' : '') +
        '<span class="spacer"></span>' +
        '<span data-retry="' + i + '"></span>' +
        '</div>';
    }).join('');
    rows.forEach(function (r, i) {
      if (!r.retry) return;
      var slot = el('pipeline').querySelector('span[data-retry="' + i + '"]');
      if (!slot) return;
      var btn = document.createElement('button');
      btn.className = 'btn sm'; btn.textContent = 'Retry now';
      btn.title = 'Age this round past the stale window so the daemon re-enqueues it. ' +
        'A session that still shows life defers the respawn -- kill it first.';
      btn.addEventListener('click', function () { act('/action/retry', r.retry, btn); });
      slot.appendChild(btn);
    });
  }

  function renderInbox(items) {
    if (!items.length) { el('inbox').innerHTML = '<div class="empty">Inbox clear.</div>'; return; }
    el('inbox').innerHTML = items.map(function (it) {
      return '<div class="inbox-item"><span>' +
        '<span class="inbox-kind ' + esc(it.kind) + '">' + esc(it.kind) + '</span> ' +
        '<a href="' + esc(it.url) + '" target="_blank" rel="noopener">' +
        esc(it.repo_slug) + '#' + esc(it.number) + '</a>' +
        (it.detail ? ' <span class="muted mono">' + esc(it.detail) + '</span>' : '') +
        '</span><span class="muted num">' + dur(it.age_seconds) + '</span></div>';
    }).join('');
  }

  function act(url, body, btn) {
    btn.disabled = true;
    return fetch(url, {method: 'POST', headers: {'X-CSRF-Token': CSRF, 'Content-Type': 'application/json'},
      body: JSON.stringify(body)}).then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function () { load(); }).catch(function () { btn.disabled = false; });
  }

  function renderSessions(sessions) {
    if (!sessions.length) { el('sessions').innerHTML = '<div class="empty">No managed sessions.</div>'; return; }
    var head = '<table><thead><tr><th>Session</th><th>PR</th><th>Round</th><th>State</th>' +
      '<th class="num">Age</th><th class="num">CPU%</th><th class="num">RSS</th><th></th></tr></thead><tbody>';
    var rows = sessions.map(function (s) {
      var dotcls = s.status === 'busy' ? 'busy' : s.status === 'gone' ? 'gone' : 'idle';
      var unmanaged = s.managed ? '' : ' <span class="muted">(unmanaged)</span>';
      return '<tr><td class="mono">' + esc(s.name) + unmanaged + '</td>' +
        '<td class="mono">' + esc(s.pr || '--') + '</td>' +
        '<td class="num">' + (s.round == null ? '--' : esc(s.round)) + '</td>' +
        '<td><span class="dot ' + dotcls + '"></span>' + esc(s.status) + '</td>' +
        '<td class="num">' + dur(s.age_seconds) + '</td>' +
        '<td class="num">' + (s.cpu_percent == null ? '--' : s.cpu_percent) + '</td>' +
        '<td class="num">' + bytes(s.rss_bytes) + '</td>' +
        '<td class="num" data-name="' + esc(s.name) + '"></td></tr>';
    }).join('');
    el('sessions').innerHTML = head + rows + '</tbody></table>';
    sessions.forEach(function (s) {
      var cell = el('sessions').querySelector('td[data-name="' + String(s.name).replace(/"/g, '') + '"]');
      if (!cell) return;
      var kill = document.createElement('button');
      kill.className = 'btn danger sm'; kill.textContent = 'Kill';
      kill.addEventListener('click', function () {
        if (!confirm('Kill ' + s.name + '?')) return;
        act('/action/kill', {session: s.name}, kill);
      });
      cell.appendChild(kill);
      if (s.retry) {
        var retry = document.createElement('button');
        retry.className = 'btn sm'; retry.textContent = 'Retry'; retry.style.marginLeft = '0.4rem';
        retry.addEventListener('click', function () { act('/action/retry', s.retry, retry); });
        cell.appendChild(retry);
      }
    });
  }

  function renderLog(log) {
    el('log-path').textContent = log.path || '(no log configured)';
    el('log').textContent = log.lines.length ? log.lines.join('\n') : '(log empty or unavailable)';
    el('log').scrollTop = el('log').scrollHeight;
  }

  function renderHeader(d) {
    var h = d.header;
    el('h-repos').textContent = h.repos.length ? h.repos.join(', ') : 'ANY REPO';
    el('h-login').textContent = h.reviewer_login || 'gh token identity';
    el('h-poll').textContent = h.poll_interval;
    el('h-cap').textContent = h.round_cap;
    el('h-uptime').textContent = dur(h.uptime_seconds);
    el('h-dryrun').innerHTML = h.dry_run ? ' &middot; <span class="inbox-kind">DRY-RUN</span>' : '';
    var chip = el('drift'), dr = h.drift;
    chip.className = 'chip drift ' + dr.state;
    chip.textContent = 'v' + dr.running + (dr.state === 'behind' ? ' -> ' + dr.latest : '');
    chip.title = 'running ' + dr.running + ' / latest ' + (dr.latest || '?') + ' (' + dr.state + ')';
  }

  function render(d) {
    renderHeader(d);
    renderTiles(d);
    sparkline(el('spark-duration'), d.sparklines.poll_duration_ms);
    sparkline(el('spark-active'), d.sparklines.active_sessions);
    renderPipeline(d.pipeline);
    renderInbox(d.inbox);
    renderSessions(d.sessions);
    renderLog(d.log);
    var when = new Date(d.generated_at * 1000).toLocaleTimeString();
    el('status-line').textContent = 'updated ' + when;
  }

  function load() {
    return fetch('/api/state', {headers: {'X-CSRF-Token': CSRF}}).then(function (r) {
      if (r.status === 401) { location.reload(); return null; }
      return r.json();
    }).then(function (d) { if (d) render(d); }).catch(function () {
      el('status-line').textContent = 'connection error -- retrying';
    });
  }

  load();
  setInterval(load, 10000);
})();
"""


def login_page(error: "str | None" = None) -> str:
    banner = f'<p class="banner">{error}</p>' if error else ""
    return _LOGIN.replace("__CSS__", _CSS).replace("__ERROR__", banner)


def dashboard_page(csrf_token: str, version: str) -> str:
    return (
        _DASHBOARD.replace("__CSS__", _CSS)
        .replace("__JS__", _JS)
        .replace("__CSRF__", csrf_token)
        .replace("__VERSION__", version)
    )
