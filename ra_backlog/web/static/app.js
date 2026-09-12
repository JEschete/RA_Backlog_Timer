/* RA Backlog Timer dashboard. Vanilla JS, no build step, no CDN. */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  games: [],
  summary: null,
  prefs: null,
  views: [],
  launch: { enabled: false, targets: [] },
  openGame: null,
  timer: null,
  sort: { key: "points_per_hour", dir: "desc" },
  filters: { search: "", system: "", tag: "", quality: "", hasTime: false, pinned: false },
};

// --- helpers ---------------------------------------------------------------

const fmt = {
  int: (n) => (n == null ? "–" : Math.round(n).toLocaleString()),
  hours: (n) => (n == null ? "–" : n.toFixed(1)),
  pct: (n) => (n == null ? "–" : `${Math.round(n)}%`),
  compactHours(n) {
    if (n == null) return "–";
    return n >= 1000 ? `${(n / 1000).toFixed(1)}k h` : `${Math.round(n)} h`;
  },
  clock(seconds) {
    const m = Math.floor(seconds / 60);
    return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
  },
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 4000);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...options,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* non-JSON */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body ?? {}) });
const del = (p) => api(p, { method: "DELETE" });

// --- load ------------------------------------------------------------------

async function loadAll() {
  try {
    const [games, summary, prefs, views, launch] = await Promise.all([
      api("/api/games"), api("/api/summary"), api("/api/preferences"),
      api("/api/views"), api("/api/launch/config"),
    ]);
    state.games = games.games;
    state.summary = summary;
    state.prefs = prefs;
    state.views = views.views;
    state.launch = launch;

    renderPrefs();
    renderSummary();
    renderSystems();
    renderQuality();
    renderPace();
    renderFilterOptions();
    renderViews();
    renderTable();
    loadSidePanels();
  } catch (err) {
    toast(`Could not load data: ${err.message}`, "error");
  }
}

function loadSidePanels() {
  loadNearly(); loadNewSets(); loadEvents();
}

// --- preferences (#1, #2) --------------------------------------------------

function renderPrefs() {
  const p = state.prefs;
  if (!p) return;
  for (const [id, value] of [["seg-goal", p.goal], ["seg-mode", p.mode]]) {
    $(id).querySelectorAll("button").forEach((b) =>
      b.classList.toggle("on", b.dataset.value === value));
  }
  // Column headers must say what they are now showing.
  $("th-hours").textContent = p.goal === "beat" ? "To beat" : "To master";
  $("kpi-hours-label").textContent = p.goal === "beat" ? "Total to beat" : "Total to master";
  $("kpi-avg-note").textContent = `to ${p.goal}`;
}

async function setPref(key, value) {
  try {
    state.prefs = await post("/api/preferences", { [key]: value });
    renderPrefs();
    await loadAll();
    toast(`Now showing ${state.prefs.label} times.`);
  } catch (err) {
    toast(err.message, "error");
  }
}

// --- KPIs ------------------------------------------------------------------

function renderSummary() {
  const s = state.summary;
  if (!s) return;
  $("kpi-games").textContent = fmt.int(s.total_games);
  $("kpi-games-note").textContent =
    s.no_time_data ? `${s.no_time_data} without time data` : "all have time data";
  $("kpi-hours").textContent = fmt.compactHours(s.total_hours);
  $("kpi-hours-note").textContent =
    s.projection && s.projection.feasible
      ? `${s.projection.years} yr at your pace`
      : (s.total_hours ? `${(s.total_hours / 24).toFixed(0)} days` : "");
  $("kpi-avg").textContent = s.avg_hours ? `${s.avg_hours.toFixed(1)} h` : "–";
  $("kpi-points").textContent = fmt.int(s.total_points);
  $("kpi-points-note").textContent =
    s.with_ra_mastery ? `${s.with_ra_mastery} with RA data` : "";

  if (s.username) {
    $("user-chip").textContent = s.username;
    $("user-chip").hidden = false;
  }
  if (s.db_path) $("creds-db").textContent = `Database: ${s.db_path}`;
}

function renderSystems() {
  const box = $("system-bars");
  const systems = state.summary?.systems || [];
  box.innerHTML = "";
  if (!systems.length) { box.innerHTML = '<p class="panel-note muted">No data yet.</p>'; return; }
  const max = Math.max(...systems.map((s) => s.count));
  for (const sys of systems.slice(0, 12)) {
    const row = document.createElement("div");
    row.className = "bar-row";
    row.title = `${sys.name}: ${sys.count} games`;
    row.innerHTML =
      `<span class="bar-name">${escapeHtml(sys.name)}</span>` +
      `<span class="bar-count">${sys.count}</span>` +
      `<div class="bar-track"><div class="bar-fill" style="width:${(sys.count / max) * 100}%"></div></div>`;
    box.appendChild(row);
  }
}

const QUALITY_META = {
  exact: { icon: "●", cls: "s-good", label: "Exact" },
  fuzzy: { icon: "◐", cls: "s-good", label: "Fuzzy" },
  loose: { icon: "▲", cls: "s-warning", label: "Loose" },
  poor: { icon: "▲", cls: "s-serious", label: "Poor — verify" },
  none: { icon: "✕", cls: "s-critical", label: "No match" },
};

function renderQuality() {
  const list = $("quality-list");
  const counts = state.summary?.match_quality || {};
  const keys = Object.keys(QUALITY_META).filter((k) => counts[k]);
  list.innerHTML = keys.length
    ? keys.map((k) => {
        const m = QUALITY_META[k];
        return `<li><span class="status-icon ${m.cls}" aria-hidden="true">${m.icon}</span>` +
               `<span class="status-label">${m.label}</span>` +
               `<span class="status-count">${counts[k]}</span></li>`;
      }).join("")
    : '<li class="panel-note muted">No matches yet.</li>';
}

function renderPace() {
  const s = state.summary;
  const box = $("pace-body");
  if (!s || !s.calibration) return;
  const rows = [];
  rows.push(`<div class="pace-line"><span>Calibration</span><strong>${
    s.calibration.confident ? `${s.calibration.factor}×` : "—"}</strong></div>`);
  rows.push(`<p class="panel-note">${escapeHtml(s.calibration.description)}</p>`);
  if (s.velocity && s.velocity.hours_per_week) {
    rows.push(`<div class="pace-line"><span>Recent pace</span><strong>${
      s.velocity.hours_per_week} h/wk</strong></div>`);
  }
  if (s.projection && s.projection.feasible) {
    rows.push(`<div class="pace-line"><span>Backlog runway</span><strong>${
      s.projection.years} years</strong></div>`);
  }
  box.innerHTML = rows.join("");
}

// --- filters, views --------------------------------------------------------

function renderFilterOptions() {
  const sysSel = $("f-system");
  const cur = sysSel.value;
  sysSel.innerHTML = '<option value="">All systems</option>';
  for (const sys of state.summary?.systems || []) {
    sysSel.insertAdjacentHTML("beforeend",
      `<option value="${escapeHtml(sys.name)}">${escapeHtml(sys.name)} (${sys.count})</option>`);
  }
  sysSel.value = cur;

  const tags = [...new Set(state.games.flatMap((g) => g.tags || []))].sort();
  const tagSel = $("f-tag");
  const curTag = tagSel.value;
  tagSel.innerHTML = '<option value="">Any tag</option>' +
    tags.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("");
  tagSel.value = curTag;
}

function renderViews() {
  const menu = $("views-menu");
  const saved = state.views.map((v) =>
    `<button data-view="${v.view_id}">${escapeHtml(v.name)}</button>`).join("");
  menu.innerHTML = (saved || '<button disabled>No saved views</button>') +
    '<button data-save="1">＋ Save current filters</button>';
}

// --- table -----------------------------------------------------------------

function visibleGames() {
  const f = state.filters;
  const needle = f.search.trim().toLowerCase();
  let rows = state.games.filter((g) => {
    if (g.hidden) return false;
    if (needle && !(g.title || "").toLowerCase().includes(needle)) return false;
    if (f.system && g.system !== f.system) return false;
    if (f.tag && !(g.tags || []).includes(f.tag)) return false;
    if (f.quality && g.match_quality !== f.quality) return false;
    if (f.hasTime && g.effective_hours == null) return false;
    if (f.pinned && !g.pinned) return false;
    return true;
  });

  const { key, dir } = state.sort;
  const factor = dir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    // Pinned games float regardless of the active sort.
    if ((b.pinned || 0) !== (a.pinned || 0)) return (b.pinned || 0) - (a.pinned || 0);
    const av = a[key], bv = b[key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "string") return av.localeCompare(bv) * factor;
    return (av - bv) * factor;
  });
  return rows;
}

function cell(value, formatter = fmt.hours) {
  return value == null ? '<td class="num dash">–</td>'
                       : `<td class="num">${formatter(value)}</td>`;
}

function renderTable() {
  const rows = visibleGames();
  const maxPph = Math.max(1, ...rows.map((g) => g.points_per_hour || 0));

  $("row-count").textContent = rows.length === state.games.length
    ? `${rows.length} games` : `${rows.length} of ${state.games.length}`;
  $("empty").hidden = state.games.length > 0;

  $("grid-body").innerHTML = rows.map((g) => {
    const m = QUALITY_META[g.match_quality];
    const quality = m
      ? `<span class="status-icon ${m.cls}" aria-hidden="true">${m.icon}</span> ${m.label}`
      : '<span class="dash">–</span>';

    const pphWidth = g.points_per_hour ? (g.points_per_hour / maxPph) * 100 : 0;
    const pph = g.points_per_hour == null
      ? '<td class="num dash">–</td>'
      : `<td class="num"><span class="pph">${g.points_per_hour.toFixed(1)}` +
        `<span class="pph-bar" style="width:${pphWidth}%"></span></span></td>`;

    const diff = g.rarity_score == null
      ? '<td class="num dash">–</td>'
      : `<td class="num"><span class="diff">${Math.round(g.rarity_score)}` +
        `<span class="diff-bar"><span class="diff-fill" style="width:${g.rarity_score}%"></span></span></span></td>`;

    const tags = (g.tags || []).map((t) => `<span class="tagchip">${escapeHtml(t)}</span>`).join("");

    return `<tr data-id="${g.ra_id}">
      <td class="col-pin">${g.pinned ? '<span class="pin-on">📌</span>' : ""}</td>
      <td class="col-title" title="${escapeHtml(g.title)}">${escapeHtml(g.title)} ${tags}</td>
      <td>${escapeHtml(g.system || "")}</td>
      ${cell(g.points, fmt.int)}
      ${cell(g.effective_hours)}
      ${diff}
      ${cell(g.completion_pct, fmt.pct)}
      ${pph}
      <td>${quality}</td>
    </tr>`;
  }).join("");
}

// --- side panels -----------------------------------------------------------

function miniList(items, onClick) {
  if (!items.length) return '<p class="panel-note muted">Nothing here.</p>';
  return `<ul class="minilist">${items.map((it) =>
    `<li class="${onClick ? "clickable" : ""}" data-id="${it.id ?? ""}">
       <span class="mini-title">${escapeHtml(it.title)}</span>
       <span class="mini-meta">${escapeHtml(it.meta)}</span></li>`).join("")}</ul>`;
}

async function loadNearly() {
  try {
    const data = await api("/api/nearly-there");
    const items = data.games.map((g) => ({
      id: g.ra_id, title: g.title,
      meta: `${g.remaining_achievements} left`,
    }));
    $("nearly-body").innerHTML = items.length
      ? miniList(items, true)
      : '<p class="panel-note muted">Nothing within reach yet — run a scan to pull your earned achievements.</p>';
  } catch (err) { $("nearly-body").innerHTML = `<p class="panel-note muted">${escapeHtml(err.message)}</p>`; }
}

async function loadNewSets() {
  try {
    const data = await api("/api/new-sets");
    const parts = [];
    if (data.newly_published.length) {
      parts.push('<p class="panel-note"><strong>New sets published:</strong></p>');
      parts.push(miniList(data.newly_published.map((g) => ({
        id: g.ra_id, title: g.title, meta: `${g.achievements} achievements` })), true));
    }
    parts.push(miniList(data.awaiting.map((g) => ({
      id: g.ra_id, title: g.title, meta: g.system })), true));
    $("newsets-body").innerHTML = parts.join("");
  } catch (err) { $("newsets-body").innerHTML = `<p class="panel-note muted">${escapeHtml(err.message)}</p>`; }
}

async function loadEvents() {
  try {
    const data = await api("/api/events");
    $("events-body").innerHTML = data.events.length
      ? miniList(data.events.map((e) => ({
          id: e.ra_id, title: e.name,
          meta: e.title ? `on your list: ${e.title}` : "not on your list" })), true)
      : '<p class="panel-note muted">No events configured.</p>';
  } catch (err) { $("events-body").innerHTML = `<p class="panel-note muted">${escapeHtml(err.message)}</p>`; }
}

async function spin() {
  const hours = parseFloat($("roul-hours").value);
  const q = hours > 0 ? `?max_hours=${encodeURIComponent(hours)}` : "";
  try {
    const data = await api(`/api/roulette${q}`);
    if (!data.game) {
      $("roulette-body").innerHTML = '<p class="panel-note muted">Nothing matches.</p>';
      return;
    }
    const g = data.game;
    $("roulette-body").innerHTML =
      `<div class="pick clickable" data-id="${g.ra_id}">
         <div class="pick-title">${escapeHtml(g.title)}</div>
         <div class="pick-meta">${escapeHtml(g.system || "")} · ${g.hours ?? "?"}h · ${fmt.int(g.points)} pts</div>
       </div><p class="panel-note" style="margin-top:8px">Chosen from ${data.candidates} candidates.</p>`;
  } catch (err) { toast(err.message, "error"); }
}

async function runPlanner() {
  const budget = parseFloat($("plan-budget").value);
  if (!budget || budget <= 0) return toast("Enter a positive number of hours.", "error");
  const box = $("plan-result");
  box.innerHTML = '<p class="panel-note muted">Planning…</p>';
  try {
    const plan = await api(`/api/plan?budget=${encodeURIComponent(budget)}`);
    box.innerHTML = plan.games.length
      ? `<div class="plan-summary"><strong>${fmt.int(plan.total_points)}</strong> points in ` +
        `<strong>${plan.total_hours.toFixed(1)} h</strong> · ${plan.efficiency} pts/hr</div>` +
        miniList(plan.games.map((g) => ({
          id: g.ra_id, title: g.title,
          meta: `${g.hours.toFixed(1)}h · ${fmt.int(g.points)}p` })), true)
      : '<p class="panel-note muted">Nothing fits in that budget.</p>';
  } catch (err) { box.innerHTML = `<p class="panel-note muted">${escapeHtml(err.message)}</p>`; }
}

async function runTarget() {
  const target = parseInt($("target-points").value, 10);
  if (!target || target <= 0) return toast("Enter a positive points target.", "error");
  const box = $("target-result");
  box.innerHTML = '<p class="panel-note muted">Solving…</p>';
  try {
    const plan = await api(`/api/points-target?target=${target}`);
    if (!plan.reachable) {
      box.innerHTML = `<p class="panel-note">Your whole backlog is worth ${
        fmt.int(plan.total_points)} points — short of ${fmt.int(target)}.</p>`;
      return;
    }
    box.innerHTML =
      `<div class="plan-summary"><strong>${fmt.int(plan.total_points)}</strong> points in ` +
      `<strong>${plan.total_hours.toFixed(1)} h</strong></div>` +
      miniList(plan.games.map((g) => ({
        id: g.ra_id, title: g.title,
        meta: `${g.hours ?? "?"}h · ${fmt.int(g.points)}p` })), true);
  } catch (err) { box.innerHTML = `<p class="panel-note muted">${escapeHtml(err.message)}</p>`; }
}

async function buildSchedule() {
  const hours = parseFloat($("sched-hours").value);
  const weeks = parseInt($("sched-weeks").value, 10);
  const box = $("schedule-result");
  box.innerHTML = '<p class="panel-note muted">Building…</p>';
  try {
    const plan = await api(`/api/schedule?weeks=${weeks}&weekly_hours=${hours}`);
    if (!plan.weeks.length) {
      box.innerHTML = '<p class="panel-note muted">Nothing to schedule.</p>';
      return;
    }
    box.innerHTML = plan.weeks.map((w) =>
      `<div class="weekblock">
         <div class="weekhead"><span>${w.week_start}</span><span>${w.hours.toFixed(1)}h</span></div>
         <ul class="minilist">${w.games.map((g) =>
           `<li class="clickable" data-id="${g.ra_id}">
              <span class="mini-title ${g.partial ? "partial" : ""}">${escapeHtml(g.title)}${
                g.partial ? " (cont.)" : ""}</span>
              <span class="mini-meta">${g.hours.toFixed(1)}h</span></li>`).join("")}</ul>
       </div>`).join("") +
      (plan.unscheduled ? `<p class="panel-note">${plan.unscheduled} games beyond this window.</p>` : "");
  } catch (err) { box.innerHTML = `<p class="panel-note muted">${escapeHtml(err.message)}</p>`; }
}

// --- drawer ----------------------------------------------------------------

async function openGame(raId) {
  const game = state.games.find((g) => g.ra_id === Number(raId));
  if (!game) return;
  state.openGame = game;

  $("drawer-title").textContent = game.title;
  $("drawer-sub").textContent =
    `${game.system || ""}${game.hltb_name && game.hltb_name !== game.title
      ? ` · HLTB: ${game.hltb_name}` : ""}`;
  $("drawer-note").value = game.note || "";
  $("drawer-pin").textContent = game.pinned ? "📌 Unpin" : "📌 Pin";
  $("drawer-hide").textContent = game.hidden ? "Unhide" : "Hide";

  $("drawer-stats").innerHTML = [
    ["Points", fmt.int(game.points)],
    [state.prefs?.goal === "beat" ? "To beat" : "To master",
     game.effective_hours != null ? `${game.effective_hours.toFixed(1)}h` : "–"],
    ["Points/hr", game.points_per_hour != null ? game.points_per_hour.toFixed(1) : "–"],
    ["Done", game.completion_pct != null ? `${Math.round(game.completion_pct)}%` : "–"],
    ["Difficulty", game.rarity_score != null ? Math.round(game.rarity_score) : "–"],
    ["Logged", game.logged_hours != null ? `${game.logged_hours}h` : "–"],
  ].map(([label, value]) =>
    `<div class="dstat"><div class="dstat-label">${label}</div>
     <div class="dstat-value">${value}</div></div>`).join("");

  renderDrawerTags();
  renderRomSection();
  $("drawer-scrim").hidden = false;
  $("drawer").hidden = false;

  loadAchievements(game.ra_id);
  loadGameSessions(game.ra_id);
}

function closeDrawer() {
  $("drawer").hidden = true;
  $("drawer-scrim").hidden = true;
  state.openGame = null;
}

function renderDrawerTags() {
  const g = state.openGame;
  $("drawer-tags").innerHTML = (g.tags || []).length
    ? g.tags.map((t) => `<span class="tagchip">${escapeHtml(t)}<button data-tag="${
        escapeHtml(t)}" aria-label="Remove ${escapeHtml(t)}">✕</button></span>`).join("")
    : '<span class="panel-note muted">None</span>';
}

async function loadAchievements(raId) {
  const box = $("drawer-achievements");
  try {
    const data = await api(`/api/games/${raId}/achievements`);
    if (!data.achievements.length) {
      $("ach-count").textContent = "";
      box.innerHTML = '<p class="panel-note muted">Not fetched yet — run a scan.</p>';
      return;
    }
    const done = data.achievements.filter((a) => a.earned).length;
    $("ach-count").textContent = `${done}/${data.achievements.length}`;

    const summary = data.summary;
    const head = summary
      ? `<p class="panel-note">Difficulty ${Math.round(summary.score ?? 0)} · rarest ${
          summary.rarest_pct ?? "?"}% · ${summary.counts.brutal} brutal, ${
          summary.counts.hard} hard</p>` : "";

    box.innerHTML = head + `<ul class="achlist">${data.achievements.map((a) =>
      `<li class="${a.earned ? "ach-earned" : ""}">
         <span class="${a.earned ? "ach-done" : "ach-todo"}" aria-hidden="true">${a.earned ? "✓" : "○"}</span>
         <span class="ach-name" title="${escapeHtml(a.description || "")}">${escapeHtml(a.title || "")}</span>
         <span class="ach-rate b-${a.band}">${a.earn_rate != null ? a.earn_rate.toFixed(1) + "%" : "–"} ${a.band}</span>
         <span class="ach-pts">${a.points}p</span>
       </li>`).join("")}</ul>`;
  } catch (err) { box.innerHTML = `<p class="panel-note muted">${escapeHtml(err.message)}</p>`; }
}

async function loadGameSessions(raId) {
  try {
    const data = await api(`/api/sessions?ra_id=${raId}`);
    $("drawer-sessions").innerHTML = data.sessions.length
      ? miniList(data.sessions.slice(0, 8).map((s) => ({
          title: (s.started_at || "").slice(0, 16).replace("T", " "),
          meta: s.minutes ? `${Math.round(s.minutes)} min` : "running" })))
      : '<p class="panel-note muted">No sessions logged.</p>';
    updateNowPlaying(data.open);
  } catch (_) { /* non-fatal */ }
}

function renderRomSection() {
  const g = state.openGame;
  const box = $("rom-body");
  if (!state.launch.enabled) {
    box.innerHTML = '<p class="panel-note muted">Launching is off. Start the server with ' +
      '<code>--rom-root DIR</code> to enable it.</p>';
    $("drawer-launch").hidden = true;
    return;
  }
  const target = state.launch.targets.find((t) => t.ra_id === g.ra_id);
  $("drawer-launch").hidden = !target;
  box.innerHTML = (target
    ? `<p class="rom-line">${escapeHtml(target.rom_path)}<br><span class="muted">${
        escapeHtml(target.emulator || "")}</span></p>
       <button class="btn btn-small" id="btn-rom-clear">Remove</button>`
    : "") +
    `<form class="inline-form" id="rom-form">
       <input class="input" id="rom-path" placeholder="Path to ROM…" aria-label="ROM path">
       <select class="input" id="rom-emu" aria-label="Emulator">${
         state.launch.emulators.map((e) => `<option>${e}</option>`).join("")}</select>
       <button class="btn btn-small" type="submit">Save</button>
     </form>
     <p class="panel-note muted">Must sit under: ${
       state.launch.roots.map(escapeHtml).join(", ")}</p>`;
}

// --- session timer (#13) ---------------------------------------------------

function updateNowPlaying(open) {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  if (!open) { $("nowplaying").hidden = true; return; }

  $("np-title").textContent = open.title || "";
  $("nowplaying").hidden = false;
  $("nowplaying").dataset.session = open.session_id;

  const started = new Date(open.started_at).getTime();
  const tick = () => {
    $("np-clock").textContent = fmt.clock((Date.now() - started) / 1000);
  };
  tick();
  state.timer = setInterval(tick, 1000);
}

async function refreshNowPlaying() {
  try { updateNowPlaying((await api("/api/sessions")).open); } catch (_) { /* ignore */ }
}

// --- scanning --------------------------------------------------------------

let wasRunning = false;

function connectStream() {
  const source = new EventSource("/api/scan/stream");
  source.onmessage = (e) => {
    let p; try { p = JSON.parse(e.data); } catch (_) { return; }
    updateScanUI(p);
  };
  source.onerror = () => { /* EventSource retries on its own */ };
}

function updateScanUI(p) {
  const running = p.state === "running";
  $("scanbar").hidden = !running;
  $("btn-scan").disabled = running;
  $("btn-cancel").hidden = !running;

  if (running) {
    $("scanbar-fill").style.width = `${p.total ? (p.done / p.total) * 100 : 0}%`;
    $("scanbar-count").textContent = p.total ? `${p.done} / ${p.total}` : "";
    $("scanbar-detail").textContent = p.title ? `${p.title} — ${p.detail}` : p.detail;
  }
  if (wasRunning && !running) {
    if (p.state === "done") toast("Scan complete.", "ok");
    else if (p.state === "cancelled") toast("Scan cancelled.");
    else if (p.state === "error") toast(p.detail || "Scan failed.", "error");
    loadAll();
  }
  wasRunning = running;
}

// --- wiring ----------------------------------------------------------------

function init() {
  try {
    const saved = localStorage.getItem("ra-theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch (_) { /* private mode */ }

  $("btn-theme").addEventListener("click", () => {
    const root = document.documentElement;
    const isDark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = isDark ? "light" : "dark";
    try { localStorage.setItem("ra-theme", root.dataset.theme); } catch (_) { /* ignore */ }
  });

  // Goal / mode
  document.querySelectorAll(".seg").forEach((seg) => {
    seg.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-value]");
      if (btn) setPref(seg.dataset.pref, btn.dataset.value);
    });
  });

  // Side tabs
  $("side-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    document.querySelectorAll("#side-tabs button").forEach((b) =>
      b.classList.toggle("on", b === btn));
    document.querySelectorAll(".tabpanel").forEach((p) =>
      p.classList.toggle("on", p.dataset.panel === btn.dataset.tab));
  });

  $("btn-scan").addEventListener("click", async () => {
    try { await post("/api/scan"); toast("Scan started."); }
    catch (err) {
      if (err.message.includes("credentials")) return openCreds();
      toast(err.message, "error");
    }
  });
  $("btn-cancel").addEventListener("click", () => post("/api/scan/cancel").catch(() => {}));

  // Export menu
  const menu = $("export-menu");
  $("btn-export").addEventListener("click", (e) => {
    e.stopPropagation(); menu.hidden = !menu.hidden;
  });
  menu.addEventListener("click", async (e) => {
    const f = e.target.dataset.fmt; if (!f) return;
    menu.hidden = true;
    try { toast(`Saved ${(await post(`/api/export/${f}`)).name}`, "ok"); }
    catch (err) { toast(err.message, "error"); }
  });

  // Views menu
  const vmenu = $("views-menu");
  $("btn-views").addEventListener("click", (e) => {
    e.stopPropagation(); vmenu.hidden = !vmenu.hidden;
  });
  vmenu.addEventListener("click", async (e) => {
    const btn = e.target.closest("button"); if (!btn) return;
    vmenu.hidden = true;
    if (btn.dataset.save) {
      const name = prompt("Name this view:");
      if (!name) return;
      try {
        await post("/api/views", { name, filters: state.filters });
        state.views = (await api("/api/views")).views;
        renderViews(); toast("View saved.", "ok");
      } catch (err) { toast(err.message, "error"); }
    } else if (btn.dataset.view) {
      const view = state.views.find((v) => String(v.view_id) === btn.dataset.view);
      if (!view) return;
      Object.assign(state.filters, view.payload);
      $("f-search").value = state.filters.search || "";
      $("f-system").value = state.filters.system || "";
      $("f-tag").value = state.filters.tag || "";
      $("f-quality").value = state.filters.quality || "";
      $("f-hastime").checked = !!state.filters.hasTime;
      $("f-pinned").checked = !!state.filters.pinned;
      renderTable();
    }
  });

  document.addEventListener("click", () => { menu.hidden = true; vmenu.hidden = true; });

  // Filters
  const bind = (id, key, prop = "value") =>
    $(id).addEventListener(prop === "checked" ? "change" : "input", (e) => {
      state.filters[key] = e.target[prop]; renderTable();
    });
  bind("f-search", "search");
  bind("f-hastime", "hasTime", "checked");
  bind("f-pinned", "pinned", "checked");
  // Selects fire "change", not "input".
  for (const [id, key] of [["f-system", "system"], ["f-tag", "tag"],
                           ["f-quality", "quality"]]) {
    $(id).addEventListener("change", (e) => {
      state.filters[key] = e.target.value;
      renderTable();
    });
  }

  // Sorting
  document.querySelectorAll("#grid th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.sort.key === key) {
        state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
      } else {
        state.sort.key = key;
        state.sort.dir = ["title", "system", "match_quality"].includes(key) ? "asc" : "desc";
      }
      document.querySelectorAll("#grid th").forEach((h) =>
        h.classList.remove("sorted-asc", "sorted-desc"));
      th.classList.add(state.sort.dir === "asc" ? "sorted-asc" : "sorted-desc");
      renderTable();
    });
  });

  // Row -> drawer
  $("grid-body").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (tr) openGame(tr.dataset.id);
  });
  // Side panels -> drawer
  document.querySelector(".col-side").addEventListener("click", (e) => {
    const el = e.target.closest("[data-id]");
    if (el && el.dataset.id) openGame(el.dataset.id);
  });

  // Panel buttons
  $("btn-plan").addEventListener("click", runPlanner);
  $("btn-target").addEventListener("click", runTarget);
  $("btn-schedule").addEventListener("click", buildSchedule);
  $("btn-roulette").addEventListener("click", spin);

  // Drawer
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("drawer").hidden) closeDrawer();
  });

  $("drawer-pin").addEventListener("click", () => annotate({ pinned: !state.openGame.pinned }));
  $("drawer-hide").addEventListener("click", () => annotate({ hidden: !state.openGame.hidden }));
  $("btn-save-note").addEventListener("click", () => annotate({ note: $("drawer-note").value }));

  $("tag-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("tag-input").value.trim();
    if (!name) return;
    try {
      await post(`/api/games/${state.openGame.ra_id}/tags`, { name });
      $("tag-input").value = "";
      await refreshGame();
      renderDrawerTags();
    } catch (err) { toast(err.message, "error"); }
  });

  $("drawer-tags").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-tag]"); if (!btn) return;
    const tags = await api("/api/tags");
    const tag = tags.tags.find((t) => t.name === btn.dataset.tag);
    if (!tag) return;
    await del(`/api/games/${state.openGame.ra_id}/tags/${tag.tag_id}`);
    await refreshGame();
    renderDrawerTags();
  });

  $("session-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const minutes = parseFloat($("session-minutes").value);
    if (!minutes || minutes <= 0) return toast("Enter minutes played.", "error");
    try {
      await post(`/api/games/${state.openGame.ra_id}/sessions`, { minutes });
      $("session-minutes").value = "";
      await loadGameSessions(state.openGame.ra_id);
      await refreshGame();
      toast("Session logged.", "ok");
    } catch (err) { toast(err.message, "error"); }
  });

  $("drawer-play").addEventListener("click", async () => {
    try {
      await post(`/api/games/${state.openGame.ra_id}/sessions/start`);
      await refreshNowPlaying();
      toast("Timing your session.");
    } catch (err) { toast(err.message, "error"); }
  });

  $("drawer-launch").addEventListener("click", async () => {
    try {
      await post(`/api/games/${state.openGame.ra_id}/launch`);
      await refreshNowPlaying();
      toast("Launching…", "ok");
    } catch (err) { toast(err.message, "error"); }
  });

  $("rom-body").addEventListener("submit", async (e) => {
    if (e.target.id !== "rom-form") return;
    e.preventDefault();
    try {
      await post(`/api/games/${state.openGame.ra_id}/launch-target`, {
        rom_path: $("rom-path").value.trim(),
        emulator: $("rom-emu").value,
      });
      state.launch = await api("/api/launch/config");
      renderRomSection();
      toast("ROM registered.", "ok");
    } catch (err) { toast(err.message, "error"); }
  });
  $("rom-body").addEventListener("click", async (e) => {
    if (e.target.id !== "btn-rom-clear") return;
    await del(`/api/games/${state.openGame.ra_id}/launch-target`);
    state.launch = await api("/api/launch/config");
    renderRomSection();
  });

  $("np-stop").addEventListener("click", async () => {
    const id = $("nowplaying").dataset.session;
    try {
      await post(`/api/sessions/${id}/finish`);
      await refreshNowPlaying();
      await loadAll();
      toast("Session logged.", "ok");
    } catch (err) { toast(err.message, "error"); }
  });

  // Events editor
  $("btn-edit-events").addEventListener("click", async () => {
    const data = await api("/api/events");
    $("events-text").value = data.events.map((e) =>
      [e.name, e.ra_id ?? "", (e.ends_at || "").slice(0, 10)].join(" | ")).join("\n");
    $("events-modal").showModal();
  });
  $("events-cancel").addEventListener("click", () => $("events-modal").close());
  $("events-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const events = $("events-text").value.split("\n")
      .map((line) => line.split("|").map((s) => s.trim()))
      .filter((p) => p[0])
      .map((p) => ({ name: p[0], ra_id: p[1] ? Number(p[1]) : null, ends_at: p[2] || null }));
    try {
      await post("/api/events", { events });
      $("events-modal").close();
      loadEvents();
      toast("Events saved.", "ok");
    } catch (err) { toast(err.message, "error"); }
  });

  // Credentials
  $("btn-settings").addEventListener("click", openCreds);
  $("creds-cancel").addEventListener("click", () => $("creds-modal").close());
  $("creds-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await post("/api/credentials", {
        username: $("creds-user").value, api_key: $("creds-key").value });
      $("creds-modal").close(); $("creds-key").value = "";
      toast("Credentials saved.", "ok"); loadAll();
    } catch (err) { toast(err.message, "error"); }
  });
  $("creds-clear").addEventListener("click", async () => {
    try {
      await del("/api/credentials");
      $("creds-user").value = ""; $("creds-key").value = "";
      toast("Credentials cleared.");
    } catch (err) { toast(err.message, "error"); }
  });

  loadAll();
  connectStream();
  refreshNowPlaying();
}

async function annotate(fields) {
  try {
    await post(`/api/games/${state.openGame.ra_id}/annotation`, fields);
    await refreshGame();
    openGame(state.openGame.ra_id);
    renderTable();
  } catch (err) { toast(err.message, "error"); }
}

async function refreshGame() {
  const games = await api("/api/games");
  state.games = games.games;
  const id = state.openGame?.ra_id;
  if (id) state.openGame = state.games.find((g) => g.ra_id === id) || state.openGame;
  renderFilterOptions();
  renderTable();
}

async function openCreds() {
  try {
    const status = await api("/api/credentials");
    $("creds-storage").textContent = status.storage;
    $("creds-user").value = status.username || "";
  } catch (_) { /* opens empty */ }
  $("creds-modal").showModal();
}

document.addEventListener("DOMContentLoaded", init);
