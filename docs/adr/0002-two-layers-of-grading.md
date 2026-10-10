# ADR-0002: Two layers of grading: platform outcome grader inside sessions, factory judges between them

**Status:** accepted · **Date:** 2026-10-09

## Context
Managed Agents outcomes give every session an independent grader that iterates the agent against a rubric. The brief asks for explicit judge agents (a spec judge, a review grader). Do we need both?

## Decision
Both, with distinct jobs.
- **Platform outcome grader**: the cheap inner loop inside writing sessions (decomposer, spec writer, implementer). It catches omissions and self-reported failures before the artifact leaves the session. Its reasoning is opaque to us; we see only `result` and `explanation`.
- **Factory judges** (`spec_judge`, `merge_judge`, and the reviewer as a critic): separate agents in separate sessions, scoring the same rubric criteria explicitly with evidence, plus the cross-artifact judgements the inner grader cannot make (do these specs fit together; who was right in this review exchange). Their verdicts are stored, shown to humans, and evaluated.

## Reasons
- A grader that can only see one session cannot judge coherence across specs or an exchange across sessions.
- Gate inputs must be inspectable. A human at Gate 2 needs the judge's per-criterion evidence, not a `satisfied` flag.
- Judge quality must be measurable (kappa, paired discrimination). We can evaluate an agent whose output we read; we cannot evaluate the platform grader's reasoning.
- The inner loop is still worth having: it is cheap, it reduces the rounds the expensive judge spends on trivial omissions, and Anthropic's own harness work found that tuning a separate evaluator is tractable where self-critique is not.

## Consequences
- Rubrics are written once (`rubrics/`) and used by both layers; the anchors must work for a grader that cannot ask questions.
- Two graders disagreeing is expected and informative: the inner loop said `satisfied` and the judge said `revise` means the rubric anchors or the judge need attention. Log both.
- Judging stages (spec judge, reviewer, merge judge, integrator) are kicked off with `user.message`, not an outcome, so they do not grade their own judgement.
