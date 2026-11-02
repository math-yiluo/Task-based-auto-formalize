'use strict';

const assert = require('node:assert/strict');
const {test} = require('node:test');
const {projectStatus, personControl, workerActivity, actionSummary, elapsedLabel, taskOutlineState, outlineProgress} = require('../../src/formal_geyser/coordination/dashboard.js');

const run = (extra = {}) => ({id: 'run-1', state: 'running', dispatch_hold: null, ...extra});
const working = {id: 'w1', active_attempt: 'a1', state: 'running', work_intention: 'working'};

test('the status names what the project is doing and the one action it needs', () => {
  assert.deepEqual(projectStatus({runs: []}).actions, ['setup']);
  const armed = {service: {armed_runs: ['run-1']}};
  assert.match(projectStatus({...armed, runs: [run()], workers: [working]}).text, /^Running · 1 turn in progress \(Codex 1\)$/);
  assert.equal(projectStatus({...armed, runs: [run()], workers: [working]}).tone, 'active');
  // Codex and Claude are separate accounts: the count says how many turns each runs.
  const claudeTurn = {...working, id: 'w2', backend: 'claude'};
  assert.match(projectStatus({...armed, runs: [run()], workers: [working, claudeTurn, {...claudeTurn, id: 'w3'}]}).text,
    /^Running · 3 turns in progress \(Codex 1, Claude 2\)$/);
  assert.match(projectStatus({...armed, runs: [run()], workers: [claudeTurn]}).text, /^Running · 1 turn in progress \(Claude 1\)$/);
  assert.match(projectStatus({...armed, runs: [run()], workers: []}).text, /idle, waiting for work/);
  // A service that is not driving the run must not look like a live run.
  const unarmed = projectStatus({service: {armed_runs: []}, runs: [run()], workers: [working]});
  assert.deepEqual([unarmed.tone, unarmed.actions], ['attention', ['start']]);
  assert.deepEqual(projectStatus({...armed, runs: [run({state: 'paused'})]}).actions, ['resume']);
  // A hold on the machine is the same for both backends: said once, with no Resume to offer.
  const machine = {code: 'sandboxUnavailable', message: 'WSL interop is on. New sessions wait until it is fixed and then start without Resume.'};
  const sandbox = projectStatus({...armed, runs: [run({dispatch_holds: {codex: {...machine, backend: 'codex'}, claude: {...machine, backend: 'claude'}}})]});
  assert.equal(sandbox.text, machine.message + ' Turns already running finish on their own.');
  assert.deepEqual(sandbox.actions, ['pause_after_turn', 'pause']);
  const held = projectStatus({...armed, runs: [run({dispatch_hold: {message: 'Usage limit reached: switch the Codex account, then Resume.'}})]});
  // Resume releases dispatch; both pauses stay available for turns that are still running.
  assert.deepEqual(held.actions, ['resume', 'pause_after_turn', 'pause']);
  const heldAndPaused = projectStatus({...armed, runs: [run({state: 'paused', dispatch_hold: {message: 'Usage limit reached.'}})]});
  assert.deepEqual(heldAndPaused.actions, ['resume']);
  assert.match(held.text, /^Usage limit reached.*Turns already running finish on their own\.$/);
  // Each backend bills its own account; a Claude hold is named so Codex work is not mistaken as stopped.
  const claudeHeld = projectStatus({...armed, runs: [run({dispatch_holds: {claude: {backend: 'claude', message: 'Claude limit.'}}})]});
  assert.match(claudeHeld.text, /^Claude: Claude limit\. Turns already running/);
  const bothHeld = projectStatus({...armed, runs: [run({dispatch_holds: {
    codex: {backend: 'codex', message: 'Codex limit.'}, claude: {backend: 'claude', message: 'Claude limit.'}}})]});
  assert.match(bothHeld.text, /^Codex: Codex limit\. Claude: Claude limit\./);
  const retrying = projectStatus({service: {armed_runs: [], startup_resume: {retrying: true, error: 'locked'}}, runs: [run()]});
  assert.match(retrying.text, /retrying automatically: locked/);
  assert.deepEqual(projectStatus({runs: [run({state: 'complete'})]}).actions, []);
});

test('pausing or resuming one person is reported and follows the run otherwise', () => {
  const armed = {service: {armed_runs: ['run-1']}};
  const resumed = {id: 'w2', state: 'idle', work_intention: 'working', run_control: 'resumed'};
  const paused = {...resumed, run_control: 'paused'};
  assert.equal(projectStatus({...armed, runs: [run({state: 'paused'})], workers: [resumed]}).text,
    'Paused · 1 person continues only their own current work.');
  assert.equal(projectStatus({...armed, runs: [run()], workers: [working, paused]}).text, 'Running · 1 turn in progress (Codex 1) · 1 person paused');
  const plain = {group: null, afterTurn: false, finishing: false};
  assert.deepEqual(personControl(run({state: 'paused'}), resumed), {individual: 'resumed', running: true, ...plain});
  assert.deepEqual(personControl(run(), paused), {individual: 'paused', running: false, ...plain});
  assert.deepEqual(personControl(run({state: 'paused'}), {id: 'w3'}), {individual: null, running: false, ...plain});
  assert.deepEqual(personControl(run(), {id: 'w3'}), {individual: null, running: true, ...plain});
  assert.equal(personControl(run({state: 'stopped'}), resumed), null);
  assert.equal(personControl(run(), {id: 'w4', work_intention: 'retired'}), null);
});

test('worker activity is specific and shows elapsed time for live turns', () => {
  const now = 10 * 60000;
  assert.equal(workerActivity({...working, active_since: now - 4 * 60000}, {now}), 'Working · 4 min');
  assert.equal(elapsedLabel(0, 125 * 60000), '2 h 5 min');
  assert.equal(elapsedLabel(0, 20000), 'under 1 min');
  const waiting = {id: 'w2', state: 'idle', work_intention: 'waiting'};
  assert.equal(workerActivity(waiting, {asking: ['question']}), 'Waiting for your answer');
  assert.equal(workerActivity(waiting, {asking: ['literature']}), 'Waiting for a source from you');
  const ready = {id: 'w3', state: 'idle', work_intention: 'working'};
  assert.equal(workerActivity(ready, {asking: ['question']}), 'Ready');
  assert.equal(workerActivity(ready, {awaitingReview: true}), 'Ready · submission in review');
  assert.equal(workerActivity({id: 'r1', kind: 'review', state: 'retired', work_intention: 'retired'}), 'Review finished');
});

test('a turn summary counts actions in plain language', () => {
  const call = (operation, args = {}) => ({tool: 'taskhub_call', operation, arguments: {operation, arguments: args}, status: 'completed'});
  assert.equal(actionSummary([]), '');
  assert.equal(actionSummary([call('message_send'), call('request', {kind: 'literature'})]),
    'Messaged you · asked you for a source');
  assert.equal(actionSummary([
    {tool: 'commandExecution', status: 'completed'}, {tool: 'commandExecution', status: 'failed'},
    call('lean_check'), call('lean_check'), call('lean_check'), call('contribute'), call('search'),
  ]), 'Ran 2 commands · ran 3 Lean checks · submitted work for review · looked up project state · 1 action failed');
  assert.equal(actionSummary([call('brand_new_operation')]), 'Used brand new operation');
  assert.equal(actionSummary([call('batch_status'), call('batch_prepare'), call('batch_publish')]),
    'Looked up project state · worked on integration');
});

test('the task overview names each state plainly and counts reviews with their task', () => {
  const member = (status, extra = {}) => ({executor: 1, membership_state: 'member', execution_status: status, ...extra});
  const work = (extra = {}) => ({state: 'ready', contract: {kind: 'work'}, collaboration: {members: [member('Ready')]}, ...extra});
  assert.equal(taskOutlineState(work({state: 'done'})).text, 'Done');
  assert.equal(taskOutlineState(work({reviews_pending: 1, running: true})).text, 'Submitted · awaiting review');
  assert.equal(taskOutlineState(work({running: true})).text, 'In progress');
  assert.equal(taskOutlineState(work({collaboration: {members: []}})).text, 'Waiting for a worker');
  assert.equal(taskOutlineState(work({collaboration: {members: [member('Recovering')]}})).text, 'Recovering');
  const review = (collaboration, extra = {}) => ({state: 'ready', contract: {kind: 'review'}, collaboration, ...extra});
  assert.equal(taskOutlineState(review({members: []})).text, 'Waiting for a reviewer');
  assert.equal(taskOutlineState(review({members: [], review: {feedback: {verdict: 'reject'}}})).text, 'Changes requested');
  assert.equal(taskOutlineState(review({members: [], review: {state: 'integrating'}})).text, 'Accepted · integrating');
  const items = [work({state: 'done'}), work({reviews_pending: 1}), work({running: true}), work(),
    review({members: []}), work({state: 'retired'})];
  assert.deepEqual(outlineProgress(items), {total: 4, done: 1, review: 1, active: 1});
});


test('Pause all remains available while only individual people are resumed, including an account hold', () => {
  const partial = run({state: 'paused', resumed_workers: ['w1']});
  for (const hold of [null, {message: 'Account unavailable.'}]) {
    assert.deepEqual(projectStatus({runs: [{...partial, dispatch_hold: hold}], workers: [{run_control: 'resumed'}]}).actions, ['resume', 'pause']);
  }
  assert.deepEqual(projectStatus({runs: [run({state: 'paused'})], workers: [{run_control: 'resumed'}]}).actions,
    ['resume', 'pause']);
  // Dashboard run records do not carry per-worker controls; the worker list does.
  assert.deepEqual(projectStatus({runs: [run({state: 'paused', resumed_workers: []})],
    workers: [{run_control: 'resumed'}]}).actions, ['resume', 'pause']);
});
