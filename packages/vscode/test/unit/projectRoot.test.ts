import * as assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import * as os from 'node:os';
import * as path from 'node:path';
import test from 'node:test';

import {OpenNotebookRoots, validateSourceRoot} from '../../src/projectRoot';

test('accepts Designer and EDT source layouts with canonical selected paths', async () => {
  await withTemporaryDirectory(async directory => {
    const designer = path.join(directory, 'designer');
    const edtParent = path.join(directory, 'edt-project');
    const edtSource = path.join(edtParent, 'src');
    await createDesignerFixture(designer);
    await createEdtFixture(edtSource);

    const selectedDesigner = await validateSourceRoot(designer);
    const selectedEdtSource = await validateSourceRoot(edtSource);
    const selectedEdtParent = await validateSourceRoot(edtParent);

    assert.deepEqual(selectedDesigner, {
      canonicalPath: await fs.realpath(designer),
      layout: 'designer',
    });
    assert.deepEqual(selectedEdtSource, {
      canonicalPath: await fs.realpath(edtSource),
      layout: 'edt-src',
    });
    assert.deepEqual(selectedEdtParent, {
      canonicalPath: await fs.realpath(edtParent),
      layout: 'edt-parent',
    });
  });
});

test('rejects missing and empty source roots', async () => {
  await withTemporaryDirectory(async directory => {
    await assert.rejects(
      validateSourceRoot(path.join(directory, 'missing')),
      /does not exist/i,
    );
    await assert.rejects(validateSourceRoot(directory), /does not match/i);
  });
});

test('rejects a source tree that has layout markers but no readable module pair', async () => {
  await withTemporaryDirectory(async directory => {
    await writeFixtureFile(directory, 'Configuration.xml');
    await fs.mkdir(path.join(directory, 'CommonModules', 'Probe'), {recursive: true});

    await assert.rejects(validateSourceRoot(directory), /does not match/i);
  });
});

test('rejects a selected root or ancestor that is a link', async t => {
  await withTemporaryDirectory(async directory => {
    const realParent = path.join(directory, 'real-parent');
    const designer = path.join(realParent, 'designer');
    const linkedRoot = path.join(directory, 'linked-root');
    const linkedParent = path.join(directory, 'linked-parent');
    await createDesignerFixture(designer);

    try {
      await fs.symlink(designer, linkedRoot, 'junction');
      await fs.symlink(realParent, linkedParent, 'junction');
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'EPERM') {
        t.skip('creating directory links requires Windows developer mode or elevated privileges');
        return;
      }
      throw error;
    }

    await assert.rejects(validateSourceRoot(linkedRoot), /link/i);
    await assert.rejects(validateSourceRoot(path.join(linkedParent, 'designer')), /link/i);
  });
});

test('holds roots separately for each open notebook and clears only the closed notebook', () => {
  const roots = new OpenNotebookRoots();
  roots.set('file:///a.ipynb', 'C:/designer');
  roots.set('file:///b.ipynb', 'C:/edt');

  roots.clear('file:///a.ipynb');

  assert.equal(roots.get('file:///a.ipynb'), undefined);
  assert.equal(roots.get('file:///b.ipynb'), 'C:/edt');
});

async function createDesignerFixture(root: string): Promise<void> {
  await writeFixtureFile(root, 'Configuration.xml');
  await writeFixtureFile(root, 'CommonModules/Probe.xml');
  await writeFixtureFile(root, 'CommonModules/Probe/Ext/Module.bsl');
}

async function createEdtFixture(root: string): Promise<void> {
  await writeFixtureFile(root, 'Configuration/Configuration.mdo');
  await writeFixtureFile(root, 'CommonModules/Probe/Probe.mdo');
  await writeFixtureFile(root, 'CommonModules/Probe/Module.bsl');
}

async function writeFixtureFile(root: string, relativePath: string): Promise<void> {
  const filePath = path.join(root, relativePath);
  await fs.mkdir(path.dirname(filePath), {recursive: true});
  await fs.writeFile(filePath, 'fixture', 'utf8');
}

async function withTemporaryDirectory(callback: (directory: string) => Promise<void>): Promise<void> {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'bsl-source-root-'));
  try {
    await callback(directory);
  } finally {
    await fs.rm(directory, {recursive: true, force: true});
  }
}
