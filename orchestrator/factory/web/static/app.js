/* Factory Console. Vanilla JS, hash routes, no build step. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ helpers
  const $main = document.getElementById("main");
  const $foot = document.getElementById("side-foot");
  const $inboxCount = document.getElementById("inbox-count");

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

  async function api(path, opts) {
    const r = await fetch(path, opts && opts.body ? { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts.body) } : opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.error || `${r.status} ${r.statusText}`);
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
  const GATE_NAME = { gate_1_decomposition: "Gate 1: approve the plan", gate_2_spec_readiness: "Gate 2: approve the specs",
    escalation: "Escalation: the judge could not pass the specs", deploy_failure: "Deploy failure" };
  const GATE_SHORT = { gate_1_decomposition: "Gate 1", gate_2_spec_readiness: "Gate 2", escalation: "Escalation", deploy_failure: "Deploy failure" };
  const DECISION_LABEL = { approve: "Approve", approve_subset: "Approve selected", request_changes: "Request changes", reject: "Reject" };

  function ago(iso) {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }
  function clock(iso) { return iso ? iso.slice(11, 19) : ""; }
  function money(n) { return n == null ? "" : `$${Number(n).toFixed(2)}`; }
  function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  function words(s) { return String(s ?? "").replace(/_/g, " "); }
  function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + "s")}`; }

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
  function miniSteps(stage) {
    const idx = STAGES.findIndex(s => s[0] === stage);
    const terminal = TERMINAL[stage];
    return el("span", { class: "mini-steps", title: terminal || (STAGES[idx] || [])[1] || stage },
      STAGES.map((s, i) => el("i", { class: terminal ? (i < 2 ? "done" : "") : i < idx ? "done" : i === idx ? "current" : "" })),
      terminal ? el("i", { class: "terminal" }) : null);
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

  // Light by default; dark is a choice remembered per browser.
  function applyTheme(t) { document.documentElement.dataset.theme = t === "dark" ? "dark" : ""; try { localStorage.setItem("factory-theme", t); } catch (e) { /* private window */ } }
  try { applyTheme(localStorage.getItem("factory-theme") || "light"); } catch (e) { applyTheme("light"); }

  async function loadChrome() {
    try {
      const [c, inbox] = await Promise.all([api("/api/config"), api("/api/inbox")]);
      state.config = c;
      const n = inbox.gates.filter(g => !g.recorded).length + inbox.questions.length;
      $inboxCount.textContent = n; $inboxCount.classList.toggle("zero", n === 0);
      const dark = document.documentElement.dataset.theme === "dark";
      $foot.replaceChildren(
        el("b", null, "Agents"), el("span", { class: c.can_run_design ? "" : "warn" }, c.can_run_design ? "ready for live runs" : "not configured"),
        el("br"), el("b", null, "Code host"), el("span", { class: c.can_run_live ? "" : "warn" }, c.can_run_live ? `${c.forge}: ${c.forge_repo}` : "not configured"),
        el("br"), el("a", { href: "#/settings", style: "color:#94A3B8" }, "Settings"),
        el("br"), el("button", { class: "theme", type: "button", onclick: () => { applyTheme(dark ? "light" : "dark"); loadChrome(); } }, dark ? "Switch to light mode" : "Switch to dark mode"));
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
    return !!(a && $main.contains(a) && (a.tagName === "TEXTAREA" || a.tagName === "INPUT"));
  }
  // Re-render a list view only when the server's picture changed, never while the person is typing in it.
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

  // ------------------------------------------------------------------ inbox
  async function viewInbox() {
    setNav("inbox");
    const inbox = await api("/api/inbox");
    const items = [
      ...inbox.gates.map(g => ({ kind: "gate", at: g.opened_at, g })),
      ...inbox.questions.map(q => ({ kind: "question", at: q.asked_at, q })),
    ].sort((a, b) => (a.at || "").localeCompare(b.at || ""));
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Inbox", "Everything that is waiting on a person. Runs pause here until you decide or answer.",
      [el("a", { class: "btn primary", href: "#/outcomes/new" }, "New outcome")]));
    if (!items.length) {
      frag.append(card(null, el("div", { class: "empty" }, el("h3", null, "Nothing is waiting on you"),
        el("div", null, "Runs keep going on their own until the next gate or a question from an agent."),
        el("a", { class: "btn", href: "#/runs" }, "See the runs"))));
    } else {
      const list = el("ul", { class: "list" });
      for (const it of items) {
        if (it.kind === "gate") {
          const g = it.g;
          list.append(el("li", null,
            el("span", { class: "tag gate" }, GATE_SHORT[g.kind] || g.kind),
            el("div", { class: "main" }, el("div", { class: "title" }, g.run_title), el("div", { class: "desc" }, (GATE_NAME[g.kind] || g.kind) + ". " + g.summary)),
            el("div", { class: "side-meta" }, el("b", null, g.recorded ? "Decision recorded" : `opened ${ago(g.opened_at)}`)),
            el("a", { class: "btn primary", href: `#/gates/${g.gate_id}` }, g.recorded ? "View" : "Review and decide")));
        } else {
          const q = it.q;
          list.append(el("li", null,
            el("span", { class: "tag question" }, q.blocking ? "Question" : "Note"),
            el("div", { class: "main" }, el("div", { class: "title" }, q.run_title), el("div", { class: "desc" }, `The ${q.asked_by} asks: ${q.question}`)),
            el("div", { class: "side-meta" }, el("b", null, q.blocking ? "Agent paused" : "Default applied"), ago(q.asked_at)),
            el("a", { class: "btn primary", href: `#/runs/${q.run_id}#q` }, "Answer")));
        }
      }
      frag.append(card("Needs you", list, plural(items.length, "item")));
    }
    frag.append(card("How a run moves", el("div", null, stepper("x"),
      el("p", { class: "explain", style: "margin-top:12px" }, "Agents do the stages in grey. The two gates are yours: nothing is built until you have approved the plan and then the specs. After Gate 2, review and merge happen on the pull request as usual.")), null, "soft"));
    $main.replaceChildren(frag);
    state.lastKey.inbox = JSON.stringify(items.map(i => i.kind === "gate" ? [i.g.gate_id, !!i.g.recorded] : i.q.question_id));
    startPoll(refreshWhenChanged("inbox", async () => { const x = await api("/api/inbox"); return JSON.stringify([...x.gates.map(g => [g.gate_id, !!g.recorded]), ...x.questions.map(q => q.question_id)]); }, viewInbox), 5000);
  }

  // ------------------------------------------------------------------ runs
  async function viewRuns() {
    setNav("runs");
    const runs = (await api("/api/runs")).runs;
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Runs", "One run per outcome. A run ends with pull requests for a person to review.",
      [el("a", { class: "btn primary", href: "#/outcomes/new" }, "New outcome")]));
    if (!runs.length) {
      frag.append(card(null, el("div", { class: "empty" }, el("h3", null, "No runs yet"),
        el("div", null, "Write an outcome, or load the sample one and run it as a demo to see how the gates work."),
        el("a", { class: "btn primary", href: "#/outcomes/new?example=sample-app" }, "Load the sample app outcome"))));
    } else {
      const tbl = el("table", null, el("tr", null, el("th", null, "Outcome"), el("th", null, "Stage"), el("th", null, "Work items"), el("th", null, "Spend"), el("th", null, "Last activity")));
      for (const r of runs) {
        const wi = r.work_items || { total: 0 };
        const waiting = r.waiting ? r.waiting.gates + r.waiting.questions : 0;
        tbl.append(el("tr", { class: "link", onclick: () => { location.hash = `#/runs/${r.run_id}`; } },
          el("td", null, el("div", { class: "title" }, el("a", { href: `#/runs/${r.run_id}` }, r.title), r.demo ? el("span", { class: "tag demo" }, "demo") : el("span", { class: "tag live" }, r.design_only ? "live, design only" : "live")),
            waiting ? el("div", { class: "explain" }, `${plural(waiting, "item")} waiting on you`) : null),
          el("td", null, miniSteps(r.stage), " ", el("span", { class: "explain" }, TERMINAL[r.stage] || (STAGES.find(s => s[0] === r.stage) || [])[1] || r.stage)),
          el("td", null, wi.total), el("td", null, money(r.cost)), el("td", { class: "explain" }, ago(r.updated_at))));
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
    const send = el("button", { class: "btn primary", type: "button", style: "margin-top:8px", onclick: async () => {
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
      el("div", { class: "why" }, `The ${q.asked_by} is paused until you answer. ${q.why ? "Why it matters: " + q.why : ""}`),
      q.default ? el("div", { class: "why" }, `If nobody answers: ${q.default}`) : null,
      q.options.length ? options : null, ta, send, status);
    return box;
  }

  async function viewRun(runId) {
    setNav("runs");
    const r = await api(`/api/runs/${runId}`);
    const frag = document.createDocumentFragment();
    const stageName = TERMINAL[r.stage] || (STAGES.find(s => s[0] === r.stage) || [])[1] || r.stage;
    frag.append(pageHead(r.title, r.outcome ? r.outcome.statement : null,
      [r.demo ? el("span", { class: "tag demo" }, "demo run") : el("span", { class: "tag live" }, r.design_only ? "live design run" : "live run"),
       el("span", { class: "tag" }, `${money(r.cost)} spent`)],
      [el("a", { href: "#/runs" }, "Runs"), " / ", el("span", { class: "mono" }, r.run_id)]));
    frag.append(card("Where it is", el("div", null, stepper(r.stage, r.design_only), el("p", { class: "explain", style: "margin-top:10px" },
      r.stage === "completed" && r.design_only ? "Done: this was a design run. The approved specs are in the files below; nothing was built. Start a full run from the outcome to implement them."
      : r.stage === "completed" ? "Done. Every approved item has a pull request; review and merge happen on the code host."
      : r.resumable ? "Failed during implementation. The work so far is kept: resume below and the run continues from the last finished step."
      : TERMINAL[r.stage] ? `${stageName}. Nothing more happens in this run.`
      : `Now: ${stageName}.${r.design_only ? " This is a design run: it stops after Gate 2." : ""}`))));

    const gates = r.gates.filter(g => !g.recorded), recorded = r.gates.filter(g => g.recorded);
    if (gates.length || r.questions.length || recorded.length) {
      const body = el("div", { class: "stack" });
      for (const q of r.questions) body.append(askBlock(q, () => setTimeout(() => viewRun(runId), 1500)));
      for (const g of gates) body.append(el("div", { class: "list" }, el("li", null,
        el("span", { class: "tag gate" }, GATE_SHORT[g.kind] || g.kind),
        el("div", { class: "main" }, el("div", { class: "title" }, GATE_NAME[g.kind] || g.kind), el("div", { class: "desc" }, g.summary)),
        el("a", { class: "btn primary", href: `#/gates/${g.gate_id}` }, "Review and decide"))));
      for (const g of recorded) body.append(el("div", { class: "notice ok" }, el("b", null, `${DECISION_LABEL[g.recorded.decision] || g.recorded.decision} recorded on ${GATE_SHORT[g.kind]}. `), "The run picks it up within a few seconds. ", el("a", { href: `#/gates/${g.gate_id}` }, "View")));
      frag.append(card("Needs you", body));
    }

    if (r.resumable) {
      const why = (r.events || []).slice().reverse().find(e => e.kind === "run.failed");
      const box = el("div", { class: "stack" },
        el("p", { class: "explain" }, "The orchestrator stopped before this run reached a pull request", why && why.detail ? `: ${why.detail}` : ".",
          " Resuming does not repeat finished agent sessions; it re-checks the branch and carries on from there."),
        el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", onclick: async (ev) => {
          const b = ev.currentTarget; b.disabled = true; b.textContent = "Resuming…";
          try {
            const res = await api(`/api/runs/${runId}/resume`, { body: {} });
            box.append(el("div", { class: "notice ok" }, el("b", null, "Resumed. "), `The run continues in the background (log ${res.log}). This page follows it.`));
            setTimeout(() => viewRun(runId), 2500);
          } catch (e) { b.disabled = false; b.textContent = "Resume run"; box.append(el("div", { class: "notice bad" }, String(e.message || e))); }
        } }, "Resume run")));
      frag.append(card("Resume", box));
    }

    const items = el("table", null, el("tr", null, el("th", null, "Work item"), el("th", null, "Stage"), el("th", null, "Depends on"), el("th", null, "Pull request")));
    for (const w of r.work_items) items.append(el("tr", null,
      el("td", null, el("div", null, w.title || w.work_item_id), el("div", { class: "explain mono" }, w.work_item_id)),
      el("td", null, el("span", { class: "tag" }, ITEM_STAGE[w.stage] || words(w.stage))),
      el("td", { class: "explain" }, (w.depends_on || []).join(", ") || "none"),
      el("td", null, w.cr_url ? el("a", { href: w.cr_url, target: "_blank", rel: "noopener" }, `#${w.cr_id}`) : (w.branch ? el("span", { class: "explain mono" }, w.branch) : el("span", { class: "explain" }, "not yet")))));
    frag.append(card("Work items", r.work_items.length ? items : el("p", { class: "explain" }, "Work items appear once the decomposition exists."), r.work_items.length ? plural(r.work_items.length, "item") : null));

    const tl = el("ol", { class: "timeline" });
    for (const ev of [...r.events].reverse().slice(0, 80)) {
      const [who, what] = describe(ev);
      const link = what.session_id && state.config && state.config.console_session_url && !r.demo
        ? el("a", { href: state.config.console_session_url.replace("SESSION_ID", what.session_id), target: "_blank", rel: "noopener" }, " open in the Anthropic Console") : null;
      tl.append(el("li", null, el("time", { datetime: ev.at, title: ev.at }, clock(ev.at)),
        el("span", null, el("span", { class: who === "human" ? "human" : "" }, what.main), what.detail ? el("span", { class: "detail" }, " ", what.detail) : null, link)));
    }
    const files = el("table", null, el("tr", null, el("th", null, "Stage"), el("th", null, "File"), el("th", null, "Fingerprint")));
    for (const a of r.artifacts) files.append(el("tr", null, el("td", { class: "explain" }, a.stage),
      el("td", null, el("a", { href: `/api/artifacts/${r.run_id}/${a.stage}/${a.name}`, target: "_blank", rel: "noopener" }, a.name)),
      el("td", { class: "explain mono" }, a.sha256 ? a.sha256.slice(0, 12) : "")));
    frag.append(el("div", { class: "grid-2" }, card("Timeline", tl, "newest first"), card("Files produced", r.artifacts.length ? files : el("p", { class: "explain" }, "None yet."))));
    $main.replaceChildren(frag);
    state.lastKey.run = runKey(r);
    if (!(TERMINAL[r.stage] || r.stage === "completed")) startPoll(refreshWhenChanged("run", async () => runKey(await api(`/api/runs/${runId}`)), () => viewRun(runId)), 4000);
    else stopPoll();
  }
  function runKey(r) {
    return JSON.stringify([r.updated_at, r.stage, r.gates.map(g => [g.gate_id, !!g.recorded]), r.questions.map(q => q.question_id)]);
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
      case "merge.verdict": return [who, { main: `Merge judge: ${p.result}`, detail: item }];
      case "cr.opened": return [who, { main: p.design ? "Design pull request opened" : `Pull request opened for ${item}`, detail: p.url }];
      case "cr.merged": return ["human", { main: `Merged ${item}` }];
      case "deploy.started": return [who, { main: `Dev deploy started for ${item}` }];
      case "deploy.succeeded": return [who, { main: `Dev deploy succeeded for ${item}`, detail: p.environment_url }];
      case "deploy.failed": return [who, { main: `Dev deploy failed for ${item}` }];
      case "e2e.dev_passed": return [who, { main: `Dev checks passed for ${item}` }];
      case "e2e.dev_failed": return [who, { main: `Dev checks failed for ${item}` }];
      case "session.started": return [who, { main: `Agent session started: ${p.agent_key || p.agent_id || "agent"}`, detail: p.title || "", session_id: p.session_id }];
      case "session.finished": return [who, { main: `Agent session finished: ${p.agent_id || "agent"}`, detail: p.list_cost_usd != null ? money(p.list_cost_usd) : "", session_id: p.session_id }];
      case "work_item.stage": return [who, { main: `${ev.work_item_id}: ${ITEM_STAGE[p.stage] || words(p.stage)}` }];
      case "run.escalated": return [who, { main: "Escalated", detail: p.reason }];
      case "run.completed": return [who, { main: p.design_only ? "Design stages completed" : "Run completed", detail: (p.specs_with_change_requests || p.approved_specs || []).join(", ") }];
      case "run.failed": return [who, { main: "Run failed", detail: p.reason }];
      case "run.resumed": return [who, { main: "Run resumed", detail: Object.entries(p.points || {}).map(([s, x]) => `${s}: round ${x.round}${x.implementer_done ? " (implementer done)" : ""}`).join("; ") }];
      case "budget.reached": return [who, { main: "Budget reached" }];
      default: return [who, { main: ev.kind }];
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
    frag.append(pageHead(GATE_NAME[rec.kind] || rec.kind, g.run_title, [el("a", { class: "btn", href: `/gates/${gateId}/page`, target: "_blank", rel: "noopener" }, "Open as a page")],
      [el("a", { href: "#/inbox" }, "Inbox"), " / ", el("a", { href: `#/runs/${g.run_id}` }, g.run_title), " / opened ", ago(rec.opened_at)]));

    const doc = el("div", { class: "stack" });
    const prev = rv.previous_decisions || [];
    if (prev.length) {
      const last = prev[prev.length - 1];
      doc.append(el("div", { class: "notice info" }, el("b", null, `Revised after you chose "${DECISION_LABEL[last.decision] || last.decision}". `), last.comments ? `Your note was: "${last.comments}"` : ""));
    }
    if (rv.kind === "decomposition" && rv.decomposition) renderDecomposition(doc, rv, notes);
    else if (rv.kind === "specs" && rv.verdict) renderSpecs(doc, rv, notes, picks, rec.options.includes("approve_subset"));
    else if (rv.kind === "deploy") renderDeploy(doc, rv, rec);
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
    // dependency order: the order the work would happen in
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
      el("p", { class: "explain" }, el("strong", null, "The decomposer turned the outcome into work items. "), "Approving means agents start writing a specification for each item. Nothing is built yet, and you get another gate before code."),
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
      el("p", { class: "explain" }, el("strong", null, "Agents wrote one specification per work item; an independent judge checked the set. "), "A spec is the contract an agent will build to: acceptance criteria, interfaces, and which files it may touch. Approving a spec starts its implementation."),
      summaryCard("The judge's summary", ov.summary_for_human || ""),
      glance([[words(ov.result), "judge's verdict", ov.result === "pass" ? "ok" : "warn"], [Object.keys(specs).length, "specs"],
        [`${totalAC} (${userVisible} user-visible)`, "acceptance criteria"], [sev("blocking") ? `${sev("blocking")} blocking` : sev("major") ? `${sev("major")} major` : allIssues.length, "issues the judge flagged", sev("blocking") ? "bad" : sev("major") ? "warn" : ""],
        [`${(v.goal_coverage || []).length - notCovered.length}/${(v.goal_coverage || []).length}`, "metrics covered by criteria", notCovered.length ? "warn" : "ok"]]))));

    doc.append(card("What to check", checklist([
      ["The judge's verdict and anything it flagged", allIssues.length ? `${allIssues.length} issues, listed under each spec. Blocking ones should be resolved before you approve.` : "The judge found nothing outstanding."],
      ["User-visible acceptance criteria", `${userVisible} criteria describe behaviour a user or operator will see. Read those as if they were the feature description.`],
      ["Product decisions hiding in the criteria", "Customer-facing wording, limits, timings, and defaults in a criterion are decisions you are making by approving."],
      ["Where each agent may change code", "Each spec lists allowed paths. If a spec reaches into something it should not, request changes."],
      ["Open questions and their defaults", openQ.length ? `${openQ.length} questions across the specs will use their defaults unless you say otherwise.` : "None."],
    ])));

    const gc = el("table", { class: "coverage" }, el("tr", null, el("th", null, "Success metric"), el("th", null, "Covered?"), el("th", null, "By which criteria"), el("th", null, "Judge's note")));
    for (const g of v.goal_coverage || []) gc.append(el("tr", null, el("td", null, el("b", null, g.metric)), el("td", { class: g.assessment === "covered" ? "ok" : "gap" }, words(g.assessment)), el("td", { class: "mono explain" }, (g.covering_acs || []).join(", ")), el("td", null, g.note || "")));
    const coh = Object.entries(v.coherence || {}).flatMap(([bucket, items]) => (items || []).map(it => ({ bucket, it })));
    doc.append(card("Metrics and how the specs fit together", el("div", { class: "stack" }, gc,
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
            subsetAllowed ? el("label", { class: "pick" }, cb, "Approve this spec") : null)),
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
          rv.spec_md[sid] ? el("details", null, el("summary", null, "The spec as the agent wrote it"), el("div", { class: "reading", html: md(rv.spec_md[sid]) })) : null,
          noteField(sid, "What should change in this spec?", notes))));
    });
    doc.append(card("The specs", wrap, plural(Object.keys(specs).length, "spec")));
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

    const composeComments = () => {
      const lines = [];
      if (comment.value.trim()) lines.push(comment.value.trim());
      for (const [id, ta] of notes) if (ta.value.trim()) lines.push(`${id}: ${ta.value.trim()}`);
      return lines.join("\n");
    };
    const subset = () => [...picks].filter(([, cb]) => cb.checked).map(([sid]) => sid);
    const refresh = () => {
      const n = [...notes.values()].filter(ta => ta.value.trim()).length;
      const picked = picks.size ? subset().length : null;
      counts.textContent = [n ? `${plural(n, "note")} on items will be sent with Request changes` : "", picked !== null ? `${picked} of ${picks.size} specs selected` : ""].filter(Boolean).join(". ");
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
      gate_1_decomposition: { approve: "Agents start writing a spec for every work item, with your notes as guidance. You get Gate 2 before any code.", approve_subset: "", request_changes: "The decomposer revises the plan using your notes and asks you again.", reject: "The run closes. Nothing else happens." },
      gate_2_spec_readiness: { approve: "Agents start implementing all specs on separate branches; pull requests follow.", approve_subset: "Selected specs are implemented; the others go back to the writer with your notes.", request_changes: "The spec writers revise using your notes; the judge re-checks; you decide again.", reject: "The run closes. Nothing is built." },
      escalation: { approve: "Proceed with the specs as they are.", approve_subset: "Selected specs proceed; the rest go back.", request_changes: "The writers revise using your notes.", reject: "The run closes." },
      deploy_failure: { approve: "The failure was the platform's; the next deploy is watched.", request_changes: "An agent fixes it on a new branch; a follow-up pull request follows.", reject: "This work item is closed." },
    }[rec.kind] || {};
    const buttons = rec.options.map(opt => el("button", { class: `btn block ${opt === "approve" || opt === "approve_subset" ? "approve" : opt === "request_changes" ? "changes" : "danger"}`,
      type: "button", dataset: { decision: opt }, onclick: () => arm(opt) }, DECISION_LABEL[opt] || opt));

    function arm(opt) {
      error.textContent = "";
      const comments = composeComments();
      if (opt === "request_changes" && !comments) { error.textContent = "Say what should change first."; return; }
      buttons.forEach(b => b.hidden = true);
      const go = el("button", { class: `btn block ${opt === "reject" ? "danger" : opt === "request_changes" ? "changes" : "approve"}`, type: "button", disabled: true, onclick: () => decide(opt, comments) }, `Yes, ${(DECISION_LABEL[opt] || opt).toLowerCase()}`);
      confirmBox.replaceChildren(el("div", { class: "what" }, `${DECISION_LABEL[opt] || opt}?`),
        el("div", { class: "explain" }, consequences[opt] || ""),
        el("div", { class: "btn-col", style: "margin-top:10px" }, go, el("button", { class: "btn block", type: "button", onclick: disarm }, "Cancel")));
      confirmBox.hidden = false;
      // Confirm sits where no first-step button was and wakes after a beat: a doubled or mis-aimed click never decides.
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
        `${ago(d.decided_at)}. The run picks it up within a few seconds. `, el("a", { href: `#/runs/${g.run_id}` }, "Back to the run")));
      form.hidden = true;
    }
    function showDecided(p) {
      status.replaceChildren(el("div", { class: `recorded ${cls(p.decision)}` }, el("b", null, `${DECISION_LABEL[p.decision] || p.decision}, applied`),
        "The run has moved on. ", el("a", { href: `#/runs/${g.run_id}` }, "See where it is now."), p.comments ? el("div", { class: "explain", style: "margin-top:6px" }, `“${p.comments}”`) : null));
      form.hidden = true;
    }

    const form = el("div", null,
      el("p", { class: "explain" }, Object.entries(consequences).filter(([k]) => rec.options.includes(k) && consequences[k]).map(([k, v]) => el("span", { style: "display:block;margin-bottom:4px" }, el("b", null, DECISION_LABEL[k] + ": "), v))),
      el("label", { for: "decide-comment" }, "Your comment"), comment, counts,
      confirmBox, el("div", { class: "btn-col", style: "margin-top:12px" }, buttons), error,
      el("details", null, el("summary", null, "Why this is safe to approve"),
        el("div", { class: "explain", style: "margin-top:6px" }, "Your decision is tied to the exact files you are looking at, by fingerprint. If an agent changed one after this page loaded, the decision is refused and the gate reopens, so you can never approve something you did not read."),
        el("ul", { class: "files" }, rec.artifacts.map(a => el("li", null, el("span", null, a.name), el("code", { title: a.sha256 }, a.sha256.slice(0, 10)))))));
    const node = el("aside", { class: "decide" }, card("Your decision", el("div", null, status, form)));
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
      [el("a", { class: "btn", href: "#/outcomes/new?example=sample-app" }, "Load the sample app outcome"), el("a", { class: "btn", href: "#/outcomes/new?example=ledgerline" }, "Load the Ledgerline example"), el("a", { class: "btn primary", href: "#/outcomes/new" }, "New outcome")]));
    if (!outs.length) {
      frag.append(card(null, el("div", { class: "empty" }, el("h3", null, "No outcomes saved yet"), el("div", null, "Write one in your own words, or load the sample app outcome (examples/sample-app) to run the real test case; the Ledgerline example is the fictional one the demo uses."),
        el("a", { class: "btn primary", href: "#/outcomes/new?example=sample-app" }, "Load the sample app outcome"))));
    } else {
      const list = el("ul", { class: "list" });
      for (const o of outs) {
        const start = el("div", { class: "actions" });
        list.append(el("li", null, el("div", { class: "main" }, el("div", { class: "title" }, el("a", { href: `#/outcomes/new?outcome=${o.outcome_id}` }, o.title || o.outcome_id)),
          el("div", { class: "desc" }, `${o.owner ? "Owner " + o.owner + ". " : ""}${o.runs.length ? plural(o.runs.length, "run") + ": " + o.runs.map(r => r.slice(4, 12)).join(", ") : "No runs yet."}`)),
          start));
        startButtons(start, o.outcome_id, cfg);
      }
      frag.append(card("Saved outcomes", list, plural(outs.length, "outcome")));
    }
    $main.replaceChildren(frag);
    stopPoll();
  }

  function startButtons(container, outcomeId, cfg) {
    const go = async (mode, btn) => {
      btn.disabled = true;
      try {
        const r = await api("/api/runs", { body: { outcome_id: outcomeId, mode } });
        if (r.run_id) location.hash = `#/runs/${r.run_id}`;
        else { location.hash = "#/runs"; }
      } catch (e) { alertBox(container, e.message); btn.disabled = false; }
    };
    const live = el("button", { class: "btn primary", type: "button", disabled: !cfg.can_run_live, title: cfg.can_run_live ? "" : "Needs the code host configured; see Settings" }, "Start full run");
    const design = el("button", { class: "btn", type: "button", disabled: !cfg.can_run_design, title: cfg.can_run_design ? "" : "Needs the agents and API key; see Settings" }, "Start design run");
    const demo = el("button", { class: "btn", type: "button" }, "Run as demo");
    live.onclick = () => go("live", live); design.onclick = () => go("design", design); demo.onclick = () => go("demo", demo);
    container.replaceChildren(live, design, demo);
  }
  function alertBox(container, msg) { container.append(el("div", { class: "notice bad", style: "margin:8px 0 0" }, msg)); }

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
      el("p", { class: "help" }, "An existing repository, or one the factory creates for you on the code host before the run starts (initialised with a README and the preview workflow). The URL names it either way."),
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
      notice.replaceChildren(el("div", { class: "notice ok" }, el("b", null, "Saved. "), "Start a run below, or come back to it from Outcomes."));
      return r;
    }
    async function start(mode, btn) {
      btn.disabled = true;
      const r = await save();
      if (!r) { btn.disabled = false; return; }
      try {
        const s = await api("/api/runs", { body: { outcome_id: savedId, mode } });
        if (s.run_id) location.hash = `#/runs/${s.run_id}`;
        else { notice.replaceChildren(el("div", { class: "notice ok" }, el("b", null, "Run started. "), "The orchestrator is running; the run appears under Runs within a minute. Its log is under Settings.")); btn.disabled = false; }
      } catch (e) { notice.replaceChildren(el("div", { class: "notice bad" }, e.message)); btn.disabled = false; }
    }
    const saveBtn = el("button", { class: "btn", type: "button", onclick: save }, "Save");
    const liveBtn = el("button", { class: "btn primary", type: "button", disabled: !cfg.can_run_live, title: cfg.can_run_live ? "" : "Needs the code host configured; see Settings" }, "Save and start full run");
    const designBtn = el("button", { class: "btn", type: "button", disabled: !cfg.can_run_design, title: cfg.can_run_design ? "" : "Needs the agents and API key; see Settings" }, "Save and start design run");
    const demoBtn = el("button", { class: "btn", type: "button" }, "Save and run as demo");
    liveBtn.onclick = () => start("live", liveBtn); designBtn.onclick = () => start("design", designBtn); demoBtn.onclick = () => start("demo", demoBtn);
    form.append(card("Start", el("div", null,
      el("p", { class: "explain" }, el("b", null, "Full run: "), "real agents decompose, specify, implement, and open pull requests on your repository. ",
        el("b", null, "Design run: "), "real agents through Gate 2, no code, no repository access needed. ",
        el("b", null, "Demo: "), "simulated agents, real gates, nothing leaves this machine.",
        (!cfg.can_run_live || !cfg.can_run_design) ? el("span", null, " ", el("a", { href: "#/settings" }, "What is missing")) : null),
      notice, el("div", { class: "startbar" }, saveBtn, el("span", { class: "spacer" }), demoBtn, designBtn, liveBtn))));
    frag.append(form);
    $main.replaceChildren(frag);
    stopPoll();
  }

  // ------------------------------------------------------------------ settings
  async function viewSettings() {
    setNav("settings");
    const [c, logs] = await Promise.all([api("/api/config"), api("/api/logs")]);
    state.config = c;
    const frag = document.createDocumentFragment();
    frag.append(pageHead("Settings", "What this Console can do depends on what the orchestrator has been given. Set these as environment variables where you run `factory web`."));
    const want = (gaps, keys) => gaps.filter(g => keys.some(k => g.includes(k)));
    const platformGaps = want(c.design_gaps, ["agent:", "environment_id", "ANTHROPIC_API_KEY"]);
    const forgeGaps = want(c.gaps, ["FORGE", "FACTORY_FORGE", "FACTORY_VAULT_ID"]);
    const surfaceGaps = want(c.gaps, ["LINEAR", "FACTORY_GATE_SURFACE", "FACTORY_PAGE_PUBLISHER"]);
    const r = (title, ok, desc, gaps, how, label) => el("div", { class: "r" }, el("h3", null, title), el("div", { class: `status ${ok ? "ok" : "warn"}` }, ok ? "Ready" : (label || "Not configured")),
      el("p", { class: "explain", style: "margin-top:6px" }, desc), gaps.length ? el("ul", null, gaps.map(x => el("li", null, x))) : null, how ? el("p", { class: "explain" }, how) : null);
    const agentsCard = r("Agents and platform", !platformGaps.length, "The eight agents and the sandbox environment in your Console workspace, plus the API key the orchestrator uses to start sessions.", platformGaps, "Run `ant apply agents environments` once and put ANTHROPIC_API_KEY (and ANTHROPIC_WORKSPACE_ID if the key is not workspace-scoped) in .env at the repository root.", "Checking...");
    frag.append(card("Readiness", el("div", { class: "readiness" },
      agentsCard,
      r("Code host", !forgeGaps.length, `Where the code lives and pull requests are opened (${c.forge}). Needed for full runs, not for design runs.`, forgeGaps, "Export FORGE_REPO=owner/repo and FORGE_TOKEN (a fine-grained GitHub token)."),
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
        for (const a of p.agents) t.append(el("tr", null, el("td", null, a.key), el("td", { class: "mono explain" }, a.id), el("td", null, el("span", { class: `tag ${a.found ? "ok" : "bad"}` }, a.found ? `found${a.name ? ": " + a.name : ""}` : "missing"))));
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
    try {
      if (path === "/" || path === "" || path === "/inbox") await viewInbox();
      else if (path === "/runs") await viewRuns();
      else if (path.startsWith("/runs/")) await viewRun(path.slice(6).split("#")[0]);
      else if (path.startsWith("/gates/")) await viewGate(path.slice(7));
      else if (path === "/outcomes") await viewOutcomes();
      else if (path === "/outcomes/new") await viewOutcomeForm(params);
      else if (path === "/settings") await viewSettings();
      else { $main.replaceChildren(card(null, el("div", { class: "empty" }, el("h3", null, "No such page"), el("a", { class: "btn", href: "#/inbox" }, "Back to the inbox")))); stopPoll(); }
    } catch (e) {
      stopPoll();
      $main.replaceChildren(pageHead("Something did not load"), card(null, el("div", { class: "empty" }, el("div", null, e.message), el("a", { class: "btn", href: "#/inbox" }, "Back to the inbox"))));
    }
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", route);
  loadChrome().then(route);
})();
