import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import * as vscode from 'vscode';
import {FileEvent, watchProject} from '../../src/fileChanges';

export async function run(): Promise<void> {
  const fixture = await fs.mkdtemp(path.join(os.tmpdir(), 'bsl-watch-host-'));
  const root = vscode.Uri.file(fixture);
  assert.equal(vscode.workspace.getWorkspaceFolder(root), undefined);
  const first: FileEvent[] = [], second: FileEvent[] = [];
  const a = watchProject(root, (changes) => first.push(...changes));
  const b = watchProject(root, (changes) => second.push(...changes));
  try {
    // Give the real VS Code recursive watcher time to attach outside its workspace.
    await delay(1000);
    const module = path.join(fixture, 'Probe.bsl');
    await fs.writeFile(module, 'first');
    await eventually(() => first.some(e => e.type === 1) && second.some(e => e.type === 1));
    a.dispose(); first.length = 0; second.length = 0;
    await fs.writeFile(module, 'saved signature');
    await eventually(() => second.some(e => e.type === 2));
    assert.equal(first.length, 0, 'disposing one reference must detach only that subscriber');
    second.length = 0;
    await fs.rename(module, path.join(fixture, 'Renamed.bsl'));
    await eventually(() => second.some(e => e.type === 3) && second.some(e => e.type === 1));
    second.length = 0;
    const metadata = path.join(fixture, 'Probe.mdo');
    await fs.writeFile(metadata, '<metadata/>');
    await eventually(() => second.some(e => e.type === 1 && e.uri.endsWith('Probe.mdo')));
    second.length = 0;
    const renamedMetadata = path.join(fixture, 'Renamed.mdo');
    await fs.rename(metadata, renamedMetadata);
    await eventually(() => second.some(e => e.type === 3 && e.uri.endsWith('Probe.mdo')) && second.some(e => e.type === 1 && e.uri.endsWith('Renamed.mdo')));
    second.length = 0;
    await fs.rm(renamedMetadata);
    await eventually(() => second.some(e => e.type === 3 && e.uri.endsWith('Renamed.mdo')));
    b.dispose(); second.length = 0;
    await fs.writeFile(path.join(fixture, 'After.os'), 'after release');
    await delay(500);
    assert.equal(second.length, 0);
    assert.throws(() => watchProject(vscode.Uri.parse('untitled:/root'), () => {}));
    const target = path.join(fixture, 'target');
    const linked = path.join(fixture, 'linked');
    await fs.mkdir(target);
    await fs.symlink(target, linked, process.platform === 'win32' ? 'junction' : 'dir');
    assert.throws(() => {
      const linkedWatcher = watchProject(vscode.Uri.file(linked), () => {});
      linkedWatcher.dispose();
    }, /root/, 'a linked selected root must be rejected before watcher creation');
  } finally { a.dispose(); b.dispose(); await fs.rm(fixture, {recursive: true, force: true}); }
}

async function eventually(ready: () => boolean): Promise<void> {
  const deadline = Date.now() + 10_000;
  while (!ready() && Date.now() < deadline) await delay(50);
  assert.ok(ready(), 'explicit-root watcher must observe saved files within 10 seconds');
}
