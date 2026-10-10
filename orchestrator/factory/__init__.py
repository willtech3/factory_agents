"""Factory orchestrator.

The orchestrator is the deterministic outer loop (12-Factor Agents, factor 8: own
your control flow). It owns:

- the run state machine (`state.py`), derived from an append-only event log;
- one Managed Agents session per stage per work item (`sessions.py`, `stages/`);
- human gates and in-session questions (`gates.py`);
- deterministic checks that no agent may override (`checks.py`);
- Forge integration (`forge/`: GitHub default, GitLab second) for change requests and pipeline reads.
- Gate surfaces (`surfaces/`: tracker, artifact pages) for human gates (ADR-0007).

Agents never talk to each other. They read and write artifacts; the orchestrator
moves artifacts between sessions and decides what happens next.
"""

__version__ = "0.1.0"
