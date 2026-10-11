"""Isolated Console design preview. Uses test fixtures; never starts paid sessions.
These are seeded states with no advancing controller; answers and decisions are stored locally only.

Run from the repository: python orchestrator/tests/support/preview_console.py
The printed temporary folder contains the fixture IDs and an offline switch.
"""
import sys, json, tempfile, argparse
from pathlib import Path
root=Path(__file__).resolve().parents[3]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=8766)
args=parser.parse_args()
sys.path[:0]=[str(root/'orchestrator'),str(root/'orchestrator/tests')]
from conftest import make_config
from support.scripted_run import ScriptedRun
from factory.budget import BudgetPaused, reserve_budget
from factory.gates import LogNotifier
from factory.stages import RunContext
from factory.state import Policy
from factory.web.app import create_app
from fastapi.responses import JSONResponse
import uvicorn
runs=Path(tempfile.mkdtemp(prefix='factory-ui-audit-'))
s=ScriptedRun(runs,root/'schemas',root/'examples/ledgerline')
ids={}
for phase in ['question','plan','contracts','complete','active','failed','stopped']:
 o=s.example_outcome();o['title']='[UI fixture] '+{'question':'Clarify the retry schedule','plan':'Review the recovery plan','contracts':'Approve the contracts','complete':'Recovery changes ready for review','active':'Build the retry scheduler','failed':'Recover from a build failure','stopped':'Controller needs restarting'}[phase]
 rid=s.seed(o);ids[phase]=rid
 if phase=='question':continue
 m=s._load(rid);s.queue.record_answer(m['question_id'], 'Stripe Smart Retries', 'preview')
 s.tick(rid)
 if phase=='plan':continue
 m=s._load(rid);g=s.queue.gate(m['gate_id']);s.queue.record_decision(g.gate_id, 'approve', 'preview')
 s.tick(rid)
 if phase=='contracts':continue
 m=s._load(rid);g=s.queue.gate(m['gate_id']);s.queue.record_decision(g.gate_id, 'approve', 'preview')
 s.tick(rid)
 if phase=='complete':continue
 # Keep the real scripted artifacts but stop the event log at implementation.
 p=runs/rid/'events.jsonl';events=[json.loads(x) for x in p.read_text().splitlines()];cut=next(i for i,e in enumerate(events) if e['kind']=='stage.entered' and e['payload'].get('stage')=='implementing')
 p.write_text(''.join(json.dumps(e)+'\n' for e in events[:cut+1]))
 s._event(rid,'session.started',{'session_id':'sess_preview_implementer','agent_key':'implementer','title':'Implement the retry scheduler'},stage='implement',spec_id='spec_smart-retries',work_item_id='wi-smart-retries')
 if phase=='active':
  s._event(rid,'session.started',{'session_id':'sess_preview_researcher','agent_key':'codebase_researcher','title':'Research the existing retry behavior'},stage='implement',spec_id='spec_smart-retries',work_item_id='wi-smart-retries')
 if phase=='failed':s._event(rid,'run.failed',{'reason':'Verification could not start: the preview server exited before it became ready.','interrupted_sessions':['sess_preview_implementer']})
 if phase=='stopped':(runs/rid/'controller.lock').touch()
cfg=make_config(root,runs,gate_surface='web')
app=create_app(cfg)
# Real budget gate behavior in the fixture store.
rid='run_previewbudget';ids['budget']=rid
ctx=RunContext(cfg=cfg,store=s.store,driver=None,queue=s.queue,notifier=LogNotifier(),forge=None,
               policy=Policy(run_budget_usd=60.0,stage_budget_usd={'review':15.0}),run_id=rid)
ctx.event('run.created',{'title':'[UI fixture] Review the remaining budget','outcome_id':'preview','budget_usd':60.0,'policy':{'preview_mode':'local'}})
s.store.put_artifact(rid,'intake','outcome.json',json.dumps({'title':'[UI fixture] Review the remaining budget','statement':'Finish the review safely.','owner':{'name':'Preview','handle':'preview'}}).encode())
for stage in ('decomposing','gate_1_decomposition','specifying','gate_2_spec_readiness','implementing'):ctx.event('stage.entered',{'stage':stage})
ctx.event('session.finished',{'session_id':'budget-session','list_cost_usd':36.2})
try:reserve_budget(ctx,'review',2)
except BudgetPaused:pass
onboarding=app.state.onboarding
sample=onboarding.create(kind='existing',name='[UI fixture] Sample app',repository_url='https://github.com/example/app',description='A sample application for checking setup recovery.',budget_usd=10)
sample.update(status='failed',phase='Discovery needs attention',spent_usd=1.18,error='The onboarding agent did not return a complete setup plan within its session limit.')
sample['sessions']=[{'stage':'onboarding-discovery','session_id':'sess_preview_app','cost_usd':1.18,'accounted':True}]
onboarding.write(sample)
ids['app']=sample['app_id']

@app.middleware('http')
async def isolated(request,call_next):
 if (runs/'offline').exists() and request.url.path.startswith('/api/'):
  return JSONResponse({'detail':'Preview offline scenario'},status_code=503)
 if request.url.path=='/api/apps/environments':
  return JSONResponse({'configured':True,'apps':[{'name':'[UI fixture] Shared sample','repository_url':'https://github.com/example/shared','environment':{'id':'env_preview','name':'Shared sample sandbox','console_url':'','created_at':'2026-10-11T05:00:00Z','verified_at':'','current':False,'in_use':False}}], 'retire':[]})
 if request.url.path in ['/api/platform','/api/repositories']:
  return JSONResponse({'ok':False,'configured':False,'error':'Isolated UI preview: provider connections are disabled.','sessions':[],'agents':[],'repositories':[]})
 if request.method!='GET' and not request.url.path.endswith(('/answer','/decision')):
  return JSONResponse({'detail':'Isolated UI preview: paid work and provider changes are disabled.'},status_code=403)
 return await call_next(request)
(runs/'manifest.json').write_text(json.dumps({'runs_dir':str(runs),'ids':ids},indent=2))
print(f'Fixture manifest: {runs}/manifest.json',flush=True)
print(json.dumps(ids),flush=True)
uvicorn.run(app,host='127.0.0.1',port=args.port)
