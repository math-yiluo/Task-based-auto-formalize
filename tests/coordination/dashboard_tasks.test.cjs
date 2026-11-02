'use strict';

const assert = require('node:assert/strict');
const {test} = require('node:test');
const {TaskBrowserStore, workerStateLabel, workerTaskPrefix, preserveDisclosures, admissionLabel} =
  require('../../src/formal_geyser/coordination/dashboard.js');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function task(id = 'a', revision = 3, extras = {}) {
  return {id, revision, current_revision: revision, state: 'claimed',
    contract: {title: id + ' revision ' + revision, kind: 'work', objective: 'Objective ' + revision},
    collaboration: {members: []}, ...extras};
}
function page(items = [task()], extras = {}) {
  return {items, view: 'all', query: '', total: items.length, offset: 0, limit: 25,
    counts: {all: 60, running: 2, pool: 4, history: 20}, has_more: false, next_offset: null, ...extras};
}
function revisions(current = 3, extras = {}) {
  return {items: Array.from({length: Math.min(current, 20)}, (_, index) => ({revision: current - index,
    title: 'Revision ' + (current - index), contract_hash: 'hash-' + (current - index)})),
    total: current, offset: 0, limit: 20, current_revision: current, has_more: current > 20,
    next_offset: current > 20 ? 20 : null, ...extras};
}
function opened(request, changed) {
  const store = new TaskBrowserStore(request, changed);
  store.setOpen(true);
  return store;
}

test('task search and pagination go to the server with a bounded default page', async () => {
  const urls = [];
  const store = opened(async path => { urls.push(new URL(path, 'http://local')); return page([task('a result beyond the former first page')]); });
  await store.browse({view: 'pool', query: ' exact theorem & source '});
  assert.equal(urls[0].pathname, '/v1/task-browser');
  assert.equal(urls[0].searchParams.get('limit'), '25');
  assert.equal(urls[0].searchParams.get('offset'), '0');
  assert.equal(urls[0].searchParams.get('query'), 'exact theorem & source');
  assert.equal(urls[0].searchParams.get('view'), 'pool');
  assert.equal(store.page.items[0].id, 'a result beyond the former first page');
  assert.equal(store.counts.pool, 4);
  assert.equal(store.titles.get(store.page.items[0].id), store.page.items[0].contract.title);
  await assert.rejects(store.browse({limit: 101}), /Invalid task page/);
  await store.browse({offset: 100, limit: 100});
  assert.equal(urls[1].searchParams.get('limit'), '100');
  assert.equal(urls[1].searchParams.get('query'), 'exact theorem & source');
});

test('overlapping task polls share the same request', async () => {
  const response = deferred(); let calls = 0;
  const store = opened(() => { calls++; return response.promise; });
  const first = store.loadPage(), second = store.loadPage();
  assert.equal(calls, 1);
  response.resolve(page()); await Promise.all([first, second]);
  assert.equal(store.page.items.length, 1);
  assert.equal(store.pageLoad, null);
});

test('a late search result cannot replace the newer search or its error state', async () => {
  const first = deferred(), second = deferred(), third = deferred();
  const replies = [first, second, third];
  const store = opened(() => replies.shift().promise);
  const old = store.browse({query: 'old'});
  const current = store.browse({query: 'new'});
  second.resolve(page([task('new')], {query: 'new'})); await current;
  first.resolve(page([task('old')], {query: 'old'})); await old;
  assert.equal(store.query, 'new');
  assert.equal(store.page.items[0].id, 'new');
  const failed = store.browse({query: 'lost'});
  store.setOpen(false);
  third.reject(Error('Old transport failed'));
  await assert.rejects(failed, /Old transport failed/);
  assert.equal(store.error, '');
});

test('running and pool views may return the same task without a client-side exclusion', async () => {
  const item = task('shared', 1, {running: true, pool_opportunity: {opportunity: 'collaborator', invitation: 'Check the definitions.'}});
  const store = opened(async () => page([item]));
  await store.browse({view: 'running'});
  assert.equal(store.page.items[0].id, 'shared');
  await store.browse({view: 'pool'});
  assert.equal(store.page.items[0].id, 'shared');
  assert.equal(store.page.items[0].pool_opportunity.opportunity, 'collaborator');
});

test('a shrinking list adopts the server page offset and a new view resets pagination', async () => {
  const urls = [];
  const store = opened(async path => {
    const url = new URL(path, 'http://local'); urls.push(url);
    return page([task()], {offset: url.searchParams.get('view') === 'all' ? 25 : 0, total: 26});
  });
  await store.browse({offset: 75});
  assert.equal(store.offset, 25);
  await store.browse({view: 'history'});
  assert.equal(urls[1].searchParams.get('offset'), '0');
  assert.equal(store.offset, 0);
});

test('failed page refresh retains the visible page and retry clears its error', async () => {
  let fail = false;
  const store = opened(async () => { if (fail) throw Error('Offline'); return page(); });
  await store.loadPage(); fail = true;
  await assert.rejects(store.loadPage(), /Offline/);
  assert.equal(store.page.items[0].id, 'a');
  assert.equal(store.pageLoad, null);
  assert.equal(store.error, 'Offline');
  fail = false; await store.loadPage(); assert.equal(store.error, '');
});

test('closing the browser stops polling and ignores already in-flight results', async () => {
  const response = deferred(); let calls = 0;
  const store = opened(() => { calls++; return response.promise; });
  const pending = store.loadPage(); store.setOpen(false);
  await store.refresh(); await store.loadPage();
  response.resolve(page()); await pending;
  assert.equal(calls, 1); assert.equal(store.page, null);
  store.setOpen(true); await store.loadPage();
  assert.equal(calls, 2); assert.equal(store.page.items.length, 1);
});

test('current detail updates during a pinned historical contract without replacing it', async () => {
  let current = task('a', 3);
  const urls = [];
  const store = opened(async path => {
    urls.push(path);
    if (path.startsWith('/v1/task-browser')) return page([current]);
    if (path.includes('/revisions?')) return revisions(current.revision);
    if (path.includes('?revision=')) return task('a', 1, {state: 'old-state', current_revision: 3});
    return current;
  });
  await store.selectTask('a');
  await store.selectRevision(1);
  current = task('a', 4, {state: 'done', current_evidence: {state: 'invalid'}});
  await store.refresh();
  const detail = store.detail();
  assert.equal(detail.selectedRevision, 1);
  assert.equal(detail.history.get(1).contract.objective, 'Objective 1');
  assert.equal(detail.current.state, 'done');
  assert.equal(detail.current.current_revision, 4);
  assert.equal(detail.current.current_evidence.state, 'invalid');
  assert.equal(detail.revisions.current_revision, 4);
  assert.equal(store.titles.get('a'), 'a revision 4');
  assert.equal(urls.filter(path => path.includes('?revision=')).length, 1);
  await store.selectRevision(null);
  assert.equal(detail.selectedRevision, null);
  assert.equal(detail.current.contract.objective, 'Objective 4');
});

test('historical selection is preserved when an older current-detail poll arrives later', async () => {
  const poll = deferred(); let pendingPoll = false;
  const store = opened(async path => {
    if (path.includes('/revisions?')) return revisions();
    if (path.includes('?revision=2')) return task('a', 2);
    return pendingPoll ? poll.promise : task();
  });
  await store.selectTask('a'); pendingPoll = true;
  const refresh = store.loadCurrent();
  await store.selectRevision(2);
  poll.resolve(task('a', 4)); await refresh;
  assert.equal(store.detail().selectedRevision, 2);
  assert.equal(store.detail().history.get(2).revision, 2);
  assert.equal(store.detail().current.revision, 4);
});

test('switching task discards the old task responses and permits a fresh request on return', async () => {
  const a = deferred(); let calls = 0;
  const store = opened(async path => {
    if (path.includes('/revisions?')) return revisions();
    if (path === '/v1/tasks/a') { calls++; return a.promise; }
    return task('b');
  });
  const previous = store.selectTask('a');
  await store.selectTask('b');
  a.resolve(task('a')); await previous;
  assert.equal(store.selectedTask, 'b');
  assert.equal(store.detail().current.id, 'b');
  assert.equal(store.detail('a').current, null);
  await store.selectTask('a');
  assert.equal(calls, 2); assert.equal(store.detail().current.id, 'a');
});

test('returning to Current ignores a late frozen contract or its failed request', async () => {
  const old = deferred();
  const store = opened(async path => {
    if (path.includes('/revisions?')) return revisions();
    if (path.includes('?revision=')) return old.promise;
    return task();
  });
  await store.selectTask('a');
  const pinned = store.selectRevision(1);
  await store.selectRevision(null);
  old.reject(Error('Historical source unavailable'));
  await assert.rejects(pinned, /Historical source unavailable/);
  assert.equal(store.detail().selectedRevision, null);
  assert.equal(store.detail().historicalError, '');
  assert.equal(store.detail().current.revision, 3);
});

test('revision pages are server-paged and stale page responses cannot overwrite a newer page', async () => {
  const older = deferred(); const urls = [];
  const store = opened(async path => {
    urls.push(path);
    if (path.includes('/revisions?offset=20')) return older.promise;
    if (path.includes('/revisions?')) return revisions(55);
    return task('a', 55);
  });
  await store.selectTask('a');
  const pending = store.loadRevisions('a', 20);
  await store.loadRevisions('a', 0);
  older.resolve(revisions(55, {items: [{revision: 35}], offset: 20, next_offset: 40}));
  await pending;
  assert.equal(store.detail().revisionOffset, 0);
  assert.equal(store.detail().revisions.items[0].revision, 55);
  assert.ok(urls.includes('/v1/tasks/a/revisions?offset=20&limit=20'));
});

test('revision paging adopts a clamped server offset and permits retry after errors', async () => {
  let fail = false;
  const store = opened(async path => {
    if (path.includes('/revisions?')) {
      if (fail) throw Error('Revision index unavailable');
      return revisions(7, {offset: 0});
    }
    return task('a', 7);
  });
  await store.selectTask('a');
  fail = true; await assert.rejects(store.loadRevisions('a', 40), /Revision index unavailable/);
  assert.equal(store.detail().revisionsLoad, null);
  fail = false; await store.loadRevisions();
  assert.equal(store.detail().revisionOffset, 0);
  assert.equal(store.detail().revisionsError, '');
});

test('the current task pointer never makes a waiting or retained worker appear to be executing', () => {
  const value = {id: 'a', display_name: 'Worker 1', current_task_id: 'task', state: 'suspended', work_intention: 'waiting', active_attempt: null};
  assert.equal(workerStateLabel(value), 'Waiting');
  assert.equal(workerTaskPrefix(value, []), 'Waiting on ');
  const items = [{id: 'task', collaboration: {members: [{worker_id: 'a', execution_status: 'Retained'}]}}];
  assert.equal(workerTaskPrefix({...value, state: 'idle'}, items), 'Retained task: ');
  items[0].collaboration.members[0].execution_status = 'Working elsewhere';
  assert.equal(workerTaskPrefix({...value, active_attempt: 'other-task'}, items), 'Retained task: ');
  items[0].collaboration.members[0].execution_status = 'Ready';
  assert.equal(workerTaskPrefix(value, items), 'Ready to continue ');
  assert.equal(workerStateLabel({...value, state: 'running', active_attempt: 'message', execution_mode: 'message'}), 'Handling message');
  assert.equal(workerStateLabel({...value, state: 'draining', active_attempt: 'turn'}), 'Finishing turn');
});

class Disclosure {
  constructor(key, open = false, lazy = null) {
    this.dataset = {key}; this.open = open; this.children = []; this.lazy = lazy;
  }
  querySelectorAll() { return this.children.flatMap(child => [child, ...child.querySelectorAll()]); }
  dispatchEvent(event) {
    assert.equal(event.type, 'toggle');
    if (this.open && this.lazy && !this.children.length) this.children.push(this.lazy());
  }
}

test('refresh preserves open and closed lazy disclosures and both scroll positions', () => {
  const root = new Disclosure('root');
  const parent = new Disclosure('parent', true), child = new Disclosure('child', true), closed = new Disclosure('closed', false);
  parent.children.push(child); root.children.push(parent, closed);
  const scroller = {scrollTop: 250, scrollLeft: 7};
  preserveDisclosures(root, () => {
    root.children = [new Disclosure('parent', false, () => new Disclosure('child')), new Disclosure('closed', true)];
    scroller.scrollTop = 0; scroller.scrollLeft = 0;
  }, scroller);
  assert.equal(root.children[0].open, true);
  assert.equal(root.children[0].children[0].open, true);
  assert.equal(root.children[1].open, false);
  assert.equal(scroller.scrollTop, 250);
  assert.equal(scroller.scrollLeft, 7);
});

test('a reload after the operator changed a task drops the reads begun before it', async () => {
  const before = deferred(), after = deferred(), responses = [before.promise, after.promise];
  const store = opened(() => responses.shift());
  const polled = store.loadPage();
  const reloaded = store.reload();
  before.resolve(page([task('a', 1, {admission: {priority: 1, pinned_at: null}})]));
  after.resolve(page([task('a', 1, {admission: {priority: 1, pinned_at: 5}})]));
  await Promise.all([polled, reloaded]);
  assert.equal(store.page.items[0].admission.pinned_at, 5);
  assert.equal(store.pageLoad, null);
});

test('a task shows its place in admission while it is unfinished and not a review', () => {
  const placed = {priority: 0, contract_priority: 1, operator_priority: 0, pinned_at: 5};
  assert.equal(admissionLabel(task('a', 1, {admission: {priority: 1, contract_priority: 1, operator_priority: null, pinned_at: null}})),
    'Priority 1');
  assert.equal(admissionLabel(task('a', 1, {admission: placed})), 'Priority 0 · set by the operator (contract 1)');
  assert.equal(admissionLabel(task('a', 1, {admission: placed, state: 'done'})), '');
  assert.equal(admissionLabel(task('a', 1, {admission: placed, contract: {title: 'Review', kind: 'review'}})), '');
  assert.equal(admissionLabel(task()), '');
});
