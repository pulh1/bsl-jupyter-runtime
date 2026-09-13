import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {test} from 'node:test';
async function api() {
  assert.ok(existsSync(new URL('../src/runtimeFence.ts', import.meta.url)), 'response fence missing');
  return import('../src/runtimeFence.js');
}
test('delayed completion and resolve cannot cross widget generation; Python delegates unchanged', async () => {
  const {fenceCompletionProvider, RuntimeResponseFence} = await api();
  const a = new RuntimeResponseFence(); const b = new RuntimeResponseFence();
  const waiting: ((value: any) => void)[] = [];
  const provider: any = {identifier: 'lsp', isApplicable: async () => true,
    fetch: () => new Promise(resolve => waiting.push(resolve)),
    resolve: () => new Promise(resolve => waiting.push(resolve))};
  const wrapped = fenceCompletionProvider(provider, (c: any) => c.language === 'bsl' ? c.widget === 'a' ? a : b : undefined);
  const pending = wrapped.fetch({} as any, {widget: 'a', language: 'bsl'} as any);
  a.advance(); waiting.shift()!({start: 0, end: 1, items: [{label:'old'}]});
  assert.deepEqual((await pending).items, []);
  const other = wrapped.fetch({} as any, {widget: 'b', language: 'bsl'} as any);
  a.advance(); waiting.shift()!({start: 0, end: 1, items: [{label:'other'}]});
  const item = (await other).items[0]; assert.equal(item.label, 'other');
  const resolve = wrapped.resolve!(item, {widget: 'b', language: 'bsl'} as any);
  b.advance(); waiting.shift()!({...item, documentation:'stale'});
  await assert.rejects(resolve, /expired/);
  const python = wrapped.fetch({} as any, {widget:'a', language:'python'} as any);
  a.advance(); const reply = {items:[{label:'python'}]}; waiting.shift()!(reply);
  assert.equal(await python, reply);
});

test('diagnostic version fence clears delayed old cache before delivery and preserves newest valid response', async () => {
  const {RuntimeResponseFence} = await api(); const fence = new RuntimeResponseFence();
  const applied: any[] = [];
  let version = 2;
  const clear = (p: any) => applied.push(p);
  fence.diagnostic({uri:'file:///a.bsl', version:1, diagnostics:[{message:'old'}]}, () => version, clear);
  await Promise.resolve(); assert.deepEqual(applied.at(-1).diagnostics, []);
  fence.diagnostic({uri:'file:///a.bsl', version:1, diagnostics:[{message:'old'}]}, () => version, clear);
  fence.diagnostic({uri:'file:///a.bsl', version:2, diagnostics:[{message:'new'}]}, () => version, clear);
  await Promise.resolve(); assert.equal(applied.at(-1).diagnostics[0].message, 'new');
  fence.diagnostic({uri:'file:///a.bsl', version:1, diagnostics:[{message:'delayed old'}]}, () => version, clear);
  await Promise.resolve(); assert.equal(applied.at(-1).diagnostics[0]?.message, 'new');
  fence.advance(); version = 3;
  fence.diagnostic({uri:'file:///a.bsl', version:2, diagnostics:[{message:'old'}]}, () => version, clear);
  await Promise.resolve(); assert.deepEqual(applied.at(-1).diagnostics, []);
});

test('replacement blocks transition-version diagnostics until acknowledged child authority then advances again', async () => {
  const {RuntimeResponseFence, fenceCompletionProvider} = await api();
  const {NotebookRuntimeBinding} = await import('../src/runtimeBinding.js');
  const fence = new RuntimeResponseFence(); let sentVersion = 0; let post: (value: any) => void;
  const replies: ((value: any) => void)[] = [];
  const provider: any = {identifier:'lsp', isApplicable:async () => true,
    fetch: () => new Promise(resolve => replies.push(resolve)), resolve: async () => ({label:'pending resolve'})};
  const wrapped = fenceCompletionProvider(provider, () => fence);
  const controller = new NotebookRuntimeBinding('w', async method => method === 'POST' ?
    new Promise(resolve => {post = resolve;}) : null,
    ready => {sentVersion++; fence.advance(ready);}, undefined, () => () => {});
  const updating = controller.update('a.ipynb', 'new-kernel', ['file:///a.bsl']);
  const transitionVersion = sentVersion;
  const old = {uri:'file:///a.bsl', version:transitionVersion, diagnostics:[{message:'old context at transition version'}]};
  const accepted: any[] = [];
  fence.diagnostic(old, () => sentVersion, response => accepted.push(response));
  await Promise.resolve(); assert.deepEqual(accepted.at(-1).diagnostics, []);
  const duringPending = wrapped.fetch({} as any, {} as any);
  await assert.rejects(wrapped.resolve!({label:'untracked item'}, {} as any), /expired/);
  replies.shift()!({items:[{label:'pending authority'}]});
  assert.deepEqual((await duringPending).items, []);
  const status: any = {binding_id:'new-binding', runtime_id:'new-runtime', epoch:1, analysis_state:'indexing'};
  post!(status); await updating;
  assert.equal(sentVersion, transitionVersion, 'REST registry acceptance is not a child acknowledgement');
  const awaitingAck = wrapped.fetch({} as any, {} as any);
  controller.accept({epoch:controller.epoch, kernelId:'new-kernel'}, {...status, analysis_state:'ready'});
  assert.ok(sentVersion > transitionVersion);
  replies.shift()!({items:[{label:'started before acknowledgement'}]});
  assert.deepEqual((await awaitingAck).items, []);
  fence.diagnostic(old, () => sentVersion, response => accepted.push(response));
  await Promise.resolve(); assert.deepEqual(accepted.at(-1).diagnostics, []);
  fence.diagnostic({...old, version:sentVersion, diagnostics:[{message:'current'}]}, () => sentVersion, response => accepted.push(response));
  await Promise.resolve(); assert.equal(accepted.at(-1).diagnostics[0].message, 'current');
  controller.dispose();
});
