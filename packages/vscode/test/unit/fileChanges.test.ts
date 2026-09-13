import assert from 'node:assert/strict';
import {mkdtemp, mkdir, rm, symlink, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {test} from 'node:test';

import {FileChangeCoalescer, isProjectFile} from '../../src/fileChanges';

test('coalesces atomic saves and retains structural metadata restart across a batch', () => {
  const events = new FileChangeCoalescer();
  const uri = 'file:///export/CommonModules/Probe/Ext/Module.bsl';
  events.push({uri, type: 2}); events.push({uri, type: 2});
  assert.deepEqual(events.flush(), {events: [{uri, type: 2}], restart: false});
  events.push({uri, type: 3}); events.push({uri, type: 1});
  assert.deepEqual(events.flush(), {events: [{uri, type: 2}], restart: false});
  events.push({uri: 'file:///export/CommonModules/New.XML', type: 1});
  events.push({uri: 'file:///export/CommonModules/New.XML', type: 3});
  assert.equal(events.flush().restart, true);
  events.push({uri: 'file:///export/CommonModules/Probe/Probe.mdo', type: 1});
  assert.equal(events.flush().restart, true);
  events.push({uri: 'file:///export/CommonModules/Probe/Probe.mdo', type: 3});
  assert.equal(events.flush().restart, true);
  assert.deepEqual(events.flush(), {events: [], restart: false});
});

test('overflow is bounded at 4096 unique paths and resets after a restart batch', () => {
  const events = new FileChangeCoalescer();
  for (let index = 0; index < 5000; index++) events.push({uri: `file:///export/${index}.bsl`, type: 2});
  const batch = events.flush();
  assert.ok(batch.events.length <= 4096);
  assert.equal(batch.restart, true);
  events.push({uri: 'file:///export/next.os', type: 2});
  assert.deepEqual(events.flush(), {events: [{uri: 'file:///export/next.os', type: 2}], restart: false});
});

test('canonical module create/delete/rename restarts while atomic replacement remains a save', () => {
  for (const uri of ['file:///export/CommonModules/Probe/Ext/Module.bsl', 'file:///edt/src/CommonModules/Probe/Module.bsl']) {
    const events = new FileChangeCoalescer();
    events.push({uri, type: 3}); assert.equal(events.flush().restart, true, 'canonical deletion must evict native LS cache');
    events.push({uri, type: 1}); assert.equal(events.flush().restart, true, 'canonical creation must reindex metadata module');
    events.push({uri, type: 3}); events.push({uri, type: 1}); assert.equal(events.flush().restart, false);
  }
});

test('source event validation rejects outside paths, URI tricks, symlinks and junctions, including deleted descendants', async () => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), 'bsl-watch-security-'));
  try {
    const root = path.join(fixture, 'root');
    const outside = path.join(fixture, 'root-other');
    await mkdir(root); await mkdir(outside);
    await writeFile(path.join(root, 'Probe.bsl'), '');
    await writeFile(path.join(outside, 'Escape.bsl'), '');
    await symlink(outside, path.join(root, 'linked'), process.platform === 'win32' ? 'junction' : 'dir');
    const uri = (relative: string) => pathToFileURL(path.join(root, relative)).href;
    assert.equal(isProjectFile(root, uri('Probe.bsl')), true);
    assert.equal(isProjectFile(root, uri('deleted/Module.bsl')), true);
    assert.equal(isProjectFile(root, uri('../root-other/Escape.bsl')), false);
    assert.equal(isProjectFile(root, uri('linked/Escape.bsl')), false);
    assert.equal(isProjectFile(root, uri('linked/deleted.bsl')), false);
    assert.equal(isProjectFile(root, uri('Probe.bsl') + '?alternate'), false);
    assert.equal(isProjectFile(root, 'untitled:/Probe.bsl'), false);
    assert.equal(isProjectFile(root, uri('ignored.txt')), false);
    assert.equal(isProjectFile(path.join(root, 'linked'), uri('linked/Escape.bsl')), false);
  } finally { await rm(fixture, {recursive: true, force: true}); }
});
