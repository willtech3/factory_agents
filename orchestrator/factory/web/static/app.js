/* Factory Console. Vanilla JS, hash routes, no build step. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ helpers
  const $main = document.getElementById("main");
  const $context = document.getElementById("context");

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
    ["intake", "Intake", false], ["decomposing", "Decomposing", false], ["gate_1_decomposition", "Gate 1", true],
    ["specifying", "Specifying", false], ["gate_2_spec_readiness", "Gate 2", true], ["implementing", "Implementing", false],
    ["completed", "Done", false],
  ];
  const TERMINAL = { closed: "Closed", escalated: "Escalated", failed: "Failed", paused_budget: "Paused at budget" };
  const ITEM_STAGE = {
    pending: "Waiting", spec_drafting: "Spec being written", spec_judging: "Spec being judged", spec_approved: "Spec approved",
    implementing: "Implementing", reviewing: "In review", responding: "Responding to review", merge_judging: "Merge judge",
    integrating: "Opening change request", cr_open: "Change request open", merged: "Merged", deploying_dev: "Deploying to dev",
    deployed_dev: "Verified in dev", closed: "Closed", escalated: "Escalated", blocked: "Blocked",
  };
  const GATE_NAME = { gate_1_decomposition: "Gate 1: decomposition review", gate_2_spec_readiness: "Gate 2: specification readiness",
    escalation: "Escalation", deploy_failure: "Deploy failure" };
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

  function track(stage, mini) {
    const idx = STAGES.findIndex(s => s[0] === stage);
    const terminal = TERMINAL[stage];
    const t = el("div", { class: "track" + (mini ? " mini" : ""), role: "list", "aria-label": "Run stages" });
    STAGES.forEach(([key, label, gate], i) => {
      let cls = "step" + (gate ? " gate" : "");
      if (terminal) cls += i < 2 ? " done" : "";
      else if (i < idx) cls += " done";
      else if (i === idx) cls += " current";
      t.append(el("span", { class: cls, role: "listitem", dataset: { label } }, el("span", { class: "mark", "aria-hidden": "true" }), label));
    });
    if (terminal) t.append(el("span", { class: "step terminal", role: "listitem", dataset: { label: terminal } }, el("span", { class: "mark", "aria-hidden": "true" }), terminal));
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

  // ------------------------------------------------------------------ context + nav
  async function loadContext() {
    try {
      const c = await api("/api/config");
      $context.replaceChildren(
        el("span", null, `forge ${c.forge}${c.forge_repo ? " " + c.forge_repo : ""}`),
        el("span", null, `decisions via ${c.gate_surface === "tracker" ? "Linear and this console" : "this console"}`),
        c.can_run_live ? null : el("span", { class: "warn", title: c.gaps.join("\n") }, "live runs need configuration"),
      );
      state.config = c;
    } catch (e) { $context.textContent = "console offline"; }
  }
  function setNav(key) {
    document.querySelectorAll("[data-nav]").forEach(a => a.toggleAttribute("aria-current", false));
    const a = document.querySelector(`[data-nav="${key}"]`);
    if (a) a.setAttribute("aria-current", "page");
  }

  // ------------------------------------------------------------------ views
  const state = { config: null, poll: null, view: null };

  function stopPoll() { if (state.poll) { clearInterval(state.poll); state.poll = null; } }
  function startPoll(fn, ms) { stopPoll(); state.poll = setInterval(() => { if (document.visibilityState === "visible") fn(); }, ms); }

  async function viewHome() {
    setNav("home");
    const [inbox, runs] = await Promise.all([api("/api/inbox"), api("/api/runs")]);
    const waiting = [
      ...inbox.gates.map(g => ({ kind: "gate", at: g.opened_at, g })),
      ...inbox.questions.map(q => ({ kind: "question", at: q.asked_at, q })),
    ].sort((a, b) => (a.at || "").localeCompare(b.at || ""));

    const frag = document.createDocumentFragment();
    frag.append(el("h1", { class: "page" }, waiting.length ? `Waiting on you` : "Nothing is waiting on you"),
      el("p", { class: "lede" }, waiting.length
        ? `${waiting.length === 1 ? "One decision" : waiting.length + " items"} across your runs. Gates bind to the exact artifacts you read; questions pause an agent until you answer.`
        : "Runs continue on their own until the next gate or a blocking question. New outcomes start from the form."));

    if (waiting.length) {
      const list = el("ul", { class: "rows" });
      for (const w of waiting) {
        if (w.kind === "gate") {
          const g = w.g;
          list.append(el("li", null, el("a", { class: "row", href: `#/gates/${g.gate_id}` },
            el("span", { class: "row-kind gate" }, GATE_SHORT[g.kind] || g.kind),
            el("span", null, el("span", { class: "row-title" }, g.run_title), el("div", { class: "row-sub" }, g.summary)),
            el("span", { class: "row-meta" }, el("b", null, g.recorded ? "Decision recorded" : "Review"), `opened ${ago(g.opened_at)}`))));
        } else {
          const q = w.q;
          list.append(el("li", null, el("a", { class: "row", href: `#/runs/${q.run_id}` },
            el("span", { class: "row-kind" }, q.blocking ? "Question, blocking" : "Question"),
            el("span", null, el("span", { class: "row-title" }, q.run_title), el("div", { class: "row-sub" }, q.question)),
            el("span", { class: "row-meta" }, el("b", null, "Answer"), `from the ${q.asked_by}, ${ago(q.asked_at)}`))));
        }
      }
      frag.append(list);
    }

    frag.append(el("h2", null, "Runs"));
    if (!runs.runs.length) {
      frag.append(el("div", { class: "empty" }, "No runs yet. ", el("a", { href: "#/new" }, "Write an outcome"),
        " to start one, or ", el("a", { href: "#/new?example=1" }, "load the Ledgerline example"), " and run it as a demo."));
    } else {
      const list = el("ul", { class: "rows runs" });
      for (const r of runs.runs) {
        const wi = r.work_items || { total: 0 };
        const waitingN = r.waiting ? r.waiting.gates + r.waiting.questions : 0;
        list.append(el("li", null, el("a", { class: "row", href: `#/runs/${r.run_id}` },
          el("span", null, el("span", { class: "row-title" }, r.title), " ", r.demo ? el("span", { class: "pill demo" }, "demo") : null,
            el("div", { class: "small muted" }, waitingN ? `${waitingN} waiting on you` : `${wi.total} work item${wi.total === 1 ? "" : "s"}`)),
          el("span", null, track(r.stage, true)),
          el("span", { class: "row-meta" }, el("b", null, money(r.cost)), "spent"),
          el("span", { class: "row-meta" }, el("b", null, ago(r.updated_at)), el("span", { class: "mono" }, r.run_id.slice(4, 12))))));
      }
      frag.append(list);
    }
    $main.replaceChildren(frag);
    startPoll(() => viewHome(), 5000);
  }

  // ---- question block (used on the run page)
  function askBlock(q, onDone) {
    const ta = el("textarea", { placeholder: "Your answer", "aria-label": "Your answer" });
    const status = el("div", { class: "small muted" });
    const options = el("div", { class: "options" }, q.options.map(o => el("button", { class: "btn quiet", type: "button", onclick: () => { ta.value = o; ta.focus(); } }, o)));
    const send = el("button", { class: "btn inline send", type: "button", onclick: async () => {
      if (!ta.value.trim()) { status.textContent = "Type or pick an answer first."; return; }
      send.disabled = true;
      try { await api(`/api/questions/${q.question_id}/answer`, { body: { answer: ta.value.trim(), by: "console" } });
        box.replaceChildren(el("div", { class: "q" }, q.question), el("div", { class: "done" }, `Answered: ${ta.value.trim()}`),
          el("div", { class: "small muted" }, "The session resumes with your answer."));
        if (onDone) onDone();
      } catch (e) { status.textContent = e.message; send.disabled = false; }
    } }, "Send answer");
    const box = el("div", { class: "ask" },
      el("div", { class: "q" }, q.question),
      el("div", { class: "why" }, `Asked by the ${q.asked_by}, ${ago(q.asked_at)}. ${q.why ? "Why it matters: " + q.why : ""}`),
      q.default ? el("div", { class: "why" }, `If nobody answers: ${q.default}`) : null,
      q.options.length ? options : null, ta, send, status);
    return box;
  }

  async function viewRun(runId) {
    setNav("home");
    const r = await api(`/api/runs/${runId}`);
    const frag = document.createDocumentFragment();
    frag.append(el("div", { class: "run-head" },
      el("h1", { class: "page", style: "margin:0" }, r.title),
      r.demo ? el("span", { class: "pill demo" }, "demo") : null,
      el("span", { class: "run-id" }, r.run_id),
      el("span", { class: "cost" }, el("b", null, money(r.cost)), " spent so far")));
    frag.append(track(r.stage, false));
    if (r.outcome && r.outcome.statement) frag.append(el("p", { class: "lede", style: "margin-top:14px" }, r.outcome.statement));

    const gates = r.gates.filter(g => !g.recorded), recorded = r.gates.filter(g => g.recorded);
    if (gates.length || r.questions.length || recorded.length) {
      frag.append(el("h2", null, "Waiting on you"));
      for (const q of r.questions) frag.append(askBlock(q, () => setTimeout(() => viewRun(runId), 1500)));
      for (const g of gates) frag.append(el("a", { class: "row", href: `#/gates/${g.gate_id}` },
        el("span", { class: "row-kind gate" }, GATE_SHORT[g.kind] || g.kind),
        el("span", null, el("span", { class: "row-title" }, GATE_NAME[g.kind] || g.kind), el("div", { class: "row-sub" }, g.summary)),
        el("span", { class: "row-meta" }, el("b", null, "Review"), `opened ${ago(g.opened_at)}`)));
      for (const g of recorded) frag.append(el("a", { class: "row", href: `#/gates/${g.gate_id}` },
        el("span", { class: "row-kind" }, GATE_SHORT[g.kind] || g.kind),
        el("span", null, el("span", { class: "row-title" }, `${DECISION_LABEL[g.recorded.decision] || g.recorded.decision} recorded`),
          el("div", { class: "row-sub muted" }, "Waiting for the run to pick it up.")),
        el("span", { class: "row-meta" }, el("b", null, ago(g.recorded.decided_at)))));
    }

    frag.append(el("h2", null, "Work items"));
    if (!r.work_items.length) frag.append(el("p", { class: "muted" }, "Work items appear once the decomposition exists."));
    else {
      const tbl = el("table", null, el("tr", null, el("th", null, "Work item"), el("th", null, "Stage"), el("th", null, "Depends on"), el("th", null, "Change request")));
      for (const w of r.work_items) tbl.append(el("tr", null,
        el("td", null, el("div", null, w.title || w.work_item_id), el("div", { class: "hash" }, w.work_item_id)),
        el("td", null, ITEM_STAGE[w.stage] || words(w.stage), w.review_round ? el("span", { class: "muted" }, ` (round ${w.review_round})`) : null),
        el("td", { class: "muted" }, (w.depends_on || []).join(", ") || "none"),
        el("td", null, w.cr_url ? el("a", { href: w.cr_url, target: "_blank", rel: "noopener" }, `#${w.cr_id}`) : (w.branch ? el("span", { class: "mono muted" }, w.branch) : el("span", { class: "muted" }, "not yet")))));
      frag.append(tbl);
    }

    frag.append(el("h2", null, "Timeline"));
    const tl = el("ol", { class: "timeline" });
    for (const ev of [...r.events].reverse().slice(0, 80)) {
      const [who, what] = describe(ev);
      tl.append(el("li", null, el("time", { datetime: ev.at, title: ev.at }, clock(ev.at)),
        el("span", null, el("span", { class: who === "human" ? "human" : "" }, what.main), what.detail ? el("span", { class: "detail" }, " ", what.detail) : null)));
    }
    frag.append(tl);

    if (r.artifacts.length) {
      frag.append(el("h2", null, "Artifacts"));
      const tbl = el("table", null, el("tr", null, el("th", null, "Stage"), el("th", null, "File"), el("th", null, "sha256")));
      for (const a of r.artifacts) tbl.append(el("tr", null, el("td", { class: "muted" }, a.stage),
        el("td", null, el("a", { href: `/api/artifacts/${r.run_id}/${a.stage}/${a.name}`, target: "_blank", rel: "noopener" }, a.name)),
        el("td", { class: "hash" }, a.sha256 ? a.sha256.slice(0, 16) : "")));
      frag.append(tbl);
    }
    $main.replaceChildren(frag);
    if (!(TERMINAL[r.stage] || r.stage === "completed")) startPoll(() => viewRun(runId), 4000);
  }

  function describe(ev) {
    const p = ev.payload || {};
    const who = ev.actor;
    const item = ev.spec_id || ev.work_item_id;
    switch (ev.kind) {
      case "run.created": return [who, { main: "Run created", detail: p.title }];
      case "stage.entered": return [who, { main: `Entered ${({ decomposing: "decomposing", gate_1_decomposition: "Gate 1", specifying: "specifying", gate_2_spec_readiness: "Gate 2", implementing: "implementing", closed: "closed", completed: "completed" })[p.stage] || p.stage}` }];
      case "gate.opened": return [who, { main: `${GATE_SHORT[p.kind] || p.kind} opened`, detail: `${(p.artifacts || []).length} artifacts` }];
      case "gate.decided": return ["human", { main: `${GATE_SHORT[p.kind] || p.kind}: ${DECISION_LABEL[p.decision] || p.decision}`, detail: p.comments ? `"${p.comments.slice(0, 120)}"` : "" }];
      case "question.asked": return [who, { main: `The ${p.asked_by} asked a question`, detail: p.question }];
      case "question.answered": return ["human", { main: "Question answered", detail: p.answer }];
      case "judge.verdict": return [who, { main: `Spec judge round ${p.round}: ${p.result}`, detail: p.blocking ? `${p.blocking} blocking` : "" }];
      case "review.findings": return [who, { main: `Review round ${p.round}: ${p.count} findings`, detail: item }];
      case "merge.verdict": return [who, { main: `Merge judge: ${p.result}`, detail: item }];
      case "cr.opened": return [who, { main: p.design ? "Design change request opened" : `Change request opened for ${item}`, detail: p.url }];
      case "cr.merged": return ["human", { main: `Merged ${item}` }];
      case "deploy.started": return [who, { main: `Dev deploy started for ${item}` }];
      case "deploy.succeeded": return [who, { main: `Dev deploy succeeded for ${item}`, detail: p.environment_url }];
      case "deploy.failed": return [who, { main: `Dev deploy failed for ${item}` }];
      case "e2e.dev_passed": return [who, { main: `Dev checks passed for ${item}` }];
      case "e2e.dev_failed": return [who, { main: `Dev checks failed for ${item}` }];
      case "session.finished": return [who, { main: `Session finished: ${p.agent_id || "agent"}`, detail: p.list_cost_usd != null ? money(p.list_cost_usd) : "" }];
      case "work_item.stage": return [who, { main: `${ev.work_item_id}: ${ITEM_STAGE[p.stage] || p.stage}` }];
      case "run.escalated": return [who, { main: "Escalated", detail: p.reason }];
      case "run.completed": return [who, { main: "Run completed", detail: (p.specs_with_change_requests || []).join(", ") }];
      case "run.failed": return [who, { main: "Run failed", detail: p.reason }];
      case "budget.reached": return [who, { main: "Budget reached" }];
      default: return [who, { main: ev.kind }];
    }
  }

  // ---- gate review
  async function viewGate(gateId) {
    setNav("home");
    const g = await api(`/api/gates/${gateId}`);
    const rec = g.gate, rv = g.review;
    const notes = new Map();     // item id -> textarea
    const picks = new Map();     // spec id -> checkbox
    const frag = document.createDocumentFragment();

    frag.append(el("p", { class: "small muted crumb" }, el("a", { href: `#/runs/${g.run_id}` }, g.run_title), el("span", null, `opened ${ago(rec.opened_at)}`)));
    frag.append(el("h1", { class: "page", style: "margin-top:4px" }, GATE_NAME[rec.kind] || rec.kind));

    const doc = el("div", { class: "doc" });
    const prevDecisions = rv.previous_decisions || [];
    if (prevDecisions.length) {
      const last = prevDecisions[prevDecisions.length - 1];
      doc.append(el("div", { class: "prev" }, el("b", null, `Revision after your earlier decision (${DECISION_LABEL[last.decision] || last.decision})`),
        last.comments ? el("div", null, last.comments) : null));
    }

    if (rv.kind === "decomposition" && rv.decomposition) renderDecomposition(doc, rv, notes);
    else if (rv.kind === "specs" && rv.verdict) renderSpecs(doc, rv, notes, picks, rec.options.includes("approve_subset"));
    else if (rv.kind === "deploy") renderDeploy(doc, rv, rec);
    else doc.append(el("div", { class: "summary" }, rec.summary));

    const rail = buildRail(g, notes, picks);
    frag.append(el("div", { class: "docket" }, doc, rail.node));
    $main.replaceChildren(frag);
    startPoll(async () => {
      try { const fresh = await api(`/api/gates/${gateId}`); rail.update(fresh); } catch (e) { /* keep the page */ }
    }, 4000);
  }

  function noteField(id, placeholder, notes) {
    const ta = el("textarea", { placeholder, "aria-label": `Note on ${id}` });
    notes.set(id, ta);
    return el("div", { class: "note-field" }, ta);
  }

  function renderDecomposition(doc, rv, notes) {
    const d = rv.decomposition, o = rv.outcome || {};
    doc.append(el("h2", null, "The decomposer's summary"));
    doc.append(el("div", { class: "summary" }, d.summary));
    doc.append(el("p", { class: "verdict-line" }, `Revision ${d.revision}, ${d.work_items.length} work items.`,
      rv.checks.length ? ` Deterministic checks: ${rv.checks.every(c => c.passed) ? "all passed" : "failures below"}.` : ""));

    doc.append(el("h2", null, "Does every metric have work behind it?"));
    const metrics = (o.success_metrics || []).map(m => m.metric);
    const cov = Object.fromEntries((d.coverage || []).map(c => [c.metric, c]));
    const tbl = el("table", { class: "coverage" }, el("tr", null, el("th", null, "Success metric"), el("th", null, "Target"), el("th", null, "Covered by"), el("th", null, "Why")));
    for (const m of (metrics.length ? metrics : Object.keys(cov))) {
      const c = cov[m] || {}; const ids = c.work_item_ids || [];
      const target = ((o.success_metrics || []).find(x => x.metric === m) || {}).target || "";
      tbl.append(el("tr", null, el("td", null, m), el("td", { class: "serif" }, target),
        el("td", { class: ids.length ? "ok" : "gap" }, ids.length ? ids.join(", ") : "not covered"), el("td", { class: "serif" }, c.rationale || "")));
    }
    doc.append(tbl);

    doc.append(el("h2", null, "Work items"));
    for (const w of d.work_items) {
      doc.append(el("div", { class: "item", id: `wi-${w.work_item_id}` },
        el("div", { class: "item-head" }, el("span", { class: "id" }, w.work_item_id), el("span", { class: "title" }, w.title),
          el("span", { class: "facts" }, [w.kind, w.size ? `size ${w.size}` : null].filter(Boolean).join(", "),
            w.human_judgement_required ? el("span", { class: "judge" }, "needs your judgement") : null)),
        el("div", { class: "intent" }, w.intent),
        el("div", { class: "deps" }, (w.depends_on || []).length ? `Depends on ${w.depends_on.join(", ")}. ` : "No dependencies. ",
          (w.contributes_to_metrics || []).length ? `Moves: ${w.contributes_to_metrics.join("; ")}.` : ""),
        (w.touches || []).length ? el("details", null, el("summary", null, "Where it touches the code"), el("ul", { class: "paths" }, w.touches.map(t => el("li", null, typeof t === "string" ? t : JSON.stringify(t))))) : null,
        noteField(w.work_item_id, "Anything the decomposer should change about this item?", notes)));
    }

    doc.append(el("h2", null, "Open questions"));
    const qs = d.open_questions || [];
    if (!qs.length) doc.append(el("p", { class: "muted" }, "None."));
    else doc.append(el("ul", { class: "questions" }, qs.map(q => el("li", null, q.question,
      el("div", { class: "q-meta" }, `${q.blocking ? "Blocking. " : ""}Default if unanswered: ${q.default_if_unanswered || "none"}`)))));

    if ((d.explicitly_out_of_scope || []).length) {
      doc.append(el("h2", null, "Explicitly out of scope"));
      doc.append(el("ul", { class: "reading" }, d.explicitly_out_of_scope.map(x => el("li", null, x))));
    }
    if ((d.assumptions || []).length || (d.risks || []).length) {
      doc.append(el("h2", null, "Assumptions and risks"));
      doc.append(el("ul", { class: "reading" }, [...(d.assumptions || []), ...(d.risks || [])].map(x => el("li", null, typeof x === "string" ? x : (x.risk || x.description || JSON.stringify(x))))));
    }
    if (rv.checks.length) {
      doc.append(el("h2", null, "Deterministic checks"));
      doc.append(el("ul", { class: "checks" }, rv.checks.map(c => el("li", { class: c.passed ? "ok" : "bad" }, el("code", null, c.check), " ", c.detail))));
    }
    if (rv.decomposition_md) {
      doc.append(el("h2", null, "The full decomposition"));
      doc.append(el("div", { class: "reading", html: md(rv.decomposition_md) }));
    }
  }

  function renderSpecs(doc, rv, notes, picks, subsetAllowed) {
    const v = rv.verdict, o = rv.outcome || {}, specs = rv.specs;
    const ov = v.overall || {};
    doc.append(el("h2", null, rv.escalation ? "The judge could not pass this set" : "The judge's summary"));
    doc.append(el("div", { class: "summary" }, ov.summary_for_human || ""));
    doc.append(el("p", { class: "verdict-line" }, "Result ", el("b", { class: ov.result }, words(ov.result)), ` after ${v.round} round${v.round === 1 ? "" : "s"}, ${ov.blocking_issue_count || 0} blocking issue${ov.blocking_issue_count === 1 ? "" : "s"}, ${Object.keys(specs).length} specs.`));
    doc.append(el("nav", { class: "spec-nav", "aria-label": "Specs" }, Object.keys(specs).map(sid => el("a", { href: `#spec-${sid}`, onclick: e => { e.preventDefault(); document.getElementById(`spec-${sid}`)?.scrollIntoView({ behavior: "smooth", block: "start" }); } }, sid))));

    doc.append(el("h2", null, "Does every metric have acceptance criteria behind it?"));
    const tbl = el("table", { class: "coverage" }, el("tr", null, el("th", null, "Success metric"), el("th", null, "Assessment"), el("th", null, "Acceptance criteria"), el("th", null, "Note")));
    for (const gc of v.goal_coverage || []) tbl.append(el("tr", null, el("td", null, gc.metric),
      el("td", { class: gc.assessment === "covered" ? "ok" : "gap" }, words(gc.assessment)), el("td", { class: "mono small" }, (gc.covering_acs || []).join(", ")), el("td", { class: "serif" }, gc.note || "")));
    doc.append(tbl);

    const coh = v.coherence || {};
    const cohItems = Object.entries(coh).flatMap(([bucket, items]) => (items || []).map(it => ({ bucket, it })));
    if (cohItems.length) {
      doc.append(el("h2", null, "Across the set"));
      doc.append(el("ul", { class: "reading" }, cohItems.map(({ bucket, it }) => el("li", null, el("b", { class: "small", style: "font-family:var(--sans)" }, bucket.replace(/_/g, " ") + ": "),
        typeof it === "string" ? it : (it.description || JSON.stringify(it)), it.related_spec_ids ? el("span", { class: "muted small" }, ` (${it.related_spec_ids.join(", ")})`) : null))));
    }

    const issuesBy = Object.fromEntries((v.per_spec || []).map(ps => [ps.spec_id, ps]));
    for (const [sid, s] of Object.entries(specs)) {
      const ps = issuesBy[sid] || { issues: [] };
      const cb = el("input", { type: "checkbox", checked: true });
      picks.set(sid, cb);
      const head = el("div", { class: "item-head" }, el("span", { class: "id" }, sid), el("span", { class: "title" }, s.title),
        subsetAllowed ? el("label", { class: "pick" }, cb, "Include in approval") : el("span", { class: "facts" }, `size ${s.estimated_size || "?"}`));
      const acs = el("table", null, el("tr", null, el("th", null, "Criterion"), el("th", null, "Given, when, then"), el("th", null, "How it is verified")));
      for (const ac of s.acceptance_criteria || []) acs.append(el("tr", null,
        el("td", null, el("code", null, ac.ac_id), el("div", { class: "small muted" }, ac.user_visible ? "user-visible" : "internal")),
        el("td", { class: "ac" }, el("b", null, "Given"), ac.given, " ", el("b", null, "When"), ac.when, " ", el("b", null, "Then"), ac.then),
        el("td", null, words(ac.verification))));
      const issues = (ps.issues || []).map(i => el("div", { class: "issue" }, el("span", { class: `sev ${i.severity}` }, i.severity), i.description,
        i.suggested_resolution ? el("div", { class: "small muted" }, i.suggested_resolution) : null));
      const tp = s.test_plan || {};
      doc.append(el("div", { class: "item", id: `spec-${sid}` }, head,
        el("div", { class: "intent" }, s.goal),
        el("h3", null, "Acceptance criteria"), acs,
        issues.length ? el("div", null, el("h3", null, "What the judge flagged"), ...issues) : null,
        el("details", null, el("summary", null, `Interfaces (${(s.interfaces || []).length}), allowed paths (${(s.allowed_paths || []).length}), test plan`),
          (s.interfaces || []).length ? el("ul", { class: "reading", style: "font-family:var(--sans);font-size:14px" }, s.interfaces.map(i => el("li", null, el("code", null, i.name), ` ${i.kind}, ${i.change}`, el("pre", { class: "block" }, i.definition)))) : null,
          el("h3", null, "Allowed paths"), el("ul", { class: "paths" }, (s.allowed_paths || []).map(p => el("li", null, p))),
          el("h3", null, "Test plan"), el("ul", { class: "paths" }, (tp.commands || []).map(c => el("li", null, c))),
          (tp.e2e || []).length ? el("ul", { class: "paths" }, tp.e2e.map(e => el("li", null, `${e.command}  (${e.target}, covers ${(e.covers_ac || []).join(", ")})`))) : null),
        (s.open_questions || []).length ? el("details", null, el("summary", null, `Open questions (${s.open_questions.length})`), el("ul", { class: "questions" }, s.open_questions.map(q => el("li", null, q.question, el("div", { class: "q-meta" }, `Default: ${q.default_if_unanswered || "none"}`))))) : null,
        rv.spec_md[sid] ? el("details", null, el("summary", null, "The spec as written"), el("div", { class: "reading", html: md(rv.spec_md[sid]) })) : null,
        noteField(sid, "Anything the writer should change in this spec?", notes)));
    }
  }

  function renderDeploy(doc, rv, rec) {
    doc.append(el("h2", null, "What happened"));
    doc.append(el("div", { class: "summary" }, rec.summary));
    if (rv.report) {
      doc.append(el("h2", null, "Log excerpt"));
      doc.append(el("pre", { class: "block" }, rv.report.log_excerpt || "(none)"));
      if (rv.report.environment_url) doc.append(el("p", null, "Environment: ", el("a", { href: rv.report.environment_url, target: "_blank", rel: "noopener" }, rv.report.environment_url)));
    }
  }

  function buildRail(g, notes, picks) {
    const rec = g.gate;
    const comment = el("textarea", { placeholder: "What should change, or why you decided this", "aria-label": "Comment for the agent" });
    const status = el("div");
    const error = el("div", { class: "error", role: "alert" });
    const counts = el("div", { class: "counts" });
    const node = el("aside", { class: "rail", "aria-label": "Sign-off" });

    const composeComments = () => {
      const lines = [];
      if (comment.value.trim()) lines.push(comment.value.trim());
      for (const [id, ta] of notes) if (ta.value.trim()) lines.push(`${id}: ${ta.value.trim()}`);
      return lines.join("\n");
    };
    const subset = () => [...picks].filter(([, cb]) => cb.checked).map(([sid]) => sid);
    const refreshCounts = () => {
      const n = [...notes.values()].filter(ta => ta.value.trim()).length;
      const picked = picks.size ? subset().length : null;
      counts.textContent = [n ? `${n} note${n === 1 ? "" : "s"} on items` : "", picked !== null ? `${picked} of ${picks.size} specs selected` : ""].filter(Boolean).join(", ");
      buttons.forEach(b => {
        if (b.dataset.decision === "approve_subset") b.disabled = picked === 0 || picked === picks.size;
        if (b.dataset.decision === "approve" && picks.size) b.disabled = picked !== picks.size;
        if (b.dataset.decision === "request_changes") b.disabled = !composeComments();
      });
    };
    notes.forEach(ta => ta.addEventListener("input", refreshCounts));
    picks.forEach(cb => cb.addEventListener("change", refreshCounts));
    comment.addEventListener("input", refreshCounts);

    const sub = {
      approve: "Everything shown moves to the next stage.", approve_subset: "Selected specs go ahead; the rest return to the writer with your notes.",
      request_changes: "Your comment and notes go back to the agent verbatim.", reject: rec.kind === "deploy_failure" ? "Closes this work item." : "Closes the run.",
    };
    if (rec.kind === "deploy_failure") { sub.approve = "The failure was the platform's; watch the next pipeline."; sub.request_changes = "Back to an implementer on a new branch with your comment."; }
    const buttons = rec.options.map(opt => el("button", { class: `btn ${opt === "approve" || opt === "approve_subset" ? "approve" : opt === "request_changes" ? "changes" : "reject"}`,
      type: "button", dataset: { decision: opt }, onclick: () => decide(opt) }, DECISION_LABEL[opt] || opt, el("small", null, sub[opt] || "")));

    async function decide(opt) {
      error.textContent = "";
      const body = { decision: opt, comments: composeComments(), approved_subset: opt === "approve_subset" ? subset() : [], by: "console" };
      if (opt === "request_changes" && !body.comments) { error.textContent = "Say what should change first."; return; }
      if (opt === "reject" && !window.confirm(rec.kind === "deploy_failure" ? "Close this work item?" : "Reject and close the run?")) return;
      buttons.forEach(b => b.disabled = true);
      try {
        const r = await api(`/api/gates/${rec.gate_id}/decision`, { body });
        showRecorded({ ...r.recorded, decision: opt });
      } catch (e) { error.textContent = e.message; refreshCounts(); }
    }

    function showRecorded(d) {
      const cls = d.decision === "request_changes" ? "changes" : d.decision === "reject" ? "reject" : "";
      status.replaceChildren(el("div", { class: `recorded ${cls}` }, el("b", null, `${DECISION_LABEL[d.decision] || d.decision} recorded`),
        `by ${d.decided_by}, ${ago(d.decided_at)}. The run picks it up within a few seconds; the hashes above travel with it.`,
        el("div", { style: "margin-top:8px" }, el("a", { href: `#/runs/${g.run_id}` }, "Back to the run"))));
      form.hidden = true;
    }
    function showDecided(p) {
      const cls = p.decision === "request_changes" ? "changes" : p.decision === "reject" ? "reject" : "";
      status.replaceChildren(el("div", { class: `recorded ${cls}` }, el("b", null, `${DECISION_LABEL[p.decision] || p.decision}, applied`),
        "The run has moved on. ", el("a", { href: `#/runs/${g.run_id}` }, "See where it is now."), p.comments ? el("div", { class: "small", style: "margin-top:6px" }, `“${p.comments}”`) : null));
      form.hidden = true;
    }

    const form = el("div", null,
      el("label", { for: "rail-comment" }, "Comment for the agent"), comment, counts,
      el("div", { class: "actions" }, buttons), error);
    comment.id = "rail-comment";

    node.append(el("h2", null, "Sign-off"),
      el("ul", { class: "files" }, rec.artifacts.map(a => el("li", null, el("span", null, a.name), el("code", { title: a.sha256 }, a.sha256.slice(0, 10))))),
      el("p", { class: "note" }, "Your decision binds to these exact files. If an agent rewrote one in the meantime, the decision is refused and the gate reopens."),
      notes.size ? el("p", { class: "note" }, "Notes you leave under an item travel with Request changes, prefixed by the item's id.") : null,
      status, form);

    if (g.decided) showDecided(g.decided);
    else if (rec.recorded) showRecorded(rec.recorded);
    refreshCounts();
    return { node, update(fresh) { if (fresh.decided && !form.hidden) showDecided(fresh.decided); else if (fresh.decided) showDecided(fresh.decided); } };
  }

  // ---- outcome form
  async function viewNew(params) {
    setNav("new");
    const t = (await api("/api/outcomes/template")).outcome;
    const example = t._example;
    let o = JSON.parse(JSON.stringify(t)); delete o._example;
    if (params.get("example")) o = JSON.parse(JSON.stringify(example));
    if (params.get("outcome")) { try { o = (await api(`/api/outcomes/${params.get("outcome")}`)).outcome; } catch (e) { /* fall through */ } }

    const frag = document.createDocumentFragment();
    frag.append(el("h1", { class: "page" }, o.title ? o.title : "A new outcome"),
      el("p", { class: "lede" }, "Describe the business result in your own words, with metrics that can be measured. The factory decomposes it; you approve the shape before any code is written."));

    const form = el("form", { class: "form", onsubmit: e => e.preventDefault() });
    const f = {};
    const field = (key, label, help, input) => { f[key] = input; return el("div", { class: "field" }, el("label", { class: "lab", for: input.id = `f-${key}` }, label), help ? el("p", { class: "help" }, help) : null, input); };
    form.append(field("statement", "What should be true when this is done?", "Plain language, from the business side. The decomposer reads this first.",
      el("textarea", { class: "big", value: o.statement || "" })));
    f.statement.value = o.statement || "";
    form.append(field("title", "A short title", null, el("input", { type: "text", value: o.title || "" })));

    const metricsBox = el("div");
    const metricRows = [];
    const addMetric = (m = {}) => {
      const row = { metric: el("input", { type: "text", value: m.metric || "", placeholder: "Involuntary churn rate" }),
        baseline: el("input", { type: "text", value: m.baseline || "", placeholder: "4.1% per month (leave empty if unknown)" }),
        target: el("input", { type: "text", value: m.target || "", placeholder: "Below 2.9% within two months" }),
        measurement: el("input", { type: "text", value: m.measurement || "", placeholder: "Where it is read from" }) };
      const node = el("div", { class: "metric" }, el("div", { class: "grid2" }, el("div", null, el("label", { class: "help" }, "Metric"), row.metric), el("div", null, el("label", { class: "help" }, "Target"), row.target)),
        el("div", { class: "grid2" }, el("div", null, el("label", { class: "help" }, "Baseline today"), row.baseline), el("div", null, el("label", { class: "help" }, "How it is measured"), row.measurement)),
        el("button", { class: "rm", type: "button", onclick: () => { node.remove(); metricRows.splice(metricRows.indexOf(row), 1); } }, "Remove this metric"));
      metricRows.push(row); metricsBox.append(node);
    };
    (o.success_metrics && o.success_metrics.length ? o.success_metrics : [{}]).forEach(addMetric);
    form.append(el("div", { class: "field" }, el("label", { class: "lab" }, "Success metrics"), el("p", { class: "help" }, "Each needs a target. A vague metric is refused."),
      metricsBox, el("button", { class: "add", type: "button", onclick: () => addMetric() }, "Add a metric")));

    const c = o.constraints || {};
    form.append(el("div", { class: "field" }, el("label", { class: "lab" }, "Constraints"),
      el("div", { class: "grid2" },
        el("div", null, el("label", { class: "help", for: "f-must_not" }, "Must not (one per line)"), f.must_not = el("textarea", { id: "f-must_not" })),
        el("div", null, el("label", { class: "help", for: "f-compliance" }, "Compliance (one per line)"), f.compliance = el("textarea", { id: "f-compliance" }))),
      el("div", { class: "grid2", style: "margin-top:10px" },
        el("div", null, el("label", { class: "help", for: "f-budget" }, "Budget in USD"), f.budget = el("input", { id: "f-budget", type: "number", min: "1", value: c.budget_usd ?? 150 })),
        el("div", null, el("label", { class: "help", for: "f-deadline" }, "Deadline"), f.deadline = el("input", { id: "f-deadline", type: "date", value: c.deadline || "" })))));
    f.must_not.value = (c.must_not || []).join("\n"); f.compliance.value = (c.compliance || []).join("\n");

    const repo = (o.target_repositories || [{}])[0] || {};
    form.append(el("div", { class: "field" }, el("label", { class: "lab" }, "Target repository"),
      el("div", { class: "grid2" },
        el("div", null, el("label", { class: "help", for: "f-repo" }, "HTTPS URL"), f.repo = el("input", { id: "f-repo", type: "text", value: repo.url || "", placeholder: "https://github.com/you/app" })),
        el("div", null, el("label", { class: "help", for: "f-branch" }, "Default branch"), f.branch = el("input", { id: "f-branch", type: "text", value: repo.default_branch || "main" }))),
      el("label", { class: "help", for: "f-paths", style: "margin-top:10px" }, "Paths in scope (one per line, globs allowed)"), f.paths = el("textarea", { id: "f-paths" })));
    f.paths.value = (repo.paths_in_scope || []).join("\n");

    const ow = o.owner || {};
    form.append(el("div", { class: "field" }, el("label", { class: "lab" }, "Owner"),
      el("div", { class: "grid3" },
        el("div", null, el("label", { class: "help", for: "f-oname" }, "Name"), f.oname = el("input", { id: "f-oname", type: "text", value: ow.name || "" })),
        el("div", null, el("label", { class: "help", for: "f-ohandle" }, "Forge handle"), f.ohandle = el("input", { id: "f-ohandle", type: "text", value: ow.handle || "", placeholder: "your GitHub login" })),
        el("div", null, el("label", { class: "help", for: "f-orole" }, "Role"), f.orole = el("input", { id: "f-orole", type: "text", value: ow.role || "" })))));

    const errors = el("div", { hidden: true });
    const saved = el("div", { hidden: true });
    let savedId = o.outcome_id && params.get("outcome") ? o.outcome_id : null;
    const lines = ta => ta.value.split("\n").map(s => s.trim()).filter(Boolean);
    const collect = () => ({
      outcome_id: savedId || "", title: f.title.value.trim(), statement: f.statement.value.trim(),
      success_metrics: metricRows.map(r => ({ metric: r.metric.value.trim(), baseline: r.baseline.value.trim() || null, target: r.target.value.trim(), measurement: r.measurement.value.trim() })).filter(m => m.metric || m.target),
      constraints: { must_not: lines(f.must_not), compliance: lines(f.compliance), budget_usd: Number(f.budget.value) || undefined, deadline: f.deadline.value || undefined },
      context_links: o.context_links || [], clarifications: o.clarifications || [],
      target_repositories: [{ url: f.repo.value.trim(), default_branch: f.branch.value.trim() || "main", paths_in_scope: lines(f.paths) }],
      owner: { name: f.oname.value.trim(), handle: f.ohandle.value.trim(), role: f.orole.value.trim() },
      created_at: o.created_at || undefined,
    });

    const live = state.config && state.config.can_run_live;
    const runBtns = el("span", { style: "display:inline-flex;gap:10px;flex-wrap:wrap" },
      el("button", { class: "btn inline approve", type: "button", onclick: () => start("live"), disabled: !live, title: !live && state.config ? state.config.gaps.join("\n") : "" },
        "Save and start the run", el("small", null, live ? "On the live platform" : "Needs configuration; see factory doctor")),
      el("button", { class: "btn inline", type: "button", onclick: () => start("demo") }, "Save and run as a demo", el("small", null, "Simulated agents, real gates")));
    async function save() {
      errors.hidden = true; saved.hidden = true;
      const r = await api("/api/outcomes", { body: { outcome: collect() } });
      if (!r.ok) { errors.replaceChildren(el("div", { class: "errors" }, el("b", null, "Not saved yet. "), ...r.errors.map(e => el("div", null, e)))); errors.hidden = false; return null; }
      savedId = r.outcome_id;
      saved.replaceChildren(el("div", { class: "saved" }, el("b", null, "Saved. "), `Outcome ${r.outcome_id}.`)); saved.hidden = false;
      return r;
    }
    async function start(mode) {
      const r = await save(); if (!r) return;
      try {
        const r = await api("/api/runs", { body: { outcome_id: savedId, mode } });
        if (r.run_id) location.hash = `#/runs/${r.run_id}`;
        else { saved.replaceChildren(el("div", { class: "saved" }, el("b", null, "Run started. "), `The orchestrator is running (pid ${r.pid}); it appears on the runs board within a minute. Log: ${r.log}`)); saved.hidden = false; }
      } catch (e) { errors.replaceChildren(el("div", { class: "errors" }, e.message)); errors.hidden = false; }
    }

    form.append(errors, saved, el("div", { class: "bar" },
      el("button", { class: "btn inline", type: "button", onclick: save }, "Save outcome"), runBtns,
      el("span", { class: "spacer" }),
      params.get("example") ? null : el("a", { href: "#/new?example=1", class: "small" }, "Load the Ledgerline example instead")));
    frag.append(form);
    $main.replaceChildren(frag);
    stopPoll();
  }

  // ------------------------------------------------------------------ router
  async function route() {
    const hash = location.hash || "#/";
    const [path, query] = hash.slice(1).split("?");
    const params = new URLSearchParams(query || "");
    try {
      if (path === "/" || path === "") await viewHome();
      else if (path === "/new") await viewNew(params);
      else if (path.startsWith("/runs/")) await viewRun(path.slice(6));
      else if (path.startsWith("/gates/")) await viewGate(path.slice(7));
      else { $main.replaceChildren(el("div", { class: "empty" }, "No such page. ", el("a", { href: "#/" }, "Back to runs"))); stopPoll(); }
    } catch (e) {
      stopPoll();
      $main.replaceChildren(el("h1", { class: "page" }, "Something did not load"), el("div", { class: "empty" }, e.message, " ", el("a", { href: "#/" }, "Back to runs")));
    }
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", route);
  loadContext().then(route);
})();
