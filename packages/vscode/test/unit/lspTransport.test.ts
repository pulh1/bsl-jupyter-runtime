import assert from 'node:assert/strict';
import {access} from 'node:fs/promises';
import path from 'node:path';
import {test} from 'node:test';
import {BslTransport, TRANSPORT_LIMITS} from '../../src/lspTransport';

const fixture = path.resolve(__dirname, '../fixtures/fake-ls.js');
const privateConfig = {sendErrors: 'never', traceLog: null, diagnostics: {computeTrigger: 'onType'}};
async function start() { return BslTransport.start(process.execPath, undefined, [fixture]); }

test('initialize, didOpen, and configuration use the private child connection', async t => {
  const child = await start();
  t.after(() => child.close());
  assert.ok((await child.request<{capabilities: object}>('initialize', {processId: null})).capabilities);
  const opened = {textDocument: {uri: 'file:///virtual.bsl', languageId: 'bsl', version: 1, text: 'Сообщить(1);'}};
  await child.notify('textDocument/didOpen', opened);
  assert.deepEqual(await child.request('test/opened', {}), opened);
  assert.deepEqual(await child.request('test/configuration', {}), [privateConfig, privateConfig]);
  for (const method of ['window/workDoneProgress/create', 'client/registerCapability']) {
    assert.equal(await child.request('test/serverRequest', {method, params: {}}), null);
  }
  await assert.rejects(child.request('test/serverRequest', {method: 'workspace/applyEdit', params: {}}));
});

test('child cannot inherit secrets, source cwd, or user configuration and close removes private files', async () => {
  process.env.ONEC_TEST_SECRET = 'never-forward-this';
  const child = await BslTransport.start(process.execPath, path.resolve(__dirname), [fixture]);
  delete process.env.ONEC_TEST_SECRET;
  let directory = '';
  try {
    const info = await child.request<{cwd: string; env: NodeJS.ProcessEnv; configuration: unknown}>('test/environment', {});
    directory = info.cwd;
    assert.notEqual(directory, path.resolve(__dirname));
    assert.notEqual(directory, process.cwd());
    assert.equal(info.env.ONEC_TEST_SECRET, undefined);
    assert.equal(info.env.NODE_OPTIONS, undefined);
    for (const key of ['HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP']) assert.equal(info.env[key], directory);
    assert.deepEqual(info.configuration, privateConfig);
  } finally { await child.close(); }
  await assert.rejects(access(directory));
  await child.close();
  await assert.rejects(child.request('initialize', {}), /closed/i);
});

test('aborting a request rejects promptly and preserves responsive requests', async t => {
  const child = await start();
  t.after(() => child.close());
  const controller = new AbortController();
  const pending = child.request('test/hang', {}, controller.signal);
  controller.abort();
  await assert.rejects(pending, /cancel/i);
  assert.ok((await child.request<{capabilities: object}>('initialize', {})).capabilities);
  await assert.rejects(child.request('test/hang', {}, AbortSignal.abort()), /cancel/i);
});

test('hung request has a deadline and terminates the unresponsive child', {timeout: 15000}, async t => {
  const child = await start();
  t.after(() => child.close());
  const failures: Error[] = [];
  child.onFailure(error => failures.push(error));
  await child.request('initialize', {});
  t.mock.timers.enable({apis: ['setTimeout']});
  const hung = child.request('test/hang', {});
  await child.request('initialize', {}); // The hanging request was fully written first.
  t.mock.timers.tick(TRANSPORT_LIMITS.requestTimeoutMs + 1);
  t.mock.timers.reset();
  await assert.rejects(hung, /timed out/i);
  await child.close();
  assert.throws(() => process.kill(child.pid, 0));
  assert.equal(failures.length, 1);
});

test('pending request bound rejects overflow without disrupting existing requests', async t => {
  const child = await start();
  t.after(() => child.close());
  const requests = Array.from({length: TRANSPORT_LIMITS.maxPending}, () => child.request('test/hang', {}).catch(() => {}));
  await assert.rejects(child.request('test/hang', {}), /pending|busy/i);
  await child.close();
  await Promise.all(requests);
});

for (const mode of ['oversize', 'badHeader', 'secretError', 'flood']) {
  test(`rejects ${mode} input, reports sanitized failure, and reaps the child`, async t => {
    const child = await start();
    t.after(() => child.close());
    const failed = new Promise<Error>(resolve => child.onFailure(resolve));
    await child.notify(`test/${mode}`, {});
    const failure = await failed;
    assert.doesNotMatch(failure.message, /secret-password/);
    await child.close();
    assert.throws(() => process.kill(child.pid, 0));
  });
}

test('outbound byte bound rejects large content and stderr is drained without forwarding', async t => {
  const child = await start();
  t.after(() => child.close());
  await assert.rejects(child.notify('textDocument/didOpen', {text: 'x'.repeat(TRANSPORT_LIMITS.maxMessageBytes + 1)}), /large|limit/i);
  await child.notify('test/stderr', {});
  assert.ok((await child.request<{capabilities: object}>('initialize', {})).capabilities);
});

test('a blocked request write fails before the longer request deadline', async t => {
  const child = await start();
  t.after(() => child.close());
  await child.request('test/pauseInput', {});
  t.mock.timers.enable({apis: ['setTimeout']});
  const pending = child.request('test/hang', {text: 'x'.repeat(1024 * 1024)});
  const failed = new Promise<Error>(resolve => child.onFailure(resolve));
  // Let the JSON-RPC writer reach the real full pipe before advancing its deadline.
  await new Promise<void>(resolve => setImmediate(resolve));
  t.mock.timers.tick(TRANSPORT_LIMITS.writeTimeoutMs + 1);
  t.mock.timers.reset();
  await Promise.race([
    assert.rejects(pending, /closed|write|failed/i),
    new Promise((_, reject) => { const timer = setTimeout(() => reject(new Error('Blocked write deadline missing')), 1000); timer.unref(); }),
  ]);
  assert.match((await failed).message, /write|output/i);
});

test('unexpected child exit rejects pending requests and failure subscription can be disposed', async t => {
  const child = await start();
  t.after(() => child.close());
  let disposedCalls = 0;
  child.onFailure(() => disposedCalls++).dispose();
  const failure = new Promise<Error>(resolve => child.onFailure(resolve));
  await assert.rejects(child.request('test/exit', {}));
  assert.match((await failure).message, /exited|closed/i);
  assert.equal(disposedCalls, 0);
});

test('missing executable rejects startup with a sanitized error', async () => {
  await assert.rejects(BslTransport.start(path.join(__dirname, 'missing-executable'), undefined), /start|spawn/i);
});
