'use strict';

const assert = require('node:assert/strict');
const {test} = require('node:test');
const {turnStream} = require('../../src/formal_geyser/coordination/dashboard.js');

const at = second => '2026-10-11T10:00:' + String(second).padStart(2, '0') + 'Z';
const command = (id, second) => ({id, tool: 'commandExecution', status: 'completed', started_at: at(second)});
const call = (operation, args, second, result = null) => ({id: operation + second, tool: 'taskhub_call', operation,
  arguments: {operation, arguments: args}, result, started_at: at(second)});

test('a turn becomes one chronological stream with the worker on the left and its inputs on the right', () => {
  const turn = {
    inputs: [
      {input_id: 'start', kind: 'initial', at: at(0), text: JSON.stringify({operation: 'start_task', task: {id: 't1'}})},
      {input_id: 'steer', kind: 'steer', source: 'directed_messages', at: at(20), text: '{}', messages: [
        {sender: 'user', sender_role: 'user', path: 'correspondence/a.md', text: 'Check duality first.'},
        {sender: 'Worker 1', sender_role: 'worker', path: '.task-based/private/peer-talk/b.json', text: 'I take smoothness.'}]},
      {input_id: 'refresh', kind: 'steer', source: 'context_refresh', at: at(40), text: '{}'},
    ],
    replies: [
      {key: 'progress', phase: 'commentary', text: 'Reading the guides.', started_at: at(5)},
      {key: 'answer', phase: 'final', text: 'Done for now.', started_at: at(50)},
    ],
    actions: [
      command('c1', 10), command('c2', 11),
      call('talk_send', {recipient_id: 'Worker 1', text: 'Yes please.'}, 25),
      call('request', {kind: 'literature', title: 'Henniart 2000', text: 'Section 3 is missing.'}, 30, {id: 'request-1'}),
      command('c3', 45),
    ],
  };
  const stream = turnStream(turn);
  assert.deepEqual(stream.map(item => [item.kind, item.side]), [
    ['prompt', 'right'], ['reply', 'left'], ['tools', 'left'], ['message', 'right'], ['message', 'right'],
    ['outgoing', 'left'], ['request', 'left'], ['context', 'right'], ['tools', 'left'], ['reply', 'left']]);
  assert.deepEqual(stream[2].actions.map(action => action.id), ['c1', 'c2']);  // Adjacent calls fold together.
  assert.deepEqual(stream.slice(3, 5).map(item => [item.sender, item.senderRole, item.text]), [
    ['user', 'user', 'Check duality first.'], ['Worker 1', 'worker', 'I take smoothness.']]);
  assert.deepEqual([stream[5].recipient, stream[5].text], ['Worker 1', 'Yes please.']);
  assert.deepEqual([stream[6].requestKind, stream[6].title, stream[6].requestId], ['literature', 'Henniart 2000', 'request-1']);
  assert.equal(stream[7].title, 'Context refreshed after compaction');
  assert.equal(stream[1].reply.text, 'Reading the guides.');
  assert.equal(stream[9].reply.phase, 'final');
});

test('message references without inline bodies use the provided reader, and equal times keep their source order', () => {
  const packet = {human_messages: {messages: [{sender: 'user', sender_role: 'user', file: {path: 'correspondence/m.md'}}]},
    private_talk: {messages: [{sender: 'Worker 2', sender_role: 'worker', file: {path: 'peer-talk/x.json'}}]}};
  const turn = {inputs: [{input_id: 'steer', kind: 'steer', at: at(1), text: JSON.stringify(packet)}],
    replies: [{key: 'r', phase: 'commentary', text: 'Noted.', started_at: at(1)}], actions: [command('c', 1)]};
  const read = [];
  const stream = turnStream(turn, {messageText: path => { read.push(path); return path.endsWith('.md') ? 'Body' : undefined; }});
  assert.deepEqual(read, ['correspondence/m.md', 'peer-talk/x.json']);
  assert.deepEqual(stream.map(item => item.kind), ['message', 'message', 'reply', 'tools']);
  assert.deepEqual(stream.slice(0, 2).map(item => [item.sender, item.path, item.text]), [
    ['user', 'correspondence/m.md', 'Body'], ['Worker 2', 'peer-talk/x.json', null]]);
  assert.deepEqual(turnStream({inputs: [{kind: 'steer', at: at(0), text: 'not json'}]}).map(item => item.title), ['Additional input']);
  assert.deepEqual(turnStream({}), []);
});


test('message bubbles preserve delivery evidence and one exact steer input per delivery', () => {
  for (const [status, expected] of [
    ['delivered', 'Delivered to model'], ['prepared', 'Prepared · not yet delivered'],
    ['not_delivered', 'Not delivered'], ['uncertain', 'Delivery uncertain'], [undefined, 'Delivery not confirmed'],
  ]) {
    const input = {input_id: 'steer', kind: 'steer', status, at: at(1), text: JSON.stringify({messages: [
      {sender: 'operator', sender_role: 'operator', text: 'First message.'},
      {sender: 'Worker 1', sender_role: 'worker', text: 'Second message.'}]})};
    const stream = turnStream({inputs: [input]});
    assert.deepEqual(stream.map(item => item.delivery.text), [expected, expected]);
    assert.deepEqual(stream.map(item => item.showInput), [true, false]);
    assert.equal(stream[0].input, input);
    assert.equal(stream[1].input.text, input.text);
    assert.equal(stream[0].delivery.tone === '', status === 'delivered');
  }
  const initial = turnStream({inputs: [{kind: 'initial', status: 'delivered', text: '{}',
    messages: [{text: 'Initial message.'}]}]});
  assert.equal(initial[0].kind, 'prompt');
  assert.equal(initial[1].showInput, false);  // Initial input already has its prompt disclosure.
});
