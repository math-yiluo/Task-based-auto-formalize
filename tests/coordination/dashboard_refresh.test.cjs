'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../../src/formal_geyser/coordination/dashboard.js'), 'utf8');
function extract(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first);
  return source.slice(first, last);
}

class Element {
  constructor(tag = 'div', text = '', cls = '') {
    this.tag = tag; this.textContent = text; this.className = cls;
    this.children = []; this.value = ''; this.disabled = false;
    this.classList = {toggle() {}, remove() {}}; this.dataset = {};
  }
  append(...children) {
    for (const child of children) if (child instanceof Element) child.parent = this;
    this.children.push(...children);
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  querySelectorAll(selector) {
    const matches = item => selector.startsWith('.')
      ? item.className.split(' ').includes(selector.slice(1)) : item.tag === selector;
    return this.children.flatMap(item => item instanceof Element
      ? [...(matches(item) ? [item] : []), ...item.querySelectorAll(selector)] : []);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  setAttribute() {}
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this);
  }
}

function elements() {
  const map = new Map();
  return id => {
    if (!map.has(id)) map.set(id, new Element());
    return map.get(id);
  };
}

const healthy = {name: 'Synthetic project', task_count: 42, tasks: [], workers: [], requests: [], runs: [], drivers: {}, delivery_holds: []};

test('health polling clears resolved notices while preserving unrelated interaction errors', async () => {
  const el = elements();
  const queue = [
    {...healthy, delivery_holds: [{id: 'held-message'}]}, healthy,
    {...healthy, delivery_recovery_error: 'Stored transcript unavailable'}, healthy,
    {...healthy, drivers: {run: {state: 'failed', error: {message: 'Driver needs recovery'}}}}, healthy,
    new Error('Temporary connection failure'), healthy,
  ];
  const context = vm.createContext({refreshing: false, data: null, selected: null,
    notices: {interaction: '', health: ''}, el,
    document: {body: {classList: {toggle() {}}}},
    taskStore: {rememberTasks() {}, async refresh() {}},
    api: async () => { const result = queue.shift(); if (result instanceof Error) throw result; return result; },
    renderWorkers() {}, indexWorkers() {}, loadRetired() {}, retired: {count: null}, renderStatus() {}, renderProfiles() {}, renderRequests() {}, renderQueue() {}, renderLean() {}, renderTasks() {},
    renderWorkerHead() {}, renderPinnedRequests() {}, refreshQueueHistory() {}, refreshLibraryStats() {},
    loadWorker: async () => {}});
  vm.runInContext(extract('  function notice(', '\n  function time('), context);
  vm.runInContext(extract('  async function refresh() {', "\n  el('show-setup').onclick"), context);
  context.notice('Your message was not sent.');
  for (let i = 0; i < 4; i++) {
    await context.refresh();
    assert.ok(context.notices.health);
    assert.match(el('notice').textContent, /Your message was not sent/);
    await context.refresh();
    assert.equal(context.notices.health, '');
    assert.equal(el('notice').textContent, 'Your message was not sent.');
  }
  context.notice('');
  assert.equal(el('notice').textContent, '');
  assert.equal(el('counts').textContent, '42 tasks · 0 workers · 0 reviewers');
  assert.equal(el('show-tasks').textContent, 'Tasks (42)');
});

test('request refresh updates participants in place without losing the reply or upload path', () => {
  const el = elements(), requestViews = new Map();
  const initial = {id: 'request-1', kind: 'literature', state: 'pending', title: 'Exact edition',
    text: 'Please supply the referenced source.', workers: ['Worker 0']};
  const context = vm.createContext({data: {requests: [initial]}, el, requestViews, pinnedViews: new Map(), requestDrafts: new Map(),
    node: (tag, text, cls) => new Element(tag, text, cls), workerButton: id => new Element('button', id),
    label: value => value, empty: (target, title) => target.replaceChildren(new Element('p', title)),
    disclosure: () => new Element('details')});
  vm.runInContext(extract('  function updateRequestAuthors(', '\n  async function refresh() {'), context);
  context.renderRequests();
  const card = requestViews.get(initial.id), reply = card.querySelector('textarea'), input = card.querySelector('input');
  reply.value = 'This is my unsent answer.';
  input.value = 'C:/Reference/source.pdf';
  const resolve = card.querySelectorAll('button').at(-2);
  resolve.disabled = true;
  context.data.requests = [{...initial, workers: ['Worker 0', 'Worker 1']}];
  context.renderRequests();
  assert.equal(requestViews.get(initial.id), card);
  assert.deepEqual(card.querySelector('.request-workers').querySelectorAll('button').map(item => item.textContent), ['Worker 0', 'Worker 1']);
  assert.equal(card.querySelector('textarea'), reply);
  assert.equal(reply.value, 'This is my unsent answer.');
  assert.equal(input.value, 'C:/Reference/source.pdf');
  assert.equal(resolve.disabled, true);
  context.data.requests = [{...initial, state: 'fulfilled', workers: ['Worker 0', 'Worker 1']}];
  context.renderRequests();
  assert.equal(requestViews.has(initial.id), false);
  assert.deepEqual(el('resolved-requests').querySelectorAll('button').map(item => item.textContent), ['Worker 0', 'Worker 1']);
});

test('a pinned request and its dialog card share one unsent draft', () => {
  const el = elements(), requestViews = new Map(), pinnedViews = new Map(), requestDrafts = new Map();
  const request = {id: 'request-1', kind: 'question', state: 'pending', title: 'Which edition?', text: 'Please say.', workers: ['Worker 0']};
  const other = {id: 'request-2', kind: 'question', state: 'pending', title: 'Another', text: 'For Worker 1.', workers: ['Worker 1']};
  const context = vm.createContext({data: {requests: [request, other]}, el, requestViews, pinnedViews, requestDrafts, selected: 'Worker 0',
    node: (tag, text, cls) => new Element(tag, text, cls), workerButton: id => new Element('button', id),
    label: value => value, empty: (target, title) => target.replaceChildren(new Element('p', title)),
    disclosure: () => new Element('details')});
  vm.runInContext(extract('  function updateRequestAuthors(', '\n  async function refresh() {'), context);
  context.renderRequests(); context.renderPinnedRequests();
  assert.deepEqual([...pinnedViews.keys()], ['request-1']);  // Only the selected person's requests.
  assert.equal(el('worker-request-list').children.length, 1);
  const pinned = pinnedViews.get('request-1').querySelector('textarea');
  pinned.value = 'The 2000 edition.'; pinned.oninput();
  assert.equal(requestViews.get('request-1').querySelector('textarea').value, 'The 2000 edition.');
  context.selected = 'Worker 1'; context.renderPinnedRequests();
  assert.deepEqual([...pinnedViews.keys()], ['request-2']);
  context.selected = 'Worker 0'; context.renderPinnedRequests();
  assert.equal(pinnedViews.get('request-1').querySelector('textarea').value, 'The 2000 edition.');  // The draft survives.
  context.data.requests = [{...request, state: 'fulfilled'}, other];
  context.renderRequests(); context.renderPinnedRequests();
  assert.equal(pinnedViews.size, 0);
  assert.equal(requestDrafts.has('request-1'), false);
});

test('integration queue groups a later holder before waiting positions and real publications', () => {
  const el = elements(), toggles = [];
  el('queue-group').classList = {toggle: (name, on) => toggles.push([name, on])};
  const queue = {holder: {review_task_id: 'r3', worker_id: 'Reviewer 3', granted_at: 0}, reviews: [
    {review_task_id: 'r2', reviewer_id: 'Reviewer 2', position: 2, accepted_at: 2, can_run: true},
    {review_task_id: 'r1', reviewer_id: 'Reviewer 1', position: 1, accepted_at: 1, can_run: false},
    {review_task_id: 'r3', reviewer_id: 'Reviewer 3', holding: true, position: null, accepted_at: 3}]};
  const queueHistory = {loaded: true, publications: [
    {reviewer_id: 'Reviewer 4', created_at: 10}, {reviewer_id: 'Reviewer 5', created_at: 20},
    {reviewer_id: 'Reviewer 3', created_at: 30}]}; // A newer history read can precede the queue poll.
  const context = vm.createContext({data: {integration_queue: queue}, el, queueHistory,
    node: (tag, text, cls) => new Element(tag, text, cls), workerButton: id => new Element('button', id),
    taskTitle: id => 'Review ' + id, elapsedLabel: () => '5 min'});
  vm.runInContext(extract('  function renderQueue() {', '\n  function updateRequestCard('), context);
  context.renderQueue();
  assert.deepEqual(toggles.at(-1), ['hidden', false]);
  assert.equal(el('queue-count').textContent, '(3 unpublished)');
  assert.deepEqual(el('queue-list').children.filter(row => row.tag === 'h3').map(row => row.textContent),
    ['In progress (1)', 'Waiting (2)', 'Completed (2)']);
  assert.deepEqual(el('queue-list').children.filter(row => row.className === 'worker').map(row =>
    row.children.map(item => item.textContent)), [
    ['Reviewer 3', 'Integrating · 5 min'], ['Reviewer 1', '#1 · waiting · accepted 5 min ago'],
    ['Reviewer 2', '#2 · ready · accepted 5 min ago'],
    ['Reviewer 5', 'Published · 5 min ago'], ['Reviewer 4', 'Published · 5 min ago']]);
  assert.deepEqual(queue.reviews.map(row => row.review_task_id), ['r2', 'r1', 'r3']);
  context.data.integration_queue = {holder: null, reviews: []};
  context.renderQueue();
  assert.deepEqual(el('queue-list').children.filter(row => row.tag === 'h3').map(row => row.textContent),
    ['In progress (0)', 'Waiting (0)', 'Completed (3)']);
  context.data = {};
  context.renderQueue();
  assert.deepEqual(toggles.at(-1), ['hidden', true]);
});

test('publication history refreshes after queue membership changes and preserves evidence on failure', async () => {
  const queueHistory = {publications: [], loaded: false, key: null, inflight: null, error: ''};
  let calls = 0, fail = false;
  const context = vm.createContext({data: {integration_queue: {reviews: [{review_task_id: 'r1'}]}},
    queueHistory, renderQueue() {}, api: async endpoint => {
      assert.equal(endpoint, '/v1/repository'); calls += 1;
      if (fail) throw Error('Disconnected');
      return {publications: [{reviewer_id: 'Reviewer 0', created_at: 10}]};
    }});
  vm.runInContext(extract('  async function refreshQueueHistory() {', '\n  function updateRequestCard('), context);
  await context.refreshQueueHistory(); await context.refreshQueueHistory();
  assert.equal(calls, 1);
  context.data.integration_queue.reviews = [];
  fail = true; await context.refreshQueueHistory();
  assert.equal(calls, 2); assert.match(queueHistory.error, /retrying/);
  assert.equal(queueHistory.publications[0].reviewer_id, 'Reviewer 0');
  fail = false; await context.refreshQueueHistory();
  assert.equal(calls, 3); assert.equal(queueHistory.error, '');
});

function libraryContext(request) {
  const el = elements();
  let now = 1000;
  class Clock extends Date { static now() { return now; } }
  const context = vm.createContext({el, Date: Clock,
    libraryStats: {attemptedAt: null, inflight: null, value: null, error: ''},
    api: request});
  vm.runInContext(extract('  async function refreshLibraryStats() {', '\n  function renderStatus() {'), context);
  return {context, el, advance: amount => { now += amount; }};
}

test('Library counts refresh every minute and retain stale evidence on errors', async () => {
  let calls = 0;
  const {context, el, advance} = libraryContext(async endpoint => {
    assert.equal(endpoint, '/v1/library/stats'); calls += 1;
    if (calls === 2) throw Error('Temporarily unavailable');
    return {lean_lines: calls === 1 ? 1654 : 1700, lean_files: 44};
  });
  await context.refreshLibraryStats();
  assert.match(el('library-stats').textContent, /1,654 Lean lines/);
  advance(59999); await context.refreshLibraryStats(); assert.equal(calls, 1);
  advance(1); await context.refreshLibraryStats(); assert.equal(calls, 2);
  assert.match(el('library-stats').textContent, /1,654 Lean lines · stale/);
  assert.match(el('library-stats').title, /Refresh failed/);
  advance(60000); await context.refreshLibraryStats();
  assert.match(el('library-stats').textContent, /1,700 Lean lines$/);
});

test('Library refresh never overlaps', async () => {
  let calls = 0, finish;
  const {context, el, advance} = libraryContext(() => {
    calls += 1;
    return new Promise(resolve => { finish = resolve; });
  });
  const first = context.refreshLibraryStats();
  advance(60000); await context.refreshLibraryStats(); assert.equal(calls, 1);
  finish({lean_lines: 999, lean_files: 1}); await first;
  assert.match(el('library-stats').textContent, /999 Lean lines$/);
});

test('Library counts show when the service counted, and a failed hourly count as stale', async () => {
  let calls = 0;
  const counted = new Date(2026, 9, 30, 10, 0, 0).getTime();
  const {context, el, advance} = libraryContext(async () => {
    calls += 1;
    return {lean_lines: 667342, lean_files: 5017, counted_at: counted,
      ...(calls === 2 ? {error: 'Invalid Library navigation'} : {})};
  });
  await context.refreshLibraryStats();
  assert.match(el('library-stats').textContent, /667,342 Lean lines$/);
  assert.match(el('library-stats').title, /Counted .*recounted every hour on the hour/);
  advance(60000); await context.refreshLibraryStats();
  assert.match(el('library-stats').textContent, /667,342 Lean lines · stale/);
  assert.match(el('library-stats').title, /The latest hourly count failed: Invalid Library navigation/);
});


test('a fixed batch displays all selected contributions before later arrivals', () => {
  const el = elements();
  const queue = {mode: 'batch', holder: {worker_id: 'Integrator', granted_at: 0},
    reviews: [
      {review_task_id: 'r3', reviewer_id: 'Reviewer 3', position: 1, holding: false, can_run: true},
      {review_task_id: 'r1', reviewer_id: 'Reviewer 1', integrator_id: 'Integrator', holding: true},
      {review_task_id: 'r2', reviewer_id: 'Reviewer 2', integrator_id: 'Integrator', holding: true}
    ]};
  const context = vm.createContext({data: {integration_queue: queue}, el,
    queueHistory: {loaded: true, publications: []},
    node: (tag, text, cls) => new Element(tag, text, cls),
    workerButton: id => new Element('button', id), name: id => id,
    taskTitle: id => id, elapsedLabel: () => '1 min'});
  vm.runInContext(extract('  function renderQueue() {', '\n  function updateRequestCard('), context);
  context.renderQueue();
  assert.deepEqual(el('queue-list').children.filter(row => row.tag === 'h3').map(row => row.textContent),
    ['In progress (2)', 'Waiting (1)', 'Completed (0)']);
  const rows = el('queue-list').children.filter(row => row.className === 'worker');
  assert.equal(rows[0].children[0].textContent, 'Integrator');
  assert.equal(rows[0].children[2].textContent, 'Reviewed by Reviewer 1');
  assert.equal(rows[1].children[2].textContent, 'Reviewed by Reviewer 2');
  assert.equal(rows[2].children[0].textContent, 'Reviewer 3');
});


test('deferred contributions expose age and an explicit reasoned retry', async () => {
  const el = elements(), calls = [];
  const queue = {mode: 'batch', threshold: 8, delay_minutes: 21, holder: null,
    reviews: [{review_task_id: 'r1', reviewer_id: 'Reviewer 1', candidate_id: 'c1',
      accepted_at: 1, deferred_at: 2, position: 1, deferred_reason: 'Missing source.'}]};
  const context = vm.createContext({data: {integration_queue: queue}, el,
    queueHistory: {loaded: true, publications: []},
    node: (tag, text, cls) => new Element(tag, text, cls),
    workerButton: id => new Element('button', id), name: id => id,
    taskTitle: id => id, elapsedLabel: () => '2 min',
    window: {prompt: () => 'The missing source is available.'},
    command: async (name, payload) => { calls.push([name, payload]); }});
  vm.runInContext(extract('  function renderQueue() {', '\n  function updateRequestCard('), context);
  context.renderQueue();
  const list = el('queue-list');
  const row = list.children.find(element => element.className === 'worker');
  assert.ok(row.children.some(element => element.textContent === 'Deferred 2 min ago'));
  const button = row.children.find(element => element.textContent === 'Retry integration');
  await button.onclick();
  assert.equal(calls[0][0], 'integrator.retry');
  assert.equal(calls[0][1].candidate_id, 'c1');
  assert.equal(calls[0][1].reason, 'The missing source is available.');
  assert.ok(list.children.some(element => element.textContent?.includes('8 ready contributions, or 21 minutes')));
  assert.ok(list.children.some(element => element.textContent?.includes('explicit retry')));
});

test('an unchanged overview keeps the page while elapsed labels still advance', async () => {
  const el = elements();
  let clock = 1000000, renders = 0;
  class FakeDate extends Date { static now() { return clock; } }
  const options = [], responses = [healthy, null, null, {...healthy, task_count: 43}];
  const context = vm.createContext({refreshing: false, data: null, selected: null, Date: FakeDate,
    notices: {interaction: '', health: ''}, el,
    document: {body: {classList: {toggle() {}}}},
    taskStore: {rememberTasks() {}, async refresh() {}},
    api: async (route, body, option) => { options.push(option); return responses.shift(); },
    renderWorkers() { renders++; }, indexWorkers() {}, loadRetired() {}, retired: {count: null}, renderStatus() {}, renderRequests() {}, renderQueue() {}, renderLean() {},
    renderTasks() {}, renderWorkerHead() {}, renderPinnedRequests() {}, refreshQueueHistory() {}, refreshLibraryStats() {},
    loadWorker: async () => {}});
  vm.runInContext(extract('  function notice(', '\n  function time('), context);
  vm.runInContext(extract('  async function refresh() {', "\n  el('show-setup').onclick"), context);
  await context.refresh();
  assert.equal(renders, 1);
  assert.equal(options[0].conditional, false);  // Nothing to compare against yet.
  clock += 2000;
  await context.refresh();
  assert.equal(renders, 1);  // 304: the page keeps what it has.
  assert.equal(options[1].conditional, true);
  assert.match(el('connection').textContent, /^Connected · /);
  clock += 31000;
  await context.refresh();
  assert.equal(renders, 2);  // Unchanged data, but elapsed labels are refreshed.
  await context.refresh();
  assert.equal(renders, 3);
  assert.equal(el('counts').textContent, '43 tasks · 0 workers · 0 reviewers');
});
