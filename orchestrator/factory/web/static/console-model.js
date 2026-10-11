/* Evidence-derived presentation. No requests, timers, or DOM: shared by the UI and node tests. */
(function (root) {
  "use strict";
  const phases = {
    intake: "Preparing the run", decomposing: "Planning the work", gate_1_decomposition: "Plan ready for review",
    specifying: "Writing and checking contracts", gate_2_spec_readiness: "Contracts ready for review",
    implementing: "Building and reviewing", completed: "Run finished",
  };
  const roleNames = {codebase_researcher:"Researcher", decomposer:"Decomposer", spec_writer:"Spec writer", spec_judge:"Spec judge", implementer:"Implementer", reviewer:"Reviewer", security_reviewer:"Security reviewer", merge_judge:"Merge judge", integrator:"Integrator"};
  const terminal = new Set(["failed", "closed", "escalated", "completed"]);
  const words = x => String(x || "").replace(/_/g, " ");
  function activeSessions(r) {
    if (terminal.has(r.stage) || r.summary?.terminal || r.summary?.status === "stopped" || r.summary?.controller_alive === false) return [];
    const sessions = new Map();
    for (const e of r.events || []) {
      const p = e.payload || {};
      if (e.kind === "run.resumed") sessions.clear();
      if (e.kind === "session.started" && p.session_id) sessions.set(p.session_id, {
        id:p.session_id, role:p.agent_key || "agent", name:roleNames[p.agent_key] || words(p.agent_key) || "Agent",
        title:(p.title || "").replace(`${r.run_id} `,""), since:e.at, work_item_id:e.work_item_id, spec_id:e.spec_id,
      });
      if (e.kind === "session.finished") sessions.delete(p.session_id);
      if (e.kind === "run.failed") for (const id of p.interrupted_sessions || []) sessions.delete(id);
    }
    return [...sessions.values()];
  }
  function lastPhase(r) {
    if (phases[r.stage]) return r.stage;
    return [...(r.events || [])].reverse().find(e => e.kind === "stage.entered" && phases[e.payload?.stage])?.payload.stage || "intake";
  }
  function narrative(r, now = Date.now()) {
    const s = r.summary || {}, sessions = activeSessions(r);
    const questions = (r.questions || []).filter(q => !q.answered);
    const gates = (r.gates || []).filter(g => !g.recorded);
    const recorded = (r.gates || []).filter(g => g.recorded);
    let title = phases[r.stage] || words(r.stage), next = "Factory will show the next decision here.", tone = "crew";
    const roles = [...new Set(sessions.map(x => x.role))];
    if (roles.length === 1) title = ({decomposer:"Planning the work",codebase_researcher:"Researching the codebase",spec_writer:"Writing contracts",spec_judge:"Checking the contracts",implementer:"Implementing the changes",reviewer:"Reviewing the changes",security_reviewer:"Checking security",merge_judge:"Checking merge readiness",integrator:"Preparing pull requests"})[roles[0]] || title;
    if (r.stage === "intake" || r.stage === "decomposing") next = "Next: review the plan before contracts are written.";
    if (r.stage === "specifying") next = "Next: review the contracts before implementation starts.";
    if (r.stage === "implementing") next = "Next: review the pull requests on the code host.";
    if (gates.length) { title = r.stage === "gate_1_decomposition" ? "Your plan review is ready" : r.stage === "gate_2_spec_readiness" ? "Your contract review is ready" : "A decision needs you"; next = "Review the evidence and choose how to continue."; tone = "gate"; }
    if (questions.length) { title = questions.some(q => q.blocking) ? "Waiting for your answer" : "The crew has a question"; next = questions.some(q => q.blocking) ? "Answer below to unblock the agent." : "Answer below, or the agent can use its stated default."; tone = "gate"; }
    if (recorded.length && !gates.length && !questions.length) { title = "Decision recorded"; next = "Waiting for the controller to apply your decision. This page will update."; }
    if (r.stage === "completed") {
      title = r.design_only ? "Design approved" : (s.pull_requests || []).some(p => p.stage === "cr_open") ? "Ready for code review" : "Run finished";
      next = r.design_only ? "The design is complete. No implementation ran in this design-only run." : s.headline || "Review the delivery evidence below.";
      tone = "ok";
    }
    if (r.stage === "paused_budget") { title = "Paused at the budget limit"; next = "Review the required budget below, then continue or close the run."; tone = "warn"; }
    if (r.stage === "failed") { title = "The run needs recovery"; next = r.resumable ? "Review the failure below. Resume rechecks the branch and continues from saved work." : "Review the failure and saved work before starting another run."; tone = "bad"; }
    if (r.stage === "escalated") { title = "The crew needs your judgement"; next = gates.length ? "Review the escalation below and choose how to continue." : "Review the recorded reason before starting another run."; tone = "warn"; }
    if (r.stage === "closed") { title = "Run closed"; next = "No more work is scheduled. The history and files remain available."; tone = "neutral"; }
    if (s.status === "stopped") { title = "The controller has stopped"; next = "No controller is advancing this run. Review diagnostics before restarting it."; tone = "bad"; }
    const quiet = s.status === "running" && s.controller_alive == null && !s.live_sessions && !!s.updated_at && now - Date.parse(s.updated_at) > 3600e3;
    if (quiet) { title = "No recent activity confirmed"; next = "Inspect the event log and controller diagnostics before assuming this run is advancing."; tone = "warn"; }
    const transitions = new Set(["run.created","stage.entered","gate.opened","gate.decided","question.asked","question.answered","run.failed","run.escalated","run.completed","run.resumed","budget.reached","budget.raised","session.truncated","session.started","review.findings","merge.verdict","judge.verdict"]);
    const recent = [...(r.events || [])].reverse();
    let event = recent.find(e => transitions.has(e.kind));
    // A parallel session starting cannot replace the reason a current decision is blocking the user.
    if (questions.length) event = recent.find(e => e.kind === "question.asked" && questions.some(q => q.question_id === e.payload?.question_id)) || event;
    else if (gates.length) event = recent.find(e => e.kind === "gate.opened" && gates.some(g => g.gate_id === e.payload?.gate_id)) || event;
    const p = event?.payload || {};
    let reason = "No transition reason has been recorded yet.";
    if (event) {
      const gate = p.kind === "gate_1_decomposition" ? "the plan" : p.kind === "gate_2_spec_readiness" ? "the contracts" : "the decision";
      switch (event.kind) {
        case "session.started": reason = p.reason || `The controller opened a ${roleNames[p.agent_key]?.toLowerCase() || words(p.agent_key) || "new agent"} session${p.title ? `: ${p.title.replace(`${r.run_id} `,"")}` : "."}`; break;
        case "review.findings": reason = `Review round ${p.round || "—"} reported ${p.count ?? "an unknown number of"} findings${p.blocking != null ? `, ${p.blocking} blocking` : ""}.`; break;
        case "merge.verdict": reason = `The merge judge returned: ${words(p.result)}.`; break;
        case "judge.verdict": reason = `The spec judge returned ${words(p.result)} on round ${p.round || "—"}${p.blocking ? `, with ${p.blocking} blocking issues` : ""}.`; break;
        case "run.created": reason = "The outcome was submitted to Factory."; break;
        case "gate.decided": reason = p.decision === "request_changes" ? `Changes were requested to ${gate}.` : p.decision === "reject" ? `The owner rejected ${gate}.` : `The owner approved ${gate}.`; break;
        case "gate.opened": if (p.kind === "budget" && r.budget?.pause?.next) { const b=r.budget.pause; reason=`$${Number(b.spent_usd).toFixed(2)} has been recorded. The next step could cost up to $${Number(b.next.cap_usd).toFixed(2)}, exceeding the $${Number(b.budget_usd).toFixed(2)} budget.`; break; } reason = p.kind === "gate_1_decomposition" ? "The plan was submitted for human review." : p.kind === "gate_2_spec_readiness" ? "The contracts were submitted for human review." : p.summary || "Factory opened a decision for the owner."; break;
        case "question.asked": reason = p.question || "An agent requested input from the owner."; break;
        case "question.answered": reason = "The owner answered the agent’s question."; break;
        case "run.failed": case "run.escalated": reason = p.reason || "Factory did not record a reason."; break;
        case "budget.reached": reason = p.next ? "The next step could exceed the remaining run budget." : p.reason || "An agent session reached its limit."; break;
        case "budget.raised": reason = `The owner raised the budget to $${Number(p.budget_usd).toFixed(2)}.`; break;
        case "session.truncated": reason = p.reason || "An agent session ended before its work was complete."; break;
        case "run.resumed": reason = "The controller resumed from saved work."; break;
        case "run.completed": reason = p.design_only || r.design_only ? "The approved design reached the end of this run." : "The controller finished its assigned work. Delivery status is shown separately below."; break;
        case "stage.entered": {
          // Do not invent causality from an unrelated old approval. Only connect an adjacent approval to its known successor.
          const index = (r.events || []).indexOf(event);
          const prior = r.events.slice(0,index).reverse().find(e => transitions.has(e.kind) && !["session.started","review.findings","merge.verdict","judge.verdict"].includes(e.kind));
          const approved = prior?.kind === "gate.decided" && ["approve","approve_subset"].includes(prior.payload?.decision);
          const matched = approved && ((p.stage === "specifying" && prior.payload.kind === "gate_1_decomposition") || (p.stage === "implementing" && prior.payload.kind === "gate_2_spec_readiness"));
          reason = matched ? `The owner approved ${p.stage === "specifying" ? "the plan, so contract writing can begin" : "the contracts, so implementation can begin"}.` : `Factory entered ${phases[p.stage]?.toLowerCase() || words(p.stage)}. No further reason was recorded for this transition.`;
          break;
        }
      }
    }
    if (quiet) reason = "No controller or open agent session is confirmed, and no activity has been recorded for over an hour.";
    if (s.status === "stopped") reason = "The controller is no longer running. It did not record why it stopped.";
    return {title,next,tone,reason,at:s.status === "stopped" ? null : event?.at, eventKind:s.status === "stopped" ? null : event?.kind, sessions,phase:lastPhase(r)};
  }
  const api = {activeSessions,lastPhase,narrative,phases,roleNames};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FactoryModel = api;
})(typeof window === "undefined" ? globalThis : window);
