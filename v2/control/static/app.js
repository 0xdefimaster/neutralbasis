/* NeutralBasis Control Center (PAPER, read-only).
 * Vanilla JS. Every text goes through textContent; no innerHTML, no eval, no inline
 * script or style. It only GETs this server's read-model API (same origin); a refresh
 * never reaches a venue. UNKNOWN values are shown as UNKNOWN, never as 0 or OK. */
"use strict";

const REFRESH_MS = 10000;
const view = document.getElementById("view");
let timer = null;
let current = null;
const state = { oppPage: 1, oppFilters: { strategy: "", label: "", safety: "", sort: "expected_net" },
                mktPage: 1, mktVenue: "", scenarios: false };

/* ---------------------------------------------------------------- helpers */
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
}
function add(parent, ...kids) { for (const k of kids) if (k) parent.appendChild(k); return parent; }
function link(href, text, cls) { const a = el("a", cls || "link", text); a.href = href; return a; }

const STATUS_CLASS = {
  HEALTHY: "chip chip-ok", RUNNING: "chip chip-ok", OK: "chip chip-ok", PAPER_ELIGIBLE: "chip chip-ok",
  DEGRADED: "chip chip-warn", STALE: "chip chip-warn", RATE_LIMITED: "chip chip-warn",
  ESTIMATED_RESEARCH_ONLY: "chip chip-warn", ESTIMATED_PAPER_CANDIDATE: "chip chip-warn",
  NOT_SAFE: "chip chip-bad", DISCONNECTED: "chip chip-bad", INTEGRITY_FAILURE: "chip chip-bad",
  NOT_RUNNING: "chip chip-bad", BLOCKED: "chip chip-bad", BLOCKED_ECONOMICS: "chip chip-bad",
  HISTORICAL_ONLY: "chip chip-hist", HISTORICAL: "chip chip-hist", LIVE_OBSERVED: "chip chip-hist",
  UNVERIFIED: "chip chip-unv", UNKNOWN: "chip chip-unv", MOCK_TESTED: "chip chip-unv",
  NOT_SUPPORTED: "chip chip-na", UNSUPPORTED: "chip chip-na", NOT_TESTED: "chip chip-na",
  DOCUMENTED: "chip chip-na", WATCHLIST: "chip chip-muted", DATA_STALE: "chip chip-warn",
  RESEARCH_ONLY: "chip chip-muted", CLOSED: "chip chip-muted", POSITION_MONITORING: "chip chip-hist",
  ESTIMATED: "chip chip-warn", VERIFIED: "chip chip-ok", STALE_HB: "chip chip-warn",
};
function chip(status, title) {
  const s = status === null || status === undefined || status === "" ? "UNKNOWN" : String(status);
  const c = el("span", STATUS_CLASS[s] || "chip chip-muted", s.replace(/_/g, " "));
  if (title) c.title = title;
  return c;
}
function isNum(x) { return typeof x === "number" && isFinite(x); }
function unknown(reason) { const s = el("span", "unknown", "UNKNOWN"); if (reason) s.title = reason; return s; }
function num(x, digits, reason) {
  if (!isNum(x)) return unknown(reason);
  const s = el("span", "num" + (x > 0 ? " pos" : x < 0 ? " neg" : ""), x.toFixed(digits === undefined ? 2 : digits));
  return s;
}
function usd(x, reason) {
  if (!isNum(x)) return unknown(reason);
  return el("span", "num" + (x > 0 ? " pos" : x < 0 ? " neg" : ""), (x < 0 ? "-$" : "$") + Math.abs(x).toFixed(2));
}
function sci(x) { return isNum(x) ? el("span", "num", x.toExponential(2)) : unknown(); }
function age(sec) {
  if (!isNum(sec)) return "UNKNOWN";
  const a = Math.abs(sec);
  const t = a < 90 ? Math.round(a) + " s" : a < 5400 ? Math.round(a / 60) + " min" :
    a < 172800 ? (a / 3600).toFixed(1) + " h" : (a / 86400).toFixed(1) + " d";
  return sec < 0 ? "in " + t : t + " ago";
}
function utc(ts) {
  if (!isNum(ts)) return "UNKNOWN";
  return new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC";
}
function nowS() { return Date.now() / 1000; }
function kv(rows) {
  const dl = el("dl", "kv");
  for (const [k, v] of rows) {
    add(dl, el("dt", null, k));
    const dd = el("dd");
    if (v instanceof Node) dd.appendChild(v); else dd.textContent = v === null || v === undefined ? "UNKNOWN" : String(v);
    dl.appendChild(dd);
  }
  return dl;
}
function card(title, ...kids) {
  const c = el("section", "card");
  if (title) { const t = el("h2", "card-title"); if (title instanceof Node) t.appendChild(title); else t.textContent = title; c.appendChild(t); }
  return add(c, ...kids);
}
function grid(cls, ...kids) { return add(el("div", cls), ...kids); }
function note(text) { return el("p", "muted mt-2", text); }

async function api(path) {
  const r = await fetch(path, { method: "GET", cache: "no-store", credentials: "same-origin",
                                headers: { Accept: "application/json" } });
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  if (!r.ok) throw new Error((body && body.error) || ("HTTP " + r.status));
  return body;
}

function header(meta) {
  const mode = document.getElementById("mode");
  const banner = document.getElementById("banner");
  const rm = meta && meta.read_model;
  if (!rm) {
    mode.className = "chip chip-unv"; mode.textContent = "NO READ MODEL";
    banner.textContent = "Read model not built: run python tools/nb_control.py build";
    banner.classList.remove("hidden");
    return;
  }
  mode.className = STATUS_CLASS[rm.data_mode] || "chip chip-muted";
  mode.textContent = String(rm.data_mode || "UNKNOWN").replace(/_/g, " ");
  mode.title = "read model built " + age(rm.age_s);
  if (rm.banner) {
    banner.textContent = rm.banner + " Built " + age(rm.age_s) + ".";
    banner.classList.remove("hidden");
  } else banner.classList.add("hidden");
}

function setActive(name) {
  for (const a of document.querySelectorAll("[data-view]")) {
    a.classList.toggle("active", a.getAttribute("data-view") === name);
  }
}

/* ------------------------------------------------------------------ views */
const SEVERITY = ["INTEGRITY_FAILURE", "DISCONNECTED", "RATE_LIMITED", "STALE", "DEGRADED",
  "UNVERIFIED", "NOT_TESTED", "HISTORICAL_ONLY", "NOT_SUPPORTED", "HEALTHY"];

function oppCard(o, compact) {
  const c = el("article", "card");
  const top = grid("flex flex-wrap items-center gap-1.5");
  add(top, el("a", "mr-auto text-sm font-semibold text-slate-100", o.asset));
  top.firstChild.href = "#/opp/" + o.id;
  add(top, chip(o.strategy), chip(o.label), o.safety === "NOT_SAFE" ? chip("NOT_SAFE", "positive expected net with negative or unknown stress net") : null);
  add(c, top, el("p", "muted mt-1", "long " + o.long + "  /  short " + o.short + "  ·  $" + (o.size_usd || "?")));
  add(c, grid("mt-2", kv([["expected net", usd(o.expected_net, "not evaluated")],
                          ["stress net", usd(o.stress_net, "not evaluated")],
                          ["carry / 8h", isNum(o.carry_8h) ? el("span", "num", (o.carry_8h * 100).toFixed(4) + " %") : unknown()],
                          ["gap", isNum(o.gap_bp) ? el("span", "num", o.gap_bp.toFixed(1) + " bp") : unknown()]])));
  if (!compact) {
    add(c, grid("mt-2", kv([["breakeven", isNum(o.breakeven_h) ? o.breakeven_h.toFixed(1) + " h" : null],
                            ["freshness", o.freshness || "UNKNOWN"],
                            ["evidence", o.evidence_level || "UNKNOWN"],
                            ["execution model", o.execution_model || "UNKNOWN"],
                            ["hedge reference", o.reference || "UNKNOWN"],
                            ["STRICT first rejection", o.strict_first_rejection || "-"],
                            ["ESTIMATED first rejection", o.estimated_first_rejection || "-"]])));
    const fm = o.funding_models;
    if (fm && fm.net) {
      // Phase 15 P0-B: three funding models, ESTIMATED; null = UNKNOWN, never zero
      add(c, el("p", "muted mt-3", "Funding models (ESTIMATED, " + (fm.hours || "?") +
        " h; baseline = published rate held, comparison only)"));
      add(c, grid("mt-1", kv([["baseline net", usd(fm.net.baseline, "UNKNOWN")],
                              ["persistence net", usd(fm.net.persistence, "UNKNOWN: no settled history")],
                              ["adverse net", usd(fm.net.adverse, "UNKNOWN: no settled history")],
                              ["model confidence", fm.confidence || "UNKNOWN"]])));
    }
  } else {
    add(c, el("p", "muted mt-2", "rejected: " + (o.estimated_first_rejection || o.strict_first_rejection || "-")));
  }
  if (o.flags && o.flags.length) {
    const f = grid("mt-2 flex flex-wrap gap-1");
    for (const x of o.flags) add(f, el("span", "chip chip-muted", x.replace(/_/g, " ")));
    add(c, f);
  }
  return c;
}

async function vHome() {
  const [ov, meta] = await Promise.all([api("/api/doctor/overview"), api("/api/meta")]);
  header(meta);
  const out = el("div", "grid gap-3 md:grid-cols-2");
  const sr = ov.system_running || {};
  const sys = ov.system || {};
  const run = card("Is it running?",
    kv([["recorder", chip(sr.recorder)], ["PAPER engine", chip(sr.paper_engine)],
        ["control server", chip("RUNNING", sr.control_server)],
        ["read model built", meta.read_model ? age(meta.read_model.age_s) : "UNKNOWN"],
        ["last venue observation", isNum(ov.last_observation_at) ? age(nowS() - ov.last_observation_at) : "UNKNOWN"],
        ["clock", chip((sys.clock || {}).status, (sys.clock || {}).reason)],
        ["disk", chip((sys.disk || {}).status, (sys.disk || {}).reason)]]),
    note("Live / historical / simulation: " + ((ov.data_kinds || {}).live || "") + ". " +
         ((ov.data_kinds || {}).simulation || "")));
  const venues = (ov.venues || []).slice().sort((a, b) => SEVERITY.indexOf(a.health) - SEVERITY.indexOf(b.health));
  const vl = el("ul", "divide-y divide-slate-800");
  for (const v of venues) {
    const li = el("li", "flex items-center gap-2 py-1.5");
    add(li, link("#/venue/" + v.exchange_id, v.exchange_id, "link mr-auto truncate text-sm"),
        v.paper_venue ? el("span", "chip chip-paper", "PAPER") : el("span", "chip chip-na", "OBS"),
        chip(v.health), el("span", "muted w-20 text-right", isNum(v.last_verification_age_s) ? age(v.last_verification_age_s) : "never"));
    vl.appendChild(li);
  }
  const vc = card("Venues (worst first)", vl, note("Offline, no venue can be HEALTHY: past live checks are HISTORICAL ONLY."));
  const opps = (ov.opportunities || {}).top || [];
  const oc = card("Worth research");
  if (!opps.length) oc.appendChild(note("No opportunity passed the research filters in the last scan."));
  for (const o of opps) oc.appendChild(grid("mt-2", oppCard(o, true)));
  const counts = (ov.opportunities || {}).counts || {};
  oc.appendChild(note("Scan " + utc((ov.opportunities || {}).snapshot_at) + ": " + (counts.raw_positive_edge ?? "?") +
    " raw edges, " + (counts.expected_net_positive ?? "?") + " expected-positive, " + (counts.stress_positive ?? "?") +
    " stress-positive, " + ((ov.opportunities || {}).strict_eligible ?? "UNKNOWN") + " STRICT eligible."));
  oc.appendChild(link("#/opps", "All opportunities →"));
  const pp = ov.paper_positions || {};
  const pc = card("PAPER positions (simulated)", kv([["positions", pp.total], ["open", pp.open], ["reliably valued", pp.reliable], ["never filled", pp.never_filled]]),
    note("Fills are ESTIMATED from observed public data; none is a real fill."), link("#/paper", "Positions →"));
  const sec = ov.doctor_sections || {};
  const dl = grid("flex flex-wrap gap-1.5");
  for (const k of Object.keys(sec)) add(dl, add(el("span", "flex items-center gap-1 text-xs text-slate-400"), el("span", null, k), chip(sec[k])));
  const dc = card("Doctor", dl, grid("mt-2 flex gap-3", link("#/doctor", "Doctor →"), link("#/markets", "Markets →"),
    link("#/funding", "Funding persistence →"), link("#/history", "History →")));
  add(out, run, vc, oc, pc, dc);
  return out;
}

async function vVenues() {
  const d = await api("/api/doctor/exchanges");
  const out = el("div");
  add(out, el("h1", "h1", "Exchanges"), note("Evidence level per capability. LIVE OBSERVED is a past observation: it is current only when checked within the last hour."));
  const g = el("div", "mt-3 grid gap-3 md:grid-cols-2");
  for (const e of d.items || []) {
    const c = card(null);
    const t = grid("mb-2 flex flex-wrap items-center gap-1.5");
    add(t, link("#/venue/" + e.exchange_id, e.exchange_id, "link mr-auto text-sm font-semibold"),
        e.paper_venue ? el("span", "chip chip-paper", "PAPER") : el("span", "chip chip-na", "OBSERVATION"), chip(e.health));
    add(c, t, el("p", "muted", "last verification: " + (isNum(e.last_verification_age_s) ? age(e.last_verification_age_s) : "never") +
      (e.last_verification_current ? " (current)" : " (not current)") + " · problems: " + (e.problems || []).length));
    const caps = grid("mt-2 grid grid-cols-2 gap-1 sm:grid-cols-3");
    for (const [k, v] of Object.entries(e.capabilities || {})) {
      add(caps, add(el("div", "flex items-center justify-between gap-1 rounded bg-slate-950/60 px-1.5 py-1"),
        el("span", "truncate text-[11px] text-slate-400", k.replace(/_/g, " ")), chip(v.level)));
    }
    add(c, caps);
    g.appendChild(c);
  }
  return add(out, g);
}

async function vVenue(id) {
  const d = await api("/api/doctor/exchanges/" + encodeURIComponent(id));
  const e = d.item;
  const out = el("div");
  add(out, link("#/venues", "← Exchanges"), el("h1", "h1 mt-2", e.exchange_id));
  add(out, card("Status", kv([["role", e.paper_venue ? "PAPER venue" : "observation only"], ["venue status", e.venue_status],
    ["health", chip(e.health)], ["last verification", isNum(e.last_successful_verification) ? utc(e.last_successful_verification) : "never"],
    ["current", e.last_verification_current ? "yes" : "no"]])));
  const t = el("table", "tbl");
  add(t, add(el("thead"), add(el("tr"), el("th", null, "capability"), el("th", null, "level"), el("th", "hidden sm:table-cell", "value"), el("th", null, "seen"))));
  const tb = el("tbody");
  for (const [k, v] of Object.entries(e.capabilities || {})) {
    add(tb, add(el("tr"), el("td", "text-slate-300", k.replace(/_/g, " ")), add(el("td"), chip(v.level)),
      el("td", "hidden max-w-xs break-words text-xs text-slate-400 sm:table-cell", v.value || ""),
      el("td", "text-xs text-slate-400", isNum(v.as_of) ? age(nowS() - v.as_of) : "-")));
  }
  add(out, grid("mt-3", card("Capabilities", grid("overflow-x-auto", add(t, tb)))));
  const pl = el("ul", "space-y-1.5 text-sm");
  for (const p of e.problems || []) add(pl, add(el("li", "flex flex-wrap items-start gap-1.5"), chip(p.status), el("span", "text-slate-300", p.dimension + " / " + (p.channel || "-") + ": " + p.reason)));
  if (!(e.problems || []).length) pl.appendChild(el("li", "muted", "No problem records."));
  add(out, grid("mt-3", card("Problems", pl)));
  return out;
}

function select(options, value, onchange, label) {
  const wrap = el("label", "block text-xs text-slate-400");
  wrap.appendChild(el("span", "mb-1 block", label));
  const s = el("select", "sel");
  for (const [v, t] of options) { const o = el("option", null, t); o.value = v; if (v === value) o.selected = true; s.appendChild(o); }
  s.addEventListener("change", () => onchange(s.value));
  wrap.appendChild(s);
  return wrap;
}
function pager(p, onpage) {
  const w = grid("mt-3 flex items-center justify-between gap-2");
  const prev = el("button", "btn", "← Prev"); prev.disabled = p.page <= 1;
  const next = el("button", "btn", "Next →"); next.disabled = p.page >= p.pages;
  prev.addEventListener("click", () => onpage(p.page - 1));
  next.addEventListener("click", () => onpage(p.page + 1));
  return add(w, prev, el("span", "muted", "page " + p.page + " / " + Math.max(1, p.pages) + " · " + p.total + " rows"), next);
}

async function vOpps() {
  const f = state.oppFilters;
  const q = new URLSearchParams({ page: String(state.oppPage), page_size: "20", sort: f.sort });
  for (const k of ["strategy", "label", "safety"]) if (f[k]) q.set(k, f[k]);
  const [d, s] = await Promise.all([api("/api/opportunities?" + q.toString()), api("/api/opportunities/summary")]);
  const out = el("div");
  add(out, el("h1", "h1", "Opportunity scanner"),
      note("Snapshot " + utc(s.snapshot_at) + " (" + age(s.snapshot_age_s) + "). " + (s.note ? s.note.replace(/\.?$/, ".") : "") +
           " Research only: nothing here is verified execution."));
  const c = s.counts || {};
  add(out, grid("mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4",
    card(null, el("p", "muted", "raw positive edge"), el("p", "num text-lg", c.raw_positive_edge ?? "UNKNOWN")),
    card(null, el("p", "muted", "expected net > 0"), el("p", "num text-lg", c.expected_net_positive ?? "UNKNOWN")),
    card(null, el("p", "muted", "stress net > 0"), el("p", "num text-lg", c.stress_positive ?? "UNKNOWN")),
    card(null, el("p", "muted", "STRICT eligible"), el("p", "num text-lg", (s.execution_readiness || {}).strict_paper_eligible ?? "UNKNOWN"))));
  const rej = Object.entries(s.estimated_first_rejection || {}).slice(0, 5);
  if (rej.length) add(out, grid("mt-2", note("Most common rejections: " + rej.map(([k, v]) => k + " " + v).join(" · "))));
  const set = (k) => (v) => { state.oppFilters[k] = v; state.oppPage = 1; render(); };
  add(out, grid("mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4",
    select([["", "all"], ["funding", "funding"], ["price_gap", "price gap"], ["hybrid", "hybrid"]], f.strategy, set("strategy"), "strategy"),
    select([["", "all"], ["ESTIMATED_PAPER_CANDIDATE", "estimated candidate"], ["WATCHLIST", "watchlist"], ["BLOCKED_ECONOMICS", "blocked economics"], ["DATA_STALE", "data stale"]], f.label, set("label"), "label"),
    select([["", "all"], ["NOT_SAFE", "not safe"], ["RESEARCH_ONLY", "research only"]], f.safety, set("safety"), "safety"),
    select([["expected_net", "expected net"], ["stress_net", "stress net"], ["risk_adjusted_score", "risk adjusted"], ["gap_bp", "gap"], ["carry_8h", "carry"], ["asset", "asset"]], f.sort, set("sort"), "sort")));
  const g = el("div", "mt-3 grid gap-2 lg:grid-cols-2");
  for (const o of d.items || []) g.appendChild(oppCard(o, false));
  if (!(d.items || []).length) g.appendChild(note("No rows for these filters."));
  add(out, g, pager(d, (p) => { state.oppPage = p; render(); }));
  return out;
}

async function vOpp(id) {
  const d = await api("/api/opportunities/" + encodeURIComponent(id));
  const o = d.item;
  const out = el("div");
  add(out, link("#/opps", "← Opportunities"), grid("mt-2", oppCard(o, false)));
  const rl = el("ul", "list-inside list-disc text-sm text-slate-300");
  for (const r of o.rejections || []) rl.appendChild(el("li", null, r));
  add(out, grid("mt-3", card("All rejection reasons", rl)));
  add(out, grid("mt-3", card("Funding", kv([["evidence", (o.funding_evidence || []).join(", ") || "UNKNOWN"],
    ["carry / 8h", isNum(o.carry_8h) ? (o.carry_8h * 100).toFixed(4) + " %" : null]]),
    note("A published-only rate (TxFlow, Variational) is valid for its current period only; see Funding persistence."))));
  return out;
}

async function vMarkets() {
  const q = new URLSearchParams({ page: String(state.mktPage), page_size: "25" });
  if (state.mktVenue) q.set("venue", state.mktVenue);
  const d = await api("/api/doctor/markets?" + q.toString());
  const out = el("div");
  add(out, el("h1", "h1", "Markets"), note("One row per reference asset from the last scan (historical)."));
  add(out, grid("mt-3 max-w-xs", select([["", "all venues"], ["TxFlow", "TxFlow"], ["tradeXYZ", "tradeXYZ"], ["Lighter", "Lighter RH"], ["Arcus", "Arcus"], ["Variational", "Variational"], ["RISEx", "RISEx"]],
    state.mktVenue, (v) => { state.mktVenue = v; state.mktPage = 1; render(); }, "venue")));
  const t = el("table", "tbl mt-3");
  add(t, add(el("thead"), add(el("tr"), el("th", null, "asset"), el("th", "hidden sm:table-cell", "venues"), el("th", null, "best exp."), el("th", null, "stress"), el("th", "hidden md:table-cell", "top rejection"))));
  const tb = el("tbody");
  for (const m of d.items || []) {
    add(tb, add(el("tr"), el("td", "font-semibold text-slate-200", m.asset), el("td", "hidden text-xs text-slate-400 sm:table-cell", (m.venues || []).join(", ")),
      add(el("td"), usd(m.best_expected_net, "not evaluated")), add(el("td"), usd(m.best_stress_net, "not evaluated")),
      el("td", "hidden text-xs text-slate-400 md:table-cell", m.top_rejection || "-")));
  }
  add(out, grid("overflow-x-auto", add(t, tb)), pager(d, (p) => { state.mktPage = p; render(); }));
  return out;
}

async function vPaper() {
  const d = await api("/api/paper/positions?page_size=50" + (state.scenarios ? "&include_scenarios=1" : ""));
  const out = el("div");
  add(out, el("h1", "h1", "PAPER positions"),
      note("Simulated on observed public data. Fills are ESTIMATED, never real; funding from published rates is UNVERIFIED as a payment."));
  const tg = el("button", "btn mt-2", state.scenarios ? "Hide stress scenarios" : "Show stress scenarios");
  tg.addEventListener("click", () => { state.scenarios = !state.scenarios; render(); });
  out.appendChild(tg);
  const g = el("div", "mt-3 grid gap-3 lg:grid-cols-2");
  for (const p of d.items || []) {
    const c = card(null);
    const t = grid("flex flex-wrap items-center gap-1.5");
    add(t, el("span", "mr-auto text-sm font-semibold text-slate-100", p.asset + " · " + p.strategy), chip(p.state),
        p.valuation_reliable === null ? chip("NOT_TESTED", "never filled: nothing to value") :
          p.valuation_reliable ? chip("OK", "valuation reliable") : chip("UNVERIFIED", "valuation not reliable"),
        p.scenario_only ? el("span", "chip chip-na", "SCENARIO " + p.scenario) : null);
    add(c, t, el("p", "muted mt-1", "long " + p.long + " / short " + p.short + " · $" + p.size_usd + " · " + p.id));
    const pnl = p.pnl || {};
    const fe = pnl.funding || {};
    add(c, grid("mt-2", kv([["opened", utc(p.opened_at)], ["closed", p.closed_at ? utc(p.closed_at) + " (" + (p.close_reason || "") + ")" : "open"],
      ["expected net at entry", usd((p.expected || {}).net)], ["stress net at entry", usd((p.expected || {}).stress)],
      ["price PnL", usd(pnl.price_pnl)], ["fees", usd(isNum(pnl.fees) ? -pnl.fees : null)],
      ["funding: settled rate", usd(fe.SETTLED_RATE)], ["funding: published estimate", usd(fe.PUBLISHED_ESTIMATE)],
      ["funding: carried forward", usd(fe.PUBLISHED_CARRIED_FORWARD)], ["realized total", usd(pnl.realized_total)],
      ["unrealized", usd(pnl.unrealized, "no aligned marks")]])));
    if ((p.valuation_problems || []).length) {
      const ul = el("ul", "mt-2 list-inside list-disc text-xs text-amber-300");
      for (const x of p.valuation_problems) ul.appendChild(el("li", null, x));
      c.appendChild(ul);
    }
    const ft = el("table", "tbl mt-2 text-xs");
    add(ft, add(el("thead"), add(el("tr"), el("th", null, "leg"), el("th", null, "action"), el("th", null, "price"), el("th", null, "label"))));
    const fb = el("tbody");
    for (const f of p.fills || []) add(fb, add(el("tr"), el("td", null, f.leg), el("td", null, f.action), add(el("td"), num(f.price, 4)), el("td", "text-amber-300", f.label + (f.verified ? "" : " (not real)"))));
    add(c, grid("overflow-x-auto", add(ft, fb)));
    g.appendChild(c);
  }
  if (!(d.items || []).length) g.appendChild(note("No PAPER positions."));
  return add(out, g);
}

async function vDoctor() {
  const [ov, sys, rd] = await Promise.all([api("/api/doctor/overview"), api("/api/doctor/system"), api("/api/strategies/readiness")]);
  const out = el("div");
  add(out, el("h1", "h1", "Doctor"), note("Offline doctor: reads saved artifacts and local system state; it never contacts a venue. CLI: python tools/nb_doctor.py"));
  const sec = ov.doctor_sections || {};
  const sl = el("ul", "grid grid-cols-2 gap-1.5 sm:grid-cols-3");
  for (const k of Object.keys(sec)) add(sl, add(el("li", "flex items-center justify-between gap-1 rounded bg-slate-950/60 px-2 py-1"), el("span", "truncate text-xs text-slate-300", k), chip(sec[k])));
  add(out, grid("mt-3", card("Sections", sl)));
  const ul = el("ul", "space-y-1.5 text-sm");
  for (const r of sys.items || []) add(ul, add(el("li", "flex flex-wrap items-start gap-1.5"), chip(r.health_status), el("span", "font-medium text-slate-200", r.channel), el("span", "text-slate-400", r.health_reason)));
  add(out, grid("mt-3", card("System", ul)));
  const t = el("table", "tbl");
  add(t, add(el("thead"), add(el("tr"), el("th", null, "strategy"), el("th", null, "status"), el("th", null, "exp > 0"), el("th", null, "stress > 0"), el("th", "hidden sm:table-cell", "top STRICT rejection"))));
  const tb = el("tbody");
  for (const [k, v] of Object.entries(rd.items || {})) {
    add(tb, add(el("tr"), el("td", "font-medium", k), add(el("td"), chip(v.status)), el("td", "num", v.expected_net_positive), el("td", "num", v.stress_positive),
      el("td", "hidden text-xs text-slate-400 sm:table-cell", Object.keys(v.strict_first || {})[0] || "-")));
  }
  add(out, grid("mt-3", card("Strategy readiness", grid("overflow-x-auto", add(t, tb)), note(rd.note || ""))));
  return out;
}

async function vFunding() {
  const d = await api("/api/funding/persistence");
  const out = el("div");
  add(out, el("h1", "h1", "Funding persistence"), note(d.label || d.reason || ""));
  if (d.semantics && d.semantics.length) {
    // Phase 15 P0: a published rate is valid for its current period only
    const KIND = { CURRENT_PERIOD_RUNNING_CONVERGES: "running, ends at payment",
      CURRENT_PERIOD_RUNNING_PAYMENT_UNKNOWN: "running, payment UNKNOWN",
      LAST_SETTLED_PROJECTION: "last settlement reused",
      PUBLISHED_SEMANTICS_UNVERIFIED: "UNVERIFIED", UNKNOWN: "UNKNOWN" };
    const st = el("table", "tbl");
    add(st, add(el("thead"), add(el("tr"), el("th", null, "venue"), el("th", null, "published value"), el("th", "hidden sm:table-cell", "sign"), el("th", "hidden sm:table-cell", "settled history"))));
    const sb = el("tbody");
    for (const r of d.semantics) {
      add(sb, add(el("tr"), el("td", "text-xs", r.exchange_id),
        add(el("td", "text-xs"), el("span", null, (KIND[r.rate_kind] || "UNKNOWN") + " (" + (r.rate_kind_evidence || "UNKNOWN") + ")")),
        el("td", "hidden text-xs sm:table-cell", (r.positive_pays ? "+ = " + r.positive_pays + " pays" : "UNKNOWN") + " (" + (r.sign_evidence || "UNKNOWN") + ")"),
        el("td", "hidden text-xs sm:table-cell", r.settled_history ? "yes" : "none")));
    }
    add(out, grid("mt-3", card("What each venue's funding field means", grid("overflow-x-auto", add(st, sb)),
      note("A published value is valid for its current funding period only; later hours are models (ESTIMATED)."))));
  }
  if (d.status !== "OK") return add(out, card(null, chip(d.status), note(d.reason || "")));
  const c = d.natgas_case || {};
  const ref = c.reference || {};
  const t = el("table", "tbl");
  add(t, add(el("thead"), add(el("tr"), el("th", null, "model"), el("th", null, "24 h carry"), el("th", null, "% / 8h"))));
  const tb = el("tbody");
  for (const [k, m] of Object.entries(c.models || {})) add(tb, add(el("tr"), el("td", null, k), add(el("td"), usd(m.cash_24h, "a leg is UNKNOWN after its period")), add(el("td"), num(m.carry_8h_pct, 3))));
  add(out, grid("mt-3", card("NATGAS: long TxFlow / short tradeXYZ at " + (ref.at || "?") + " (UTC+3)",
    kv([["TxFlow published / h", sci(ref.txflow_rate_h)], ["tradeXYZ published / h", sci(ref.xyz_rate_h)], ["carry at reference", num(ref.carry_8h_pct, 3)]]),
    grid("mt-2 overflow-x-auto", add(t, tb)), note(c.txflow_payment_evidence || ""))));
  const nl = el("ul", "space-y-1 text-xs text-slate-300");
  for (const n of c.next_observations || []) nl.appendChild(el("li", "num", n.at + "  TxFlow " + (isNum(n.txflow_rate_h) ? n.txflow_rate_h.toExponential(2) : "?") + "  xyz " + (isNum(n.xyz_rate_h) ? n.xyz_rate_h.toExponential(2) : "?") + "  carry " + (isNum(n.carry_8h_pct) ? n.carry_8h_pct.toFixed(3) : "?") + " %/8h"));
  add(out, grid("mt-3", card("What followed (published, not paid)", nl)));
  const vt = el("table", "tbl");
  add(vt, add(el("thead"), add(el("tr"), el("th", null, "venue"), el("th", null, "markets"), el("th", null, "validated"), el("th", "hidden sm:table-cell", "best model"))));
  const vb = el("tbody");
  for (const [k, v] of Object.entries(d.by_venue || {})) {
    const best = Object.entries(v.best_model || {}).sort((a, b) => b[1] - a[1])[0];
    add(vb, add(el("tr"), el("td", null, k), el("td", "num", v.markets), el("td", "num", v.validated), el("td", "hidden text-xs sm:table-cell", best ? best[0] + " (" + best[1] + ")" : "-")));
  }
  add(out, grid("mt-3", card("Settled histories (7 days, chronological validation)", grid("overflow-x-auto", add(vt, vb)))));
  return out;
}

async function vHistory() {
  const d = await api("/api/health/history");
  const out = el("div");
  add(out, el("h1", "h1", "History"));
  const rl = el("ul", "space-y-2");
  for (const r of (d.doctor_runs || []).slice().reverse()) {
    const li = el("li");
    add(li, el("p", "text-sm text-slate-200", utc(r.at)));
    const g = grid("mt-1 flex flex-wrap gap-1");
    for (const [k, v] of Object.entries(r.sections || {})) add(g, add(el("span", "flex items-center gap-1 text-[11px] text-slate-400"), el("span", null, k), chip(v)));
    rl.appendChild(add(li, g));
  }
  if (!(d.doctor_runs || []).length) rl.appendChild(note("No saved Doctor runs."));
  add(out, card("Doctor runs (last 5)", rl));
  const rec = d.recorder || [];
  add(out, grid("mt-3", card("Recorder health history (24 h)", rec.length ? el("p", "text-sm", rec.length + " heartbeat points") :
    note("No recorder history: the recorder is not running (HISTORICAL data only)."))));
  return out;
}

/* ----------------------------------------------------------------- router */
const ROUTES = [
  [/^#?\/?$/, "home", () => vHome()],
  [/^#\/venues$/, "venues", () => vVenues()],
  [/^#\/venue\/([a-z0-9_]{1,32})$/, "venues", (m) => vVenue(m[1])],
  [/^#\/markets$/, "markets", () => vMarkets()],
  [/^#\/opps$/, "opps", () => vOpps()],
  [/^#\/opp\/(op-[0-9]{5})$/, "opps", (m) => vOpp(m[1])],
  [/^#\/paper$/, "paper", () => vPaper()],
  [/^#\/doctor$/, "doctor", () => vDoctor()],
  [/^#\/funding$/, "funding", () => vFunding()],
  [/^#\/history$/, "history", () => vHistory()],
];

async function render() {
  const hash = location.hash || "#/";
  let name = null, fn = null, m = null;
  for (const [re, n, f] of ROUTES) { m = hash.match(re); if (m) { name = n; fn = f; break; } }
  if (!fn) { name = "home"; fn = () => vHome(); }
  setActive(name);
  const token = {};
  current = token;
  try {
    const node = await fn(m);
    if (current !== token) return;
    if (name !== "home") api("/api/meta").then(header).catch(() => {});
    view.replaceChildren(node);
  } catch (e) {
    if (current !== token) return;
    view.replaceChildren(card("Cannot load", el("p", "text-sm text-rose-300", String(e.message || e)),
      note("The panel only reads the local read model. If the server stopped, restart: python tools/nb_control.py serve")));
  }
}

function schedule() {
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) render(); }, REFRESH_MS);
}

window.addEventListener("hashchange", () => { window.scrollTo(0, 0); render(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) render(); });
render();
schedule();
