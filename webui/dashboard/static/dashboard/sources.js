const $ = id => document.getElementById(id);
const SERVER_NOW = Date.parse(JSON.parse($("now-data").textContent));
const LOADED = Date.now();
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = text => new Date(text + "T12:00:00Z").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
const capital = text => text[0].toUpperCase() + text.slice(1);
function shift(text, days) { const d = new Date(text + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); }

// usual free publishing times, as days from the delivery day and Madrid clock time
const INPUTS = [
  { name: "Single Day-Ahead Coupling (SDAC)", paid: "Not needed", steps: [[-1, "13:00", "prices"], [-1, "18:00", "demand, wind and solar forecasts"]] },
  { name: "Day-ahead schedule", paid: "Not needed", steps: [[-1, "14:00", "schedule"]] },
  { name: "National Phase 1 redispatch", paid: "Not needed", steps: [[-1, "16:00", "totals"]] },
  { name: "Weather forecast", paid: "Open-Meteo commercial plan", note: "Free for non-commercial use.", steps: [[-2, "00:00", "forecast"]] },
  { name: "IDA1, IDA2", paid: "OMIE membership or vendors (Montel, LSEG)", omie: true,
    steps: [[-1, "15:00", "IDA1 cleared"], [-1, "22:30", "IDA2 cleared"]] },
  { name: "IDA3", paid: "OMIE membership or vendors (Montel, LSEG)", omie: true, note: "Covers hours 13 to 24 only.",
    steps: [[0, "10:30", "IDA3 cleared"]] },
  { name: "Unit output, two days before", paid: "Vendors (Montel, Kpler, LSEG)", late: true, steps: [[-1, "01:00", "full day"]] },
  { name: "Unit output, the day before up to noon", paid: "Vendors (Montel, Kpler, LSEG)", late: true, steps: [[-1, "13:00", "morning"]] },
];

function madridNow() {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Madrid", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(SERVER_NOW + Date.now() - LOADED)).map(p => [p.type, p.value]));
  return { day: `${parts.year}-${parts.month}-${parts.day}`, clock: `${parts.hour}:${parts.minute}` };
}

const at = (delivery, offset, clock) => `${shift(delivery, offset)} ${clock}`;
const label = (delivery, offset, clock, now) => `${clock}${shift(delivery, offset) === now.day ? " today" : ` on ${fmt(shift(delivery, offset))}`}`;

function status(input, delivery, now) {
  const stamp = `${now.day} ${now.clock}`;
  const done = input.steps.filter(([o, c]) => at(delivery, o, c) <= stamp);
  if (input.omie) {
    const next = input.steps.find(([o, c]) => at(delivery, o, c) > stamp);
    const cleared = done.length ? `${done.map(s => s[2].split(" ")[0]).join(" and ")} cleared` : "";
    const parts = [cleared, next ? `${next[2].split(" ")[0]} clears about ${label(delivery, next[0], next[1], now)}` : ""].filter(Boolean);
    return ["hollow", `${parts.join("; ")}; free files not expected before ${fmt(shift(delivery, 1))}.`, ""];
  }
  if (done.length === input.steps.length) {
    return input.late ? ["warn", "Due by now, but ENTSO-E is often a day or more late.", ""] : ["hit", "Out now.", ""];
  }
  const out = done.map(s => s[2]).join(", "), next = input.steps[done.length];
  return [out ? "warn" : "hollow", capital(`${out ? `${out} out; ` : ""}${next[2]} expected about ${label(delivery, next[0], next[1], now)}.`), ""];
}

const COLOURS = { hit: "var(--hit)", warn: "var(--warn)" };

function table(id, delivery, now) {
  $(id).innerHTML = INPUTS.map(input => {
    const [state, text, extra] = status(input, delivery, now);
    const dot = state === "hollow" ? `<i class="dot hollow"></i>` : `<i class="dot" style="background:${COLOURS[state]}"></i>`;
    const notes = [extra, input.note].filter(Boolean).map(n => ` ${esc(n)}`).join("");
    return `<tr><td>${esc(input.name)}</td><td><span class="free-state">${dot}<span>${esc(text)}${notes}</span></span></td><td>${esc(input.paid)}</td></tr>`;
  }).join("");
}

function render() {
  const now = madridNow(), tomorrow = shift(now.day, 1);
  $("now").textContent = `${fmt(now.day)}, ${now.clock}`;
  $("dayahead-day").textContent = fmt(tomorrow);
  $("daily-day").textContent = fmt(now.day);
  table("dayahead-rows", tomorrow, now);
  table("daily-rows", now.day, now);
}

render();
setInterval(render, 60000);
