import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import * as vscode from 'vscode';
import * as extension from '../../src/extension';

export async function run(): Promise<void> {
  const api = extension as unknown as {getNotebookCoordinator?: () => any};
  assert.equal(typeof api.getNotebookCoordinator, 'function', 'activation must own notebook lifecycle');
  const coordinator = api.getNotebookCoordinator!();
  const commands = await vscode.commands.getCommands();
  for (const id of ['selectSources', 'changeSources', 'clearSources']) assert.ok(commands.includes(`onec-bsl.${id}`));
  const directory = await fs.mkdtemp(path.join(vscode.workspace.workspaceFolders![0].uri.fsPath, 'bsl-lifecycle-'));
  const file = path.join(directory, 'lifecycle.ipynb');
  const bytes = JSON.stringify({nbformat: 4, nbformat_minor: 5, metadata: {language_info: {name: 'python'}}, cells: [
    {cell_type: 'code', id: 'one', execution_count: null, metadata: {}, outputs: [], source: ['%%bsl\n', 'Сообщить(1);']},
  ]});
  const source = path.join(directory, 'source');
  const settings = path.join(directory, '.vscode/settings.json');
  try {
    await fs.writeFile(file, bytes);
    await fs.mkdir(path.dirname(settings), {recursive: true}); await fs.writeFile(settings, '{"editor.fontSize": 15}');
    await fs.mkdir(path.join(source, 'CommonModules/Probe/Ext'), {recursive: true});
    const sourceFiles = ['Configuration.xml', 'CommonModules/Probe.xml', 'CommonModules/Probe/Ext/Module.bsl'];
    for (const relative of sourceFiles) await fs.writeFile(path.join(source, relative), `fixture:${relative}`);
    await vscode.commands.executeCommand('vscode.openWith', vscode.Uri.file(file), 'jupyter-notebook');
    await eventually(() => vscode.workspace.notebookDocuments.some(value => value.uri.toString() === vscode.Uri.file(file).toString()));
    const notebook = vscode.workspace.notebookDocuments.find(value => value.uri.toString() === vscode.Uri.file(file).toString())!;
    assert.ok(notebook);
    const key = notebook.uri.toString();
    await eventually(() => !!coordinator.snapshotFor(key));
    assert.equal(coordinator.sessionFor(key), undefined, 'opening a notebook stays idle in on-demand mode');
    await coordinator.ensureSession(key);
    assert.ok(coordinator.sessionFor(key), 'an explicit request wakes the shared BSL session');
    assert.equal(coordinator.snapshotFor(key).text, 'Сообщить(1);');
    await extension.selectSourceRootForNotebook(notebook, undefined, async () => [vscode.Uri.file(source)]);
    assert.equal(coordinator.rootFor(key), await fs.realpath(source));
    assert.equal(notebook.isDirty, false);
    assert.equal(await fs.readFile(settings, 'utf8'), '{"editor.fontSize": 15}');
    for (const relative of sourceFiles) assert.equal(await fs.readFile(path.join(source, relative), 'utf8'), `fixture:${relative}`);
    await vscode.commands.executeCommand('onec-bsl.clearSources');
    assert.equal(coordinator.rootFor(key), undefined);
    const edit = new vscode.WorkspaceEdit();
    edit.replace(notebook.cellAt(0).document.uri, new vscode.Range(1, 0, 1, 12), 'Сообщить(2);');
    await vscode.workspace.applyEdit(edit);
    await eventually(() => coordinator.snapshotFor(key)?.text === 'Сообщить(2);');
    assert.equal(notebook.cellAt(0).document.languageId, 'python');
    assert.equal(await fs.readFile(file, 'utf8'), bytes);
    // Revert the deliberate test edit before closing, so no save prompt is needed.
    await vscode.commands.executeCommand('workbench.action.files.revert');
    const tab = vscode.window.tabGroups.all.flatMap(group => group.tabs).find(tab => tab.input instanceof vscode.TabInputNotebook && tab.input.uri.toString() === key);
    assert.ok(tab); assert.equal(await vscode.window.tabGroups.close(tab), true);
    await eventually(() => coordinator.snapshotFor(key) === undefined);
    assert.equal(coordinator.rootFor(key), undefined); assert.equal(coordinator.sessionFor(key), undefined);
    assert.equal(await fs.readFile(file, 'utf8'), bytes);
    await vscode.commands.executeCommand('vscode.openWith', vscode.Uri.file(file), 'jupyter-notebook');
    await eventually(() => vscode.workspace.notebookDocuments.some(value => value.uri.toString() === key));
    const reopened = vscode.workspace.notebookDocuments.find(value => value.uri.toString() === key)!;
    assert.equal(coordinator.rootFor(reopened.uri.toString()), undefined);
    assert.equal(coordinator.sessionFor(key), undefined);
    const configuration = vscode.workspace.getConfiguration('onecBsl');
    const previous = configuration.inspect<string>('serverStartup')?.globalValue;
    try {
      await configuration.update('serverStartup', 'onNotebookOpen', vscode.ConfigurationTarget.Global);
      await eventually(() => !!coordinator.sessionFor(key));
      assert.ok(coordinator.sessionFor(key), 'switching to onNotebookOpen must start already-open BSL notebooks');
    } finally {
      await configuration.update('serverStartup', previous, vscode.ConfigurationTarget.Global);
    }
    console.log('Task 8 activated lifecycle text/close/reopen and unchanged notebook bytes passed.');
  } finally {await fs.rm(directory, {recursive: true, force: true});}
}
async function eventually(check: () => boolean): Promise<void> {
  const deadline = Date.now() + 15_000;
  while (!check() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 25));
  assert.ok(check(), 'lifecycle did not converge');
}
