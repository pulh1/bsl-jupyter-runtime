import * as assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import * as vscode from 'vscode';

import {getOpenNotebookRoot, selectSourceRootForNotebook} from '../../src/extension';
import {OpenNotebookRoots} from '../../src/projectRoot';

export async function run(): Promise<void> {
  const fixtureDirectory = await fs.mkdtemp(path.join(vscode.workspace.workspaceFolders![0].uri.fsPath, 'bsl-notebook-source-root-'));
  const notebookPath = path.join(fixtureDirectory, 'bsl-cell.ipynb');
  const sourceRoot = path.join(fixtureDirectory, 'designer');
  const notebookContents = JSON.stringify(notebookFixture());

  try {
    await fs.writeFile(notebookPath, notebookContents, 'utf8');
    await createDesignerFixture(sourceRoot);
    const notebookUri = vscode.Uri.file(notebookPath);
    const notebook = await openNotebookInEditor(notebookUri);
    const roots = new OpenNotebookRoots();
    roots.set(notebook.uri.toString(), 'C:/old-root');
    const rejectedMessages: string[] = [];

    await selectSourceRootForNotebook(
      notebook,
      roots,
      async () => [vscode.Uri.parse('untitled:/remote-source-root')],
      message => rejectedMessages.push(message),
    );

    assert.equal(roots.get(notebook.uri.toString()), 'C:/old-root');
    assert.equal(rejectedMessages.length, 1);
    assert.equal(notebook.isDirty, false);
    assert.equal(await fs.readFile(notebookPath, 'utf8'), notebookContents);

    await assertUnsupportedNotebookDoesNotOpenPicker(
      {uri: vscode.Uri.file(notebookPath), notebookType: 'test-notebook'} as vscode.NotebookDocument,
      roots,
      sourceRoot,
    );
    await assertUnsupportedNotebookDoesNotOpenPicker(
      {uri: vscode.Uri.parse('untitled:/bsl-cell.ipynb'), notebookType: 'jupyter-notebook'} as vscode.NotebookDocument,
      roots,
      sourceRoot,
    );

    await selectSourceRootForNotebook(
      notebook,
      roots,
      async () => [vscode.Uri.file(sourceRoot)],
      message => rejectedMessages.push(message),
    );

    assert.equal(roots.get(notebook.uri.toString()), await fs.realpath(sourceRoot));
    assert.equal(notebook.isDirty, false);
    assert.equal(await fs.readFile(notebookPath, 'utf8'), notebookContents);

    await selectSourceRootForNotebook(
      notebook,
      undefined,
      async () => [vscode.Uri.file(sourceRoot)],
    );
    assert.equal(getOpenNotebookRoot(notebook.uri.toString()), await fs.realpath(sourceRoot));
    await closeNotebookDocument(notebook);
    assert.equal(getOpenNotebookRoot(notebook.uri.toString()), undefined);

    const reopened = await openNotebookInEditor(notebookUri);
    assert.equal(getOpenNotebookRoot(reopened.uri.toString()), undefined);
  } finally {
    await fs.rm(fixtureDirectory, {recursive: true, force: true});
  }
}

async function assertUnsupportedNotebookDoesNotOpenPicker(
  notebook: vscode.NotebookDocument,
  roots: OpenNotebookRoots,
  sourceRoot: string,
): Promise<void> {
  let pickerWasCalled = false;
  const messages: string[] = [];
  const previousRoot = roots.get(notebook.uri.toString());

  await selectSourceRootForNotebook(
    notebook,
    roots,
    async () => {
      pickerWasCalled = true;
      return [vscode.Uri.file(sourceRoot)];
    },
    message => messages.push(message),
  );

  assert.equal(pickerWasCalled, false);
  assert.equal(roots.get(notebook.uri.toString()), previousRoot);
  assert.equal(messages.length, 1);
}

async function closeNotebookDocument(notebook: vscode.NotebookDocument): Promise<void> {
  const closed = new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(() => {
      subscription.dispose();
      reject(new Error('Notebook did not close within 20 seconds.'));
    }, 20_000);
    const subscription = vscode.workspace.onDidCloseNotebookDocument(closedNotebook => {
      if (closedNotebook.uri.toString() !== notebook.uri.toString()) return;
      clearTimeout(timeout);
      subscription.dispose();
      resolve();
    });
  });

  const tab = vscode.window.tabGroups.all
    .flatMap(group => group.tabs)
    .find(candidate => candidate.input instanceof vscode.TabInputNotebook &&
      candidate.input.uri.toString() === notebook.uri.toString());
  assert.ok(tab, 'expected the fixture notebook to have an editor tab');
  assert.equal(await vscode.window.tabGroups.close(tab), true);
  await closed;
  assert.equal(
    vscode.workspace.notebookDocuments.some(openNotebook =>
      openNotebook.uri.toString() === notebook.uri.toString()),
    false,
  );
}

async function openNotebookInEditor(uri: vscode.Uri): Promise<vscode.NotebookDocument> {
  const opened = new Promise<vscode.NotebookDocument>((resolve, reject) => {
    const timeout = setTimeout(() => {
      subscription.dispose();
      reject(new Error('Notebook did not open within five seconds.'));
    }, 5_000);
    const subscription = vscode.workspace.onDidOpenNotebookDocument(notebook => {
      if (notebook.uri.toString() !== uri.toString()) return;
      clearTimeout(timeout);
      subscription.dispose();
      resolve(notebook);
    });
  });

  await vscode.commands.executeCommand('vscode.openWith', uri, 'jupyter-notebook');
  return opened;
}

function notebookFixture(): object {
  return {
    cells: [{
      cell_type: 'code',
      execution_count: null,
      metadata: {},
      outputs: [],
      source: ['%%bsl\n', 'Процедура Проба()\n', 'КонецПроцедуры'],
    }],
    metadata: {
      kernelspec: {display_name: 'Python 3', language: 'python', name: 'python3'},
      language_info: {name: 'python'},
    },
    nbformat: 4,
    nbformat_minor: 5,
  };
}

async function createDesignerFixture(root: string): Promise<void> {
  for (const relativePath of [
    'Configuration.xml',
    'CommonModules/Probe.xml',
    'CommonModules/Probe/Ext/Module.bsl',
  ]) {
    const filePath = path.join(root, relativePath);
    await fs.mkdir(path.dirname(filePath), {recursive: true});
    await fs.writeFile(filePath, 'fixture', 'utf8');
  }
}
