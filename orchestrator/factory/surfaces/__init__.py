"""Gate surfaces: where a human reads a gate and where their decision is read from (ADR-0007).

- `tracker.py`: the issue tracker as board and record (Linear; Jira stub). Decisions by label + comment.
- `artifact_pages.py`: one self-contained HTML page per gate, for reading; published locally or as an artifact.
- The forge's design change request (`runner._design_change_request`) remains the `forge` surface.
- The CLI's file queue (`cli.FileQueue`) is the `file` surface and is always available alongside the others.
"""
