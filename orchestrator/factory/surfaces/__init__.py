"""Gate surfaces: where a human reads a gate and where their decision is read from (ADR-0007).

- `tracker.py`: the issue tracker as board and record (Linear; Jira stub). Decisions by label + comment.
- `artifact_pages.py`: one self-contained HTML page per gate, for reading; published locally or as an artifact.
- There is no forge surface: nothing reaches the target repository before Gate 3 (ADR-0011).
- The CLI's file queue (`surfaces/filequeue.py`) is the `web` surface and is always available alongside the others.
"""
