import assert from 'node:assert/strict';
import {test} from 'node:test';
import {NotebookSnapshot} from '../../src/notebookModel';
import {NotebookCoordinator, CoordinatorSession} from '../../src/notebookCoordinator';
import {FileEvent} from '../../src/fileChanges';

const snapshot = (text = '%%bsl\nСообщить(1);', version = 1) => NotebookSnapshot.fromCells([
  {uri: 'cell:a', kind: 'code', languageId: 'python', text},
], version);
async function harness(options: ConstructorParameters<typeof NotebookCoordinator>[1] = {}) {
  const calls: string[] = [], live = new Set<CoordinatorSession>();
  let peak = 0, now = 0;
  const listeners = new Map<string, (changes: readonly FileEvent[]) => void>();
  const coordinator = new NotebookCoordinator(async (uri: string, value: NotebookSnapshot, root?: string) => {
    const session = {snapshot: value, status: 'ready' as const, reason: undefined,
      async update(value: NotebookSnapshot) {this.snapshot = value; calls.push(`update:${uri}`);},
      async setRoot(root?: string) {calls.push(`root:${uri}:${root}`);},
      async filesChanged() {calls.push(`files:${uri}`);},
      async close() {calls.push(`close:${uri}`); live.delete(session);},
    };
    calls.push(`open:${uri}:${root}`); live.add(session); peak = Math.max(peak, live.size); return session;
  }, {now: () => now, watch: (root: string, listener: (changes: readonly FileEvent[]) => void) => {
    listeners.set(root, listener); return {dispose: () => {calls.push(`unwatch:${root}`); listeners.delete(root);}};
  }, refreshDiagnostics: (uri: string) => calls.push(`refresh:${uri}`), clearDiagnostics: (uri: string) => calls.push(`clear:${uri}`), ...options});
  return {coordinator, calls, live, listeners, peak: () => peak, advance: (ms: number) => {now += ms;}};
}

test('lifecycle isolates roots, updates snapshots, refreshes after synchronization, and clears close/rename', async () => {
  const h = await harness(), c = h.coordinator;
  try {
    await c.open('a', snapshot()); await c.open('b', snapshot());
    await c.setRoot('a', '/root-a'); await c.setRoot('b', '/root-b');
    assert.equal(c.rootFor('a'), '/root-a'); assert.equal(c.rootFor('b'), '/root-b');
    const edited = snapshot('%%bsl\nСообщить(2);', 2);
    await c.update('a', edited); assert.equal(c.snapshotFor('a'), edited);
    assert.deepEqual(h.calls.slice(-2), ['update:a', 'refresh:a']);
    h.listeners.get('/root-a')!([{uri: 'file:///root-a/Module.bsl', type: 2}]);
    assert.equal(h.calls.at(-1), 'files:a', 'saved changes invalidate immediately');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.calls.at(-1), 'refresh:a');
    await c.close('a'); assert.equal(c.rootFor('a'), undefined); assert.equal(c.sessionFor('a'), undefined);
    await c.rename('b', 'c'); assert.equal(c.rootFor('b'), undefined); assert.equal(c.rootFor('c'), undefined);
    assert.equal(h.live.size, 0); assert.equal(h.listeners.size, 0);
    await c.open('a', snapshot()); assert.equal(c.rootFor('a'), undefined);
  } finally {await c.dispose();}
});

test('demand mode remembers notebook edits and selected sources without opening a session', async () => {
  const h = await harness(), c = h.coordinator;
  try {
    await c.update('a', snapshot(), false);
    await c.setRoot('a', '/selected', false);
    const newest = snapshot('%%bsl\nСообщить(3);', 3);
    await c.update('a', newest, false);
    assert.equal(c.snapshotFor('a'), newest);
    assert.equal(c.rootFor('a'), '/selected');
    assert.equal(c.sessionFor('a'), undefined);
    assert.equal(h.live.size, 0);
    assert.equal(h.listeners.size, 0);
    await c.ensureSession('a');
    assert.equal(c.sessionFor('a')?.snapshot, newest);
    assert.ok(h.calls.includes('open:a:/selected'));
  } finally {await c.dispose();}
});

test('demand mode workspace restart does not wake an idle notebook', async () => {
  const h = await harness(), c = h.coordinator;
  try {
    await c.update('a', snapshot(), false);
    await c.restart('a', false);
    assert.equal(c.sessionFor('a'), undefined);
    assert.equal(h.live.size, 0);
  } finally {await c.dispose();}
});

test('eight child cap, idle eviction, and demand wake preserve only open notebook roots', async () => {
  const h = await harness(), c = h.coordinator;
  try {
    await Promise.all(Array.from({length: 10}, (_, i) => c.open(String(i), snapshot())));
    assert.equal(h.peak(), 8); assert.equal(h.live.size, 8);
    await c.setRoot('9', '/keep'); h.advance(300_001); await c.expireIdle();
    assert.equal(h.live.size, 0); assert.equal(c.rootFor('9'), '/keep');
    await c.ensureSession('9'); assert.ok(c.sessionFor('9')); assert.equal(h.live.size, 1);
    await c.update('9', snapshot('print(1)', 2)); assert.equal(h.live.size, 0);
  } finally {await c.dispose();}
  assert.equal(c.rootFor('9'), undefined);
});

test('close during startup cannot resurrect a session or root', async () => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => {release = resolve;});
  let closed = 0;
  const pending = new NotebookCoordinator(async (_uri: string, value: NotebookSnapshot) => {
    await gate; return {snapshot: value, status: 'ready', async update() {}, async setRoot() {}, async filesChanged() {}, async close() {closed++;}};
  });
  const opening = pending.open('a', snapshot()); await new Promise(resolve => setImmediate(resolve));
  const closing = pending.close('a'); release(); await Promise.all([opening, closing]);
  assert.equal(pending.sessionFor('a'), undefined); assert.equal(pending.rootFor('a'), undefined); assert.equal(closed, 1);
  await pending.dispose();
});

test('failed document close remains owned and is retried during disposal', async () => {
  let closes = 0;
  const coordinator = new NotebookCoordinator(async (_uri: string, value: NotebookSnapshot) => ({
    snapshot: value, status: 'ready' as const,
    async update() {}, async setRoot() {}, async filesChanged() {},
    async close() {if (++closes === 1) throw new Error('user canceled temporary tab closure');},
  }));
  await coordinator.open('a', snapshot());
  await assert.rejects(coordinator.close('a'), /canceled/);
  await coordinator.dispose();
  assert.equal(closes, 2, 'deactivation must retry the still-owned document');
});

test('executable changes replace the session before starting another and retain selected root', async () => {
  const h = await harness(), c = h.coordinator;
  try {
    await c.open('a', snapshot()); await c.setRoot('a', '/selected');
    const previous = c.sessionFor('a');
    assert.equal(typeof c.restart, 'function', 'executable changes need a fresh transport factory');
    await c.restart('a');
    assert.notEqual(c.sessionFor('a'), previous); assert.equal(c.rootFor('a'), '/selected');
    assert.equal(h.peak(), 1); assert.ok(h.calls.indexOf('close:a') < h.calls.lastIndexOf('open:a:/selected'));
  } finally {await c.dispose();}
});

test('lost root falls back to notebook analysis and keeps the selected path visible with reason', async () => {
  let readable = true;
  const h = await harness({checkRoot: async () => {if (!readable) throw new Error('unreadable');}}), c = h.coordinator;
  try {
    await c.open('a', snapshot()); await c.setRoot('a', '/selected'); readable = false;
    assert.equal(typeof c.recheckRoots, 'function', 'lost source root must be detected');
    await c.recheckRoots();
    assert.equal(c.rootFor('a'), '/selected'); assert.equal(c.statusFor('a').status, 'unavailable');
    assert.match(c.statusFor('a').reason!, /notebook only/); assert.ok(h.calls.includes('root:a:undefined'));
  } finally {await c.dispose();}
});

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(yes => {resolve = yes;});
  return {promise, resolve};
}
async function startupHarness(failFirstWatch = false) {
  const opening = deferred(), syncing = deferred();
  const subscriptions: {root: string; callback: (events: readonly FileEvent[]) => void; disposed: boolean}[] = [];
  const sessions: {snapshot: NotebookSnapshot; root?: string; status: 'ready'; received: FileEvent[]; closed: boolean;
    update(value: NotebookSnapshot): Promise<void>; setRoot(): Promise<void>; filesChanged(events: readonly FileEvent[]): Promise<void>; close(): Promise<void>}[] = [];
  let refreshes = 0;
  let watchAttempts = 0;
  const c = new NotebookCoordinator(async (_uri, value, root) => {
    const session = {snapshot: value, root, status: 'ready' as const, received: [] as FileEvent[], closed: false,
      async update(value: NotebookSnapshot) {this.snapshot = value;}, async setRoot() {},
      async filesChanged(events: readonly FileEvent[]) {this.received.push(...events); await syncing.promise;},
      async close() {this.closed = true;},
    };
    sessions.push(session); await opening.promise; return session;
  }, {watch: (root, callback) => {
    if (++watchAttempts === 1 && failFirstWatch) throw new Error('source temporarily unavailable');
    const subscription = {root, callback, disposed: false}; subscriptions.push(subscription);
    return {dispose() {subscription.disposed = true;}};
  }, refreshDiagnostics: () => {refreshes++;}});
  await c.open('a', snapshot('print(1)')); await c.setRoot('a', '/root-a');
  const started = c.update('a', snapshot()); await new Promise(resolve => setImmediate(resolve));
  return {c, started, opening, syncing, sessions, subscriptions, refreshes: () => refreshes};
}
const saved: FileEvent = {uri: 'file:///root-a/Module.bsl', type: 2};

test('startup source events are replayed before exposing the session or refreshing diagnostics', async () => {
  const h = await startupHarness();
  try {
    h.subscriptions.at(-1)!.callback([saved]); h.opening.resolve();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(h.sessions[0].received, [saved], 'a save during initialize must reach the session');
    assert.equal(h.c.sessionFor('a'), undefined, 'unsynchronized startup must not answer editor requests');
    assert.equal(h.refreshes(), 0);
    h.syncing.resolve(); await h.started;
    assert.ok(h.c.sessionFor('a')); assert.equal(h.refreshes(), 1);
  } finally {h.opening.resolve(); h.syncing.resolve(); await h.c.dispose();}
});

test('startup root reselection and A to B to A preserve exactly one current watcher and reject stale callbacks', async () => {
  for (const roots of [['/root-a'], ['/root-b', '/root-a']]) {
    const h = await startupHarness();
    try {
      const stale = h.subscriptions.at(-1)!;
      const changing = roots.map(root => h.c.setRoot('a', root));
      stale.callback([saved]);
      h.opening.resolve(); h.syncing.resolve(); await Promise.all([h.started, ...changing]);
      const current = h.subscriptions.filter(subscription => !subscription.disposed);
      assert.equal(current.length, 1, 'ready selected-root session must own its watcher');
      assert.equal(current[0].root, '/root-a');
      assert.equal(h.sessions[0].closed, true, 'root generation changes must invalidate the earlier scan');
      assert.equal(h.sessions.at(-1)!.root, '/root-a');
      assert.equal(h.sessions.flatMap(session => session.received).length, 0, 'disposed callback must not cross root generation');
      current[0].callback([saved]); await new Promise(resolve => setImmediate(resolve));
      assert.deepEqual(h.sessions.at(-1)!.received, [saved]);
    } finally {h.opening.resolve(); h.syncing.resolve(); await h.c.dispose();}
  }
});

test('magic removal and readdition during startup restore watching and synchronize the latest snapshot', async () => {
  const h = await startupHarness();
  try {
    await h.c.update('a', snapshot('print(1)', 2));
    const newest = snapshot('%%bsl\nСообщить(3);', 3), updating = h.c.update('a', newest);
    h.opening.resolve(); h.syncing.resolve(); await Promise.all([h.started, updating]);
    assert.equal(h.subscriptions.filter(subscription => !subscription.disposed).length, 1);
    assert.equal(h.c.sessionFor('a')?.snapshot, newest);
    assert.equal(h.sessions[0].closed, true);
  } finally {h.opening.resolve(); h.syncing.resolve(); await h.c.dispose();}
});

test('overflow during startup discards the stale scan and starts a fresh bounded session', async () => {
  const h = await startupHarness();
  try {
    h.subscriptions.at(-1)!.callback(Array.from({length: 5000}, (_, i) => ({uri: `file:///root-a/${i}.bsl`, type: 2})));
    h.opening.resolve(); h.syncing.resolve(); await h.started;
    assert.equal(h.sessions[0].closed, true, 'a scan with incomplete saved-file history must be replaced');
    assert.equal(h.sessions.length, 2); assert.ok(h.c.sessionFor('a'));
    assert.ok(h.sessions.every(session => session.received.length <= 4096));
  } finally {h.opening.resolve(); h.syncing.resolve(); await h.c.dispose();}
});

test('watcher recovery during rootless startup replaces the scan before publishing the selected root', async () => {
  const h = await startupHarness(true);
  try {
    assert.equal(h.sessions[0].root, undefined, 'failed watcher must initially degrade to rootless analysis');
    const retry = h.c.ensureSession('a');
    h.opening.resolve(); h.syncing.resolve(); await Promise.all([h.started, retry]);
    assert.equal(h.c.sessionFor('a')?.root, '/root-a', 'published session must use the recovered selected root');
    assert.equal(h.sessions[0].closed, true); assert.equal(h.sessions.length, 2);
    assert.equal(h.subscriptions.filter(subscription => !subscription.disposed).length, 1);
    assert.equal(h.c.statusFor('a').status, 'ready');
    await h.c.recheckRoots();
    assert.equal(h.c.sessionFor('a')?.root, '/root-a');
  } finally {h.opening.resolve(); h.syncing.resolve(); await h.c.dispose();}
});
