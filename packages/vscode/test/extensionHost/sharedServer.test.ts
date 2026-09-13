import assert from 'node:assert/strict';
import {mkdtemp, mkdir, readFile, realpath, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import * as vscode from 'vscode';
import * as extension from '../../src/extension';
import {NotebookSnapshot} from '../../src/notebookModel';

export async function run(): Promise<void> {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bsl-shared-host-'));
  const notebookUri = vscode.Uri.file(path.join(directory, 'shared.ipynb'));
  const body = '%%bsl\nЗапрос = Новый Запрос;\nЗапрос.';
  const moduleBody = '%%bsl\nРезультат = ProbeServer.';
  try {
    await writeFile(notebookUri.fsPath, JSON.stringify({nbformat: 4, nbformat_minor: 5,
      metadata: {language_info: {name: 'python'}}, cells: [body, moduleBody].map(source =>
        ({cell_type: 'code', execution_count: null, metadata: {}, outputs: [], source}))}));
    const notebook = await vscode.workspace.openNotebookDocument(notebookUri);
    await vscode.window.showNotebookDocument(notebook);
    const key = notebookUri.toString();
    const owner = extension.getNotebookCoordinator();
    assert.ok(owner);
    assert.equal(owner.sessionFor(key), undefined, 'notebook opening must not start the shared server in on-demand mode');
    await vscode.commands.executeCommand<vscode.CompletionList>(
      'vscode.executeCompletionItemProvider', notebook.cellAt(0).document.uri, new vscode.Position(2, 7));
    let deadline = Date.now() + 60_000;
    while (!owner.sessionFor(key) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 50));
    const session = owner.sessionFor(key);
    assert.ok(session, 'notebook must acquire a BSL session');
    assert.equal(vscode.Uri.parse(session.virtualUri).scheme, 'file', 'shared server queries must use a clean temporary file');
    assert.equal(path.relative(os.tmpdir(), path.dirname(vscode.Uri.parse(session.virtualUri).fsPath)).startsWith('..'), false);
    let found = false;
    deadline = Date.now() + 20_000;
    while (!found && Date.now() < deadline) {
      const reply = await vscode.commands.executeCommand<vscode.CompletionList>(
        'vscode.executeCompletionItemProvider', notebook.cellAt(0).document.uri, new vscode.Position(2, 7));
      found = !!reply?.items.some(item => (typeof item.label === 'string' ? item.label : item.label.label) === 'Выполнить');
      if (!found) await new Promise(resolve => setTimeout(resolve, 250));
    }
    assert.ok(found, 'notebook cell must receive Query methods from the installed BSL extension');
    found = false;
    const sourceRoot = vscode.Uri.file(path.join(vscode.workspace.workspaceFolders![0].uri.fsPath, 'sources'));
    let acceptedRoot = '';
    await extension.selectSourceRootForNotebook(notebook, {set(_uri, root) {acceptedRoot = root;}},
      async () => [sourceRoot]);
    assert.equal(acceptedRoot, await realpath(sourceRoot.fsPath), 'source root below the opened parent folder must be accepted');
    deadline = Date.now() + 20_000;
    while (!found && Date.now() < deadline) {
      const reply = await vscode.commands.executeCommand<vscode.CompletionList>(
        'vscode.executeCompletionItemProvider', notebook.cellAt(1).document.uri, new vscode.Position(1, 24));
      found = !!reply?.items.some(item => (typeof item.label === 'string' ? item.label : item.label.label) === 'ДисковыйВызов');
      if (!found) await new Promise(resolve => setTimeout(resolve, 250));
    }
    assert.ok(found, 'shared LS must complete a saved common module below the opened parent folder');
    assert.equal(vscode.window.activeNotebookEditor?.notebook.uri.toString(), key,
      'the temporary BSL file must not steal focus from the notebook');
    assert.equal(vscode.workspace.textDocuments.some(document => document.uri.toString() === session.virtualUri), false,
      'querying the temporary file must not keep a text document open');
    assert.equal(vscode.window.tabGroups.all.flatMap(group => group.tabs)
      .some(tab => tab.input instanceof vscode.TabInputText && tab.input.uri.toString() === session.virtualUri), false,
      'querying the temporary file must not create an editor tab');
    const synthetic = vscode.languages.createDiagnosticCollection('shared-bsl-stale-test');
    const publications: string[][] = [];
    const diagnosticSubscription = session.onDiagnostics(items => publications.push(items.map(item => item.message)));
    synthetic.set(vscode.Uri.parse(session.virtualUri), [
      new vscode.Diagnostic(new vscode.Range(0, 0, 0, 1), '__stale-before-edit'),
    ]);
    await session.diagnose();
    assert.equal(publications.flat().includes('__stale-before-edit'), false,
      'unversioned upstream diagnostics must not be mirrored into notebook cells');
    const edit = new vscode.WorkspaceEdit();
    edit.insert(notebook.cellAt(0).document.uri, new vscode.Position(2, 7), 'Т');
    assert.equal(await vscode.workspace.applyEdit(edit), true);
    const syncDeadline = Date.now() + 10_000;
    while (owner.sessionFor(key)?.snapshot.text.includes('Запрос.Т') !== true && Date.now() < syncDeadline)
      await new Promise(resolve => setTimeout(resolve, 25));
    const temporaryPath = vscode.Uri.parse(session.virtualUri).fsPath;
    while (!(await readFile(temporaryPath, 'utf8')).includes('Запрос.Т') && Date.now() < syncDeadline)
      await new Promise(resolve => setTimeout(resolve, 25));
    assert.ok((await readFile(temporaryPath, 'utf8')).includes('Запрос.Т'), 'editing a cell must update the temporary BSL file');
    await session.diagnose();
    assert.equal(publications.flat().includes('__stale-before-edit'), false,
      'diagnostics from the previous document version must stay cleared after an edit');
    diagnosticSubscription.dispose(); synthetic.dispose();
    let entered!: () => void, release!: () => void;
    const providerEntered = new Promise<void>(resolve => {entered = resolve;});
    const providerRelease = new Promise<void>(resolve => {release = resolve;});
    let block = true, observedText = '', providerRequests = 0;
    const delayedProvider = vscode.languages.registerCompletionItemProvider({scheme: 'file', language: 'bsl'}, {
      async provideCompletionItems(document) {
        if (document.uri.toString() !== session.virtualUri) return [];
        ++providerRequests;
        observedText = document.getText();
        if (block) {entered(); await providerRelease; block = false;}
        return [];
      },
    });
    try {
      const oldText = await readFile(temporaryPath, 'utf8');
      const oldQuery = session.query('textDocument/completion', notebook.cellAt(0).document.uri.toString(), {line: 2, character: 7});
      let timeout!: NodeJS.Timeout;
      const deadline = new Promise<never>((_, reject) => {
        timeout = setTimeout(() => reject(new Error('Delayed completion provider was not reached')), 10_000);
      });
      try {await Promise.race([providerEntered, deadline]);} finally {clearTimeout(timeout);}
      const changed = NotebookSnapshot.fromCells(notebook.getCells().map((cell, index) => ({
        uri: cell.document.uri.toString(), kind: 'code' as const, languageId: cell.document.languageId,
        text: cell.document.getText() + (index === 0 ? '\nНоваяПеременная = 1;' : ''),
      })), session.snapshot.version + 1);
      const pendingUpdate = session.update(changed);
      const cancellation = new AbortController();
      const cancelledQuery = session.query('textDocument/completion', notebook.cellAt(0).document.uri.toString(),
        {line: 2, character: 7}, cancellation.signal);
      cancellation.abort();
      await new Promise(resolve => setTimeout(resolve, 100));
      assert.equal(await readFile(temporaryPath, 'utf8'), oldText,
        'a snapshot write must wait for an in-flight provider request');
      release();
      assert.equal(await oldQuery, undefined, 'a provider result started before the edit must be discarded');
      await pendingUpdate;
      assert.equal(await cancelledQuery, undefined);
      await session.query('textDocument/completion', notebook.cellAt(0).document.uri.toString(), {line: 2, character: 7});
      assert.equal(observedText, changed.text, 'the next provider request must read the new snapshot');
      assert.equal(providerRequests, 2, 'a cancelled queued request must not call the shared server');
    } finally {release(); delayedProvider.dispose();}
    const outside = path.join(directory, 'sources');
    await mkdir(path.join(outside, 'CommonModules', 'Probe', 'Ext'), {recursive: true});
    for (const name of ['Configuration.xml', 'CommonModules/Probe.xml', 'CommonModules/Probe/Ext/Module.bsl'])
      await writeFile(path.join(outside, name), `fixture:${name}`);
    let selected = false, error = '';
    await extension.selectSourceRootForNotebook(notebook, {set() {selected = true;}},
      async () => [vscode.Uri.file(outside)], message => {error = message;});
    assert.equal(selected, false, 'a root outside the first workspace cannot feed the shared LS');
    assert.match(error, /open.*folder|workspace/i);
    const oldVirtualUri = session.virtualUri;
    await rm(vscode.Uri.parse(oldVirtualUri).fsPath);
    await session.query('textDocument/completion', notebook.cellAt(0).document.uri.toString(), {line: 2, character: 7});
    assert.equal(session.status, 'unavailable', 'a removed temporary file must invalidate the session');
    await owner.ensureSession(key);
    const activeSession = owner.sessionFor(key);
    assert.ok(activeSession && activeSession !== session, 'the next request must recover the missing temporary file');
    await assert.rejects(readFile(vscode.Uri.parse(oldVirtualUri).fsPath), {code: 'ENOENT'});
    const virtualUri = activeSession.virtualUri;
    assert.notEqual(virtualUri, oldVirtualUri);
    let exposedTemporaryDocument = false;
    const visibleSubscription = vscode.window.onDidChangeVisibleTextEditors(editors => {
      if (editors.some(editor => editor.document.uri.toString() === virtualUri)) exposedTemporaryDocument = true;
    });
    const activeSubscription = vscode.window.onDidChangeActiveTextEditor(editor => {
      if (editor?.document.uri.toString() === virtualUri) exposedTemporaryDocument = true;
    });
    try {await owner.close(key);} finally {visibleSubscription.dispose(); activeSubscription.dispose();}
    assert.equal(exposedTemporaryDocument, false,
      'releasing the idle BSL session must not reveal its private document');
    await assert.rejects(readFile(vscode.Uri.parse(virtualUri).fsPath), {code: 'ENOENT'});
    assert.equal(vscode.workspace.textDocuments.some(document => document.uri.toString() === virtualUri), false,
      'closing a notebook must leave no BSL text document open');
    await vscode.commands.executeCommand('workbench.action.files.revert');
    const tab = vscode.window.tabGroups.all.flatMap(group => group.tabs)
      .find(candidate => candidate.input instanceof vscode.TabInputNotebook && candidate.input.uri.toString() === key);
    assert.ok(tab);
    assert.equal(await vscode.window.tabGroups.close(tab), true);
  } finally {await rm(directory, {recursive: true, force: true});}
}
