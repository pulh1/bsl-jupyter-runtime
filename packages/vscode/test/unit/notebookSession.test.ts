import assert from 'node:assert/strict';
import {mkdtemp, readFile, readdir, rm, rmdir, stat, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {setImmediate as nextTurn} from 'node:timers/promises';
import {setTimeout as delay} from 'node:timers/promises';
import {test} from 'node:test';
import {BslTransport} from '../../src/lspTransport';
import {NotebookSnapshot} from '../../src/notebookModel';
import {NotebookLspSession, TransportFactory} from '../../src/notebookSession';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

// The only fake boundary is the external child/JSON-RPC connection. Session
// scheduling, model mapping, workspace creation and filesystem checks are real.
class FakeTransport {
  messages: {method: string; params: any; signal?: AbortSignal}[] = [];
  pending: ReturnType<typeof deferred<unknown>>[] = [];
  failures = new Set<(error: Error) => void>();
  closed = 0;
  closeGate?: Promise<void>;
  closeError?: Error;
  private closing?: Promise<void>;
  initializeGate?: Promise<unknown>;
  notifyGate?: {method: string; promise: Promise<void>};
  constructor(readonly pid: number) {}
  request<T>(method: string, params: unknown, signal?: AbortSignal): Promise<T> {
    this.messages.push({method, params, signal});
    if (method === 'initialize') return (this.initializeGate ?? Promise.resolve({capabilities: {}})) as Promise<T>;
    const reply = deferred<unknown>();
    this.pending.push(reply);
    return reply.promise as Promise<T>;
  }
  async notify(method: string, params: unknown): Promise<void> {
    this.messages.push({method, params});
    if (this.notifyGate?.method === method) await this.notifyGate.promise;
  }
  onFailure(listener: (error: Error) => void) {
    this.failures.add(listener);
    return {dispose: () => { this.failures.delete(listener); }};
  }
  close(): Promise<void> {
    return this.closing ??= (async () => {
      this.closed++;
      await this.closeGate;
      if (this.closeError) throw this.closeError;
    })();
  }
  fail(): void { for (const listener of this.failures) listener(new Error('fake failure')); }
}

class FakeFactory implements TransportFactory {
  transports: FakeTransport[] = [];
  roots: (string | undefined)[] = [];
  startGate?: Promise<void>;
  onStart?: () => void;
  async start(rootPath: string | undefined): Promise<BslTransport> {
    this.roots.push(rootPath);
    this.onStart?.();
    await this.startGate;
    const transport = new FakeTransport(100 + this.transports.length);
    this.transports.push(transport);
    return transport as unknown as BslTransport;
  }
}

function snapshot(version = 7, text = 'А😀Б'): NotebookSnapshot {
  return NotebookSnapshot.fromCells([
    {uri: 'cell:a', kind: 'code', languageId: 'python', text: `%%bsl\n${text}`},
    {uri: 'cell:p', kind: 'code', languageId: 'python', text: 'print(1)'},
    {uri: 'cell:b', kind: 'code', languageId: 'python', text: '%%bsl\nВызов()'},
  ], version);
}

const position = {line: 1, character: 3};
const diagnostic = {range: {start: {line: 0, character: 0}, end: {line: 0, character: 1}}, message: 'Проверка', severity: 2};

test('first saved event clears diagnostics and cancels old queries before the 150ms batch is sent', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'session-watch-'));
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), root, factory);
  t.after(async () => { await session.close(); await rmdir(root); });
  const server = factory.transports[0];
  const diagnosing = session.diagnose(); await nextTurn();
  server.pending[0].resolve({kind: 'full', items: [diagnostic]}); await diagnosing;
  const query = session.query('textDocument/hover', 'cell:a', position); await nextTurn();
  const signal = server.messages.at(-1)?.signal;
  const revision = session.projectRevision;
  const uri = pathToFileURL(path.join(root, 'Module.bsl')).href;
  const first = session.filesChanged([{uri, type: 2}]);
  const second = session.filesChanged([{uri, type: 2}]);
  assert.deepEqual(session.diagnostics, []);
  assert.equal(signal?.aborted, true);
  assert.equal(session.projectRevision, revision + 1);
  assert.equal(session.epoch.projectRevision, revision + 1);
  assert.equal(session.status, 'updating');
  assert.equal(server.messages.filter(m => m.method === 'workspace/didChangeWatchedFiles').length, 0);
  server.pending[1].resolve({contents: 'old source'});
  assert.equal(await query, undefined);
  await Promise.all([first, second]);
  assert.deepEqual(server.messages.filter(m => m.method === 'workspace/didChangeWatchedFiles').map(m => m.params), [{changes: [{uri, type: 2}]}]);
  assert.equal(session.status, 'ready');
  assert.equal(factory.transports.length, 1);
});

test('metadata bursts and overflow restart once, replay the current virtual text, and root transitions discard pending events', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'session-structure-'));
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), root, factory);
  t.after(async () => { await session.close(); await rmdir(root); });
  const uri = (name: string) => pathToFileURL(path.join(root, name)).href;
  await Promise.all([
    session.filesChanged([{uri: uri('Old.xml'), type: 3}]),
    session.filesChanged([{uri: uri('New.xml'), type: 1}]),
    session.update(snapshot(8, 'ПоследнийТекст')),
  ]);
  assert.equal(factory.transports.length, 2);
  assert.equal(factory.transports[0].closed, 1);
  assert.equal(factory.transports[1].messages.find(m => m.method === 'textDocument/didOpen')?.params.textDocument.text, 'ПоследнийТекст\n\nВызов()');
  await session.filesChanged(Array.from({length: 4097}, (_, i) => ({uri: uri(`${i}.bsl`), type: 2})));
  assert.equal(factory.transports.length, 3);
  const pending = session.filesChanged([{uri: uri('New.xml'), type: 3}]);
  await session.setRoot(undefined); await pending;
  assert.equal(factory.transports.length, 4);
  assert.equal(factory.transports[3].messages.some(m => m.method === 'workspace/didChangeWatchedFiles'), false);
});

test('events arriving behind a blocked lifecycle coalesce and survive a structural restart', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'session-backlog-'));
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), undefined, factory);
  t.after(async () => { await session.close(); await rmdir(root); });
  const gate = deferred<void>();
  factory.transports[0].closeGate = gate.promise;
  const changingRoot = session.setRoot(root);
  const first = session.filesChanged([{uri: pathToFileURL(path.join(root, 'New.xml')).href, type: 1}]);
  await delay(200);
  const secondUri = pathToFileURL(path.join(root, 'Module.bsl')).href;
  const second = session.filesChanged([{uri: secondUri, type: 2}]);
  await delay(200);
  const revision = session.projectRevision;
  gate.resolve(); await Promise.all([changingRoot, first, second]);
  assert.equal(revision, 1, 'blocked flush must share one bounded pending batch');
  assert.equal(factory.transports.length, 3);
  assert.equal(session.status, 'ready');
});

test('failure during source debounce settles the obsolete batch and permits later structural recovery', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'session-failed-debounce-'));
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), root, factory);
  t.after(async () => { await session.close(); await rmdir(root); });
  const obsolete = session.filesChanged([{uri: pathToFileURL(path.join(root, 'Old.bsl')).href, type: 2}]);
  let settled = false;
  void obsolete.then(() => { settled = true; });
  factory.transports[0].fail();
  await nextTurn();
  const settledBeforeTimer = settled;
  await obsolete;
  await session.filesChanged([{uri: pathToFileURL(path.join(root, 'Fresh.xml')).href, type: 1}]);
  assert.equal(session.status, 'ready', 'later structural changes must recover after a failed pending batch');
  assert.equal(session.projectRevision, 2);
  assert.equal(factory.transports.length, 2);
  assert.equal(settledBeforeTimer, true, 'failure must settle pending debounce without waiting for its timer');
  assert.equal(factory.transports[0].messages.some(m => m.method === 'workspace/didChangeWatchedFiles'), false);
});

test('failure behind blocked lifecycle settles old events without erasing the newer recovery batch', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'session-failed-queued-'));
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), root, factory);
  const gate = deferred<void>();
  t.after(async () => { gate.resolve(); await session.close(); await rmdir(root); });
  const old = factory.transports[0];
  old.notifyGate = {method: 'textDocument/didChange', promise: gate.promise};
  const updating = session.update(snapshot(8, 'ПослеИзменения'));
  await nextTurn();
  const obsolete = session.filesChanged([{uri: pathToFileURL(path.join(root, 'Old.bsl')).href, type: 2}]);
  let settled = false;
  void obsolete.then(() => { settled = true; });
  await delay(200); // Its debounce has queued the drain behind the blocked write.
  old.fail();
  await nextTurn();
  const settledWhileBlocked = settled;
  const recovery = session.filesChanged([{uri: pathToFileURL(path.join(root, 'Fresh.xml')).href, type: 1}]);
  // A real failed transport rejects outstanding writes. Its late failure handler
  // and the stale queued drain must not clear the newer generation's batch.
  gate.reject(new Error('transport write failed'));
  await Promise.all([updating, obsolete, recovery]);
  assert.equal(session.status, 'ready', 'stale queued flush must leave the new recovery batch intact');
  assert.equal(settledWhileBlocked, true);
  assert.equal(session.projectRevision, 2);
  assert.equal(factory.transports.length, 2);
  assert.equal(factory.transports[1].messages.find(m => m.method === 'textDocument/didOpen')?.params.textDocument.text, 'ПослеИзменения\n\nВызов()');
  assert.equal(old.messages.some(m => m.method === 'workspace/didChangeWatchedFiles'), false);
});

test('events received during a successful source drain survive in the following batch', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'session-successful-drain-'));
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), root, factory);
  const gate = deferred<void>();
  t.after(async () => { gate.resolve(); await session.close(); await rmdir(root); });
  const server = factory.transports[0];
  server.notifyGate = {method: 'workspace/didChangeWatchedFiles', promise: gate.promise};
  const firstUri = pathToFileURL(path.join(root, 'First.bsl')).href;
  const secondUri = pathToFileURL(path.join(root, 'Second.bsl')).href;
  const first = session.filesChanged([{uri: firstUri, type: 2}]);
  await delay(200);
  assert.equal(server.messages.filter(m => m.method === 'workspace/didChangeWatchedFiles').length, 1);
  const second = session.filesChanged([{uri: secondUri, type: 2}]);
  await delay(200);
  gate.resolve();
  await Promise.all([first, second]);
  assert.deepEqual(server.messages.filter(m => m.method === 'workspace/didChangeWatchedFiles').map(m => m.params), [
    {changes: [{uri: firstUri, type: 2}]}, {changes: [{uri: secondUri, type: 2}]},
  ]);
  assert.equal(session.projectRevision, 2);
  assert.equal(session.status, 'ready');
  assert.equal(factory.transports.length, 1);
});

test('rootless initialization owns an empty workspace and never writes the virtual BSL file', async t => {
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), undefined, factory);
  t.after(() => session.close());
  const server = factory.transports[0];
  assert.deepEqual(server.messages.map(m => m.method), ['initialize', 'initialized', 'textDocument/didOpen']);
  const init = server.messages[0].params;
  const workspace = fileURLToPath(init.rootUri);
  assert.equal(path.dirname(workspace), os.tmpdir());
  assert.deepEqual(await readdir(workspace), []);
  assert.equal(init.workspaceFolders[0].uri, init.rootUri);
  assert.equal(init.capabilities.general.positionEncodings[0], 'utf-16');
  const opened = server.messages[2].params.textDocument;
  assert.equal(opened.text, 'А😀Б\n\nВызов()');
  assert.equal(opened.languageId, 'bsl');
  assert.equal(path.dirname(fileURLToPath(opened.uri)), workspace);
  assert.equal(path.extname(fileURLToPath(opened.uri)), '.bsl');
  await assert.rejects(stat(fileURLToPath(opened.uri)), {code: 'ENOENT'});
  assert.deepEqual(session.epoch, {notebookVersion: 7, rootKey: '', processId: 100, projectRevision: 0});
  assert.equal(session.status, 'ready');
  await session.close();
  await assert.rejects(stat(workspace), {code: 'ENOENT'});
  assert.equal(server.messages.at(-1)?.method, 'textDocument/didClose');
  assert.equal(server.closed, 1);
});

test('selected root initializes its file URI without changing any project bytes', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'session-project-'));
  // No recursive cleanup is needed: only this known fixture file is written.
  t.after(async () => { await rm(path.join(root, 'Module.bsl')); await rmdir(root); });
  await writeFile(path.join(root, 'Module.bsl'), 'Процедура Проба()\nКонецПроцедуры');
  const before = await readFile(path.join(root, 'Module.bsl'));
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), root, factory);
  t.after(() => session.close());
  assert.equal(factory.transports[0].messages[0].params.rootUri, pathToFileURL(root).href);
  assert.equal(session.epoch.rootKey, pathToFileURL(root).href);
  assert.equal(path.dirname(fileURLToPath(session.virtualUri)), root);
  const uri = session.virtualUri;
  await session.update(snapshot(8, 'Другое'));
  assert.equal(session.virtualUri, uri);
  await session.close();
  assert.deepEqual(await readdir(root), ['Module.bsl']);
  assert.deepEqual(await readFile(path.join(root, 'Module.bsl')), before);
});

test('coalesced edits send the latest full text with increasing document versions', async t => {
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), undefined, factory);
  t.after(() => session.close());
  const updates = [session.update(snapshot(8, 'Первое')), session.update(snapshot(9, 'Второе'))];
  assert.equal(session.epoch.notebookVersion, 9);
  assert.equal(session.status, 'updating');
  await Promise.all(updates);
  await session.update(snapshot(10, 'Третье'));
  const documents = factory.transports[0].messages.filter(m => /didOpen|didChange/.test(m.method));
  assert.equal(documents.length, 3);
  assert.deepEqual(documents[1].params.contentChanges, [{text: 'Второе\n\nВызов()'}]);
  assert.deepEqual(documents[2].params.contentChanges, [{text: 'Третье\n\nВызов()'}]);
  assert.ok(documents[0].params.textDocument.version < documents[1].params.textDocument.version);
  assert.ok(documents[1].params.textDocument.version < documents[2].params.textDocument.version);
  assert.equal(session.status, 'ready');
});

test('query maps UTF-16 body positions, forwards cancellation, and ignores magic or other cells', async t => {
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), undefined, factory);
  t.after(() => session.close());
  const server = factory.transports[0];
  assert.equal(await session.query('textDocument/hover', 'cell:a', {line: 0, character: 0}), undefined);
  assert.equal(await session.query('textDocument/hover', 'cell:p', {line: 0, character: 0}), undefined);
  assert.equal(server.pending.length, 0);
  const abort = new AbortController();
  const result = session.query('textDocument/hover', 'cell:a', position, abort.signal);
  await nextTurn();
  assert.deepEqual(server.messages.at(-1)?.params, {textDocument: {uri: session.virtualUri}, position: {line: 0, character: 3}});
  const forwarded = server.messages.at(-1)?.signal;
  assert.equal(forwarded?.aborted, false);
  server.pending[0].resolve({contents: 'Текст'});
  assert.deepEqual(await result, {contents: 'Текст'});
  const cancelled = session.query('textDocument/hover', 'cell:a', position, abort.signal);
  await nextTurn(); abort.abort();
  assert.equal(server.messages.at(-1)?.signal?.aborted, true);
  server.pending[1].resolve({contents: 'cancelled'});
  assert.equal(await cancelled, undefined);
});

test('two notebooks own separate children and rootless workspaces', async t => {
  const factory = new FakeFactory();
  const a = await NotebookLspSession.open('file:///a.ipynb', snapshot(), undefined, factory);
  const b = await NotebookLspSession.open('file:///b.ipynb', snapshot(), undefined, factory);
  t.after(async () => { await a.close(); await b.close(); });
  assert.notEqual(a.epoch.processId, b.epoch.processId);
  assert.notEqual(path.dirname(fileURLToPath(a.virtualUri)), path.dirname(fileURLToPath(b.virtualUri)));
  await a.close();
  assert.equal(factory.transports[1].closed, 0);
  assert.equal(b.status, 'ready');
});

for (const transition of ['edit', 'root', 'close', 'failure'] as const) {
  test(`late query and diagnostics are discarded immediately after ${transition}`, async t => {
    const factory = new FakeFactory();
    const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), undefined, factory);
    t.after(() => session.close());
    const server = factory.transports[0];
    const initial = session.diagnose();
    await nextTurn();
    server.pending[0].resolve({kind: 'full', items: [diagnostic]});
    await initial;
    assert.deepEqual(session.diagnostics, [diagnostic]);
    const published: unknown[] = [];
    const subscription = session.onDiagnostics(items => published.push(items));
    t.after(() => subscription.dispose());
    const query = session.query('textDocument/completion', 'cell:a', position);
    const diagnostics = session.diagnose();
    await nextTurn();
    let work: Promise<void> | undefined;
    if (transition === 'edit') work = session.update(snapshot(8, 'Новое'));
    if (transition === 'root') work = session.setRoot(os.tmpdir());
    if (transition === 'close') work = session.close();
    if (transition === 'failure') server.fail();
    assert.deepEqual(session.diagnostics, []);
    assert.deepEqual(published.at(-1), []);
    server.pending[1].resolve([{label: 'Старое'}]);
    server.pending[2].resolve({kind: 'full', items: [diagnostic]});
    assert.equal(await query, undefined);
    assert.equal(await diagnostics, undefined);
    await work;
    assert.deepEqual(session.diagnostics, []);
    if (transition === 'close' || transition === 'failure') assert.equal(session.status, 'unavailable');
  });
}

test('root transitions close the old child before starting a new one and replay the newest snapshot', async t => {
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), undefined, factory);
  t.after(() => session.close());
  const old = factory.transports[0];
  const closing = deferred<void>();
  old.closeGate = closing.promise;
  const change = session.setRoot(os.tmpdir());
  assert.equal(session.status, 'indexing');
  assert.equal(session.epoch.processId, 0);
  const update = session.update(snapshot(8, 'Последнее'));
  await nextTurn();
  assert.equal(factory.transports.length, 1);
  assert.equal(old.messages.at(-1)?.method, 'textDocument/didClose');
  closing.resolve();
  await Promise.all([change, update]);
  assert.equal(factory.transports[1].messages.find(m => m.method === 'textDocument/didOpen')?.params.textDocument.text, 'Последнее\n\nВызов()');
  assert.equal(session.epoch.processId, 101);
  await session.setRoot(undefined);
  assert.equal(session.epoch.rootKey, '');
  assert.deepEqual(await readdir(path.dirname(fileURLToPath(session.virtualUri))), []);
});

test('close during a delayed replacement start reaps the arriving child without opening a document', async t => {
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), undefined, factory);
  t.after(() => session.close());
  const starting = deferred<void>();
  const entered = deferred<void>();
  factory.startGate = starting.promise;
  factory.onStart = () => entered.resolve();
  const change = session.setRoot(os.tmpdir());
  await entered.promise;
  const closing = session.close();
  starting.resolve();
  await Promise.all([change, closing]);
  assert.equal(factory.transports.length, 2);
  assert.equal(factory.transports[1].closed, 1);
  assert.deepEqual(factory.transports[1].messages, []);
  assert.equal(session.status, 'unavailable');
});

test('missing executable becomes unavailable and root retry can recover', async t => {
  const factory = new FakeFactory();
  let missing = true;
  const retryFactory: TransportFactory = {start: root => missing ? Promise.reject(new Error('secret executable path')) : factory.start(root)};
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), undefined, retryFactory);
  t.after(() => session.close());
  assert.equal(session.status, 'unavailable');
  assert.ok(session.reason);
  assert.ok(!session.reason.includes('secret'));
  assert.equal(await session.query('textDocument/hover', 'cell:a', position), undefined);
  missing = false;
  await session.setRoot(undefined);
  assert.equal(session.status, 'ready');
});

test('failed child cleanup retains ownership and prevents a replacement from starting', async t => {
  const factory = new FakeFactory();
  const session = await NotebookLspSession.open('file:///one.ipynb', snapshot(), undefined, factory);
  const old = factory.transports[0];
  const workspace = path.dirname(fileURLToPath(session.virtualUri));
  t.after(async () => {
    await session.close().catch(() => {});
    // A fake failed child owns no process. Remove only its empty test workspace.
    await rmdir(workspace).catch(error => { if (error.code !== 'ENOENT') throw error; });
  });
  old.closeError = new Error('secret child cleanup details');
  await assert.doesNotReject(() => session.setRoot(os.tmpdir()));
  assert.equal(session.status, 'unavailable');
  assert.ok(!session.reason?.includes('secret'));
  await session.setRoot(undefined);
  assert.equal(factory.transports.length, 1);
  old.closeError = undefined;
  await session.setRoot(undefined);
  assert.equal(factory.transports.length, 1);
  assert.equal(session.status, 'unavailable');
  assert.equal(old.closed, 1);
  await assert.rejects(session.close(), /secret child cleanup details/);
});
