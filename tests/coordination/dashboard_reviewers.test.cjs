'use strict';

const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');
const {principalName, principalNumber} = require('../../src/formal_geyser/coordination/dashboard.js');

const directory = path.resolve(__dirname, '../../src/formal_geyser/coordination');
const source = fs.readFileSync(path.join(directory, 'dashboard.js'), 'utf8');
const markup = fs.readFileSync(path.join(directory, 'dashboard.html'), 'utf8');

// A small DOM runs the complete dashboard script and its actual event handlers.
// All requests are synthetic; unknown endpoints and execution commands fail.
class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.parent = null; this.dataset = {}; this.style = {};
    this.attributes = {}; this.events = {}; this.className = ''; this.value = '';
    this.open = false; this.disabled = false; this.scrollTop = 0; this.scrollLeft = 0;
    this.scrollHeight = 500; this.clientHeight = 500;
    this.classList = {
      contains: name => this.className.split(' ').includes(name),
      toggle: (name, force) => {
        const enabled = force ?? !this.classList.contains(name);
        const classes = new Set(this.className.split(' ').filter(Boolean));
        if (enabled) classes.add(name); else classes.delete(name);
        this.className = [...classes].join(' '); return enabled;
      },
      add: name => this.classList.toggle(name, true),
      remove: name => this.classList.toggle(name, false),
    };
  }
  get textContent() { return this.children.map(child => child instanceof Element ? child.textContent : String(child)).join(''); }
  set textContent(value) { this.replaceChildren(String(value)); }
  get firstChild() { return this.children[0] || null; }
  append(...children) {
    for (const child of children) if (child instanceof Element) child.parent = this;
    this.children.push(...children);
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
  dispatchEvent(event) { for (const callback of this.events[event.type] || []) callback(event); }
  getBoundingClientRect() { return {top: 0}; }
  close() { this.open = false; this.dispatchEvent({type: 'close'}); }
  showModal() { this.open = true; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  matches(selector) {
    const normalized = selector.replace(/:last-child$/, '');
    const tag = normalized.match(/^[a-z]+/);
    if (tag && this.tag !== tag[0]) return false;
    for (const [, cls] of normalized.matchAll(/\.([\w-]+)/g)) if (!this.classList.contains(cls)) return false;
    for (const [, attribute] of normalized.matchAll(/\[([\w-]+)\]/g)) {
      if (attribute === 'open' ? !this.open : attribute.startsWith('data-')
        ? this.dataset[attribute.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] === undefined
        : this.attributes[attribute] === undefined) return false;
    }
    return !selector.endsWith(':last-child') || this.parent?.children.at(-1) === this;
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => child instanceof Element
      ? [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)] : []);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector); }
}

const ordinary = {id: 'ordinary-internal-7dd8dffa', kind: 'ordinary', worker_number: 0,
  display_name: 'Worker 0', state: 'active', work_intention: 'working', thread_id: 'ordinary-native'};
const direct = {id: 'direct-review-internal-8bddf003', kind: 'review', worker_number: null,
  reviewer_number: 1, display_name: 'Reviewer 1', state: 'active', work_intention: 'working'};
const second = {id: 'second-review-internal-b33b25d0', kind: 'review', worker_number: 17,
  reviewer_number: 2, display_name: 'Reviewer 2', state: 'active', work_intention: 'working'};

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const sessions = (id, extra = []) => ({sessions: [{key: 'native-main', kind: 'main', turn_count: 1,
  started_at: '2026-10-11T10:00:00Z', context_version: 1}, ...extra]});
function history(id, key = 'native-main') {
  return {session: {key, turn_count: 1, context_version: 1}, context: [], turns: [
    {key: 'same-turn-key', ordinal: 1, status: 'completed', inputs: [
      {input_id: 'same-input-key', kind: 'input', status: 'delivered', text: id + ' EXACT PROMPT\nSecond line'}],
    replies: [{key: 'same-reply-key', phase: 'final', text: id + ' EXACT RESPONSE'}], actions: []},
  ], has_more: false, next_before: null};
}

function harness(workers = [second, ordinary, direct], handler) {
  const elements = new Map();
  for (const [, tag, attributes, id] of markup.matchAll(/<([a-z]+)([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(tag);
    element.id = id; element.className = attributes.match(/class="([^"]*)"/)?.[1] || '';
    element.open = /\bopen(?:\s|$)/.test(attributes); elements.set(id, element);
  }
  const el = id => { assert.ok(elements.has(id), 'Missing markup element: ' + id); return elements.get(id); };
  // Native details nodes persist; the dashboard only replaces their child lists.
  el('workers-group').append(el('worker-count'), el('worker-list'));
  el('reviewers-group').append(el('reviewer-count'), el('reviewer-list'));
  const state = {overview: {name: 'Synthetic project', task_count: 0, tasks: [], workers,
    requests: [], runs: [], drivers: {}, delivery_holds: []}};
  const requests = []; let refresh;
  const context = vm.createContext({document: {getElementById: el,
    createElement: tag => new Element(tag), querySelectorAll: () => [], body: new Element('body'),
    addEventListener() {}},
    sessionStorage: {getItem: () => '', setItem() {}, removeItem() {}},
    crypto: {randomUUID}, TextEncoder, Event: class { constructor(type) { this.type = type; } },
    setInterval(callback) { refresh = callback; }, setTimeout, clearTimeout,
    location: {reload() {}},
    async fetch(url, options) {
      requests.push({url, ...options});
      assert.equal(options.method, 'GET', 'Browsing must never send commands or start model execution');
      let result;
      if (url === '/v1/overview?compact=true') result = state.overview;
      else {
        const match = url.match(/^\/v1\/workers\/([^/]+)\/(sessions|messages)(?:\/([^?]+))?(?:\?.*)?$/);
        assert.ok(match, 'Unexpected endpoint: ' + url);
        const id = decodeURIComponent(match[1]), key = match[3] && decodeURIComponent(match[3]);
        const type = match[2] === 'messages' ? 'messages' : key ? 'history' : 'sessions';
        result = handler && await handler({id, key, type, url});
        if (result === undefined) result = type === 'sessions' ? sessions(id) : type === 'history' ? history(id, key)
          : {messages: [], delivery_updates: [], next_after: 0, has_more: false};
      }
      return {ok: true, json: async () => result};
    },
  });
  vm.runInContext(source, context);
  return {el, state, requests, refresh: () => refresh(),
    async load() { await settle(); },
    button(id) { return [...el('worker-list').children, ...el('reviewer-list').children].find(child => child.dataset.worker === id); },
  };
}

test('reviewer names use their independent number and never use a legacy worker number or long ID', () => {
  assert.equal(principalName(ordinary), 'Worker 0');
  assert.equal(principalName({...second, display_name: undefined}), 'Reviewer 2');
  assert.equal(principalName({...direct, display_name: direct.id}), 'Reviewer 1');
  assert.equal(principalName({...second, display_name: 'Worker 17', reviewer_number: null}), 'Unnamed reviewer');
  assert.equal(principalName({kind: 'ordinary', worker_number: null}), 'Unnamed worker');
  assert.equal(principalName(undefined, 'Reviewer 12'), 'Reviewer 12');
  assert.equal(principalName(undefined, direct.id), 'Unregistered contributor');
  assert.equal(principalNumber({...second, worker_number: 900}), 2);
  assert.equal(principalNumber({kind: 'review', display_name: 'Reviewer 10'}), 10);
});

test('sidebar markup supplies independent native disclosures and an inclusive history pane', () => {
  for (const [plural, singular, title] of [['workers', 'worker', 'Workers'], ['reviewers', 'reviewer', 'Reviewers'], ['integrators', 'integrator', 'Integrator and Dispatcher']]) {
    assert.match(markup, new RegExp('<details id="' + plural + '-group"[^>]*open><summary>' + title +
      ' <span id="' + singular + '-count"[^>]*>\\(0\\)</span></summary><div id="' + singular + '-list"'));
  }
  assert.match(markup, /aria-label="Find a conversation"/);
  assert.match(markup, /aria-label="Selected person's history"/);
  assert.match(markup, /<html lang="en">/);
});

test('polling, filtering, and selection preserve independent collapse state and accurate group counts', async () => {
  const reviewer10 = {...direct, id: 'tenth-review', reviewer_number: 10, display_name: 'Reviewer 10'};
  const app = harness([reviewer10, second, ordinary, direct]); await app.load();
  const titles = id => app.el(id).querySelectorAll('strong').map(item => item.textContent);
  assert.deepEqual(titles('worker-list'), ['Worker 0']);
  assert.deepEqual(titles('reviewer-list'), ['Reviewer 1', 'Reviewer 2', 'Reviewer 10']);
  assert.equal(app.el('worker-count').textContent, '(1)');
  assert.equal(app.el('reviewer-count').textContent, '(3)');
  assert.equal(app.el('counts').textContent, '0 tasks · 1 worker · 3 reviewers');
  const workersGroup = app.el('workers-group'), reviewersGroup = app.el('reviewers-group');
  workersGroup.open = false; reviewersGroup.open = true;
  app.button(direct.id).onclick(); await settle();
  assert.equal(app.el('conversation-title').textContent, 'Reviewer 1');
  app.state.overview = {...app.state.overview, workers: [...app.state.overview.workers,
    {...ordinary, id: 'worker-three', worker_number: 3, display_name: 'Worker 3'}]};
  await app.refresh();
  assert.equal(app.el('workers-group'), workersGroup); assert.equal(workersGroup.open, false);
  assert.equal(app.el('reviewers-group'), reviewersGroup); assert.equal(reviewersGroup.open, true);
  assert.equal(app.el('worker-count').textContent, '(2)');
  app.el('worker-filter').value = 'Reviewer 2'; app.el('worker-filter').oninput();
  assert.equal(app.el('worker-count').textContent, '(0/2)');
  assert.equal(app.el('reviewer-count').textContent, '(1/3)');
  assert.deepEqual(titles('reviewer-list'), ['Reviewer 2']);
  assert.equal(app.el('conversation-title').textContent, 'Reviewer 1');
  assert.equal(workersGroup.open, false); assert.equal(reviewersGroup.open, true);
  workersGroup.open = true; reviewersGroup.open = false;
  app.el('worker-filter').value = ''; app.el('worker-filter').oninput(); await app.refresh();
  assert.equal(workersGroup.open, true); assert.equal(reviewersGroup.open, false);
  assert.equal(app.el('reviewer-count').textContent, '(3)');
  assert.ok(app.requests.every(request => request.method === 'GET'));
});

test('reviewers use the existing direct user channel, each with its own draft', async () => {
  const app = harness(); await app.load();
  app.button(direct.id).onclick(); await settle();
  assert.equal(app.el('history-heading').textContent, 'REVIEWER HISTORY');
  assert.equal(app.el('worker-kind').textContent, 'Reviewer');
  assert.match(app.el('worker-ids').textContent, /^Internal reviewer ID:/);
  // The composer sits under the conversation itself; there is no separate messages view.
  assert.equal(app.el('compose').classList.contains('hidden'), false);
  assert.equal(app.el('message-label').textContent, 'Message Reviewer 1');
  assert.equal(app.el('message').disabled, false);
  app.el('message').value = 'A direct user draft for Reviewer 1'; app.el('message').oninput();
  app.button(second.id).onclick(); await settle();
  assert.equal(app.el('message').value, '');
  app.button(ordinary.id).onclick(); await settle();
  assert.equal(app.el('conversation-title').textContent, 'Worker 0');
  assert.equal(app.el('history-heading').textContent, 'WORKER HISTORY');
  app.button(direct.id).onclick(); await settle();
  assert.equal(app.el('message').value, 'A direct user draft for Reviewer 1');
  app.state.overview = {...app.state.overview, workers: [ordinary, second, {...direct, state: 'retired', work_intention: 'retired'}]};
  await app.refresh();
  assert.equal(app.el('message').disabled, true); assert.equal(app.el('send').disabled, true);
  assert.match(app.el('message-state').textContent, /Reviewer 1 is retired/);
  assert.equal(app.el('message').value, 'A direct user draft for Reviewer 1');
  assert.ok(app.requests.every(request => request.method === 'GET'));
});

test('late worker history cannot replace a selected reviewer and identical native keys remain person-specific', async () => {
  const pending = deferred();
  const app = harness([ordinary, direct], ({id, type}) => id === ordinary.id && type === 'history' ? pending.promise : undefined);
  await app.load(); app.button(ordinary.id).onclick(); await settle();
  app.button(direct.id).onclick(); await settle();
  // The model input is collapsed until opened; opening renders this person's exact prompt.
  for (const panel of app.el('transcript').querySelectorAll('details.input-panel')) {
    panel.open = true; panel.dispatchEvent({type: 'toggle'});
  }
  assert.match(app.el('transcript').textContent, new RegExp(direct.id + ' EXACT PROMPT'));
  assert.match(app.el('transcript').textContent, new RegExp(direct.id + ' EXACT RESPONSE'));
  assert.doesNotMatch(app.el('transcript').textContent, new RegExp(ordinary.id));
  pending.resolve(history(ordinary.id)); await settle();
  assert.equal(app.el('conversation-title').textContent, 'Reviewer 1');
  assert.doesNotMatch(app.el('transcript').textContent, new RegExp(ordinary.id));
  app.button(ordinary.id).onclick(); await settle();
  assert.match(app.el('transcript').textContent, new RegExp(ordinary.id + ' EXACT RESPONSE'));
  assert.doesNotMatch(app.el('transcript').textContent, new RegExp(direct.id));
  assert.equal(app.el('session-select').children.length, 1);
  assert.ok(app.requests.some(request => request.url.startsWith('/v1/workers/' + direct.id + '/sessions/')));
});

test('empty groups remain visible and a failed reviewer history read does not expose another person history', async () => {
  const app = harness([ordinary, direct], ({id, type}) => {
    if (id === direct.id && type === 'sessions') throw Error('Synthetic reviewer history unavailable');
  });
  await app.load(); app.button(ordinary.id).onclick(); await settle();
  app.button(direct.id).onclick(); await settle();
  assert.match(app.el('transcript').textContent, /Synthetic reviewer history unavailable/);
  assert.doesNotMatch(app.el('transcript').textContent, new RegExp(ordinary.id));
  app.state.overview = {...app.state.overview, workers: []}; await app.refresh();
  assert.equal(app.el('worker-count').textContent, '(0)'); assert.equal(app.el('reviewer-count').textContent, '(0)');
  assert.equal(app.el('worker-list').textContent, 'No workers yet');
  assert.equal(app.el('reviewer-list').textContent, 'No reviewers yet');
});

test('session selection stays with each person without adding helper sessions to the sidebar', async () => {
  const app = harness([ordinary, direct], ({id, type, key}) => {
    if (type === 'sessions' && id === ordinary.id) return sessions(id, [{key: 'native-helper', kind: 'recruitment',
      turn_count: 1, started_at: '2026-10-11T11:00:00Z', context_version: 1}]);
    if (type === 'history') return history(id + ' ' + key, key);
  });
  await app.load(); app.button(ordinary.id).onclick(); await settle();
  assert.equal(app.el('session-select').children.length, 2);
  assert.match(app.el('session-select').textContent, /Collaboration decision/);
  app.el('session-select').value = 'native-helper'; app.el('session-select').onchange(); await settle();
  assert.match(app.el('transcript').textContent, /native-helper EXACT RESPONSE/);
  assert.doesNotMatch(app.el('transcript').textContent, /native-main EXACT RESPONSE/);
  app.button(direct.id).onclick(); await settle();
  assert.equal(app.el('session-select').children.length, 1);
  assert.match(app.el('transcript').textContent, new RegExp(direct.id + ' native-main EXACT RESPONSE'));
  app.button(ordinary.id).onclick(); await settle();
  assert.match(app.el('transcript').textContent, /native-helper EXACT RESPONSE/);
  assert.equal(app.el('session-select').children.find(option => option.selected).value, 'native-helper');
  assert.equal(app.el('worker-count').textContent, '(1)'); assert.equal(app.el('reviewer-count').textContent, '(1)');
  assert.equal(app.el('worker-list').querySelectorAll('button').length, 1);
  assert.equal(app.el('reviewer-list').querySelectorAll('button').length, 1);
});

test('the sidebar lists the Lean service queue without blocking normal conversation refresh', async () => {
  const app = harness(); await app.load();
  app.state.overview = {...app.state.overview, lean_resources: {
    threads: 16, threads_in_use: 6, memory_available: 12 * 2 ** 30, sessions: [{key: 'session'}],
    counts: {running: 1, queued: 2, suspended: 0},
    jobs: [{id: 'a', lane: 'official', owner: ordinary.id, label: 'Proof candidate c-1', state: 'running', threads: 6,
            blocking: true, seconds: 41.5, private_memory: 3 * 2 ** 30},
           {id: 'b', lane: 'interactive', owner: direct.id, label: 'lean_check Example', state: 'queued', threads: 0,
            blocking: false, seconds: 3},
           {id: 'c', lane: 'interactive', owner: 'operator', label: 'formalizectl check Other', state: 'queued', threads: 0,
            blocking: false, seconds: 125}]}};
  await app.refresh();
  const group = app.el('lean-group'), list = app.el('lean-list');
  assert.equal(group.classList.contains('hidden'), false);
  assert.equal(app.el('lean-count').textContent, '(3 checks)');
  assert.equal(app.el('lean-stats').textContent, 'Lean service · 1 running · 2 waiting');
  group.open = false; app.el('lean-stats').onclick();
  assert.equal(group.open, true);  // The status bar's summary opens the list.
  assert.match(list.textContent, /6 of 16 threads in use · 12\.0 GiB free · 1 open Lean session/);
  assert.deepEqual(list.children.filter(row => row.tag === 'h3').map(row => row.textContent), ['Running (1)', 'Waiting (2)']);
  assert.deepEqual(list.children.filter(row => row.className === 'worker').map(row => row.children.map(item => item.textContent)), [
    ['Worker 0', 'Proof candidate c-1', 'Validation, priority · running under 1 min', '6 threads · 3.0 GiB'],
    ['Reviewer 1', 'lean_check Example', '#1 · Dev check · waiting under 1 min'],
    ['You', 'formalizectl check Other', '#2 · Dev check · waiting 2 min']]);
  app.button(direct.id).onclick(); await settle();
  assert.equal(app.el('message').disabled, false);
  app.state.overview = {...app.state.overview, lean_resources: {threads: 16, threads_in_use: 0, sessions: [],
    counts: {running: 0, queued: 0, suspended: 0}, jobs: []}};
  await app.refresh();
  assert.equal(app.el('lean-count').textContent, '(idle)');
  assert.match(list.textContent, /No check is running\..*No check is waiting\./);
  app.state.overview = {...app.state.overview, lean_resources: {running: false}};
  await app.refresh();
  assert.equal(app.el('lean-count').textContent, '(off)');
  assert.equal(app.el('lean-stats').textContent, 'Lean service · off');
  assert.match(list.textContent, /Not running\. It starts with the next Lean check\./);
  app.state.overview = {...app.state.overview, lean_resources: null};  // Not a Lean project.
  await app.refresh();
  assert.equal(group.classList.contains('hidden'), true);
  assert.equal(app.el('lean-stats').classList.contains('hidden'), true);
  // A TaskHub older than the Lean service reports its memory admission, which is no queue.
  app.state.overview = {...app.state.overview, lean_resources: {reserved_jobs: 2, waiting: [{label: 'Proof candidate'}]}};
  await app.refresh();
  assert.equal(group.classList.contains('hidden'), true);
  assert.doesNotMatch(app.el('lean-stats').textContent + list.textContent, /undefined/);
});
