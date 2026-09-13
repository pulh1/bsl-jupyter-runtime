import * as assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import * as os from 'node:os';
import * as path from 'node:path';
import * as vscode from 'vscode';

const bslCellSource = '%%bsl\nПроцедура Проба()\nКонецПроцедуры';

export async function run(): Promise<void> {
  const fixtureDirectory = await fs.mkdtemp(path.join(os.tmpdir(), 'bsl-notebook-'));
  const fixtureUri = vscode.Uri.file(path.join(fixtureDirectory, 'bsl-cell.ipynb'));
  const fixture = {
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

  try {
    await fs.writeFile(fixtureUri.fsPath, JSON.stringify(fixture), 'utf8');
    const notebook = await vscode.workspace.openNotebookDocument(fixtureUri);
    const cell = notebook.cellAt(0);

    assert.equal(cell.document.languageId, 'python');
    assert.equal(cell.document.getText(), bslCellSource);
    const extension = vscode.extensions.getExtension('onec-interactive.bsl-notebook');
    assert.ok(extension);
    await extension.activate();
    assert.ok(extension.isActive);
    const {getNotebookCoordinator} = require('../../src/extension') as typeof import('../../src/extension');
    assert.ok(getNotebookCoordinator()?.snapshotFor(fixtureUri.toString()), 'opening a notebook must retain its BSL snapshot');
    assert.equal(getNotebookCoordinator()?.sessionFor(fixtureUri.toString()), undefined,
      'default on-demand startup must leave the shared server idle on notebook open');
  } finally {
    await fs.rm(fixtureDirectory, {recursive: true, force: true});
  }
}
