import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import path from 'node:path';
import {test} from 'node:test';
import {terminateOwnedProcessTree} from '../../src/processTree';
import {BslTransport} from '../../src/lspTransport';

const fixture = path.resolve(__dirname, '../fixtures/fake-ls.js');

export async function processExists(pid: number): Promise<boolean> {
  // A reparented zombie is already terminated, though kill(0) can still see it on Linux CI.
  if (process.platform === 'linux') {
    const {readFile} = await import('node:fs/promises');
    try { if (/^\d+ \(.*\) Z /.test(await readFile(`/proc/${pid}/stat`, 'utf8'))) return false; }
    catch { /* Fall back to the portable existence check. */ }
  }
  try { process.kill(pid, 0); return true; } catch { return false; }
}

test('close terminates the real owned grandchild and keeps another transport alive', async t => {
  const a = await BslTransport.start(process.execPath, undefined, [fixture]);
  t.after(() => a.close());
  const b = await BslTransport.start(process.execPath, undefined, [fixture]);
  t.after(() => b.close());
  const grandchild = await a.request<number>('test/grandchild', {});
  assert.equal(await processExists(grandchild), true);
  await a.close();
  assert.equal(await processExists(a.pid), false);
  assert.equal(await processExists(grandchild), false);
  assert.equal(await processExists(b.pid), true);
  assert.ok((await b.request<{capabilities: object}>('initialize', {})).capabilities);
});

test('owned process teardown is idempotent after the child has exited', async () => {
  const child = spawn(process.execPath, ['-e', 'process.exit(0)'], {detached: process.platform !== 'win32', windowsHide: true});
  await new Promise<void>((resolve, reject) => { child.once('exit', () => resolve()); child.once('error', reject); });
  await terminateOwnedProcessTree(child);
  await terminateOwnedProcessTree(child);
  assert.equal(await processExists(child.pid!), false);
});
