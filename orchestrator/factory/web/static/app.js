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

  const STAGES = [
    ["intake", "Outcome", "What the business wants, with metrics."],
    ["decomposing", "Decomposing", "An agent splits it into work items."],
    ["gate_1_decomposition", "Gate 1: you", "You approve the shape of the work."],
    ["specifying", "Specifying", "Agents write a spec per item; a judge checks the set."],
    ["gate_2_spec_readiness", "Gate 2: you", "You approve the contracts before any code."],
    ["implementing", "Building", "Agents implement, review, and open pull requests."],
    ["completed", "Done", "Every item has a pull request for human review."],
  ];
  const TERMINAL = { closed: "Closed", escalated: "Escalated", failed: "Failed", paused_budget: "Paused at budget" };
  const ITEM_STAGE = {
    pending: "Waiting", spec_drafting: "Spec being written", spec_judging: "Spec being judged", spec_approved: "Spec approved",
    implementing: "Implementing", reviewing: "In review", responding: "Responding to review", merge_judging: "Merge judge",
    integrating: "Opening pull request", cr_open: "Pull request open", merged: "Merged", deploying_dev: "Deploying to dev",
    deployed_dev: "Verified in dev", closed: "Closed", escalated: "Escalated", blocked: "Blocked",
  };
  const GATE_NAME = { gate_1_decomposition: "Gate 1: approve the plan", gate_2_spec_readiness: "Gate 2: approve the contracts",
    escalation: "Escalation: the judge could not pass the specs", budget: "Budget: raise it or close the run", deploy_failure: "Deploy failure" };
  const GATE_SHORT = { gate_1_decomposition: "Gate 1", gate_2_spec_readiness: "Gate 2", escalation: "Escalation", budget: "Budget", deploy_failure: "Deploy failure" };
  const DECISION_LABEL = { approve: "Approve", approve_subset: "Approve selected", request_changes: "Request changes", reject: "Reject" };
  const ROLE_NAME = { po: "Product owner", eng: "Engineer", ops: "Ops", crew: "Claude crew" };
  const ROLE_DOES = { po: "Decides what gets built", eng: "Reviews and merges", ops: "Watches it land", crew: "Does the work between gates" };
  const CREW_NAME = { codebase_researcher: "Researcher", decomposer: "Decomposer", spec_writer: "Spec writer", spec_judge: "Spec judge", implementer: "Implementer",
    reviewer: "Reviewer", security_reviewer: "Security reviewer", merge_judge: "Merge judge", integrator: "Integrator" };
  const CREW_DOES = { decomposer: "Outcome into work items", spec_writer: "One contract per item", spec_judge: "Scores the contracts", implementer: "Builds it, tests first",
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
  const CREW_TOOL = { codebase_researcher: "book", decomposer: "clipboard", spec_writer: "pencil", spec_judge: "lens", implementer: "wrench", reviewer: "lens", security_reviewer: "shield", merge_judge: "gavel", integrator: "stamp" };

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
  function clock(iso) { return iso ? iso.slice(11, 16) : ""; }
  function money(n) { return n == null ? "" : `$${Number(n).toFixed(2)}`; }
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
  // state: asleep | working | done | blocked | still. size: "l" (72x80) or "s" (44x50)
  function crewFigure(role, state, size) {
    const big = size !== "s";
    const u = big ? 6.5 : 4, ox = big ? 6.5 : 4, oy = big ? 14 : 9;
    const w = big ? 72 : 44, h = big ? 80 : 50;
    const node = svg("svg", { class: `crew ${state || "asleep"}`, width: w, height: h, viewBox: `0 0 ${w} ${h}`, "aria-hidden": "true" });
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
    if (big) node.append(svg("rect", { x: 4, y: 70, width: 64, height: 6, rx: 3, fill: "var(--line)" }));
    return node;
  }
  document.getElementById("brand-mark").append(crewFigure("implementer", "still", "s"));

  // ------------------------------------------------------------------ the line: a compact progress strip with the active station's crew member
  function lineStrip(r) {
    const open = (r.gates || []).filter(g => !g.recorded);
    const stage = r.stage;
    const terminal = TERMINAL[stage];
    const S = [
      { key: "intake", lbl: "Outcome", who: "po", d: "The brief" },
      { key: "decomposing", lbl: "Decompose", who: "crew", d: "Work items", role: "decomposer" },
      { key: "gate_1_decomposition", lbl: "Plan check", who: "po", d: "Gate 1" },
      { key: "specifying", lbl: "Specify and judge", who: "crew", d: "Contracts", role: "spec_writer" },
      { key: "gate_2_spec_readiness", lbl: "Contract check", who: "po", d: "Gate 2" },
      { key: "implementing", lbl: "Build and review", who: "crew", d: "Branches", role: "implementer" },
      { key: "completed", lbl: "Pull request", who: "eng", d: "Review and merge" },
    ];
    const idx = STAGES.findIndex(s => s[0] === stage);
    const working = (r.crew || []).filter(c => c.state === "working").map(c => c.role);
    const stations = el("div", { class: "stations" });
    S.forEach((s, i) => {
      let cls = `st ${s.who}`;
      const done = !terminal && i < idx, current = !terminal && i === idx;
      if (done || (stage === "completed" && i === S.length - 1)) cls += " done";
      if (current && !r.design_only) cls += " current";
      const isCall = open.length && ((s.key === "gate_1_decomposition" && open.some(g => g.kind === "gate_1_decomposition")) || (s.key === "gate_2_spec_readiness" && open.some(g => g.kind === "gate_2_spec_readiness")));
      if (isCall) cls += " call calling";
      let fig = null;
      if (current && s.who === "crew") {
        const role = working.find(w => ["decomposer", "spec_writer", "spec_judge", "implementer", "reviewer", "security_reviewer", "merge_judge", "integrator"].includes(w)) || s.role;
        fig = el("span", { class: "fig" }, crewFigure(role, working.length ? "working" : "still", "s"));
      }
      stations.append(el("div", { class: cls, title: s.d },
        el("div", { class: "bar" }), el("span", { class: "n" }, `Station ${i + 1}`), el("span", { class: "lbl" }, s.lbl),
        el("span", { class: `who ${s.who}` }, isCall ? "Your call" : ROLE_NAME[s.who]), fig));
    });
    const belt = el("div", { class: "belt" });
    return el("div", { class: `line ${terminal || stage === "completed" ? "still" : ""}` }, stations, belt);
  }
  function miniSteps(stage) {
    const idx = STAGES.findIndex(s => s[0] === stage);
    const terminal = TERMINAL[stage];
    return el("span", { class: "mini-steps", title: terminal || (STAGES[idx] || [])[1] || stage },
      STAGES.map((s, i) => el("i", { class: terminal ? (i < 2 ? "done" : "") : i < idx ? "done" : i === idx ? "current" : "" })),
      terminal ? el("i", { class: "terminal" }) : null);
  }
  function stepper(stage, designOnly) {
    const idx = STAGES.findIndex(s => s[0] === stage);
    const terminal = TERMINAL[stage];
    const t = el("div", { class: "stepper", role: "list", "aria-label": "Run stages" });
    const stages = designOnly ? STAGES.map(s => s[0] === "implementing" ? [s[0], "Building", "Skipped: design run."] : s[0] === "completed" ? [s[0], "Done", "Specs approved; nothing built."] : s) : STAGES;
    stages.forEach(([key, label, desc], i) => {
      let cls = "step";
      if (!terminal && i < idx) cls += " done";
      else if (!terminal && i === idx) cls += " current";
      else if (terminal && i < 2) cls += " done";
      t.append(el("div", { class: cls, role: "listitem" }, el("span", { class: "n" }, i + 1), el("span", { class: "lbl" }, label), el("span", { class: "d" }, desc)));
    });
    if (terminal) t.append(el("div", { class: "step terminal", role: "listitem" }, el("span", { class: "lbl" }, terminal)));
    return t;
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

  // what the board says is waiting on a person; the sidebar badge and the board header count the same cards
  const onBoard = b => b.runs.filter(r => !r.terminal || r.cards.length);
  const waitingOf = runs => {
    const cards = runs.flatMap(r => r.cards);
    return { decisions: cards.filter(c => c.kind === "gate" || c.kind === "question").length, reviews: cards.filter(c => c.kind === "pr").length };
  };
  const gapsMatching = (gaps, keys) => gaps.filter(g => keys.some(k => g.includes(k)));
  const forgeGapsOf = c => gapsMatching(c.gaps, ["FORGE", "FACTORY_FORGE", "FACTORY_VAULT_ID"]);
  async function loadChrome() {
    try {
      const [c, board] = await Promise.all([api("/api/config"), api("/api/board")]);
      state.config = c;
      const w = waitingOf(onBoard(board)), n = w.decisions + w.reviews;
      $inboxCount.textContent = n; $inboxCount.classList.toggle("zero", n === 0);
      const dark = document.documentElement.dataset.theme === "dark";
      const forgeOk = !forgeGapsOf(c).length;
      $foot.replaceChildren(
        el("b", null, "Crew"), el("span", { class: c.can_run_design ? "" : "warn" }, c.can_run_design ? "ready" : "not configured"),
        el("br"), el("b", null, "Code host"), el("span", { class: forgeOk ? "" : "warn" }, forgeOk ? `${c.forge}${c.forge_repo ? ": " + c.forge_repo : ""}` : "not configured"),
        el("br"), el("button", { class: "theme", type: "button", onclick: () => { applyTheme(dark ? "light" : "dark"); loadChrome(); } }, dark ? "Light mode" : "Dark mode"));
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
    return async () => {
      if (typing()) return;
      try {
        const k = await fetchKey();
        if (k !== state.lastKey[key]) { state.lastKey[key] = k; await render(); }
        loadChrome();
      } catch (e) { /* keep the page */ }
    };
  }

  function pageHead(title, sub, actions, crumbs) {
    return el("div", { class: "page-head" },
      el("div", null, crumbs ? el("div", { class: "crumbs" }, crumbs) : null, el("h1", null, title), sub ? el("div", { class: "sub" }, sub) : null),
      actions ? el("div", { class: "actions" }, actions) : null);
  }
  function card(title, body, meta, cls) {
    return el("section", { class: `card ${cls || ""}` },
      title ? el("div", { class: "card-head" }, el("h2", null, title), meta ? el("span", { class: "meta" }, meta) : null) : null,
      el("div", { class: "card-body" }, body));
  }
  function roleLegend() {
    return el("div", { style: "display:flex;flex-wrap:wrap;gap:10px 22px;align-items:center;font-size:14px" },
      el("span", { class: "role crew" }, el("i"), "Claude crew"),
      el("span", { class: "role po" }, el("i"), "Product owner decides what is built"),
      el("span", { class: "role eng" }, el("i"), "Engineer reviews and merges"),
      el("span", { class: "role ops" }, el("i"), "Ops watches it land"),
      el("span", { class: "role call", style: "margin-left:auto" }, el("i"), "A human is needed now"));
  }

  // ------------------------------------------------------------------ the board: columns are the pipeline, rows are who owns the work
  async function viewInbox() {
    setNav("inbox");
    const b = await api("/api/board");
    // a finished run stays on the board while a person still owes it something (a pull request to review, a deploy to watch)
    const live = onBoard(b);
    const active = live.filter(r => !r.terminal).length, handedOff = live.length - active;
    const w = waitingOf(live);
    const waitingLabel = [w.decisions ? plural(w.decisions, "decision") : null, w.reviews ? plural(w.reviews, "pull request") : null].filter(Boolean).join(", ");
    const frag = document.createDocumentFragment();
    frag.append(pageHead("The board", "Columns are the pipeline. Rows are who owns the work. A card only sits in its owner's row, so whose turn it is is the row, not a label.",
      [waitingLabel ? el("span", { class: "tag gate" }, `${waitingLabel} waiting`) : el("span", { class: "tag ok" }, "Nothing waiting on a person"),
       el("a", { class: "btn primary", href: "#/start" }, "Start a run")]));
    if (!live.length) {
      frag.append(card(null, el("div", { class: "empty" }, crewFigure("implementer", "asleep", "l"), el("h3", null, "The floor is quiet"),
        el("div", null, "No run is in progress. Start one from an outcome; the crew wakes up and the board fills in."),
        el("a", { class: "btn primary", href: "#/start" }, "Start a run"))));
    } else {
      const g = el("div", { class: "g" });
      g.append(el("div"));
      for (const col of [["plan", "Plan"], ["specify", "Specify"], ["build", "Build"], ["review", "Review"], ["ship", "Ship"]]) g.append(el("div", { class: "colh" }, col[1]));
      for (const row of ["po", "eng", "ops", "crew"]) {
        g.append(el("div", { class: `rowh ${row}` }, el("b", null, ROLE_NAME[row]), el("span", null, ROLE_DOES[row])));
        for (const col of ["plan", "specify", "build", "review", "ship"]) {
          const cell = el("div", { class: "cell" });
          for (const r of live) {
            for (const c of r.cards.filter(c => c.row === row && c.column === col)) cell.append(boardCard(r, c));
            // quiet placeholders that say what will happen next for a human, so an empty row still answers "when is it my turn"
            if (row === "eng" && col === "ship" && r.column !== "ship" && !r.cards.some(c => c.kind === "pr")) cell.append(el("div", { class: "k quiet" }, el("span", { class: "run" }, r.title), el("span", { class: "t" }, "Review the pull request"), el("span", { class: "s" }, "after Build and Review")));
          }
          g.append(cell);
        }
      }
      frag.append(card(null, el("div", { class: "board" }, g, el("div", { class: "legend" }, el("span", null, [active ? `${plural(active, "run")} in progress.` : null, handedOff ? `${plural(handedOff, "finished run")} with pull requests to review.` : null, "Finished runs are under Runs."].filter(Boolean).join(" ")))), null));
    }
    frag.append(card("How a run moves", el("div", null, roleLegend(), el("p", { class: "explain", style: "margin-top:12px" }, "The crew do the stages between the gates. The two gates are the product owner's: nothing is built until the plan and then the contracts are approved. The engineer reviews and merges the pull request. Ops watches the deploy.")), null, "soft"));
    $main.replaceChildren(frag);
    state.lastKey.inbox = boardKey(b);
    startPoll(refreshWhenChanged("inbox", async () => boardKey(await api("/api/board")), viewInbox), 5000);
  }
  function boardKey(b) { return JSON.stringify(b.runs.map(r => [r.run_id, r.stage, r.updated_at, r.cards.map(c => [c.kind, c.gate_id || c.question_id || c.role || c.work_item_id])])); }
  function boardCard(r, c) {
    if (c.kind === "agent") {
      return el("div", { class: "k agent" }, crewFigure(c.role, "working", "s"),
        el("div", { class: "txt" }, el("span", { class: "t" }, CREW_NAME[c.role] || words(c.role)), el("span", { class: "s" }, `${r.title}, ${dur(c.since)}`)));
    }
    if (c.kind === "gate") {
      return el("div", { class: "k call calling" }, el("span", { class: "since" }, `Your call since ${clock(c.since)}`), el("span", { class: "t" }, GATE_NAME[c.title] || words(c.title)),
        el("span", { class: "s" }, `${r.title}. ${(c.summary || "").slice(0, 140)}`), el("a", { class: "btn call", href: `#/gates/${c.gate_id}`, style: "align-self:flex-start" }, "Review and decide"));
    }
    if (c.kind === "question") {
      return el("div", { class: "k call calling" }, el("span", { class: "since" }, `${c.blocking ? "Agent paused" : "Question"} since ${clock(c.since)}`), el("span", { class: "t" }, c.title),
        el("span", { class: "s" }, r.title), el("a", { class: "btn call", href: `#/runs/${r.run_id}#q`, style: "align-self:flex-start" }, "Answer"));
    }
    if (c.kind === "pr") {
      return el("div", { class: "k call" }, el("span", { class: "since" }, "Waiting for you"), el("span", { class: "t" }, c.title), el("span", { class: "s" }, r.title),
        c.url ? el("a", { class: "btn", href: c.url, target: "_blank", rel: "noopener", style: "align-self:flex-start" }, "Open on the code host") : null);
    }
    return el("div", { class: "k quiet" }, el("span", { class: "run" }, r.title), el("span", { class: "t" }, c.title));
  }

  // ------------------------------------------------------------------ runs
  async function viewRuns() {
    setNav("runs");
    const runs = (await api("/api/runs")).runs;
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Runs", "One run per outcome. A run ends with pull requests for a person to review.",
      [el("a", { class: "btn primary", href: "#/start" }, "Start a run")]));
    if (!runs.length) {
      frag.append(card(null, el("div", { class: "empty" }, crewFigure("decomposer", "asleep", "l"), el("h3", null, "No runs yet"),
        el("div", null, "Start one from an outcome, or run the sample outcome as a demo to see how the gates work."),
        el("a", { class: "btn primary", href: "#/start" }, "Start a run"))));
    } else {
      const tbl = el("table", null, el("tr", null, el("th", null, "Outcome"), el("th", null, "Where"), el("th", null, "Items"), el("th", null, "Spend"), el("th", null, "Last activity")));
      for (const r of runs) {
        const wi = r.work_items || { total: 0 };
        const waiting = r.waiting ? r.waiting.gates + r.waiting.questions : 0;
        tbl.append(el("tr", { class: "link", onclick: () => { location.hash = `#/runs/${r.run_id}`; } },
          el("td", null, el("div", { class: "title" }, el("a", { href: `#/runs/${r.run_id}` }, r.title), r.demo ? el("span", { class: "tag demo" }, "demo") : el("span", { class: "tag live" }, r.design_only ? "design only" : "live")),
            waiting ? el("div", { class: "explain", style: "color:var(--call-text);font-weight:700" }, `${plural(waiting, "decision")} waiting on you`) : null),
          el("td", null, miniSteps(r.stage), " ", el("span", { class: "explain" }, TERMINAL[r.stage] || (STAGES.find(s => s[0] === r.stage) || [])[1] || r.stage)),
          el("td", null, wi.total), el("td", null, money(r.cost), r.budget_usd != null ? el("span", { class: "explain" }, ` of ${money(r.budget_usd)}`) : null), el("td", { class: "explain" }, ago(r.updated_at))));
      }
      frag.append(card("All runs", tbl, plural(runs.length, "run")));
    }
    $main.replaceChildren(frag);
    state.lastKey.runs = JSON.stringify(runs.map(r => [r.run_id, r.stage, r.updated_at]));
    startPoll(refreshWhenChanged("runs", async () => JSON.stringify((await api("/api/runs")).runs.map(r => [r.run_id, r.stage, r.updated_at])), viewRuns), 5000);
  }

  function askBlock(q, onDone) {
    const ta = el("textarea", { placeholder: "Your answer", "aria-label": "Your answer" });
    const status = el("div", { class: "explain" });
    const options = el("div", { class: "options" }, q.options.map(o => el("button", { class: "btn sm", type: "button", onclick: () => { ta.value = o; ta.focus(); } }, o)));
    const send = el("button", { class: "btn call", type: "button", style: "margin-top:8px", onclick: async () => {
      if (!ta.value.trim()) { status.textContent = "Type or pick an answer first."; return; }
      send.disabled = true;
      try { await api(`/api/questions/${q.question_id}/answer`, { body: { answer: ta.value.trim(), by: "console" } });
        box.replaceChildren(el("div", { class: "q" }, q.question), el("div", { class: "done" }, `Answered: ${ta.value.trim()}`),
          el("div", { class: "explain" }, "The agent continues with your answer."));
        if (onDone) onDone();
      } catch (e) { status.textContent = e.message; send.disabled = false; }
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
    const r = await api(`/api/runs/${runId}`);
    const frag = document.createDocumentFragment();
    const stageName = TERMINAL[r.stage] || (STAGES.find(s => s[0] === r.stage) || [])[1] || r.stage;
    const b = r.budget || {};
    const started = r.created_at;
    frag.append(pageHead(r.title, r.outcome ? r.outcome.statement.split(". ")[0] + "." : null,
      [r.demo ? el("span", { class: "tag demo" }, "demo") : el("span", { class: "tag live" }, r.design_only ? "design only" : (TERMINAL[r.stage] || stageName)),
       el("span", { class: `tag ${b.paused ? "warn" : ""}` }, `${money(r.cost)}${b.budget_usd != null ? ` of ${money(b.budget_usd)}` : " spent"}`),
       el("span", { class: "tag" }, dur(started, TERMINAL[r.stage] || r.stage === "completed" ? r.updated_at : null))],
      [el("a", { href: "#/runs" }, "Runs"), " / ", el("span", { class: "mono" }, r.run_id)]));

    frag.append(card(null, lineStrip(r), null, "soft"));

    for (const check of r.browser_checks_pending || []) frag.append(card("Browser check in progress",
      el("div", null, el("p", null, check.description), el("p", { class: "explain" }, `${check.persona} · ${check.environment.name} · waiting for browser observations and screenshots`))));
    if ((r.visual_evidence || []).length) frag.append(visualEvidenceCard(r));

    // roster
    const roster = el("div", { class: "roster" });
    for (const m of r.crew || []) {
      const costTxt = m.cost_usd ? money(m.cost_usd) : (m.state === "working" && m.started_at ? dur(m.started_at) : "");
      const st = m.state === "working" ? (m.title ? m.title.replace(`${r.run_id} `, "") : "Working") :
        m.state === "done" ? `Done${m.sessions > 1 ? `, ${plural(m.sessions, "session")}` : ""}` :
        m.state === "blocked" ? "Stopped at its budget" : crewAsleepText(m.role, r);
      roster.append(el("div", { class: `m ${m.state}` }, crewFigure(m.role, m.state, "s"),
        el("div", { class: "txt" }, el("div", { class: "nm" }, el("span", null, CREW_NAME[m.role] || words(m.role)), el("span", null, costTxt)),
          el("span", { class: "st" }, st))));
    }
    const consoleUrl = sid => state.config && state.config.console_session_url && !r.demo ? state.config.console_session_url.replace("SESSION_ID", sid) : null;
    const liveMember = (r.crew || []).find(m => m.state === "working" && m.session_id);
    const rosterCard = card("The crew", el("div", null, roster,
      el("p", { class: "explain", style: "margin-top:10px;font-size:12.5px" }, "Asleep means not started or finished for this run.", liveMember && consoleUrl(liveMember.session_id) ? [" ", el("a", { href: consoleUrl(liveMember.session_id), target: "_blank", rel: "noopener" }, "Open the live session in the Anthropic Console.")] : null)));

    // the story so far
    const story = el("div", { class: "story" });
    const entries = storyEntries(r).reverse();
    for (const e of entries) story.append(el("div", { class: "e" }, el("time", { datetime: e.at, title: e.at }, clock(e.at)),
      el("div", { class: "rail" }, el("i", { class: e.who }), el("b")),
      el("div", { class: "body" }, el("div", { class: "t" }, el("span", null, e.title), el("span", { class: `who ${e.who}` }, e.whoLabel)), e.body ? el("div", { class: "d" }, e.body) : null)));
    const storyCard = card("The story so far", entries.length ? story : el("p", { class: "explain" }, "Nothing has happened yet."), "newest first");

    // your turn
    const onCard = g => g.kind === "budget" && b.paused && b.pause;
    const gates = r.gates.filter(g => !g.recorded && !onCard(g)), recorded = r.gates.filter(g => g.recorded && !onCard(g));
    const turn = el("div", { class: "turn" });
    const waiting = gates.length + r.questions.length + (b.paused ? 1 : 0);
    turn.append(el("div", { class: "dot" }, el("i", { class: waiting ? "on" : "" }), el("h2", { style: "font-size:17px" }, "Your turn")));
    if (b.paused && b.pause) turn.append(budgetCard(r));
    if (waiting) {
      for (const q of r.questions) turn.append(askBlock(q, () => setTimeout(() => viewRun(runId), 1500)));
      for (const g of gates) turn.append(el("div", { class: "card call", style: "padding:12px" },
        el("div", { style: "font-size:12px;font-weight:700;color:var(--call-text)" }, `Your call since ${clock(g.opened_at)}`),
        el("div", { style: "font-weight:700;font-size:15px;margin:4px 0" }, GATE_NAME[g.kind] || g.kind),
        el("div", { class: "explain" }, (g.summary || "").slice(0, 220)),
        el("a", { class: "btn call", href: `#/gates/${g.gate_id}`, style: "margin-top:10px" }, "Review and decide")));
    } else {
      turn.append(el("p", { style: "margin:0;font-size:15px" }, r.stage === "completed" ? "Nothing waiting on you here. The pull request is on the code host." : TERMINAL[r.stage] ? `${stageName}. Nothing more happens in this run.` : "Nothing waiting on you."));
    }
    for (const g of recorded) turn.append(el("div", { class: "notice ok", style: "margin:0" }, el("b", null, `${DECISION_LABEL[g.recorded.decision] || g.recorded.decision} recorded on ${GATE_SHORT[g.kind]}. `), "The run picks it up within seconds. ", el("a", { href: `#/gates/${g.gate_id}` }, "View")));
    if (!TERMINAL[r.stage]) {
      const up = el("div", { class: "up" }, el("span", { class: "explain", style: "font-size:12.5px" }, "Coming up for humans"));
      const pendingGate = r.stage === "decomposing" || r.stage === "intake" ? "approve the plan (Gate 1)" : r.stage === "specifying" ? "approve the contracts (Gate 2)" : null;
      if (pendingGate) up.append(el("span", null, el("b", { class: "po" }, "Product owner"), `: ${pendingGate}, when the crew is done`));
      if (!r.design_only) up.append(el("span", null, el("b", { class: "eng" }, "Engineer"), r.stage === "completed" ? ": review and merge the pull request" : ": review the pull request, after Build and Review"));
      up.append(el("span", null, el("b", { class: "ops" }, "Ops"), r.watch && r.watch.pending && r.watch.pending.length ? ": the dev deploy is being watched" : ": nothing in this setup"));
      turn.append(up);
    }
    if (r.resumable && r.resuming) turn.append(el("div", { class: "notice info", style: "margin:0" }, el("b", null, "Resuming. "), "The orchestrator is re-checking the branch; this page follows it."));
    else if (r.resumable && !b.paused) turn.append(resumeBox(r, runId));
    if (r.watch && (r.watch.pending.length || r.watch.heartbeat)) turn.append(watchBox(r, runId));
    const turnCard = card(null, turn, null, waiting ? "call" : "");

    // how this run is tuned (read-only here; the next run is tuned on the Start screen)
    const p = { ...(r.policy || {}), ...(b.budget_usd != null ? { run_budget_usd: b.budget_usd } : {}) };
    const tune = el("div", { class: "tune" });
    const caps = p.stage_budget_usd || {};
    const summary = [p.run_budget_usd ? `$${p.run_budget_usd} cap` : null, p.preview_mode === "local" ? "local previews" : p.preview_mode ? `${p.preview_mode} previews` : null,
      p.reviewer_passes ? `${p.reviewer_passes} reviewers${p.security_review ? " plus security" : ""}` : null, p.review_max_rounds ? `${p.review_max_rounds} review rounds` : null].filter(Boolean).join(", ");
    const details = el("div", { class: "tier", hidden: true },
      el("dl", null,
        tunePair("Run budget", p.run_budget_usd ? `$${p.run_budget_usd}` : "from the outcome"), tunePair("Preview", p.preview_mode || "platform"),
        tunePair("Reviewer passes", p.reviewer_passes ?? "default"), tunePair("Review rounds", p.review_max_rounds ?? "default"),
        tunePair("Security reviewer", p.security_review === false ? "off" : "on"), tunePair("Items built at once", p.parallel_implementations ?? "default"),
        tunePair("Grader iterations", p.outcome_max_iterations ?? "default"), tunePair("Cap per implementer", caps.implement ? `$${caps.implement}` : "default"),
        tunePair("Cap per judge", caps.judge ? `$${caps.judge}` : "default"), tunePair("Cap per review", caps.review ? `$${caps.review}` : "default")),
      el("p", { class: "explain", style: "margin:10px 0 0;font-size:12.5px" }, "Settings are fixed for a run once it starts. ", el("a", { href: "#/start" }, "Tune the next run on the Start screen.")));
    const toggle = el("button", { class: "btn sm", type: "button", onclick: () => { details.hidden = !details.hidden; toggle.textContent = details.hidden ? "Show all" : "Hide"; } }, "Show all");
    tune.append(el("div", { class: "row" }, el("h2", { style: "font-size:16px" }, "How this run is tuned"), toggle), el("p", { class: "explain", style: "margin:0" }, summary || "Defaults."), details);
    const tuneCard = card(null, tune);

    frag.append(el("div", { class: "floor" }, rosterCard, storyCard, el("aside", { class: "stack" }, turnCard, tuneCard)));

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
      tl.append(el("li", null, el("time", { datetime: ev.at, title: ev.at }, ev.at ? ev.at.slice(11, 19) : ""),
        el("span", null, el("span", { class: who === "human" ? "human" : "" }, what.main), what.detail ? el("span", { class: "detail" }, " ", what.detail) : null, link)));
    }
    const files = el("table", null, el("tr", null, el("th", null, "Stage"), el("th", null, "File"), el("th", null, "Fingerprint")));
    for (const a of r.artifacts) files.append(el("tr", null, el("td", { class: "explain" }, a.stage),
      el("td", null, el("a", { href: `/api/artifacts/${r.run_id}/${a.stage}/${a.name}`, target: "_blank", rel: "noopener" }, a.name)),
      el("td", { class: "explain mono" }, a.sha256 ? a.sha256.slice(0, 12) : "")));
    const more = el("details", { style: "margin-top:16px" }, el("summary", { class: "explain", style: "cursor:pointer;font-weight:700" }, `Work items (${r.work_items.length}), every event (${r.events.length}) and every file (${r.artifacts.length})`),
      el("div", { class: "stack", style: "margin-top:12px" }, card("Work items", r.work_items.length ? items : el("p", { class: "explain" }, "Work items appear once the decomposition exists.")),
        el("div", { class: "grid-2" }, card("Every event", tl, "newest first"), card("Files produced", r.artifacts.length ? files : el("p", { class: "explain" }, "None yet.")))));
    frag.append(more);

    $main.replaceChildren(frag);
    if (location.hash.endsWith("#q")) { const q = document.getElementById("q"); if (q) q.scrollIntoView(); }
    state.lastKey.run = runKey(r);
    // A parked run stops polling; one paused at its budget keeps polling so the page follows the resume it started.
    if (!(TERMINAL[r.stage] || r.stage === "completed") || r.stage === "paused_budget") startPoll(refreshWhenChanged("run", async () => runKey(await api(`/api/runs/${runId}`)), () => viewRun(runId)), 4000);
    else stopPoll();
  }
  function visualEvidenceCard(r) {
    const body = el("div", { class: "stack" });
    const url = artifact => `/api/artifacts/${encodeURIComponent(r.run_id)}/${artifact.split("/").map(encodeURIComponent).join("/")}`;
    for (const evidence of r.visual_evidence) {
      const box = el("section", { class: "visual-check" });
      box.append(el("div", { class: "visual-heading" }, el("h3", null, evidence.description || "Browser check"),
        el("span", { class: `tag ${evidence.current && evidence.status === "passed" ? "live" : "warn"}` }, evidence.current ? words(evidence.status) : "Earlier commit")));
      box.append(el("p", null, evidence.summary));
      box.append(el("p", { class: "explain" }, `${evidence.persona || "Browser user"} · ${(evidence.head_sha || "").slice(0, 10)} · ${evidence.viewport ? `${evidence.viewport.width} × ${evidence.viewport.height}` : "viewport unavailable"}`));
      const media = (evidence.media || []).filter(m => m.artifact);
      const demo = media.find(m => m.role === "demo"), still = media.find(m => m.role === "screenshot");
      if (demo && demo.mime_type.startsWith("video/")) {
        box.append(el("p", { class: "visual-label" }, "Watch the enhancement"), el("video", {
          controls: true, preload: "metadata", playsinline: true, class: "visual-demo", src: url(demo.artifact),
          ...(still ? { poster: url(still.artifact) } : {}), "aria-label": `Demonstration: ${evidence.description || "enhancement"}`
        }));
      } else if (demo) {
        // GIFs stay collapsed until requested; no autoplaying motion on the run page.
        box.append(el("details", null, el("summary", null, "Watch the demonstration (GIF)"),
          el("img", { class: "visual-demo", loading: "lazy", src: url(demo.artifact), alt: demo.caption || "Recorded browser demonstration" })));
      } else if (still) {
        box.append(el("img", { class: "visual-demo", loading: "lazy", src: url(still.artifact), alt: still.caption || evidence.description }),
          el("p", { class: "explain" }, evidence.demo_note || "Recording unavailable; screenshot evidence is shown."));
      }
      const details = el("details", null, el("summary", null, "Coverage and test environment"));
      if (evidence.environment) details.append(el("p", { class: "explain" }, `${evidence.environment.name} · ${words(evidence.environment.data)} · ${words(evidence.environment.authentication)}`));
      const steps = el("ol");
      for (const step of evidence.steps_observed || []) steps.append(el("li", null, `${words(step.status)}: ${step.step} — ${step.observation}`));
      details.append(steps);
      for (const m of media) details.append(el("p", null, el("a", { href: url(m.artifact), target: "_blank", rel: "noopener" }, m.caption || m.name)));
      box.append(details); body.append(box);
    }
    return card("See the change", body, "Browser evidence is separate from code tests. Earlier recordings do not verify a new commit.", "visual-evidence-card");
  }
  function tunePair(k, v) { return el("div", null, el("dt", null, k), el("dd", null, String(v))); }
  function crewAsleepText(role, r) {
    const past = STAGES.findIndex(s => s[0] === r.stage);
    const needs = { decomposer: 1, spec_writer: 3, spec_judge: 3, implementer: 5, reviewer: 5, security_reviewer: 5, merge_judge: 5, integrator: 5 }[role] || 0;
    if (TERMINAL[r.stage]) return "Not needed";
    if (r.design_only && needs >= 5) return "Not in a design run";
    const waits = { decomposer: "Waits for the outcome", spec_writer: "Waits for the plan", spec_judge: "Waits for the contracts", implementer: "Waits for the approved contract",
      reviewer: "Waits for a branch", security_reviewer: "Waits for a branch", merge_judge: "Waits for the reviews", integrator: "Waits for the verdict" };
    return past < needs ? waits[role] : (waits[role] || "Waiting");
  }
  function runKey(r) {
    const hb = r.watch && r.watch.heartbeat;
    return JSON.stringify([r.updated_at, r.stage, r.gates.map(g => [g.gate_id, !!g.recorded]), r.questions.map(q => q.question_id),
      hb ? [hb.alive, hb.last_poll_at] : null, (r.browser_checks_pending || []).map(c => c.id), (r.crew || []).map(c => c.state), r.resuming || null]);
  }
  function storyEntries(r) {
    const out = [];
    const title = (r.outcome && r.outcome.title) || r.title;
    for (const ev of r.events) {
      const p = ev.payload || {}, item = ev.spec_id || ev.work_item_id;
      const E = (who, whoLabel, t, body) => out.push({ at: ev.at, who, whoLabel, title: t, body });
      switch (ev.kind) {
        case "run.created": E("po", "Product owner", "Outcome written", title); break;
        case "repository.created": E("po", "Product owner", "Repository created", `${p.url}${p.workflow_seeded === false ? ". The preview workflow could not be seeded (token scope); local previews do not need it." : ""}`); break;
        case "session.started": { const role = p.agent_key; if (!role || role === "codebase_researcher") break;
          const what = { decomposer: "Decomposing started", spec_writer: "Writing the contract", spec_judge: "Judging the contract", implementer: "Building started",
            reviewer: "Reviewing the branch", security_reviewer: "Security review started", merge_judge: "Deciding merge readiness", integrator: "Opening the pull request" }[role] || words(role);
          E("crew", CREW_NAME[role] || words(role), what, p.title ? p.title.replace(`${r.run_id} `, "") : ""); break; }
        case "gate.opened": E("call", "Your call", `${GATE_SHORT[p.kind] || p.kind} opened`, (p.summary || "").slice(0, 260)); break;
        case "gate.decided": E("po", "Product owner", `${GATE_SHORT[p.kind] || p.kind}: ${(DECISION_LABEL[p.decision] || p.decision).toLowerCase()}`, p.comments ? `“${p.comments.slice(0, 240)}${p.comments.length > 240 ? "…" : ""}”` : "No notes."); break;
        case "question.asked": E("call", "Your call", `The ${words(p.asked_by)} asked a question`, p.question); break;
        case "question.answered": E("po", "Product owner", "Question answered", p.answer); break;
        case "judge.verdict": E("crew", "Spec judge", `Spec ${p.result === "pass" ? "passed" : "sent back"} on round ${p.round}`, p.blocking ? `${plural(p.blocking, "blocking issue")}.` : ""); break;
        case "review.findings": E("crew", "Reviewers", `Review round ${p.round}: ${plural(p.count, "finding")}`, p.blocking ? `${p.blocking} blocking.` : p.count ? "None blocking." : "Nothing to fix."); break;
        case "review.response": { const n = {}; for (const d of (p.dispositions || [])) n[d] = (n[d] || 0) + 1;
          E("crew", "Implementer", `Answered review round ${p.round}`, Object.entries(n).map(([d, c]) => `${c} ${words(d)}`).join(", ")); break; }
        case "merge.verdict": E("crew", "Merge judge", `Merge judge: ${words(p.result)}`, item); break;
        case "cr.opened": E(p.design ? "po" : "crew", p.design ? "Product owner" : "Integrator", p.design ? "Design pull request opened" : "Pull request opened", p.url); break;
        case "cr.merged": E("eng", "Engineer", `Merged${p.id ? ` #${p.id}` : ""}`, p.merge_commit_sha ? `as ${String(p.merge_commit_sha).slice(0, 10)}` : ""); break;
        case "cr.closed": E("eng", "Engineer", "Pull request closed without merging", item); break;
        case "deploy.started": E("ops", "Ops", "Dev deploy started", p.pipeline_url || ""); break;
        case "deploy.succeeded": E("ops", "Ops", "Dev deploy succeeded", p.environment_url || ""); break;
        case "deploy.failed": E("ops", "Ops", "Dev deploy failed", p.status ? words(p.status) : ""); break;
        case "e2e.dev_passed": E("ops", "Ops", "Dev checks passed", ""); break;
        case "e2e.dev_failed": E("ops", "Ops", "Dev checks failed", ""); break;
        case "run.escalated": E("call", "Your call", "Escalated", p.reason); break;
        case "run.failed": E("call", "Your call", "The orchestrator stopped", p.reason); break;
        case "run.resumed": E("po", "Product owner", "Run resumed", Object.entries(p.points || {}).map(([s, x]) => `${s}: round ${x.round}${x.implementer_done ? ", implementer done" : ""}`).join("; ")); break;
        case "budget.reached": E("call", "Your call", "Budget reached", p.reason || ""); break;
        case "run.completed": E("eng", "Engineer", p.design_only ? "Design stages done" : "Done: the pull request is yours", p.design_only ? "Nothing was built; start a full run from the outcome to implement the approved specs." : "Review and merge happen on the code host."); break;
        default: break;
      }
    }
    return out;
  }

  function resumeBox(r, runId) {
    const why = (r.events || []).slice().reverse().find(e => e.kind === "run.failed");
    const box = el("div", { class: "stack" },
      el("p", { class: "explain", style: "margin:0" }, "The orchestrator stopped before this run reached a pull request", why && why.payload && why.payload.reason ? `: ${why.payload.reason}` : ".",
        " Resuming does not repeat finished agent sessions; it re-checks the branch and carries on."),
      el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", onclick: async (ev) => {
        const b = ev.currentTarget; b.disabled = true; b.textContent = "Resuming…";
        try {
          const res = await api(`/api/runs/${runId}/resume`, { body: {} });
          box.append(el("div", { class: "notice ok" }, el("b", null, "Resumed. "), `The run continues in the background (log ${res.log}). This page follows it.`));
          setTimeout(() => viewRun(runId), 2500);
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
        list.append(el("li", { style: "padding:10px 0" }, el("span", { class: "tag" }, ITEM_STAGE[x.stage] || words(x.stage || "")),
          el("div", { class: "main" }, el("div", { class: "title" }, x.title || wid), el("div", { class: "desc" }, stageText[x.stage] || "")),
          x.cr_url ? el("a", { class: "btn sm", href: x.cr_url, target: "_blank", rel: "noopener" }, `Open #${x.cr_id}`) : null));
      }
      box.append(list);
    }
    if (hb && hb.alive) {
      box.append(el("div", { class: "notice ok", style: "margin:0" }, el("b", null, "Watching. "), `Last checked ${ago(hb.last_poll_at)}.`,
        hb.last_tick && hb.last_tick.errors && hb.last_tick.errors.length ? ` Last error: ${hb.last_tick.errors[0].error}` : ""));
    } else if (hb && !w.pending.length) {
      box.append(el("div", { class: "notice info", style: "margin:0" }, el("b", null, "Watcher finished. "), `Last checked ${ago(hb.last_poll_at)}; nothing is left to watch.`));
    } else {
      if (hb) box.append(el("div", { class: "notice info", style: "margin:0" }, el("b", null, "Not watching. "), `The last watcher checked ${ago(hb.last_poll_at)} and is no longer running. Starting it again is safe.`));
      if (w.watchable) box.append(el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", onclick: async (ev) => {
        const b = ev.currentTarget; b.disabled = true; b.textContent = "Starting…";
        try {
          const res = await api(`/api/runs/${runId}/watch`, { body: {} });
          box.append(el("div", { class: "notice ok" }, el("b", null, "Watching. "), `The watcher runs in the background (log ${res.log}); this page follows it.`));
          setTimeout(() => viewRun(runId), 2500);
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
      [money(p.budget_usd), "the run's budget", "warn"], [money(next.cap_usd), `next: ${next.label || next.stage} (${plural(next.sessions || 1, "session")}), at most`],
      [money(needed), "needed to continue", "bad"]]));
    box.append(el("p", { class: "explain" }, "Every agent session carries its own hard cap, so the check is spent plus the next step's caps: the run can never go past the budget, and a step is refused before it starts rather than cut off midway. The caps are conservative; a step usually costs less than its cap."));
    const status = el("div");
    const err = el("div", { class: "err", role: "alert" });
    async function go(body, btn, label) {
      btn.disabled = true; btn.textContent = label; err.textContent = "";
      try {
        const res = await api(`/api/runs/${r.run_id}/resume`, { body });
        status.replaceChildren(el("div", { class: "notice ok" }, el("b", null, body.budget_usd != null ? `Budget raised to ${money(body.budget_usd)}. ` : "Decision applied. "),
          `The orchestrator continues in the background (log ${res.log}). This page follows it.`));
        setTimeout(() => viewRun(r.run_id), 2500);
      } catch (e) { btn.disabled = false; btn.textContent = btn.dataset.label; err.textContent = String(e.message || e); }
    }
    if (recorded) {
      const raise = recorded.decision === "approve";
      const amount = raise ? (String(recorded.comments || "").match(/(\d+(?:\.\d+)?)/) || [])[1] : null;
      const apply = el("button", { class: "btn primary", type: "button", dataset: { label: raise ? "Continue the run" : "Close the run" } }, raise ? "Continue the run" : "Close the run");
      apply.onclick = () => go({}, apply, raise ? "Continuing…" : "Closing…");
      box.append(el("div", { class: "notice info" }, el("b", null, raise ? `Raise to ${amount ? money(amount) : "a new budget"} recorded${recorded.decided_by ? " by " + recorded.decided_by : ""}. ` : `Close recorded${recorded.decided_by ? " by " + recorded.decided_by : ""}. `),
        "The orchestrator is not running while the run is paused; apply the decision to continue."),
        el("div", { class: "actions" }, apply), status, err);
    } else {
      const input = el("input", { type: "number", min: String(Math.ceil(needed)), step: "1", value: String(Math.ceil(needed)), "aria-label": "New budget in USD", style: "max-width:140px" });
      const raise = el("button", { class: "btn primary", type: "button", dataset: { label: "Raise the budget and continue" } }, "Raise the budget and continue");
      const close = el("button", { class: "btn danger", type: "button", dataset: { label: "Close the run" } }, "Close the run");
      raise.onclick = () => {
        const v = Number(input.value);
        if (!(v >= needed)) { err.textContent = `The next step needs at least ${money(needed)}.`; return; }
        go({ budget_usd: v }, raise, "Raising…");
      };
      const actions = el("div", { class: "actions", style: "align-items:center;gap:10px;flex-wrap:wrap" },
        el("label", { class: "help", style: "margin:0" }, "New budget in USD"), input, raise, close);
      // Closing is two steps, like every gate decision: the confirm appears after a beat where no first button was.
      close.onclick = () => {
        const yes = el("button", { class: "btn danger", type: "button", disabled: true, dataset: { label: "Yes, close the run" } }, "Yes, close the run");
        const cancel = el("button", { class: "btn", type: "button", onclick: () => confirmRow.replaceWith(actions) }, "Cancel");
        const confirmRow = el("div", { class: "confirm" }, el("div", { class: "what" }, "Close this run?"),
          el("div", { class: "explain" }, "Nothing more is built. The work so far stays in the files, and the branch on the code host is left as it is."),
          el("div", { class: "actions", style: "margin-top:10px" }, yes, cancel));
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
      case "gate.decided": return ["human", { main: `${GATE_SHORT[p.kind] || p.kind}: ${DECISION_LABEL[p.decision] || p.decision}`, detail: p.comments ? `"${p.comments.slice(0, 120)}"` : "" }];
      case "question.asked": return [who, { main: `The ${p.asked_by} asked a question`, detail: p.question }];
      case "question.answered": return ["human", { main: "Question answered", detail: p.answer }];
      case "judge.verdict": return [who, { main: `Spec judge round ${p.round}: ${p.result}`, detail: p.blocking ? `${p.blocking} blocking` : "" }];
      case "review.findings": return [who, { main: `Review round ${p.round}: ${p.count} findings`, detail: item }];
      case "review.response": { const n = {}; for (const d of (p.dispositions || [])) n[d] = (n[d] || 0) + 1;
        return [who, { main: `Implementer answered review round ${p.round}`, detail: Object.entries(n).map(([d, c]) => `${c} ${d.replace("_", " ")}`).join(", ") }]; }
      case "merge.verdict": return [who, { main: `Merge judge: ${p.result}`, detail: item }];
      case "cr.opened": return [who, { main: p.design ? "Design pull request opened" : `Pull request opened for ${item}`, detail: p.url }];
      case "cr.merged": return ["human", { main: `Merged ${item}${p.id ? ` (#${p.id})` : ""}`, detail: [p.merge_commit_sha ? `as ${String(p.merge_commit_sha).slice(0, 10)}` : "", p.approved === false ? "without an approving review" : ""].filter(Boolean).join(", ") }];
      case "cr.closed": return ["human", { main: `Pull request closed without merging for ${item}${p.id ? ` (#${p.id})` : ""}` }];
      case "deploy.started": return [who, { main: `Dev deploy started for ${item}${p.attempt ? ` (observation ${p.attempt + 1})` : ""}`, detail: p.pipeline_url || (p.pipeline_id ? `pipeline ${p.pipeline_id}` : "no pipeline yet") }];
      case "deploy.succeeded": return [who, { main: `Dev deploy succeeded for ${item}`, detail: p.environment_url }];
      case "deploy.failed": return [who, { main: `Dev deploy failed for ${item}`, detail: p.status ? words(p.status) : "" }];
      case "e2e.dev_passed": return [who, { main: `Dev checks passed for ${item}`, detail: p.results ? `${p.results.length} of ${p.results.length} passed on ${p.environment_url || "dev"}` : "" }];
      case "e2e.dev_failed": { const n = (p.results || []).length, bad = (p.results || []).filter(x => x.exit_code !== 0).length;
        return [who, { main: `Dev checks failed for ${item}`, detail: n ? `${bad} of ${n} failed on ${p.environment_url || "dev"}` : "" }]; }
      case "session.started": return [who, { main: `Agent session started: ${CREW_NAME[p.agent_key] || p.agent_key || p.agent_id || "agent"}`, detail: p.title || "", session_id: p.session_id }];
      case "session.finished": return [who, { main: `Agent session finished: ${p.agent_id || "agent"}`, detail: p.list_cost_usd != null ? money(p.list_cost_usd) : "", session_id: p.session_id }];
      case "work_item.stage": return [who, { main: `${ev.work_item_id}: ${ITEM_STAGE[p.stage] || words(p.stage)}` }];
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
    const [outs, cfg, examples] = await Promise.all([api("/api/outcomes"), state.config || api("/api/config"), api("/api/outcomes/examples")]);
    state.config = cfg;
    let app = null, appNotice = null;
    if (params.get("app")) {
      try { app = await api(appPath(params.get("app"))); appNotice = el("div", { class: "notice info" }, el("b", null, `Requesting a change to ${app.name}. `), "Its repository and hosted test setup are selected. Pick or write the outcome; the verified setup must be on the selected base branch before this run can start."); }
      catch (e) { appNotice = el("div", { class: "notice bad" }, el("b", null, "The app did not load. "), `${e.message}. Return to Apps before starting this app run.`); }
    }
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Start a run", "Three things decide how a run goes: what, where, and the guardrails. Everything else has defaults from the last calibration.",
      [el("span", { style: "display:inline-flex;align-items:center;gap:10px" }, crewFigure("implementer", "working", "s"), el("span", { class: "explain" }, "The crew is ready."))]));
    if (appNotice) frag.append(appNotice);

    // 1. what
    let outcomeId = params.get("outcome") || (outs.outcomes[0] || {}).outcome_id || null;
    let outcome = null;
    const choices = el("div", { class: "choices" });
    const choiceNodes = [];
    const pickOutcome = async (id) => {
      outcomeId = id; choiceNodes.forEach(n => n.classList.toggle("on", n.dataset.id === id));
      try { outcome = (await api(`/api/outcomes/${id}`)).outcome; } catch (e) { outcome = null; }
      syncRepo(); syncWho(); syncSummary();
    };
    for (const o of outs.outcomes) {
      const radio = el("input", { type: "radio", name: "outcome", checked: o.outcome_id === outcomeId, onchange: () => pickOutcome(o.outcome_id) });
      const n = el("label", { class: `choice ${o.outcome_id === outcomeId ? "on" : ""}`, dataset: { id: o.outcome_id } }, radio,
        el("span", { class: "txt" }, el("b", null, o.title || o.outcome_id), el("span", null, `${o.runs.length ? plural(o.runs.length, "run") + " so far." : "No runs yet."}${o.owner ? " Owner " + o.owner + "." : ""}`)));
      choiceNodes.push(n); choices.append(n);
    }
    const exampleLinks = el("div", { class: "actions", style: "margin-top:10px" }, examples.examples.map(e => el("a", { class: "btn sm", href: `#/outcomes/new?example=${encodeURIComponent(e.name)}`, title: e.title }, `Load the ${e.name} example`)), el("a", { class: "btn sm", href: "#/outcomes/new" }, "Write a new outcome"));
    const whatCard = card("1. What should be built", el("div", null, outs.outcomes.length ? choices : el("p", { class: "explain" }, "No saved outcomes yet. Load an example or write one."), exampleLinks));

    // 2. where: browse the repositories visible to the configured GitHub account.
    let repositories = [], nextRepoPage = 1, selectedRepo = null;
    const repoUrl = el("input", { type: "url", placeholder: "https://github.com/you/app", "aria-label": "Repository URL" });
    const repoNew = el("input", { type: "radio", name: "repo" }), repoExisting = el("input", { type: "radio", name: "repo", checked: true });
    const newName = el("input", { type: "url", placeholder: "https://github.com/you/factory-new-app", "aria-label": "New repository URL" });
    const searchRepos = el("input", { type: "search", placeholder: "Filter by owner or repository name", "aria-label": "Filter repositories" });
    const repoSelect = el("select", { "aria-label": "GitHub repository" }, el("option", { value: "" }, "Choose a repository"));
    const repoStatus = el("p", { class: "explain", role: "status", "aria-live": "polite", style: "margin:0" });
    const repoDetails = el("p", { class: "explain", style: "margin:0" });
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
    };
    const whereCard = card("2. Where it is built", el("div", { class: "stack" }, existingChoice, newChoice,
      el("p", { class: "explain", style: "margin:0;font-size:13px" }, "Only repositories this GitHub connection can access are shown. Archived and read-only repositories cannot be selected. The run works on a separate branch and opens a pull request.")));

    // 3. guardrails
    const budget = el("input", { type: "number", min: "5", step: "5", value: 60 });
    const preview = el("select", { disabled: !!app }, ...(app ? [el("option", {value:"managed", selected:true}, "Hosted sandbox · verified app setup")] : []), el("option", { value: "local" }, "Local, started by the orchestrator"), el("option", { value: "platform" }, "Platform deploy workflow"));
    const owner = (outcome && outcome.owner) || {};
    const whoBox = el("div", { class: "who" });
    const syncWho = () => { const ow = (outcome && outcome.owner) || {}; whoBox.replaceChildren(
      el("span", null, el("b", { class: "po" }, "Product owner"), el("span", null, ow.name || ow.handle || "the outcome's owner")),
      el("span", null, el("b", { class: "eng" }, "Engineer"), el("span", null, ow.handle || "the outcome's owner")),
      el("span", null, el("b", { class: "ops" }, "Ops"), el("span", { class: "explain" }, "nobody yet"))); };
    const guards = el("div", { class: "guards" },
      el("label", null, "Run budget (USD)", budget, el("span", { class: "h" }, "The last one-item run cost about $33. The run pauses and asks before it goes over.")),
      el("label", null, "Preview", preview, el("span", { class: "h" }, app ? "Application, database and browser tests run in a fresh hosted sandbox." : "Local runs app commands on this computer. Platform uses the repository deploy workflow.")),
      el("div", null, el("span", { style: "font-size:13px;color:var(--text-3);display:block;margin-bottom:6px" }, "Who decides"), whoBox, el("a", { href: "#/outcomes", style: "font-size:13px;font-weight:700" }, "Change on the outcome")));
    // more
    const passes = el("input", { type: "number", min: "1", max: "4", value: 2 }), rounds = el("input", { type: "number", min: "1", max: "4", value: 2 });
    const security = el("input", { type: "checkbox", checked: true }), parallel = el("input", { type: "number", min: "1", max: "6", value: 3 });
    const scale = el("input", { type: "number", min: "0.2", max: "3", step: "0.1", value: 1 });
    const more = el("div", { class: "tier", hidden: true },
      el("label", null, "Reviewer passes per round", passes), el("label", null, "Review rounds before the judge", rounds),
      el("label", { class: "check" }, security, "Security reviewer on every round"),
      el("label", null, "Items built at once", parallel), el("label", null, "Scale every per-stage cap by", scale));
    const iterations = el("input", { type: "number", min: "1", max: "6", value: 3 });
    const expert = el("div", { class: "tier", hidden: true },
      el("label", null, "Grader iterations per stage", iterations),
      el("div", { class: "explain", style: "grid-column:1 / -1;font-size:12.5px" }, "Effort per agent and the platform timeouts live in the agent definitions and the orchestrator's environment; they are not per-run knobs yet."));
    const moreBtn = el("button", { class: "btn link", type: "button", onclick: () => { more.hidden = !more.hidden; moreBtn.textContent = more.hidden ? "More controls: passes, rounds, caps" : "Fewer controls"; if (more.hidden) { expert.hidden = true; expertBtn.hidden = true; } else expertBtn.hidden = false; } }, "More controls: passes, rounds, caps");
    const expertBtn = el("button", { class: "btn link", type: "button", hidden: true, onclick: () => { expert.hidden = !expert.hidden; expertBtn.textContent = expert.hidden ? "Expert: iterations" : "Hide expert settings"; } }, "Expert: iterations");
    const guardsCard = card("3. Guardrails", el("div", { class: "stack" }, guards, el("div", null, moreBtn), more, el("div", null, expertBtn), expert));

    // summary + start
    const summary = el("ul", { class: "summary-list" });
    const notice = el("div");
    const syncSummary = () => {
      const repo = repoNew.checked ? (newName.value.trim() || "a new private repository") : (repoUrl.value.trim() || "the outcome's repository");
      summary.replaceChildren(
        el("li", null, el("b", null, outcome ? outcome.title : "No outcome selected")),
        el("li", null, "Into ", el("span", { class: "mono" }, repo.replace(/^https:\/\/github\.com\//, ""))),
        el("li", null, `Up to $${budget.value}; the run asks before going over`),
        el("li", null, "You decide at Gate 1 (plan) and Gate 2 (contracts); you review the pull request"),
        el("li", null, `${passes.value} reviewer${passes.value === "1" ? "" : "s"}${security.checked ? " and the security reviewer" : ""} on every round`));
    };
    [budget, passes, security, repoUrl, newName].forEach(i => i.addEventListener("input", syncSummary));
    const settings = () => ({ budget_usd: Number(budget.value) || undefined, preview: preview.value, reviewer_passes: Number(passes.value) || undefined, review_rounds: Number(rounds.value) || undefined,
      security_review: security.checked, parallel_items: Number(parallel.value) || undefined, grader_iterations: Number(iterations.value) || undefined, budget_scale: Number(scale.value) !== 1 ? Number(scale.value) : undefined });
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
      btn.disabled = true;
      try {
        await saveRepo();
        const s = await api("/api/runs", { body: { outcome_id: outcomeId, app_id: app ? app.app_id : undefined, mode, settings: mode === "demo" ? {} : settings() } });
        if (s.run_id) location.hash = `#/runs/${s.run_id}`;
        else { notice.append(el("div", { class: "notice ok" }, el("b", null, "Run started. "), "The crew is waking up; the run appears under Runs within a minute.")); setTimeout(() => { location.hash = "#/runs"; }, 4000); }
      } catch (e) { notice.append(el("div", { class: "notice bad" }, e.message)); btn.disabled = false; }
    }
    const startBtn = el("button", { class: "btn call", type: "button", style: "min-height:48px;font-size:16px", disabled: !cfg.can_run_live, title: cfg.can_run_live ? "" : "Needs the code host configured; see Settings", onclick: ev => go("live", ev.currentTarget) }, "Start the run");
    const designBtn = el("button", { class: "btn", type: "button", disabled: !cfg.can_run_design, title: cfg.can_run_design ? "" : "Needs the crew and API key; see Settings", onclick: ev => go("design", ev.currentTarget) }, "Design only: stop after Gate 2");
    const demoBtn = el("button", { class: "btn sm", type: "button", onclick: ev => go("demo", ev.currentTarget) }, "Run as a demo");
    const aside = el("aside", null,
      card("This run", el("div", { class: "stack" }, summary, el("div", { class: "btn-col" }, startBtn, designBtn), notice,
        el("p", { class: "explain", style: "margin:0;font-size:12.5px" }, "A design-only run costs about $8 and needs no repository access. ", demoBtn),
        (!cfg.can_run_live || !cfg.can_run_design) ? el("p", { class: "explain", style: "margin:0;font-size:12.5px" }, el("a", { href: "#/settings" }, "What is missing to run")) : null)),
      card(null, el("div", { class: "explain", style: "font-size:13px" }, el("b", { style: "display:block;color:var(--text);font-size:14px;margin-bottom:4px" }, "What you are not asked"),
        "Model choices, iteration caps, timeouts and per-stage budgets have defaults from the last calibration and live under More and Expert. A run never needs them changed to succeed.")));

    frag.append(el("div", { class: "start" }, el("main", null, whatCard, whereCard, guardsCard), aside));
    $main.replaceChildren(frag);
    stopPoll();
    if (outcomeId) await pickOutcome(outcomeId); else { syncWho(); syncSummary(); }
    syncWho();
    if (cfg.forge === "github") await loadRepos();
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
    const g = await api(`/api/gates/${gateId}`);
    const rec = g.gate, rv = g.review;
    const notes = new Map();
    const picks = new Map();
    const frag = document.createDocumentFragment();
    frag.append(pageHead(GATE_NAME[rec.kind] || rec.kind, g.run_title, rec.kind === "budget" ? null : [el("a", { class: "btn", href: `/gates/${gateId}/page`, target: "_blank", rel: "noopener" }, "Open as a page")],
      [el("a", { href: "#/inbox" }, "Board"), " / ", el("a", { href: `#/runs/${g.run_id}` }, g.run_title), " / opened ", ago(rec.opened_at)]));

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
    $main.replaceChildren(frag);
    startPoll(async () => { try { decide.update(await api(`/api/gates/${gateId}`)); } catch (e) { /* keep */ } }, 4000);
  }

  function glance(items) {
    return el("div", { class: "glance" }, items.map(([v, k, cls]) => el("div", { class: "g" }, el("div", { class: `v ${cls || ""}` }, v), el("div", { class: "k" }, k))));
  }
  function checklist(rows) {
    return el("ul", { class: "checklist" }, rows.map(([t, d]) => {
      const cb = el("input", { type: "checkbox", "aria-label": t });
      return el("li", null, cb, el("div", null, el("div", { class: "t" }, t), el("div", { class: "d" }, d)));
    }));
  }
  function summaryCard(label, text) {
    const body = el("div", { class: "text clamp" }, text);
    const more = el("button", { class: "btn sm more", type: "button", onclick: () => { body.classList.toggle("clamp"); more.textContent = body.classList.contains("clamp") ? "Read the whole summary" : "Show less"; } }, "Read the whole summary");
    const node = el("div", { class: "summary-card" }, el("h2", null, label), body, more);
    requestAnimationFrame(() => { if (body.scrollHeight <= body.clientHeight + 4) { body.classList.remove("clamp"); more.remove(); } });
    return node;
  }
  function noteField(id, placeholder, notes) {
    const ta = el("textarea", { placeholder, "aria-label": `Note on ${id}` });
    notes.set(id, ta);
    return el("div", { class: "note" }, el("label", null, "Your note on this item (goes to the agent: as feedback with Request changes, as guidance with Approve)"), ta);
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
    ])));

    const tbl = el("table", { class: "coverage" }, el("tr", null, el("th", null, "Success metric"), el("th", null, "Target"), el("th", null, "Work items that move it"), el("th", null, "Why")));
    for (const m of (metrics.length ? metrics : Object.keys(cov))) {
      const c = cov[m] || {}; const ids = c.work_item_ids || [];
      const target = ((o.success_metrics || []).find(x => x.metric === m) || {}).target || "";
      tbl.append(el("tr", null, el("td", null, el("b", null, m)), el("td", null, target), el("td", { class: ids.length ? "ok" : "gap" }, ids.length ? ids.join(", ") : "nothing"), el("td", null, c.rationale || "")));
    }
    doc.append(card("Metric coverage", tbl));

    const lvl = waves(items);
    const ordered = [...items].sort((a, b) => (lvl[a.work_item_id] - lvl[b.work_item_id]) || items.indexOf(a) - items.indexOf(b));
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
            el("span", null, el("b", null, "After: "), (w.depends_on || []).join(", ") || "nothing, can start first")),
          (w.touches || []).length ? el("details", null, el("summary", null, "Where it touches the code"), el("div", null, el("ul", { class: "paths" }, w.touches.map(t => el("li", null, typeof t === "string" ? t : JSON.stringify(t)))))) : null,
          noteField(w.work_item_id, "What should change about this item?", notes))));
    });
    doc.append(card("Work items, in the order they would be built", wrap, plural(items.length, "item")));

    const rest = el("div", { class: "stack" });
    if (openQ.length) rest.append(el("div", null, el("h3", null, "Open questions"), el("ul", { class: "reading" }, openQ.map(q => el("li", null, q.question, el("div", { class: "explain" }, `Default: ${q.default_if_unanswered || "none"}${q.blocking ? " (blocking)" : ""}`))))));
    if ((d.explicitly_out_of_scope || []).length) rest.append(el("div", null, el("h3", null, "Explicitly out of scope"), el("ul", { class: "reading" }, d.explicitly_out_of_scope.map(x => el("li", null, x)))));
    if ((d.assumptions || []).length || (d.risks || []).length) rest.append(el("div", null, el("h3", null, "Assumptions and risks"), el("ul", { class: "reading" }, [...(d.assumptions || []), ...(d.risks || [])].map(x => el("li", null, typeof x === "string" ? x : (x.risk || x.description || JSON.stringify(x)))))));
    if ((rv.checks || []).length) rest.append(el("div", null, el("h3", null, "Automatic checks"), el("ul", { class: "reading" }, rv.checks.map(c => el("li", null, el("span", { class: `tag ${c.passed ? "ok" : "bad"}` }, c.passed ? "passed" : "failed"), " ", el("code", null, c.check.split(":")[0]), " ", c.detail)))));
    if (rv.decomposition_md) rest.append(el("details", null, el("summary", { class: "explain", style: "cursor:pointer" }, "The full document the agent wrote (decomposition.md)"), el("div", { class: "reading", html: md(rv.decomposition_md) })));
    doc.append(card("Questions, scope, and checks", rest));
  }

  function renderSpecs(doc, rv, notes, picks, subsetAllowed) {
    const v = rv.verdict, specs = rv.specs, ov = v.overall || {};
    const issuesBy = Object.fromEntries((v.per_spec || []).map(ps => [ps.spec_id, ps]));
    const allIssues = (v.per_spec || []).flatMap(ps => ps.issues || []);
    const sev = s => allIssues.filter(i => i.severity === s).length;
    const userVisible = Object.values(specs).flatMap(s => s.acceptance_criteria || []).filter(a => a.user_visible).length;
    const totalAC = Object.values(specs).flatMap(s => s.acceptance_criteria || []).length;
    const notCovered = (v.goal_coverage || []).filter(g => g.assessment !== "covered");
    const openQ = Object.values(specs).flatMap(s => (s.open_questions || []).map(q => ({ ...q, sid: s.spec_id })));

    doc.append(card("What you are deciding", el("div", { class: "stack" },
      el("p", { class: "explain" }, el("strong", null, "The crew wrote one contract per work item; an independent judge checked the set. "), "A contract is what an agent will build to: acceptance criteria, interfaces, and which files it may touch. Approving a contract starts its implementation."),
      summaryCard("The judge's summary", ov.summary_for_human || ""),
      glance([[words(ov.result), "judge's verdict", ov.result === "pass" ? "ok" : "warn"], [Object.keys(specs).length, "contracts"],
        [`${totalAC} (${userVisible} user-visible)`, "acceptance criteria"], [sev("blocking") ? `${sev("blocking")} blocking` : sev("major") ? `${sev("major")} major` : allIssues.length, "issues the judge flagged", sev("blocking") ? "bad" : sev("major") ? "warn" : ""],
        [`${(v.goal_coverage || []).length - notCovered.length}/${(v.goal_coverage || []).length}`, "metrics covered by criteria", notCovered.length ? "warn" : "ok"]]))));

    doc.append(card("What to check", checklist([
      ["The judge's verdict and anything it flagged", allIssues.length ? `${allIssues.length} issues, listed under each contract. Blocking ones should be resolved before you approve.` : "The judge found nothing outstanding."],
      ["User-visible acceptance criteria", `${userVisible} criteria describe behaviour a user or operator will see. Read those as if they were the feature description.`],
      ["Product decisions hiding in the criteria", "Customer-facing wording, limits, timings, and defaults in a criterion are decisions you are making by approving."],
      ["Where each agent may change code", "Each contract lists allowed paths. If one reaches into something it should not, request changes."],
      ["Open questions and their defaults", openQ.length ? `${openQ.length} questions across the contracts will use their defaults unless you say otherwise.` : "None."],
    ])));

    const gc = el("table", { class: "coverage" }, el("tr", null, el("th", null, "Success metric"), el("th", null, "Covered?"), el("th", null, "By which criteria"), el("th", null, "Judge's note")));
    for (const g of v.goal_coverage || []) gc.append(el("tr", null, el("td", null, el("b", null, g.metric)), el("td", { class: g.assessment === "covered" ? "ok" : "gap" }, words(g.assessment)), el("td", { class: "mono explain" }, (g.covering_acs || []).join(", ")), el("td", null, g.note || "")));
    const coh = Object.entries(v.coherence || {}).flatMap(([bucket, items]) => (items || []).map(it => ({ bucket, it })));
    doc.append(card("Metrics and how the contracts fit together", el("div", { class: "stack" }, gc,
      coh.length ? el("div", null, el("h3", null, "Across the set"), el("ul", { class: "reading" }, coh.map(({ bucket, it }) => el("li", null, el("span", { class: "tag" }, words(bucket)), " ", typeof it === "string" ? it : (it.description || JSON.stringify(it)), it.related_spec_ids ? el("span", { class: "explain" }, ` (${it.related_spec_ids.join(", ")})`) : null)))) : null)));

    const wrap = el("div");
    Object.entries(specs).forEach(([sid, s], i) => {
      const ps = issuesBy[sid] || { issues: [] };
      const cb = el("input", { type: "checkbox", checked: true });
      picks.set(sid, cb);
      const acs = s.acceptance_criteria || [];
      const uv = acs.filter(a => a.user_visible).length;
      const blocking = (ps.issues || []).filter(x => x.severity === "blocking").length;
      const acTable = el("table", null, el("tr", null, el("th", null, "Criterion"), el("th", null, "Given, when, then"), el("th", null, "Verified by")));
      for (const ac of acs) acTable.append(el("tr", { class: "ac-row" },
        el("td", null, el("code", null, ac.ac_id), el("div", null, el("span", { class: `tag ${ac.user_visible ? "gate" : ""}` }, ac.user_visible ? "user-visible" : "internal"))),
        el("td", { class: "gwt" }, el("b", null, "Given"), ac.given, " ", el("b", null, "When"), ac.when, " ", el("b", null, "Then"), ac.then),
        el("td", null, words(ac.verification))));
      const issues = (ps.issues || []).map(x => el("div", { class: "issue" }, el("span", { class: `sev ${x.severity}` }, x.severity), x.description, x.suggested_resolution ? el("div", { class: "fix" }, "Suggested: " + x.suggested_resolution) : null));
      const tp = s.test_plan || {};
      wrap.append(el("div", { class: "item-card", id: `spec-${sid}` },
        el("div", { class: "ih" }, el("span", { class: "num" }, i + 1), el("span", { class: "t" }, s.title), el("span", { class: "id" }, sid),
          el("span", { class: "right" }, blocking ? el("span", { class: "tag bad" }, `${blocking} blocking`) : (ps.issues || []).length ? el("span", { class: "tag warn" }, plural((ps.issues || []).length, "issue")) : el("span", { class: "tag ok" }, "no issues"),
            subsetAllowed ? el("label", { class: "pick" }, cb, "Approve this contract") : null)),
        el("div", { class: "ib" },
          el("div", { class: "intent" }, s.goal),
          el("div", { class: "facts" }, el("span", null, el("b", null, `${acs.length} criteria`), `, ${uv} user-visible`), el("span", null, el("b", null, "Size: "), s.estimated_size || "?"),
            el("span", null, el("b", null, "After: "), ((s.dependencies || {}).specs || []).join(", ") || "nothing"), el("span", null, el("b", null, "May edit: "), plural((s.allowed_paths || []).length, "path"))),
          issues.length ? el("div", null, el("h3", { style: "font-size:13px;margin:8px 0 4px" }, "What the judge flagged"), ...issues) : null,
          el("details", { open: true }, el("summary", null, "Acceptance criteria"), el("div", null, acTable)),
          el("details", null, el("summary", null, `Interfaces (${(s.interfaces || []).length}), allowed paths (${(s.allowed_paths || []).length}), test plan`), el("div", null,
            (s.interfaces || []).length ? el("ul", { class: "reading" }, s.interfaces.map(x => el("li", null, el("code", null, x.name), ` ${words(x.kind)}, ${x.change}`, el("pre", { class: "block" }, x.definition)))) : null,
            el("h3", { style: "font-size:13px;margin:8px 0 4px" }, "Allowed paths"), el("ul", { class: "paths" }, (s.allowed_paths || []).map(p => el("li", null, p))),
            el("h3", { style: "font-size:13px;margin:8px 0 4px" }, "Test plan"), el("ul", { class: "paths" }, (tp.commands || []).map(c => el("li", null, c))),
            (tp.e2e || []).length ? el("ul", { class: "paths" }, tp.e2e.map(e => el("li", null, `${e.command}  (${e.target}, covers ${(e.covers_ac || []).join(", ")})`))) : null)),
          (s.open_questions || []).length ? el("details", null, el("summary", null, `Open questions (${s.open_questions.length})`), el("div", null, el("ul", { class: "reading" }, s.open_questions.map(q => el("li", null, q.question, el("div", { class: "explain" }, `Default: ${q.default_if_unanswered || "none"}`)))))) : null,
          rv.spec_md[sid] ? el("details", null, el("summary", null, "The contract as the agent wrote it"), el("div", { class: "reading", html: md(rv.spec_md[sid]) })) : null,
          noteField(sid, "What should change in this contract?", notes))));
    });
    doc.append(card("The contracts", wrap, plural(Object.keys(specs).length, "contract")));
  }

  function renderBudget(doc, rv, rec, g) {
    const p = rv.budget || {}, next = p.next || {};
    doc.append(card("What you are deciding", el("div", { class: "stack" },
      el("p", { class: "explain" }, el("strong", null, "The run stopped before starting an agent session that could take it past its budget. "),
        "Nothing is running. Approve with a new budget to continue from exactly where it stopped, or reject to close the run; the work so far stays in the files either way."),
      glance([[money(p.spent_usd), `spent across ${plural(p.sessions_finished || 0, "session")}`], [money(p.budget_usd), "the run's budget", "warn"],
        [money(next.cap_usd), `next: ${next.label || next.stage} (${plural(next.sessions || 1, "session")}), at most`], [money(p.needed_usd), "needed to continue", "bad"]]),
      el("p", { class: "explain" }, `Paused in the ${words(p.run_stage || "")} stage. Every session carries its own hard cap, so the check is spent plus the next step's caps: the run can never go past the budget. A step usually costs less than its cap.`),
      el("p", { class: "explain" }, "The decision is applied by the orchestrator when the run is continued: from the ", el("a", { href: `#/runs/${g.run_id}` }, "run page"), ", or with ", el("code", null, `factory resume ${g.run_id}`), "."))));
  }

  function renderDeploy(doc, rv, rec) {
    doc.append(card("What happened", el("div", { class: "stack" }, el("div", { class: "reading" }, rec.summary),
      rv.report ? el("pre", { class: "block" }, rv.report.log_excerpt || "(no log)") : null,
      rv.report && rv.report.environment_url ? el("p", null, "Environment: ", el("a", { href: rv.report.environment_url, target: "_blank", rel: "noopener" }, rv.report.environment_url)) : null)));
  }

  function buildDecide(g, notes, picks) {
    const rec = g.gate;
    const comment = el("textarea", { id: "decide-comment", placeholder: "Optional for Approve. Required for Request changes: say what should change." });
    const status = el("div");
    const error = el("div", { class: "err", role: "alert" });
    const counts = el("div", { class: "explain", style: "margin-top:6px" });
    const confirmBox = el("div", { class: "confirm", hidden: true });

    const isBudget = rec.kind === "budget";
    const needed = isBudget ? Number(((g.review || {}).budget || {}).needed_usd || 0) : 0;
    const budgetInput = isBudget ? el("input", { id: "decide-budget", type: "number", min: String(Math.ceil(needed)), step: "1", value: String(Math.ceil(needed)), "aria-label": "New budget in USD", style: "max-width:140px" }) : null;
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
      counts.textContent = [n ? `${plural(n, "note")} on items will be sent with Request changes` : "", picked !== null ? `${picked} of ${picks.size} contracts selected` : ""].filter(Boolean).join(". ");
      buttons.forEach(b => {
        if (b.dataset.decision === "approve_subset") b.hidden = picked === picks.size;
        if (b.dataset.decision === "approve" && picks.size) b.hidden = picked !== picks.size;
        if (b.dataset.decision === "request_changes") b.disabled = !composeComments();
      });
    };
    notes.forEach(ta => ta.addEventListener("input", refresh));
    picks.forEach(cb => cb.addEventListener("change", refresh));
    comment.addEventListener("input", refresh);

    const consequences = {
      gate_1_decomposition: { approve: "The crew starts writing a contract for every work item, with your notes as guidance. You get Gate 2 before any code.", approve_subset: "", request_changes: "The decomposer revises the plan using your notes and asks you again.", reject: "The run closes. Nothing else happens." },
      gate_2_spec_readiness: { approve: "The crew starts implementing all contracts on separate branches; pull requests follow.", approve_subset: "Selected contracts are implemented; the others go back to the writer with your notes.", request_changes: "The writers revise using your notes; the judge re-checks; you decide again.", reject: "The run closes. Nothing is built." },
      escalation: { approve: "Proceed with the contracts as they are.", approve_subset: "Selected contracts proceed; the rest go back.", request_changes: "The writers revise using your notes.", reject: "The run closes." },
      deploy_failure: { approve: "The failure was the platform's; the next deploy is watched.", request_changes: "An agent fixes it on a new branch; a follow-up pull request follows.", reject: "This work item is closed." },
      budget: { approve: "The run's budget becomes the amount above; when continued, it picks up at the step it stopped before.", reject: "The run closes when continued. Nothing more is built; the files stay." },
    }[rec.kind] || {};
    const buttons = rec.options.map(opt => el("button", { class: `btn block ${opt === "approve" || opt === "approve_subset" ? "approve" : opt === "request_changes" ? "changes" : "danger"}`,
      type: "button", dataset: { decision: opt }, onclick: () => arm(opt) }, DECISION_LABEL[opt] || opt));

    function arm(opt) {
      error.textContent = "";
      const comments = composeComments();
      if (opt === "request_changes" && !comments) { error.textContent = "Say what should change first."; return; }
      if (isBudget && opt === "approve" && !(Number(budgetInput.value) >= needed)) { error.textContent = `The next step needs at least ${money(needed)}.`; return; }
      buttons.forEach(b => b.hidden = true);
      const go = el("button", { class: `btn block ${opt === "reject" ? "danger" : opt === "request_changes" ? "changes" : "approve"}`, type: "button", disabled: true, onclick: () => decide(opt, comments) }, `Yes, ${(DECISION_LABEL[opt] || opt).toLowerCase()}`);
      confirmBox.replaceChildren(el("div", { class: "what" }, `${DECISION_LABEL[opt] || opt}?`),
        el("div", { class: "explain" }, consequences[opt] || ""),
        el("div", { class: "btn-col", style: "margin-top:10px" }, go, el("button", { class: "btn block", type: "button", onclick: disarm }, "Cancel")));
      confirmBox.hidden = false;
      setTimeout(() => { go.disabled = false; go.focus(); }, 600);
    }
    function disarm() { confirmBox.hidden = true; buttons.forEach(b => b.hidden = false); refresh(); }
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
      status.replaceChildren(el("div", { class: `recorded ${cls(d.decision)}` }, el("b", null, `${DECISION_LABEL[d.decision] || d.decision} recorded`),
        `${ago(d.decided_at)}. ${isBudget ? "The run is parked: continue it from its page to apply the decision. " : "The run picks it up within a few seconds. "}`,
        el("a", { href: `#/runs/${g.run_id}` }, isBudget ? "Go to the run" : "Back to the run")));
      form.hidden = true;
    }
    function showDecided(p) {
      status.replaceChildren(el("div", { class: `recorded ${cls(p.decision)}` }, el("b", null, `${DECISION_LABEL[p.decision] || p.decision}, applied`),
        "The run has moved on. ", el("a", { href: `#/runs/${g.run_id}` }, "See where it is now."), p.comments ? el("div", { class: "explain", style: "margin-top:6px" }, `“${p.comments}”`) : null));
      form.hidden = true;
    }

    const form = el("div", null,
      el("p", { class: "explain" }, Object.entries(consequences).filter(([k]) => rec.options.includes(k) && consequences[k]).map(([k, v]) => el("span", { style: "display:block;margin-bottom:4px" }, el("b", null, DECISION_LABEL[k] + ": "), v))),
      isBudget ? el("div", { style: "margin-bottom:10px" }, el("label", { for: "decide-budget" }, "New budget in USD"), budgetInput) : null,
      el("label", { for: "decide-comment" }, "Your comment"), comment, counts,
      confirmBox, el("div", { class: "btn-col", style: "margin-top:12px" }, buttons), error,
      el("details", null, el("summary", null, "Why this is safe to approve"),
        el("div", { class: "explain", style: "margin-top:6px" }, "Your decision is tied to the exact files you are looking at, by fingerprint. If an agent changed one after this page loaded, the decision is refused and the gate reopens, so you can never approve something you did not read."),
        el("ul", { class: "files" }, rec.artifacts.map(a => el("li", null, el("span", null, a.name), el("code", { title: a.sha256 }, a.sha256.slice(0, 10)))))));
    const node = el("aside", { class: "decide" }, card("Your call", el("div", null, status, form), null, "call"));
    if (g.decided) showDecided(g.decided);
    else if (rec.recorded) showRecorded(rec.recorded);
    refresh();
    return { node, update(fresh) { if (fresh.decided) showDecided(fresh.decided); } };
  }

  // ------------------------------------------------------------------ outcomes
  async function viewOutcomes() {
    setNav("outcomes");
    const [outs, cfg] = [(await api("/api/outcomes")).outcomes, state.config || await api("/api/config")];
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Outcomes", "An outcome is the business result you want, with metrics. Each run starts from one.",
      [el("a", { class: "btn", href: "#/outcomes/new?example=sample-app" }, "Load the sample app outcome"), el("a", { class: "btn", href: "#/outcomes/new?example=switchboard" }, "Load the Switchboard example"), el("a", { class: "btn primary", href: "#/outcomes/new" }, "New outcome")]));
    if (!outs.length) {
      frag.append(card(null, el("div", { class: "empty" }, el("h3", null, "No outcomes saved yet"), el("div", null, "Write one in your own words, or load an example."),
        el("a", { class: "btn primary", href: "#/outcomes/new?example=sample-app" }, "Load the sample app outcome"))));
    } else {
      const list = el("ul", { class: "list" });
      for (const o of outs) {
        list.append(el("li", null, el("div", { class: "main" }, el("div", { class: "title" }, el("a", { href: `#/outcomes/new?outcome=${o.outcome_id}` }, o.title || o.outcome_id)),
          el("div", { class: "desc" }, `${o.owner ? "Owner " + o.owner + ". " : ""}${o.runs.length ? plural(o.runs.length, "run") + ": " + o.runs.map(r => r.slice(4, 12)).join(", ") : "No runs yet."}`)),
          el("a", { class: "btn primary", href: `#/start?outcome=${o.outcome_id}` }, "Start a run")));
      }
      frag.append(card("Saved outcomes", list, plural(outs.length, "outcome")));
    }
    $main.replaceChildren(frag);
    stopPoll();
  }

  async function viewOutcomeForm(params) {
    setNav("outcomes");
    const which = params.get("example") === "1" ? "sample-app" : params.get("example");
    const t = (await api(`/api/outcomes/template${which ? "?example=" + encodeURIComponent(which) : ""}`)).outcome;
    const example = t._example;
    const cfg = state.config || await api("/api/config");
    let o = JSON.parse(JSON.stringify(t)); delete o._example;
    if (which) o = JSON.parse(JSON.stringify(example));
    if (params.get("outcome")) { try { o = (await api(`/api/outcomes/${params.get("outcome")}`)).outcome; } catch (e) { /* fall through */ } }
    let savedId = params.get("outcome") && o.outcome_id ? o.outcome_id : null;

    const frag = document.createDocumentFragment();
    frag.append(pageHead(savedId ? o.title : "New outcome", "Describe the result you want in your own words. The factory decomposes it into work items; you approve the plan before anything is specified or built.", null,
      [el("a", { href: "#/outcomes" }, "Outcomes"), " / ", savedId ? "Edit" : "New"]));

    const form = el("form", { class: "form stack", onsubmit: e => e.preventDefault() });
    const f = {};
    const field = (key, label, help, input) => { f[key] = input; input.id = `f-${key}`; return el("div", { class: "field" }, el("label", { class: "lab", for: input.id }, label), help ? el("p", { class: "help" }, help) : null, input); };
    const statement = el("textarea", { class: "big" }); statement.value = o.statement || "";
    form.append(card("The outcome", el("div", null,
      field("statement", "What should be true when this is done?", "Plain language, from the business side. The decomposer reads this first.", statement),
      field("title", "Short title", null, el("input", { type: "text", value: o.title || "" })))));

    const metricsBox = el("div");
    const metricRows = [];
    const addMetric = (m = {}) => {
      const row = { metric: el("input", { type: "text", value: m.metric || "", placeholder: "Involuntary churn rate" }),
        baseline: el("input", { type: "text", value: m.baseline || "", placeholder: "4.1% per month (leave empty if unknown)" }),
        target: el("input", { type: "text", value: m.target || "", placeholder: "Below 2.9% within two months" }),
        measurement: el("input", { type: "text", value: m.measurement || "", placeholder: "Where it is read from" }) };
      const node = el("div", { class: "metric" },
        el("div", { class: "grid2" }, el("div", null, el("label", { class: "help" }, "Metric"), row.metric), el("div", null, el("label", { class: "help" }, "Target"), row.target)),
        el("div", { class: "grid2" }, el("div", null, el("label", { class: "help" }, "Baseline today"), row.baseline), el("div", null, el("label", { class: "help" }, "How it is measured"), row.measurement)),
        el("button", { class: "rm", type: "button", onclick: () => { node.remove(); metricRows.splice(metricRows.indexOf(row), 1); } }, "Remove this metric"));
      metricRows.push(row); metricsBox.append(node);
    };
    (o.success_metrics && o.success_metrics.length ? o.success_metrics : [{}]).forEach(addMetric);
    form.append(card("Success metrics", el("div", null, el("p", { class: "help" }, "Each needs a target. A vague metric is refused."), metricsBox, el("button", { class: "add", type: "button", onclick: () => addMetric() }, "Add a metric"))));

    const c = o.constraints || {};
    f.must_not = el("textarea"); f.must_not.value = (c.must_not || []).join("\n");
    f.compliance = el("textarea"); f.compliance.value = (c.compliance || []).join("\n");
    f.budget = el("input", { type: "number", min: "1", value: c.budget_usd ?? 150 });
    f.deadline = el("input", { type: "date", value: c.deadline || "" });
    form.append(card("Constraints", el("div", null,
      el("div", { class: "grid2" }, el("div", null, el("label", { class: "help" }, "Must not (one per line)"), f.must_not), el("div", null, el("label", { class: "help" }, "Compliance (one per line)"), f.compliance)),
      el("div", { class: "grid2", style: "margin-top:10px" }, el("div", null, el("label", { class: "help" }, "Budget in USD for agent spend"), f.budget), el("div", null, el("label", { class: "help" }, "Deadline"), f.deadline)))));

    const repo = (o.target_repositories || [{}])[0] || {};
    f.repo = el("input", { type: "text", value: repo.url || "", placeholder: "https://github.com/you/app" });
    f.branch = el("input", { type: "text", value: repo.default_branch || "main" });
    f.paths = el("textarea"); f.paths.value = (repo.paths_in_scope || []).join("\n");
    f.create = el("input", { type: "checkbox", checked: !!repo.create });
    f.visibility = el("select", null, el("option", { value: "private", selected: repo.visibility !== "public" }, "private"), el("option", { value: "public", selected: repo.visibility === "public" }, "public"));
    form.append(card("Repository the agents will change", el("div", null,
      el("p", { class: "help" }, "An existing repository, or one the factory creates for you on the code host before the run starts. The URL names it either way."),
      el("div", { class: "grid2" }, el("div", null, el("label", { class: "help" }, "HTTPS URL"), f.repo), el("div", null, el("label", { class: "help" }, "Default branch"), f.branch)),
      el("div", { class: "grid2", style: "margin-top:10px" },
        el("label", { class: "pick", style: "font-weight:500" }, f.create, "Create this repository if it does not exist"),
        el("div", null, el("label", { class: "help" }, "Visibility when created"), f.visibility)),
      el("label", { class: "help", style: "margin-top:10px" }, "Paths in scope (one per line, globs allowed). Agents may only change these. Leave empty for a new repository."), f.paths)));

    const ow = o.owner || {};
    f.oname = el("input", { type: "text", value: ow.name || "" }); f.ohandle = el("input", { type: "text", value: ow.handle || "", placeholder: "your GitHub login" }); f.orole = el("input", { type: "text", value: ow.role || "" });
    form.append(card("Owner (who decides the gates)", el("div", { class: "grid3" }, el("div", null, el("label", { class: "help" }, "Name"), f.oname), el("div", null, el("label", { class: "help" }, "Code host handle"), f.ohandle), el("div", null, el("label", { class: "help" }, "Role"), f.orole))));

    const notice = el("div");
    const lines = ta => ta.value.split("\n").map(s => s.trim()).filter(Boolean);
    const collect = () => ({
      outcome_id: savedId || "", title: f.title.value.trim(), statement: f.statement.value.trim(),
      success_metrics: metricRows.map(r => ({ metric: r.metric.value.trim(), baseline: r.baseline.value.trim() || null, target: r.target.value.trim(), measurement: r.measurement.value.trim() })).filter(m => m.metric || m.target),
      constraints: { must_not: lines(f.must_not), compliance: lines(f.compliance), budget_usd: Number(f.budget.value) || undefined, deadline: f.deadline.value || undefined },
      context_links: o.context_links || [], clarifications: o.clarifications || [],
      target_repositories: [{ url: f.repo.value.trim(), default_branch: f.branch.value.trim() || "main", paths_in_scope: lines(f.paths),
        create: f.create.checked || undefined, visibility: f.create.checked ? f.visibility.value : undefined }],
      owner: { name: f.oname.value.trim(), handle: f.ohandle.value.trim(), role: f.orole.value.trim() },
      created_at: o.created_at || undefined,
    });
    async function save() {
      notice.replaceChildren();
      const r = await api("/api/outcomes", { body: { outcome: collect() } });
      if (!r.ok) { notice.replaceChildren(el("div", { class: "notice bad" }, el("b", null, "Not saved yet. "), ...r.errors.map(e => el("div", null, e)))); return null; }
      savedId = r.outcome_id;
      notice.replaceChildren(el("div", { class: "notice ok" }, el("b", null, "Saved. "), el("a", { href: `#/start?outcome=${savedId}` }, "Start a run from it.")));
      return r;
    }
    const saveBtn = el("button", { class: "btn primary", type: "button", onclick: save }, "Save");
    const startLink = el("button", { class: "btn call", type: "button", onclick: async () => { const r = await save(); if (r) location.hash = `#/start?outcome=${savedId}`; } }, "Save and start a run");
    form.append(card("Save", el("div", null, notice, el("div", { class: "startbar" }, saveBtn, el("span", { class: "spacer" }), startLink))));
    frag.append(form);
    $main.replaceChildren(frag);
    stopPoll();
  }

  // ------------------------------------------------------------------ apps: learn once how to build, run and test an app, then runs use it
  const APP_STATUS = {
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
    ["Evidence", "Results and screenshots from that run."],
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
    return { label, text: text || discovering, cls };
  }
  function appStatusTag(a) { const s = appStatusText(a); return el("span", { class: `tag ${s.cls}` }, s.label); }
  function appMoney(a) { return `${money(a.spent_usd || 0)} of ${money(a.budget_usd)}`; }
  function appLeft(a) { return money(a.remaining_usd ?? Math.max(0, (a.budget_usd || 0) - (a.spent_usd || 0))); }
  function appKey(a) { return JSON.stringify([a.status, a.phase, a.updated_at, a.plan_sha256, a.spent_usd, (a.sessions || []).length, (a.credentials || []).map(c => [c.name, c.status]), a.pr_url, a.error]); }
  function onboardingFacts() {
    return card("What onboarding does, and what it does not", el("ul", { class: "facts" },
      el("li", null, el("b", null, "Runs hosted. "), "Discovery and verification are agent sessions in a hosted sandbox, not on this computer."),
      el("li", null, el("b", null, "Synthetic data only. "), "Verification uses generated test data and test-only keys, never production data or real accounts."),
      el("li", null, el("b", null, "Charged to the app's budget. "), "Discovery and verification spend from the budget you set. Each session and its cost is listed on the app page."),
      el("li", null, el("b", null, "People still decide. "), "The setup arrives as a pull request for an engineer to review and merge. Gate 1 (the plan), Gate 2 (the contracts) and every merge stay with people.")), null, "soft");
  }

  async function viewApps() {
    setNav("apps");
    const apps = (await api("/api/apps")).apps || [];
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Apps", "An app is a repository the factory has learned to build, run and test. Onboarding finds that setup, verifies it once in a hosted sandbox, and proposes it as a pull request. Runs on the app then use it to check their work.",
      [el("a", { class: "btn primary", href: "#/apps/new" }, "Add an app")]));
    if (!apps.length) {
      frag.append(card(null, el("div", { class: "empty" }, crewFigure("codebase_researcher", "asleep", "l"), el("h3", null, "No apps yet"),
        el("div", null, "Add an existing GitHub repository, or describe a new app. An agent works out how to test it; you review before anything is verified or published."),
        el("a", { class: "btn primary", href: "#/apps/new" }, "Add an app"))));
    } else {
      const tbl = el("table", null, el("tr", null, el("th", null, "App"), el("th", null, "Where it is"), el("th", null, "Spend"), el("th", null, "Last activity")));
      for (const a of apps) {
        tbl.append(el("tr", { class: "link", onclick: () => { location.hash = appHref(a.app_id); } },
          el("td", null, el("div", { class: "title" }, el("a", { href: appHref(a.app_id) }, a.name || a.app_id), el("span", { class: "tag" }, appKind(a.kind))),
            el("div", { class: "explain mono" }, repoName(a.repository_url))),
          el("td", null, appStatusTag(a), a.phase ? el("div", { class: "explain" }, a.phase) : null),
          el("td", null, appMoney(a)), el("td", { class: "explain" }, ago(a.updated_at))));
      }
      frag.append(card("All apps", tbl, plural(apps.length, "app")));
    }
    frag.append(onboardingFacts());
    $main.replaceChildren(frag);
    const listKey = list => JSON.stringify(list.map(a => [a.app_id, a.status, a.phase, a.updated_at, a.spent_usd]));
    state.lastKey.apps = listKey(apps);
    startPoll(refreshWhenChanged("apps", async () => listKey((await api("/api/apps")).apps || []), viewApps), apps.some(a => APP_BUSY.has(a.status)) ? 4000 : 15000);
  }

  // The authenticated GitHub repository list, searchable and paged, with a typed URL as the alternative.
  function repositoryPicker(onPick) {
    let repositories = [], nextPage = 1;
    const search = el("input", { type: "search", id: "app-repo-search", placeholder: "Filter by owner or repository name", autocomplete: "off" });
    const select = el("select", { id: "app-repo-select" }, el("option", { value: "" }, "Choose a repository"));
    const status = el("p", { class: "explain", role: "status", "aria-live": "polite", style: "margin:0" }, "Loading GitHub repositories…");
    const details = el("p", { class: "explain", style: "margin:0" });
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
    return { node, load };
  }

  async function viewAppNew() {
    setNav("apps");
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
    const picker = repositoryPicker(r => { picked = r; if (!nameTouched && r) name.value = r.name; sync(); });
    const newUrl = el("input", { type: "url", id: "app-new-url", placeholder: "https://github.com/you/new-app", autocomplete: "off", spellcheck: "false" });
    const newPanel = el("div", { class: "stack", hidden: true },
      el("label", { class: "lab", for: "app-new-url" }, "Where the new repository will live"),
      el("p", { class: "help" }, "HTTPS form: https://github.com/owner/name. Nothing is created now; the private repository is created when you publish the setup."), newUrl);
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
      existingBox.classList.toggle("on", !isNew); newBox.classList.toggle("on", isNew);
      existingPanel.hidden = isNew; newPanel.hidden = !isNew;
      descLabel.textContent = isNew ? "What the new app should do" : "What the app is for";
      descHelp.textContent = isNew ? "Two or three sentences. The agent proposes a scaffold from this; you review it before anything is created."
        : "One or two sentences. They help the agent pick the user journeys worth testing.";
      sync();
    };
    kindExisting.onchange = kindNew.onchange = syncKind;
    [newUrl, description, budget, revision].forEach(i => i.addEventListener("input", () => sync()));

    const whichCard = card("1. Which app", el("div", { class: "stack" }, el("div", { class: "choices" }, existingBox, newBox), existingPanel, newPanel));
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
      repository_url: (kindNew.checked ? newUrl.value : (picked ? picked.url : "")).trim().replace(/\/+$/, "").replace(/\.git$/, ""),
      description: description.value.trim(), budget_usd: Number(budget.value), ...(kindNew.checked || !revision.value.trim() ? {} : { revision: revision.value.trim() }) });
    const problems = b => [
      !b.name ? "Give the app a name." : null,
      !b.repository_url ? (b.kind === "greenfield" ? "Say where the new repository will live." : "Choose a repository or enter its URL.") : null,
      b.repository_url && !GITHUB_HTTPS.test(b.repository_url) ? "The repository URL must look like https://github.com/owner/name." : null,
      !b.description ? (b.kind === "greenfield" ? "Describe what the new app should do." : "Say in a sentence what the app is for.") : null,
      !(b.budget_usd > 0 && b.budget_usd <= 60) ? "Set a budget above $0 and at most $60." : null,
      b.revision && !/^[A-Za-z0-9_.\-/]{1,180}$/.test(b.revision) ? "The source branch or commit may use only letters, digits, and . _ - /" : null,
    ].filter(Boolean);
    function sync() {
      const b = body();
      summary.replaceChildren(
        el("li", null, el("b", null, b.name || "An unnamed app"), ` · ${appKind(b.kind).toLowerCase()}`),
        el("li", null, b.repository_url ? el("span", { class: "mono" }, repoName(b.repository_url)) : "No repository chosen yet", b.revision ? el("span", { class: "mono" }, ` at ${b.revision}`) : null),
        el("li", null, `Discovery starts at once and is charged to the ${b.budget_usd > 0 ? money(b.budget_usd) : "$0"} budget`),
        el("li", null, "Verification and publishing wait for your click"));
    }
    const addBtn = el("button", { class: "btn call", type: "button", style: "min-height:48px;font-size:16px" }, "Add the app and start discovery");
    addBtn.onclick = async () => {
      notice.replaceChildren();
      const b = body(), bad = problems(b);
      if (bad.length) { notice.append(el("div", { class: "notice bad" }, el("b", null, "Not added yet. "), ...bad.map(p => el("div", null, p)))); return; }
      addBtn.disabled = true; addBtn.textContent = "Adding the app…";
      let app;
      try { app = await api("/api/apps", { body: b }); } catch (e) {
        notice.append(el("div", { class: "notice bad" }, el("b", null, "Not added. "), e.message));
        addBtn.disabled = false; addBtn.textContent = "Add the app and start discovery"; return;
      }
      addBtn.textContent = "Starting discovery…";
      try { await api(`${appPath(app.app_id)}/discover`, { body: {} }); } catch (e) {
        state.flash = { appId: app.app_id, text: `The app was added, but discovery did not start: ${e.message}` };
      }
      location.hash = appHref(app.app_id);
    };
    const aside = el("aside", null,
      card("When you click", el("div", { class: "stack" }, summary, addBtn,
        el("p", { class: "explain", style: "margin:0;font-size:12.5px" }, "This starts a charged agent session. It reads or proposes code only; it changes nothing in the repository."), notice)),
      onboardingFacts());
    frag.append(el("div", { class: "start" }, el("div", { class: "start-main" }, whichCard, aboutCard, budgetCard), aside));
    $main.replaceChildren(frag);
    syncKind();
    picker.load();
  }

  // ---- one app: the steps so far, the agent's status, and the next thing a person can do
  function appStepper(a) {
    const s = a.status, plan = a.plan || null;
    let idx = { connected: 1, discovering: 1, needs_input: 2, setup_ready: 3, verifying: 3, ready: 5, ready_limited: 5, publishing: 5 }[s] ?? 1;
    if (a.pr_url && s !== "publishing") idx = APP_STEPS.length;
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
  function appAgentRole(a) { return a.status === "verifying" ? "reviewer" : a.status === "publishing" ? "integrator" : "codebase_researcher"; }
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
      a.phase && a.phase !== s.text ? el("p", { class: "explain", style: "margin:8px 0 0" }, s.text) : null,
      el("p", { class: "explain", style: "margin:8px 0 0;font-size:12.5px" }, `Updated ${ago(a.updated_at) || "just now"}.`),
      el("dl", { class: "kv app-money" }, el("dt", null, "Budget"), el("dd", null, money(a.budget_usd)), el("dt", null, "Spent"), el("dd", null, money(a.spent_usd || 0)),
        el("dt", null, "Available"), el("dd", null, appLeft(a))),
      a.reserved_usd ? el("p", { class: "explain" }, `${money(a.reserved_usd)} is reserved for the current session.`) : null,
      a.cost_warning ? el("p", { class: "notice warn" }, a.cost_warning) : null,
      (a.sessions || []).length ? el("div", null, el("h3", { class: "mini-h" }, "Charged sessions"), sessions)
        : el("p", { class: "explain", style: "margin:8px 0 0;font-size:12.5px" }, "No agent session has run yet."));
  }
  function capabilityList(plan) {
    const ul = el("ul", { class: "caps" });
    for (const c of plan.capabilities || []) {
      ul.append(el("li", null, el("span", { class: `tag ${c.available ? "ok" : "warn"}` }, c.available ? "Testable" : "Not testable here"),
        el("span", { class: "cap" }, c.label || c.id), c.limitation ? el("span", { class: "lim" }, c.limitation) : null));
    }
    return ul;
  }
  // only what the reviewed test recipe declares (test_environment.schema.json: name, required, allowed_hosts, injection, description)
  function declaredSecrets(plan) {
    const list = plan && plan.profile && Array.isArray(plan.profile.secrets) ? plan.profile.secrets : [];
    return list.filter(x => x && typeof x.name === "string" && SECRET_NAME.test(x.name))
      .map(x => ({ name: x.name, required: x.required === true, hosts: Array.isArray(x.allowed_hosts) ? x.allowed_hosts.map(String) : [],
        injection: x.injection ? String(x.injection) : "", purpose: x.description ? String(x.description) : "" }));
  }
  function evidenceGallery(a) {
    const grid = el("div", {class:"stack"}), videos = el("div", {class:"evidence-grid"}), shots = el("div", {class:"evidence-grid"}), files = el("div", {class:"stack"});
    let shotCount = 0, fileCount = 0;
    for (const e of a.evidence || []) {
      const url = typeof e.url === "string" && e.url.startsWith(`${appPath(a.app_id)}/evidence/`) ? e.url : null;
      const nm = String(e.name || (url || "").split("/").pop() || "evidence");
      if (!url) { grid.append(el("figure", null, el("figcaption", null, `${nm} (not a Console evidence link, not shown)`))); continue; }
      const ext = nm.toLowerCase().split(".").pop();
      let media;
      if (["png", "jpg", "jpeg", "webp"].includes(ext)) media = el("a", { href: url, target: "_blank", rel: "noopener" }, el("img", { src: url, alt: nm, loading: "lazy" }));
      else if (["mp4", "webm"].includes(ext)) media = el("video", { src: url, controls: true, preload: "metadata", playsinline: true, "aria-label": `Recording: ${nm}` });
      else if (ext === "gif") media = el("details", null, el("summary", null, "Play the recording (GIF)"), el("img", { src: url, alt: `Recording: ${nm}`, loading: "lazy" }));
      else media = null;
      const figure = el("figure", null, media, el("figcaption", null, el("a", { href: url, target: "_blank", rel: "noopener" }, nm)));
      if (["mp4","webm","gif"].includes(ext)) videos.append(figure);
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
      try { const a = await call(); line.textContent = ""; paint(a && a.app_id ? a : await api(appPath(appId))); }
      catch (e) { line.textContent = e.status === 409 ? `The agent is already working on this app; the page follows it. (${e.message})` : e.message; btn.disabled = false; if (e.status === 409) tick(); }
    };
    const actionLine = () => el("p", { class: "explain action-line", role: "status", "aria-live": "polite" });
    const charged = text => el("p", { class: "explain", style: "margin:6px 0 0;font-size:12.5px" }, text);

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
        const btn = el("button", { class: "btn call", type: "button", disabled: busy() }, plan ? "Discover again" : "Start discovery");
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
        const line = actionLine(), btn = el("button", {class:"btn call",type:"button"}, "Revise this setup");
        btn.onclick = () => {
          if (!feedback.value.trim()) { line.textContent = "Describe the change first."; return; }
          act(btn,line,async () => {
            await api(`${appPath(appId)}/feedback`, {body:{text:feedback.value.trim()}});
            delete drafts.feedback;
            return api(`${appPath(appId)}/discover`, {body:{}});
          });
        };
        out.push(card("Improve or repair the setup", el("details", {open:a.status === "failed" || !!a.feedback},
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
        el("dl", { class: "kv", style: "margin-top:10px" }, el("dt", null, "Plan fingerprint"), el("dd", { class: "mono" }, a.plan_sha256 || "out of date until discovery runs again"),
          el("dt", null, "Source commit inspected"), el("dd", { class: "mono" }, a.source_sha || "not known yet"),
          el("dt", null, "Test recipe file"), el("dd", { class: "mono" }, plan.profile_path || "none yet")),
        (plan.files || []).length ? el("div", null, el("h3", { class: "mini-h" }, `Proposed files (${(plan.files || []).length})`), files) : null,
        plan.profile ? el("details", null, el("summary", null, "Raw test recipe JSON"), el("pre", null, JSON.stringify(plan.profile, null, 2))) : null,
        el("details", null, el("summary", null, "Raw plan JSON"), el("pre", null, JSON.stringify(plan, null, 2))));
      out.push(card("What the agent found", el("div", { class: "stack" },
        !a.plan_sha256 ? el("div", { class: "notice info", style: "margin:0" }, el("b", null, "This plan is out of date. "), "Your answers are saved. Verification and publishing wait until discovery runs again with them.") : null,
        el("p", { class: "lead" }, (plan.summary || "No summary given.").split(/\n\s*\n/)[0]),
        (plan.summary || "").includes("\n\n") ? el("details", null, el("summary", null, "Read the agent’s full setup explanation"), el("p", {class:"reading"}, plan.summary)) : null,
        plan.app_kind ? el("p", { class: "explain" }, `Kind of app: ${words(plan.app_kind)}`) : null,
        (plan.journeys || []).length ? el("div", null, el("h3", { class: "mini-h" }, "User journeys it would test"), journeys) : null,
        (plan.capabilities || []).length ? el("div", null, el("h3", { class: "mini-h" }, "What a test run can exercise"), capabilityList(plan)) : null,
        (plan.limitations || []).length ? el("div", null, el("h3", { class: "mini-h" }, "Limits of this setup"), el("ul", { class: "plain-list" }, plan.limitations.map(x => el("li", null, x)))) : null,
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
        const againBtn = el("button", { class: "btn call", type: "button", disabled: busy() }, "Save and discover again");
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
            el("p", { class: "explain", style: "margin:0" }, sec.purpose ? `Unlocks: ${sec.purpose}` : "The recipe does not say what this unlocks."),
            el("p", { class: "explain", style: "margin:0" }, sec.hosts.length ? `Sent only to ${sec.hosts.join(", ")}${sec.injection ? `, in the request ${sec.injection.replace(/-/g, " ")}` : ""}.` : "The recipe declares no host for this key."),
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
          v.phases ? el("ul", {class:"plain-list"}, v.phases.map(p => el("li", null, `${words(p.phase)}: ${words(p.status)}${p.seconds != null ? ` · ${p.seconds}s` : ""}${p.exit_code ? ` · exit ${p.exit_code}` : ""}`))) : null,
          uncovered.length ? el("div", { class: "notice info", style: "margin:0" }, el("b", null, "Not covered by this verification: "),
            uncovered.map(c => `${c.label || c.id}${c.limitation ? ` (${c.limitation})` : ""}`).join("; "), ". The app is ready only for what is listed as covered.") : null,
          (a.evidence || []).length ? evidenceGallery(a) : el("p", { class: "explain" }, "No media was accepted from this verification."),
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
        const confirmBtn = el("button", { class: "btn call", type: "button" }, `Confirm: ${label.toLowerCase()}`);
        const cancel = el("button", { class: "btn", type: "button", onclick: () => { confirmBox.hidden = true; first.hidden = false; first.focus(); } }, "Cancel");
        confirmBtn.onclick = () => act(confirmBtn, line, () => api(`${appPath(appId)}/publish`, { body: { plan_sha256: sha } }));
        confirmBox.append(el("p", { style: "margin:0 0 8px;font-weight:700" }, a.kind === "greenfield"
          ? `This creates the private repository ${repoName(a.repository_url)} and opens a pull request with the setup.`
          : `This opens a pull request on ${repoName(a.repository_url)} with the setup files.`),
          el("p", { class: "explain" }, "Nothing is merged. An engineer reviews and merges the pull request."), el("div", { class: "startbar" }, confirmBtn, cancel));
        const change = a.kind === "existing" || a.pr_url
          ? el("div", null, el("a", { class: "btn", href: `#/start?app=${encodeURIComponent(a.app_id)}` }, "Request a change"),
            el("p", { class: "explain", style: "margin:6px 0 0;font-size:12.5px" }, "Starts a normal run on this app's repository. You still approve the plan and the contracts."))
          : el("p", { class: "explain" }, "Requesting changes opens once the repository exists, after you publish.");
        out.push(card("Setup pull request", el("div", { class: "stack" },
          a.pr_url && /^https:\/\//.test(a.pr_url) ? el("p", null, el("a", { href: a.pr_url, target: "_blank", rel: "noopener" }, "Open the setup pull request"), el("span", { class: "explain" }, " · waiting for an engineer to review and merge")) : null,
          a.status === "publishing" ? el("p", { class: "explain" }, "Publishing. This page follows it.") : a.pr_url ? null : !(plan.files || []).length ? el("p", {class:"explain"}, "The existing setup was reused unchanged. No new setup pull request is needed.") : el("div", null, first, confirmBox, line),
          change)));
      }
      out.push(sourceDetails(a));
      return out;
    }
    function sourceDetails(a) {
      return el("details", { class: "tech card", style: "padding:12px 18px" }, el("summary", null, "Technical details: repository, branch and commit"),
        el("dl", { class: "kv", style: "margin-top:10px" },
          el("dt", null, "Repository"), el("dd", { class: "mono" }, a.repository_url || "not given"),
          el("dt", null, "Source branch or commit"), el("dd", { class: "mono" }, a.revision || "the default branch"),
          el("dt", null, "Default branch"), el("dd", { class: "mono" }, a.default_branch || "not known until discovery reads the repository"),
          el("dt", null, "Source commit inspected"), el("dd", { class: "mono" }, a.source_sha || "not known until discovery reads the repository"),
          el("dt", null, "App id"), el("dd", { class: "mono" }, a.app_id)));
    }

    function paint(a) {
      app = a; pending = null; updateBar.hidden = true;
      headBox.replaceChildren(pageHead(a.name || a.app_id, a.description || null,
        [el("span", { class: "tag" }, appKind(a.kind)), appStatusTag(a), el("span", { class: "tag" }, appMoney(a))],
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
          el("button", { class: "btn sm", type: "button", onclick: () => paint(pending || a) }, "Show the update"));
        updateBar.hidden = false;
        state.lastKey.app = appKey(a);
        return;
      }
      paint(a);
    }
    function schedule() { startPoll(tick, busy() ? 3000 : 12000); }

    const aside = el("aside", null, card("Agent status", statusBox), onboardingFacts());
    $main.replaceChildren(headBox, updateBar, el("div", { class: "start" }, body, aside));
    paint(app);
  }

  // ------------------------------------------------------------------ settings
  async function viewSettings() {
    setNav("settings");
    const [c, logs] = await Promise.all([api("/api/config"), api("/api/logs")]);
    state.config = c;
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Settings", "What this Console can do depends on what the orchestrator has been given. Set these as environment variables where you run `factory web`."));
    const platformGaps = gapsMatching(c.design_gaps, ["agent:", "environment_id", "ANTHROPIC_API_KEY"]);
    const forgeGaps = forgeGapsOf(c);
    const surfaceGaps = gapsMatching(c.gaps, ["LINEAR", "FACTORY_GATE_SURFACE", "FACTORY_PAGE_PUBLISHER"]);
    const r = (title, ok, desc, gaps, how, label) => el("div", { class: "r" }, el("h3", null, title), el("div", { class: `status ${ok ? "ok" : "warn"}` }, ok ? "Ready" : (label || "Not configured")),
      el("p", { class: "explain", style: "margin-top:6px" }, desc), gaps.length ? el("ul", null, gaps.map(x => el("li", null, x))) : null, how ? el("p", { class: "explain" }, how) : null);
    const agentsCard = r("The crew and the platform", !platformGaps.length, "The agents and the sandbox environment in your Console workspace, plus the API key the orchestrator uses to start sessions.", platformGaps, "Run `ant apply agents environments` once and put ANTHROPIC_API_KEY (and ANTHROPIC_WORKSPACE_ID if the key is not workspace-scoped) in .env at the repository root.", "Checking...");
    frag.append(card("Readiness", el("div", { class: "readiness" },
      agentsCard,
      r("Code host", !forgeGaps.length, `Where the code lives and pull requests are opened (${c.forge}). Needed for full runs, not for design runs.`, forgeGaps, "Export FORGE_TOKEN (a GitHub token with repo and workflow scopes); FORGE_REPO may stay unset, it follows the outcome."),
      r("Decisions", !surfaceGaps.length, `Where gate decisions are read from: ${c.gate_surface === "tracker" ? "Linear and this Console" : "this Console and the CLI"}.`, surfaceGaps, null))));
    const platformBox = el("div", null, el("p", { class: "explain" }, "Checking the platform..."));
    frag.append(card("On the platform", platformBox, "live check of the agents in claude-lock.json and the last sessions"));
    api("/api/platform").then(p => {
      const st = agentsCard.querySelector(".status");
      st.className = `status ${p.ok ? "ok" : "warn"}`;
      st.textContent = p.ok ? `Ready: ${p.agents.length} agents found on the platform` : (p.error ? "Needs attention" : "Not configured");
      const parts = [];
      if (p.error) parts.push(el("div", { class: "notice bad" }, el("b", null, "Cannot reach the agents. "), p.error));
      if (p.agents && p.agents.length) {
        const t = el("table", null, el("tr", null, el("th", null, "Agent"), el("th", null, "Id"), el("th", null, "On the platform")));
        for (const a of p.agents) t.append(el("tr", null, el("td", null, el("span", { style: "display:inline-flex;align-items:center;gap:8px" }, crewFigure(a.key, "still", "s"), CREW_NAME[a.key] || a.key)), el("td", { class: "mono explain" }, a.id), el("td", null, el("span", { class: `tag ${a.found ? "ok" : "bad"}` }, a.found ? `found${a.name ? ": " + a.name : ""}` : "missing"))));
        parts.push(t);
      }
      if (p.sessions && p.sessions.length) {
        const t = el("table", { class: "platform-sessions" }, el("tr", null, el("th", null, "Session"), el("th", null, "Status"), el("th", null, "Run"), el("th", null, "Created")));
        for (const s of p.sessions) t.append(el("tr", null, el("td", null, el("a", { href: s.url, target: "_blank", rel: "noopener" }, s.title || s.id)), el("td", null, el("span", { class: "tag" }, s.status || "?")),
          el("td", null, s.run_id ? el("a", { href: `#/runs/${s.run_id}` }, s.run_id) : el("span", { class: "explain" }, "not a factory run")), el("td", { class: "explain" }, s.created_at ? ago(s.created_at) : "")));
        parts.push(el("h3", { style: "font-size:13px;margin:14px 0 6px" }, "Recent sessions in the workspace"), t);
      } else if (!p.error) parts.push(el("p", { class: "explain" }, p.sessions_error ? `Sessions could not be listed: ${p.sessions_error}` : "No sessions yet. Start a design run to see the decomposer appear here and in the Anthropic Console."));
      platformBox.replaceChildren(...parts);
    }).catch(e => platformBox.replaceChildren(el("div", { class: "notice bad" }, e.message)));
    frag.append(card("Runs directory", el("dl", { class: "kv" }, el("dt", null, "Path"), el("dd", { class: "mono" }, c.runs_dir), el("dt", null, "Decisions"), el("dd", null, "Written to _queue/ under it; a running orchestrator polls that directory."), el("dt", null, "Workspace"), el("dd", null, c.workspace_id ? el("span", { class: "mono" }, c.workspace_id) : el("span", { class: "explain" }, "ANTHROPIC_WORKSPACE_ID not set (needed when the API key is not scoped to a workspace)")))));
    if (logs.logs.length) {
      const body = el("div", { class: "stack" }, logs.logs.map(l => el("div", null, el("div", { class: "title" }, el("span", { class: "mono" }, l.name), l.run_id ? el("a", { href: `#/runs/${l.run_id}` }, l.run_id) : el("span", { class: "tag" }, "no run id yet"), el("span", { class: "explain" }, ago(l.modified_at))),
        el("pre", { class: "log" }, l.tail))));
      frag.append(card("Live orchestrator logs", body, "started from this Console"));
    }
    $main.replaceChildren(frag);
    startPoll(viewSettings, 30000);
  }

  // ------------------------------------------------------------------ router
  async function route() {
    const hash = location.hash || "#/inbox";
    const [path, query] = hash.slice(1).split("?");
    const params = new URLSearchParams(query || "");
    loadChrome();
    try {
      if (path === "/" || path === "" || path === "/inbox" || path === "/board") await viewInbox();
      else if (path === "/runs") await viewRuns();
      else if (path.startsWith("/runs/")) await viewRun(path.slice(6).split("#")[0]);
      else if (path.startsWith("/gates/")) await viewGate(path.slice(7));
      else if (path === "/start") await viewStart(params);
      else if (path === "/outcomes") await viewOutcomes();
      else if (path === "/outcomes/new") await viewOutcomeForm(params);
      else if (path === "/apps") await viewApps();
      else if (path === "/apps/new") await viewAppNew();
      else if (path.startsWith("/apps/")) await viewApp(decodeURIComponent(path.slice(6)));
      else if (path === "/settings") await viewSettings();
      else { $main.replaceChildren(card(null, el("div", { class: "empty" }, el("h3", null, "No such page"), el("a", { class: "btn", href: "#/inbox" }, "Back to the board")))); stopPoll(); }
    } catch (e) {
      stopPoll();
      $main.replaceChildren(pageHead("Something did not load"), card(null, el("div", { class: "empty" }, el("div", null, e.message), el("a", { class: "btn", href: "#/inbox" }, "Back to the board"))));
    }
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", route);
  loadChrome().then(route);
})();
