const {test} = require('node:test');
const assert = require('node:assert/strict');
const {activeSessions, narrative, lastPhase} = require('../factory/web/static/console-model.js');
const ev = (kind,payload={},extra={}) => ({kind,payload,at:'2026-10-11T12:00:00Z',...extra});
const base = (overrides={}) => ({run_id:'run_ui',stage:'implementing',summary:{status:'running'},events:[],gates:[],questions:[],...overrides});
const start = (id,role='implementer') => ev('session.started',{session_id:id,agent_key:role,title:`Task for ${id}`});
test('every parallel session remains visible, including researcher',()=>{
 const r=base({events:[start('a'),start('b'),start('r','codebase_researcher'),ev('session.finished',{session_id:'b'})]});
 assert.deepEqual(activeSessions(r).map(x=>x.id),['a','r']);
 assert.equal(activeSessions(r)[1].name,'Researcher');
});
test('stopped and terminal runs never advertise open activity',()=>{
 for(const status of ['stopped','failed','closed','completed','escalated']){
  const r=base({stage:status==='stopped'?'implementing':status,summary:{status},events:[start('a')]});
  assert.deepEqual(activeSessions(r),[]);
 }
});
test('resume clears orphaned sessions and respects later starts',()=>{
 assert.deepEqual(activeSessions(base({events:[start('old'),ev('run.resumed'),start('new')]})).map(x=>x.id),['new']);
});
test('a recorded decision is distinct from one the controller applied',()=>{
 const r=base({stage:'gate_1_decomposition',gates:[{recorded:{decision:'approve'}}]});
 assert.equal(narrative(r).title,'Decision recorded');
 assert.match(narrative(r).next,/Waiting for the controller/);
});
test('blocking and nonblocking questions explain different consequences',()=>{
 assert.equal(narrative(base({questions:[{blocking:true}]})).title,'Waiting for your answer');
 assert.match(narrative(base({questions:[{blocking:false}]})).next,/default/);
});
test('an adjacent matching approval explains the next stage',()=>{
 const r=base({events:[ev('gate.decided',{kind:'gate_2_spec_readiness',decision:'approve'}),ev('stage.entered',{stage:'implementing'})]});
 assert.match(narrative(r).reason,/owner approved the contracts/);
});
test('an unrelated approval cannot become a fabricated cause',()=>{
 const r=base({events:[ev('gate.decided',{kind:'budget',decision:'approve'}),ev('stage.entered',{stage:'implementing'})]});
 assert.match(narrative(r).reason,/No further reason was recorded/);
});
test('role changes are visible even when the pipeline stage stays the same',()=>{
 const r=base({events:[start('a'),ev('session.finished',{session_id:'a'}),start('r','reviewer')]});
 assert.equal(narrative(r).title,'Reviewing the changes');
 assert.match(narrative(r).reason,/reviewer session/);
});
test('last phase remains visible after failure',()=>{
 assert.equal(lastPhase(base({stage:'failed',events:[ev('stage.entered',{stage:'implementing'}),ev('run.failed',{reason:'Verification failed'})]})),'implementing');
});
test('completed run with open PRs is ready for review, not delivered',()=>{
 assert.equal(narrative(base({stage:'completed',summary:{pull_requests:[{stage:'cr_open'}]}})).title,'Ready for code review');
 assert.equal(narrative(base({stage:'completed',design_only:true})).title,'Design approved');
});
test('stopped controller overrides an old question and does not invent a stop time',()=>{
 const n=narrative(base({summary:{status:'stopped'},questions:[{blocking:true}],events:[start('a')]}));
 assert.equal(n.title,'The controller has stopped'); assert.equal(n.at,null); assert.match(n.reason,/did not record why/);
});
test('unknown roles and missing records are readable without errors',()=>{
 assert.equal(narrative(base()).reason,'No transition reason has been recorded yet.');
 assert.equal(activeSessions(base({events:[start('a','future_role')]}))[0].name,'future role');
});
test('a parallel start cannot obscure the reason for an unanswered question',()=>{
 const r=base({questions:[{question_id:'q1',blocking:true}],events:[ev('question.asked',{question_id:'q1',question:'Choose the schedule'}),start('r','codebase_researcher')]});
 assert.equal(narrative(r).reason,'Choose the schedule');
});
test('old logs without a controller or open session cannot imply current work',()=>{
 const r=base({summary:{status:'running',controller_alive:null,live_sessions:0,updated_at:'2026-10-11T10:00:00Z'}});
 assert.equal(narrative(r,Date.parse('2026-10-11T12:00:00Z')).title,'No recent activity confirmed');
});
