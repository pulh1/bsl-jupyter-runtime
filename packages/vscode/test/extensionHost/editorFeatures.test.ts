import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {mkdtemp, writeFile, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import * as vscode from 'vscode';
import {NotebookSnapshot, Range} from '../../src/notebookModel';
import {NotebookLspSession} from '../../src/notebookSession';
import {BslTransport} from '../../src/lspTransport';

const range = (line: number, start = 0, end = start): Range => ({start: {line, character: start}, end: {line, character: end}});
class Server {
  pid = 700;
  replies = new Map<string, unknown>();
  requests: string[] = [];
  positions: {method: string; position?: {line: number; character: number}}[] = [];
  async request<T>(method: string, params: {position?: {line: number; character: number}}): Promise<T> {
    this.requests.push(method);
    this.positions.push({method, position: params.position});
    return (method === 'initialize' ? {} : await this.replies.get(method)) as T;
  }
  async notify(): Promise<void> {}
  onFailure(): {dispose(): void} { return {dispose() {}}; }
  async close(): Promise<void> {}
}

export async function run(): Promise<void> {
  assert.ok(existsSync(path.join(__dirname, '../../src/editorFeatures.js')), 'BSL notebook editor providers must be implemented');
  const {registerBslFeatures, createBslProviders} = require('../../src/editorFeatures');
  const fixture = await mkdtemp(path.join(os.tmpdir(), 'bsl-features-host-'));
  const contents = ['%%bsl\nА😀Б', 'print(1)', '%%bsl\nМетод(Арг)\nКонтекст.Таблица[0].'];
  const uri = vscode.Uri.file(path.join(fixture, 'features.ipynb'));
  await writeFile(uri.fsPath, JSON.stringify({cells: contents.map(source => ({cell_type: 'code', execution_count: null, outputs: [], metadata: {}, source})), metadata: {language_info: {name: 'python'}, kernelspec: {name: 'python3', display_name: 'Python 3', language: 'python'}}, nbformat: 4, nbformat_minor: 5}));
  const notebook = await vscode.workspace.openNotebookDocument(uri);
  await vscode.window.showNotebookDocument(notebook);
  const cells = notebook.getCells();
  const inputs = cells.map(cell => ({uri: cell.document.uri.toString(), kind: 'code' as const, languageId: cell.document.languageId, text: cell.document.getText()}));
  let snapshot = NotebookSnapshot.fromCells(inputs, 1);
  const server = new Server();
  const session = await NotebookLspSession.open(uri.toString(), snapshot, fixture, {start: async () => server as unknown as BslTransport});
  const getSession = (key: string) => key === uri.toString() ? session : undefined;
  const getSnapshot = (key: string) => key === uri.toString() ? snapshot : undefined;
  const context = {subscriptions: [] as vscode.Disposable[]};
  const registered = registerBslFeatures(context, getSession, getSnapshot);
  const providers = createBslProviders(getSession, getSnapshot);
  const cancel = new vscode.CancellationTokenSource();
  const position = new vscode.Position(1, 3);
  const complete = (doc = cells[0].document, pos = position) => providers.completion.provideCompletionItems(doc, pos, cancel.token, {triggerKind: vscode.CompletionTriggerKind.Invoke});
  try {
    server.replies.set('textDocument/completion', {isIncomplete: true, items: [{label: '__BSL_host_probe', kind: 3,
      textEdit: {range: range(0, 1, 4), newText: 'Замена'}, keepWhitespace: true,
      command: {title: 'After insert', command: 'onec-bsl.test-after-insert'}}]});
    let demandSession: typeof session | undefined;
    let starts = 0;
    const demand = createBslProviders(() => demandSession, getSnapshot, async () => {starts++; demandSession = session;});
    const demandComplete = (doc: vscode.TextDocument, pos: vscode.Position) =>
      demand.completion.provideCompletionItems(doc, pos, cancel.token, {triggerKind: vscode.CompletionTriggerKind.Invoke});
    assert.equal(await demandComplete(cells[1].document, new vscode.Position(0, 2)), undefined);
    assert.equal(await demandComplete(cells[2].document, new vscode.Position(2, 20)), undefined);
    assert.equal(starts, 0, 'non-BSL and kernel context requests must not start the shared server');
    assert.equal((await demandComplete(cells[0].document, position))?.items[0]?.label, '__BSL_host_probe');
    assert.equal(starts, 1, 'first BSL request must await the shared server');
    assert.equal((await complete())?.items[0]?.label, '__BSL_host_probe', 'direct provider should map the BSL completion');
    const completion = await vscode.commands.executeCommand<vscode.CompletionList>('vscode.executeCompletionItemProvider', cells[0].document.uri, position);
    const item = completion?.items.find(item => item.label === '__BSL_host_probe');
    assert.ok(item, `registered provider should return static BSL completion; requests=${server.requests.join(',')}; language=${cells[0].document.languageId}; notebookType=${notebook.notebookType}`);
    assert.deepEqual(item.range, new vscode.Range(1, 1, 1, 4));
    assert.equal(item.insertText, 'Замена');
    assert.equal(item.kind, vscode.CompletionItemKind.Function);
    assert.equal(item.keepWhitespace, true);
    assert.equal(item.command?.command, 'onec-bsl.test-after-insert');
    assert.equal((await complete()).isIncomplete, true);
    const count = server.requests.filter(method => method === 'textDocument/completion').length;
    assert.equal(await complete(cells[1].document, new vscode.Position(0, 2)), undefined);
    assert.equal(await complete(cells[0].document, new vscode.Position(0, 3)), undefined);
    assert.equal(await complete(cells[2].document, new vscode.Position(2, 20)), undefined);
    const plain = await vscode.workspace.openTextDocument({language: 'bsl', content: 'А😀Б'});
    assert.equal(await complete(plain, new vscode.Position(0, 3)), undefined);
    assert.equal(server.requests.filter(method => method === 'textDocument/completion').length, count, 'unsupported positions must never query static completion');

    server.replies.set('textDocument/signatureHelp', {signatures: [{label: 'Метод(Арг)', parameters: [{label: [6, 9], documentation: 'Параметр'}]}], activeSignature: 0, activeParameter: 0});
    const signature = await vscode.commands.executeCommand<vscode.SignatureHelp>('vscode.executeSignatureHelpProvider', cells[2].document.uri, new vscode.Position(1, 7));
    assert.equal(signature?.signatures[0].label, 'Метод(Арг)');
    assert.deepEqual(signature?.signatures[0].parameters[0].label, [6, 9]);
    assert.deepEqual(server.positions.find(entry => entry.method === 'textDocument/signatureHelp')?.position, {line: 2, character: 7});
    server.replies.set('textDocument/hover', {contents: {kind: 'markdown', value: '**BSL hover**'}, range: range(2, 0, 5)});
    const hovers = await vscode.commands.executeCommand<vscode.Hover[]>('vscode.executeHoverProvider', cells[2].document.uri, new vscode.Position(1, 2));
    assert.ok(hovers?.some(hover => hover.range?.isEqual(new vscode.Range(1, 0, 1, 5))));

    server.replies.set('textDocument/definition', [{uri: session.virtualUri, range: range(0, 1, 3)}]);
    const definitions = await vscode.commands.executeCommand<vscode.Location[]>('vscode.executeDefinitionProvider', cells[2].document.uri, new vscode.Position(1, 2));
    assert.ok(definitions?.some(target => target.uri.toString() === cells[0].document.uri.toString() && target.range.isEqual(new vscode.Range(1, 1, 1, 3))));
    const source = vscode.Uri.file(path.join(fixture, 'Real.bsl'));
    await writeFile(source.fsPath, 'Перем А;');
    server.replies.set('textDocument/definition', {uri: source.toString(), range: range(0, 0, 5)});
    const real = await providers.definition.provideDefinition(cells[2].document, new vscode.Position(1, 2), cancel.token);
    assert.equal(real[0].uri.toString(), source.toString());
    const sourceDocument = await vscode.workspace.openTextDocument(real[0].uri);
    assert.equal(sourceDocument.getText(), 'Перем А;');
    assert.equal(await complete(sourceDocument, new vscode.Position(0, 3)), undefined, 'ordinary .bsl source files are not notebook-provider inputs');

    server.replies.set('textDocument/diagnostic', {kind: 'full', items: [{range: range(0, 1, 3), message: '__BSL diagnostic', severity: 2}, {range: range(1), message: 'separator'}]});
    registered.refreshDiagnostics(uri.toString());
    await eventually(() => vscode.languages.getDiagnostics(cells[0].document.uri).some(d => d.message === '__BSL diagnostic'));
    assert.equal(vscode.languages.getDiagnostics(cells[0].document.uri).find(d => d.message === '__BSL diagnostic')?.severity, vscode.DiagnosticSeverity.Warning);
    assert.equal(vscode.languages.getDiagnostics().flatMap(([, items]) => items).some(d => d.message === 'separator'), false);
    let resolveDiagnostic!: (reply: unknown) => void;
    server.replies.set('textDocument/diagnostic', new Promise(yes => {resolveDiagnostic = yes;}));
    const lateDiagnostic = session.diagnose();
    let resolve!: (reply: unknown) => void;
    server.replies.set('textDocument/completion', new Promise(yes => {resolve = yes;}));
    const late = complete(); await delay(10);
    snapshot = NotebookSnapshot.fromCells([inputs[2], inputs[1], inputs[0]], 2);
    const updating = session.update(snapshot);
    assert.equal(vscode.languages.getDiagnostics(cells[0].document.uri).some(d => d.message === '__BSL diagnostic'), false);
    resolve({items: [{label: 'stale', textEdit: {range: range(0, 1, 4), newText: 'bad'}}]});
    await updating;
    assert.equal(await late, undefined, 'reordered responses must be discarded');
    resolveDiagnostic({kind: 'full', items: [{range: range(0, 1, 3), message: '__BSL stale'}]});
    await lateDiagnostic;
    assert.equal(vscode.languages.getDiagnostics().flatMap(([, items]) => items).some(d => d.message === '__BSL stale'), false);
    server.replies.set('textDocument/completion', [{label: 'reordered', textEdit: {range: range(3, 1, 4), newText: 'После'}}]);
    assert.deepEqual((await complete()).items[0].range, new vscode.Range(1, 1, 1, 4));
    server.replies.set('textDocument/diagnostic', {kind: 'full', items: [{range: range(3, 1, 3), message: '__BSL diagnostic'}]});
    registered.refreshDiagnostics(uri.toString());
    await eventually(() => vscode.languages.getDiagnostics(cells[0].document.uri).some(d => d.message === '__BSL diagnostic'));
    await session.setRoot(undefined);
    assert.equal(vscode.languages.getDiagnostics(cells[0].document.uri).some(d => d.message === '__BSL diagnostic'), false);
    registered.refreshDiagnostics(uri.toString());
    await eventually(() => vscode.languages.getDiagnostics(cells[0].document.uri).some(d => d.message === '__BSL diagnostic'));
    await session.close();
    assert.equal(vscode.languages.getDiagnostics(cells[0].document.uri).some(d => d.message === '__BSL diagnostic'), false);
    registered.clearDiagnostics(uri.toString());
    assert.equal(cells[0].document.getText(), contents[0]);
    assert.equal(cells[0].document.languageId, 'python');
    console.log('Task 7 editor feature host assertions passed (kernel/Pylance coexistence unverified).');
  } finally {
    registered.dispose(); cancel.dispose();
    for (const disposable of context.subscriptions) disposable.dispose();
    await session.close();
    await rm(fixture, {recursive: true, force: true});
  }
  await delayedSynchronizationDiagnostics();
}

async function delayedSynchronizationDiagnostics(): Promise<void> {
  const {registerBslFeatures} = require('../../src/editorFeatures') as typeof import('../../src/editorFeatures');
  const fixture = await mkdtemp(path.join(os.tmpdir(), 'bsl-delayed-sync-'));
  const uri = vscode.Uri.file(path.join(fixture, 'delayed.ipynb'));
  await writeFile(uri.fsPath, JSON.stringify({cells: [{cell_type: 'code', execution_count: null, outputs: [], metadata: {}, source: '%%bsl\nЗначение'}], metadata: {language_info: {name: 'python'}}, nbformat: 4, nbformat_minor: 5}));
  const notebook = await vscode.workspace.openNotebookDocument(uri);
  const original = notebook.cellAt(0).document;
  const makeSnapshot = (version: number) => NotebookSnapshot.fromCells(notebook.getCells().map(cell => ({uri: cell.document.uri.toString(), kind: 'code' as const, languageId: cell.document.languageId, text: cell.document.getText()})), version);
  let snapshot = makeSnapshot(1);
  const server = new Server();
  const session = await NotebookLspSession.open(uri.toString(), snapshot, fixture, {start: async () => server as unknown as BslTransport});
  const registration = registerBslFeatures({subscriptions: []}, key => key === uri.toString() ? session : undefined, key => key === uri.toString() ? snapshot : undefined);
  const has = (message: string) => vscode.languages.getDiagnostics(original.uri).some(item => item.message === message);
  const report = (message: string) => ({kind: 'full', items: [{range: range(snapshot.toVirtual(original.uri.toString(), {line: 1, character: 0})!.line, 0, 1), message}]});
  try {
    for (const change of ['text', 'notebook'] as const) {
      server.replies.set('textDocument/diagnostic', report('__BSL before edit'));
      registration.refreshDiagnostics(uri.toString());
      await eventually(() => has('__BSL before edit'));
      let resolve!: (reply: unknown) => void;
      server.replies.set('textDocument/diagnostic', new Promise(yes => {resolve = yes;}));
      const beforePull = server.requests.filter(method => method === 'textDocument/diagnostic').length;
      const pending = session.diagnose();
      await eventually(() => server.requests.filter(method => method === 'textDocument/diagnostic').length > beforePull);
      const oldSnapshot = snapshot, oldDocumentVersion = original.version, oldNotebookVersion = notebook.version;
      const edit = new vscode.WorkspaceEdit();
      if (change === 'text') edit.insert(original.uri, new vscode.Position(1, 0), 'Новое');
      else edit.set(uri, [vscode.NotebookEdit.insertCells(0, [new vscode.NotebookCellData(vscode.NotebookCellKind.Code, '%%bsl\nВставлено', 'python')])]);
      assert.equal(await vscode.workspace.applyEdit(edit), true);
      await eventually(() => change === 'text' ? original.version > oldDocumentVersion : notebook.version > oldNotebookVersion);
      // Intentionally hold the coordinator's update until the old LS response arrives.
      assert.equal(session.snapshot, oldSnapshot);
      assert.equal(snapshot, oldSnapshot);
      assert.equal(has('__BSL before edit'), false, `${change}: clear existing diagnostics on the raw event`);
      resolve(report('__BSL stale before synchronization'));
      await pending;
      assert.equal(has('__BSL stale before synchronization'), false, `${change}: raw editor changes must detach pre-edit diagnostics before session synchronization`);
      const afterPull = server.requests.filter(method => method === 'textDocument/diagnostic').length;
      await delay(200);
      assert.equal(server.requests.filter(method => method === 'textDocument/diagnostic').length, afterPull, `${change}: do not pull from the old snapshot during delayed synchronization`);
      snapshot = makeSnapshot(snapshot.version + 1);
      await session.update(snapshot);
      server.replies.set('textDocument/diagnostic', report('__BSL after synchronization'));
      registration.refreshDiagnostics(uri.toString());
      await eventually(() => has('__BSL after synchronization'));
    }
    console.log('Task 7 delayed text/notebook synchronization diagnostic regressions passed.');
  } finally {
    registration.dispose();
    await session.close();
    await rm(fixture, {recursive: true, force: true});
  }
}
async function eventually(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt++) { if (predicate()) return; await delay(25); }
  assert.ok(predicate(), 'expected current mapped diagnostics');
}
