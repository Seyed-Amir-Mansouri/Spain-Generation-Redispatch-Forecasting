const $ = id => document.getElementById(id);
const UNITS = Object.fromEntries(JSON.parse($("units-data").textContent).map(u => [u.u, u]));
const LIMITS = JSON.parse($("limits-data").textContent);
const MAX_DAYS = 14;
const STATUS = { have: "On disk", missing: "Published, ready to download", late: "Due, but the source has not published it yet",
  pending: "Not published yet", gap: "Final, with gaps at the source" };
const SHORT = { market: "Day-ahead market", intraday: "Intraday auctions", constraints: "Phase I totals", schedule: "Day-ahead schedule", weather: "Weather forecast", generation: "Unit output" };
const clock = text => new Date(text).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Madrid" });
const when = text => new Date(text).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Madrid" });
const STEP_STATE = { waiting: "Waiting", running: "Running", checking: "Downloaded · checking", done: "Done", on_disk: "Already on disk",
  not_published: "Skipped · not published yet", retry_later: "Skipped · asked recently, will ask again later", late: "Asked · the source has not published it yet",
  skipped: "Skipped", failed: "Failed" };
const JOB_STATE = { running: "Running", done: "Finished", failed: "Failed", stopped: "Stopped" };
const DATASETS = [["market", "Day-ahead price and demand, wind and solar forecasts · ENTSO-E"], ["intraday", "Intraday auction prices · OMIE"],
  ["constraints", "National Phase I redispatch totals · REE e-sios"], ["schedule", "Day-ahead schedule per technology · REE e-sios"],
  ["weather", "Weather forecast at the wind and solar sites · Open-Meteo"], ["generation", "Actual output of each unit · ENTSO-E"]];
const state = { plan: null, timer: null };
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (text, long) => new Date(text + "T12:00:00Z").toLocaleDateString("en-GB", long
  ? { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" } : { day: "numeric", month: "short", timeZone: "UTC" });
const stamp = text => new Date(text).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function csrf() { const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ""; }

async function api(url, body) {
  const response = await fetch(url, body ? { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() }, body: JSON.stringify(body) } : {});
  const data = await response.json().catch(() => ({ error: `The server answered ${response.status}.` }));
  if (!response.ok) throw new Error(data.error || `The server answered ${response.status}.`);
  return data;
}

function dates() { return { first: $("first").value, last: $("last").value }; }

async function check() {
  const { first, last } = dates();
  if (!first || !last) return;
  $("check").disabled = true;
  $("pick-note").textContent = "Checking which data is already on disk…";
  try {
    state.plan = await api(`/api/plan/?first=${first}&last=${last}`);
    $("pick-note").textContent = "";
    renderPlan();
  } catch (error) {
    $("pick-note").textContent = error.message;
  } finally {
    $("check").disabled = false;
  }
}

function renderPlan() {
  const plan = state.plan;
  const rows = plan.datasets.flatMap(d => d.rows);
  const count = s => rows.filter(r => r.status === s).length;
  $("data-step").hidden = false;
  $("data-intro").textContent = `${plural(plan.days.length, "delivery day")} ${plan.days.length === 1 ? "needs" : "need"} ${plural(rows.length, "data item")}, one per data set and day: `
    + [[count("have") + count("gap"), "on disk"], [count("missing"), "ready to download"], [count("late"), "late at the source"], [count("pending"), "not published yet"]]
      .filter(([n]) => n).map(([n, t]) => `${n} ${t}`).join(", ") + ". "
    + "Data is never downloaded twice: the download log remembers every item, and data that is not published yet is not requested.";
  renderTimeline();
  $("needs").innerHTML = `<table class="needs-table"><thead><tr><th>Data</th><th>Source</th><th>Days</th></tr></thead><tbody>${plan.datasets.map(d => `
    <tr><td>${esc(d.title)}</td><td class="src">${esc(d.source)}</td><td><div class="chips">${d.rows.map(r => {
      const why = d.key === "generation" ? ` · used for ${r.for.map(x => fmt(x)).join(", ")}` : "";
      const tried = r.downloaded ? ` · last checked ${stamp(r.downloaded)}` : "";
      const due = (r.status === "pending" || r.status === "late" ? ` · expected ${when(r.expected)}` : "")
        + (r.status === "late" ? (r.fetch ? " · will be asked again on the next download" : ` · asked again from ${when(r.retry)}`) : "");
      return `<span class="chip ${r.status}" title="${STATUS[r.status]}${due}${tried}${why}">${fmt(r.day)}</span>`;
    }).join("")}</div></td></tr>`).join("")}</tbody></table>`;
  const retries = rows.filter(r => r.fetch && r.status === "late").length;
  const nextTry = rows.filter(r => r.status === "late" && !r.fetch).map(r => r.retry).sort()[0];
  $("download").textContent = plan.missing ? `Download ${plural(plan.missing, "item")}` + (retries ? ` (${retries} late, asked again)` : "")
    : plan.pending || plan.waiting ? "Nothing new to download yet" : "Everything is on disk";
  $("download").disabled = !plan.missing || running();
  $("folder").textContent = (plan.pending ? `${plural(plan.pending, "item")} not published yet. ` : "")
    + (nextTry ? `Late items were asked recently; they are asked again from ${when(nextTry)}. ` : "") + `Saved in ${plan.folder}`;
  renderRun();
}

function renderTimeline() {
  const plan = state.plan, days = plan.days, axis = plan.axis, n = axis.length, single = days.length === 1, roomy = n <= 4;
  const t0 = Date.parse(axis[0].start), t1 = Date.parse(plan.timeline[days[days.length - 1]].end);
  const pos = t => Math.min(100, Math.max(0, (Date.parse(t) - t0) / (t1 - t0) * 100));
  const edge = i => i < n ? pos(axis[i].start) : 100;
  const index = d => axis.findIndex(a => a.day === d);
  const at = p => `left:calc(var(--lab) + (100% - var(--lab)) * ${(p / 100).toFixed(4)})`;
  const box = (a, b) => `left:${a}%;width:${b - a}%`;
  $("tl-title").textContent = single ? `When the data for ${fmt(days[0], true)} is published`
    : `When the data for ${fmt(days[0])} to ${fmt(days[days.length - 1], true)} is published`;
  const short = d => new Date(d + "T12:00:00Z").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", timeZone: "UTC" });
  const bands = axis.map((a, i) => {
    const label = single ? ["Two days before · ", "Day before · ", "Delivery day · "][i] + fmt(a.day) : roomy ? fmt(a.day) : short(a.day);
    return `<span class="${days.includes(a.day) ? "deliv" : ""}" style="${box(edge(i), edge(i + 1))}" title="${fmt(a.day, true)}${days.includes(a.day) ? " · delivery day" : ""}">${label}</span>`;
  }).join("");
  const frame = axis.map((a, i) => (days.includes(a.day) ? `<span class="tl-deliv" style="${box(edge(i), edge(i + 1))}"></span>` : "")
    + `<span class="tl-sep" style="left:${edge(i)}%"></span>`).join("");
  const order = ["pending", "late", "missing", "gap", "have"];
  const lines = plan.datasets.map(d => {
    let rows = d.rows;
    if (d.key === "weather") rows = [{ ...rows[0], status: order.find(s => rows.some(r => r.status === s)), expected: axis[0].start }];
    const cover = d.rows.map(r => {
      const i = index(r.day), a = edge(i), b = edge(i + 1);
      return `<span class="tl-cover" style="${box(a, d.key === "generation" && !r.full ? (a + b) / 2 : b)}"></span>`;
    }).join("");
    const marks = rows.map(r => {
      const p = pos(r.expected), label = d.key === "weather" ? "any time" : roomy ? clock(r.expected) : "";
      return `<span class="tl-mark ${r.status}${p > 84 ? " flip" : ""}" style="left:${p}%" title="${esc(STATUS[r.status])} · ${d.key === "weather" ? "" : `for ${fmt(r.day)} · ${when(r.expected)} · `}${esc(r.note)}"><i></i>${label ? `<em>${label}</em>` : ""}</span>`;
    }).join("");
    return `<div class="tl-row"><div class="tl-label">${SHORT[d.key]}<small>${esc(d.source)}</small></div><div class="tl-track">${frame}<span class="tl-rail"></span>${cover}${marks}</div></div>`;
  }).join("");
  const hours = roomy ? axis.flatMap((a, i) => [0, 6, 12, 18].map(h => `<span style="left:${edge(i) + (edge(i + 1) - edge(i)) * h / 24}%">${String(h).padStart(2, "0")}</span>`)).join("") : "";
  const firsts = days.map(d => `<span class="tl-line first" style="${at(pos(plan.timeline[d].earliest))}">${days.length <= 3 ? `<b>${single ? "All inputs in" : `${fmt(d)} ready`}</b>` : ""}</span>`).join("");
  const nowMs = Date.parse(plan.now), showNow = nowMs > t0 && nowMs < t1;
  $("timeline").className = `tl${roomy ? " fine" : ""}`;
  $("timeline").style.setProperty("--cols", n * 4);
  $("timeline").innerHTML = `<div class="tl-head"><div></div><div class="tl-bands">${bands}</div></div>${lines}
    <div class="tl-hours"><div></div><div>${hours}</div></div>${firsts}
    ${showNow ? `<span class="tl-line now" style="${at(pos(plan.now))}"><b>Now</b></span>` : ""}`;
  const late = plan.datasets.flatMap(d => d.rows.filter(r => r.status === "late").map(r => `${SHORT[d.key]} for ${fmt(r.day)} was due ${when(r.expected)}`));
  const lateNote = late.length ? ` <span class="late-note">Running late at the source: ${late.map(esc).join("; ")}. ENTSO-E's unit output often arrives a day or more after its due time, and OMIE posts its intraday files after the auctions clear.</span>` : "";
  if (single) {
    const day = days[0], w = plan.timeline[day], [hh, mm] = clock(w.earliest).split(":").map(Number);
    const started = new Date(w.earliest).toLocaleDateString("en-CA", { timeZone: "Europe/Madrid" }) === day ? hh + (mm > 0 ? 1 : 0) : 0;
    $("tl-note").innerHTML = `All inputs for this day are normally in by <b>${when(w.earliest)}</b>`
      + (started ? `, so a forecast issued then can only cover the rest of the day: hours 1 to ${started} have already started.` : ", before the delivery day starts.") + lateNote;
  } else {
    const first = plan.timeline[days[0]].earliest;
    $("tl-note").innerHTML = `Each dashed blue line marks when a delivery day's inputs are normally all in, at <b>${clock(first)}</b> on the delivery day itself, because IDA3 clears that morning. Shaded columns are the delivery days; hover over a marker for its day and time.` + lateNote;
  }
}

function listDays(days) { return days.map(d => fmt(d, days.length <= 4)).join(", "); }

function waitingFor(day) {
  const plan = state.plan;
  const open = plan.datasets.flatMap(d => d.rows.filter(r => r.for.includes(day) && r.status !== "have" && r.status !== "gap")
    .map(r => ({ ...r, name: SHORT[d.key] + (d.key === "generation" ? ` for ${fmt(r.day)}` : "") })));
  const named = status => open.filter(r => r.status === status);
  const pending = named("pending"), late = named("late"), missing = named("missing");
  const parts = [];
  if (pending.length) parts.push(`Not published yet: ${pending.map(r => esc(r.name)).join(", ")}.`);
  if (late.length) parts.push(`<span class="late-note">Late at the source: ${late.map(r => `${esc(r.name)} (due ${when(r.expected)})`).join(", ")}.</span>`);
  if (missing.length) parts.push(`Ready to download now: ${missing.map(r => esc(r.name)).join(", ")}.`);
  const lead = pending.length
    ? `can be forecast at ${when(plan.timeline[day].earliest)} at the earliest${late.length ? ", and only once the late items arrive" : ""}`
    : late.length ? "can be forecast once the late items arrive" : "can be forecast once the items below are downloaded";
  return `<li><b>${fmt(day, true)}</b> ${lead}. ${parts.join(" ")}</li>`;
}

function renderRun() {
  const plan = state.plan;
  if (!plan) return;
  const model = plan.model, ready = plan.ready, waiting = plan.days.filter(d => !ready.includes(d));
  $("run-step").hidden = false;
  $("run-intro").innerHTML = (ready.length ? `<p>Ready to forecast: <b>${listDays(ready)}</b>.</p>` : "")
    + (waiting.length ? `<ul class="waits">${waiting.map(waitingFor).join("")}</ul>` : "");
  $("model").innerHTML = model.ready
    ? `<p class="hint">Models trained ${model.trained ? stamp(model.trained) : ""} on ${model.units} units.</p>`
    : `<div class="warn"><b>The models need retraining.</b> ${esc(model.reason)} Retraining uses every day with published redispatch and takes a while; when it is done, press Run the forecast.</div>`;
  $("train").hidden = model.ready;
  $("train").disabled = running();
  $("run").disabled = !model.ready || !ready.length || running();
  $("run").textContent = ready.length ? `Run the forecast for ${plural(ready.length, "day")}` : "Run the forecast";
}

function running() { return !!(state.job && state.job.status === "running"); }

async function startJob(kind) {
  const body = { kind, ...dates() };
  if (kind === "forecast") body.days = state.plan.ready;
  try {
    state.job = await api("/api/job/start/", body);
    renderJob();
    poll();
  } catch (error) {
    $("pick-note").textContent = error.message;
  }
}

function poll() {
  clearTimeout(state.timer);
  state.timer = setTimeout(async () => {
    try { state.job = await api("/api/job/"); } catch (error) { return poll(); }
    renderJob();
    if (running()) return poll();
    finished(state.job);
  }, 1500);
}

function finished(job) {
  if (job.kind === "forecast" && job.days.length) showResults(job.days[0], job.days[job.days.length - 1]);
  if (job.kind === "download" || job.kind === "train") check();
}

function renderJob() {
  const job = state.job;
  if (!job || !job.kind) return;
  $("job").hidden = false;
  $("job-title").textContent = job.title;
  $("job-status").textContent = JOB_STATE[job.status] || job.status;
  $("job-status").className = `pill ${job.status}`;
  let steps = job.steps.map(s => [s.label, s.state]);
  if (job.kind === "download") {
    steps = DATASETS.map(([k, title]) => {
      const p = job.progress[k];
      if (!p) return [title, job.status === "running" ? "waiting" : "skipped"];
      const extra = p.pending && (p.state === "done" || p.state === "running") ? ` · ${p.pending} not published yet` : "";
      return [title, p.state, extra];
    });
  }
  $("job-steps").innerHTML = steps.map(([label, s, extra = ""]) => `<li class="${s}"><span>${esc(label)}</span><b>${STEP_STATE[s] || s}${extra}</b></li>`).join("");
  const log = $("job-log");
  log.textContent = job.log.slice(-80).join("\n");
  log.scrollTop = log.scrollHeight;
  if (job.status === "failed") $("job-steps").insertAdjacentHTML("beforeend", `<li class="failed note"><span>${esc(job.log.slice(-1)[0] || "The job stopped with an error.")}</span></li>`);
  ["download", "run", "train", "check"].forEach(id => { if (running()) $(id).disabled = true; });
  if (!running()) { $("check").disabled = false; if (state.plan) renderPlan(); }
}

function hourStrip(unit) {
  const cells = [];
  for (let h = 1; h <= 24; h++) cells.push(`<i class="${unit.up.includes(h) ? "up" : unit.down.includes(h) ? "down" : ""}" title="Hour ${h}"></i>`);
  return `<div class="hours">${cells.join("")}</div><div class="hour-ticks">${Array.from({ length: 24 }, (_, h) => `<span>${h + 1}</span>`).join("")}</div>`;
}

async function showResults(first, last) {
  let data;
  try { data = await api(`/api/forecasts/?first=${first}&last=${last}`); } catch (error) { return; }
  $("results").hidden = false;
  $("result-days").innerHTML = data.days.map(day => {
    if (!day.issued) return `<div class="group"><h3>${fmt(day.day, true)}</h3><p class="hint">No forecast for this day yet.</p></div>`;
    const units = day.units.filter(u => UNITS[u.u]);
    const called = units.filter(u => u.up.length || u.down.length), quiet = units.filter(u => !u.up.length && !u.down.length);
    const up = called.filter(u => u.up.length).length, down = called.filter(u => u.down.length).length;
    return `<div class="group"><h3>${fmt(day.day, true)} <span class="num">${up} upward · ${down} downward</span></h3>
      <p class="hint">Issued ${stamp(day.issued)}. ${plural(called.length, "unit")} called, ${quiet.length} expected to keep their schedule.</p>
      <div class="cards">${called.map(u => { const info = UNITS[u.u] || { name: u.u, tech: "" };
        const dir = u.up.length && u.down.length ? "Upward and downward" : u.up.length ? "Upward" : "Downward";
        return `<div class="card" style="--state:var(${u.up.length ? "--up" : "--down"})"><div class="card-top"><div><div class="code">${esc(u.u)}</div><div class="name">${esc(info.name)}${info.tech ? ` · ${esc(info.tech)}` : ""}</div></div>
          <span class="pill">${dir}</span></div>${hourStrip(u)}
          <div class="tally num"><span>Upward probability <b>${u.pu.toFixed(2)}</b></span><span>Downward <b>${u.pd.toFixed(2)}</b></span></div></div>`; }).join("")}</div>
      ${quiet.length ? `<p class="hint">Not called: ${quiet.map(u => esc(u.u)).join(", ")}.</p>` : ""}</div>`;
  }).join("");
}

$("pick").addEventListener("submit", e => { e.preventDefault(); check(); });
$("download").addEventListener("click", () => startJob("download"));
$("run").addEventListener("click", () => startJob("forecast"));
$("train").addEventListener("click", () => startJob("train"));
function shift(text, days) { const d = new Date(text + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); }

function keepRange(changed) {
  const first = $("first").value, last = $("last").value;
  if (!first || !last) return;
  const latest = [shift(first, MAX_DAYS - 1), LIMITS.last].sort()[0], earliest = [shift(last, 1 - MAX_DAYS), LIMITS.first].sort()[1];
  if (changed === "first" && (last < first || last > latest)) $("last").value = latest;
  if (changed === "last" && (first > last || first < earliest)) $("first").value = earliest;
}
$("first").addEventListener("change", () => keepRange("first"));
$("last").addEventListener("change", () => keepRange("last"));

(async () => {
  ["first", "last"].forEach((id, i) => { $(id).min = LIMITS.first; $(id).max = LIMITS.last; $(id).value = LIMITS.pick[i]; });
  try { state.job = await api("/api/job/"); } catch (e) { state.job = null; }
  if (state.job && state.job.kind) {
    renderJob();
    if (running()) poll();
    else if (state.job.kind === "forecast" && state.job.status === "done") showResults(state.job.days[0], state.job.days[state.job.days.length - 1]);
  }
  if (!running()) check();
})();
