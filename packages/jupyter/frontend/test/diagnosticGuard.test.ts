import assert from 'node:assert/strict';
import { test } from 'node:test';
import { guardDisposedBslDiagnostics } from '../src/diagnosticGuard.js';

test('only disposed BSL callbacks are ignored; live/non-BSL calls and receiver are unchanged', () => {
  const failure = new Error('original live failure');
  const feature = {handleDiagnostic(this: unknown, ...args: unknown[]) {
    if ((args[0] as any)?.fail) throw failure;
    return {receiver: this, args};
  }};
  guardDisposedBslDiagnostics(feature);
  assert.equal(feature.handleDiagnostic({}, {language: 'bsl', isDisposed: true}, {}), undefined);
  for (const doc of [{language:'bsl', isDisposed:false}, {language:'python', isDisposed:true},
    {language:'bsl'}, null]) {
    const response = {};
    const adapter = {};
    const result = feature.handleDiagnostic(response, doc, adapter)!;
    assert.equal(result.receiver, feature);
    assert.deepEqual(result.args, [response, doc, adapter]);
    assert.throws(() => feature.handleDiagnostic({fail:true}, doc, adapter), error => error === failure);
  }
});

test('live asynchronous diagnostic errors are not swallowed by the guard', async () => {
  const failure = new Error('original rejection');
  const feature = {async handleDiagnostic(..._args: unknown[]) {throw failure;}};
  guardDisposedBslDiagnostics(feature);
  await assert.rejects(feature.handleDiagnostic({}, {language:'bsl', isDisposed:false}), error => error === failure);
});
