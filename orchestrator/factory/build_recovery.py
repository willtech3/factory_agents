"""A hash-bound human decision for a stopped build, including historical runs."""
from __future__ import annotations
import hashlib
import json
from .gates import ArtifactRef, Decision, Gate, GateKind
from .state import RunStage


def snapshot(store, run_id):
    events = store.events(run_id)
    from .runner import resumable_build_escalation
    if not resumable_build_escalation(store.state(run_id), events):
        return None
    event = next((e for e in reversed(events) if e.kind == 'run.escalated'), None)
    if not event or not event.spec_id:
        return None
    approvals = [e.payload for e in events if e.kind == 'artifact.produced' and e.payload.get('artifact_type') == 'gate2_approval']
    if not approvals or event.spec_id not in approvals[-1].get('approved', []):
        return None
    folder = store.root / run_id / 'artifacts' / 'merge_judge'
    paths = sorted(folder.glob(f'{event.spec_id}-*merge_readiness.json'), key=lambda p: p.stat().st_mtime_ns)
    last_resume = max((e.seq for e in events if e.kind == 'run.resumed'), default=-1)
    last_judge = max((e.seq for e in events if e.kind == 'merge.verdict' and e.spec_id == event.spec_id), default=-1)
    last_blocked = max((e.seq for e in events if e.kind == 'work_item.stage' and e.spec_id == event.spec_id
                        and e.payload.get('stage') == 'blocked'), default=-1)
    judged = last_judge > max(last_resume, last_blocked)
    verdict = store.get_json(run_id, 'merge_judge', paths[-1].name) if paths and judged else {}
    import re
    reports = [(int(m.group(1)), p) for p in (store.root / run_id / 'artifacts' / 'implement').glob(f'{event.spec_id}-r*-implementation_report.json')
               if (m := re.fullmatch(re.escape(event.spec_id) + r'-r(\d+)-implementation_report.json', p.name))]
    round_no, report_path = max(reports, default=(int(verdict.get('round', 1)), None), key=lambda pair: pair[0])
    report = store.get_json(run_id, 'implement', report_path.name) if report_path else {}
    blocked_path = store.root / run_id / 'artifacts' / 'implement' / f'{event.spec_id}-r{round_no}-blocked.md'
    artifact = f'merge_judge/{paths[-1].name}' if paths and judged else (f'implement/{blocked_path.name}' if blocked_path.is_file() else None)
    reviews = sorted((store.root / run_id / 'artifacts' / 'review').glob(f'{event.spec_id}-r*-findings.merged.json'), key=lambda p: p.stat().st_mtime_ns)
    review = store.get_json(run_id, 'review', reviews[-1].name) if reviews else None
    record = {'review_findings': review, 'event_id': event.event_id, 'spec_id': event.spec_id, 'round': max(round_no, int(verdict.get('round', 1))),
              'head_sha': verdict.get('_head_sha') or report.get('head_sha') or report.get('head_commit'), 'summary': event.payload.get('summary') or verdict.get('summary_for_human') or event.payload.get('reason'),
              'checks': verdict.get('deterministic_checks', []), 'criteria': verdict.get('criteria', []),
              'findings': verdict.get('unresolved_findings', []), 'risks': verdict.get('residual_risks', []),
              'artifact': artifact}
    record['revision'] = hashlib.sha256(json.dumps(record,sort_keys=True).encode()).hexdigest()
    return record


def open_gate(store, queue, run_id, record):
    gate_id = 'gate_build_' + record['event_id'].removeprefix('evt_') + '_' + record['revision'][:12]
    existing = next((e for e in store.events(run_id) if e.kind == 'gate.opened' and e.payload['gate_id'] == gate_id), None)
    if existing:
        return gate_id
    for old in store.events(run_id):
        if old.kind == 'gate.opened' and old.payload['gate_id'].startswith('gate_build_' + record['event_id'].removeprefix('evt_')) and old.payload['gate_id'] != gate_id:
            if old.payload['gate_id'] in store.state(run_id).open_gates:
                store.append(run_id, 'gate.superseded', {'type':'orchestrator','id':'orchestrator'}, {'gate_id':old.payload['gate_id'],'reason':'Recovery evidence refreshed; review the new snapshot'})
    name = record['event_id'] + '-' + record['revision'][:12] + '.json'
    sha = store.put_artifact(run_id, 'recovery', name, json.dumps(record,indent=2).encode())
    gate = Gate(run_id=run_id, kind=GateKind.BUILD_RECOVERY, gate_id=gate_id,
                artifacts=(ArtifactRef(name,sha),), assignees=(), summary=record['summary'],
                options=(Decision.APPROVE,Decision.REQUEST_CHANGES,Decision.REJECT))
    queue.open(gate)
    store.append(run_id,'gate.opened',{'type':'orchestrator','id':'orchestrator'},
                 {'gate_id':gate_id,'kind':gate.kind.value,'artifacts':[a.__dict__ for a in gate.artifacts],
                  'summary':gate.summary,'assignees':[]},stage='implement',spec_id=record['spec_id'])
    return gate_id


def apply_decision(store, queue, run_id):
    record = snapshot(store,run_id)
    if not record:
        return None
    gate_id = 'gate_build_' + record['event_id'].removeprefix('evt_') + '_' + record['revision'][:12]
    events = store.events(run_id)
    prior = next((e.payload for e in reversed(events) if e.kind == 'build.recovery' and e.payload.get('event_id') == record['event_id'] and e.payload.get('revision') == record['revision']),None)
    if prior:
        return prior
    opened = next((e for e in events if e.kind == 'gate.opened' and e.payload['gate_id'] == gate_id),None)
    if not opened:
        if any(g.get('kind') == 'build_recovery' for g in store.state(run_id).open_gates.values()):
            raise ValueError('Recovery evidence changed; review the latest snapshot before resuming.')
        return None  # older controller: compatibility with explicit CLI resume
    raw = store.get_artifact(run_id,'recovery',opened.payload['artifacts'][0]['name'])
    if hashlib.sha256(raw).hexdigest() not in {a['sha256'] for a in opened.payload['artifacts']}:
        raise ValueError('The recovery decision artifact no longer matches the gate.')
    frozen = json.loads(raw)
    if frozen['revision'] != record['revision']:
        raise ValueError('Build evidence changed; review the latest escalation before deciding.')
    decision = queue.decision(gate_id)
    if not decision:
        raise ValueError('Decide the build recovery gate before resuming.')
    actor={'type':'human','id':decision.decided_by}
    payload={**record,'decision':decision.decision.value,'comments':decision.comments,'gate_id':gate_id}
    store.append(run_id,'gate.decided',actor,{'gate_id':gate_id,'kind':GateKind.BUILD_RECOVERY.value,
                 'decision':decision.decision.value,'comments':decision.comments,'artifact_shas':list(decision.artifact_shas)})
    store.append(run_id,'build.recovery',actor,payload,spec_id=record['spec_id'])
    if decision.decision == Decision.REJECT:
        store.append(run_id,'stage.entered',actor,{'stage':'closed'})
    return payload
