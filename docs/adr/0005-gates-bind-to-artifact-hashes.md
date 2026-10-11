# ADR-0005: Gate decisions bind to artifact hashes

**Status:** accepted · **Date:** 2026-10-09

## Context
A human approves what they read. Between reading and clicking, or between clicking and the orchestrator acting, an agent re-run or a race could change the artifact. The "Loopjacking" work (arXiv:2609.21081) reproduced this in several agent frameworks: the human approves A, mutable state executes B. The fix is complete canonical rendering of what is approved and exact comparison at use time.

## Decision
Every gate presents a frozen set of artifacts identified by sha256. A decision is accepted only if it carries the same set of hashes. The orchestrator refuses a stale decision (`StaleDecision`) and re-presents the gate. Approvals are recorded in the event log with the hashes. Downstream stages consume the artifact by hash from the store, not "the latest file".

## Consequences
- The design change request's commit sha serves the same purpose on the forge; the orchestrator records both. On the tracker surface the gate comment carries the hash list and the decision copies it back ([ADR-0007](0007-pluggable-gate-surfaces.md)); the implementation PR links to the approved spec and verdicts in Factory’s artifact store.
- Re-running a stage after approval (for any reason) invalidates the approval and reopens the gate.
- Surfaces (Console, Slack buttons) must carry the hashes through; the CLI does this automatically in `factory decide`, the tracker by parsing its own gate comment.
