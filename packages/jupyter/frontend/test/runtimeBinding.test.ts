import assert from 'node:assert/strict';
import { test } from 'node:test';
import { existsSync } from 'node:fs';

async function api() {
  assert.ok(existsSync(new URL('../src/runtimeBinding.ts', import.meta.url)), 'runtime lifecycle controller missing');
  return import('../src/runtimeBinding.js');
}
const response = (id: string, epoch = 1, installation = 'installation-a') => ({
  binding_id: id, epoch, mode: 'project', reason: null, analysis_state: 'ready', analysis_reason: null,
  installation_id: installation
});
function boundary() {
  const calls: {method: string; path: string; body?: unknown}[] = [];
  const replies: ((value: any) => void)[] = [];
  return {calls, replies, request: (method: string, path: string, body?: unknown) => {
    calls.push({method, path, body});
    if (method === 'DELETE') return Promise.resolve(null);
    return new Promise<any>(resolve => replies.push(resolve));
  }};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('empty installed tracker falls back by widget identity across rename and late registration', async () => {
  const module = await api(); assert.equal(typeof module.findNotebookAdapter, 'function');
  const widget = {path:'before.ipynb'}; const foreign = {widget:{path:'before.ipynb'}};
  const map = new Map([['before.ipynb', foreign]]);
  const tracker = {find: () => undefined};
  assert.equal(module.findNotebookAdapter(widget, tracker, undefined), undefined);
  assert.equal(module.findNotebookAdapter(widget, tracker, map), undefined);
  const actual = {widget}; map.set('old-key.ipynb', actual); widget.path = 'renamed.ipynb';
  assert.equal(module.findNotebookAdapter(widget, tracker, map), actual);
  const preferred = {widget};
  assert.equal(module.findNotebookAdapter(widget, {find: () => preferred}, map), preferred);
});

test('startup and late attachment bind actual document URI and isolate notebooks sharing kernel', async () => {
  const {NotebookRuntimeBinding} = await api(); const io = boundary();
  const a = new NotebookRuntimeBinding('widget-a', io.request);
  const b = new NotebookRuntimeBinding('widget-b', io.request);
  await a.update('a.ipynb', 'k', []);
  assert.equal(io.calls.length, 0);
  const pa = a.update('a.ipynb', 'k', ['file:///a.python-bsl.bsl']);
  const pb = b.update('b.ipynb', 'k', ['file:///b.python-bsl.bsl']);
  assert.deepEqual(io.calls.map(c => c.body), [
    {notebook_path: 'a.ipynb', kernel_id: 'k', document_uri: 'file:///a.python-bsl.bsl'},
    {notebook_path: 'b.ipynb', kernel_id: 'k', document_uri: 'file:///b.python-bsl.bsl'}]);
  io.replies.shift()!(response('a')); io.replies.shift()!(response('b')); await Promise.all([pa, pb]);
  assert.equal(a.status?.binding_id, 'a'); assert.equal(b.status?.binding_id, 'b');
  a.dispose(); b.dispose();
});

test('kernel transitions, restart and rename immediately fence old work and delete old association', async () => {
  const {NotebookRuntimeBinding} = await api(); const io = boundary(); let fences = 0;
  const a = new NotebookRuntimeBinding('w', io.request, () => fences++);
  const old = a.update('a.ipynb', 'k1', ['file:///a.bsl']);
  const changed = a.update('a.ipynb', 'k2', ['file:///a.bsl']);
  io.replies.shift()!(response('old')); io.replies.shift()!(response('new')); await Promise.all([old, changed]);
  assert.equal(a.status?.binding_id, 'new');
  assert.ok(io.calls.some(c => c.method === 'DELETE' && c.path.endsWith('/old')));
  const restarted = a.update('a.ipynb', 'k2', ['file:///a.bsl'], true);
  assert.equal(a.status, null); assert.ok(fences >= 3);
  io.replies.shift()!(response('restart')); await restarted;
  const renamed = a.update('renamed.ipynb', 'k2', ['file:///renamed.bsl']);
  io.replies.shift()!(response('renamed')); await renamed;
  assert.equal(a.widgetId, 'w'); assert.equal(a.status?.binding_id, 'renamed');
  a.dispose(); assert.ok(io.calls.some(c => c.method === 'DELETE' && c.path.endsWith('/renamed')));
});

test('binding ticket rejects stale replies and status refresh fences runtime replacement and epochs', async () => {
  const {NotebookRuntimeBinding} = await api(); const io = boundary(); let fences = 0;
  const a = new NotebookRuntimeBinding('w', io.request, () => fences++);
  const first = a.beginBinding('kernel-a'); const second = a.beginBinding('kernel-b');
  assert.equal(a.accept(first, response('a')), false); assert.equal(a.accept(second, response('b')), true);
  assert.equal(a.accept(second, response('b', 0)), false);
  const previous = fences;
  assert.equal(a.accept(second, response('b', 2, 'runtime-b')), true);
  assert.ok(fences > previous);
  a.dispose(); assert.equal(a.accept(second, response('b', 3)), false);
});

test('bounded background refresh works without typing and stops on disposal', async () => {
  const {NotebookRuntimeBinding} = await api(); const io = boundary();
  let scheduled: (() => void) | undefined; let cancelled = false;
  const a = new NotebookRuntimeBinding('w', io.request, undefined, undefined,
    callback => { scheduled = callback; return () => { cancelled = true; scheduled = undefined; }; });
  const binding = a.update('a.ipynb', null, ['file:///a.bsl']);
  io.replies.shift()!(response('a')); await binding;
  scheduled!(); await tick(); assert.equal(io.calls.at(-1)?.method, 'GET');
  io.replies.shift()!(response('a', 2)); await tick(); assert.equal(a.status?.epoch, 2);
  a.dispose(); assert.equal(cancelled, true); assert.equal(scheduled, undefined);
});

test('replacement resumes only on current child acknowledgement, not indexing or failed/stale replies', async () => {
  const {NotebookRuntimeBinding} = await api(); const io = boundary(); const ready: boolean[] = [];
  const a = new NotebookRuntimeBinding('w', io.request, value => ready.push(value), undefined, () => () => {});
  const updating = a.update('a.ipynb', 'k', ['file:///a.bsl']);
  io.replies.shift()!({...response('a'), analysis_state:'indexing'}); await updating;
  const ticket = {epoch:a.epoch, kernelId:'k'};
  assert.deepEqual(ready, [false]);
  a.accept(ticket, response('a')); a.accept(ticket, response('a'));
  assert.deepEqual(ready, [false, true]);
  a.accept(ticket, {...response('a', 2), analysis_state:'synchronizing'});
  assert.deepEqual(ready, [false, true, false]);
  a.accept(ticket, response('a', 2));
  assert.deepEqual(ready, [false, true, false, true]);
  const replacement = a.beginBinding('next-kernel');
  assert.equal(a.accept(ticket, response('a', 3)), false);
  a.accept(replacement, {...response('next'), analysis_state:'unavailable'});
  assert.equal(ready.at(-1), false);
  a.dispose();
});

test('server restart missing binding recovers with fresh association without editing', async () => {
  const {NotebookRuntimeBinding} = await api(); let scheduled: (() => void) | undefined; let posts = 0;
  const a = new NotebookRuntimeBinding('w', async method => {
    if (method === 'POST') return response(++posts === 1 ? 'old-server' : 'new-server');
    if (method === 'GET') throw {status:404};
    return null;
  }, undefined, undefined, callback => { scheduled = callback; return () => { scheduled = undefined; }; });
  await a.update('a.ipynb', 'k', ['file:///a.bsl']);
  scheduled!(); await tick();
  assert.equal(a.status?.binding_id, 'new-server');
  assert.equal(posts, 2); a.dispose();
});

test('status outage invalidates once and preserves epoch authority on recovery', async () => {
  const {NotebookRuntimeBinding} = await api(); let scheduled: (() => void) | undefined;
  let fences = 0; let fail = true; let recovered = response('a', 1);
  const a = new NotebookRuntimeBinding('w', async method => {
    if (method === 'POST') return response('a', 2);
    if (method === 'GET') { if (fail) throw {status:503}; return recovered; }
    return null;
  }, () => fences++, undefined, callback => { scheduled = callback; return () => {}; });
  await a.update('a.ipynb', 'k', ['file:///a.bsl']);
  const initial = fences;
  scheduled!(); await tick();
  assert.equal(a.status, null); assert.equal(fences, initial + 1);
  scheduled!(); await tick(); assert.equal(fences, initial + 1);
  fail = false; scheduled!(); await tick(); assert.equal(a.status, null);
  recovered = response('a', 3); scheduled!(); await tick();
  assert.equal(a.status?.epoch, 3); assert.equal(fences, initial + 2);
  a.dispose();
});

test('status describes source analysis and explicitly unconfirmed project convergence', async () => {
  await api(); const {formatRuntimeStatus} = await import('../src/runtimeStatus.js');
  const status = {binding_id: 'a', epoch: 2, installation_id: 'i', mode: 'project',
    reason: null, analysis_state: 'ready', analysis_reason: null};
  const text = formatRuntimeStatus(status);
  assert.match(text, /project/);
  assert.doesNotMatch(text, /generation|retained|operation|runtime outcome/i);
  assert.match(formatRuntimeStatus({...status, analysis_reason:'index-convergence-unconfirmed'}), /queries available.*index convergence unconfirmed/i);
  assert.doesNotMatch(formatRuntimeStatus({...status, mode:'virtual-only'}), /index/i);
  assert.match(formatRuntimeStatus({...status, analysis_state:'updating'}), /updating/i);
});

test('binding emits exact source-free claims after POST and authorized refresh and retries readiness', async () => {
  const {NotebookRuntimeBinding} = await api(); const io = boundary(); let scheduled: (() => void) | undefined;
  const claims: unknown[] = [];
  const binding = new NotebookRuntimeBinding('w', io.request, undefined, undefined,
    callback => {scheduled = callback; return () => {};}, (uri, id) => claims.push({settings:{onecProjectBinding:{document_uri:uri, binding_id:id}}}));
  const pending = binding.update('a.ipynb', 'k', ['file:///a.bsl']);
  io.replies.shift()!(response('a')); await pending;
  assert.deepEqual(claims, [{settings:{onecProjectBinding:{document_uri:'file:///a.bsl', binding_id:'a'}}}]);
  scheduled!(); await tick(); io.replies.shift()!(response('a')); await tick();
  assert.equal(claims.length, 2);
  scheduled!(); await tick(); binding.beginBinding('new');
  io.replies.shift()!(response('a', 2)); await tick(); assert.equal(claims.length, 2);
  assert.ok(io.calls.every(call => !call.path.includes('/sources/')));
  binding.dispose();
});

test('acknowledged degraded virtual children allow queries while child failures and pending work stay fenced', async () => {
  const {NotebookRuntimeBinding} = await api(); const ready: boolean[] = [];
  const binding = new NotebookRuntimeBinding('w', async () => null, state => ready.push(state));
  const ticket = binding.beginBinding('k');
  for (const reason of ['workspace-unsafe', 'workspace-safety-limit', 'workspace-unavailable',
    'workspace-replaced', 'workspace-notification-unavailable', 'workspace-stopped']) {
    binding.accept(ticket, {...response('a'), analysis_state:'updating'});
    assert.equal(ready.at(-1), false);
    binding.accept(ticket, {...response('a'), analysis_state:'unavailable', analysis_reason:reason});
    assert.equal(ready.at(-1), true, reason);
    assert.equal(binding.status?.analysis_reason, reason);
  }
  for (const reason of ['child-unavailable', 'child-capacity-unavailable', 'child-synchronization-failed', 'control-unavailable', 'unrecognized']) {
    binding.accept(ticket, {...response('a'), analysis_state:'unavailable', analysis_reason:reason});
    assert.equal(ready.at(-1), false, reason);
  }
  binding.dispose();
});
