import assert from 'node:assert/strict';
import {test} from 'node:test';
import {existsSync} from 'node:fs';
async function api() {
  assert.ok(existsSync(new URL('../src/sourceViewer.ts', import.meta.url)), 'current source viewer missing');
  return import('../src/sourceViewer.js');
}
test('explicit repeat open reads current bytes and deleted files visibly become unavailable', async () => {
  const {SourceViewer} = await api(); let disk: string | null = 'first'; let displayed = '';
  const states: string[] = [];
  const context = {ready:Promise.resolve(), async revert() {
    if (disk === null) throw {status:404}; displayed = disk;
  }};
  const viewer = new SourceViewer(context, state => states.push(state));
  await viewer.refresh(); assert.equal(displayed, 'first'); assert.equal(states.at(-1), 'available');
  disk = 'saved'; await viewer.refresh(); assert.equal(displayed, 'saved');
  disk = null; await viewer.refresh(); assert.equal(states.at(-1), 'unavailable');
  assert.equal(displayed, 'saved'); // old bytes must never be described as current after failure
  disk = 'restored'; await viewer.refresh(); assert.equal(displayed, 'restored');
  viewer.dispose();
});
test('overlapping refreshes serialize current reads and disposed callbacks cannot claim availability', async () => {
  const {SourceViewer} = await api(); const states: string[] = []; const releases: (() => void)[] = [];
  let active = 0; let maximum = 0;
  const viewer = new SourceViewer({ready:Promise.resolve(), async revert() {
    maximum = Math.max(maximum, ++active); await new Promise<void>(resolve => releases.push(resolve)); active--;
  }}, state => states.push(state));
  const one = viewer.refresh(); await new Promise(resolve => setImmediate(resolve));
  const two = viewer.refresh(); releases.shift()!();
  await new Promise(resolve => setImmediate(resolve)); assert.equal(maximum, 1);
  viewer.dispose(); const before = states.length; releases.shift()!(); await Promise.all([one, two]);
  assert.equal(states.length, before);
});

test('repeated opens coalesce into one pending refresh instead of an unbounded read queue', async () => {
  const {SourceViewer} = await api(); let calls = 0;
  let release: (() => void) | undefined;
  const viewer = new SourceViewer({ready:Promise.resolve(), async revert() {
    calls++;
    if (calls === 1) await new Promise<void>(resolve => {release = resolve;});
  }}, () => {});
  const pending = [viewer.refresh()];
  await new Promise(resolve => setImmediate(resolve));
  for (let i = 0; i < 100; i++) pending.push(viewer.refresh());
  release!(); await Promise.all(pending);
  assert.equal(calls, 2);
  viewer.dispose();
});

test('failed source lookup makes existing viewer unavailable and fences older successful reads', async () => {
  const {SourceViewer} = await api(); const states: string[] = [];
  let release: (() => void) | undefined;
  const viewer = new SourceViewer({ready:Promise.resolve(), async revert() {
    await new Promise<void>(resolve => {release = resolve;});
  }}, state => states.push(state));
  const pending = viewer.refresh(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(typeof viewer.unavailable, 'function', 'failed lookup notification missing');
  viewer.unavailable(); release!(); await pending;
  assert.equal(states.at(-1), 'unavailable');
  viewer.dispose();
});

test('only the reserved fallback namespace is recognized and only safe complete identities map back to viewers', async () => {
  const module = await api();
  assert.equal(typeof module.sourceViewerPath, 'function', 'source viewer path guard missing');
  const suffix = 'a'.repeat(32) + '/' + 'b'.repeat(64) + '/CommonModules/Модуль/Module.bsl';
  assert.deepEqual(module.sourceViewerPath('onec-bsl:' + suffix), {canonical:'onec-bsl:' + suffix, fallback:false});
  assert.deepEqual(module.sourceViewerPath('.lsp_symlink/onec-bsl:' + suffix), {canonical:'onec-bsl:' + suffix, fallback:true});
  for (const path of ['.lsp_symlink/other:foo.bsl', '.lsp_symlink/onec-bsl-malicious:foo.bsl',
    'onec-bsl:bad/file.bsl', '.lsp_symlink/onec-bsl:' + suffix + '/..',
    '.lsp_symlink/onec-bsl:' + suffix.replace('Module.bsl', '%2e%2e/Module.bsl'),
    '.lsp_symlink/onec-bsl:' + suffix.replace('Module.bsl', 'Module.py')]) {
    assert.equal(module.sourceViewerPath(path), null, path);
  }
});

test('public source drive preserves failed-read rejection and reports only its exact canonical source', async () => {
  const module = await api();
  assert.equal(typeof module.SourceDrive, 'function', 'source drive failure notification missing');
  const {ServerConnection} = await import('@jupyterlab/services');
  const reports: string[] = [];
  const drive = new module.SourceDrive({name:'onec-bsl', apiEndpoint:'onec-bsl/sources',
    serverSettings:ServerConnection.makeSettings({baseUrl:'http://localhost:8888/',
      fetch:async () => new Response('{"message":"unavailable"}', {status:404, statusText:'Not Found'})})},
    path => reports.push(path));
  const path = 'a'.repeat(32) + '/' + 'b'.repeat(64) + '/CommonModules/Module.bsl';
  await assert.rejects(drive.get(path, {content:false}), error => (error as any).response.status === 404);
  assert.deepEqual(reports, ['onec-bsl:' + path]);
  await assert.rejects(drive.get('../other.bsl'));
  assert.equal(reports.length, 1);
  drive.dispose();
});
