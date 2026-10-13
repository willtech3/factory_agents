# ADR-0004: The review and refutation protocol

**Status:** accepted · **Date:** 2026-10-09

## Context
The brief asks for an adversarial reviewer "with refutation" that posts findings back to the implementer, and a grader that decides when review is satisfied. The research says two things that pull against each other: an independent critic is the only configuration that beats self-review (Parmar et al.), and multi-turn reviewer-author exchanges raise false positives faster than recall (Song). Reviewers also systematically over-reject correct code (Jin and Chen).

## Decision
A structured, bounded exchange through artifacts, with execution as the arbiter.

1. **Reviewer round N** is a fresh session. It receives the spec, the diff, the implementation report (labelled as hypotheses), and for N > 1 the previous findings and the implementer's response. It must read the diff step by step, run the test plan and its own probes, and emit findings where each has a falsifiable `claim`, `evidence` (locations; reproduction for blocking/major), `violates`, and a `refutation_condition`.
2. **Implementer response** is a fresh implementer session on the same branch. Every finding gets exactly one disposition: `fixed` (commit plus a test that would have caught it), `refuted` (evidence meeting the refutation condition, typically the reviewer's reproduction re-run), or `accepted_risk` (a human decides at the MR).
3. **Reviewer round N+1** judges each prior finding against its own refutation condition (`upheld`/`withdrawn`, one sentence each) and reviews only the new hunks for new findings. No reopening without new evidence.
4. **Bound:** `review_max_rounds = 2`. After that, the merge judge decides.
5. **Merge judge** receives everything plus the orchestrator's authoritative test results and deterministic checks. It re-runs reproductions, checks refutations literally, spot-checks withdrawals, and decides `ready` / `another_round` / `escalate`. A failing deterministic check is not negotiable.
6. **Independent passes:** each review round runs `reviewer_passes` (default 2) separate reviewer sessions; findings are merged by location with the strictest severity. Since #46 the count follows the change (`review_plan`): a first round of at most `review_single_pass_lines` (150) hand-written lines gets one pass, a larger one `reviewer_passes`, and a later round `reviewer_passes_followup` (1) over the delta.

## Why this shape
- Fresh sessions per round give the independence Song's result requires while still letting the implementer answer, which the brief requires.
- Refutation conditions force the reviewer to commit to a falsifiable position and give the implementer a concrete target, which is the "evidence-gated generation" that made the critic configuration win in Parmar et al.
- Execution as arbiter (reproductions, orchestrator-run tests) counters over-rejection: a finding that does not reproduce is withdrawn on evidence, not argument (Jin and Chen's fix-as-counterfactual idea, applied to findings).
- The merge judge is the critic-over-the-review from Qiu and Gill's best configuration.
- Two passes merged is cheap insurance against single-pass variance; the eval question "does the second pass add detection" is scheduled.

## Consequences
- Review cost is roughly 2 reviewer sessions plus 1 implementer session plus 1 judge session per round.
- Over-arguing is structurally limited; the escape valve is `accepted_risk`, which puts the disagreement in front of a human with both positions stated.
- `review_findings.schema.json` and `review_response.schema.json` are the protocol; changing the protocol means changing the schemas.
