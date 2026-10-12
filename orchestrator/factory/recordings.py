"""Evals from recordings (ADR-0016): every finished agent session of a real run is an eval item.

A session step records what the session was asked (`stages.common.session_inputs_record`: the instructions, rubric,
limits and every upload, its bytes kept as a blob) and what it returned (its outputs as blobs). `items` reads them back
with what the run later made of that work, the labels:

- an implementer's round: the orchestrator's own deterministic checks of the head it pushed, and where the round went;
- a reviewer's round: the merged findings and where the round went;
- a merge judge: its verdict, after the post-conditions in code (`rules.merge_ready_violations`);
- a decomposer, spec writer or spec judge: the owner's decision at the gate that followed (Gate 1, Gate 2).

`export` writes items to a dataset directory (`items.jsonl` plus content-addressed `blobs/`) that `evals/run_eval.py`
grades again (`--regrade`) or asks of another agent version (`--replay`), and `grade` is the deterministic grading both
use: the output's schema validity on receipt and the judges' decision rules, the same code the pipeline runs.

A recording holds repository content and the owner's notes. It belongs with the run, never in a public repository.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import rules
from .checks import CheckResult, schema_errors

# The output each role is graded on, and its schema.
OUTPUTS = {"decomposer": ("decomposition.json", "decomposition.schema.json"), "spec_writer": ("spec.json", "spec.schema.json"),
           "spec_judge": ("verdict.json", "judge_verdict.schema.json"),
           "implementer": ("implementation_report.json", "implementation_report.schema.json"),
           "reviewer": ("findings.json", "review_findings.schema.json"),
           "security_reviewer": ("findings.json", "review_findings.schema.json"),
           "merge_judge": ("merge_readiness.json", "merge_readiness.schema.json")}
GATE_AFTER = {"decompose": "gate_1", "specify": "gate_2"}
# What the orchestrator stamps on receipt; a recorded output is graded as the pipeline would have read it.
STAMP = {"agent_name": "recorded", "agent_id": "agent_recorded", "agent_version": 1, "session_id": "sesn_recorded",
         "model": "recorded", "produced_at": "2026-01-01T00:00:00Z"}


@dataclass
class Item:
    item_id: str                      # <run_id>/<session step key>
    run_id: str
    stage: str
    agent_key: str
    agent_version: int | None
    inputs: dict[str, Any]            # the recorded inputs; uploads by sha256, their bytes under blobs/
    outputs: dict[str, str]           # output name -> blob sha256
    list_cost_usd: float | None
    pipeline_step: str                # the pipeline step the session ran in
    labels: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _scope(key: str, stage: str) -> str:
    """The pipeline step of a session step key `session/<stage>/<scope>/<prefix><title>#<attempt>`."""
    rest = key.removeprefix(f"session/{stage}/")
    return rest.rsplit("/", 1)[0] if "/" in rest else ""


def _labels(store: Any, run_id: str, agent_key: str, scope: str) -> dict[str, Any]:
    def result(key: str) -> Any:
        step = store.step(run_id, key)
        return step.result if step is not None and step.status == "done" else None
    if agent_key == "implementer" and scope.endswith("/author"):
        r = result(scope.removesuffix("/author")) or {}
        return {"checks": r.get("det"), "next": r.get("next"), "head_sha": r.get("head_sha")} if r else {}
    if agent_key in ("reviewer", "security_reviewer"):
        r = result(scope) or {}
        return {"merged_findings": r.get("findings"), "next": r.get("next")} if r else {}
    if agent_key == "merge_judge":
        r = result(scope) or {}
        return {"result": r.get("result"), "deterministic_checks": r.get("deterministic_checks")} if r else {}
    stage, _, n = scope.partition("#")
    if stage in GATE_AFTER and n:
        r = result(f"{GATE_AFTER[stage]}#{n}") or {}
        return {"gate": GATE_AFTER[stage], "decision": (r.get("decision") or {}).get("decision")} if r else {}
    return {}


def items(store: Any, run_id: str, agents: set[str] | None = None) -> list[Item]:
    """Every finished session of the run that recorded its inputs (sessions from before ADR-0016 did not)."""
    out = []
    for step in store.steps(run_id):
        r = step.result or {}
        if step.kind != "session" or step.status != "done" or not r.get("inputs") or not isinstance(r.get("outputs"), dict):
            continue
        inputs = json.loads(store.get_blob(run_id, r["inputs"]))
        agent_key = str(inputs.get("agent_key") or "")
        if agents and agent_key not in agents:
            continue
        stage = str(inputs.get("stage") or step.key.split("/")[1])
        scope = _scope(step.key, stage)
        out.append(Item(f"{run_id}/{step.key}", run_id, stage, agent_key, r.get("agent_version"), inputs, dict(r["outputs"]),
                        r.get("list_cost_usd"), scope, _labels(store, run_id, agent_key, scope)))
    return out


def export(store: Any, run_id: str, dest: Path, agents: set[str] | None = None) -> int:
    """Add the run's recorded sessions to the dataset at `dest`; returns how many items were new. Every blob an item
    names (its outputs, its uploads and, for labels held in blobs, those) is copied under `dest/blobs/`."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "blobs").mkdir(exist_ok=True)
    index = dest / "items.jsonl"
    known = {json.loads(line)["item_id"] for line in index.read_text().splitlines() if line.strip()} if index.exists() else set()
    new = [i for i in items(store, run_id, agents) if i.item_id not in known]
    for item in new:
        shas = set(item.outputs.values()) | {u["blob"] for u in item.inputs.get("uploads", {}).values() if u.get("blob")}
        shas |= {v for v in item.labels.values() if isinstance(v, str) and len(v) == 64}
        for sha in shas:
            target = dest / "blobs" / sha
            if not target.exists():
                target.write_bytes(store.get_blob(run_id, sha))
    with index.open("a") as f:
        for item in new:
            f.write(json.dumps(item.to_dict()) + "\n")
    return len(new)


def load(dest: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (dest / "items.jsonl").read_text().splitlines() if line.strip()]


def blob(dest: Path, sha: str) -> bytes:
    return (dest / "blobs" / sha).read_bytes()


def uploads(dest: Path, item: dict[str, Any]) -> dict[str, bytes] | None:
    """The item's uploads as the session got them, or None when one was too large to record (it cannot be replayed)."""
    out = {}
    for name, u in item["inputs"].get("uploads", {}).items():
        if not u.get("blob"):
            return None
        out[name] = blob(dest, u["blob"])
    return out


# ---------------------------------------------------------------------- grading

def grade(agent_key: str, outputs: dict[str, bytes], schema_dir: Path, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """The deterministic grade of one session's outputs, as the pipeline reads them on receipt (rule `artifact-schema`)
    plus the judges' decision rules (`spec-pass`, `merge-ready`): `ok` when the main output exists, parses, validates and
    keeps its decision rule."""
    name, schema = OUTPUTS.get(agent_key, (None, None))
    if name is None:
        return {"ok": True, "graded": False}
    if name not in outputs:
        return {"ok": False, "missing": name}
    try:
        artifact = json.loads(outputs[name])
    except ValueError as exc:
        return {"ok": False, "not_json": str(exc)[:200]}
    if not isinstance(artifact, dict):
        return {"ok": False, "not_json": "not an object"}
    artifact = {k: v for k, v in artifact.items() if not k.startswith("_")}
    artifact["produced_by"] = dict(STAMP)
    if agent_key == "merge_judge":
        artifact.setdefault("round", 1)
    problems = [f"{'/'.join(map(str, e.path)) or '$'}: {e.message[:200]}" for e in schema_errors(artifact, schema_dir / schema, schema_dir)[:8]]
    out: dict[str, Any] = {"ok": not problems, "schema_errors": problems}
    if agent_key == "spec_judge" and not problems:
        out["rule_violations"] = rules.spec_pass_violations(artifact)
        out["result"] = artifact["overall"]["result"]
    elif agent_key == "merge_judge" and not problems and spec is not None:
        det = [CheckResult(d["check"], bool(d["passed"])) for d in artifact.get("deterministic_checks") or []]
        out["rule_violations"] = rules.merge_ready_violations(artifact, spec, det)
        out["result"] = artifact["result"]
    elif agent_key in ("reviewer", "security_reviewer") and not problems:
        out["findings"] = len(artifact["findings"])
        out["blocking"] = sum(f["severity"] == "blocking" for f in artifact["findings"])
    elif agent_key == "implementer" and not problems:
        out["criteria_mapped"] = len(artifact["criteria"])
    out["ok"] = out["ok"] and not out.get("rule_violations")
    return out


def spec_of(dest: Path, item: dict[str, Any]) -> dict[str, Any] | None:
    """The spec a build-stage session was given (its spec.json upload), for the decision rules that read it."""
    u = item["inputs"].get("uploads", {}).get("spec.json")
    return json.loads(blob(dest, u["blob"])) if u and u.get("blob") else None


__all__ = ["Item", "OUTPUTS", "blob", "export", "grade", "items", "load", "spec_of", "uploads"]
