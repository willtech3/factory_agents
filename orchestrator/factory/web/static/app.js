/* Factory Console: the factory floor. Vanilla JS, hash routes, no build step. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ helpers
  const $main = document.getElementById("main");
  const $foot = document.getElementById("side-foot");
  const $inboxCount = document.getElementById("inbox-count");
  const SVG = "http://www.w3.org/2000/svg";

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "html") node.innerHTML = v;
        else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
        else if (k === "dataset") Object.assign(node.dataset, v);
        else node.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  function svg(tag, attrs, ...children) {
    const node = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) node.setAttribute(k, v);
    for (const c of children.flat()) if (c) node.append(c);
    return node;
  }

  async function api(path, opts) {
    const r = await fetch(path, opts && opts.body ? { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts.body) } : opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const err = new Error(data.detail || data.error || `${r.status} ${r.statusText}`); err.status = r.status; throw err; }
    return data;
  }

  // The one model of a run (docs/10-console-design-system.md): four phases group seven stations on one line. Every view
  // that shows where a run is (the Runs list, the run page, the Board, the gate pages) draws from this table and nothing else.
  const STATIONS = [
    { key: "intake", label: "Outcome", phase: "plan", who: "Product owner", doing: "The brief, with its metrics." },
    { key: "decomposing", label: "Decompose", phase: "plan", who: "the crew", role: "decomposer", doing: "The decomposer turns the outcome into work items." },
    { key: "gate_1_decomposition", label: "Gate 1", phase: "plan", who: "Product owner", gate: "gate_1_decomposition", doing: "A person approves the plan." },
    { key: "specifying", label: "Specify", phase: "specify", who: "the crew", role: "spec_writer", doing: "Writers draft a contract per item; a judge checks the set." },
    { key: "gate_2_spec_readiness", label: "Gate 2", phase: "specify", who: "Product owner", gate: "gate_2_spec_readiness", doing: "A person approves the contracts." },
    { key: "implementing", label: "Build", phase: "build", who: "the crew", role: "implementer", doing: "Implement, review, judge, and open a pull request per item." },
    { key: "completed", label: "Pull requests", phase: "ship", who: "Engineer", doing: "An engineer reviews and merges; the dev deploy is observed." },
  ];
  const PHASES = { plan: "Plan", specify: "Specify", build: "Build", ship: "Ship" };
  const PHASE_DOES = { plan: "outcome, decompose, Gate 1", specify: "contracts, Gate 2", build: "implement, review, judge", ship: "pull requests, dev" };
  const stationIndex = stage => STATIONS.findIndex(s => s.key === stage);
  const TERMINAL = { closed: "Closed", escalated: "Escalated", failed: "Failed", paused_budget: "Paused at budget" };
  const stageLabel = key => TERMINAL[key] || (STATIONS.find(st => st.key === key) || {}).label || words(key);
  // a terminal or parked run keeps the station it reached; the status says what happened there
  const stationOf = r => {
    const i = stationIndex(r.stage);
    if (i >= 0) return i;
    if (r.design_only && r.stage === "completed") return 4;   // a design run ends at Gate 2: nothing is built
    const sm = r.summary || r;
    const stage = r.pause && r.pause.run_stage ? r.pause.run_stage : r.failed_in || sm.failed_in || sm.escalated_in || sm.closed_in || null;
    const j = stationIndex(stage);
    if (j >= 0) return j;
    if (r.stage === "closed" || r.stage === "escalated") { const d = (r.decisions || []).slice(-1)[0]; const k = stationIndex(d && d.kind); if (k >= 0) return k; }
    return (r.counts && r.counts.work_items) ? 5 : 1;
  };
  const RUN_FILTERS = [["all", "All"], ["needs_you", "Needs you"], ["active", "Open"], ["done", "Done"], ["unfinished", "Did not finish"]];
  const runMatches = (r, f) => f === "all" ? true
    : f === "needs_you" ? (r.status === "needs_you" || r.status === "stopped" || (r.waiting && r.waiting.gates + r.waiting.questions > 0) || (r.pull_requests || []).some(p => p.stage === "cr_open"))
    : f === "active" ? !r.terminal
    : f === "done" ? r.status === "completed"
    : ["failed", "closed", "escalated", "unreadable"].includes(r.status);
  const STATUS_TONE = { needs_you: "call", running: "crew", stopped: "bad", paused: "call", completed: "ok", closed: "", failed: "bad", escalated: "warn", unreadable: "bad" };
  const crewNames = roles => { const n = (roles || []).map(w => CREW_NAME[w] || words(w)); return n.length > 1 ? `${n.slice(0, -1).join(", ")} and ${n[n.length - 1]}` : n[0] || "The crew"; };
  // in the middle of a sentence the roles are lower case: "the implementer, reviewer and security reviewer"
  const crewNamesLower = roles => { const n = (roles || []).map(w => (CREW_NAME[w] || words(w)).toLowerCase()); return n.length > 1 ? `the ${n.slice(0, -1).join(", ")} and ${n[n.length - 1]}` : n[0] ? `the ${n[0]}` : "the crew"; };
  const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;
  // actor ids that are not a person's name: the record says "who", the page says the role instead
  const SYSTEM_ACTORS = new Set(["console", "human", "agent", "platform", "orchestrator", "system", "cli", "file", "tracker", "forge", ""]);
  const personName = name => name && !SYSTEM_ACTORS.has(String(name).toLowerCase()) ? name : null;
  const itemShort = id => String(id || "").replace(/^(spec_|wi-)/, "");
  // a session title names its item by id; people read the item's short name
  const prettyTitle = t => String(t || "").replace(/\b(spec_|wi-)([A-Za-z0-9-]+)/g, "$2").replace(/\brun_[a-f0-9]+\s*/g, "");
  // a review round counts its findings either as a total or by severity; an answer either lists dispositions or counts them
  const findingCount = p => p.count != null ? p.count : ["blocking", "major", "minor", "nit"].reduce((a, k) => a + (p[k] || 0), 0);
  const dispositionText = p => { const n = {}; for (const d of (p.dispositions || [])) n[d] = (n[d] || 0) + 1; const parts = Object.entries(n).map(([d, c]) => `${c} ${words(d)}`);
    return parts.length ? parts.join(", ") : [p.fixed != null ? `${p.fixed} fixed` : "", p.refuted ? `${p.refuted} refuted` : "", p.deferred ? `${p.deferred} deferred` : ""].filter(Boolean).join(", "); };
  // the roles a budget pause names as the next step (budget.py STAGE_LABEL keys)
  const FORGE_NAME = { github: "GitHub", gitlab: "GitLab" };
  const NEXT_ROLE = { decompose: "decomposer", specify: "spec_writer", judge: "spec_judge", implement: "implementer", review: "reviewer", security_review: "security_reviewer", merge_judge: "merge_judge", integrate: "integrator" };
  const gateLabel = kind => ({ gate_1_decomposition: "Gate 1", gate_2_spec_readiness: "Gate 2", escalation: "the escalation gate", build_recovery: "the build recovery decision", budget: "the budget gate", deploy_failure: "the deploy-failure gate" })[kind] || words(kind || "a gate");
  // The state of a run, written: a bold lead in the tone of the state, then the reason. One sentence on the Runs list, the
  // same sentence under the title on the run page. Inputs are the summary record (summary.py); nothing here is recorded.
  function statusSentence(r, long) {
    const lead = (text, tone) => el("b", { class: tone || "" }, text + " ");
    const since = r.working_since ? dur(r.working_since) : null;
    const waitingOn = r.open_gates && r.open_gates.length ? r.open_gates[0] : r.open_questions ? "a question" : null;
    const waited = r.updated_at ? dur(r.updated_at) : "";
    const c = r.counts || {};
    const prsText = () => (r.pull_requests || []).length ? r.headline.replace(/^Completed\.?\s*/, "") : "No pull requests were opened.";
    if (r.status === "unreadable") return el("span", { class: "s" }, lead("Unreadable.", "bad"), r.headline);
    switch (r.status) {
      case "needs_you": {
        const what = r.open_questions ? (long ? `${cap(crewNamesLower((r.working && r.working.length ? r.working : ["decomposer"]).slice(0, 1)))} is waiting for your answer,` : "A question is waiting for your answer,")
          : cap(`${waitingOn || "a decision"} is waiting on you,`);
        return el("span", { class: "s" }, lead(what, "call"), `for ${waited}.`, long && r.open_questions === 0 && c.work_items ? ` ${plural(c.work_items, "work item")} so far.` : "");
      }
      case "running": {
        const st = STATIONS[stationOf(r)] || STATIONS[1];
        const doing = { decomposing: "Decomposing.", specifying: "Specifying.", implementing: "Building.", intake: "Taking in the outcome." }[r.stage] || `${st.label}.`;
        const who = r.working && r.working.length ? `${cap(crewNamesLower(r.working))} ${r.working.length > 1 ? "are" : "is"} at work${since ? `, ${since} in` : ""}.` : `Last activity ${ago(r.updated_at)}.`;
        const prs = (r.pull_requests || []).filter(p => p.state === "open");
        return el("span", { class: "s" }, lead(doing, "crew"), who, prs.length ? ` ${plural(prs.length, "pull request")} open.` : "");
      }
      case "paused": {
        const p = r.pause || {}, next = p.next || {};
        if (!p.next) return el("span", { class: "s" }, lead("Paused: a session hit its cap,", "call"), `for ${waited}. Decide its budget gate: run it again with a higher cap, or close the run.`);
        return el("span", { class: "s" }, lead("Paused at its budget,", "call"), `for ${waited}. ${money(p.spent_usd)} of ${money(p.budget_usd)} spent; ${next.label || "the next step"} needs the cap raised to ${money(p.needed_usd)}, or the run closed.`);
      }
      case "stopped": return el("span", { class: "s" }, lead(`Stopped at ${STATIONS[stationOf(r)].label}.`, "bad"), "The controller exited without recording why; a decision here is not picked up until one drives the run again.");
      case "failed": return el("span", { class: "s" }, lead(`Failed ${({ implementing: "while building", decomposing: "while decomposing", specifying: "while specifying", intake: "while taking in the outcome", gate_1_decomposition: "at Gate 1", gate_2_spec_readiness: "at Gate 2" })[r.failed_in] || "while running"}.`, "bad"), r.failure || "", r.failure ? " " : "", long ? "" : "");
      case "escalated": return el("span", { class: "s" }, lead(`Escalated from ${STATIONS[stationOf(r)].label}.`, "warn"), "Review the blockers and choose how to continue.");
      case "closed": { const d = (r.decisions || []).slice(-1)[0] || {}; return el("span", { class: "s" }, lead(`Closed at ${gateLabel(d.kind)}${personName(d.by) ? ` by ${d.by}` : ""}.`, ""), d.comments ? `“${d.comments}”` : "Nothing more happens in this run."); }
      case "completed": return el("span", { class: "s" }, lead("Done.", "ok"), r.design_only ? `Design run: ${plural(c.specs_approved, "contract")} approved, nothing built.` : prsText());
      default: return el("span", { class: "s" }, r.headline || "");
    }
  }
  // the meta line under the sentence: when it started, what it cost, the last thing a person did
  function metaLine(r) {
    const parts = [];
    parts.push(r.terminal ? [`took ${dur(r.started_at, r.ended_at || r.updated_at)}, ended `, timeEl(r.ended_at || r.updated_at)] : ["started ", timeEl(r.started_at)]);
    if (r.cost_usd != null) parts.push([`${money(r.cost_usd)}${r.budget_usd != null ? ` of ${money(r.budget_usd)}` : ""}${r.terminal ? "" : " so far"}${r.live_sessions && !r.terminal ? `, not counting ${plural(r.live_sessions, "open session")}` : ""}`]);
    const d = (r.decisions || []).slice(-1)[0];
    if (d) parts.push([`${cap(gateLabel(d.kind))} ${({ approve: "approved", approve_subset: "approved in part", request_changes: "sent back", reject: "rejected" })[d.decision] || d.decision}${personName(d.by) ? ` by ${d.by}` : ""} `, timeEl(d.at)]);
    return parts.flatMap((p, i) => i ? [" · ", ...p] : p);
  }
  const ITEM_STAGE = {
    pending: "Waiting", spec_drafting: "Contract being written", spec_judging: "Contract being judged", spec_approved: "Contract approved",
    implementing: "Implementing", reviewing: "In review", responding: "Responding to review", merge_judging: "Merge judge",
    integrating: "Opening pull request", cr_open: "Pull request open", merged: "Merged", deploying_dev: "Deploying to dev",
    deployed_dev: "Verified in dev", closed: "Closed", escalated: "Escalated", blocked: "Blocked",
  };
  const GATE_NAME = { gate_1_decomposition: "Gate 1 · the plan", gate_2_spec_readiness: "Gate 2 · the contracts",
    escalation: "Escalation · the judge could not pass the contracts", build_recovery: "Build recovery", budget: "Budget · raise it or close the run", deploy_failure: "Deploy failure" };
  const GATE_SHORT = { gate_1_decomposition: "Gate 1", gate_2_spec_readiness: "Gate 2", escalation: "Escalation", build_recovery: "Build recovery", budget: "Budget", deploy_failure: "Deploy failure" };
  const DECISION_LABEL = { approve: "Approve", approve_subset: "Approve selected", request_changes: "Request changes", reject: "Reject" };
  const ROLE_NAME = { po: "Product owner", eng: "Engineer", ops: "Ops", crew: "The crew" };
  const ROLE_DOES = { po: "decides what gets built", eng: "reviews and merges", ops: "watches it land", crew: "does the work between gates" };
  const CREW_NAME = { app_onboarding: "App onboarding", codebase_researcher: "Researcher", decomposer: "Decomposer", spec_writer: "Spec writer", spec_judge: "Spec judge", implementer: "Implementer",
    reviewer: "Reviewer", security_reviewer: "Security reviewer", merge_judge: "Merge judge", integrator: "Integrator" };
  const CREW_DOES = { app_onboarding: "Learns how to run and test an app", decomposer: "Outcome into work items", spec_writer: "One contract per item", spec_judge: "Scores the contracts", implementer: "Builds it, tests first",
    reviewer: "Tries to break it", security_reviewer: "Attack paths and secrets", merge_judge: "Ready, or another round", integrator: "Opens the pull request" };
  const TOOLS = {
    clipboard: "M26 54h20v16H26z M30 52h12v4H30z M29 60h14v2H29z M29 65h10v2H29z",
    pencil: "M44 50l6 6-14 14-8 2 2-8z M46 48l4-4 6 6-4 4z",
    lens: "M42 56a7 7 0 1 1 .1 0z M43 56a4.5 4.5 0 1 0 .1 0z M47 62l8 8-3 3-8-8z",
    wrench: "M48 50a7 7 0 0 1-8 9l-9 9-4-4 9-9a7 7 0 0 1 9-8l-4 4 3 3z",
    shield: "M44 50l9 3v7c0 6-4 10-9 12-5-2-9-6-9-12v-7z M44 54l5 2v4c0 4-2 6-5 8-3-2-5-4-5-8v-4z",
    gavel: "M28 58l6-6 10 10-6 6z M44 50l6-6 4 4-6 6z M30 70h18v4H30z",
    stamp: "M30 62h20v8H30z M36 50h8v12h-8z M28 70h24v3H28z",
    book: "M28 52h18v18H28z M30 55h14v1.6H30z M30 59h14v1.6H30z M30 63h10v1.6H30z",
  };
  const CREW_TOOL = { app_onboarding: "book", codebase_researcher: "book", decomposer: "clipboard", spec_writer: "pencil", spec_judge: "lens", implementer: "wrench", reviewer: "lens", security_reviewer: "shield", merge_judge: "gavel", integrator: "stamp" };

  function ago(iso) {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }
  function dur(fromIso, toIso) {
    if (!fromIso) return "";
    const s = Math.max(0, ((toIso ? Date.parse(toIso) : Date.now()) - Date.parse(fromIso)) / 1000);
    if (s < 90) return `${Math.round(s)} s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  }
  function clock(iso) { return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZoneName: "short" }) : ""; }
  function fullClock(iso) { return iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : ""; }
  // a time is relative on the page and absolute on hover: <time datetime title="Oct 11, 2026, 05:41">4 min ago</time>
  function timeEl(iso, text) { return el("time", { datetime: iso || null, title: fullClock(iso) }, text != null ? text : ago(iso)); }
  // cut long agent prose at a sentence boundary, never mid-word, and say that there is more
  function firstSentences(text, max) {
    const s = String(text || "").trim();
    if (s.length <= max) return s;
    const head = s.slice(0, max);
    const cut = Math.max(head.lastIndexOf(". "), head.lastIndexOf("! "), head.lastIndexOf("? "));
    if (cut > max * 0.4) return head.slice(0, cut + 1);
    return head.replace(/\s+\S*$/, "") + "…";
  }
  function money(n) { if (n == null) return ""; const v = Number(n); return Number.isInteger(v) ? `$${v}` : `$${v.toFixed(2)}`; }
  function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  function words(s) { return String(s ?? "").replace(/_/g, " "); }
  function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + "s")}`; }

  // ------------------------------------------------------------------ the crew: one pixel Claude, a tool per role, five states
  const PIX = [".1.....1.", ".1111111.", ".1111111.", "111111111", "111111111", ".1111111.", ".11...11.", ".11...11."];
  function pixPath(u, ox, oy) {
    let d = "";
    PIX.forEach((row, y) => { row.split("").forEach((c, x) => { if (c === "1") d += `M${(ox + x * u).toFixed(1)} ${(oy + y * u).toFixed(1)}h${u}v${u}h-${u}z `; }); });
    return d;
  }
  function eyesPath(u, ox, oy) { return [3, 6].map(x => `M${(ox + x * u).toFixed(1)} ${(oy + 3 * u).toFixed(1)}h${u}v${u}h-${u}z`).join(" "); }
  // state: asleep | working | done | blocked | still. size: "l" (72x80), "s" (44x50) or "xs" (28x30, the Runs list)
  const FIG = { l: { u: 6.5, ox: 6.5, oy: 14, w: 72, h: 80 }, s: { u: 4, ox: 4, oy: 9, w: 44, h: 50 }, xs: { u: 2.5, ox: 2.5, oy: 5, w: 28, h: 30 } };
  function crewFigure(role, state, size, label) {
    if (size === "xs") {
      const f = FIG.xs;
      const node = svg("svg", { class: `crew ${state || "still"}`, width: f.w, height: f.h, viewBox: `0 0 ${f.w} ${f.h}`, role: label ? "img" : null, "aria-label": label || null, "aria-hidden": label ? null : "true" });
      node.append(svg("path", { d: pixPath(f.u, f.ox, f.oy), fill: "var(--crew)" }), svg("path", { class: "eye", d: eyesPath(f.u, f.ox, f.oy), fill: "#16202A" }));
      return node;
    }
    const big = size !== "s";
    const u = big ? 6.5 : 4, ox = big ? 6.5 : 4, oy = big ? 14 : 9;
    const w = big ? 72 : 44, h = big ? 80 : 50;
    const node = svg("svg", { class: `crew ${state || "asleep"}`, width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: label ? "img" : null, "aria-label": label || null, "aria-hidden": label ? null : "true" });
    if (state === "asleep") {
      node.append(svg("text", { class: "z", x: big ? 54 : 34, y: big ? 18 : 10, "font-size": big ? 11 : 9, "font-weight": 700, fill: "#5B6B7A", opacity: 0 }, document.createTextNode("z")));
      if (big) node.append(svg("text", { class: "z2", x: 60, y: 10, "font-size": 9, "font-weight": 700, fill: "#5B6B7A", opacity: 0 }, document.createTextNode("z")));
    }
    node.append(svg("path", { d: pixPath(u, ox, oy), fill: "var(--crew)" }));
    node.append(svg("path", { class: "eye", d: eyesPath(u, ox, oy), fill: "#16202A" }));
    const tool = TOOLS[CREW_TOOL[role] || "wrench"];
    if (tool) {
      const g = svg("g", { class: "tool", transform: big ? "" : "scale(0.6) translate(1,0)" });
      g.append(svg("path", { d: tool, fill: "#16202A" }));
      node.append(g);
    }
    const bx = big ? 62 : 38, by = big ? 22 : 13, br = big ? 8 : 5.5;
    const ok = svg("g", { class: "badge-ok" }); ok.append(svg("circle", { cx: bx, cy: by, r: br, fill: "var(--ok)" }), svg("path", { d: big ? "M57.5 22l3 3 6-6" : "M35 13l2 2 4-4", stroke: "#fff", "stroke-width": big ? 2.2 : 1.6, fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round" }));
    const alert = svg("g", { class: "badge-alert" }); alert.append(svg("circle", { cx: bx, cy: by, r: br, fill: "var(--call)" }), svg("rect", { x: bx - 1.2, y: by - 5, width: 2.4, height: 6.5, rx: 1, fill: "#fff" }), svg("circle", { cx: bx, cy: by + 4.2, r: 1.4, fill: "#fff" }));
    node.append(ok, alert);
    return node;
  }
  // a person on the line: grey once they decided ("decided"), orange when the decision is theirs now ("you")
  const HUMAN = ["...111...", "...111...", ".1111111.", "1..111..1", "1..111..1", "...111...", "..11.11..", "..11.11.."];
  function humanFigure(kind, size, label) {
    const f = FIG[size] || FIG.s;
    const node = svg("svg", { class: `human ${kind}`, width: f.w, height: f.h, viewBox: `0 0 ${f.w} ${f.h}`, role: "img", "aria-label": label || (kind === "you" ? "A decision is yours" : "A person decided here") });
    let d = "";
    HUMAN.forEach((row, y) => { row.split("").forEach((c, x) => { if (c === "1") d += `M${(f.ox + x * f.u).toFixed(1)} ${(f.oy + y * f.u).toFixed(1)}h${f.u}v${f.u}h-${f.u}z `; }); });
    node.append(svg("path", { d: d.trim(), fill: kind === "you" ? "var(--call)" : "var(--human)" }));
    return node;
  }
  document.getElementById("brand-mark").append(crewFigure("implementer", "still", "s"));

  // ------------------------------------------------------------------ the line: a belt, seven posts, and whoever is at work standing on it
  // `r` is a summary record (the Runs list, the Board) or a run detail (the run page: it carries crew, gates, questions and
  // summary). size "full" draws names and a fact under each post; "mini" draws only the belt, the posts and the figure.
  function lineFacts(r) {
    const s = r.summary || r, facts = {};
    const decided = kind => (s.decisions || []).filter(d => d.kind === kind).slice(-1)[0];
    const crewAt = roles => (r.crew || []).filter(m => roles.includes(m.role) && m.sessions);
    const sum = ms => ({ n: ms.reduce((a, m) => a + m.sessions, 0), cost: ms.reduce((a, m) => a + (m.cost_usd || 0), 0) });
    const owner = r.outcome && r.outcome.owner ? (r.outcome.owner.name || r.outcome.owner.handle) : null;
    facts.intake = `${owner || "written"} · ${ago(s.started_at || r.created_at)}`;
    const dec = sum(crewAt(["decomposer"])); if (dec.n) facts.decomposing = `${plural(dec.n, "session")} · ${money(dec.cost)}`;
    const sp = sum(crewAt(["spec_writer", "spec_judge"])); if (sp.n) facts.specifying = `${plural(sp.n, "session")} · ${money(sp.cost)}`;
    const bd = sum(crewAt(["implementer", "reviewer", "security_reviewer", "merge_judge", "integrator"])); if (bd.n) facts.implementing = `${plural(bd.n, "session")} · ${money(bd.cost)}`;
    for (const kind of ["gate_1_decomposition", "gate_2_spec_readiness"]) {
      const d = decided(kind);
      if (d) facts[kind] = `${(DECISION_LABEL[d.decision] || d.decision).toLowerCase()} · ${ago(d.at)}`;
    }
    const prs = s.pull_requests || [];
    if (prs.length) { const open = prs.filter(p => p.state === "open").length, closed = prs.filter(p => /closed/.test(p.state || "")).length, merged = prs.length - open - closed;
      facts.completed = [open ? `${open} open` : null, merged ? `${merged} merged` : null, closed ? `${closed} closed` : null].filter(Boolean).join(" · "); }
    return facts;
  }
  function lineStrip(r, size) {
    const s = r.summary || r;
    const mini = size === "mini";
    const stage = r.stage;
    const parked = stage === "paused_budget" || s.status === "stopped";       // the crew sleeps at the station; the run can continue
    const terminal = (!!TERMINAL[stage] && !parked) || stage === "completed";
    const idx = stationOf(s.stage ? s : r);
    const openGates = (r.gates || []).filter(g => !g.recorded).map(g => g.kind).concat((s.open_gates || []).map(label => ({ "Gate 1": "gate_1_decomposition", "Gate 2": "gate_2_spec_readiness" })[label] || label));
    const question = (r.questions || []).length > 0 || (s.open_questions || 0) > 0;
    const working = s.status === "stopped" ? [] : (r.crew || []).filter(c => c.state === "working").map(c => c.role);
    const workingRoles = working.length ? working : (s.working || []);
    const moving = !terminal && s.status !== "stopped" && s.status !== "paused" && (workingRoles.length > 0 || s.status === "running");
    const facts = mini ? {} : lineFacts(r);
    const stations = el("div", { class: "stations" });
    STATIONS.forEach((st, i) => {
      const done = i < idx || (stage === "completed" && (s.design_only ? i === idx : i === STATIONS.length - 1));
      const current = i === idx && !(stage === "completed" && (s.design_only || i === STATIONS.length - 1));
      const isCall = st.gate && openGates.includes(st.gate);
      const cls = ["st", done ? "done" : current ? "current" : "ahead", isCall ? "call" : ""].filter(Boolean).join(" ");
      const figs = el("span", { class: "figs" });
      if (isCall) figs.append(humanFigure("you", mini ? "xs" : "s", `${st.label} is waiting on you`));
      else if (st.gate && done && !mini) figs.append(humanFigure("decided", "s", facts[st.key] ? `${st.label}: ${facts[st.key]}` : `${st.label} decided`));
      else if (current && question) {
        if (!mini && st.role) figs.append(crewFigure(st.role, "asleep", "s", `${CREW_NAME[st.role] || words(st.role)}, paused on a question`));
        figs.append(humanFigure("you", mini ? "xs" : "s", "A question is waiting for your answer"));
      }
      else if (current && st.role && !terminal) {
        if (mini) figs.append(crewFigure(workingRoles[0] || st.role, workingRoles.length ? "working" : "still", "xs", workingRoles.length ? `${crewNames(workingRoles)} at work` : "The crew is here"));
        else {
          const roles = workingRoles.length ? workingRoles.slice(0, 3) : [st.role];
          roles.forEach((role, k) => figs.append(crewFigure(role, workingRoles.length ? "working" : (parked ? "asleep" : "still"), k === 0 ? "l" : "s", `${CREW_NAME[role] || words(role)}${workingRoles.length ? ", at work" : parked ? ", asleep while the run is parked" : ""}`)));
          if (workingRoles.length > 3) figs.append(el("span", { class: "muted fs-xs self-center" }, `+${workingRoles.length - 3}`));
        }
      }
      const here = !current ? null : stage === "paused_budget" ? "Paused here" : s.status === "stopped" ? "Stopped here" : stage === "failed" ? "Failed here" : stage === "escalated" ? "Escalated here" : stage === "closed" ? "Closed here" : null;
      const fact = current && workingRoles.length && st.role
        ? `${workingRoles.length > 1 ? `${workingRoles.length} at work` : `${CREW_NAME[workingRoles[0]] || words(workingRoles[0])} at work`}${s.working_since ? ` · ${dur(s.working_since)} in` : ""}`
        : [here, facts[st.key] || ""].filter(Boolean).join(" · ");
      stations.append(el("div", { class: cls, title: `${st.label}: ${st.doing}` }, figs, el("i", { class: "post" }),
        mini ? null : el("span", { class: "lbl" }, st.label), mini || !fact ? null : el("span", { class: "d" }, fact)));
    });
    return el("div", { class: `line ${mini ? "mini" : ""} ${moving ? "" : "still"}`, role: "img", "aria-label": `Station ${idx + 1} of 7: ${STATIONS[idx].label}` }, el("div", { class: "belt" }), stations);
  }

  // tiny markdown subset for decomposition.md and spec.md
  function md(text) {
    const lines = (text || "").split("\n");
    const out = []; let list = false, code = false, para = [];
    const inline = s => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    const flush = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
    const endList = () => { if (list) { out.push("</ul>"); list = false; } };
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      if (ln.startsWith("```")) { flush(); endList(); out.push(code ? "</pre>" : "<pre class=block>"); code = !code; continue; }
      if (code) { out.push(esc(ln)); continue; }
      const h = ln.match(/^(#{1,6})\s+(.*)$/);
      if (h) { flush(); endList(); const l = Math.min(h[1].length + 1, 6); out.push(`<h${l}>${inline(h[2])}</h${l}>`); continue; }
      if (/^\s*[-*]\s+/.test(ln)) { flush(); if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(ln.replace(/^\s*[-*]\s+/, ""))}</li>`); continue; }
      if (ln.startsWith("|")) {
        flush(); endList(); const rows = [];
        while (i < lines.length && lines[i].startsWith("|")) { if (!/^\|[\s:|-]+\|$/.test(lines[i])) rows.push(lines[i].replace(/^\||\|$/g, "").split("|").map(c => c.trim())); i++; }
        i--;
        if (rows.length) out.push("<table><tr>" + rows[0].map(c => `<th>${inline(c)}</th>`).join("") + "</tr>" +
          rows.slice(1).map(r => "<tr>" + r.map(c => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</table>");
        continue;
      }
      if (!ln.trim()) { flush(); endList(); continue; }
      para.push(ln.trim());
    }
    flush(); endList(); if (code) out.push("</pre>");
    return out.join("\n");
  }

  // ------------------------------------------------------------------ chrome: sidebar, nav, polling
  const state = { config: null, poll: null, lastKey: {} };

  function applyTheme(t) { document.documentElement.dataset.theme = t === "dark" ? "dark" : ""; try { localStorage.setItem("factory-theme", t); } catch (e) { /* private window */ } }
  try { applyTheme(localStorage.getItem("factory-theme") || "light"); } catch (e) { applyTheme("light"); }
  // the crew can be held still from the page itself, for anyone whose system does not ask for reduced motion
  function applyMotion(off) { document.documentElement.dataset.motion = off ? "off" : ""; try { localStorage.setItem("factory-motion", off ? "off" : "on"); } catch (e) { /* private window */ } }
  try { applyMotion(localStorage.getItem("factory-motion") === "off"); } catch (e) { /* private window */ }

  // what the board says is waiting on a person; the sidebar badge and the board header count the same cards
  const onBoard = b => b.runs.filter(r => !r.terminal || r.cards.length);
  const waitingOf = runs => {
    const cards = runs.flatMap(r => r.cards);
    const n = k => cards.filter(c => c.kind === k).length;
    return { gates: n("gate"), questions: n("question"), reviews: n("pr"), total: n("gate") + n("question") + n("pr") };
  };
  const gapsMatching = (gaps, keys) => gaps.filter(g => keys.some(k => g.includes(k)));
  const forgeGapsOf = c => gapsMatching(c.gaps, ["FORGE", "FACTORY_FORGE", "FACTORY_VAULT_ID"]);
  async function loadChrome() {
    try {
      const [c, board] = await Promise.all([api("/api/config"), api("/api/board")]);
      state.config = c;
      const w = waitingOf(onBoard(board)), n = w.total;
      $inboxCount.textContent = n; $inboxCount.hidden = n === 0; $inboxCount.classList.toggle("zero", n === 0);
      $inboxCount.closest("a").setAttribute("aria-label", n ? `Board, ${n} waiting on a person` : "Board");
      const dark = document.documentElement.dataset.theme === "dark", still = document.documentElement.dataset.motion === "off";
      const forgeOk = !forgeGapsOf(c).length;
      // rebuilt only when something in it changed: a poll must not recreate the button a person is about to press
      const footKey = JSON.stringify([c.can_run_design, forgeOk, c.forge, c.forge_repo, dark, still]);
      if (footKey === state.lastKey.foot) return;
      state.lastKey.foot = footKey;
      const pressed = [...$foot.querySelectorAll("button")].indexOf(document.activeElement);   // a button a person just pressed keeps the focus
      $foot.replaceChildren(
        el("b", null, "Crew"), el("span", { class: c.can_run_design ? "" : "warn" }, c.can_run_design ? "ready" : "not configured"),
        el("br"), el("b", null, "Code host"), el("span", { class: forgeOk ? "" : "warn" }, forgeOk ? `${c.forge}${c.forge_repo ? ": " + c.forge_repo : ""}` : "not configured"),
        el("br"), el("button", { class: "theme", type: "button", onclick: () => { applyTheme(dark ? "light" : "dark"); loadChrome(); } }, dark ? "Light mode" : "Dark mode"), " ",
        el("button", { class: "theme", type: "button", onclick: () => { applyMotion(!still); loadChrome(); } }, still ? "Animate" : "Hold still"));
      if (pressed >= 0) { const b = $foot.querySelectorAll("button")[pressed]; if (b) b.focus(); }
    } catch (e) { $foot.textContent = "Console offline"; }
  }
  function setNav(key) {
    document.querySelectorAll("[data-nav]").forEach(a => a.removeAttribute("aria-current"));
    const a = document.querySelector(`[data-nav="${key}"]`);
    if (a) a.setAttribute("aria-current", "page");
  }
  function stopPoll() { if (state.poll) { clearInterval(state.poll); state.poll = null; } }
  function startPoll(fn, ms) { stopPoll(); state.poll = setInterval(() => { if (document.visibilityState === "visible") fn(); }, ms); }
  function typing() {
    const a = document.activeElement;
    return !!(a && $main.contains(a) && (a.tagName === "TEXTAREA" || a.tagName === "INPUT" || a.tagName === "SELECT"));
  }
  function refreshWhenChanged(key, fetchKey, render) {
    const nav = state.nav;   // the page this poll belongs to: a fetch that lands after a route change must not paint
    return async () => {
      if (typing()) return;
      try {
        const k = await fetchKey();
        if (nav !== state.nav) return;
        if (k !== state.lastKey[key]) { state.lastKey[key] = k; await render(); }
        loadChrome();
        delete document.body.dataset.offline;
      } catch (e) { document.body.dataset.offline = "1"; }   // the page keeps what it showed; app.css says contact is lost
    };
  }

  function pageHead(title, sub, actions, crumbs) {
    document.title = `${title} · Factory`;
    return el("div", { class: "page-head" },
      el("div", null, crumbs ? el("div", { class: "crumbs" }, crumbs) : null, el("h1", null, title), sub ? el("div", { class: "sub" }, sub) : null),
      actions ? el("div", { class: "actions" }, actions) : null);
  }
  function card(title, body, meta, cls) {
    return el("section", { class: `card ${cls || ""}` },
      title ? el("div", { class: "card-head" }, el("h2", null, title), meta ? el("span", { class: "meta" }, meta) : null) : null,
      el("div", { class: "card-body" }, body));
  }
  // ------------------------------------------------------------------ the board: rows are who owns the work, columns are where the run is
  const BOARD_COLS = ["plan", "specify", "build", "ship"];
  const boardColumn = col => col === "review" ? "build" : col;     // the server still files reviewers under "review"; Build holds them
  async function viewInbox() {
    setNav("inbox");
    const nav = state.nav;
    const b = await api("/api/board");
    // a finished run stays on the board while a person still owes it something (a pull request to review, a deploy to watch)
    const live = onBoard(b);
    const w = waitingOf(live);
    const atWork = live.flatMap(r => r.cards).filter(c => c.kind === "agent").length;
    const runsAtWork = new Set(live.filter(r => r.cards.some(c => c.kind === "agent")).map(r => r.run_id)).size;
    const parts = [w.gates ? plural(w.gates, "decision") : null, w.questions ? plural(w.questions, "question") : null, w.reviews ? plural(w.reviews, "pull request") : null].filter(Boolean);
    const waitingLabel = parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}` : parts[0] || "";
    const frag = document.createDocumentFragment();
    const sub = el("p", { class: "sub" }, waitingLabel ? el("b", { class: "call" }, `${waitingLabel} ${w.total === 1 ? "is" : "are"} waiting on a person.`) : "Nothing is waiting on a person.",
      atWork ? ` ${plural(atWork, "crew member")} ${atWork === 1 ? "is" : "are"} at work across ${plural(runsAtWork, "run")}.` : "");
    frag.append(pageHead("Board", sub, [el("a", { class: "btn primary", href: "#/start" }, "Start a run")]));
    if (!live.length) {
      frag.append(card(null, el("div", { class: "empty" }, crewFigure("implementer", "asleep", "l"), el("h3", null, "The floor is quiet"),
        el("div", null, "No run is in progress. Start one from an outcome; the crew wakes up and the board fills in."),
        el("a", { class: "btn primary", href: "#/start" }, "Start a run"))));
    } else {
      const g = el("div", { class: "g" });
      g.append(el("div", { class: "corner" }));
      for (const col of BOARD_COLS) g.append(el("div", { class: "colh" }, PHASES[col], " ", el("span", null, PHASE_DOES[col])));
      const rows = ["po", "eng", "ops", "crew"];
      rows.forEach((row, ri) => {
        // on a phone the grid stacks: a role, then only its columns that hold cards, each labelled (data-col)
        const cards = live.flatMap(r => r.cards.filter(c => c.row === row).map(c => [r, c]));
        const last = ri === rows.length - 1 ? " last" : "";
        const n = row === "crew" ? (cards.length ? `${cards.length} at work` : "") : cards.length ? `${cards.length} waiting` : "";
        g.append(el("div", { class: `rowh ${row}${cards.length ? "" : " empty-row"}${last}` }, el("b", null, ROLE_NAME[row]), el("span", null, ROLE_DOES[row]), n ? el("span", { class: "n" }, n) : null));
        for (const col of BOARD_COLS) {
          const cell = el("div", { class: `cell${last}`, dataset: { col: PHASES[col] } });
          // a budget or escalation gate belongs where the run stopped, not in a fixed column
          const columnOf = (r, c) => c.kind === "gate" && (c.title === "budget" || c.title === "escalation") ? boardColumn(r.column) : boardColumn(c.column);
          for (const [r, c] of cards) if (columnOf(r, c) === col) cell.append(boardCard(r, c));
          g.append(cell);
        }
      });
      frag.append(card(null, el("div", { class: "board" }, g, el("div", { class: "legend" }, "Rows are who owns the work; columns are where the run is. Finished runs leave the board once nobody owes them anything; the full history is under Runs.")), null));
    }

    if (!paint(frag, nav)) return;
    state.lastKey.inbox = boardKey(b);
    startPoll(refreshWhenChanged("inbox", async () => boardKey(await api("/api/board")), viewInbox), 5000);
  }
  function boardKey(b) { return JSON.stringify([b.runs.some(r => r.cards.some(c => c.kind === "agent")) ? Math.floor(Date.now() / 30000) : 0,
    b.runs.map(r => [r.run_id, r.stage, r.updated_at, r.cards.map(c => [c.kind, c.gate_id || c.question_id || c.role || c.work_item_id])])]); }
  // Every card names its run as a link, so any card on the board leads to that run's evidence and next action.
  function boardCard(r, c) {
    const run = el("a", { class: "run-link", href: `#/runs/${r.run_id}` }, r.title);
    // a stopped run's gate or question can be decided, but the decision waits for a controller: say so, and keep the orange for live calls
    const stopped = r.status === "stopped";
    const stoppedNote = stopped ? el("span", { class: "since t-bad" }, "The controller is stopped: the decision is recorded and applied once a controller drives the run again.") : null;
    if (c.kind === "agent") {
      const im = /(spec_|wi-)([A-Za-z0-9-]+)/.exec(c.title || ""), item = im ? im[2] : "";
      return el("div", { class: "k agent" }, crewFigure(c.role, "working", "s", `${CREW_NAME[c.role] || words(c.role)}, at work`),
        el("div", { class: "txt" }, el("span", { class: "t" }, CREW_NAME[c.role] || words(c.role)), el("span", { class: "s" }, run),
          el("span", { class: "since" }, item && item !== c.title ? `${item} · ` : "", c.since ? [dur(c.since), " in"] : "at work")));
    }
    if (c.kind === "gate") {
      return el("div", { class: "k call" }, el("span", { class: "t" }, GATE_NAME[c.title] || words(c.title)), el("span", { class: "s" }, run),
        c.summary ? el("span", { class: "s" }, firstSentences(c.summary, 160)) : null,
        el("span", { class: "since" }, "waiting ", timeEl(c.since, dur(c.since))), stoppedNote,
        el("a", { class: stopped ? "btn" : "btn call", href: c.title === "budget" ? `#/runs/${r.run_id}` : `#/gates/${c.gate_id}` }, c.title === "budget" ? "Decide" : "Review and decide"));
    }
    if (c.kind === "question") {
      return el("div", { class: "k call" }, el("span", { class: "t" }, "A question from the crew"), el("span", { class: "s" }, run), el("span", { class: "s" }, firstSentences(c.title, 160)),
        el("span", { class: "since" }, c.blocking ? ["the crew has been paused for ", timeEl(c.since, dur(c.since))] : ["asked ", timeEl(c.since)]), stoppedNote,
        el("a", { class: stopped ? "btn" : "btn call", href: `#/runs/${r.run_id}#q` }, "Answer"));
    }
    if (c.kind === "pr") {
      return el("div", { class: "k" }, el("span", { class: "t" }, c.title), el("span", { class: "s" }, run),
        el("span", { class: "since" }, "waiting for an engineer"),
        c.url ? el("a", { class: "btn", href: c.url, target: "_blank", rel: "noopener" }, "Open on the code host") : null);
    }
    return el("div", { class: "k quiet" }, el("span", { class: "t" }, c.title), el("span", { class: "s" }, run), el("span", { class: "since" }, "nothing to do unless it fails"));
  }

  // ------------------------------------------------------------------ runs: the history, and the home page
  const runsKey = runs => JSON.stringify([runs.some(r => r.live_sessions && !r.terminal) ? Math.floor(Date.now() / 30000) : 0,
    runs.map(r => [r.run_id, r.status, r.stage, r.updated_at, r.live_sessions, r.waiting ? [r.waiting.gates, r.waiting.questions] : null])]);
  async function viewRuns() {
    setNav("runs");
    const nav = state.nav;
    const runs = (await api("/api/runs")).runs;
    const filter = state.runsFilter || "all";
    const shown = runs.filter(r => runMatches(r, filter));
    const needs = runs.filter(r => runMatches(r, "needs_you")).length, active = runs.filter(r => !r.terminal).length;
    const running = runs.filter(r => r.status === "running").length, paused = runs.filter(r => r.status === "paused").length, finished = runs.filter(r => r.terminal).length;
    const frag = document.createDocumentFragment();
    const sub = el("p", { class: "sub" }, needs ? el("b", { class: "call" }, `${plural(needs, "run")} need${needs === 1 ? "s" : ""} you. `) : active ? "Nothing needs you. " : "",
      [running ? `${running} ${running === 1 ? "is" : "are"} being worked by the crew` : null, paused ? `${paused} ${paused === 1 ? "is" : "are"} paused` : null, finished ? `${finished} ${finished === 1 ? "has" : "have"} finished` : null].filter(Boolean).join(", ") + (runs.length ? "." : ""));
    frag.append(pageHead("Runs", runs.length ? sub : "Every run of the factory, unfinished ones first.", [el("a", { class: "btn primary", href: "#/start" }, "Start a run")]));
    if (!runs.length) {
      frag.append(card(null, el("div", { class: "empty" }, crewFigure("decomposer", "asleep", "l"), el("h3", null, "No runs yet"),
        el("div", null, "Start one from an outcome. Every run shows up here with its status and what came out of it, for as long as its event log is kept."),
        el("a", { class: "btn primary", href: "#/start" }, "Start a run"))));
    } else {
      const chips = el("div", { class: "chips", role: "group", "aria-label": "Show runs" }, RUN_FILTERS.map(([key, label]) =>
        el("button", { class: "btn sm", type: "button", "aria-pressed": String(key === filter), onclick: () => { state.runsFilter = key; viewRuns(); } },
          label, " ", el("span", { class: "n" }, String(runs.filter(r => runMatches(r, key)).length)))));
      const list = el("div", { class: "runs-list" });
      for (const r of shown) {
        const prs = (r.pull_requests || []).filter(p => p.url);
        const sentence = statusSentence(r, false);
        if (r.status === "completed" && prs.length) sentence.append(" ", el("span", { class: "prs" }, prs.map(p => `#${p.cr_id} ${p.state}`).join(", "), "."));   // no link inside the row's link; the run page has them
        list.append(el("a", { class: "run-row", href: `#/runs/${r.run_id}`, title: r.run_id }, lineStrip(r, "mini"),
          el("span", { class: "what" }, el("span", { class: "t" }, r.title, r.design_only ? el("span", { class: "muted" }, " · design run") : null), sentence),
          el("span", { class: "meta" }, el("span", null, `${money(r.cost_usd)}${r.budget_usd != null ? ` of ${money(r.budget_usd)}` : ""}`),
            el("span", null, r.terminal ? [`took ${dur(r.started_at, r.ended_at || r.updated_at)} · `, timeEl(r.ended_at || r.updated_at)] : ["started ", timeEl(r.started_at)]))));
      }
      if (!shown.length) list.append(el("div", { class: "run-row explain" }, "No run matches this filter."));
      frag.append(card(null, el("div", null, chips, list)), el("p", { class: "runs-foot" }, "Hover a row’s line for the station names and a time for the clock. Run identifiers live on the run page."));
    }
    if (!paint(frag, nav)) return;
    state.lastKey.runs = runsKey(runs);
    startPoll(refreshWhenChanged("runs", async () => runsKey((await api("/api/runs")).runs), viewRuns), 5000);
  }

  // what a finished (or stopped) run produced, read from its summary: the first card on its page
  // one grey line while building: every work item and where it is
  const itemsLine = items => `${plural(items.length, "work item")}: ${items.map(w => `${w.title || itemShort(w.work_item_id)} ${(ITEM_STAGE[w.stage] || words(w.stage)).toLowerCase()}${w.cr_id ? ` #${w.cr_id}` : ""}`).join(" · ")}`;
  function resultCard(r) {
    const s = r.summary;
    const body = el("div", { class: "result" });
    const facts = el("dl", { class: "kv" });
    const pair = (k, v) => { if (v != null && v !== "") facts.append(el("dt", null, k), el("dd", null, v)); };
    pair("Mode", s.mode === "design" ? "Design only: real agents through Gate 2, nothing built" : `Live${FORGE_NAME[s.forge] ? `, pull requests on ${FORGE_NAME[s.forge]}` : ""}`);
    pair("Spend", `${money(s.cost_usd)}${s.budget_usd != null ? ` of ${money(s.budget_usd)}` : ""} over ${plural(s.counts.sessions, "agent session")}`);
    pair("Took", s.ended_at ? `${dur(s.started_at, s.ended_at)}, ended ${ago(s.ended_at)}` : `${dur(s.started_at, s.updated_at)} so far`);
    if (s.status === "failed" && s.failure && !String(s.headline || "").includes(s.failure)) pair("Why it failed", s.failure);
    if (!r.recovery && s.escalations && s.escalations.length) pair("Why it escalated", el("ul", null, s.escalations.map(x => el("li", null, x))));
    const runFacts = el("details", { class: "more" }, el("summary", null, "Run facts and decisions"), facts);
    body.append(runFacts);
    if (s.pull_requests.length) {
      const t = el("table", null, el("tr", null, el("th", null, "Pull request"), el("th", null, "Work item"), el("th", null, "Where it got to")));
      for (const p of s.pull_requests) t.append(el("tr", null,
        el("td", null, p.url ? el("a", { href: p.url, target: "_blank", rel: "noopener" }, `#${p.cr_id}`) : (p.cr_id != null ? `#${p.cr_id}` : "not opened")),
        el("td", null, p.title || p.work_item_id, el("div", { class: "explain mono" }, p.spec_id || p.work_item_id)),
        el("td", null, el("span", { class: `tag ${p.stage === "deployed_dev" ? "ok" : p.stage === "closed" ? "bad" : ""}` }, p.state),
          p.merge_sha ? el("span", { class: "explain mono" }, ` ${p.merge_sha.slice(0, 10)}`) : null,
          p.dev_url ? [" ", el("a", { href: p.dev_url, target: "_blank", rel: "noopener" }, "open dev")] : null)));
      body.append(el("h3", null, "Pull requests"), t);
      for (const rec of r.run_records || []) body.append(runRecordBlock(r, rec));
    } else if (s.design_only && s.specs_approved.length) {
      body.append(el("h3", null, "Contracts approved"), el("ul", null, s.specs_approved.map(x => el("li", { class: "mono" }, x))));
    }
    if (s.decisions.length) {
      runFacts.append(el("h3", null, "Your decisions"), el("ul", null, s.decisions.map(d => el("li", null,
        el("b", null, `${d.label}: ${DECISION_LABEL[d.decision] || d.decision}`), personName(d.by) ? ` by ${d.by}, ` : ", ", timeEl(d.at),
        d.approved_subset.length ? ` (${d.approved_subset.join(", ")})` : "",
        d.comments ? el("div", { class: "explain" }, `“${firstSentences(d.comments, 300)}”`) : null))));
    }
    if (!s.pull_requests.length && !s.design_only) body.prepend(el("p", { class: "m-0" }, "No pull request opened."));
    return card("What came out", body, s.status_label, s.status === "completed" ? "" : "soft");
  }

  // The full evidence behind one pull request (the run record the integrate stage writes). The pull request itself
  // carries only a short reviewer-first description and links here; media are shown in "See the change".
  function runRecordBlock(r, rec) {
    const pr = rec.pull_request || {};
    const box = el("details", { class: "run-record", id: `record-${rec.spec_id}` },
      el("summary", null, `Full record${pr.id != null ? ` for #${pr.id}` : ""}: ${rec.title || rec.spec_id}`));
    const section = (title, ...kids) => box.append(el("h4", { class: "mini-h" }, title), ...kids.filter(k => k != null));
    const artifact = a => `/api/artifacts/${encodeURIComponent(r.run_id)}/${a.split("/").map(encodeURIComponent).join("/")}`;
    if (rec.what_changed) section("What changed", el("p", null, rec.what_changed));
    section("Needs a decision", (rec.decisions || []).length
      ? el("ul", null, rec.decisions.map(d => el("li", null, d.decision, d.location ? el("span", { class: "explain mono" }, ` ${d.location}`) : null)))
      : el("p", { class: "explain" }, "Nothing."));
    const sz = rec.size || {};
    if (sz.line) section("Size", el("p", null, sz.line.replace(/\*\*/g, "")), sz.flag ? el("div", { class: "notice warn m-0" }, el("b", null, "Over budget. "),
      "Reported for you to judge; it did not trigger another round.") : null,
      (sz.measured && (sz.measured.files || []).length) ? el("details", null, el("summary", null, `Lines per file (${sz.measured.files.length})`),
        el("ul", null, sz.measured.files.map(f => el("li", { class: "mono" }, `${f.path}: ${f.lines} (${f.kind})`)))) : null);
    const crit = el("table", null, el("tr", null, el("th", null, "Criterion"), el("th", null, "Given / when / then"), el("th", null, "Status"), el("th", null, "Evidence"), el("th", null, "End-to-end rows"), el("th", null, "Reviewers")));
    for (const c of rec.criteria || []) crit.append(el("tr", null, el("td", { class: "mono" }, c.ac_id),
      el("td", null, `${c.given || ""} / ${c.when || ""} / ${c.then || ""}`),
      el("td", null, el("span", { class: `tag ${c.status === "met" ? "ok" : c.status === "not_met" ? "bad" : "warn"}` }, words(c.status))),
      el("td", { class: "explain" }, c.evidence || ""),
      el("td", { class: "explain mono" }, (c.e2e || []).length ? c.e2e.map(e => `exit ${e.exit_code ?? "not run"} (${e.target})`).join("; ") : "none"),
      el("td", { class: "explain" }, c.reviewer_status ? words(c.reviewer_status) : "")));
    section("Acceptance criteria", crit);
    section("Review findings", (rec.findings || []).length ? el("ul", null, rec.findings.map(f => el("li", null,
      el("b", null, `${f.finding_id} ${f.severity}`), ` ${f.claim || ""}`,
      el("div", { class: "explain" }, [f.disposition ? `implementer: ${words(f.disposition)}` : null, f.final_status ? `final: ${words(f.final_status)}` : null,
        f.judge_assessment ? `judge: ${f.judge_assessment}` : null].filter(Boolean).join(" · "))))) : el("p", { class: "explain" }, "No findings."));
    const v = rec.verification || {};
    const env = v.environment || {};
    section("Verification", el("p", { class: "explain" }, [env.mode ? `Mode ${env.mode}` : null, env.host, env.session_id ? `session ${env.session_id}` : null,
      env.profile_path ? `${env.profile_path} ${(env.profile_sha256 || "").slice(0, 12)}` : null, rec.head_sha ? `head ${rec.head_sha.slice(0, 10)}` : null].filter(Boolean).join(" · ")),
      el("ul", null, (v.commands || []).map(c => el("li", { class: "mono" }, `${c.command} → exit ${c.exit_code ?? "not run"}${c.passed || c.failed ? ` (${c.passed} passed, ${c.failed} failed)` : ""}`)),
        (v.e2e || []).map(e => el("li", { class: "mono" }, `${e.command} → exit ${e.exit_code ?? "not run"} (${e.target}${e.visual ? `, browser ${e.visual}` : ""}; covers ${(e.covers_ac || []).join(", ")})`))),
      el("ul", null, (rec.checks || []).map(c => el("li", null, el("span", { class: `tag ${c.passed ? "ok" : "bad"}` }, c.passed ? "passed" : "failed"), ` ${c.check}`, c.detail ? el("span", { class: "explain" }, `: ${c.detail}`) : null))));
    const media = rec.media || [];
    section("Screenshots and recordings", media.length ? el("ul", null, media.map(m => el("li", null,
      `${m.description || "Browser check"}: ${words(m.status)}${m.current ? "" : " (earlier commit)"} · `,
      ...(m.media || []).flatMap((x, i) => [i ? ", " : "", el("a", { href: artifact(x.artifact), target: "_blank", rel: "noopener" }, x.caption || x.name)])))) : el("p", { class: "explain" }, "No browser checks."),
      media.length ? el("p", { class: "explain" }, "Step observations and playback are in See the change.") : null);
    if ((rec.residual_risks || []).length || rec.judge_summary) section("Residual risks and the judge's summary",
      el("ul", null, (rec.residual_risks || []).map(x => el("li", null, x))), rec.judge_summary ? el("p", { class: "explain" }, rec.judge_summary) : null);
    const prov = rec.provenance || {};
    const sess = el("table", null, el("tr", null, el("th", null, "Stage"), el("th", null, "Agent"), el("th", null, "Session"), el("th", null, "Duration"), el("th", null, "List cost")));
    for (const x of prov.sessions || []) sess.append(el("tr", null, el("td", null, x.stage), el("td", null, `${x.agent_name || x.agent_key || ""}${x.agent_version ? ` v${x.agent_version}` : ""}`),
      el("td", { class: "mono" }, x.console_url ? el("a", { href: x.console_url, target: "_blank", rel: "noopener" }, x.session_id) : x.session_id), el("td", null, x.duration || ""), el("td", null, money(x.list_cost_usd))));
    section("Sessions and cost", sess, el("p", { class: "explain" }, `Sessions above: ${money(prov.sessions_cost_usd)} over ${prov.sessions_duration || "n/a"}; whole run when integrated: ${money(prov.run_cost_usd)}.`));
    const links = rec.artifact_links || {};
    if (Object.keys(links).length) section("Contract and verdicts", el("ul", null, Object.entries(links).map(([k, u]) => el("li", null, el("a", { href: u, target: "_blank", rel: "noopener" }, words(k))))));
    return box;
  }

  function askBlock(q, onDone) {
    const ta = el("textarea", { placeholder: "Your answer", "aria-label": "Your answer" });
    const status = el("div", { class: "explain", role: "status" });
    const options = el("div", { class: "options" }, q.options.map(o => el("button", { class: "btn sm", type: "button", onclick: () => { ta.value = o; ta.focus(); } }, o)));
    const send = el("button", { class: "btn call mt-2", type: "button", onclick: async () => {
      if (!ta.value.trim()) { status.textContent = "Type or pick an answer first."; return; }
      send.disabled = true; send.textContent = "Sending…";
      try { await api(`/api/questions/${q.question_id}/answer`, { body: { answer: ta.value.trim(), by: "console" } });
        const done = el("div", { class: "done", tabindex: "-1" }, `Answered: ${ta.value.trim()}`);
        box.replaceChildren(el("div", { class: "q" }, q.question), done, el("div", { class: "explain" }, "The agent continues with your answer."));
        done.focus();
        if (onDone) onDone();
      } catch (e) { status.textContent = e.message; send.disabled = false; send.textContent = "Send answer"; }
    } }, "Send answer");
    const box = el("div", { class: "ask", id: "q" },
      el("div", { class: "q" }, q.question),
      el("div", { class: "why" }, `The ${words(q.asked_by)} is paused until you answer. ${q.why ? "Why it matters: " + q.why : ""}`),
      q.default ? el("div", { class: "why" }, `If nobody answers: ${q.default}`) : null,
      q.options.length ? options : null, ta, send, status);
    return box;
  }

  // ------------------------------------------------------------------ the crew view: one run
  async function viewRun(runId) {
    setNav("runs");
    const nav = state.nav;
    const r = await api(`/api/runs/${runId}`);
    if ((r.summary || {}).status === "stopped") for (const m of r.crew || []) if (m.state === "working") m.state = "interrupted";   // the controller is gone: nobody is at work
    const frag = document.createDocumentFragment();
    const stageName = stageLabel(r.stage);
    const b = r.budget || {};
    const started = r.created_at;
    const s = r.summary || {};
    const oc = r.outcome && r.outcome.outcome_id ? el("a", { href: `#/outcomes/new?outcome=${encodeURIComponent(r.outcome.outcome_id)}`, title: r.outcome.statement || null }, "Outcome") : null;
    const head = pageHead(r.title, null, null, [el("a", { href: "#/runs" }, "Runs"), " / ", el("span", { class: "mono" }, r.run_id), r.design_only ? " · design run" : "", oc ? " · " : null, oc]);
    head.querySelector("div").append(el("div", { class: "now" }, el("p", { class: "sentence" }, statusSentence({ ...s, cost_usd: r.cost }, true)),
      el("p", { class: "meta" }, ...metaLine({ ...s, cost_usd: r.cost, budget_usd: b.budget_usd != null ? b.budget_usd : s.budget_usd })),
      r.stage === "implementing" && (r.work_items || []).length ? el("p", { class: "meta" }, itemsLine(r.work_items)) : null));
    frag.append(head);

    frag.append(el("section", { class: "card line-card" }, lineStrip(r, "full")));
    const intervention = el("div", { class: "stack" });
    frag.append(intervention);
    if (r.recovery) frag.append(recoveryCard(r));
    if (s.terminal || s.status === "stopped") frag.append(resultCard(r));
    else if ((r.run_records || []).length) frag.append(card("What came out so far", el("div", { class: "result" }, r.run_records.map(rec => runRecordBlock(r, rec))), "pull requests opened while the run continues"));

    for (const check of r.browser_checks_pending || []) frag.append(card("Browser check in progress",
      el("div", null, el("p", null, check.description), el("p", { class: "explain" }, `${check.persona} · ${check.environment.name} · waiting for browser observations and screenshots`))));
    if ((r.visual_evidence || []).length) frag.append(visualEvidenceCard(r));

    // the roster: one row per crew member. One slot, one unit: elapsed time while a member works, cost once it is done.
    const roster = el("div", { class: "roster" });
    const lastSession = (role) => [...r.events].reverse().find(ev => ev.kind === "session.started" && (ev.payload || {}).agent_key === role);
    for (const m of r.crew || []) {
      const right = m.state === "working" && m.started_at ? [dur(m.started_at), " in"] : m.sessions ? money(m.cost_usd) : "";
      const title = m.title ? m.title.replace(`${r.run_id} `, "") : "";
      const doneLine = () => {
        const ls = lastSession(m.role);
        const when = m.finished_at ? ago(m.finished_at) : ls ? ago(ls.at) : "";
        const what = { decomposer: "Decomposed the outcome", spec_writer: "Wrote the contracts", spec_judge: "Judged the contracts", implementer: "Built", reviewer: "Reviewed", security_reviewer: "Security review done",
          merge_judge: "Judged merge readiness", integrator: "Opened the pull request" }[m.role] || "Done";
        return `${what}${m.sessions > 1 ? ` in ${plural(m.sessions, "session")}` : ""}${when ? `, ${when}` : ""}`;
      };
      const st = m.state === "working" ? (prettyTitle(title) || "At work") : m.state === "done" ? doneLine() : m.state === "blocked" ? "Stopped at its own budget cap"
        : m.state === "interrupted" ? `Cut short by the ${m.interrupted_by === "resume" ? "resume" : "failure"}${m.finished_at ? `, ${ago(m.finished_at)}` : ""}` : crewAsleepText(m.role, r);
      const figState = m.state === "interrupted" ? "still" : m.state;
      roster.append(el("div", { class: `m ${m.state}` }, crewFigure(m.role, figState, "s", `${CREW_NAME[m.role] || words(m.role)}: ${m.state === "working" ? "at work" : m.state}`),
        el("div", { class: "txt" }, el("div", { class: "nm" }, el("span", null, CREW_NAME[m.role] || words(m.role)), el("span", null, right)),
          el("span", { class: "st", title: title || null }, st))));
    }
    const consoleUrl = sid => state.config && state.config.console_session_url ? state.config.console_session_url.replace("SESSION_ID", sid) : null;
    const liveMember = (r.crew || []).find(m => m.state === "working" && m.session_id);
    const rosterCard = card("The crew", el("div", null, roster,
      liveMember && consoleUrl(liveMember.session_id) ? el("p", { class: "explain roster-foot" }, el("a", { href: consoleUrl(liveMember.session_id), target: "_blank", rel: "noopener" }, `Open the ${(CREW_NAME[liveMember.role] || "live").toLowerCase()}’s live session in the Anthropic Console`)) : null));

    // the story so far: newest first, grouped by the station the run was at, each entry a cause or its effect in plain words
    const story = el("div", { class: "story" });
    const entries = storyEntries(r).reverse();
    let lastGroup = null;
    for (const e of entries) {
      if (e.group !== lastGroup) {
        lastGroup = e.group;
        const st = STATIONS.find(x => x.key === e.group);
        story.append(el("div", { class: "grp" }, st ? st.label : words(e.group || "Run"), e.groupNote ? el("span", null, e.groupNote) : null));
      }
      story.append(el("div", { class: `e ${e.who === "po" || e.who === "eng" || e.who === "ops" ? "human" : ""}` }, timeEl(e.at),
        el("div", { class: "body" }, el("div", { class: "t" }, e.title, " ", el("span", { class: `who ${e.who === "call" ? "call" : ""}` }, `· ${e.whoLabel}`)), e.body ? el("div", { class: "d" }, e.body) : null)));
    }
    const storyCard = card("The story so far", entries.length ? story : el("p", { class: "explain" }, "Nothing has happened yet."), "newest first · hover a time for the clock");

    // your turn: what a person owes this run right now, then what comes next for people
    const onCard = g => g.kind === "budget" && b.paused && b.pause;
    const gates = r.gates.filter(g => !g.recorded && !onCard(g) && g.kind !== "build_recovery"), recorded = r.gates.filter(g => g.recorded && !onCard(g));
    const turn = el("div", { class: "turn" });
    const waiting = gates.length + r.questions.length + (b.paused ? 1 : 0);
    turn.append(el("h2", null, "Your turn"));
    if (b.paused && b.pause) turn.append(budgetCard(r));
    if (s.status === "stopped") turn.append(el("div", { class: "notice bad" }, el("b", null, "The controller stopped without recording why. "),
      "Nothing moves this run, and a decision on its open gate is not picked up, until a controller drives it again."));
    if (waiting) {
      for (const q of r.questions) turn.append(askBlock(q, () => setTimeout(() => { if (location.hash.startsWith(`#/runs/${runId}`)) viewRun(runId); }, 1500)));
      for (const g of gates) turn.append(el("div", { class: "item" },
        el("div", { class: "t" }, GATE_NAME[g.kind] || g.kind),
        el("div", { class: "d" }, firstSentences(g.summary || "", 220)),
        el("div", { class: "d muted" }, "waiting on you ", timeEl(g.opened_at, dur(g.opened_at))),
        el("a", { class: "btn call", href: g.kind === "build_recovery" ? `#/runs/${g.run_id || r.run_id}#recovery` : `#/gates/${g.gate_id}` }, "Review and decide")));
    } else if (s.status !== "stopped") {
      const openPrs = (r.work_items || []).filter(w => w.stage === "cr_open");
      const engineerLine = openPrs.length ? ` ${openPrs.map(w => w.cr_id ? `Pull request #${w.cr_id}` : w.title).join(", ")} ${openPrs.length === 1 ? "is" : "are"} waiting on an engineer.` : "";
      const parkedLine = { failed: r.resumable ? "The run stopped. It can be resumed from where it was." : "The run stopped and cannot be resumed from here.",
        escalated: r.resumable ? "The build escalated. Review the reason, then resume to recheck the saved branch and obtain a fresh merge decision." : "The run is escalated. No gate is open and it cannot be resumed from here; the way forward is a new run from the outcome.", closed: "The run is closed. Nothing more happens in it." }[r.stage];
      turn.append(el("p", { class: "lead-line" }, r.stage === "completed" ? `Nothing needs you here.${r.design_only ? " Nothing was built; start a full run from the outcome to implement the approved contracts." : engineerLine || " Every pull request is merged or closed."}` : TERMINAL[r.stage] ? (parkedLine || `${stageName}.`) : `Nothing needs you right now.${engineerLine}`));
    }
    for (const g of recorded) turn.append(el("div", { class: "notice ok" }, el("b", null, `${DECISION_LABEL[g.recorded.decision] || g.recorded.decision} recorded on ${GATE_SHORT[g.kind]}. `), s.status === "stopped" ? "It is applied once a controller drives the run again. " : "The run picks it up within seconds. ", el("a", { href: g.kind === "build_recovery" ? `#/runs/${g.run_id || r.run_id}#recovery` : `#/gates/${g.gate_id}` }, "View")));
    if (!TERMINAL[r.stage]) {
      const next = [];
      if (r.stage === "decomposing" || r.stage === "intake") next.push("the product owner approves the plan at Gate 1, once the decomposer is done");
      else if (r.stage === "specifying") next.push("the product owner approves the contracts at Gate 2, once the judge passes them");
      else if (r.stage === "implementing" && !r.design_only) next.push("an engineer reviews each pull request on the code host as it opens");
      else if (r.stage === "completed" && !r.design_only) next.push("an engineer reviews and merges the pull requests on the code host");
      if (r.watch && r.watch.pending && r.watch.pending.length) next.push("the factory watches each merge and the dev deploy that follows");
      if (next.length && !(r.stage === "completed" && !(r.work_items || []).some(w => w.stage === "cr_open"))) turn.append(el("p", { class: "next" }, `Next for a person: ${next.join("; ")}.`));
    }
    if (r.resumable && r.resuming) turn.append(el("div", { class: "notice info" }, el("b", null, "Resuming. "), "The controller is re-checking the branch; this page follows it."));
    else if (r.resumable && !b.paused && !r.recovery) turn.append(resumeBox(r, runId));
    if (r.watch && (r.watch.pending.length || r.watch.heartbeat)) turn.append(watchBox(r, runId));
    const turnCard = card(null, turn, null, waiting ? "call" : "");

    // how this run is tuned (read-only here; the next run is tuned on the Start screen)
    const p = { ...(r.policy || {}), ...(b.budget_usd != null ? { run_budget_usd: b.budget_usd } : {}) };
    const tune = el("div", { class: "tune" });
    const caps = p.stage_budget_usd || {};
    const security = securityReview(p, r);
    const summary = [p.run_budget_usd ? `${money(p.run_budget_usd)} cap` : null, p.preview_mode === "local" ? "local previews" : p.preview_mode ? `${p.preview_mode} previews` : null,
      p.reviewer_passes ? `${p.reviewer_passes} reviewers${security.short}` : null, p.review_max_rounds ? `${p.review_max_rounds} review rounds` : null].filter(Boolean).join(" · ");
    const details = el("div", { class: "tier" },
      el("dl", null,
        tunePair("Run budget", p.run_budget_usd ? `$${p.run_budget_usd}` : "from the outcome"), tunePair("Preview", p.preview_mode || "platform"),
        tunePair("Reviewer passes", p.reviewer_passes ?? "default"), tunePair("Review rounds", p.review_max_rounds ?? "default"),
        tunePair("Security reviewer", security.long),
        tunePair("Grader iterations", p.outcome_max_iterations ?? "default"), tunePair("Cap per implementer", caps.implement ? `$${caps.implement}` : "default"),
        tunePair("Cap per judge", caps.judge ? `$${caps.judge}` : "default"), tunePair("Cap per review", caps.review ? `$${caps.review}` : "default")),
      el("p", { class: "explain tune-foot" }, "Settings are fixed for a run once it starts. ", el("a", { href: "#/start" }, "Tune the next run on the Start screen.")));
    tune.append(el("div", { class: "row" }, el("h2", null, "How this run is tuned")), el("p", { class: "explain" }, summary || "Defaults."),
      el("details", { class: "more" }, el("summary", null, "All settings"), details));
    const tuneCard = card(null, tune);

    if (!r.recovery) {
      if (waiting || r.resumable || s.status === "stopped") intervention.append(turnCard);
      else frag.append(turnCard);
    }
    frag.append(el("details", { class: "card run-disclosure" }, el("summary", null, "Activity and crew", el("span", { class: "explain" }, `${plural((r.events || []).length, "recorded event")}`)),
      el("div", { class: "floor" }, rosterCard, storyCard, el("aside", { class: "stack" }, tuneCard))));

    // the details a reviewer or operator sometimes needs, folded away
    const items = el("table", null, el("tr", null, el("th", null, "Work item"), el("th", null, "Stage"), el("th", null, "Depends on"), el("th", null, "Pull request")));
    for (const w of r.work_items) items.append(el("tr", null,
      el("td", null, el("div", null, w.title || w.work_item_id), el("div", { class: "explain mono" }, w.work_item_id)),
      el("td", null, el("span", { class: "tag" }, ITEM_STAGE[w.stage] || words(w.stage))),
      el("td", { class: "explain" }, (w.depends_on || []).join(", ") || "none"),
      el("td", null, w.cr_url ? el("a", { href: w.cr_url, target: "_blank", rel: "noopener" }, `#${w.cr_id}`) : (w.branch ? el("span", { class: "explain mono" }, w.branch) : el("span", { class: "explain" }, "not yet")),
        w.stage === "deployed_dev" && w.dev_environment_url ? [" · ", el("a", { href: w.dev_environment_url, target: "_blank", rel: "noopener" }, "dev")] : null,
        w.merge_sha ? [" ", el("span", { class: "explain mono" }, `merged as ${w.merge_sha.slice(0, 10)}`)] : null)));
    const tl = el("ol", { class: "timeline" });
    for (const ev of [...r.events].reverse().slice(0, 120)) {
      const [who, what] = describe(ev);
      const link = what.session_id && consoleUrl(what.session_id) ? el("a", { href: consoleUrl(what.session_id), target: "_blank", rel: "noopener" }, " open in the Anthropic Console") : null;
      tl.append(el("li", null, timeEl(ev.at, ev.at ? new Date(ev.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : ""),
        el("span", null, el("span", { class: who === "human" ? "human" : "" }, what.main), what.detail ? el("span", { class: "detail" }, " ", what.detail) : null, link)));
    }
    const files = el("table", null, el("tr", null, el("th", null, "Stage"), el("th", null, "File"), el("th", null, "Fingerprint")));
    for (const a of r.artifacts) files.append(el("tr", null, el("td", { class: "explain" }, a.stage),
      el("td", null, el("a", { href: `/api/artifacts/${r.run_id}/${a.stage}/${a.name}`, target: "_blank", rel: "noopener" }, a.name)),
      el("td", { class: "explain mono" }, a.sha256 ? a.sha256.slice(0, 12) : "")));
    const more = el("div", { class: "stack mt-4" },
      el("details", { class: "card run-disclosure" }, el("summary", null, `Work items (${r.work_items.length})`), el("div", { class: "table-scroll" }, items)),
      el("details", { class: "card run-disclosure" }, el("summary", null, `Event log (${r.events.length})`), tl),
      el("details", { class: "card run-disclosure" }, el("summary", null, `Files (${r.artifacts.length})`), el("div", { class: "table-scroll" }, files)));
    frag.append(more);

    if (!paint(frag, nav)) return;
    if (location.hash.endsWith("#q")) { const q = document.getElementById("q"); if (q) q.scrollIntoView(); }
    const recordAt = location.hash.match(/#record-([^#?]+)$/);
    if (recordAt) { const rec = document.getElementById(`record-${decodeURIComponent(recordAt[1])}`); if (rec) { rec.open = true; setTimeout(() => rec.scrollIntoView(), 0); } }
    state.lastKey.run = runKey(r);
    // A parked run stops polling; one paused at its budget keeps polling so the page follows the resume it started.
    if (!(TERMINAL[r.stage] || r.stage === "completed") || r.stage === "paused_budget" || r.resuming) startPoll(refreshWhenChanged("run", async () => runKey(await api(`/api/runs/${runId}`)), () => viewRun(runId)), 4000);
    else stopPoll();
  }
  function recoveryCard(r) {
    if (r.resuming) return card("Recovery started", el("p", { role: "status" }, "Your decision is recorded. The controller is starting; this page will follow its progress."));
    const rec = r.recovery, failed = (rec.checks || []).filter(x => !x.passed);
    const details = el("details", { class: "more" }, el("summary", null, "Full explanation and evidence"),
      el("p", null, rec.summary), el("ul", null, (rec.risks || []).map(x => el("li", null, x))),
      rec.artifact ? el("a", { href: `/api/artifacts/${r.run_id}/${rec.artifact}`, target: "_blank", rel: "noopener" }, "Open full report") : null);
    const labels = { hosted_environment: "Environment baseline", test_plan_commands: "Build and code checks" };
    const notes = el("textarea", { "aria-label": "Recovery instructions", placeholder: "What has been repaired, or what should the implementer change?" });
    const status = el("p", { role: "status", class: "explain" });
    const actions = el("div", { class: "actions" });
    const choose = (decision, label, cls) => el("button", { type: "button", class: `btn ${cls || ""}`, onclick: async () => {
      if (decision === "request_changes" && !notes.value.trim()) { status.textContent = "Add instructions for the correction first."; notes.focus(); return; }
      actions.querySelectorAll("button").forEach(b => b.disabled = true);
      status.textContent = decision === "reject" ? "Closing the run…" : "Recording your decision and starting recovery…";
      try { await api(`/api/runs/${r.run_id}/recovery`, { body: { revision: rec.revision, decision, comments: notes.value.trim() } }); await viewRun(r.run_id); }
      catch (e) { status.textContent = e.message; actions.querySelectorAll("button").forEach(b => b.disabled = false); }
    } }, label);
    actions.append(choose("approve", "Recheck saved work", "call"), choose("request_changes", "Request changes"), choose("reject", "Close run", "danger"));
    const options = el("details", { class: "more recovery-options" }, el("summary", null, "Choose how to continue"),
      el("p", { class: "explain" }, "Recheck after fixing the environment. Request changes sends your instructions through one more implementation and review cycle, within the approved contract. Closing ends this run. None of these actions bypasses a failing check."), notes, actions, status);
    return card("Your turn", el("div", { class: "recovery", id: "recovery" },
      el("p", { class: "lead-line" }, failed.length ? `${plural(failed.length, "required check")} still failing. The saved work needs a recovery decision.` : "The build needs your decision before it can continue."),
      failed.length ? el("ul", { class: "recovery-blockers" }, failed.map(x => el("li", null, el("b", null, labels[x.check] || words(x.check)), el("span", { class: "explain" }, x.detail)))) : null,
      el("p", { class: "explain" }, `${(rec.criteria || []).length ? `${rec.criteria.filter(c => c.status === "met").length} of ${rec.criteria.length} acceptance criteria met · ` : "Awaiting fresh verification · "}candidate ${(rec.head_sha || "").slice(0, 10)}`), details, options), "Build paused for a decision", "call");
  }
  function visualEvidenceCard(r) {
    // A recheck can produce several attempts for the same candidate. The API is newest-first:
    // only the latest result for each journey belongs in the glance, including a failed retry.
    const current = [], earlier = [], seen = new Set();
    for (const evidence of r.visual_evidence) {
      const key = JSON.stringify([evidence.spec_id, evidence.description, evidence.persona, evidence.viewport && evidence.viewport.width, evidence.viewport && evidence.viewport.height]);
      if (evidence.current && !seen.has(key)) { current.push(evidence); seen.add(key); }
      else earlier.push(evidence);
    }
    const body = el("div", { class: "evidence-list" });
    const url = artifact => `/api/artifacts/${encodeURIComponent(r.run_id)}/${artifact.split("/").map(encodeURIComponent).join("/")}`;
    const entry = evidence => {
      const media = (evidence.media || []).filter(m => m.artifact), steps = evidence.steps_observed || [];
      const name = (media.find(m => m.role === "demo") || {}).caption || evidence.persona || "Browser check";
      const title = name.replace(/^model-picker-/, "").replace(/-/g, " ");
      const box = el("details", { class: "visual-check", "data-evidence-id": evidence.id || evidence.head_sha + title },
        el("summary", { class: "visual-heading" }, el("span", { class: "visual-title" }, title),
          el("span", { class: `tag ${evidence.current && evidence.status === "passed" ? "ok" : "warn"}` }, words(evidence.status)),
          el("span", { class: "explain" }, `${evidence.viewport ? `${evidence.viewport.width} × ${evidence.viewport.height}` : ""} · ${plural(steps.length, "step")}`)));
      const content = el("div", { class: "visual-content" });
      if (evidence.status !== "passed") content.append(...observedFailure(evidence));
      const demo = media.find(m => m.role === "demo"), stills = media.filter(m => m.role === "screenshot");
      if (demo) {
        const playback = el("details", null, el("summary", null, "Watch demonstration"));
        // Attach media only on first expansion. Hidden historical runs never load their recordings.
        playback.addEventListener("toggle", () => { if (!playback.open || playback.dataset.loaded) return; playback.dataset.loaded = "true";
          playback.append(demo.mime_type.startsWith("video/") ? el("video", { controls: true, preload: "metadata", playsinline: true, class: "visual-demo", src: url(demo.artifact) })
            : el("img", { class: "visual-demo", src: url(demo.artifact), alt: demo.caption || "Browser demonstration" })); });
        content.append(playback);
      }
      const observed = el("details", null, el("summary", null, `Steps and screenshots (${steps.length})`));
      observed.append(el("ol", { class: "evidence-steps" }, steps.map((step, i) => el("li", null,
        el("details", null, el("summary", null, step.step.split(":")[0], " · ", words(step.status)), el("p", null, step.observation),
          stills[i] ? el("a", { href: url(stills[i].artifact), target: "_blank", rel: "noopener" }, "Open screenshot") : null)))));
      content.append(observed, el("details", null, el("summary", null, "Test contract and environment"), el("p", null, evidence.description || ""),
        el("p", { class: "explain" }, `${evidence.persona || "Browser user"} · candidate ${(evidence.head_sha || "").slice(0, 10)}`),
        evidence.environment ? el("p", { class: "explain" }, `${evidence.environment.name} · ${words(evidence.environment.data)} · ${words(evidence.environment.authentication)}`) : null));
      box.append(content); return box;
    };
    current.forEach(e => body.append(entry(e)));
    if (!current.length) body.append(el("p", { class: "explain" }, "No browser evidence for the current candidate yet."));
    if (earlier.length) body.append(el("details", { class: "earlier-evidence" }, el("summary", null, `Earlier attempts (${earlier.length})`), el("p", { class: "explain" }, "Superseded attempts. Use the latest checks above for the current result."), earlier.map(entry)));
    return card("Browser testing", body, `${current.filter(e => e.status === "passed").length} of ${current.length} latest checks passed`, "visual-evidence-card");
  }
  // A check that did not pass leads with what was observed, and says which part stopped it. The worker's summary is its
  // reading of those observations, so it follows them, labelled; the controller's own reason (a refused or missing report,
  // a check that never ran) is shown as the controller's, since then there are no observations of the app to show.
  function observedFailure(evidence) {
    const steps = evidence.steps_observed || [];
    const stopped = steps.filter(s => s.status !== "passed");
    const byController = evidence.summary_source === "controller";
    const who = byController && !steps.length ? ["Stopped by Factory, not the app. ", "The check did not run or its report was not accepted, so nothing here shows how the app behaved."]
      : byController ? ["Factory did not accept the report. ", "The browser's observations are below, but the report failed Factory's checks, so this check cannot pass."]
      : evidence.status === "failed" ? ["Stopped by the app. ", "A step's observation did not meet what the step required."]
      : ["Stopped by the browser check. ", "The worker could not finish every step; read the observations before changing the app."];
    const out = [el("div", { class: "notice bad m-0" }, el("b", null, who[0]), who[1])];
    if (byController) out.push(el("p", null, el("b", null, "Factory's reason: "), evidence.summary));
    if (stopped.length) {
      out.push(el("div", null, el("h4", { class: "mini-h" }, "What the browser saw"), el("ol", { class: "observed" }, stopped.map(s =>
        el("li", { value: String(steps.indexOf(s) + 1) }, el("span", { class: `tag ${s.status === "failed" ? "bad" : "warn"}` }, words(s.status)), " ",
          el("b", null, s.step), el("div", null, s.observation))))));
    }
    if (!byController) out.push(el("p", { class: "explain" }, el("b", null, "Worker's summary: "), evidence.summary,
      el("span", null, " This is the worker's interpretation; the observations above are the evidence.")));
    return out;
  }
  function tunePair(k, v) { return el("div", null, el("dt", null, k), el("dd", null, String(v))); }
  // Whether this run's security pass can happen: the policy asks for it, and either a security session already ran or
  // the optional security reviewer agent is deployed. Never "plus security" for a pass that is skipped.
  function securityReview(p, r) {
    const ran = ((r.crew || []).find(m => m.role === "security_reviewer") || {}).sessions > 0;
    if (p.security_review === false) return { on: false, short: "", long: "off" };
    const deployed = typeof p.security_review_available === "boolean" ? p.security_review_available   // recorded when the run started
      : !state.config || state.config.security_review_available;
    if (ran || deployed) return { on: true, short: " plus security", long: "on" };
    return { on: false, short: ", no security pass", long: "not deployed: no security pass runs" };
  }
  function crewAsleepText(role, r) {
    const past = stationIndex(r.stage);
    const needs = { decomposer: 1, spec_writer: 3, spec_judge: 3, implementer: 5, reviewer: 5, security_reviewer: 5, merge_judge: 5, integrator: 5 }[role] || 0;
    if (role === "security_reviewer" && !securityReview({ ...(r.policy || {}) }, r).on) return (r.policy || {}).security_review === false ? "Off for this run" : "Not deployed: no security pass";
    if (r.stage === "paused_budget") { const nx = ((r.summary || {}).pause || {}).next || {}; return NEXT_ROLE[nx.stage] === role ? "Next, once the budget is raised" : "Asleep while the run is paused"; }
    if (TERMINAL[r.stage] || r.stage === "completed") return "Not used in this run";
    if (r.design_only && needs >= 5) return "Not in a design run";
    const waits = { decomposer: "Waits for the outcome", spec_writer: "Waits for the plan", spec_judge: "Waits for the contracts", implementer: "Waits for the approved contract",
      reviewer: "Waits for a branch", security_reviewer: "Waits for a branch", merge_judge: "Waits for the reviews", integrator: "Waits for the verdict" };
    return past < needs ? waits[role] : (waits[role] || "Waiting");
  }
  function runKey(r) {
    const hb = r.watch && r.watch.heartbeat;
    return JSON.stringify([r.updated_at, r.stage, r.status, r.gates.map(g => [g.gate_id, !!g.recorded]), r.questions.map(q => q.question_id),
      hb ? [hb.alive, hb.last_poll_at] : null, (r.browser_checks_pending || []).map(c => c.id), (r.crew || []).map(c => c.state), r.resuming || null,
      (r.crew || []).some(c => c.state === "working") ? Math.floor(Date.now() / 30000) : 0]);   // elapsed figures move while a session is open
  }
  // The story: one entry per event a person would want to read, in plain words, each naming its cause or its effect.
  // Every entry carries the station the run was at (`group`), so the page can read the story station by station.
  const EVENT_STATION = { intake: "intake", decompose: "decomposing", gate_1: "gate_1_decomposition", gate_1_decomposition: "gate_1_decomposition",
    specify: "specifying", judge: "specifying", gate_2: "gate_2_spec_readiness", gate_2_spec_readiness: "gate_2_spec_readiness",
    implement: "implementing", review: "implementing", merge_judge: "implementing", integrate: "implementing", deploy: "completed" };
  function storyEntries(r) {
    const out = [];
    const title = (r.outcome && r.outcome.title) || r.title;
    const who = p => p.decided_by || p.answered_by || null;
    const byName = personName;
    const openGateIds = new Set((r.gates || []).filter(g => !g.recorded).map(g => g.gate_id));
    const titled = id => { const w = (r.work_items || []).find(x => itemShort(x.work_item_id) === itemShort(id) || x.spec_id === id); return w && w.title ? w.title : itemShort(id); };
    const itemName = ev => { const id = ev.spec_id || ev.work_item_id; return id ? titled(id) : ""; };
    let station = "intake", lastDecision = null;
    const visited = new Set();
    for (const ev of r.events) {
      const p = ev.payload || {};
      if (ev.kind === "gate.decided") lastDecision = p.decision;
      if (ev.kind === "stage.entered" && stationIndex(p.stage) >= 0) {
        if (visited.has(p.stage) && p.stage !== station) out.push({ at: ev.at, who: "", whoLabel: "Factory", title: `Back to ${stageLabel(p.stage)}`,
          body: lastDecision === "request_changes" ? "After your request for changes, the crew revises." : "", group: p.stage });
        visited.add(p.stage); station = p.stage;
      }
      else if (ev.kind === "run.completed") station = "completed";
      else if (EVENT_STATION[ev.stage]) station = EVENT_STATION[ev.stage];
      const item = itemName(ev);
      const E = (whoKey, whoLabel, t, body) => out.push({ at: ev.at, who: whoKey, whoLabel, title: t, body, group: station });
      switch (ev.kind) {
        case "run.created": E("po", byName((r.outcome || {}).owner && r.outcome.owner.name) || "Product owner", "Outcome written", title); break;
        case "repository.created": E("po", "Product owner", "Repository created", `${p.url}${p.workflow_seeded === false ? ". The preview workflow could not be seeded (token scope); local previews do not need it." : ""}`); break;
        case "session.started": { const role = p.agent_key; if (!role || role === "codebase_researcher") break;
          const t = String(p.title || "");
          const tt = t.replace(/\b(spec_|wi-)[A-Za-z0-9-]+/g, "");   // the item id can contain any word
          const what = /respond|answer/i.test(tt) ? "Answering the review" : /round (\d+)/i.test(tt) && role === "reviewer" ? `Review round ${tt.match(/round (\d+)/i)[1]} started` :
            { decomposer: "Decomposing started", spec_writer: "Writing a contract", spec_judge: "Judging the contracts", implementer: "Building started",
              reviewer: "Review started", security_reviewer: "Security review started", merge_judge: "Judging merge readiness", integrator: "Opening the pull request" }[role] || words(role);
          E("crew", CREW_NAME[role] || words(role), what, item || (t ? t.replace(`${r.run_id} `, "") : "")); break; }
        case "gate.opened": E(openGateIds.has(p.gate_id) ? "call" : "po", openGateIds.has(p.gate_id) ? "waiting on you" : "for the product owner", `${GATE_SHORT[p.kind] || p.kind} opened`, firstSentences(p.summary || "", 240)); break;
        case "gate.decided": {
          const d = DECISION_LABEL[p.decision] || p.decision;
          const effect = { gate_1_decomposition: { approve: "The crew starts writing the contracts.", request_changes: "The decomposer revises the plan.", reject: "The run closes." },
            gate_2_spec_readiness: { approve: "Build starts.", approve_subset: "Build starts on the selected contracts.", request_changes: "The writers revise.", reject: "The run closes." },
            budget: { approve: "The run continues once resumed.", reject: "The run closes." } }[p.kind] || {};
          E("po", byName(who(p) || ev.actor_id) || "Product owner", `${GATE_SHORT[p.kind] || p.kind}: ${d.toLowerCase()}${p.approved_subset && p.approved_subset.length ? ` (${p.approved_subset.map(itemShort).join(", ")})` : ""}`,
            `${p.comments ? `“${firstSentences(p.comments, 240)}” ` : ""}${effect[p.decision] || ""}`.trim() || "No notes."); break; }
        case "question.asked": { const open = (r.questions || []).some(q => q.question_id === p.question_id); E(open ? "call" : "po", open ? "waiting on you" : "for the product owner", `The ${words(p.asked_by)} asked a question`, p.question); break; }
        case "question.answered": E("po", byName(p.answered_by) || "Product owner", "Question answered", `${p.answer}. The ${words(p.asked_by || "crew")} continues.`); break;
        case "judge.verdict": E("crew", "Spec judge", p.result === "pass" ? `Contracts passed on round ${p.round}` : `Contracts need revision on round ${p.round}`, p.blocking ? `${plural(p.blocking, "blocking issue")}.` : p.result === "pass" ? "Gate 2 opens next." : ""); break;
        case "review.findings": { const n = findingCount(p);
          E("crew", "Reviewer", p.skipped ? `Review round ${p.round} skipped` : `Review round ${p.round}: ${plural(n, "finding")}${p.blocking ? `, ${p.blocking} blocking` : ""}`,
            p.skipped ? p.skipped : [item, p.summary ? `“${firstSentences(p.summary, 200)}”` : n ? (p.blocking ? "The implementer answers before the merge judge looks." : "None blocking.") : "Nothing to fix."].filter(Boolean).join(". ")); break; }
        case "review.response": E("crew", "Implementer", `Review round ${p.round} answered`, [item, dispositionText(p)].filter(Boolean).join(": ")); break;
        case "merge.verdict": E("crew", "Merge judge", `Merge judge: ${words(p.result)}`, [item, p.result === "ready" ? "The integrator opens the pull request." : p.result ? "Not ready." : "", p.summary ? firstSentences(p.summary, 200) : ""].filter(Boolean).join(". ")); break;
        case "cr.opened": E("crew", "Integrator", `Pull request${p.id ? ` #${p.id}` : ""} opened`, [item, p.url, "Now waiting on an engineer."].filter(Boolean).join(" · ")); break;
        case "cr.merged": E("eng", "Engineer", `Merged${p.id ? ` #${p.id}` : ""}`, [item, p.merge_commit_sha ? `as ${String(p.merge_commit_sha).slice(0, 10)}` : "", "The dev deploy is watched next."].filter(Boolean).join(" · ")); break;
        case "cr.closed": E("eng", "Engineer", `Pull request${p.id ? ` #${p.id}` : ""} closed without merging`, item ? `${item} is closed.` : ""); break;
        case "deploy.started": E("ops", "Ops", "Dev deploy started", [item, p.pipeline_url || ""].filter(Boolean).join(" · ")); break;
        case "deploy.succeeded": E("ops", "Ops", "Dev deploy succeeded", [item, p.environment_url || "", "The end-to-end checks run next."].filter(Boolean).join(" · ")); break;
        case "deploy.failed": E("ops", "Ops", "Dev deploy failed", [item, p.status ? words(p.status) : "", "A deploy-failure gate opens."].filter(Boolean).join(" · ")); break;
        case "e2e.dev_passed": E("ops", "Ops", "Dev checks passed", [item, p.passed != null ? `${p.passed} of ${p.total} passed` : "", item ? `${item} is verified in dev.` : ""].filter(Boolean).join(" · ")); break;
        case "e2e.dev_failed": E("ops", "Ops", "Dev checks failed", [item, "A deploy-failure gate opens."].filter(Boolean).join(" · ")); break;
        case "run.escalated": E("", "Factory", "Escalated", `${p.reason || ""} Nothing runs until a person decides.`.trim()); break;
        case "run.failed": E("", "Factory", "The controller stopped", `${p.reason || "No reason was recorded."}${(p.interrupted_sessions || []).length ? ` ${plural(p.interrupted_sessions.length, "open session")} ${p.interrupted_sessions.length === 1 ? "was" : "were"} cut short.` : ""}`); break;
        case "run.resumed": E("po", "Product owner", `Run resumed${p.from === "paused_budget" ? " after the budget pause" : ""}`, Object.entries(p.points || {}).map(([s, x]) => `${itemShort(s)}: round ${x.round}${x.implementer_done ? ", implementer done" : ""}`).join("; ") || (p.stage ? `Back into ${stageLabel(p.stage)}.` : "")); break;
        case "budget.reached": E("", "Factory", p.next ? "Paused at the budget" : "A session hit its cap", p.next ? `${money(p.spent_usd)} of ${money(p.budget_usd)} spent; ${p.next.label || p.next.stage || "the next step"} could cost ${money(p.next.cap_usd)}. Nothing runs until a person decides.` : `${p.title || p.agent_key || "A session"} reached its ${p.budget_usd != null ? money(p.budget_usd) : ""} cap; waiting on a person.`); break;
        case "budget.raised": E("po", byName(p.decided_by) || "Product owner", `Budget raised to ${money(p.budget_usd)}`, p.previous_budget_usd != null ? `Was ${money(p.previous_budget_usd)}. The run continues.` : "The run continues."); break;
        case "run.completed": E("eng", p.design_only ? "Product owner" : "Engineer", p.design_only ? "Design run done" : "Done: the pull requests are yours", p.design_only ? "Nothing was built; start a full run from the outcome to implement the approved contracts." : "Review and merge happen on the code host."); break;
        default: break;
      }
    }
    // a line under each station heading: how long it took and what it cost, from the sessions finished while the run was there
    const byGroup = {};
    for (const ev of r.events) {
      const p = ev.payload || {};
      if (ev.kind === "session.finished" && EVENT_STATION[ev.stage]) { const g = byGroup[EVENT_STATION[ev.stage]] ||= { n: 0, cost: 0 }; g.n += 1; g.cost += Number(p.list_cost_usd || 0); }
    }
    for (const e of out) { const g = byGroup[e.group]; if (g && g.n) e.groupNote = `${plural(g.n, "session")} · ${money(g.cost)}`; }
    return out;
  }

  function resumeBox(r, runId) {
    const box = el("div", { class: "stack" },
      el("p", { class: "explain m-0" }, "Resume rechecks the branch, runs fresh verification, and continues from saved work. A changed or escalated merge decision is judged again."),
      el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", onclick: async (ev) => {
        const b = ev.currentTarget; b.disabled = true; b.textContent = "Resuming…";
        try {
          const res = await api(`/api/runs/${runId}/resume`, { body: {} });
          box.append(el("div", { class: "notice ok" }, el("b", null, "Resumed. "), `The run continues in the background (log ${res.log}). This page follows it.`));
          setTimeout(() => { if (location.hash.startsWith(`#/runs/${runId}`)) viewRun(runId); }, 2500);
        } catch (e) { b.disabled = false; b.textContent = "Resume run"; box.append(el("div", { class: "notice bad" }, String(e.message || e))); }
      } }, "Resume run")));
    return box;
  }
  function watchBox(r, runId) {
    const w = r.watch, hb = w.heartbeat;
    const box = el("div", { class: "stack" });
    const names = Object.fromEntries(r.work_items.map(x => [x.work_item_id, x]));
    const stageText = { cr_open: "pull request open, waiting for a human to review and merge", merged: "merged; the dev deploy will be observed",
      deploying_dev: "merged; watching the dev pipeline and the end-to-end checks" };
    if (w.pending.length) {
      const list = el("ul", { class: "list stacked" });
      for (const wid of w.pending) {
        const x = names[wid] || { work_item_id: wid };
        list.append(el("li", null, el("span", { class: "tag" }, ITEM_STAGE[x.stage] || words(x.stage || "")),
          el("div", { class: "main" }, el("div", { class: "title" }, x.title || wid), el("div", { class: "desc" }, stageText[x.stage] || "")),
          x.cr_url ? el("a", { class: "btn sm", href: x.cr_url, target: "_blank", rel: "noopener" }, `Open #${x.cr_id}`) : null));
      }
      box.append(list);
    }
    if (hb && hb.alive) {
      box.append(el("div", { class: "notice ok m-0" }, el("b", null, "Watching. "), `Last checked ${ago(hb.last_poll_at)}.`,
        hb.last_tick && hb.last_tick.errors && hb.last_tick.errors.length ? ` Last error: ${hb.last_tick.errors[0].error}` : ""));
    } else if (hb && !w.pending.length) {
      box.append(el("div", { class: "notice info m-0" }, el("b", null, "Watcher finished. "), `Last checked ${ago(hb.last_poll_at)}; nothing is left to watch.`));
    } else {
      if (hb) box.append(el("div", { class: "notice info m-0" }, el("b", null, "Not watching. "), `The last watcher checked ${ago(hb.last_poll_at)} and is no longer running. Starting it again is safe.`));
      if (w.watchable) box.append(el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", onclick: async (ev) => {
        const b = ev.currentTarget; b.disabled = true; b.textContent = "Starting…";
        try {
          const res = await api(`/api/runs/${runId}/watch`, { body: {} });
          box.append(el("div", { class: "notice ok" }, el("b", null, "Watching. "), `The watcher runs in the background (log ${res.log}); this page follows it.`));
          setTimeout(() => { if (location.hash.startsWith(`#/runs/${runId}`)) viewRun(runId); }, 2500);
        } catch (e) { b.disabled = false; b.textContent = "Watch for merges"; box.append(el("div", { class: "notice bad" }, String(e.message || e))); }
      } }, "Watch for merges")));
    }
    return box;
  }

  // The run is parked at its budget: the numbers, and the decision (raise and continue, or close) in one place.
  // Both record the decision on the budget gate and then start `factory resume`, which applies it.
  function budgetCard(r) {
    const b = r.budget, p = b.pause, next = p.next || {};
    const needed = Number(p.needed_usd || 0);
    const recorded = b.decision;
    const box = el("div", { class: "stack" });
    box.append(glance([[money(p.spent_usd), `spent across ${plural(p.sessions_finished || b.sessions || 0, "session")}`],
      [money(p.budget_usd), "the run's budget"], [money(next.cap_usd), `next: ${next.label || next.stage} (${plural(next.sessions || 1, "session")}), at most`],
      [money(needed), "needed to continue"]]));
    box.append(el("p", { class: "explain" }, "Every agent session carries its own hard cap, so the check is spent plus the next step's caps: the run can never go past the budget, and a step is refused before it starts rather than cut off midway. The caps are conservative; a step usually costs less than its cap."));
    const status = el("div");
    const err = el("div", { class: "err", role: "alert" });
    async function go(body, btn, label) {
      btn.disabled = true; btn.textContent = label; err.textContent = "";
      try {
        const res = await api(`/api/runs/${r.run_id}/resume`, { body });
        status.replaceChildren(el("div", { class: "notice ok" }, el("b", null, body.budget_usd != null ? `Budget raised to ${money(body.budget_usd)}. ` : "Decision applied. "),
          `The controller continues in the background (log ${res.log}). This page follows it.`));
        setTimeout(() => { if (location.hash.startsWith(`#/runs/${r.run_id}`)) viewRun(r.run_id); }, 2500);
      } catch (e) { btn.disabled = false; btn.textContent = btn.dataset.label; err.textContent = String(e.message || e); }
    }
    if (recorded) {
      const raise = recorded.decision === "approve";
      const amount = raise ? (String(recorded.comments || "").match(/(\d+(?:\.\d+)?)/) || [])[1] : null;
      const apply = el("button", { class: "btn primary", type: "button", dataset: { label: raise ? "Continue the run" : "Close the run" } }, raise ? "Continue the run" : "Close the run");
      apply.onclick = () => go({}, apply, raise ? "Continuing…" : "Closing…");
      box.append(el("div", { class: "notice info" }, el("b", null, raise ? `Raise to ${amount ? money(amount) : "a new budget"} recorded${recorded.decided_by ? " by " + recorded.decided_by : ""}. ` : `Close recorded${recorded.decided_by ? " by " + recorded.decided_by : ""}. `),
        "The controller is not running while the run is paused; apply the decision to continue."),
        el("div", { class: "actions" }, apply), status, err);
    } else {
      const input = el("input", { type: "number", min: String(Math.ceil(needed)), step: "1", value: String(Math.ceil(needed)), "aria-label": "New budget in USD", class: "w-narrow" });
      const raise = el("button", { class: "btn primary", type: "button", dataset: { label: "Raise the budget and continue" } }, "Raise the budget and continue");
      const close = el("button", { class: "btn danger", type: "button", dataset: { label: "Close the run" } }, "Close the run");
      raise.onclick = () => {
        const v = Number(input.value);
        if (!(v >= needed)) { err.textContent = `The next step needs at least ${money(needed)}.`; return; }
        go({ budget_usd: v }, raise, "Raising…");
      };
      const actions = el("div", { class: "actions" },
        el("label", { class: "help" }, "New budget in USD"), input, raise, close);
      // Closing is two steps, like every gate decision: the confirm appears after a beat where no first button was.
      close.onclick = () => {
        const yes = el("button", { class: "btn danger", type: "button", disabled: true, dataset: { label: "Yes, close the run" } }, "Yes, close the run");
        const cancel = el("button", { class: "btn", type: "button", onclick: () => confirmRow.replaceWith(actions) }, "Cancel");
        const confirmRow = el("div", { class: "confirm" }, el("div", { class: "what" }, "Close this run?"),
          el("div", { class: "explain" }, "Nothing more is built. The work so far stays in the files, and the branch on the code host is left as it is."),
          el("div", { class: "actions mt-3" }, yes, cancel));
        yes.onclick = async () => {
          yes.disabled = true; cancel.disabled = true;
          try { await api(`/api/gates/${b.gate_id}/decision`, { body: { decision: "reject", comments: "", by: "console" } }); }
          catch (e) { yes.disabled = false; cancel.disabled = false; err.textContent = String(e.message || e); return; }
          go({}, yes, "Closing…");
        };
        actions.replaceWith(confirmRow);
        setTimeout(() => { yes.disabled = false; yes.focus(); }, 600);
      };
      box.append(actions, status, err,
        el("p", { class: "explain" }, "Also from a terminal: ", el("code", null, `factory resume ${r.run_id} --budget ${Math.ceil(needed)}`), "."));
    }
    return card("Budget", box, b.gate_id ? el("a", { href: `#/gates/${b.gate_id}` }, "gate") : null);
  }

  function describe(ev) {
    const p = ev.payload || {};
    const who = ev.actor;
    const item = ev.spec_id || ev.work_item_id;
    switch (ev.kind) {
      case "run.created": return [who, { main: "Run created", detail: p.title }];
      case "repository.created": return [who, { main: "Repository created on the code host", detail: p.url }];
      case "stage.entered": return [who, { main: `Entered ${({ decomposing: "decomposing", gate_1_decomposition: "Gate 1", specifying: "specifying", gate_2_spec_readiness: "Gate 2", implementing: "building", closed: "closed", completed: "completed" })[p.stage] || words(p.stage)}` }];
      case "gate.opened": return [who, { main: `${GATE_SHORT[p.kind] || p.kind} opened`, detail: `${(p.artifacts || []).length} files to approve` }];
      case "gate.decided": return ["human", { main: `${GATE_SHORT[p.kind] || p.kind}: ${DECISION_LABEL[p.decision] || p.decision}`, detail: p.comments ? `"${firstSentences(p.comments, 120)}"` : "" }];
      case "question.asked": return [who, { main: `The ${p.asked_by} asked a question`, detail: p.question }];
      case "question.answered": return ["human", { main: "Question answered", detail: p.answer }];
      case "judge.verdict": return [who, { main: `Spec judge round ${p.round}: ${p.result}`, detail: p.blocking ? `${p.blocking} blocking` : "" }];
      case "review.findings": return [who, { main: `Review round ${p.round}: ${plural(findingCount(p), "finding")}${p.blocking ? `, ${p.blocking} blocking` : ""}`, detail: item }];
      case "review.response": return [who, { main: `Implementer answered review round ${p.round}`, detail: dispositionText(p) }];
      case "merge.verdict": return [who, { main: `Merge judge: ${p.result}`, detail: item }];
      case "cr.opened": return [who, { main: `Pull request opened for ${item}`, detail: p.url }];
      case "cr.merged": return ["human", { main: `Merged ${item}${p.id ? ` (#${p.id})` : ""}`, detail: [p.merge_commit_sha ? `as ${String(p.merge_commit_sha).slice(0, 10)}` : "", p.approved === false ? "without an approving review" : ""].filter(Boolean).join(", ") }];
      case "cr.closed": return ["human", { main: `Pull request closed without merging for ${item}${p.id ? ` (#${p.id})` : ""}` }];
      case "deploy.started": return [who, { main: `Dev deploy started for ${item}${p.attempt ? ` (observation ${p.attempt + 1})` : ""}`, detail: p.pipeline_url || (p.pipeline_id ? `pipeline ${p.pipeline_id}` : "no pipeline yet") }];
      case "deploy.succeeded": return [who, { main: `Dev deploy succeeded for ${item}`, detail: p.environment_url }];
      case "deploy.failed": return [who, { main: `Dev deploy failed for ${item}`, detail: p.status ? words(p.status) : "" }];
      case "e2e.dev_passed": return [who, { main: `Dev checks passed for ${item}`, detail: p.results ? `${p.results.length} of ${p.results.length} passed on ${p.environment_url || "dev"}` : "" }];
      case "e2e.dev_failed": { const n = (p.results || []).length, bad = (p.results || []).filter(x => x.exit_code !== 0).length;
        return [who, { main: `Dev checks failed for ${item}`, detail: n ? `${bad} of ${n} failed on ${p.environment_url || "dev"}` : "" }]; }
      case "session.started": return [who, { main: `Agent session started: ${CREW_NAME[p.agent_key] || p.agent_key || p.agent_id || "agent"}`, detail: p.title || "", session_id: p.session_id }];
      case "session.finished": return [who, { main: `Agent session finished: ${CREW_NAME[p.agent_key] || p.agent_id || "agent"}`, detail: p.list_cost_usd != null ? money(p.list_cost_usd) : "", session_id: p.session_id }];
      case "work_item.stage": return [who, { main: `${itemShort(ev.work_item_id)}: ${ITEM_STAGE[p.stage] || words(p.stage)}` }];
      case "run.escalated": return [who, { main: "Escalated", detail: p.reason }];
      case "run.completed": return [who, { main: p.design_only ? "Design stages completed" : "Run completed", detail: (p.specs_with_change_requests || p.approved_specs || []).join(", ") }];
      case "run.failed": return [who, { main: "Run failed", detail: p.reason }];
      case "run.resumed": return [who, { main: `Run resumed${p.from === "paused_budget" ? " after the budget pause" : ""}`, detail: Object.entries(p.points || {}).map(([s, x]) => `${s}: round ${x.round}${x.implementer_done ? " (implementer done)" : ""}`).join("; ") || (p.stage ? `into ${words(p.stage)}` : "") }];
      case "budget.reached": {
        // Two shapes: the run cap (budget.py; `next` says what could not start) and one session hitting its own cap (run_session).
        if (p.next) { const n = p.next; return [who, { main: "Paused: the next step would pass the budget", detail: `${money(p.spent_usd)} spent of ${money(p.budget_usd)}; ${n.label || n.stage || "next step"} (${plural(n.sessions || 1, "session")}) could cost ${money(n.cap_usd)}` }]; }
        return [who, { main: `Budget reached: ${p.title || p.agent_key || "a session"} hit its ${p.budget_usd != null ? money(p.budget_usd) : ""} cap`, detail: p.list_cost_usd != null ? `spent ${money(p.list_cost_usd)}; waiting for the owner` : "waiting for the owner", session_id: p.session_id }];
      }
      case "budget.raised": return ["human", { main: `Budget raised to ${money(p.budget_usd)}`, detail: p.previous_budget_usd != null ? `was ${money(p.previous_budget_usd)}${p.decided_by ? ", by " + p.decided_by : ""}` : "" }];
      case "session.truncated": return [who, { main: `Session cut short: ${p.title || p.agent_key || "a session"} (${p.reason || "unfinished"})`, detail: p.outcome_iterations != null ? `${p.outcome_iterations} grader iterations; waiting for the owner` : "waiting for the owner", session_id: p.session_id }];
      default: return [who, { main: ev.kind }];
    }
  }

  // ------------------------------------------------------------------ start a run: three guardrails visible, the rest two clicks away
  async function viewStart(params) {
    setNav("start");
    const nav = state.nav;
    const [outs, cfg] = await Promise.all([api("/api/outcomes"), state.config || api("/api/config")]);
    state.config = cfg;
    let app = null, appNotice = null, apps = [];
    if (!params.get("app")) { try { apps = (await api("/api/apps")).apps || []; } catch (e) { apps = []; } }
    if (params.get("app")) {
      try { app = await api(appPath(params.get("app"))); appNotice = el("div", { class: "notice info" }, el("b", null, `Requesting a change to ${app.name}. `), "Its repository and hosted test setup are selected. Pick or write the outcome; the verified setup must be on the selected base branch before this run can start."); }
      catch (e) { appNotice = el("div", { class: "notice bad" }, el("b", null, "The app did not load. "), `${e.message}. Return to Apps before starting this app run.`); }
    }
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Start a run", "Choose the outcome, repository and budget."));
    if (appNotice) frag.append(appNotice);

    // 1. what
    const appOutcomes = app ? outs.outcomes.filter(o => (o.target_repositories || []).some(r => normRepo(r.url) === normRepo(app.repository_url))) : outs.outcomes;
    let outcomeId = appOutcomes.find(o => o.outcome_id === params.get("outcome"))?.outcome_id || (appOutcomes[0] || {}).outcome_id || null;
    let outcome = null;
    const choices = el("div", { class: "choices" });
    const choiceNodes = [];
    const pickOutcome = async (id) => {
      outcomeId = id; choiceNodes.forEach(n => n.classList.toggle("on", n.dataset.id === id));
      try { outcome = (await api(`/api/outcomes/${id}`)).outcome; } catch (e) { outcome = null; }
      budget.value = (outcome && outcome.constraints && outcome.constraints.budget_usd) ?? 60;
      syncRepo(); syncWho(); syncSummary();
    };
    for (const o of appOutcomes) {
      const radio = el("input", { type: "radio", name: "outcome", checked: o.outcome_id === outcomeId, onchange: () => pickOutcome(o.outcome_id) });
      const n = el("label", { class: `choice ${o.outcome_id === outcomeId ? "on" : ""}`, dataset: { id: o.outcome_id } }, radio,
        el("span", { class: "txt" }, el("b", null, o.title || o.outcome_id), el("span", null, `${o.runs.length ? plural(o.runs.length, "run") + " so far." : "No runs yet."}${o.owner ? " Owner " + o.owner + "." : ""}`)));
      choiceNodes.push(n); choices.append(n);
    }
    const newOutcome = el("a", { class: "btn sm mt-3", href: `#/outcomes/new${app ? '?app=' + encodeURIComponent(app.app_id) : ''}` }, "New outcome");
    const whatCard = card("1. Outcome", el("div", null, appOutcomes.length ? choices : el("p", { class: "explain" }, "Describe the change you want to make."), newOutcome));

    // 2. where: browse the repositories visible to the configured GitHub account. An onboarded app's repository binds to
    // its verified hosted setup on every start path; the server decides, this only says so before you click.
    const bindNote = el("div", { role: "status" });
    let bound = null;
    let repositories = [], nextRepoPage = 1, selectedRepo = null;
    const repoUrl = el("input", { type: "url", placeholder: "https://github.com/you/app", "aria-label": "Repository URL" });
    const repoNew = el("input", { type: "radio", name: "repo" }), repoExisting = el("input", { type: "radio", name: "repo", checked: true });
    const newName = el("input", { type: "url", placeholder: "https://github.com/you/factory-new-app", "aria-label": "New repository URL" });
    const searchRepos = el("input", { type: "search", placeholder: "Filter by owner or repository name", "aria-label": "Filter repositories" });
    const repoSelect = el("select", { "aria-label": "GitHub repository" }, el("option", { value: "" }, "Choose a repository"));
    const repoStatus = el("p", { class: "explain m-0", role: "status", "aria-live": "polite" });
    const repoDetails = el("p", { class: "explain m-0" });
    const renderRepos = () => {
      const query = searchRepos.value.trim().toLowerCase();
      const rows = repositories.filter(r => r.full_name.toLowerCase().includes(query) || r.url === repoUrl.value);
      repoSelect.replaceChildren(el("option", { value: "" }, "Choose a repository"), ...rows.map(r => el("option", {
        value: r.url, disabled: !r.selectable, selected: r.url === repoUrl.value,
      }, `${r.full_name} · ${r.private ? "private" : "public"}${r.unavailable_reason ? " · " + r.unavailable_reason : ""}`)));
      repoStatus.textContent = `${rows.length} shown of ${repositories.length} loaded.${nextRepoPage ? " Load more to include older repositories." : ""}`;
      if (!rows.length) repoStatus.textContent = repositories.length ? "No matching repositories in the loaded results." : "No repositories are visible to this GitHub connection.";
    };
    searchRepos.oninput = renderRepos;
    const loadRepos = async () => {
      loadMore.disabled = true; repoStatus.textContent = "Loading GitHub repositories…";
      try {
        const result = await api(`/api/repositories?page=${nextRepoPage || 1}`);
        const byUrl = new Map(repositories.map(r => [r.url, r]));
        result.repositories.forEach(r => byUrl.set(r.url, r));
        repositories = [...byUrl.values()]; nextRepoPage = result.next_page;
        loadMore.hidden = !nextRepoPage; loadMore.textContent = "Load more repositories";
        renderRepos();
      } catch (e) { repoStatus.textContent = e.message; loadMore.hidden = false; loadMore.textContent = "Retry loading repositories"; }
      finally { loadMore.disabled = false; }
    };
    const loadMore = el("button", { class: "btn sm", type: "button", onclick: loadRepos }, "Load repositories");
    repoSelect.onchange = () => {
      selectedRepo = repositories.find(r => r.url === repoSelect.value) || null;
      repoUrl.value = selectedRepo ? selectedRepo.url : "";
      repoDetails.textContent = selectedRepo ? `Default branch: ${selectedRepo.default_branch}. ${selectedRepo.private ? "Private" : "Public"} repository.` : "";
      syncSummary();
    };
    repoUrl.oninput = () => { selectedRepo = null; repoSelect.value = ""; repoDetails.textContent = ""; syncSummary(); };
    const manual = el("details", null, el("summary", null, "Enter a repository URL instead"), repoUrl);
    const browser = el("div", { class: "stack" }, el("label", null, "Find a repository", searchRepos),
      el("label", null, "GitHub repository", repoSelect), repoDetails, repoStatus, loadMore, manual);
    const existingChoice = el("div", { class: "choice on repo-choice" }, el("label", null, repoExisting, " An existing repository"), browser);
    const newChoice = el("div", { class: "choice repo-choice" }, el("label", null, repoNew, " A new private repository the factory creates and seeds"), newName);
    const syncRepoChoice = () => {
      existingChoice.classList.toggle("on", repoExisting.checked); newChoice.classList.toggle("on", repoNew.checked);
      browser.hidden = !repoExisting.checked; newName.hidden = !repoNew.checked; syncSummary();
    };
    repoExisting.onchange = repoNew.onchange = syncRepoChoice;
    const syncRepo = () => {
      const repo = app ? { url: app.repository_url, default_branch: app.default_branch } : ((outcome || {}).target_repositories || [{}])[0] || {};
      if (repo.create) { repoNew.checked = true; newName.value = repo.url || ""; }
      else { repoExisting.checked = true; repoUrl.value = repo.url || ""; }
      selectedRepo = repositories.find(r => r.url === repoUrl.value) || null;
      repoDetails.textContent = repo.url ? `Selected: ${repo.url} · ${selectedRepo ? selectedRepo.default_branch : repo.default_branch || "main"}` : "";
      renderRepos(); syncRepoChoice();
      if (!app) { whereSummary.textContent = repo.url ? `${repoName(repo.url)} · ${repo.default_branch || "main"}` : "Choose a repository"; whereDetails.open = !repo.url; }
    };
    const whereSummary = el("p", {class:"mono m-0"});
    const whereDetails = el("details", null, el("summary", null, "Change repository"), el("div", {class:"stack mt-3"}, existingChoice, newChoice));
    const whereCard = app ? card("2. Repository", el("div", {class:"stack"},
      el("a", {href:app.repository_url, target:"_blank", rel:"noopener"}, repoName(app.repository_url)),
      el("p", {class:"explain"}, `Base branch: ${app.default_branch || 'main'}. Uses this app's verified hosted setup.`)))

      : card("2. Repository", el("div", { class: "stack" }, whereSummary, whereDetails, bindNote));

    // 3. guardrails
    const budget = el("input", { type: "number", min: "1", step: "1", value: 60 });
    const hostedOption = el("option", { value: "managed", selected: !!app, hidden: !app }, "Hosted sandbox · verified app setup");
    const preview = el("select", { disabled: !!app }, hostedOption, el("option", { value: "local", selected: !app }, "Local (this computer)"), el("option", { value: "platform" }, "Deployed preview"));
    const previewHelp = el("span", { class: "h" }, app ? "Application, database and browser tests run in a fresh hosted sandbox." : "Local runs app commands on this computer. Deployed preview uses the repository's deploy workflow.");
    const syncBinding = () => {
      if (app) return;
      const url = repoNew.checked ? "" : repoUrl.value.trim();
      const matches = url ? apps.filter(a => normRepo(a.repository_url) === normRepo(url)) : [];
      bound = matches.length === 1 ? matches[0] : null;
      hostedOption.hidden = !bound; preview.disabled = !!bound;
      if (bound) preview.value = "managed"; else if (preview.value === "managed") preview.value = "local";
      previewHelp.textContent = bound ? "This repository is an onboarded app: live runs verify in its hosted sandbox." : "Local runs app commands on this computer. Deployed preview uses the repository's deploy workflow.";
      const ready = bound && ["ready", "ready_limited"].includes(bound.status);
      bindNote.replaceChildren(...(matches.length > 1 ? [el("div", { class: "notice bad m-0" }, el("b", null, "Several apps use this repository. "),
          `A live run must name one: start it from the app you mean (${matches.map(a => a.name || a.app_id).join(", ")}) under Apps.`)]
        : bound && ready ? [el("div", { class: "notice info m-0" }, el("b", null, `Onboarded app: ${bound.name || bound.app_id}. `),
          "A live run uses its verified hosted setup; local and platform previews are not offered for it.")]
        : bound ? [el("div", { class: "notice bad m-0" }, el("b", null, `Onboarded app ${bound.name || bound.app_id} is not ready (${words(bound.status)}). `),
          "Verify its setup on the app page before a live run; a design-only run still works.")] : []));
    };
    const whoBox = el("div", { class: "who" });
    const editOutcome = el("a", {href:"#/outcomes", class:"fs-sm"}, "Edit outcome");
    const syncWho = () => { const ow = (outcome && outcome.owner) || {}; whoBox.replaceChildren(
      el("span", null, ow.name || ow.handle || "Set an owner on the outcome"));
      editOutcome.href = outcomeId ? `#/outcomes/new?outcome=${encodeURIComponent(outcomeId)}` : "#/outcomes"; };
    const guards = el("div", { class: "guards" },
      el("label", null, "Run budget (USD)", budget, el("span", { class: "h" }, "Pauses before the next step exceeds this limit.")),
      app ? el("div", null, el("b", null, "Verification"), el("p", {class:"explain"}, "Hosted sandbox")) : el("label", null, "Verification", preview, previewHelp),
      el("div", null, el("b", null, "Owner"), whoBox, editOutcome));
    const passes = el("input", { type: "number", min: "1", max: "4", value: 2 }), rounds = el("input", { type: "number", min: "1", max: "4", value: 2 });
    // The security pass needs its optional agent deployed; without it the box cannot promise a pass that never runs.
    const securityReady = cfg.security_review_available === true;
    const security = el("input", { type: "checkbox", checked: securityReady, disabled: !securityReady, "aria-describedby": securityReady ? null : "security-unavailable" });
    const iterations = el("input", { type: "number", min: "1", max: "6", value: 3 });
    const advanced = el("details", null, el("summary", null, "Review settings"), el("div", {class:"tier"},
      el("label", null, "Reviewer passes", passes), el("label", null, "Maximum review rounds", rounds),
      el("label", null, "Grader iterations", iterations),
      el("div", null, el("label", {class:"check"}, security, "Security review"),
        securityReady ? null : el("p", { class: "explain m-0 mt-1 fs-sm", id: "security-unavailable" }, "Unavailable: no security reviewer agent is deployed in this workspace, so no security pass runs. The general reviewers still run."))));
    const guardsCard = card("3. Budget and review", el("div", {class:"stack"}, guards, advanced));

    // summary + start
    const summary = el("ul", { class: "summary-list" });
    const notice = el("div");
    const syncSummary = () => {
      syncBinding();
      const repo = repoNew.checked ? (newName.value.trim() || "a new private repository") : (repoUrl.value.trim() || "the outcome's repository");
      summary.replaceChildren(
        el("li", null, el("b", null, outcome ? outcome.title : "No outcome selected")),
        el("li", null, "Into ", el("span", { class: "mono" }, repo.replace(/^https:\/\/github\.com\//, ""))),
        el("li", null, `Budget: $${budget.value}`));
    };
    [budget, passes, security, repoUrl, newName].forEach(i => i.addEventListener("input", syncSummary));
    const settings = () => ({ budget_usd: Number(budget.value) || undefined, preview: preview.value, reviewer_passes: Number(passes.value) || undefined, review_rounds: Number(rounds.value) || undefined,
      ...(securityReady ? { security_review: security.checked } : {}), grader_iterations: Number(iterations.value) || undefined });
    async function saveRepo() {
      if (!outcome) return;
      const repo = (outcome.target_repositories || [{}])[0] || {};
      const url = repoNew.checked ? newName.value.trim() : repoUrl.value.trim();
      const branch = selectedRepo && !repoNew.checked ? selectedRepo.default_branch : repo.default_branch || "main";
      const changed = url !== (repo.url || "") || !!repo.create !== repoNew.checked || branch !== repo.default_branch;
      if (!changed) return;
      const next = { ...outcome, target_repositories: [{ ...repo, url, default_branch: branch, create: repoNew.checked || undefined, visibility: repoNew.checked ? (repo.visibility || "private") : undefined, paths_in_scope: repo.paths_in_scope || [] }] };
      const r = await api("/api/outcomes", { body: { outcome: next } });
      if (!r.ok) throw new Error((r.errors || ["The outcome did not save"]).join("; "));
      outcome = next;
    }
    async function go(mode, btn) {
      notice.replaceChildren();
      if (!outcomeId) { notice.append(el("div", { class: "notice bad" }, "Pick an outcome first.")); return; }
      if (params.get("app") && !app) { notice.append(el("div", {class:"notice bad"}, "The app could not be loaded. Return to Apps before continuing.")); return; }
      if (app && (repoNew.checked || normRepo(repoUrl.value) !== normRepo(app.repository_url))) { notice.append(el("div", {class:"notice bad"}, "This setup belongs to a different repository. Open the matching app or start a separate run.")); return; }
      const was = btn.textContent; startBtn.disabled = designBtn.disabled = true; btn.textContent = "Starting…";
      try {
        await saveRepo();
        const s = await api("/api/runs", { body: { outcome_id: outcomeId, app_id: app ? app.app_id : (mode === "live" && bound ? bound.app_id : undefined), mode, settings: settings() } });
        if (s.run_id) location.hash = `#/runs/${s.run_id}`;
        else { notice.append(el("div", { class: "notice ok" }, el("b", null, "Run started. "), "The crew is waking up; the run appears under Runs within a minute.")); setTimeout(() => { location.hash = "#/runs"; }, 4000); }
      } catch (e) { notice.append(el("div", { class: "notice bad" }, e.message)); btn.textContent = was; startBtn.disabled = !cfg.can_run_live; designBtn.disabled = !cfg.can_run_design; }
    }
    const startBtn = el("button", { class: "btn primary lg", type: "button", disabled: !cfg.can_run_live, onclick: ev => go("live", ev.currentTarget) }, "Start the run");
    const designBtn = el("button", { class: "btn", type: "button", disabled: !cfg.can_run_design, onclick: ev => go("design", ev.currentTarget) }, "Design only: stop after Gate 2");
    const aside = el("aside", null,
      card("This run", el("div", { class: "stack" }, summary, el("div", { class: "btn-col" }, startBtn, designBtn), notice,
        el("p", { class: "explain m-0 fs-sm" }, "Design only stops after contract approval."),
        !cfg.can_run_live ? el("p", { class: "explain m-0 fs-sm" }, `Starting a run needs ${(cfg.gaps || []).join("; ") || "the crew and the code host"}. `, el("a", { href: "#/settings" }, "Settings")) : null,
        !cfg.can_run_design && cfg.can_run_live ? el("p", { class: "explain m-0 fs-sm" }, `A design run needs ${(cfg.design_gaps || []).join("; ")}. `, el("a", { href: "#/settings" }, "Settings")) : null)));

    frag.append(el("div", { class: "start" }, el("main", null, whatCard, whereCard, guardsCard), aside));
    if (!paint(frag, nav)) return;
    stopPoll();
    if (outcomeId) await pickOutcome(outcomeId); else { syncRepo(); syncWho(); syncSummary(); }
    syncWho();
    if (cfg.forge === "github" && !app) await loadRepos();
    else { browser.replaceChildren(el("label", null, "Repository URL", repoUrl)); }
    if (app) {
      const match = repositories.find(r => normRepo(r.url) === normRepo(app.repository_url));
      if (match) { repoUrl.value = match.url; selectedRepo = match; repoSelect.value = match.url; repoDetails.textContent = `Selected for ${app.name}: default branch ${match.default_branch}.`; }
      else manual.open = true;
      syncSummary();
    }
  }

  // ------------------------------------------------------------------ gate review
  async function viewGate(gateId) {
    setNav("inbox");
    const nav = state.nav;
    const g = await api(`/api/gates/${gateId}`);
    const rec = g.gate, rv = g.review;
    if (rec.kind === "build_recovery") { location.hash = `#/runs/${g.run_id}#recovery`; return; }
    const notes = new Map();
    const picks = new Map();
    const frag = document.createDocumentFragment();
    const head = pageHead(rec.kind === "budget" && !rv.budget ? "Session limit: continue or stop" : GATE_NAME[rec.kind] || rec.kind, null, rec.kind === "budget" ? null : [el("a", { class: "btn", href: `/gates/${gateId}/page`, target: "_blank", rel: "noopener" }, "Open as a page")],
      [el("a", { href: "#/board" }, "Board"), " / ", el("a", { href: `#/runs/${g.run_id}` }, g.run_title)]);
    // the same sentence and line as the run page, so the person deciding sees where the run is and who waits on whom
    if (g.summary) head.querySelector("div").append(el("div", { class: "now" }, el("p", { class: "sentence" }, statusSentence(g.summary, true)), el("p", { class: "meta" }, ...metaLine(g.summary))));
    frag.append(head);
    try {
      const run = await api(`/api/runs/${g.run_id}`);
      frag.append(el("section", { class: "card line-card" }, lineStrip(run, "full")));
    } catch (e) { /* the gate is readable without the line */ }

    const doc = el("div", { class: "stack" });
    const prev = rv.previous_decisions || [];
    if (prev.length) {
      const last = prev[prev.length - 1];
      doc.append(el("div", { class: "notice info" }, el("b", null, `Revised after you chose "${DECISION_LABEL[last.decision] || last.decision}". `), last.comments ? `Your note was: "${last.comments}"` : ""));
    }
    if (rv.kind === "decomposition" && rv.decomposition) renderDecomposition(doc, rv, notes);
    else if (rv.kind === "specs" && rv.verdict) renderSpecs(doc, rv, notes, picks, rec.options.includes("approve_subset"));
    else if (rv.kind === "deploy") renderDeploy(doc, rv, rec);
    else if (rv.kind === "budget") renderBudget(doc, rv, rec, g);
    else doc.append(card("Summary", el("div", { class: "reading" }, rec.summary)));

    const decide = buildDecide(g, notes, picks);
    frag.append(el("div", { class: "review" }, doc, decide.node));
    if (!paint(frag, nav)) return;
    startPoll(async () => { try { decide.update(await api(`/api/gates/${gateId}`)); } catch (e) { /* keep */ } }, 4000);
  }

  function glance(items) {
    return el("div", { class: "glance" }, items.map(([v, k, cls]) => el("div", { class: "g" }, el("div", { class: `v ${cls || ""}` }, v), el("div", { class: "k" }, k))));
  }
  function checklist(rows) {
    // a reading aid for the reviewer: each line is a real label, so the text toggles the box and a screen reader names it
    return el("ul", { class: "checklist" }, rows.map(([t, d]) => {
      const cb = el("input", { type: "checkbox" });
      return el("li", null, el("label", { class: "check-row" }, cb, el("span", null, el("span", { class: "t" }, t), el("span", { class: "d" }, d))));
    }));
  }
  function summaryCard(label, text) {
    if (String(text || "").length <= 1200) return el("div", { class: "summary-card" }, el("h2", null, label), el("div", { class: "text" }, text));   // short enough to read whole
    const body = el("div", { class: "text clamp" }, text);
    const more = el("button", { class: "btn sm more", type: "button", "aria-expanded": "false", onclick: () => { body.classList.toggle("clamp"); const open = !body.classList.contains("clamp"); more.textContent = open ? "Show less" : "Read the whole summary"; more.setAttribute("aria-expanded", String(open)); } }, "Read the whole summary");
    const node = el("div", { class: "summary-card" }, el("h2", null, label), body, more);
    (document.fonts ? document.fonts.ready : Promise.resolve()).then(() => { if (body.scrollHeight <= body.clientHeight + 4) { body.classList.remove("clamp"); more.remove(); } });
    return node;
  }
  function noteField(id, placeholder, notes) {
    const ta = el("textarea", { placeholder, rows: "1", "aria-label": `Note on ${id}` });
    notes.set(id, ta);
    return el("div", { class: "note" }, el("label", null, "Your note on this item"), ta);
  }
  function waves(items) {
    const byId = Object.fromEntries(items.map(w => [w.work_item_id, w]));
    const level = {};
    const depth = id => { if (level[id] != null) return level[id]; const w = byId[id]; level[id] = 0; level[id] = 1 + Math.max(-1, ...(w && w.depends_on || []).filter(d => byId[d]).map(depth)); return level[id]; };
    items.forEach(w => depth(w.work_item_id));
    return level;
  }

  function renderDecomposition(doc, rv, notes) {
    const d = rv.decomposition, o = rv.outcome || {};
    const items = d.work_items || [];
    const metrics = (o.success_metrics || []).map(m => m.metric);
    const cov = Object.fromEntries((d.coverage || []).map(c => [c.metric, c]));
    const uncovered = (metrics.length ? metrics : Object.keys(cov)).filter(m => !((cov[m] || {}).work_item_ids || []).length);
    const judgement = items.filter(w => w.human_judgement_required);
    const openQ = d.open_questions || [];
    const checksBad = (rv.checks || []).filter(c => !c.passed);

    doc.append(card("What you are deciding", el("div", { class: "stack" },
      el("p", { class: "explain" }, el("strong", null, "The decomposer turned the outcome into work items. "), "Approving means the crew starts writing a contract for each item. Nothing is built yet, and you get another gate before code."),
      summaryCard("The decomposer's summary", d.summary),
      glance([[items.length, "work items"], [`${metrics.length - uncovered.length}/${metrics.length}`, "metrics covered", uncovered.length ? "bad" : "ok"],
        [judgement.length, "need your judgement", judgement.length ? "warn" : ""], [openQ.length, "open questions", ""],
        [checksBad.length ? "failed" : "passed", "automatic checks", checksBad.length ? "bad" : "ok"]]))));

    doc.append(card("What to check", checklist([
      ["Every metric has work behind it", uncovered.length ? `${uncovered.join(", ")} has no work item. Ask for one or accept the rationale below.` : "Each success metric maps to at least one work item (table below)."],
      ["The items are the right size and order", "Look at sizes and the order below; a huge first item or a missing dependency is the usual problem."],
      ["Items that need your judgement", judgement.length ? `${judgement.map(w => w.title).join("; ")}. Read these closely: they contain a product decision.` : "None flagged."],
      ["Scope", `${(d.explicitly_out_of_scope || []).length} things are explicitly out of scope. Make sure nothing you expected is there.`],
      ["Open questions and their defaults", openQ.length ? `${openQ.length} questions will use their default unless you say otherwise in a note.` : "None."],
    ]), "for your own reading; nothing is recorded"));

    const tbl = el("table", { class: "coverage" }, el("tr", null, el("th", null, "Success metric"), el("th", null, "Target"), el("th", null, "Work items that move it"), el("th", null, "Why")));
    for (const m of (metrics.length ? metrics : Object.keys(cov))) {
      const c = cov[m] || {}; const ids = c.work_item_ids || [];
      const target = ((o.success_metrics || []).find(x => x.metric === m) || {}).target || "";
      tbl.append(el("tr", null, el("td", null, el("b", null, m)), el("td", null, target), ids.length ? el("td", { class: "mono explain" }, ids.join(", ")) : el("td", { class: "gap" }, "nothing"), el("td", null, c.rationale || "")));
    }
    doc.append(card("Metric coverage", tbl));

    const lvl = waves(items);
    const ordered = [...items].sort((a, b) => (lvl[a.work_item_id] - lvl[b.work_item_id]) || items.indexOf(a) - items.indexOf(b));
    const titleOf = id => (items.find(x => x.work_item_id === id) || {}).title || id;
    const wrap = el("div");
    ordered.forEach((w, i) => {
      wrap.append(el("div", { class: "item-card", id: `wi-${w.work_item_id}` },
        el("div", { class: "ih" }, el("span", { class: "num" }, i + 1), el("span", { class: "t" }, w.title), el("span", { class: "id" }, w.work_item_id),
          el("span", { class: "right" }, el("span", { class: "tag" }, words(w.kind)), w.size ? el("span", { class: "tag" }, `size ${w.size}`) : null,
            w.human_judgement_required ? el("span", { class: "tag warn" }, "needs your judgement") : null)),
        el("div", { class: "ib" },
          el("div", { class: "intent" }, w.intent),
          el("div", { class: "facts" },
            el("span", null, el("b", null, "Moves: "), (w.contributes_to_metrics || []).join("; ") || "no metric"),
            el("span", null, el("b", null, "After: "), (w.depends_on || []).map(titleOf).join("; ") || "nothing, can start first")),
          (w.touches || []).length ? el("details", null, el("summary", null, "Where it touches the code"), el("div", null, el("ul", { class: "paths" }, w.touches.map(t => el("li", null, typeof t === "string" ? t : JSON.stringify(t)))))) : null,
          noteField(w.work_item_id, "What should change about this item?", notes))));
    });
    doc.append(card("Work items, in the order they would be built", wrap, plural(items.length, "item")));

    const rest = el("div", { class: "stack" });
    if (openQ.length) rest.append(el("div", null, el("h3", null, "Open questions"), el("ul", { class: "reading" }, openQ.map(q => el("li", null, q.question, el("div", { class: "explain" }, `Default: ${q.default_if_unanswered || "none"}${q.blocking ? " (blocking)" : ""}`))))));
    if ((d.explicitly_out_of_scope || []).length) rest.append(el("div", null, el("h3", null, "Explicitly out of scope"), el("ul", { class: "reading" }, d.explicitly_out_of_scope.map(x => el("li", null, x)))));
    if ((d.assumptions || []).length || (d.risks || []).length) rest.append(el("div", null, el("h3", null, "Assumptions and risks"), el("ul", { class: "reading" }, [...(d.assumptions || []), ...(d.risks || [])].map(x => el("li", null, typeof x === "string" ? x : (x.risk || x.description || JSON.stringify(x)))))));
    if ((rv.checks || []).length) rest.append(el("div", null, el("h3", null, "Automatic checks"), el("ul", { class: "reading" }, rv.checks.map(c => el("li", null, el("span", { class: `tag ${c.passed ? "ok" : "bad"}` }, c.passed ? "passed" : "failed"), " ", el("code", null, c.check.split(":")[0]), " ", c.detail)))));
    if (rv.decomposition_md) rest.append(el("details", null, el("summary", { class: "explain" }, "The full document the agent wrote (decomposition.md)"), el("div", { class: "reading", html: md(rv.decomposition_md) })));
    doc.append(card("Questions, scope, and checks", rest));
  }

  function renderSpecs(doc, rv, notes, picks, subsetAllowed) {
    const v = rv.verdict, specs = rv.specs, ov = v.overall || {};
    const issuesBy = Object.fromEntries((v.per_spec || []).map(ps => [ps.spec_id, ps]));
    const setIssues = Object.values(v.coherence || {}).flatMap(items => items || []).filter(it => typeof it === "object");
    const allIssues = [...(v.per_spec || []).flatMap(ps => ps.issues || []), ...setIssues];
    const sev = s => allIssues.filter(i => i.severity === s).length;
    const userVisible = Object.values(specs).flatMap(s => s.acceptance_criteria || []).filter(a => a.user_visible).length;
    const totalAC = Object.values(specs).flatMap(s => s.acceptance_criteria || []).length;
    const byPeople = (v.goal_coverage || []).filter(g => g.assessment === "not_measurable_by_code");   // closed by sign-off, by design (docs/03)
    const notCovered = (v.goal_coverage || []).filter(g => g.assessment !== "covered" && g.assessment !== "not_measurable_by_code");
    const openQ = Object.values(specs).flatMap(s => (s.open_questions || []).map(q => ({ ...q, sid: s.spec_id })));

    // The decision first, in product terms: the judge's call, what each contract changes, what is left out, and what is
    // still undecided. Interfaces, paths and raw tests stay below, folded, for whoever needs them.
    const blockingN = sev("blocking");
    const call = ov.result === "pass" && !blockingN
      ? ["ok", "The judge passed the set.", " Approving is the expected call once the user-visible criteria read right to you."]
      : blockingN ? ["bad", `The judge flagged ${plural(blockingN, "blocking issue")}.`, " Request changes unless you accept them as they stand."]
      : ["warn", `The judge's verdict is ${words(ov.result || "missing")}.`, " Read what it flagged before approving."];
    const jump = sid => () => { const node = document.getElementById(`spec-${sid}`); if (node) { node.scrollIntoView({ block: "start" }); node.querySelector("summary, textarea")?.focus({ preventScroll: true }); } };
    const firstSentence = text => firstSentences(text, 220);
    // what the whole change leaves out: a non-goal that names a contract of this set is covered there, not excluded
    const excludedBy = new Map();
    for (const s of Object.values(specs)) for (const x of s.non_goals || []) {
      if (Object.keys(specs).some(id => String(x).includes(id))) continue;
      excludedBy.set(x, [...(excludedBy.get(x) || []), s]);
    }
    const excluded = [...excludedBy].map(([x, ss]) => ({ x, s: { title: ss.map(s => s.title || s.spec_id).join(", ") } }));
    doc.append(card("The decision in brief", el("div", { class: "stack brief" },
      el("p", { class: `call ${call[0]}` }, el("b", null, call[1]), call[2]),
      el("div", null, el("h3", { class: "mini-h" }, "What changes"), el("ol", { class: "brief-list" }, Object.entries(specs).map(([sid, s]) => {
        const ps = issuesBy[sid] || { issues: [] }, acs = s.acceptance_criteria || [];
        const b = (ps.issues || []).filter(x => x.severity === "blocking").length;
        return el("li", null, el("button", { type: "button", class: "btn link", onclick: jump(sid) }, s.title || sid), " ", firstSentence(s.goal),
          el("div", { class: "explain" }, [`${plural(acs.filter(a => a.user_visible).length, "user-visible criterion", "user-visible criteria")}`,
            s.estimated_size ? `size ${s.estimated_size}` : null, b ? `${b} blocking` : (ps.issues || []).length ? plural(ps.issues.length, "issue") : "no issues"].filter(Boolean).join(" · ")));
      }))),
      excluded.length ? el("div", null, el("h3", { class: "mini-h" }, "Left out on purpose"), el("ul", { class: "brief-list" },
        excluded.slice(0, 6).map(({ x, s }) => el("li", null, x, el("span", { class: "explain" }, ` (${s.title || s.spec_id})`)))),
        excluded.length > 6 ? el("p", { class: "explain m-0 mt-1" }, `and ${excluded.length - 6} more under the contracts below.`) : null) : null,
      el("div", null, el("h3", { class: "mini-h" }, "Still undecided"), openQ.length
        ? el("ul", { class: "brief-list" }, openQ.map(q => el("li", null, q.question, el("span", { class: "explain" }, ` Default if you say nothing: ${q.default_if_unanswered || "none"}.`))))
        : el("p", { class: "explain m-0" }, "No open questions in the contracts.")))));

    doc.append(card("What you are deciding", el("div", { class: "stack" },
      el("p", { class: "explain" }, el("strong", null, "The crew wrote one contract per work item; an independent judge checked the set. "), "A contract is what an agent will build to: acceptance criteria, interfaces, and which files it may touch. Approving a contract starts its implementation."),
      summaryCard("The judge's summary", ov.summary_for_human || ""),
      glance([[words(ov.result), "judge's verdict", ov.result === "pass" ? "ok" : "warn"], [Object.keys(specs).length, "contracts"],
        [totalAC, `acceptance criteria · ${userVisible} user-visible`], [sev("blocking") ? `${sev("blocking")} blocking` : sev("major") ? `${sev("major")} major` : allIssues.length, "issues the judge flagged", sev("blocking") ? "bad" : sev("major") ? "warn" : ""],
        [`${(v.goal_coverage || []).length - notCovered.length}/${(v.goal_coverage || []).length}`, byPeople.length ? `metrics covered · ${byPeople.length} measured by people` : "metrics covered by criteria", notCovered.length ? "warn" : "ok"]]))));

    doc.append(card("What to check", checklist([
      ["The judge's verdict and anything it flagged", allIssues.length ? `${allIssues.length} issues, listed across the set and under each contract. Blocking ones should be resolved before you approve.` : "The judge found nothing outstanding."],
      ["User-visible acceptance criteria", `${userVisible} criteria describe behaviour a user or operator will see. Read those as if they were the feature description.`],
      ["Product decisions hiding in the criteria", "Customer-facing wording, limits, timings, and defaults in a criterion are decisions you are making by approving."],
      ["Where each agent may change code", "Each contract lists allowed paths. If one reaches into something it should not, request changes."],
      ["Open questions and their defaults", openQ.length ? `${openQ.length} questions across the contracts will use their defaults unless you say otherwise.` : "None."],
    ]), "for your own reading; nothing is recorded"));

    const byContract = acs => { const m = {}; for (const x of acs || []) { const [sid, ac] = String(x).split("/"); (m[sid] = m[sid] || []).push(ac || x); }
      return Object.entries(m).map(([sid, list]) => `${(specs[sid] || {}).title || sid}: ${list.join(", ")}`).join(" · "); };
    const gc = el("table", { class: "coverage" }, el("tr", null, el("th", null, "Success metric"), el("th", null, "Covered?"), el("th", null, "By which criteria"), el("th", null, "Judge's note")));
    for (const g of v.goal_coverage || []) gc.append(el("tr", null, el("td", null, el("b", null, g.metric)), el("td", { class: g.assessment === "covered" ? "ok" : g.assessment === "not_measurable_by_code" ? "" : "gap" }, g.assessment === "not_measurable_by_code" ? "measured by people" : words(g.assessment)), el("td", { class: "explain" }, byContract(g.covering_acs)), el("td", null, g.note || "")));
    const coh = Object.entries(v.coherence || {}).flatMap(([bucket, items]) => (items || []).map(it => ({ bucket, it })));
    doc.append(card("Metrics and how the contracts fit together", el("div", { class: "stack" }, gc,
      coh.length ? el("div", null, el("h3", null, "Across the set"), el("ul", { class: "reading" }, coh.map(({ bucket, it }) => el("li", null,
        el("span", { class: "tag" }, words(bucket)), " ", it.severity ? el("span", { class: `sev ${it.severity}` }, it.severity) : null, " ",
        typeof it === "string" ? it : (it.description || JSON.stringify(it)),
        it.related_spec_ids ? el("span", { class: "explain" }, ` (${it.related_spec_ids.join(", ")})`) : null,
        it.suggested_resolution ? el("div", { class: "fix" }, "Suggested: " + it.suggested_resolution) : null)))) : null)));

    const wrap = el("div");
    const several = Object.keys(specs).length > 1;
    Object.entries(specs).forEach(([sid, s], i) => {
      const ps = issuesBy[sid] || { issues: [] };
      // each box is named for its contract, so a list of them reads as a list of choices, not one label repeated
      const cb = el("input", { type: "checkbox", checked: true });
      picks.set(sid, cb);
      cb.addEventListener("change", () => {   // a contract others build on goes with them: unselecting it unselects them, selecting one selects what it needs
        const sync = (ocb, on) => { if (ocb && ocb.checked !== on) { ocb.checked = on; ocb.dispatchEvent(new Event("change")); } };
        if (!cb.checked) Object.entries(specs).forEach(([oid, o]) => { if (((o.dependencies || {}).specs || []).includes(sid)) sync(picks.get(oid), false); });
        else ((s.dependencies || {}).specs || []).forEach(dep => sync(picks.get(dep), true));
      });
      const acs = s.acceptance_criteria || [];
      const uv = acs.filter(a => a.user_visible).length;
      const blocking = (ps.issues || []).filter(x => x.severity === "blocking").length;
      const acTable = el("table", null, el("tr", null, el("th", null, "Criterion"), el("th", null, "Given, when, then"), el("th", null, "Verified by")));
      for (const ac of acs) acTable.append(el("tr", { class: "ac-row" },
        el("td", null, el("code", null, ac.ac_id), el("div", null, el("span", { class: "tag" }, ac.user_visible ? "user-visible" : "internal"))),
        el("td", { class: "gwt" }, el("b", null, "Given"), ac.given, " ", el("b", null, "When"), ac.when, " ", el("b", null, "Then"), ac.then),
        el("td", null, words(ac.verification))));
      const issues = (ps.issues || []).map(x => el("div", { class: "issue" }, el("span", { class: `sev ${x.severity}` }, x.severity), x.description, x.suggested_resolution ? el("div", { class: "fix" }, "Suggested: " + x.suggested_resolution) : null));
      const tp = s.test_plan || {};
      wrap.append(el("div", { class: "item-card", id: `spec-${sid}` },
        el("div", { class: "ih" }, el("span", { class: "num" }, i + 1), el("span", { class: "t" }, s.title), el("span", { class: "id" }, sid),
          el("span", { class: "right" }, blocking ? el("span", { class: "tag bad" }, `${blocking} blocking`) : (ps.issues || []).length ? el("span", { class: "tag warn" }, plural((ps.issues || []).length, "issue")) : null,
            subsetAllowed ? el("label", { class: "pick" }, cb, "Approve this contract", el("span", { class: "sr-only" }, `(${s.title || sid})`)) : null)),
        el("div", { class: "ib" },
          el("div", { class: "intent" }, s.goal),
          el("div", { class: "facts" }, el("span", null, el("b", null, `${acs.length} criteria`), `, ${uv} user-visible`), el("span", null, el("b", null, "Size: "), s.estimated_size || "?"),
            el("span", null, el("b", null, "After: "), ((s.dependencies || {}).specs || []).map(d => (specs[d] || {}).title || d).join("; ") || "nothing"), el("span", null, el("b", null, "May edit: "), plural((s.allowed_paths || []).length, "path"))),
          issues.length ? el("div", null, el("h3", { class: "sub-h" }, "What the judge flagged"), ...issues) : null,
          el("details", { open: !several }, el("summary", null, `Acceptance criteria (${acs.length}, ${uv} user-visible)`), el("div", null, acTable)),
          el("details", null, el("summary", null, `Interfaces (${(s.interfaces || []).length}), allowed paths (${(s.allowed_paths || []).length}), test plan`), el("div", null,
            (s.interfaces || []).length ? el("ul", { class: "reading" }, s.interfaces.map(x => el("li", null, el("code", null, x.name), ` ${words(x.kind)}, ${x.change}`, el("pre", { class: "block" }, x.definition)))) : null,
            el("h3", { class: "sub-h" }, "Allowed paths"), el("ul", { class: "paths" }, (s.allowed_paths || []).map(p => el("li", null, p))),
            el("h3", { class: "sub-h" }, "Test plan"), el("ul", { class: "paths" }, (tp.commands || []).map(c => el("li", null, c))),
            (tp.e2e || []).length ? el("ul", { class: "paths" }, tp.e2e.map(e => el("li", null, `${e.command}  (${e.target}, covers ${(e.covers_ac || []).join(", ")})`))) : null)),
          (s.open_questions || []).length ? el("details", null, el("summary", null, `Open questions (${s.open_questions.length})`), el("div", null, el("ul", { class: "reading" }, s.open_questions.map(q => el("li", null, q.question, el("div", { class: "explain" }, `Default: ${q.default_if_unanswered || "none"}`)))))) : null,
          rv.spec_md[sid] ? el("details", null, el("summary", null, "The contract as the agent wrote it"), el("div", { class: "reading", html: md(rv.spec_md[sid]) })) : null,
          noteField(sid, "What should change in this contract?", notes))));
    });
    doc.append(card("The contracts", wrap, plural(Object.keys(specs).length, "contract")));
  }

  function renderBudget(doc, rv, rec, g) {
    if (!rv.budget) {
      doc.append(card("Session needs a decision", el("p", {class:"reading"}, rec.summary)));
      return;
    }
    const p = rv.budget || {}, next = p.next || {};
    doc.append(card("What you are deciding", el("div", { class: "stack" },
      el("p", { class: "explain" }, el("strong", null, "The run stopped before starting an agent session that could take it past its budget. "),
        "Nothing is running. Approve with a new budget to continue from exactly where it stopped, or reject to close the run; the work so far stays in the files either way."),
      glance([[money(p.spent_usd), `spent across ${plural(p.sessions_finished || 0, "session")}`], [money(p.budget_usd), "the run's budget"],
        [money(next.cap_usd), `next: ${next.label || next.stage} (${plural(next.sessions || 1, "session")}), at most`], [money(p.needed_usd), "needed to continue"]]),
      el("p", { class: "explain" }, `Paused in the ${words(p.run_stage || "")} stage. Every session carries its own hard cap, so the check is spent plus the next step's caps: the run can never go past the budget. A step usually costs less than its cap.`),
      g.resumable === false ? el("p", { class: "explain" }, "This run cannot be driven again from here, so a decision recorded now is not applied. Start a new run from the outcome.")
          : el("p", { class: "explain" }, "The decision is applied by the controller when the run is continued: from the ", el("a", { href: `#/runs/${g.run_id}` }, "run page"), ", or with ", el("code", null, `factory resume ${g.run_id}`), "."))));
  }

  function renderDeploy(doc, rv, rec) {
    doc.append(card("What happened", el("div", { class: "stack" }, el("div", { class: "reading" }, rec.summary),
      rv.report ? el("pre", { class: "block" }, rv.report.log_excerpt || "(no log)") : null,
      rv.report && rv.report.environment_url ? el("p", null, "Environment: ", el("a", { href: rv.report.environment_url, target: "_blank", rel: "noopener" }, rv.report.environment_url)) : null)));
  }

  function buildDecide(g, notes, picks) {
    const rec = g.gate;
    const comment = el("textarea", { id: "decide-comment", placeholder: rec.options.includes("request_changes") ? "Required for Request changes; optional otherwise." : "Optional note about your decision." });
    const status = el("div");
    const error = el("div", { class: "err", role: "alert" });
    const counts = el("div", { class: "explain mt-2" });
    const confirmBox = el("div", { class: "confirm", hidden: true, role: "group", "aria-labelledby": "confirm-what" });
    let armed = null;

    const isBudget = rec.kind === "budget" && !!(g.review || {}).budget;
    const label = opt => isBudget && opt === "approve" ? "Raise the budget and continue" : isBudget && opt === "reject" ? "Close the run" : DECISION_LABEL[opt] || opt;
    const needed = isBudget ? Number(((g.review || {}).budget || {}).needed_usd || 0) : 0;
    const budgetInput = isBudget ? el("input", { id: "decide-budget", type: "number", min: String(Math.ceil(needed)), step: "1", value: String(Math.ceil(needed)), "aria-label": "New budget in USD", class: "w-narrow" }) : null;
    const composeComments = () => {
      const lines = [];
      if (isBudget && budgetInput.value) lines.push(`budget_usd: ${Number(budgetInput.value)}`);
      if (comment.value.trim()) lines.push(comment.value.trim());
      for (const [id, ta] of notes) if (ta.value.trim()) lines.push(`${id}: ${ta.value.trim()}`);
      return lines.join("\n");
    };
    const subset = () => [...picks].filter(([, cb]) => cb.checked).map(([sid]) => sid);
    const refresh = () => {
      const n = [...notes.values()].filter(ta => ta.value.trim()).length;
      const picked = picks.size ? subset().length : null;
      counts.textContent = [n ? `${plural(n, "note")} on items go with your decision` : "", picked !== null ? `${picked} of ${picks.size} contracts selected${picked < picks.size ? "; a contract that builds on an unselected one goes with it" : ""}` : ""].filter(Boolean).join(". ");
      buttons.forEach(b => {
        if (b.dataset.decision === "approve_subset") b.hidden = picked === picks.size;
        if (b.dataset.decision === "approve" && picks.size) b.hidden = picked !== picks.size;
      });
    };
    notes.forEach(ta => ta.addEventListener("input", refresh));
    picks.forEach(cb => cb.addEventListener("change", refresh));
    comment.addEventListener("input", refresh);

    const consequences = {
      gate_1_decomposition: { approve: "The crew starts writing a contract for every work item, with your notes as guidance. You get Gate 2 before any code.", approve_subset: "", request_changes: "The decomposer revises the plan using your notes and asks you again.", reject: "The run closes. Nothing else happens." },
      gate_2_spec_readiness: { approve: "The crew starts implementing all contracts on separate branches; your notes go to the implementers as guidance and never change a contract. Pull requests follow.", approve_subset: "Only the selected contracts are implemented, with your notes as guidance. The others are not built in this run and are not sent back to a writer; use Request changes to have them revised. A selection must include the contracts it depends on.", request_changes: "The writers revise using your notes; the judge re-checks; you decide again.", reject: "The run closes. Nothing is built." },
      escalation: { approve: "Proceed with the contracts as they are; your notes go to the implementers as guidance.", approve_subset: "Only the selected contracts proceed (with their prerequisites selected too); the rest are not built in this run.", request_changes: "The writers revise using your notes.", reject: "The run closes." },
      deploy_failure: { approve: "The failure was the platform's; the next deploy is watched.", request_changes: "An agent fixes it on a new branch; a follow-up pull request follows.", reject: "This work item is closed." },
      budget: { approve: "The run's budget becomes the amount above; when continued, it picks up at the step it stopped before.", reject: "The run closes when continued. Nothing more is built; the files stay." },
    }[rec.kind] || {};
    if (rec.kind === "budget" && !isBudget) {
      consequences.approve = "Continue according to the session decision described above.";
      consequences.reject = "Stop this run without accepting the unfinished session output.";
    }
    const buttons = rec.options.map(opt => el("button", { class: `btn block ${opt === "approve" || opt === "approve_subset" ? "approve" : opt === "request_changes" ? "changes" : "danger"}`,
      type: "button", dataset: { decision: opt }, onclick: () => arm(opt) }, label(opt)));

    function arm(opt) {
      error.textContent = "";
      const comments = composeComments();
      if (opt === "request_changes" && !comments) { error.textContent = "Say what should change first."; comment.focus(); return; }
      if (isBudget && opt === "approve" && !(Number(budgetInput.value) >= needed)) { error.textContent = `The next step needs at least ${money(needed)}.`; return; }
      buttons.forEach(b => b.hidden = true);
      armed = opt;
      // the comment and the notes are read again when the decision is confirmed, so an edit made after arming is never lost
      const go = el("button", { class: `btn block ${opt === "reject" ? "danger" : opt === "request_changes" ? "changes" : "approve"}`, type: "button", disabled: true, "aria-describedby": "confirm-why", onclick: () => decide(opt, composeComments() || comments) }, `Yes, ${label(opt).toLowerCase()}`);
      confirmBox.replaceChildren(el("div", { class: "what", id: "confirm-what" }, `${label(opt)}?`),
        el("div", { class: "explain", id: "confirm-why" }, consequences[opt] || ""),
        el("div", { class: "btn-col mt-3" }, go, el("button", { class: "btn block", type: "button", onclick: disarm }, "Cancel")));
      confirmBox.hidden = false;
      setTimeout(() => { go.disabled = false; go.focus(); }, 600);
    }
    function disarm() {
      const was = armed; armed = null; confirmBox.hidden = true; buttons.forEach(b => b.hidden = false); refresh();
      const back = Array.from(buttons).find(b => b.dataset.decision === was); if (back && !back.hidden) back.focus();   // Cancel returns focus where it came from
    }
    // changing which contracts are selected while a decision is armed changes what the decision means: start over
    picks.forEach(cb => cb.addEventListener("change", () => { if (armed) disarm(); }));
    async function decide(opt, comments) {
      confirmBox.querySelectorAll("button").forEach(b => b.disabled = true);
      try {
        const r = await api(`/api/gates/${rec.gate_id}/decision`, { body: { decision: opt, comments, approved_subset: opt === "approve_subset" ? subset() : [], by: "console" } });
        confirmBox.hidden = true;
        showRecorded({ ...r.recorded, decision: opt });
      } catch (e) { error.textContent = e.message; disarm(); }
    }
    function cls(d) { return d === "request_changes" ? "changes" : d === "reject" ? "reject" : "ok"; }
    function showRecorded(d) {
      status.replaceChildren(el("div", { class: `recorded ${cls(d.decision)}`, role: "status", tabindex: "-1" }, el("b", null, `${DECISION_LABEL[d.decision] || d.decision} recorded`),
        `${ago(d.decided_at)}. ${g.run_status === "stopped" ? (g.resumable ? `Nothing applies it until a controller drives this run again (factory resume ${g.run_id}). ` : "Nothing applies it: this run cannot be driven again from here. Start a new run from the outcome. ") : isBudget ? "The run is parked: continue it from its page to apply the decision. " : "The run picks it up within a few seconds. "}`,
        el("a", { href: `#/runs/${g.run_id}` }, isBudget ? "Go to the run" : "Back to the run")));
      form.hidden = true;
      const line = status.querySelector(".recorded"); if (line) line.focus();   // a screen reader hears the outcome
    }
    function showDecided(p) {
      status.replaceChildren(el("div", { class: `recorded ${cls(p.decision)}` }, el("b", null, `${DECISION_LABEL[p.decision] || p.decision}, applied`),
        "The run has moved on. ", el("a", { href: `#/runs/${g.run_id}` }, "See where it is now."), p.comments ? el("div", { class: "explain mt-2" }, `“${p.comments}”`) : null));
      form.hidden = true;
    }

    const form = el("div", null,
      isBudget ? el("div", { class: "mb-3" }, el("label", { for: "decide-budget" }, "New budget in USD"), budgetInput) : null,
      el("label", { for: "decide-comment" }, "Your comment"), comment, counts,
      isBudget ? null : el("p", { class: "explain m-0 fs-sm" }, "Notes on items go to the agent: as feedback with Request changes, as guidance with Approve."),
      confirmBox, el("div", { class: "btn-col mt-3" }, buttons), error,
      rec.artifacts.length ? el("details", null, el("summary", null, `Bound to the ${plural(rec.artifacts.length, "file")} you are reading`),
        el("p", { class: "explain m-0" }, "Your decision is recorded with these fingerprints. If a file no longer matches them when the run picks the decision up, the decision is refused and you are asked to look again."),
        el("ul", { class: "files" }, rec.artifacts.map(a => el("li", null, el("span", null, a.name), el("code", { title: a.sha256 }, a.sha256.slice(0, 10)))))) : null);
    // a stopped run's decision is recorded but not picked up: say so before the buttons, not after
    const stoppedNote = g.run_status === "stopped" ? el("div", { class: "notice bad m-0 mb-3" }, el("b", null, "The controller is stopped. "),
      "Your decision is recorded here and applied once a controller drives the run again (", el("code", null, `factory resume ${g.run_id}`), ").") : null;
    const node = el("aside", { class: "decide" }, card("Your call", el("div", null, stoppedNote, status, form), null, "call"));
    if (g.decided) showDecided(g.decided);
    else if (rec.recorded) showRecorded(rec.recorded);
    refresh();
    return { node, update(fresh) {
      // decided elsewhere (the CLI, another tab): the page stops offering the buttons as soon as the poll sees it
      if (fresh.decided) showDecided(fresh.decided);
      else if (fresh.gate && fresh.gate.recorded && !form.hidden) showRecorded(fresh.gate.recorded);
    } };
  }

  // ------------------------------------------------------------------ outcomes
  async function viewOutcomes() {
    setNav("outcomes");
    const nav = state.nav;
    const [outs, cfg] = [(await api("/api/outcomes")).outcomes, state.config || await api("/api/config")];
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Outcomes", "Saved briefs for new runs.",
      [el("a", { class: "btn primary", href: "#/outcomes/new" }, "New outcome")]));
    if (!outs.length) {
      frag.append(card(null, el("div", { class: "empty" }, el("h3", null, "No outcomes saved yet"), el("div", null, "Describe the change you want to make."),
        el("a", { class: "btn primary", href: "#/outcomes/new" }, "New outcome"))));
    } else {
      const list = el("ul", { class: "list" });
      for (const o of outs) {
        list.append(el("li", null, el("div", { class: "main" }, el("div", { class: "title" }, el("a", { href: `#/outcomes/new?outcome=${o.outcome_id}` }, o.title || o.outcome_id)),
          el("div", { class: "desc" }, `${o.owner ? "Owner " + o.owner + ". " : ""}${o.runs.length ? plural(o.runs.length, "run") + ": " + o.runs.map(r => r.slice(4, 12)).join(", ") : "No runs yet."}`)),
          el("a", { class: "btn primary", href: `#/start?outcome=${o.outcome_id}` }, "Start a run")));
      }
      frag.append(card("Saved outcomes", list, plural(outs.length, "outcome")));
    }
    if (!paint(frag, nav)) return;
    stopPoll();
  }

  async function viewOutcomeForm(params) {
    setNav("outcomes");
    const nav = state.nav;
    const t = (await api("/api/outcomes/template")).outcome;
    const cfg = state.config || await api("/api/config");
    let o = JSON.parse(JSON.stringify(t)); delete o._example;
    if (params.get("outcome")) { try { o = (await api(`/api/outcomes/${params.get("outcome")}`)).outcome; } catch (e) { /* fall through */ } }
    let savedId = params.get("outcome") && o.outcome_id ? o.outcome_id : null;
    const app = params.get("app") ? await api(appPath(params.get("app"))) : null;
    const appQuery = app ? `&app=${encodeURIComponent(app.app_id)}` : "";
    if (app && !savedId) {
      o.target_repositories = [{url:app.repository_url, default_branch:app.default_branch || "main", paths_in_scope:[]}];
      o.constraints = {...(o.constraints || {}), budget_usd:60};
    }
    // A new outcome starts with the owner of the most recent one: the person deciding the gates rarely changes.
    let ownerFrom = null;
    if (!savedId && !((o.owner || {}).name || (o.owner || {}).handle)) {
      try {
        const last = ((await api("/api/outcomes?runs=false")).outcomes || []).find(x => x.owner && x.owner_name);
        if (last) { o.owner = { ...(o.owner || {}), name: last.owner_name, handle: last.owner }; ownerFrom = last.title || last.outcome_id; }
      } catch (e) { /* the owner fields stay empty */ }
    }

    const frag = document.createDocumentFragment();
    frag.append(pageHead(savedId ? o.title : "New outcome", "Describe the change and how you will know it worked.", null,
      [el("a", { href: "#/outcomes" }, "Outcomes"), " / ", savedId ? "Edit" : "New"]));

    const form = el("form", { class: "form stack", onsubmit: e => e.preventDefault(), novalidate: true });
    const f = {};
    let controlSeq = 0;
    const labelControl = (label, input) => { input.id = input.id || `fc-${++controlSeq}`;
      return el("div", { class: "field-label" }, el("label", { class: "lab", for: input.id }, label), input); };
    const field = (key, label, help, input) => { f[key] = input; input.id = `f-${key}`; return el("div", { class: "field" }, el("label", { class: "lab", for: input.id }, label), help ? el("p", { class: "help" }, help) : null, input); };
    const statement = el("textarea", { class: "big" }); statement.value = o.statement || "";
    form.append(card("The outcome", el("div", null,
      field("statement", "What should be true when this is done?", null, statement),
      field("title", "Short title", null, el("input", { type: "text", value: o.title || "" })))));

    const metricsBox = el("div");
    const metricRows = [];
    const addMetric = (m = {}) => {
      const row = { original: m, metric: el("input", { type: "text", value: m.metric || "", placeholder: "What improves?" }),
        baseline: el("input", { type: "text", value: m.baseline || "", placeholder: "Optional" }),
        target: el("input", { type: "text", value: m.target || "", placeholder: "What counts as success?" }),
        measurement: el("input", { type: "text", value: m.measurement || "", placeholder: "Where it is read from" }) };
      const node = el("div", { class: "metric" },
        el("div", { class: "grid2" }, labelControl("Metric", row.metric), labelControl("Target", row.target)),
        el("details", {open: !!(m.baseline || m.measurement)}, el("summary", null, "Measurement details (optional)"),
          el("div", { class: "grid2" }, labelControl("Baseline today", row.baseline), labelControl("How it is measured", row.measurement))),
        el("button", { class: "rm", type: "button", onclick: () => { node.remove(); metricRows.splice(metricRows.indexOf(row), 1); } }, "Remove this metric"));
      metricRows.push(row); metricsBox.append(node);
    };
    (o.success_metrics && o.success_metrics.length ? o.success_metrics : [{}]).forEach(addMetric);
    const addMetricBtn = el("button", { class: "add", type: "button", onclick: () => addMetric() }, "Add a metric");
    form.append(card("Success metrics", el("div", null, el("p", { class: "help" }, "Define at least one measurable target."), metricsBox, addMetricBtn)));

    const c = o.constraints || {};
    f.must_not = el("textarea"); f.must_not.value = (c.must_not || []).join("\n");
    f.compliance = el("textarea"); f.compliance.value = (c.compliance || []).join("\n");
    f.budget = el("input", { type: "number", min: "1", value: c.budget_usd ?? 150 });
    f.deadline = el("input", { type: "date", value: c.deadline || "" });
    form.append(el("details", { class: "card optional-fields", open: !!((c.must_not || []).length || (c.compliance || []).length || c.deadline) }, el("summary", null, "Constraints and default budget"), el("div", {class:"card-body"},
      el("div", { class: "grid2" }, labelControl("Must not (one per line)", f.must_not), labelControl("Compliance (one per line)", f.compliance)),
      el("div", { class: "grid2 mt-3" }, labelControl("Budget in USD for agent spend", f.budget), labelControl("Deadline", f.deadline)))));

    const repo = (o.target_repositories || [{}])[0] || {};
    f.repo = el("input", { type: "text", value: repo.url || "", placeholder: "https://github.com/you/app" });
    f.branch = el("input", { type: "text", value: repo.default_branch || "main" });
    f.paths = el("textarea"); f.paths.value = (repo.paths_in_scope || []).join("\n");
    f.create = el("input", { type: "checkbox", checked: !!repo.create });
    f.visibility = el("select", null, el("option", { value: "private", selected: repo.visibility !== "public" }, "private"), el("option", { value: "public", selected: repo.visibility === "public" }, "public"));
    const visibilityField = labelControl("Visibility when created", f.visibility);
    visibilityField.hidden = !f.create.checked;
    f.create.addEventListener("change", () => { visibilityField.hidden = !f.create.checked; });
    const repoFields = el("div", null, labelControl("HTTPS URL", f.repo),
      el("details", {open: !!(repo.create || (repo.paths_in_scope || []).length || (repo.default_branch && repo.default_branch !== "main"))}, el("summary", null, "Branch, scope and repository creation"),
        labelControl("Default branch", f.branch),
        el("div", { class: "grid2 mt-3" },
          el("label", { class: "pick" }, f.create, "Create this repository if it does not exist"), visibilityField),
        labelControl("Allowed paths (optional, one per line)", f.paths)));
    // From an onboarded app the repository is already known: say so, and keep the fields one click away.
    const fromApp = app && normRepo(repo.url) === normRepo(app.repository_url);
    form.append(card("Repository", fromApp
      ? el("div", null, el("p", { class: "mb-2" }, "From the app ", el("a", { href: appHref(app.app_id) }, app.name || app.app_id), ": ",
          el("span", { class: "mono" }, repoName(repo.url)), ` · ${repo.default_branch || "main"}`),
        el("details", null, el("summary", null, "Use a different repository or limit the paths"), repoFields))
      : el("div", null, el("p", { class: "help" }, "The repository this outcome applies to."), repoFields)));

    const ow = o.owner || {};
    f.oname = el("input", { type: "text", value: ow.name || "", autocomplete: "name" }); f.ohandle = el("input", { type: "text", value: ow.handle || "", placeholder: "your GitHub login" });
    const ownerFields = el("div", { class: "grid2" }, labelControl("Name", f.oname), labelControl("Code host handle", f.ohandle));
    form.append(card("Owner (who decides the gates)", ownerFrom
      ? el("div", null, el("p", { class: "mb-2" }, el("b", null, ow.name), ow.handle ? ` (${ow.handle})` : "", el("span", { class: "explain" }, `, as on “${ownerFrom}”.`)),
        el("details", null, el("summary", null, "Change the owner"), ownerFields))
      : ownerFields));

    // Validation. The server names each invalid field by JSON pointer; its message goes beside that control, the control
    // is marked invalid and described by it, and focus moves to the first one, unfolding any section it sits in.
    let errorSeq = 0;
    const clearError = ctrl => {
      const id = ctrl.dataset.errorId;
      if (!id) return;
      const node = document.getElementById(id); if (node) node.remove();
      ctrl.removeAttribute("aria-invalid");
      const rest = (ctrl.getAttribute("aria-describedby") || "").split(" ").filter(x => x && x !== id).join(" ");
      if (rest) ctrl.setAttribute("aria-describedby", rest); else ctrl.removeAttribute("aria-describedby");
      delete ctrl.dataset.errorId;
    };
    const markError = (ctrl, message) => {
      const prev = ctrl.dataset.errorId && document.getElementById(ctrl.dataset.errorId);
      if (prev) { prev.append(" ", message); return; }
      const id = `field-error-${++errorSeq}`;
      ctrl.setAttribute("aria-invalid", "true");
      ctrl.setAttribute("aria-describedby", [ctrl.getAttribute("aria-describedby"), id].filter(Boolean).join(" "));
      ctrl.dataset.errorId = id;
      ctrl.after(el("p", { class: "field-error", id }, message));
      ctrl.addEventListener("input", () => clearError(ctrl), { once: true });
      for (let d = ctrl.closest("details"); d; d = d.parentElement ? d.parentElement.closest("details") : null) d.open = true;
    };
    const sentRows = () => metricRows.filter(r => r.metric.value.trim() || r.target.value.trim());
    const controlFor = (pointer, rows) => {
      const p = pointer.split("/");
      if (p[0] === "success_metrics") {
        const row = p.length > 1 ? rows[Number(p[1])] : null;
        return row ? (row[p[2]] || row.metric) : (metricRows[0] || {}).metric || addMetricBtn;
      }
      return { title: f.title, statement: f.statement, owner: f.oname, "owner/name": f.oname, "owner/handle": f.ohandle,
        "target_repositories/0/url": f.repo, "target_repositories/0/default_branch": f.branch, "target_repositories/0/paths_in_scope": f.paths,
        "constraints/budget_usd": f.budget, "constraints/deadline": f.deadline, "constraints/must_not": f.must_not,
        "constraints/compliance": f.compliance }[pointer] || null;
    };
    function showErrors(r, rows) {
      form.querySelectorAll("[data-error-id]").forEach(clearError);
      const list = r.field_errors || (r.errors || []).map(message => ({ field: "", message }));
      // listed in the order the fields appear on the page, so the list reads like the form
      const items = list.map(e => ({ ...e, ctrl: e.field ? controlFor(e.field, rows) : null }))
        .sort((a, b) => !a.ctrl || !b.ctrl ? (a.ctrl ? -1 : b.ctrl ? 1 : 0) : a.ctrl === b.ctrl ? 0
          : a.ctrl.compareDocumentPosition(b.ctrl) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
      items.forEach(e => { if (e.ctrl && e.ctrl.tagName !== "BUTTON") markError(e.ctrl, e.message); });
      notice.replaceChildren(el("div", { class: "notice bad", role: "alert" },
        el("b", null, `Not saved yet: ${plural(items.length, "thing")} to fix.`),
        el("ul", { class: "error-list" }, items.map(e => el("li", null, e.ctrl
          ? el("button", { type: "button", class: "btn link", onclick: () => e.ctrl.focus() }, e.message) : e.message)))));
      const first = form.querySelector('[aria-invalid="true"]') || (items.find(e => e.ctrl) || {}).ctrl;
      if (first) { first.focus(); first.scrollIntoView({ block: "center" }); }
    }

    const notice = el("div");
    const lines = ta => ta.value.split("\n").map(s => s.trim()).filter(Boolean);
    const collect = rows => ({
      outcome_id: savedId || "", title: f.title.value.trim(), statement: f.statement.value.trim(),
      success_metrics: rows.map(r => ({ ...r.original, metric: r.metric.value.trim(), baseline: r.baseline.value.trim() || null, target: r.target.value.trim(), measurement: r.measurement.value.trim() })),
      constraints: { must_not: lines(f.must_not), compliance: lines(f.compliance), budget_usd: Number(f.budget.value) || undefined, deadline: f.deadline.value || undefined },
      context_links: o.context_links || [], clarifications: o.clarifications || [],
      target_repositories: [{ ...repo, url: f.repo.value.trim(), default_branch: f.branch.value.trim() || "main", paths_in_scope: lines(f.paths),
        create: f.create.checked || undefined, visibility: f.create.checked ? f.visibility.value : undefined }, ...(o.target_repositories || []).slice(1)],
      owner: { name: f.oname.value.trim(), handle: f.ohandle.value.trim(), ...(ow.role ? {role: ow.role} : {}) },
      created_at: o.created_at || undefined,
    });
    async function save() {
      notice.replaceChildren();
      saveBtn.disabled = startLink.disabled = true;
      try {
        const rows = sentRows();
        const r = await api("/api/outcomes", { body: { outcome: collect(rows) } });
        if (!r.ok) { showErrors(r, rows); return null; }
        form.querySelectorAll("[data-error-id]").forEach(clearError);
        savedId = r.outcome_id;
        notice.replaceChildren(el("div", { class: "notice ok", role: "status" }, el("b", null, "Saved. "), el("a", { href: `#/start?outcome=${savedId}${appQuery}` }, "Start a run from it.")));
        return r;
      } catch (e) {
        notice.replaceChildren(el("div", {class:"notice bad", role:"alert"}, `Could not save: ${e.message}`));
        return null;
      } finally {
        saveBtn.disabled = startLink.disabled = false;
      }
    }
    const saveBtn = el("button", { class: "btn primary", type: "button", onclick: save }, "Save");
    const startLink = el("button", { class: "btn primary", hidden: !(cfg.can_run_live || cfg.can_run_design), type: "button", onclick: async () => { const r = await save(); if (r) location.hash = `#/start?outcome=${savedId}${appQuery}`; } }, "Save and start a run");
    form.append(card("Save", el("div", null, notice, el("div", { class: "startbar" }, saveBtn, el("span", { class: "spacer" }), startLink))));
    frag.append(form);
    if (!paint(frag, nav)) return;
    stopPoll();
  }

  // ------------------------------------------------------------------ apps: learn once how to build, run and test an app, then runs use it
  const APP_STATUS = {
    verified: ["Verified", "The setup ran green in the hosted environment.", "ok"],   // records from before ready / ready_limited
    connected: ["Connected", "Added. Discovery has not run yet.", ""],
    discovering: ["Discovering", null, "crew"],
    needs_input: ["Needs your input", "Discovery stopped at questions only you can answer.", "gate"],
    setup_ready: ["Ready to verify", "Review what the agent found, then verify it.", "question"],
    verifying: ["Verifying", "The setup is running in a hosted sandbox with synthetic data.", "crew"],
    ready: ["Ready", "Verification passed for the capabilities listed as covered.", "ok"],
    ready_limited: ["Ready, with limits", "Verification passed, but some capabilities were not covered.", "warn"],
    failed: ["Failed", "The last step did not finish. The error is shown on the page.", "bad"],
    publishing: ["Publishing", "Opening the setup pull request.", "crew"],
  };
  const APP_BUSY = new Set(["discovering", "verifying", "publishing"]);
  const APP_STEPS = [
    ["Choose the app", "An existing repository or a new app."],
    ["Discover the setup", "An agent finds how to build, run and test it."],
    ["Answer questions", "Only if the agent needs something from you."],
    ["Verify", "The setup runs once in a hosted sandbox."],
    ["Evidence", "Check results from that run, with screenshots for browser apps."],
    ["Setup pull request", "Proposed to the repository; a person merges it."],
  ];
  const GITHUB_HTTPS = /^https:\/\/github\.com\/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/;
  const SECRET_NAME = /^[A-Z][A-Z0-9_]*$/;
  const appPath = id => `/api/apps/${encodeURIComponent(id)}`;
  const appHref = id => `#/apps/${encodeURIComponent(id)}`;
  const appKind = k => (k === "greenfield" ? "New app" : "Existing app");
  const repoName = url => String(url || "").replace(/^https:\/\/github\.com\//, "");
  const normRepo = url => String(url || "").trim().replace(/\.git$/, "").replace(/\/+$/, "").toLowerCase();
  function appStatusText(a) {
    const [label, text, cls] = APP_STATUS[a.status] || [words(a.status), "", ""];
    const discovering = a.kind === "greenfield" ? "An agent is proposing a scaffold and test setup from your description."
      : "An agent is reading the committed source and the existing test setup.";
    return { label, text: text || (APP_BUSY.has(a.status) ? discovering : ""), cls };
  }
  function appStatusTag(a) { const s = appStatusText(a); return el("span", { class: `tag ${s.cls}` }, s.label); }
  function appMoney(a) { return `${money(a.spent_usd || 0)} of ${money(a.budget_usd)}`; }
  function appLeft(a) { return money(a.remaining_usd ?? Math.max(0, (a.budget_usd || 0) - (a.spent_usd || 0))); }
  function appKey(a) { return JSON.stringify([a.status, a.phase, a.updated_at, a.plan_sha256, a.spent_usd, (a.sessions || []).length, (a.credentials || []).map(c => [c.name, c.status]), a.pr_url, a.error]); }
  function onboardingFacts() {
    return card("Before you start", el("ul", { class: "facts" },
      el("li", null, el("b", null, "Runs hosted. "), "Discovery and tests run in a hosted sandbox."),
      el("li", null, el("b", null, "Synthetic data only. "), "Use generated data and test credentials."),
      el("li", null, el("b", null, "Charged to the app's budget. "), "Agent sessions use this app's budget."),
      el("li", null, el("b", null, "People still decide. "), "Review the setup pull request before merging.")), null, "soft");
  }

  async function viewApps() {
    setNav("apps");
    const nav = state.nav;
    const apps = (await api("/api/apps")).apps || [];
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Apps", "Repositories and their verified test setups.",
      [el("a", { class: "btn primary", href: "#/apps/new" }, "Add an app")]));
    const cloudBox = el("div", { role: "status", "aria-live": "polite" }, el("p", { class: "explain m-0" }, "Reading the workspace's execution environments…"));
    const cloudCard = card("Elsewhere in the workspace", cloudBox, "execution environments this Console does not hold");
    // One row per app: where onboarding stands, the setup pull request, and its hosted environment once the workspace answers.
    const envCells = new Map();
    if (!apps.length) {
      frag.append(cloudCard, card(null, el("div", { class: "empty" }, crewFigure("codebase_researcher", "asleep", "l"), el("h3", null, "No apps on this Console yet"),
        el("div", null, "Add an existing GitHub repository, or describe a new app. An agent works out how to test it; you review before anything is verified or published."),
        el("a", { class: "btn primary", href: "#/apps/new" }, "Add an app"))));
    } else {
      const tbl = el("table", null, el("tr", null, el("th", null, "App"), el("th", null, "Where it is"), el("th", null, "Hosted environment"), el("th", null, "Spend"), el("th", null, "Last activity")));
      for (const a of apps) {
        const envCell = el("td", { class: "explain" }, a.environment_id ? el("span", { class: "mono" }, a.environment_id) : "None until verified");
        envCells.set(a.app_id, envCell);
        tbl.append(el("tr", { class: "link", onclick: () => { location.hash = appHref(a.app_id); } },
          el("td", null, el("div", { class: "title" }, el("a", { href: appHref(a.app_id) }, a.name || a.app_id), " · ", el("span", { class: "tag" }, appKind(a.kind))),
            el("div", { class: "explain mono" }, repoName(a.repository_url))),
          el("td", null, appStatusTag(a), a.phase ? el("div", { class: "explain" }, a.phase) : null),
          envCell, el("td", null, appMoney(a)), el("td", { class: "explain" }, ago(a.updated_at))));
      }
      frag.append(card("On this Console", tbl, plural(apps.length, "app")), cloudCard);
    }

    if (!paint(frag, nav)) return;
    const showLocal = local => {
      for (const [id, cell] of envCells) {
        const a = apps.find(x => x.app_id === id);
        cell.replaceChildren(a && a.environment_id ? el("span", { class: "mono" }, a.environment_id) : "None until verified");
      }
      for (const x of local) {
        const cell = envCells.get(x.local_app_id), e = x.environment;
        if (!cell) continue;
        cell.replaceChildren(/^https:\/\//.test(e.console_url || "") ? el("a", { class: "mono", href: e.console_url, target: "_blank", rel: "noopener", onclick: ev => ev.stopPropagation() }, e.name) : el("span", { class: "mono" }, e.name),
          el("div", null, e.verified_at ? `Verified ${ago(e.verified_at)}` : `Created ${ago(e.created_at) || "just now"}`, e.in_use ? " · session running" : ""));
      }
    };
    api("/api/apps/environments").then(v => cloudEnvironments(cloudBox, v, showLocal))
      .catch(e => cloudBox.replaceChildren(el("div", { class: "notice bad" }, `The workspace could not be read: ${e.message}`)));
    const listKey = list => JSON.stringify(list.map(a => [a.app_id, a.status, a.phase, a.updated_at, a.spent_usd, a.pr_state]));
    state.lastKey.apps = listKey(apps);
    startPoll(refreshWhenChanged("apps", async () => listKey((await api("/api/apps")).apps || []), viewApps), apps.some(a => APP_BUSY.has(a.status)) ? 4000 : 15000);
  }

  // Two clicks for anything that cannot be undone from here: the confirming button appears in place of the first.
  function twoStep(label, confirmLabel, run, cls) {
    const line = el("span", { class: "explain", role: "status", "aria-live": "polite" });
    const first = el("button", { class: `btn ${cls || "sm"}`, type: "button" }, label);
    const yes = el("button", { class: `btn danger ${cls || "sm"}`, type: "button" }, confirmLabel);
    const no = el("button", { class: `btn ${cls || "sm"}`, type: "button" }, "Cancel");
    const confirmBox = el("span", { class: "row-gap", hidden: true }, yes, no);
    first.onclick = () => { first.hidden = true; confirmBox.hidden = false; yes.disabled = true; setTimeout(() => { yes.disabled = false; yes.focus(); }, 600); };
    no.onclick = () => { confirmBox.hidden = true; first.hidden = false; first.focus(); };
    yes.onclick = async () => {
      yes.disabled = no.disabled = true; line.textContent = "Working…";
      try { await run(); } catch (e) { line.textContent = e.message; yes.disabled = no.disabled = false; }
    };
    return el("span", { class: "row-gap" }, first, confirmBox, line);
  }

  // The apps' execution environments as the shared Anthropic workspace holds them: one per onboarded app, whichever
  // Console or checkout onboarded it, plus what earlier attempts left behind.
  // Apps this Console holds are shown on their own rows above (showLocal); this list is the rest of the workspace, plus any
  // environment of a local app that could be archived.
  function cloudEnvironments(box, v, showLocal) {
    const reload = async refresh => cloudEnvironments(box, await api(`/api/apps/environments${refresh ? "?refresh=true" : ""}`), showLocal);
    if (!v.configured) { box.replaceChildren(el("p", { class: "explain m-0" }, v.error)); return; }
    const again = el("button", { class: "btn sm", type: "button" }, "Check again");
    again.onclick = async () => { again.disabled = true; try { await reload(true); } catch (e) { again.disabled = false; again.textContent = `Check again (${e.message})`; } };
    const parts = [el("p", { class: "explain mb-3" },
      "Each onboarded app runs its hosted checks in one execution environment in the Anthropic workspace (Claude Console, Environments). Every Console and checkout using this workspace sees the same apps. ",
      v.checked_at ? `Checked ${ago(v.checked_at) || "just now"}. ` : "", again)];
    if (v.error) parts.push(el("div", { class: "notice bad" }, v.error));
    const all = v.apps || [];
    if (showLocal) showLocal(all.filter(a => a.local_app_id));
    const apps = all.filter(a => !a.local_app_id || (!a.environment.current && !a.environment.in_use));
    if (apps.length) {
      const tbl = el("table", null, el("tr", null, el("th", null, "App"), el("th", null, "Execution environment"), el("th", null, "State"), el("th", null, "")));
      for (const a of apps) {
        const e = a.environment;
        const here = a.local_app_id;
        const actions = [];
        if (!here && GITHUB_HTTPS.test(a.repository_url || "")) {
          actions.push(el("button", { class: "btn sm", type: "button", onclick: () => { state.appPrefill = { url: a.repository_url, name: a.name }; location.hash = "#/apps/new"; } }, "Onboard on this Console"));
        }
        if (!e.current && !e.in_use) {
          actions.push(twoStep("Archive…", "Confirm: archive", async () => {
            const r = await api(`/api/apps/environments/${encodeURIComponent(e.id)}/archive`, { body: {} });
            cloudEnvironments(box, r.environments, showLocal);
          }));
        }
        tbl.append(el("tr", null,
          el("td", null, el("div", { class: "title" }, here ? el("a", { href: appHref(here) }, a.name) : el("b", null, a.name)),
            el("div", { class: "explain mono" }, a.repository_url ? repoName(a.repository_url) : "repository not recorded")),
          el("td", null, /^https:\/\//.test(e.console_url || "") ? el("a", { class: "mono", href: e.console_url, target: "_blank", rel: "noopener" }, e.name) : el("span", { class: "mono" }, e.name),
            el("div", { class: "explain" }, `Created ${ago(e.created_at) || "just now"}`, a.leftover_count ? ` · ${plural(a.leftover_count, "older one")} below` : "")),
          el("td", null, el("span", { class: `tag ${here ? "ok" : ""}` }, here ? "On this Console" : "Onboarded elsewhere"),
            e.verified_at ? [" · ", el("span", { class: "tag ok" }, `Verified ${ago(e.verified_at)}`)] : null,
            e.in_use ? [" · ", el("span", { class: "tag crew" }, "Session running")] : null),
          el("td", null, el("div", { class: "btn-col fit" }, actions))));
      }
      parts.push(tbl);
      if (apps.some(a => !a.local_app_id)) {
        parts.push(el("p", { class: "explain m-0 mt-3 fs-sm" },
          "Apps onboarded elsewhere keep their plan, evidence and test credentials on the Console that onboarded them. To request changes from here, onboard the app on this Console: an unchanged recipe reuses its environment, so the app still has one."));
      }
    } else if (!v.error) {
      parts.push(el("p", { class: "explain m-0" }, all.length ? "Every app with an execution environment in this workspace is listed above."
        : "No app has an execution environment in this workspace yet. Verifying an app's setup creates one."));
    }
    if (v.discovery) {
      parts.push(el("p", { class: "explain m-0 mt-3 fs-sm" }, "Discovery reads source in one shared, network-less environment: ",
        el("a", { class: "mono", href: v.discovery.console_url, target: "_blank", rel: "noopener" }, v.discovery.name), "."));
    }
    const retire = v.retire || [];
    if (retire.length) {
      const list = el("ul", { class: "facts mt-2 mb-2" }, retire.map(e => el("li", null, el("span", { class: "mono" }, e.name), " · ",
        e.reason === "duplicate" ? "a duplicate of " : "replaced by ", el("span", { class: "mono" }, e.replaced_by_name),
        e.purpose === "discovery" ? " (discovery)" : e.app_name ? ` (${e.app_name})` : "")));
      parts.push(el("div", { class: "notice warn mt-3" },
        el("b", null, `${plural(retire.length, "environment")} left over from earlier attempts. `),
        "An app needs one execution environment. These hold older recipes or copies of the same one, and no session is running in them. Archived environments stay listed under Archived in the Claude Console; no new session starts in them. Repositories are not touched.",
        list, twoStep(`Archive ${plural(retire.length, "leftover")}`, `Confirm: archive ${retire.length}`, async () => {
          const r = await api("/api/apps/environments/cleanup", { body: { environment_ids: retire.map(e => e.id) } });
          cloudEnvironments(box, r.environments, showLocal);
          if ((r.failed || []).length) box.prepend(el("div", { class: "notice bad" }, `${plural(r.failed.length, "environment")} could not be archived; the list shows what remains.`));
        })));
    }
    box.replaceChildren(...parts);
  }

  // The authenticated GitHub repository list, searchable and paged, with a typed URL as the alternative.
  function repositoryPicker(onPick, onLoaded) {
    let repositories = [], nextPage = 1;
    const search = el("input", { type: "search", id: "app-repo-search", placeholder: "Filter by owner or repository name", autocomplete: "off" });
    const select = el("select", { id: "app-repo-select" }, el("option", { value: "" }, "Choose a repository"));
    const status = el("p", { class: "explain m-0", role: "status", "aria-live": "polite" }, "Loading GitHub repositories…");
    const details = el("p", { class: "explain m-0" });
    const typed = el("input", { type: "url", id: "app-repo-url", placeholder: "https://github.com/owner/app", autocomplete: "off", spellcheck: "false" });
    const manual = el("details", null, el("summary", null, "Enter a repository URL instead"),
      el("label", { class: "lab", for: "app-repo-url" }, "Repository URL"), el("p", { class: "help" }, "HTTPS form: https://github.com/owner/name"), typed);
    const more = el("button", { class: "btn sm", type: "button" }, "Load more repositories");
    const render = () => {
      const q = search.value.trim().toLowerCase();
      const keep = select.value;
      const rows = repositories.filter(r => r.full_name.toLowerCase().includes(q) || r.url === keep);
      select.replaceChildren(el("option", { value: "" }, "Choose a repository"), ...rows.map(r => el("option", { value: r.url, disabled: !r.selectable, selected: r.url === keep },
        `${r.full_name} · ${r.private ? "private" : "public"}${r.unavailable_reason ? " · " + r.unavailable_reason : ""}`)));
      status.textContent = !repositories.length ? "No repositories are visible to this GitHub connection. Enter a URL instead."
        : !rows.length ? "No matching repositories in the loaded results." : `${rows.length} shown of ${repositories.length} loaded.${nextPage ? " Load more to include older repositories." : ""}`;
    };
    const load = async () => {
      more.disabled = true; status.textContent = "Loading GitHub repositories…";
      try {
        const result = await api(`/api/repositories?page=${nextPage || 1}`);
        const byUrl = new Map(repositories.map(r => [r.url, r]));
        (result.repositories || []).forEach(r => byUrl.set(r.url, r));
        repositories = [...byUrl.values()]; nextPage = result.next_page || null;
        more.hidden = !nextPage; more.textContent = "Load more repositories";
        render();
        if (onLoaded) onLoaded();
      } catch (e) {
        status.textContent = `Could not load repositories: ${e.message}`;
        more.hidden = false; more.textContent = "Retry loading repositories";
        manual.open = true;
      } finally { more.disabled = false; }
    };
    more.onclick = load;
    search.oninput = render;
    select.onchange = () => {
      const r = repositories.find(x => x.url === select.value) || null;
      if (r) typed.value = "";
      details.textContent = r ? `Default branch: ${r.default_branch}. ${r.private ? "Private" : "Public"} repository.` : "";
      onPick(r ? { url: r.url, name: r.full_name.split("/").pop() } : null);
    };
    typed.oninput = () => {
      select.value = ""; details.textContent = "";
      onPick(typed.value.trim() ? { url: typed.value.trim(), name: repoName(typed.value.trim()).split("/").pop() } : null);
    };
    const node = el("div", { class: "stack" },
      el("label", { class: "lab", for: "app-repo-search" }, "Find a repository"), search,
      el("label", { class: "lab", for: "app-repo-select" }, "GitHub repository"), select, details, status, more, manual);
    const prefill = url => { typed.value = url; manual.open = true; typed.oninput(); };
    // where this connection can create a repository: its own account (a personal repository it administers) and the
    // organisations it belongs to; never another person's account it only collaborates on
    const owners = () => [...new Set(repositories.filter(r => r.owner_type === "Organization" ? r.selectable : r.owner_type === "User" && r.admin)
      .map(r => r.full_name.split("/")[0]))];
    return { node, load, prefill, owners };
  }

  async function viewAppNew() {
    setNav("apps");
    const nav = state.nav;
    stopPoll();
    const cfg = state.config || await api("/api/config");
    state.config = cfg;
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Add an app", "Choose the app, say what it is for, and set a budget. An agent then works out how to build and test it. You review what it finds before anything is verified or published.",
      null, [el("a", { href: "#/apps" }, "Apps"), " / ", "Add"]));

    // 1. which app
    let picked = null;
    const kindExisting = el("input", { type: "radio", name: "app-kind", value: "existing", checked: true });
    const kindNew = el("input", { type: "radio", name: "app-kind", value: "greenfield" });
    const existingBox = el("label", { class: "choice on" }, kindExisting, el("span", { class: "txt" }, el("b", null, "An app that already exists"),
      el("span", null, "A GitHub repository with code. The agent reads its committed source and existing tests.")));
    const newBox = el("label", { class: "choice" }, kindNew, el("span", { class: "txt" }, el("b", null, "A new app"),
      el("span", null, "Nothing exists yet. The agent proposes a scaffold and test setup. The private repository is created only when you publish.")));
    const name = el("input", { type: "text", id: "app-name", autocomplete: "off", maxlength: "80" });
    let nameTouched = false;
    name.oninput = () => { nameTouched = true; sync(); };
    const picker = repositoryPicker(r => { picked = r; if (!nameTouched) name.value = r ? r.name : ""; sync(); }, () => refreshOwners());
    // A new app's repository: an account this connection can create under, and a name that follows the app's name until
    // edited. A full URL stays available for an owner the loaded repositories do not show.
    const newOwner = el("select", { id: "app-new-owner" });
    const newRepo = el("input", { type: "text", id: "app-new-repo", autocomplete: "off", spellcheck: "false", maxlength: "100" });
    let newRepoTouched = false;
    newRepo.oninput = () => { newRepoTouched = true; sync(); };
    const slug = t => t.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 100);
    const newUrl = el("input", { type: "url", id: "app-new-url", placeholder: "https://github.com/you/new-app", autocomplete: "off", spellcheck: "false" });
    const newManual = el("details", null, el("summary", null, "Enter a full repository URL instead"),
      el("label", { class: "lab", for: "app-new-url" }, "Repository URL"), el("p", { class: "help" }, "HTTPS form: https://github.com/owner/name."), newUrl);
    const ownerRow = el("div", { class: "grid2 new-repo", hidden: true },
      el("div", null, el("label", { class: "lab", for: "app-new-owner" }, "Account"), newOwner),
      el("div", null, el("label", { class: "lab", for: "app-new-repo" }, "Repository name"), newRepo));
    const newPanel = el("div", { class: "stack", hidden: true },
      el("p", { class: "help m-0" }, "Where the new private repository will live. Nothing is created now; it is created when you publish the setup, after you review it."),
      ownerRow, newManual);
    const refreshOwners = () => {
      const list = picker.owners(), keep = newOwner.value;
      newOwner.replaceChildren(...list.map(o => el("option", { value: o, selected: o === keep }, o)));
      ownerRow.hidden = !list.length;
      if (!list.length) newManual.open = true;
      sync();
    };
    const newRepoUrl = () => ((newManual.open || ownerRow.hidden) && newUrl.value.trim())
      || (!ownerRow.hidden && newOwner.value && newRepo.value.trim() ? `https://github.com/${newOwner.value}/${newRepo.value.trim()}` : "");
    newManual.addEventListener("toggle", () => sync());
    // apps already on this Console, to warn before a repository is onboarded a second time
    let known = [];
    const dupNote = el("div", { role: "status" });
    const description = el("textarea", { id: "app-description", maxlength: "600" });
    const descLabel = el("label", { class: "lab", for: "app-description" });
    const descHelp = el("p", { class: "help" });
    const budget = el("input", { type: "number", id: "app-budget", min: "1", max: "60", step: "1", value: "10", inputmode: "decimal" });
    const revision = el("input", { type: "text", id: "app-revision", placeholder: "main, a feature branch, or a commit SHA", autocomplete: "off", spellcheck: "false", maxlength: "180" });
    const advanced = el("details", { class: "tech" }, el("summary", null, "Advanced"),
      el("div", { class: "field" }, el("label", { class: "lab", for: "app-revision" }, "Source branch or commit"),
        el("p", { class: "help" }, "Leave empty to use the repository's default branch. Name a reviewed branch or a commit to onboard code that is not merged yet."), revision));
    const existingPanel = el("div", { class: "stack" }, picker.node, advanced);
    const syncKind = () => {
      const isNew = kindNew.checked;
      if (!nameTouched) name.value = isNew ? "" : (picked ? picked.name : "");    // a name filled from a repository is not a new app's
      existingBox.classList.toggle("on", !isNew); newBox.classList.toggle("on", isNew);
      existingPanel.hidden = isNew; newPanel.hidden = !isNew;
      descLabel.textContent = isNew ? "What the new app should do" : "What the app is for";
      descHelp.textContent = isNew ? "Two or three sentences. The agent proposes a scaffold from this; you review it before anything is created."
        : "One or two sentences. They help the agent pick the user journeys worth testing.";
      sync();
    };
    kindExisting.onchange = kindNew.onchange = syncKind;
    [newUrl, description, budget, revision, newOwner].forEach(i => i.addEventListener("input", () => sync()));
    newOwner.addEventListener("change", () => sync());

    const whichCard = card("1. Which app", el("div", { class: "stack" }, el("div", { class: "choices" }, existingBox, newBox), existingPanel, newPanel, dupNote));
    const aboutCard = card("2. About it", el("div", { class: "form" },
      el("div", { class: "field" }, el("label", { class: "lab", for: "app-name" }, "Name"), el("p", { class: "help" }, "How the app is shown in the Console."), name),
      el("div", { class: "field" }, descLabel, descHelp, description)));
    const budgetCard = card("3. Budget", el("div", { class: "form" },
      el("div", { class: "field" }, el("label", { class: "lab", for: "app-budget" }, "Onboarding budget (USD)"),
        el("p", { class: "help" }, "Discovery and verification are agent sessions charged to this budget, which can be at most $60. The default is $10; change it here before you start. The app page lists every session and what it cost."), budget)));

    // what happens on click
    const summary = el("ul", { class: "summary-list" });
    const notice = el("div", { role: "alert" });
    const body = () => ({ kind: kindNew.checked ? "greenfield" : "existing", name: name.value.trim(),
      repository_url: (kindNew.checked ? newRepoUrl() : (picked ? picked.url : "")).trim().replace(/\/+$/, "").replace(/\.git$/, ""),
      description: description.value.trim(), budget_usd: Number(budget.value), ...(kindNew.checked || !revision.value.trim() ? {} : { revision: revision.value.trim() }) });
    const problems = b => [
      !b.name ? "Give the app a name." : null,
      !b.repository_url ? (b.kind === "greenfield" ? "Choose the account and a name for the new repository." : "Choose a repository or enter its URL.") : null,
      b.kind === "greenfield" && b.repository_url && known.some(a => normRepo(a.repository_url) === normRepo(b.repository_url)) ? "That repository is already an app here; choose another name." : null,
      b.repository_url && !GITHUB_HTTPS.test(b.repository_url) ? "The repository URL must look like https://github.com/owner/name." : null,
      !b.description ? (b.kind === "greenfield" ? "Describe what the new app should do." : "Say in a sentence what the app is for.") : null,
      !(b.budget_usd > 0 && b.budget_usd <= 60) ? "Set a budget above $0 and at most $60." : null,
      b.revision && !/^[A-Za-z0-9_.\-/]{1,180}$/.test(b.revision) ? "The source branch or commit may use only letters, digits, and . _ - /" : null,
    ].filter(Boolean);
    function sync() {
      if (kindNew.checked && !newRepoTouched) newRepo.value = slug(name.value);
      const b = body();
      const dup = b.repository_url ? known.filter(a => normRepo(a.repository_url) === normRepo(b.repository_url)) : [];
      dupNote.replaceChildren(...(dup.length ? [el("div", { class: "notice warn m-0" },
        el("b", null, b.kind === "greenfield" ? "That repository is already an app here: " : "Already onboarded here as "),
        ...dup.flatMap((a, i) => [i ? ", " : "", el("a", { href: appHref(a.app_id) }, a.name || a.app_id), ` (${appStatusText(a).label.toLowerCase()})`]), ". ",
        b.kind === "greenfield" ? "Choose another name for the new repository." : "Open it to verify again or request a change. A second app for the same repository makes every change run name which app it means.")] : []));
      if (!addBtn.disabled) addBtn.textContent = dup.length && b.kind !== "greenfield" ? "Add a second app for this repository" : "Add the app and start discovery";
      summary.replaceChildren(
        el("li", null, el("b", null, b.name || "An unnamed app"), ` · ${appKind(b.kind).toLowerCase()}`),
        el("li", null, b.repository_url ? el("span", { class: "mono" }, repoName(b.repository_url)) : "No repository chosen yet", b.revision ? el("span", { class: "mono" }, ` at ${b.revision}`) : null),
        el("li", null, `Discovery starts at once and is charged to the ${b.budget_usd > 0 ? money(b.budget_usd) : "$0"} budget`),
        el("li", null, "Verification and publishing wait for your click"));
    }
    const addBtn = el("button", { class: "btn primary lg", type: "button" }, "Add the app and start discovery");
    addBtn.onclick = async () => {
      notice.replaceChildren();
      const b = body(), bad = problems(b);
      if (bad.length) { notice.append(el("div", { class: "notice bad" }, el("b", null, "Not added yet. "), ...bad.map(p => el("div", null, p)))); return; }
      addBtn.disabled = true; addBtn.textContent = "Adding the app…";
      let app;
      try { app = await api("/api/apps", { body: b }); } catch (e) {
        notice.append(el("div", { class: "notice bad" }, el("b", null, "Not added. "), e.message));
        addBtn.disabled = false; sync(); return;
      }
      addBtn.textContent = "Starting discovery…";
      try { await api(`${appPath(app.app_id)}/discover`, { body: {} }); } catch (e) {
        state.flash = { appId: app.app_id, text: `The app was added, but discovery did not start: ${e.message}` };
      }
      location.hash = appHref(app.app_id);
    };
    const aside = el("aside", null,
      card("When you click", el("div", { class: "stack" }, summary, addBtn,
        el("p", { class: "explain m-0 fs-sm" }, "This starts a charged agent session. It reads or proposes code only; it changes nothing in the repository."), notice)),
      onboardingFacts());
    frag.append(el("div", { class: "start" }, el("div", { class: "start-main" }, whichCard, aboutCard, budgetCard), aside));
    if (!paint(frag, nav)) return;
    // From "Onboard on this Console" on an app found in the workspace: its repository and name, nothing else.
    const prefill = state.appPrefill;
    state.appPrefill = null;
    if (prefill) {
      picker.prefill(prefill.url);
      if (prefill.name) { name.value = prefill.name; nameTouched = true; }
    }
    syncKind();
    picker.load().then(refreshOwners);     // also refreshed after each "Load more"
    api("/api/apps").then(v => { known = v.apps || []; sync(); }).catch(() => { /* no duplicate warning without the list */ });
  }

  // ---- one app: the steps so far, the agent's status, and the next thing a person can do
  function appStepper(a) {
    const s = a.status, plan = a.plan || null;
    let idx = { connected: 1, discovering: 1, needs_input: 2, setup_ready: 3, verifying: 3, ready: 5, ready_limited: 5, publishing: 5 }[s] ?? 1;
    // the last step is done when the setup pull request is merged; while it is open, it is the current step
    if (a.pr_url && s !== "publishing") idx = a.pr_state === "merged" ? APP_STEPS.length : APP_STEPS.length - 1;
    const failed = s === "failed", failedAt = a.verification ? 3 : plan ? 2 : 1;
    const t = el("div", { class: "stepper app-steps", role: "list", "aria-label": "Onboarding steps" });
    APP_STEPS.forEach(([label, desc], i) => {
      const done = failed ? i < failedAt : i < idx;
      const current = !failed && i === idx;
      const d = i === 2 && plan && !(plan.questions || []).length && done ? "None were needed." : desc;
      t.append(el("div", { class: `step${done ? " done" : current ? " current" : ""}`, role: "listitem", "aria-current": current ? "step" : null },
        el("span", { class: "n" }, i + 1), el("span", { class: "lbl" }, label), el("span", { class: "d" }, d)));
    });
    if (failed) t.append(el("div", { class: "step terminal", role: "listitem" }, el("span", { class: "lbl" }, "Failed")));
    return t;
  }
  function appAgentRole(a) { return a.status === "verifying" ? "reviewer" : a.status === "publishing" ? "integrator" : "app_onboarding"; }
  function appAgentState(a) {
    if (APP_BUSY.has(a.status)) return "working";
    if (a.status === "ready" || a.status === "ready_limited") return "done";
    if (a.status === "failed" || a.status === "needs_input") return "blocked";
    return a.status === "connected" ? "asleep" : "still";
  }
  function appStatusBody(a) {
    const s = appStatusText(a);
    const consoleUrl = sid => state.config && state.config.console_session_url ? state.config.console_session_url.replace("SESSION_ID", sid) : null;
    const sessions = el("ul", { class: "sessions" });
    for (const x of a.sessions || []) {
      const link = x.console_url || consoleUrl(x.session_id);
      sessions.append(el("li", null, el("span", null, words(x.stage) || "Session", " · ",
        link && /^https:\/\//.test(link) ? el("a", { href: link, target: "_blank", rel: "noopener" }, "open in the Anthropic Console") : el("span", { class: "mono" }, String(x.session_id || "").slice(0, 18))),
        el("span", null, x.cost_usd == null ? (x.accounted ? "Usage unavailable" : "Usage pending") : money(x.cost_usd))));
    }
    return el("div", { class: "app-status" },
      el("div", { class: "head" }, crewFigure(appAgentRole(a), appAgentState(a), "s"),
        el("div", null, el("div", { class: "label" }, s.label), el("div", { class: "phase" }, a.phase || s.text))),
      a.phase && a.phase !== s.text ? el("p", { class: "explain m-0 mt-2" }, s.text) : null,
      el("p", { class: "explain m-0 mt-2 fs-sm" }, `Updated ${ago(a.updated_at) || "just now"}.`),
      el("dl", { class: "kv app-money" }, el("dt", null, "Budget"), el("dd", null, money(a.budget_usd)), el("dt", null, "Spent"), el("dd", null, money(a.spent_usd || 0)),
        el("dt", null, "Available"), el("dd", null, appLeft(a))),
      a.reserved_usd ? el("p", { class: "explain" }, `${money(a.reserved_usd)} is reserved for the current session.`) : null,
      a.cost_warning ? el("p", { class: "notice warn" }, a.cost_warning) : null,
      a.cleanup_warning ? el("p", { class: "notice warn" }, a.cleanup_warning) : null,
      (a.sessions || []).length ? el("div", null, el("h3", { class: "mini-h" }, "Charged sessions"), sessions)
        : el("p", { class: "explain m-0 mt-2 fs-sm" }, "No agent session has run yet."));
  }
  function capabilityList(plan) {
    const ul = el("ul", { class: "caps" });
    for (const c of plan.capabilities || []) {
      ul.append(el("li", null, el("span", { class: `tag ${c.available ? "ok" : "warn"}` }, c.available ? "Testable" : "Not testable here"),
        el("span", { class: "cap" }, c.label || c.id), c.limitation ? el("span", { class: "lim" }, c.limitation) : null));
    }
    return ul;
  }
  // a non-browser app has no screenshots: each declared functional check and what the recipe observed is its evidence
  function checkList(v) {
    const ul = el("ul", { class: "caps" });
    for (const c of v.checks || []) {
      const tone = c.status === "passed" ? "ok" : c.status === "failed" || c.status === "invalid" ? "bad" : "warn";
      ul.append(el("li", null, el("span", { class: `tag ${tone}` }, words(c.status)),
        el("span", { class: "cap" }, c.description || c.id),
        el("span", { class: "lim" }, c.observation ? `Observed: ${c.observation}` : "No observation was reported for this check.")));
    }
    return ul;
  }
  function readiness(v) {
    const stalled = (v.services || []).filter(s => s.status !== "passed");
    return stalled.length ? el("p", { class: "explain" }, `Not ready: ${stalled.map(s => `${s.service} (${words(s.reason || s.status)})`).join(", ")}.`) : null;
  }
  // only what the reviewed test recipe declares (test_environment.schema.json: name, required, allowed_hosts, injection, description)
  function declaredSecrets(plan) {
    const list = plan && plan.profile && Array.isArray(plan.profile.secrets) ? plan.profile.secrets : [];
    return list.filter(x => x && typeof x.name === "string" && SECRET_NAME.test(x.name))
      .map(x => ({ name: x.name, required: x.required === true, hosts: Array.isArray(x.allowed_hosts) ? x.allowed_hosts.map(String) : [],
        injection: x.injection ? String(x.injection) : "", purpose: x.description ? String(x.description) : "" }));
  }
  function evidenceGallery(a) {
    const grid = el("div", {class:"stack"}), videos = el("div", {class:"evidence-grid app-recordings"}), shots = el("div", {class:"evidence-grid"}), files = el("div", {class:"stack"});
    let shotCount = 0, fileCount = 0;
    const ok = e => typeof e.url === "string" && e.url.startsWith(`${appPath(a.app_id)}/evidence/`);
    const ext = e => String(e.name || "").toLowerCase().split(".").pop();
    // A recording's first frame is often blank: show a screenshot from the same verification as its poster, preferring
    // one named for the end state, and title it by the journeys the recipe ran rather than by its file name.
    const stills = (a.evidence || []).filter(e => ok(e) && ["png", "jpg", "jpeg", "webp"].includes(ext(e)));
    const poster = stills.find(e => /final|result|after|end|done/i.test(e.name)) || stills[stills.length - 1];
    const v = a.verification || {}, journeys = ((a.plan || {}).journeys || []).map(j => j.title).filter(Boolean);
    for (const e of a.evidence || []) {
      const url = ok(e) ? e.url : null;
      const nm = String(e.name || (url || "").split("/").pop() || "evidence");
      if (!url) { grid.append(el("figure", null, el("figcaption", null, `${nm} (not a Console evidence link, not shown)`))); continue; }
      const x = ext(e);
      let media;
      if (["png", "jpg", "jpeg", "webp"].includes(x)) media = el("a", { href: url, target: "_blank", rel: "noopener" }, el("img", { src: url, alt: nm, loading: "lazy" }));
      else if (["mp4", "webm"].includes(x)) media = el("video", { src: url, controls: true, preload: "metadata", playsinline: true, poster: poster ? poster.url : null,
        "aria-label": `Recording: ${journeys.length ? journeys.join("; ") : nm}` });
      else if (x === "gif") media = el("details", null, el("summary", null, "Play the recording (GIF)"), el("img", { src: url, alt: `Recording: ${nm}`, loading: "lazy" }));
      else media = null;
      const recording = ["mp4", "webm", "gif"].includes(x);
      const figure = el("figure", null, media, el("figcaption", null,
        recording && journeys.length ? el("b", null, journeys.join(" · ")) : null,
        recording && v.status ? el("span", null, `${words(v.status)}${v.head_sha ? ` · snapshot ${String(v.head_sha).slice(0, 10)}` : ""} · `) : null,
        el("a", { href: url, target: "_blank", rel: "noopener" }, nm)));
      if (recording) videos.append(figure);
      else if (media) { shots.append(figure); shotCount++; }
      else { files.append(figure); fileCount++; }
    }
    if (videos.childElementCount) grid.append(videos);
    if (shotCount) grid.append(el("details", null, el("summary", null, `Screenshots (${shotCount})`), shots));
    if (fileCount) grid.append(el("details", null, el("summary", null, "Download verification records"), files));
    return grid;
  }

  async function viewApp(appId) {
    setNav("apps");
    const nav = state.nav;
    let app = await api(appPath(appId));
    const drafts = (state.appDrafts = state.appDrafts || {})[appId] || (state.appDrafts[appId] = {});
    const statusBox = el("div", { role: "status", "aria-live": "polite" });
    const updateBar = el("div", { class: "notice info", hidden: true });
    const body = el("div", { class: "start-main" });
    const headBox = el("div");
    let pending = null;
    let flash = state.flash && state.flash.appId === appId ? state.flash : null;
    if (flash) state.flash = null;

    const busy = () => APP_BUSY.has(app.status);
    const act = async (btn, line, call) => {
      btn.disabled = true; line.textContent = "Working…";
      try { const a = await call(); line.textContent = ""; show(a && a.app_id ? a : await api(appPath(appId))); }
      catch (e) { line.textContent = e.status === 409 ? `The agent is already working on this app; the page follows it. (${e.message})` : e.message; btn.disabled = false; if (e.status === 409) tick(); }
    };
    const actionLine = () => el("p", { class: "explain action-line", role: "status", "aria-live": "polite" });
    const charged = text => el("p", { class: "explain m-0 mt-2 fs-sm" }, text);

    function sections(a) {
      const out = [];
      const plan = a.plan || null;
      if (flash) { out.push(el("div", { class: "notice bad", role: "alert" }, flash.text)); flash = null; }
      if (a.error) out.push(el("div", { class: "notice bad", role: "alert" }, el("b", null, "The last step failed. "), a.error));

      if (a.recoverable) {
        const line = actionLine(), btn = el("button", {class:"btn call", type:"button"}, "Recover interrupted setup");
        btn.onclick = () => act(btn, line, () => api(`${appPath(appId)}/recover`, {body:{}}));
        out.push(card("Console restarted", el("div", null, el("p", null, "The previous controller stopped. Recovery interrupts its pending sessions before allowing a retry and retains their budget reservation."), btn, line)));
      }

      // discover: started automatically after adding; offered again when idle
      if (!plan || a.status === "connected" || a.status === "failed") {
        const line = actionLine();
        const btn = el("button", { class: "btn primary", type: "button", disabled: busy() }, plan ? "Discover again" : "Start discovery");
        btn.onclick = () => act(btn, line, () => api(`${appPath(appId)}/discover`, { body: {} }));
        out.push(card("Discover the setup", el("div", null,
          el("p", null, a.status === "discovering" ? appStatusText(a).text : a.kind === "greenfield"
            ? "An agent proposes a scaffold, how to run it, and how to test it, from your description."
            : "An agent reads the repository's committed source and existing test setup, and proposes how to build, run and test it."),
          busy() ? el("p", { class: "explain" }, "The agent is working. This page follows it; you can leave and come back.") : el("div", null, btn, charged(`Starts a charged agent session (${appLeft(a)} left in this app's budget).`)), line)));
      }
      if (!busy() && (plan || a.status === "failed" || a.feedback)) {
        const feedback = el("textarea", {id:"setup-feedback", placeholder:"Describe what should change. Never paste credentials here."});
        feedback.value = drafts.feedback ?? a.feedback ?? "";
        feedback.oninput = () => { drafts.feedback = feedback.value; };
        const line = actionLine(), btn = el("button", { class: "btn primary", type: "button" }, "Revise this setup");
        btn.onclick = () => {
          if (!feedback.value.trim()) { line.textContent = "Describe the change first."; return; }
          act(btn,line,async () => {
            await api(`${appPath(appId)}/feedback`, {body:{text:feedback.value.trim()}});
            delete drafts.feedback;
            return api(`${appPath(appId)}/discover`, {body:{}});
          });
        };
        out.push(card("Improve or repair the setup", el("details", {open:a.status === "failed" || a.status === "needs_input" && !!a.feedback},
          el("summary", null, "Tell the onboarding agent what to change"),
          el("label", {for:"setup-feedback"}, "Setup feedback"), feedback, btn,
          charged(`Uses the previous proposal and failure evidence. Starts a charged discovery session (${appLeft(a)} available).`), line)));
      }
      if (!plan) { out.push(sourceDetails(a)); return out; }

      // what the agent found
      const journeys = el("ul", { class: "plain-list" }, (plan.journeys || []).map(j => {
        const cap = (plan.capabilities || []).find(c => c.id === j.capability);
        return el("li", null, el("b", null, j.title), cap ? el("span", { class: "explain" }, ` · needs ${cap.label || cap.id}${cap.available ? "" : " (not testable here)"}`) : null);
      }));
      const files = el("div", { class: "files-list" }, (plan.files || []).map(f => el("details", null, el("summary", null, el("span", { class: "mono" }, f.path)), el("pre", null, f.content || ""))));
      const tech = el("details", { class: "tech" }, el("summary", null, "Technical details: the test recipe, the files it proposes, and the raw plan"),
        el("dl", { class: "kv mt-3" }, el("dt", null, "Plan fingerprint"), el("dd", { class: "mono" }, a.plan_sha256 || "out of date until discovery runs again"),
          el("dt", null, "Source commit inspected"), el("dd", { class: "mono" }, a.source_sha || "not known yet"),
          el("dt", null, "Test recipe file"), el("dd", { class: "mono" }, plan.profile_path || "none yet")),
        (plan.files || []).length ? el("div", null, el("h3", { class: "mini-h" }, `Proposed files (${(plan.files || []).length})`), files) : null,
        plan.profile ? el("details", null, el("summary", null, "Raw test recipe JSON"), el("pre", null, JSON.stringify(plan.profile, null, 2))) : null,
        el("details", null, el("summary", null, "Raw plan JSON"), el("pre", null, JSON.stringify(plan, null, 2))));
      const explanation = plan.summary || "No summary given.";
      const firstParagraph = explanation.split(/\n\s*\n/)[0];
      const excerpt = firstSentences(firstParagraph, 420);
      out.push(card("Discovery notes", el("div", { class: "stack" },
        !a.plan_sha256 ? el("div", { class: "notice info m-0" }, el("b", null, "This plan is out of date. "), "Your answers are saved. Verification and publishing wait until discovery runs again with them.") : null,
        a.verification ? el("p", {class:"explain"}, "Written before verification. The evidence above records what actually ran; these notes describe the proposed scope and its limits.") : null,
        el("p", { class: "lead" }, excerpt),
        explanation !== excerpt ? el("details", null, el("summary", null, "Read the agent’s full setup explanation"), el("p", {class:"reading"}, explanation)) : null,
        plan.app_kind ? el("p", { class: "explain" }, `Kind of app: ${words(plan.app_kind)}`) : null,
        (plan.journeys || []).length ? el("div", null, el("h3", { class: "mini-h" }, "User journeys it would test"), journeys) : null,
        (plan.capabilities || []).length ? el("div", null, el("h3", { class: "mini-h" }, "What a test run can exercise"), capabilityList(plan)) : null,
        (plan.limitations || []).length ? el("div", null, el("h3", { class: "mini-h" }, "Agent’s planning assumptions and limits"), a.verification ? el("p", { class: "explain" }, "These notes were written before testing. Use the verification result above for the current status.") : null, el("ul", { class: "plain-list" }, plan.limitations.map(x => el("li", null, x)))) : null,
        tech), a.plan_sha256 ? `plan ${a.plan_sha256.slice(0, 10)}` : null));

      // questions: drafts survive polling; answers are saved, then discovery resumes only on an explicit click
      const qs = plan.questions || [];
      if (qs.length) {
        const answers = a.answers || {};
        const fields = qs.map(q => {
          const ta = el("textarea", { id: `answer-${q.id}`, "aria-describedby": `answer-${q.id}-h` });
          ta.value = drafts[q.id] ?? answers[q.id] ?? "";
          ta.oninput = () => { drafts[q.id] = ta.value; };
          return { q, ta, node: el("div", { class: "qa" }, el("label", { for: `answer-${q.id}` }, q.prompt, q.required ? el("span", { class: "req" }, "required") : el("span", { class: "opt" }, "optional")),
            el("p", { class: "help", id: `answer-${q.id}-h` }, answers[q.id] ? "Saved. Edit and save again to change it." : "Not answered yet."), ta) };
        });
        const collect = () => Object.fromEntries(fields.map(f => [f.q.id, f.ta.value.trim()]).filter(([, v]) => v));
        const missing = () => fields.filter(f => f.q.required && !f.ta.value.trim()).map(f => f.q.prompt);
        const line = actionLine();
        const saveBtn = el("button", { class: "btn", type: "button", disabled: busy() }, "Save answers");
        const againBtn = el("button", { class: "btn primary", type: "button", disabled: busy() }, "Save and discover again");
        const save = async () => { const r = await api(`${appPath(appId)}/answers`, { body: { answers: collect() } }); fields.forEach(f => { delete drafts[f.q.id]; }); return r; };
        saveBtn.onclick = () => act(saveBtn, line, save);
        againBtn.onclick = () => {
          const m = missing();
          if (m.length) { line.textContent = `Answer the required ${m.length === 1 ? "question" : "questions"} first: ${m.join("; ")}`; return; }
          act(againBtn, line, async () => { await save(); return api(`${appPath(appId)}/discover`, { body: {} }); });
        };
        out.push(card("Questions for you", el("div", { class: "stack" },
          el("p", { class: "explain" }, "The agent could not find these in the repository. Saving answers marks the plan out of date; discovery runs again only when you click Save and discover again."),
          ...fields.map(f => f.node), el("div", { class: "startbar" }, saveBtn, againBtn), charged("Discovering again starts a charged agent session."), line), null, a.status === "needs_input" ? "call" : ""));
      }

      // test credentials: only names the profile declares; write-only
      const secrets = declaredSecrets(plan);
      if (secrets.length) {
        const have = new Map((a.credentials || []).map(c => [c.name, c.status]));
        const rows = secrets.map(sec => {
          const input = el("input", { type: "password", id: `secret-${sec.name}`, autocomplete: "new-password", spellcheck: "false" });
          const line = actionLine();
          const btn = el("button", { class: "btn sm", type: "button" }, have.get(sec.name) ? "Replace in the test vault" : "Store in the test vault");
          btn.onclick = () => {
            const value = input.value; input.value = "";
            if (!value) { line.textContent = "Paste the test key first."; return; }
            act(btn, line, () => api(`${appPath(appId)}/credentials`, { body: { name: sec.name, value } }));
          };
          const st = have.get(sec.name);
          return el("div", { class: "secret" },
            el("div", { class: "row" }, el("span", { class: "mono" }, sec.name), el("span", { class: `tag ${sec.required ? "gate" : ""}` }, sec.required ? "required" : "optional"),
              el("span", { class: `tag ${st === "configured" ? "ok" : "warn"}` }, st ? words(st) : "not stored")),
            el("p", { class: "explain m-0" }, sec.purpose ? `Unlocks: ${sec.purpose}` : "The recipe does not say what this unlocks."),
            el("p", { class: "explain m-0" }, sec.hosts.length ? `Sent only to ${sec.hosts.join(", ")}${sec.injection ? `, in the request ${sec.injection.replace(/-/g, " ")}` : ""}.` : "The recipe declares no host for this key."),
            el("label", { class: "lab", for: `secret-${sec.name}` }, `Test value for ${sec.name}`),
            el("div", { class: "row" }, input, btn), line);
        });
        out.push(card("Test credentials", el("div", { class: "stack" },
          el("p", { class: "explain" }, "Use test-only keys, never production ones. Each value goes once to the hosted test vault and is never shown again; this page keeps only the name and whether it is stored."), ...rows)));
      }

      // verify the exact plan shown above
      if (["setup_ready", "verifying", "ready", "ready_limited", "failed"].includes(a.status) && a.plan_sha256) {
        const line = actionLine();
        const sha = a.plan_sha256;
        const unanswered = qs.filter(q => q.required && !(a.answers || {})[q.id]).length;
        const configured = new Set((a.credentials || []).filter(c => c.status === "configured").map(c => c.name));
        const keysMissing = secrets.filter(x => x.required && !configured.has(x.name)).map(x => x.name);
        const blockers = [!plan.profile ? "There is no test recipe yet; answer the questions and discover again." : null,
          unanswered ? "Answer the required questions and discover again." : null,
          keysMissing.length ? `Store the required test ${keysMissing.length === 1 ? "key" : "keys"} first: ${keysMissing.join(", ")}.` : null].filter(Boolean);
        const btn = el("button", { class: "btn call", type: "button", disabled: busy() || blockers.length > 0 }, a.verification ? "Verify again" : "Verify this setup");
        btn.onclick = () => act(btn, line, () => api(`${appPath(appId)}/verify`, { body: { plan_sha256: sha } }));
        out.push(card("Verify the setup", el("div", null,
          el("p", null, `Runs the setup above (plan ${sha.slice(0, 10)}) once in a hosted sandbox with synthetic data, and keeps the results as evidence. It approves no gate and merges nothing.`),
          blockers.length ? el("ul", { class: "plain-list explain" }, blockers.map(x => el("li", null, x))) : null,
          a.status === "verifying" ? el("p", { class: "explain" }, "Verification is running. This page follows it.") : el("div", null, btn, charged(`Starts a charged agent session (${appLeft(a)} left).`)), line)));
      }

      // the result and its evidence, with what it did not cover
      if (a.verification) {
        const v = a.verification;
        const uncovered = (plan.capabilities || []).filter(c => !c.available);
        const stale = (v.source_sha && a.source_sha !== v.source_sha) || (v.plan_sha256 && a.plan_sha256 !== v.plan_sha256);
        out.unshift(card("Evidence from the last verification", el("div", { class: "stack" },
          el("div", { class: "row-tags" }, el("span", { class: `tag ${v.status === "passed" ? "ok" : v.status === "failed" ? "bad" : "warn"}` }, words(v.status)),
            stale ? el("span", { class: "tag warn" }, "Earlier commit") : null, v.cost_usd != null ? el("span", { class: "tag" }, `${money(v.cost_usd)} charged`) : null),
          v.detail ? el("p", null, v.detail) : null,
          v.phases ? el("ul", {class:"plain-list"}, v.phases.map(p => el("li", null, `${words(p.phase)}: ${words(p.status)}${p.seconds != null ? ` · ${p.seconds}s` : ""}${p.exit_code ? ` · exit ${p.exit_code}` : ""}${p.reason ? ` · ${words(p.reason)}` : ""}`))) : null,
          readiness(v),
          (v.checks || []).length ? el("div", null, el("h3", { class: "mini-h" }, "Functional checks"), checkList(v)) : null,
          v.diagnosis && v.diagnosis.summary ? el("div", { class: "notice info m-0" },
            el("b", null, `Sandbox agent’s diagnosis${v.diagnosis.phase ? ` (${words(v.diagnosis.phase)})` : ""}: `), v.diagnosis.summary,
            el("p", { class: "explain m-0 mt-2" }, "Written by the agent after reading the sandbox logs. It explains the failure; it is not evidence.")) : null,
          uncovered.length ? el("div", { class: "notice info m-0" }, el("b", null, "Not covered by this verification: "),
            uncovered.map(c => `${c.label || c.id}${c.limitation ? ` (${c.limitation})` : ""}`).join("; "),
            v.status === "passed" && !stale ? ". The app is ready only for the verified journeys." : ". This attempt does not establish readiness for the current setup.") : null,
          (a.evidence || []).length ? evidenceGallery(a) : el("p", { class: "explain" }, (v.checks || []).length ? "This recipe produces no media; its checks above are the evidence." : "No media was accepted from this verification."),
          el("dl", { class: "kv" }, el("dt", null, "Source commit"), el("dd", { class: "mono" }, v.source_sha || a.source_sha || "new scaffold"), el("dt", null, "Tested snapshot"), el("dd", { class: "mono" }, v.head_sha || "not given"), el("dt", null, "Profile fingerprint"), el("dd", { class: "mono" }, v.profile_sha256 || "not given"),
            el("dt", null, "Session"), el("dd", { class: "mono" }, v.session_id || "not given"))), stale ? "a newer commit needs a new verification" : null));
      }

      // publish: an explicit, confirmed step; nothing merges
      if (a.status === "ready" || a.status === "ready_limited" || a.status === "publishing" || a.pr_url) {
        const label = a.kind === "greenfield" ? "Create private repository and setup PR" : "Publish setup PR";
        const line = actionLine();
        const confirmBox = el("div", { class: "confirm-box", hidden: true });
        const first = el("button", { class: "btn primary", type: "button", disabled: busy() || !a.plan_sha256 }, label);
        const sha = a.plan_sha256;
        first.onclick = () => { confirmBox.hidden = false; first.hidden = true; confirmBtn.focus(); };
        const confirmBtn = el("button", { class: "btn primary", type: "button" }, `Confirm: ${label.toLowerCase()}`);
        const cancel = el("button", { class: "btn", type: "button", onclick: () => { confirmBox.hidden = true; first.hidden = false; first.focus(); } }, "Cancel");
        confirmBtn.onclick = () => act(confirmBtn, line, () => api(`${appPath(appId)}/publish`, { body: { plan_sha256: sha } }));
        confirmBox.append(el("p", { class: "what" }, a.kind === "greenfield"
          ? `This creates the private repository ${repoName(a.repository_url)} and opens a pull request with the setup.`
          : `This opens a pull request on ${repoName(a.repository_url)} with the setup files.`),
          el("p", { class: "explain" }, "Nothing is merged. An engineer reviews and merges the pull request."), el("div", { class: "startbar" }, confirmBtn, cancel));
        const change = a.kind === "existing" || a.pr_url
          ? el("div", null, el("a", { class: "btn", href: `#/start?app=${encodeURIComponent(a.app_id)}` }, "Request a change"),
            el("p", { class: "explain m-0 mt-2 fs-sm" }, "Starts a normal run on this app's repository. You still approve the plan and the contracts."))
          : el("p", { class: "explain" }, "Requesting changes opens once the repository exists, after you publish.");
        out.push(card("Setup pull request", el("div", { class: "stack" },
          a.pr_url && /^https:\/\//.test(a.pr_url) ? el("p", null, el("a", { href: a.pr_url, target: "_blank", rel: "noopener" }, "Open the setup pull request"),
            el("span", { class: "explain" }, a.pr_state === "merged" ? ` · merged${a.pr_merged_at ? " " + ago(a.pr_merged_at) : ""}. A change run checks, when it starts, that its base branch holds this verified recipe`
              : a.pr_state === "closed" ? " · closed without merging; change runs cannot use this setup until it is on the base branch"
              : " · waiting for an engineer to review and merge")) : null,
          a.status === "publishing" ? el("p", { class: "explain" }, "Publishing. This page follows it.") : a.pr_url ? null : !(plan.files || []).length ? el("p", {class:"explain"}, "The existing setup was reused unchanged. No new setup pull request is needed.") : el("div", null, first, confirmBox, line),
          change)));
      }
      out.push(sourceDetails(a));
      return out;
    }
    function sourceDetails(a) {
      return el("details", { class: "tech card" }, el("summary", null, "Technical details: repository, branch and commit"),
        el("dl", { class: "kv mt-3" },
          el("dt", null, "Repository"), el("dd", { class: "mono" }, a.repository_url || "not given"),
          el("dt", null, "Source branch or commit"), el("dd", { class: "mono" }, a.revision || "the default branch"),
          el("dt", null, "Default branch"), el("dd", { class: "mono" }, a.default_branch || "not known until discovery reads the repository"),
          el("dt", null, "Source commit inspected"), el("dd", { class: "mono" }, a.source_sha || "not known until discovery reads the repository"),
          el("dt", null, "Execution environment"), el("dd", { class: "mono" }, a.environment_id || (a.verification || {}).environment_id || "created when the setup is verified",
            (a.retired_environments || []).length ? el("span", { class: "explain" }, ` · ${plural(a.retired_environments.length, "earlier environment")} archived after verification`) : null),
          el("dt", null, "App id"), el("dd", { class: "mono" }, a.app_id)));
    }

    function show(a) {
      app = a; pending = null; updateBar.hidden = true;
      headBox.replaceChildren(pageHead(a.name || a.app_id, [appKind(a.kind), a.description].filter(Boolean).join(" · ") || null,
        null,
        [el("a", { href: "#/apps" }, "Apps"), " / ", el("span", { class: "mono" }, repoName(a.repository_url) || a.app_id)]), card(null, appStepper(a), null, "soft"));
      statusBox.replaceChildren(appStatusBody(a));
      body.replaceChildren(...sections(a));
      state.lastKey.app = appKey(a);
      schedule();
    }
    const dirty = () => typing() || [...body.querySelectorAll("input[type=password]")].some(i => i.value) ||
      Object.keys(drafts).some(k => (drafts[k] || "") !== ((app.answers || {})[k] || ""));
    async function tick() {
      if (!document.body.contains(statusBox)) { stopPoll(); return; }
      let a;
      try { a = await api(appPath(appId)); } catch (e) { return; }
      if (appKey(a) === state.lastKey.app) return;
      statusBox.replaceChildren(appStatusBody(a));
      if (dirty()) {
        pending = a;
        updateBar.replaceChildren(el("b", null, `Now: ${appStatusText(a).label}. `), "Your typing is kept; the rest of the page updates when you are ready. ",
          el("button", { class: "btn sm", type: "button", onclick: () => show(pending || a) }, "Show the update"));
        updateBar.hidden = false;
        state.lastKey.app = appKey(a);
        return;
      }
      show(a);
    }
    function schedule() { startPoll(tick, busy() ? 3000 : 12000); }

    const aside = el("aside", null, card("Agent status", statusBox));
    if (!paint([headBox, updateBar, el("div", { class: "start" }, body, aside)], nav)) return;
    show(app);
  }

  // ------------------------------------------------------------------ settings
  async function viewSettings() {
    setNav("settings");
    const nav = state.nav;
    const [c, logs] = await Promise.all([api("/api/config"), api("/api/logs")]);
    state.config = c;
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Settings", "Connections, agent sessions and diagnostics."));
    const platformGaps = gapsMatching(c.design_gaps, ["agent:", "environment_id", "ANTHROPIC_API_KEY"]);
    const forgeGaps = forgeGapsOf(c);
    const surfaceGaps = gapsMatching(c.gaps, ["LINEAR", "FACTORY_GATE_SURFACE", "FACTORY_PAGE_PUBLISHER"]);
    const r = (title, ok, desc, gaps, how, label) => el("div", { class: "r" }, el("h3", null, title), el("div", { class: `tag ${ok ? "ok" : "warn"}` }, ok ? "Ready" : (label || "Not configured")),
      el("p", { class: "explain mt-2" }, desc), gaps.length ? el("ul", null, gaps.map(x => el("li", null, x))) : null, !ok && how ? el("p", { class: "explain" }, how) : null);
    const agentsCard = r("The crew and the platform", !platformGaps.length, "The agents and the sandbox environment in your Console workspace, plus the API key the controller uses to start sessions.", platformGaps, el("span", null, "Run ", el("code", null, "ant apply agents environments"), " once and put ", el("code", null, "ANTHROPIC_API_KEY"), " (and ", el("code", null, "ANTHROPIC_WORKSPACE_ID"), " if the key is not workspace-scoped) in ", el("code", null, ".env"), " at the repository root."), "Checking...");
    frag.append(card("Readiness", el("div", { class: "readiness" },
      agentsCard,
      r("Code host", !forgeGaps.length, `Where the code lives and pull requests are opened (${c.forge}). Needed for full runs, not for design runs.`, forgeGaps, "Export FORGE_TOKEN (a GitHub token with repo and workflow scopes); FORGE_REPO may stay unset, it follows the outcome."),
      r("Decisions", !surfaceGaps.length, `Where gate decisions are read from: ${c.gate_surface === "tracker" ? "Linear and this Console" : "this Console and the CLI"}.`, surfaceGaps, null))));
    const platformBox = el("div", { class: "platform" }, el("p", { class: "explain" }, "Checking the platform..."));
    frag.append(card("On the platform", platformBox, "live check of the agents in claude-lock.json and the last sessions"));
    api("/api/platform").then(p => {
      const st = agentsCard.querySelector(".status");
      st.className = `status ${p.ok ? "ok" : "warn"}`;
      st.textContent = p.ok ? `Ready: ${p.agents.length} agents found on the platform` : (p.error ? "Needs attention" : "Not configured");
      const parts = [];
      if (p.error) parts.push(el("div", { class: "notice bad" }, el("b", null, "Cannot reach the agents. "), p.error));
      if (p.agents && p.agents.length) {
        const t = el("table", null, el("tr", null, el("th", null, "Agent"), el("th", null, "Id"), el("th", null, "On the platform")));
        for (const a of p.agents) t.append(el("tr", null, el("td", null, el("span", { class: "row-gap" }, crewFigure(a.key, "still", "s"), CREW_NAME[a.key] || a.key)), el("td", { class: "mono explain" }, a.id), el("td", null, el("span", { class: `tag ${a.found ? "ok" : "bad"}` }, a.found ? `found${a.name ? ": " + a.name : ""}` : "missing"))));
        parts.push(el("details", {open: !p.ok}, el("summary", null, `${p.agents.length} configured agents`), t));
      }
      if (p.sessions && p.sessions.length) {
        const t = el("table", { class: "platform-sessions" }, el("tr", null, el("th", null, "Session"), el("th", null, "Status"), el("th", null, "Run"), el("th", null, "Created")));
        for (const s of p.sessions) t.append(el("tr", null, el("td", null, el("a", { href: s.url, target: "_blank", rel: "noopener" }, s.title || s.id)), el("td", null, el("span", { class: "tag" }, s.status || "?")),
          el("td", null, s.local_url ? el("a", { href: s.local_url }, s.run_id) : el("span", { class: "explain", title: s.run_id ? "Not stored on this Console; open the platform session for its trace." : "" }, s.run_id || "not a factory run")), el("td", { class: "explain" }, s.created_at ? ago(s.created_at) : "")));
        parts.push(el("h3", null, "Recent sessions in the workspace"), t);
      } else if (!p.error) parts.push(el("p", { class: "explain" }, p.sessions_error ? `Sessions could not be listed: ${p.sessions_error}` : "No sessions yet. Start a design run to see the decomposer appear here and in the Anthropic Console."));
      platformBox.replaceChildren(...parts);
    }).catch(e => platformBox.replaceChildren(el("div", { class: "notice bad" }, e.message)));
    frag.append(el("details", {class:"card optional-fields"}, el("summary", null, "Local storage and workspace"), el("dl", { class: "kv" }, el("dt", null, "Path"), el("dd", { class: "mono" }, c.runs_dir), el("dt", null, "Decisions"), el("dd", null, "Written to _queue/ under it; a running orchestrator polls that directory."), el("dt", null, "Workspace"), el("dd", null, c.workspace_id ? el("span", { class: "mono" }, c.workspace_id) : el("span", { class: "explain" }, "ANTHROPIC_WORKSPACE_ID not set (needed when the API key is not scoped to a workspace)")))));
    if (logs.logs.length) {
      const body = el("div", { class: "stack" }, logs.logs.map(l => el("div", null, el("div", { class: "title" }, el("span", { class: "mono" }, l.name), " · ", l.run_id ? el("a", { href: `#/runs/${l.run_id}` }, l.run_id) : el("span", { class: "tag" }, "no run id yet"), " · ", el("span", { class: "explain" }, ago(l.modified_at))),
        el("pre", { class: "log" }, l.tail))));
      frag.append(el("details", {class:"card optional-fields"}, el("summary", null, "Orchestrator logs"), el("div",{class:"card-body"},body)));
    }
    if (!paint(frag, nav)) return;
    state.lastKey.settings = settingsKey(c, logs);
    startPoll(refreshWhenChanged("settings", async () => { const [c2, l2] = await Promise.all([api("/api/config"), api("/api/logs")]); return settingsKey(c2, l2); }, viewSettings), 30000);
  }
  const settingsKey = (c, logs) => JSON.stringify([c.gaps, c.design_gaps, c.forge, c.forge_repo, (logs.logs || []).map(l => [l.name, l.modified_at, l.bytes])]);

  // ------------------------------------------------------------------ router
  // The page is swapped only by the navigation that asked for it: a slow fetch from an earlier click, or a poll that fires
  // after the person moved on, never paints over the current page. Open folds survive a re-render of the same page.
  function paint(frag, nav) {
    if (nav && nav !== state.nav) return false;   // a stale render: the caller stops too, so it never touches the polls
    const foldName = sm => { const owner = sm.closest("[data-evidence-id]"); return `${owner ? owner.dataset.evidenceId : ""}::${sm.textContent.trim().replace(/\s*\([^)]*\)/g, "")}`; };   // 'Work items (4)…' keeps its state when the count moves
    const open = new Set(nav && nav.fresh ? [] : [...$main.querySelectorAll("details[open] > summary")].map(foldName));
    $main.replaceChildren(...(Array.isArray(frag) ? frag : [frag]));
    if (nav && nav.fresh) { nav.fresh = false; const h1 = $main.querySelector("h1"); if (h1) { h1.tabIndex = -1; h1.focus({ preventScroll: true }); } }   // a route change, not a poll: move the reader to the new title
    if (open.size) $main.querySelectorAll("details > summary").forEach(s => { if (open.has(foldName(s))) s.parentElement.open = true; });
    return true;
  }
  async function route() {
    const hash = location.hash || "#/runs";
    const [path, query] = hash.slice(1).split("?");
    const params = new URLSearchParams(query || "");
    const nav = state.nav = { fresh: true };
    document.body.dataset.loading = "1";
    loadChrome();
    try {
      if (path === "/" || path === "" || path === "/runs") await viewRuns();
      else if (path === "/inbox" || path === "/board") await viewInbox();
      else if (path.startsWith("/runs/")) await viewRun(path.slice(6).split("#")[0]);
      else if (path.startsWith("/gates/")) await viewGate(path.slice(7));
      else if (path === "/start") await viewStart(params);
      else if (path === "/outcomes") await viewOutcomes();
      else if (path === "/outcomes/new" || path.startsWith("/outcomes/")) await viewOutcomeForm(path === "/outcomes/new" ? params : new URLSearchParams({ ...Object.fromEntries(params), outcome: decodeURIComponent(path.slice(10)) }));
      else if (path === "/apps") await viewApps();
      else if (path === "/apps/new") await viewAppNew();
      else if (path.startsWith("/apps/")) await viewApp(decodeURIComponent(path.slice(6)));
      else if (path === "/settings") await viewSettings();
      else { setNav(null); paint(card(null, el("div", { class: "empty" }, el("h3", null, "No such page"), backToRuns())), nav); stopPoll(); }
    } catch (e) {
      if (nav !== state.nav) return;
      stopPoll();
      const frag = document.createDocumentFragment();
      frag.append(pageHead("Something did not load"), card(null, el("div", { class: "empty" }, el("div", null, e.message), backToRuns())));
      if (!paint(frag, nav)) return;
    } finally {
      if (nav === state.nav) delete document.body.dataset.loading;
    }
    if (nav !== state.nav) return;
    if (!/#(q|record-[^#?]+)$/.test(hash)) window.scrollTo(0, 0);      // a link to a question or a run record scrolls to it instead
  }
  // the link works even when the hash is already #/runs (no hashchange fires then)
  const backToRuns = () => el("a", { class: "btn", href: "#/runs", onclick: ev => { if (location.hash === "#/runs") { ev.preventDefault(); route(); } } }, "Back to the runs");
  window.addEventListener("hashchange", route);
  loadChrome().then(route);
})();
