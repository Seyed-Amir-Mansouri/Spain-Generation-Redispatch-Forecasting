const DATA = JSON.parse(document.getElementById("results-data").textContent);
const MAP = JSON.parse(document.getElementById("map-data").textContent);
const STOPS = Array.from({ length: 10 }, (_, i) => +(0.5 + 0.05 * i).toFixed(2));
const TECH_ORDER = ["Combined cycle", "Solar PV", "Nuclear", "Pumped hydro", "Coal"];
const OUTCOMES = { hit: ["Moved, as called", "--hit"], miss: ["Moved, missed", "--miss"], false: ["False alarm", "--false"], quiet: ["Not moved, as expected", "--quiet"] };
const LIGHTS = { hit: "--n-hit", miss: "--n-miss", false: "--n-false", quiet: "--n-quiet" };
const TECH_LIGHTS = { "Combined cycle": "--t-ccgt", "Solar PV": "--t-solar", "Nuclear": "--t-nuclear", "Pumped hydro": "--t-hydro", "Coal": "--t-coal" };
const SEAS = [["Mar Mediterráneo", 37.25, 0.2], ["Océano Atlántico", 36.25, -9.85], ["Mar Cantábrico", 43.78, -6.2]];
const state = { t: 0.5, dir: "any", day: -1, filter: "all", query: "", open: null, mode: "forecast" };
const units = Object.fromEntries(DATA.units.map(u => [u.u, u]));
const byUnitDay = {};
DATA.records.forEach(r => { (byUnitDay[r.u] ||= [])[r.d] = r; });
const N = DATA.days.length;
const techs = [...TECH_ORDER, ...new Set(DATA.units.map(u => u.tech).filter(t => !TECH_ORDER.includes(t)))];
const $ = id => document.getElementById(id);
const fmtDay = (i, long) => { const d = new Date(DATA.days[i] + "T12:00:00Z");
  return d.toLocaleDateString("en-GB", long ? { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" } : { day: "numeric", month: "short", timeZone: "UTC" }); };
const wk = i => new Date(DATA.days[i] + "T12:00:00Z").toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" });
const pct = (a, b) => (b ? (100 * a / b) : NaN);
const show = v => (isNaN(v) ? "–" : v.toFixed(1));
const fmtP = p => p.toFixed(p >= 0.99 ? 3 : 2);
const thr = () => state.t.toFixed(2);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
document.documentElement.style.setProperty("--n", N);

function score(r) { return state.dir === "up" ? r.pu : state.dir === "down" ? r.pd : Math.max(r.pu, r.pd); }
function moved(r) { return state.dir === "up" ? r.qu > 0 : state.dir === "down" ? r.qd > 0 : (r.qu > 0 || r.qd > 0); }
function outcome(r) { const f = score(r) >= state.t, m = moved(r); return m && f ? "hit" : m ? "miss" : f ? "false" : "quiet"; }
function tally(records) {
  const c = { hit: 0, miss: 0, false: 0, quiet: 0 };
  records.forEach(r => c[outcome(r)]++);
  return c;
}
function chosen() { return DATA.records.filter(r => state.day < 0 || r.d === state.day); }

function openUnit(code, scroll) {
  state.open = state.open === code ? null : code;
  renderDetail(); renderBoard(); renderMap();
  if (state.open && scroll) $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderControls() {
  $("thr-value").textContent = thr();
  $("thr-hint").textContent = state.t === 0.5 ? "0.50 is the standard setting: a unit is called when a move is more likely than not. It gives the most right answers overall." :
    state.t >= 0.8 ? "Very strict: only the surest calls remain, and many real moves are missed." : "Stricter than 0.50: fewer calls, more of them right.";
  document.querySelectorAll("#dir button").forEach(b => b.setAttribute("aria-pressed", b.dataset.dir === state.dir));
  document.querySelectorAll("#mode button").forEach(b => b.setAttribute("aria-pressed", b.dataset.mode === state.mode));
  const days = $("days");
  if (!days.children.length) {
    const make = (i, top, bottom) => { const b = document.createElement("button"); b.type = "button"; b.id = `day-${i}`; b.dataset.day = i;
      b.innerHTML = `<small>${top}</small>${bottom}`; b.addEventListener("click", () => { state.day = i; render(); }); return b; };
    days.append(make(-1, "All", `${N} days`), ...DATA.days.map((_, i) => make(i, wk(i), fmtDay(i))));
  }
  days.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", +b.dataset.day === state.day));
}

function renderKpis() {
  const rs = chosen(), c = tally(rs), n = rs.length;
  const flagged = c.hit + c.false, real = c.hit + c.miss;
  $("k-acc").innerHTML = `${show(pct(c.hit + c.quiet, n))}<small>%</small>`;
  $("k-acc-note").textContent = `${c.hit + c.quiet} of ${n} right, moved or not`;
  $("k-prec").innerHTML = `${show(pct(c.hit, flagged))}<small>%</small>`;
  $("k-prec-note").textContent = `${c.hit} of ${flagged} calls were followed by a real move`;
  $("k-rec").innerHTML = `${show(pct(c.hit, real))}<small>%</small>`;
  $("k-rec-note").textContent = `${c.hit} of ${real} real moves were called in advance`;
  $("k-count").innerHTML = `${flagged}<small> · ${real}</small>`;
  $("k-count-note").textContent = state.day < 0 ? `Units summed over ${N} days` : `Units on ${fmtDay(state.day, true)}`;
  $("stack").replaceChildren(...Object.keys(OUTCOMES).map(k => { const i = document.createElement("i"); i.style.width = `${100 * c[k] / n}%`; i.style.background = `var(${OUTCOMES[k][1]})`; return i; }));
  $("legend").replaceChildren(...Object.entries(OUTCOMES).map(([k, [label, col]]) => { const s = document.createElement("span");
    s.innerHTML = `<i class="dot" style="background:var(${col})"></i>${label} <b class="num">${c[k]}</b>`; return s; }));
}

function renderChart() {
  const vals = DATA.days.map((_, i) => { const rs = DATA.records.filter(r => r.d === i), c = tally(rs); return pct(c.hit + c.quiet, rs.length); });
  const lo = 70, hi = 100;
  $("chart").replaceChildren(...vals.map((v, i) => {
    const b = document.createElement("button"); b.type = "button"; b.className = "bar"; b.id = `bar-${i}`; b.setAttribute("aria-pressed", state.day === i);
    b.setAttribute("aria-label", `${fmtDay(i, true)}: ${show(v)}% right`);
    const h = Math.max(4, (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo) * 100);
    b.innerHTML = `<span class="val num">${Math.round(v)}</span><span class="col" style="height:${h}%"></span><span class="lab">${wk(i)}<br>${fmtDay(i).split(" ")[0]}</span>`;
    b.addEventListener("click", () => { state.day = state.day === i ? -1 : i; render(); });
    return b;
  }));
}

const SVGNS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs, parent) => { const e = document.createElementNS(SVGNS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.append(e); return e; };
const proj = (lat, lon) => { const b = MAP.bounds;
  return [(lon - b.lon0) / (b.lon1 - b.lon0) * MAP.width, (b.lat1 - lat) / (b.lat1 - b.lat0) * MAP.height]; };
const lights = {};

function buildMap() {
  const svg = $("map");
  svg.setAttribute("viewBox", `0 0 ${MAP.width} ${MAP.height}`);
  const defs = svgEl("defs", {}, svg);
  defs.innerHTML = `
    <linearGradient id="land-grad" x1="0" y1="0" x2="0.4" y2="1"><stop offset="0" stop-color="#0d3a66"/><stop offset="1" stop-color="#082b4d"/></linearGradient>
    <filter id="coast-glow" x="-5%" y="-5%" width="110%" height="110%"><feGaussianBlur in="SourceGraphic" stdDeviation="2.2" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    <filter id="soft" x="-150%" y="-150%" width="400%" height="400%"><feGaussianBlur stdDeviation="4"/></filter>`;
  for (const [iso, d] of Object.entries(MAP.paths)) if (iso !== "ESP") svgEl("path", { d, class: "land-other" }, svg);
  MAP.grid.forEach(g => {
    svgEl("line", { x1: g.a[0], y1: g.a[1], x2: g.b[0], y2: g.b[1], class: "grat" }, svg);
    const t = svgEl("text", g.kind === "lon" ? { x: g.a[0] + 4, y: MAP.height - 8, class: "grat-label" } : { x: 6, y: g.a[1] - 4, class: "grat-label" }, svg);
    t.textContent = g.kind === "lon" ? `${Math.abs(g.value)}°${g.value < 0 ? "W" : g.value > 0 ? "E" : ""}` : `${g.value}°N`;
  });
  svgEl("path", { d: MAP.paths.ESP, class: "land-es" }, svg);
  if (MAP.provinces) svgEl("path", { d: MAP.provinces, class: "province" }, svg);
  (MAP.province_labels || []).forEach(([name, x, y]) => { svgEl("text", { x, y, class: "province-label", "text-anchor": "middle" }, svg).textContent = name; });
  SEAS.forEach(([name, lat, lon]) => { const [x, y] = proj(lat, lon); svgEl("text", { x, y, class: "sea-label" }, svg).textContent = name.toUpperCase(); });

  const sites = {};
  DATA.units.filter(u => u.lat != null).forEach(u => (sites[`${u.lat},${u.lon}`] ||= []).push(u));
  const layer = svgEl("g", { class: "lights" }, svg);
  let k = 0;
  Object.values(sites).forEach(group => group.forEach((u, i) => {
    let [x, y] = proj(u.lat, u.lon);
    if (group.length > 1) { const a = -Math.PI / 2 + 2 * Math.PI * i / group.length, r = 6 + 2 * group.length; x += r * Math.cos(a); y += r * Math.sin(a); }
    const g = svgEl("g", { class: "light", transform: `translate(${x.toFixed(1)} ${y.toFixed(1)})`, tabindex: 0, role: "button", "aria-label": `${u.u}, ${u.name}` }, layer);
    g.style.setProperty("--delay", `${(-(k++ * 0.37) % 3).toFixed(2)}s`);
    svgEl("circle", { class: "halo", r: 13, filter: "url(#soft)" }, g);
    svgEl("circle", { class: "ring", r: 6 }, g);
    svgEl("circle", { class: "sel", r: 9 }, g);
    svgEl("circle", { class: "core", r: 3.4 }, g);
    svgEl("circle", { class: "spark", r: 1.2 }, g);
    svgEl("text", { class: "tag", x: 8, y: -8 }, g).textContent = u.u;
    g.addEventListener("mouseenter", () => focusUnit(u.u, true));
    g.addEventListener("mouseleave", () => focusUnit(null));
    g.addEventListener("focus", () => focusUnit(u.u, true));
    g.addEventListener("blur", () => focusUnit(null));
    g.addEventListener("click", () => openUnit(u.u, true));
    g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openUnit(u.u, true); } });
    lights[u.u] = g;
  }));
}

function lamp(u, mode = state.mode) {
  if (mode === "technology") { const l = lamp(u, "forecast"); return { ...l, color: TECH_LIGHTS[u.tech] || "--t-other" }; }
  const rs = byUnitDay[u.u];
  if (state.day >= 0) {
    const r = rs[state.day], p = score(r);
    if (mode === "outcome") { const o = outcome(r); return { color: LIGHTS[o], on: o !== "quiet", glow: o === "quiet" ? 0.1 : 0.8, speed: 2.4, value: p, o }; }
    const up = state.dir === "up" || (state.dir === "any" && r.pu >= r.pd);
    const on = p >= state.t;
    return { color: on ? (up ? "--n-up" : "--n-down") : "--n-quiet", on, glow: on ? 0.35 + 0.55 * p : 0.1, speed: 3.4 - 1.6 * p, value: p, o: outcome(r), up };
  }
  const c = tally(rs), called = c.hit + c.false, wrong = c.miss + c.false;
  if (mode === "outcome") {
    const color = c.hit + c.miss === 0 && c.false === 0 ? "--n-quiet" : wrong === 0 ? "--n-hit" : wrong <= 2 ? "--n-up" : "--n-miss";
    return { color, on: c.hit + c.miss + c.false > 0, glow: 0.25 + 0.6 * (c.hit + c.miss) / N, speed: 2.8, value: called, c };
  }
  return { color: called ? "--n-up" : "--n-quiet", on: called > 0, glow: called ? 0.3 + 0.6 * called / N : 0.1, speed: 3.6 - 1.6 * called / N, value: called, c };
}

function renderMap() {
  DATA.units.forEach(u => {
    const g = lights[u.u]; if (!g) return;
    const l = lamp(u);
    g.style.setProperty("--c", `var(${l.color})`);
    g.style.setProperty("--glow", l.glow.toFixed(2));
    g.style.setProperty("--speed", `${Math.max(1.4, l.speed).toFixed(2)}s`);
    g.classList.toggle("on", l.on); g.classList.toggle("dim", !l.on);
    g.classList.toggle("open", state.open === u.u);
  });
  const dayText = state.day >= 0 ? fmtDay(state.day, true) : `all ${N} days`;
  $("map-title").textContent = state.mode === "forecast" ? `Lit for ${dayText}` : state.mode === "technology" ? `Technologies · ${dayText}` : `How ${dayText} went`;
  const legend = state.mode === "technology"
    ? techs.filter(t => DATA.units.some(u => u.tech === t)).map(t => [TECH_LIGHTS[t] || "--t-other", `${t} <b class="num">${DATA.units.filter(u => u.tech === t).length}</b>`])
    : state.mode === "forecast"
    ? (state.day >= 0
      ? [["--n-up", "Called up"], ["--n-down", "Called down"], ["--n-quiet", "Not called"]]
      : [["--n-up", "Called on some days; brighter and faster for more days"], ["--n-quiet", "Never called"]])
    : (state.day >= 0
      ? [["--n-hit", OUTCOMES.hit[0]], ["--n-miss", OUTCOMES.miss[0]], ["--n-false", OUTCOMES.false[0]], ["--n-quiet", OUTCOMES.quiet[0]]]
      : [["--n-hit", "Right every day"], ["--n-up", "Wrong on 1–2 days"], ["--n-miss", "Wrong on 3 or more days"], ["--n-quiet", "Never moved or called"]]);
  $("map-legend").innerHTML = legend.map(([c, t]) => `<span><i class="dot" style="background:var(${c});color:var(${c})"></i>${t}</span>`).join("")
    + (state.mode === "technology" ? `<span>Pulsing: called · dim: not called</span>` : state.day >= 0 && state.mode === "forecast" ? `<span>Brighter and faster for higher probability</span>` : "");
  renderList();
}

function renderList() {
  const items = DATA.units.map(u => ({ u, l: lamp(u) }));
  const row = ({ u, l }, value, note) => `<li tabindex="0" data-u="${u.u}" class="${state.open === u.u ? "open" : ""}" style="--c:var(${l.color})">
      <i class="lamp"></i><span class="who"><b>${u.u}</b><span>${esc(u.name)} · ${u.tech}</span></span><span class="p num">${value}<small>${note}</small></span></li>`;
  let html = "", lit = 0;
  if (state.day >= 0) {
    const called = items.filter(x => x.l.value >= state.t).sort((a, b) => b.l.value - a.l.value);
    const missed = items.filter(x => x.l.o === "miss").sort((a, b) => b.l.value - a.l.value);
    lit = called.length;
    html = called.map(x => row(x, fmtP(x.l.value), state.mode === "outcome" ? (x.l.o === "hit" ? "moved" : "no move") : (x.l.up === false ? "down" : x.l.up ? "up" : state.dir))).join("")
      || `<li class="empty">No unit reaches ${thr()} on this day.</li>`;
    if (state.mode === "outcome" && missed.length) html += `<li class="sep">Moved but not called</li>` + missed.map(x => row(x, fmtP(x.l.value), "missed")).join("");
    $("list-title").textContent = `Called · ${fmtDay(state.day, true)}`;
    $("list-sub").textContent = `${lit} of ${DATA.units.length} units with a chance of ${thr()} or more`;
  } else {
    const called = items.filter(x => x.l.value > 0).sort((a, b) => b.l.value - a.l.value);
    lit = called.length;
    html = called.map(x => row(x, `${x.l.value}/${N}`, state.mode === "outcome" ? `${x.l.c.hit + x.l.c.quiet} right` : "days called")).join("")
      || `<li class="empty">No unit reaches ${thr()} on any day.</li>`;
    $("list-title").textContent = `Called over ${N} days`;
    $("list-sub").textContent = `${lit} of ${DATA.units.length} units called at least once at ${thr()}`;
  }
  const list = $("list");
  list.innerHTML = html;
  list.querySelectorAll("li[data-u]").forEach(li => {
    li.addEventListener("mouseenter", () => focusUnit(li.dataset.u, true));
    li.addEventListener("mouseleave", () => focusUnit(null));
    li.addEventListener("click", () => openUnit(li.dataset.u, true));
    li.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openUnit(li.dataset.u, true); } });
  });
}

function focusUnit(code, tip) {
  Object.entries(lights).forEach(([c, g]) => g.classList.toggle("focus", c === code));
  $("list").querySelectorAll("li[data-u]").forEach(li => li.classList.toggle("focus", li.dataset.u === code));
  const box = $("tip");
  if (!code || !tip || !lights[code]) { box.hidden = true; return; }
  const u = units[code], l = lamp(u), rect = lights[code].getBoundingClientRect(), host = $("mapbox").getBoundingClientRect();
  let body;
  if (state.day >= 0) {
    const r = byUnitDay[code][state.day];
    body = `<div class="tip-row"><span>Up p</span><span>${fmtP(r.pu)}</span></div><div class="tip-row"><span>Down p</span><span>${fmtP(r.pd)}</span></div>
      <div class="tip-row"><span>Moved</span><span>${r.qu ? `up ${r.qu} q` : ""}${r.qu && r.qd ? " · " : ""}${r.qd ? `down ${r.qd} q` : ""}${!r.qu && !r.qd ? "no" : ""}</span></div>
      <div style="margin-top:6px"><em>${OUTCOMES[l.o][0]}</em></div>`;
  } else {
    const c = tally(byUnitDay[code]);
    body = `<div class="tip-row"><span>Called</span><span>${c.hit + c.false}/${N} days</span></div><div class="tip-row"><span>Right</span><span>${c.hit + c.quiet}/${N} days</span></div>
      <div class="tip-row"><span>Missed · false</span><span>${c.miss} · ${c.false}</span></div>`;
  }
  box.innerHTML = `<strong>${code}</strong>${esc(u.name)} · ${u.tech}${body}`;
  box.hidden = false;
  const x = rect.left + rect.width / 2 - host.left, y = rect.top + rect.height / 2 - host.top;
  box.style.left = `${Math.min(Math.max(x, 110), host.width - 110)}px`;
  box.style.top = `${y}px`;
  box.style.transform = y < 140 ? "translate(-50%, 18px)" : "";
}

function hourCells(r) {
  const cells = [];
  for (let h = 0; h < 24; h++) {
    const realBit = state.dir === "up" ? (r.au >> h) & 1 : state.dir === "down" ? (r.ad >> h) & 1 : ((r.au | r.ad) >> h) & 1;
    const p = state.dir === "up" ? r.hu[h] : state.dir === "down" ? r.hd[h] : Math.max(r.hu[h], r.hd[h]);
    const pred = p >= state.t;
    cells.push(`<i class="${realBit && pred ? "both" : pred ? "pred" : realBit ? "real" : ""}" title="${String(h).padStart(2, "0")}:00 · p ${p.toFixed(2)}"></i>`);
  }
  return cells.join("");
}

function renderBoard() {
  const all = DATA.units.filter(u => !state.query || (u.u + " " + u.name).toLowerCase().includes(state.query));
  const outcomeOf = u => state.day >= 0 ? outcome(byUnitDay[u.u][state.day]) : null;
  const counts = { all: all.length, hit: 0, miss: 0, false: 0, quiet: 0 };
  if (state.day >= 0) all.forEach(u => counts[outcomeOf(u)]++);
  const keys = state.day >= 0 ? ["all", "hit", "miss", "false", "quiet"] : ["all"];
  if (state.day < 0) state.filter = "all";
  $("filters").replaceChildren(...keys.map(k => { const b = document.createElement("button"); b.type = "button"; b.id = `filter-${k}`;
    b.setAttribute("aria-pressed", state.filter === k);
    b.innerHTML = (k === "all" ? "All units" : `<i class="dot" style="background:var(${OUTCOMES[k][1]})"></i>${OUTCOMES[k][0]}`) + ` <span class="num">${counts[k]}</span>`;
    b.addEventListener("click", () => { state.filter = k; renderBoard(); }); return b; }));
  $("board-title").textContent = state.day >= 0 ? `Units on ${fmtDay(state.day, true)}` : `Units across ${N} days`;
  $("hour-legend").innerHTML = state.day >= 0
    ? `<span><i class="dot" style="background:var(--hit)"></i>Hour moved and called</span><span><i class="dot" style="border:2px solid var(--false)"></i>Hour called, not moved</span><span><i class="dot" style="background:var(--miss)"></i>Hour moved, not called</span>`
    : `<span>Each strip is the unit's ${N} days, oldest first</span>` + Object.entries(OUTCOMES).map(([, [l, c]]) => `<span><i class="dot" style="background:var(${c})"></i>${l}</span>`).join("");
  const shown = all.filter(u => state.filter === "all" || outcomeOf(u) === state.filter);
  $("board").replaceChildren(...techs.map(tech => {
    const list = shown.filter(u => u.tech === tech);
    if (!list.length) return document.createTextNode("");
    const g = document.createElement("div"); g.className = "group";
    g.innerHTML = `<h3>${tech} <span class="num">${list.length}</span></h3>`;
    const cards = document.createElement("div"); cards.className = "cards";
    list.forEach(u => cards.append(card(u)));
    g.append(cards); return g;
  }));
}

function card(u) {
  const el = document.createElement("article"); el.className = "card" + (state.open === u.u ? " open" : ""); el.tabIndex = 0; el.id = `unit-${u.u}`;
  if (state.day >= 0) {
    const r = byUnitDay[u.u][state.day], o = outcome(r), p = score(r);
    el.style.setProperty("--state", `var(${OUTCOMES[o][1]})`);
    el.innerHTML = `<div class="card-top"><div><div class="code">${u.u}</div><div class="name">${esc(u.name)}</div></div>
      <span class="pill ${o === "quiet" ? "quiet" : ""}">${OUTCOMES[o][0]}</span></div>
      <div class="prob"><div class="meter"><i style="width:${(p * 100).toFixed(1)}%"></i><b style="left:calc(${state.t * 100}% - 1px)"></b></div><span class="num">p ${fmtP(p)}</span></div>
      <div class="hours">${hourCells(r)}</div><div class="hour-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div>`;
  } else {
    const rs = byUnitDay[u.u], c = tally(rs), worst = c.miss + c.false;
    el.style.setProperty("--state", worst === 0 ? "var(--hit)" : worst <= 2 ? "var(--warn)" : "var(--miss)");
    el.innerHTML = `<div class="card-top"><div><div class="code">${u.u}</div><div class="name">${esc(u.name)}</div></div>
      <span class="pill ${worst === 0 ? "" : "quiet"}" style="${worst === 0 ? "--state:var(--hit)" : ""}">${c.hit + c.quiet}/${N} right</span></div>
      <div class="track">${rs.map((r, i) => `<i title="${fmtDay(i, true)}: ${OUTCOMES[outcome(r)][0]}" style="background:var(${OUTCOMES[outcome(r)][1]})"></i>`).join("")}</div>
      <div class="tally num"><span><b style="color:var(--hit)">${c.hit}</b> called</span><span><b style="color:var(--miss)">${c.miss}</b> missed</span><span><b style="color:var(--false)">${c.false}</b> false alarms</span><span>${c.quiet} quiet</span></div>`;
  }
  el.addEventListener("click", () => openUnit(u.u, true));
  el.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openUnit(u.u, true); } });
  return el;
}

function renderDetail() {
  const box = $("detail");
  if (!state.open) { box.hidden = true; return; }
  const u = units[state.open], rs = byUnitDay[u.u];
  box.hidden = false;
  let body;
  if (state.day >= 0) {
    const r = rs[state.day], W = 720, H = 200, L = 34, B = 26, T = 12, cw = (W - L - 8) / 24, y = p => T + (1 - p) * (H - T - B);
    let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="min-width:520px" role="img" aria-label="Hourly probabilities">`;
    [0, 0.25, 0.5, 0.75, 1].forEach(v => { s += `<line x1="${L}" x2="${W - 8}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)" stroke-width="1"/><text x="${L - 6}" y="${y(v) + 3}" text-anchor="end">${v}</text>`; });
    for (let h = 0; h < 24; h++) {
      const x = L + h * cw;
      if ((r.au >> h) & 1) s += `<rect x="${x + 1}" y="${T}" width="${cw - 2}" height="${H - T - B}" fill="var(--up)" opacity=".10"/>`;
      if ((r.ad >> h) & 1) s += `<rect x="${x + 1}" y="${T}" width="${cw - 2}" height="${H - T - B}" fill="var(--down)" opacity=".12"/>`;
      s += `<rect x="${x + cw * 0.16}" y="${y(r.hu[h])}" width="${cw * 0.32}" height="${H - B - y(r.hu[h])}" fill="var(--up)" rx="1.5"/>`;
      s += `<rect x="${x + cw * 0.52}" y="${y(r.hd[h])}" width="${cw * 0.32}" height="${H - B - y(r.hd[h])}" fill="var(--down)" rx="1.5"/>`;
      if (h % 3 === 0) s += `<text x="${x + cw / 2}" y="${H - 8}" text-anchor="middle">${String(h).padStart(2, "0")}</text>`;
    }
    s += `<line x1="${L}" x2="${W - 8}" y1="${y(state.t)}" y2="${y(state.t)}" stroke="var(--fg)" stroke-dasharray="4 4" stroke-width="1"/><text x="${W - 10}" y="${y(state.t) - 4}" text-anchor="end">threshold ${thr()}</text></svg>`;
    body = `<div class="legend"><span><i class="dot" style="background:var(--up)"></i>Chance of moving up</span><span><i class="dot" style="background:var(--down)"></i>Chance of moving down</span>
      <span><i class="dot" style="background:color-mix(in srgb, var(--up) 30%, transparent)"></i>Hours REE moved it up</span><span><i class="dot" style="background:color-mix(in srgb, var(--down) 30%, transparent)"></i>Hours REE moved it down</span></div>
      <div class="svgwrap">${s}</div>
      <p class="hint num">Bars are the model's chance for each hour; shaded hours are when REE really moved the unit. Moved up in ${r.qu} quarter-hours and down in ${r.qd}. Highest chance: up ${r.pu.toFixed(2)}, down ${r.pd.toFixed(2)}.</p>`;
  } else {
    const c = tally(rs);
    body = `<div class="track">${rs.map((r, i) => `<i style="height:34px;background:var(${OUTCOMES[outcome(r)][1]})" title="${fmtDay(i, true)}"></i>`).join("")}</div>
      <div class="hour-axis"><span>${fmtDay(0)}</span><span>${fmtDay(N - 1)}</span></div>
      <p class="hint num">${c.hit} days moved as called, ${c.miss} moves missed, ${c.false} false alarms, ${c.quiet} days not moved as expected. Pick a day above to see its hours.</p>`;
  }
  box.innerHTML = `<div class="detail-head"><div><span class="label">${u.tech}</span><h2>${esc(u.name)}<span>${u.u}</span></h2></div>
    <button type="button" class="close" id="detail-close">Close</button></div>${body}`;
  $("detail-close").addEventListener("click", () => openUnit(u.u));
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme === "dark" ? "dark" : "light";
  $("theme-label").textContent = theme === "dark" ? "Light" : "Dark";
}
$("theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  setTheme(next);
  try { localStorage.setItem("webui-theme", next); } catch (e) {}
});
let savedTheme = null;
try { savedTheme = localStorage.getItem("webui-theme"); } catch (e) {}
setTheme(savedTheme);

function render() { renderControls(); renderKpis(); renderMap(); renderChart(); renderDetail(); renderBoard(); }

$("window").textContent = `${fmtDay(0)} – ${fmtDay(N - 1, true)}`;
$("unit-count").textContent = DATA.units.length;
$("source").textContent = `Source: ${DATA.source}.`;
$("thr").addEventListener("input", e => { state.t = STOPS[+e.target.value]; render(); });
document.querySelectorAll("#dir button").forEach(b => b.addEventListener("click", () => { state.dir = b.dataset.dir; render(); }));
document.querySelectorAll("#mode button").forEach(b => b.addEventListener("click", () => { state.mode = b.dataset.mode; renderControls(); renderMap(); }));
$("search").addEventListener("input", e => { state.query = e.target.value.trim().toLowerCase(); renderBoard(); });
buildMap();
state.day = N - 1;
render();
