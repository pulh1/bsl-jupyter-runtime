import assert from 'node:assert/strict';
import {test} from 'node:test';

function context(source: string, cellType = 'code', kernel: object | null = {}) {
  return {widget: {id: 'notebook'}, session: {kernel}, editor: {model: {sharedModel: {
    cell_type: cellType, getSource: () => source
  }}}} as any;
}

test('foreign BSL code uses the kernel even when LSP excludes foreign documents', async () => {
  const {withBslKernelCompletion} = await import('../src/kernelCompletion.js');
  const original: any = {identifier: 'CompletionProvider:kernel',
    isApplicable: async () => false, fetch: async () => ({items: []})};
  const wrapped = withBslKernelCompletion(original);
  assert.equal(await wrapped.isApplicable(context('%%bsl\nДанные[0].')), true);
  assert.equal(await wrapped.isApplicable(context('%%bsl\nДанные[0].', 'code', null)), false);
});

test('other languages and non-code cells retain the original applicability decision', async () => {
  const {withBslKernelCompletion} = await import('../src/kernelCompletion.js');
  const calls: any[] = [];
  const original: any = {identifier: 'CompletionProvider:kernel', answer: false,
    async isApplicable(value: any) {assert.equal(this, original); calls.push(value); return this.answer;},
    fetch: async () => ({items: []})};
  const wrapped = withBslKernelCompletion(original);
  for (const ctx of [context('print(1)'), context('%%sql\nSELECT 1'),
      context('%%bsl\nДанные.', 'markdown'), context('%%bslx\nДанные.')]) {
    for (const answer of [false, true]) {
      original.answer = answer;
      assert.equal(await wrapped.isApplicable(ctx), answer);
      assert.equal(calls.at(-1), ctx);
    }
  }
  assert.equal(calls.length, 8);
});

test('kernel fetch and optional provider methods preserve receiver and arguments', async () => {
  const {withBslKernelCompletion} = await import('../src/kernelCompletion.js');
  const calls: any[] = [];
  const reply = {start: 11, end: 11, items: [{label: 'Номер', source: 'kernel', type: 'property'}]};
  const model = {};
  const original: any = {identifier: 'CompletionProvider:kernel', rank: 550, renderer: {},
    isApplicable: async () => false,
    async fetch(...args: any[]) {assert.equal(this, original); calls.push(['fetch', ...args]); return reply;},
    async resolve(...args: any[]) {assert.equal(this, original); calls.push(['resolve', ...args]); return args[0];},
    modelFactory(...args: any[]) {assert.equal(this, original); calls.push(['model', ...args]); return model;},
    shouldShowContinuousHint(...args: any[]) {assert.equal(this, original); calls.push(['hint', ...args]); return true;}
  };
  const wrapped = withBslKernelCompletion(original);
  const ctx = context('%%bsl\nДанные.'); const request: any = {text: '%%bsl\nДанные.', offset: 14};
  const item = reply.items[0]; const patch: any = {start: 14, value: 'Номер'}; const change: any = {};
  assert.equal(await wrapped.fetch(request, ctx, 1), reply);
  assert.equal(await wrapped.resolve!(item, ctx, patch), item);
  assert.equal(await wrapped.modelFactory!(ctx), model);
  assert.equal(wrapped.shouldShowContinuousHint!(false, change, ctx), true);
  assert.deepEqual(calls, [['fetch', request, ctx, 1], ['resolve', item, ctx, patch], ['model', ctx], ['hint', false, change, ctx]]);
  assert.equal(wrapped.identifier, original.identifier);
  assert.equal(wrapped.rank, original.rank);
  assert.equal(wrapped.renderer, original.renderer);
});

test('BSL cell filters Python proxy methods but keeps runtime field completions', async () => {
  const {withBslKernelCompletion} = await import('../src/kernelCompletion.js');
  const original: any = {identifier: 'CompletionProvider:kernel', isApplicable: async () => true,
    fetch: async (request: any) => ({start: 14, end: 14, items: [
      {label: 'materialize', type: 'function'}, {label: 'head', type: 'function'},
      {label: 'tabularsection', type: 'function'},
      ...(request.text.endsWith('Новы') ? [] : [{label: 'Поле', type: 'property'}])
    ]})};
  const wrapped = withBslKernelCompletion(original);
  const reply = await wrapped.fetch({text: '%%bsl\nЗапрос.', offset: 14}, context('%%bsl\nЗапрос.'));
  assert.deepEqual(reply.items, [{label: 'Поле', type: 'property'}]);
  const noFields = await wrapped.fetch({text: '%%bsl\nЗапрос = Новы', offset: 19}, context('%%bsl\nЗапрос = Новы'));
  assert.deepEqual(noFields.items, []);
});
