import {NotebookSnapshot} from './notebookModel';
import {OpenNotebookRoots} from './projectRoot';
import type {FileEvent} from './fileChanges';
import type {SessionStatus} from './notebookSession';

export interface CoordinatorSession {
  readonly snapshot: NotebookSnapshot;
  readonly status: SessionStatus;
  readonly reason?: string;
  update(snapshot: NotebookSnapshot): Promise<void>;
  setRoot(root: string | undefined): Promise<void>;
  filesChanged(changes: readonly FileEvent[]): Promise<void>;
  close(): Promise<void>;
}
type Disposable = {dispose(): void};
type Options = {
  now?: () => number;
  watch?: (root: string, listener: (changes: readonly FileEvent[]) => void) => Disposable;
  checkRoot?: (root: string) => Promise<void>;
  refreshDiagnostics?: (uri: string) => void;
  clearDiagnostics?: (uri: string) => void;
  changed?: () => void;
};
type Entry<T> = {uri: string; snapshot: NotebookSnapshot; touched: number; session?: T; starting?: T;
  generation: number; watchGeneration: number; startupEvents: FileEvent[]; startupOverflow: boolean;
  pending?: Promise<void>; closing?: Promise<void>; watcher?: Disposable; reason?: string; effectiveRoot?: string};

/** Open-notebook state is memory-only. Admission serializes ownership, not edits. */
export class NotebookCoordinator<T extends CoordinatorSession = CoordinatorSession> {
  private readonly roots = new OpenNotebookRoots();
  private readonly entries = new Map<string, Entry<T>>();
  private readonly owned = new Set<T>();
  private admission: Promise<void> = Promise.resolve();
  private disposed = false;
  private readonly now: () => number;

  constructor(private readonly factory: (uri: string, snapshot: NotebookSnapshot, root?: string) => Promise<T>, private readonly options: Options = {}) {
    this.now = options.now ?? Date.now;
  }
  rootFor(uri: string): string | undefined {return this.roots.get(uri);}
  snapshotFor(uri: string): NotebookSnapshot | undefined {return this.entries.get(uri)?.snapshot;}
  sessionFor(uri: string): T | undefined {return this.entries.get(uri)?.session;}
  statusFor(uri: string): {root?: string; status: SessionStatus | 'no root' | 'idle'; reason?: string} {
    const entry = this.entries.get(uri), root = this.rootFor(uri);
    const status = entry?.reason ? 'unavailable' : entry?.pending ? 'indexing' : entry?.session?.status;
    return {root, status: status === 'unavailable' || status === 'indexing' || status === 'updating' ? status : status === 'ready' && !root ? 'no root' : status ?? 'idle',
      reason: entry?.reason ?? entry?.session?.reason};
  }
  open(uri: string, snapshot: NotebookSnapshot, startIfMissing = true): Promise<void> {
    if (this.disposed) return Promise.resolve();
    if (this.entries.has(uri)) return this.update(uri, snapshot, startIfMissing);
    this.entries.set(uri, {uri, snapshot, touched: this.now(), generation: 0, watchGeneration: 0, startupEvents: [], startupOverflow: false});
    this.options.changed?.();
    return startIfMissing ? this.ensureSession(uri) : Promise.resolve();
  }
  update(uri: string, snapshot: NotebookSnapshot, startIfMissing = true): Promise<void> {
    const entry = this.entries.get(uri);
    if (!entry) return this.open(uri, snapshot, startIfMissing);
    entry.snapshot = snapshot; entry.touched = this.now();
    this.options.clearDiagnostics?.(uri);
    if (!snapshot.hasBslCells) return this.release(entry);
    if (!entry.session || entry.closing) return startIfMissing || entry.pending ? this.ensureSession(uri) : Promise.resolve();
    if (entry.session.status === 'unavailable') return startIfMissing ? this.restart(uri) : Promise.resolve();
    const session = entry.session;
    const work = session.update(snapshot); this.options.changed?.();
    return work.then(() => this.refresh(entry, session));
  }
  setRoot(uri: string, root: string | undefined, startIfMissing = true): Promise<void> {
    const entry = this.entries.get(uri);
    if (!entry || this.disposed) return Promise.resolve();
    if (root === undefined) this.roots.clear(uri); else this.roots.set(uri, root);
    entry.touched = this.now(); entry.reason = undefined;
    ++entry.generation; this.detachWatcher(entry);
    this.options.clearDiagnostics?.(uri);
    if (!entry.session || entry.closing) return startIfMissing || entry.pending ? this.ensureSession(uri) : Promise.resolve();
    if (entry.session.status === 'unavailable') return startIfMissing ? this.restart(uri) : Promise.resolve();
    const session = entry.session;
    const effectiveRoot = this.attachWatcher(entry, session, root);
    const work = session.setRoot(effectiveRoot); this.options.changed?.();
    return work.then(() => this.refresh(entry, session));
  }
  ensureSession(uri: string): Promise<void> {
    const entry = this.entries.get(uri);
    if (!entry || this.disposed || !entry.snapshot.hasBslCells) return Promise.resolve();
    entry.touched = this.now();
    if (entry.pending) {
      if (!entry.watcher && this.rootFor(uri)) {
        const previousRoot = entry.effectiveRoot;
        const effectiveRoot = this.attachWatcher(entry, undefined, this.rootFor(uri));
        // Recovery may turn a rootless in-flight scan into a selected-root
        // watcher. That scan must restart even though the selected path is equal.
        if (effectiveRoot !== previousRoot) ++entry.generation;
      }
      return entry.pending;
    }
    if (entry.session?.status === 'unavailable' && !entry.closing) return this.restart(uri);
    if (entry.session && !entry.closing) return Promise.resolve();
    const work = this.admission.then(async () => {
      if (!this.current(entry) || !entry.snapshot.hasBslCells) return;
      if (entry.closing) await entry.closing;
      if (this.owned.size >= 8) {
        const oldest = [...this.entries.values()].filter(candidate => candidate !== entry && candidate.session)
          .sort((a, b) => a.touched - b.touched)[0];
        if (oldest) await this.release(oldest);
      }
      if (this.owned.size >= 8) throw new Error('BSL notebook document limit reached; an earlier document could not close');
      if (!this.current(entry) || !entry.snapshot.hasBslCells) return;
      while (this.current(entry) && entry.snapshot.hasBslCells) {
        const generation = entry.generation;
        const root = this.attachWatcher(entry, undefined, this.rootFor(uri));
        const session = await this.factory(uri, entry.snapshot, root);
        this.owned.add(session); entry.starting = session;
        // Startup is private until root, notebook and saved-source state agree.
        while (this.current(entry) && entry.snapshot.hasBslCells && generation === entry.generation && !entry.startupOverflow) {
          if (entry.snapshot !== session.snapshot) await session.update(entry.snapshot);
          if (!this.current(entry) || generation !== entry.generation) break;
          if (entry.startupEvents.length) await session.filesChanged(entry.startupEvents.splice(0));
          else if (entry.snapshot === session.snapshot) {
            entry.starting = undefined; entry.session = session;
            this.refresh(entry, session); return;
          }
        }
        // A root round-trip, missing magic, or overflow invalidates the scan.
        // Close it before admitting a fresh child; do not falsely free capacity.
        await session.close(); this.owned.delete(session);
        if (entry.starting === session) entry.starting = undefined;
      }
    }).catch(async error => {
      entry.reason = error instanceof Error ? error.message : 'BSL session unavailable';
      try {await this.release(entry);} catch { /* Retain owned capacity after cleanup failure. */ }
    });
    entry.pending = work.finally(() => {entry.pending = undefined; this.options.changed?.();});
    this.admission = entry.pending;
    this.options.changed?.();
    return entry.pending;
  }
  close(uri: string): Promise<void> {
    const entry = this.entries.get(uri);
    this.entries.delete(uri); this.roots.clear(uri); this.options.clearDiagnostics?.(uri); this.options.changed?.();
    if (!entry) return Promise.resolve();
    const releasing = this.release(entry);
    return Promise.all([releasing, entry.pending]).then(() => {});
  }
  async rename(oldUri: string, newUri: string): Promise<void> {await Promise.all([this.close(oldUri), this.close(newUri)]);}
  async expireIdle(): Promise<void> {
    await Promise.all([...this.entries.values()].filter(entry => entry.session && this.now() - entry.touched >= 300_000).map(entry => this.release(entry)));
  }
  async restart(uri: string, startIfMissing = true): Promise<void> {
    const entry = this.entries.get(uri);
    if (!entry || !startIfMissing && !entry.session && !entry.pending) return;
    // Wait for startup ownership before closing; admission still enforces the cap.
    await entry.pending;
    await this.release(entry);
    if (this.current(entry)) {entry.reason = undefined; await this.ensureSession(uri);}
  }
  async recheckRoots(): Promise<void> {
    await Promise.all([...this.entries.values()].map(async entry => {
      const session = entry.session, root = this.rootFor(entry.uri);
      if (!session || !root || entry.closing || entry.pending) return;
      const previous = entry.effectiveRoot;
      let readable = true;
      try {await this.options.checkRoot?.(root);} catch {readable = false;}
      if (!this.current(entry) || this.rootFor(entry.uri) !== root || entry.session !== session || entry.closing) return;
      let effective = previous;
      if (!readable) {
        this.detachWatcher(entry); effective = undefined;
        entry.reason = 'Selected source root unavailable; analyzing notebook only';
      } else if (!previous || !this.options.checkRoot) effective = this.attachWatcher(entry, session, root);
      if (previous !== effective) {
        this.options.clearDiagnostics?.(entry.uri);
        await session.setRoot(effective); this.refresh(entry, session);
      }
    }));
    this.options.changed?.();
  }
  async dispose(): Promise<void> {
    this.disposed = true;
    const results = await Promise.allSettled([...this.entries.keys()].map(uri => this.close(uri)));
    await this.admission;
    const remaining = [...this.owned];
    const cleanup = await Promise.allSettled(remaining.map(async session => {
      await session.close(); this.owned.delete(session);
    }));
    this.roots.clearAll();
    const failedCleanup = cleanup.find(result => result.status === 'rejected');
    if (failedCleanup?.status === 'rejected') throw failedCleanup.reason;
    const failedClose = results.find(result => result.status === 'rejected');
    if (failedClose?.status === 'rejected' && !remaining.length) throw failedClose.reason;
  }
  private current(entry: Entry<T>): boolean {return !this.disposed && this.entries.get(entry.uri) === entry;}
  private refresh(entry: Entry<T>, session: T): void {
    if (this.current(entry) && entry.session === session && !entry.closing) this.options.refreshDiagnostics?.(entry.uri);
    this.options.changed?.();
  }
  private attachWatcher(entry: Entry<T>, session: T | undefined, root?: string): string | undefined {
    this.detachWatcher(entry);
    if (!root) return undefined;
    const watchGeneration = entry.watchGeneration;
    try {
      entry.watcher = this.options.watch?.(root, changes => {
        if (!this.current(entry) || watchGeneration !== entry.watchGeneration || this.rootFor(entry.uri) !== root || entry.closing) return;
        const active = session ?? entry.session;
        if (!active) {
          // Bound raw startup history; overflow requires a new scan, not a
          // partially replayed history that could retain deleted native exports.
          const available = 4096 - entry.startupEvents.length;
          entry.startupEvents.push(...changes.slice(0, available));
          if (changes.length > available) entry.startupOverflow = true;
          return;
        }
        if (entry.session !== active) return;
        // Call synchronously: the session invalidates pending answers before debounce.
        const work = active.filesChanged(changes); this.options.changed?.();
        void work.then(() => this.refresh(entry, active)).catch(() => {entry.reason = 'Source synchronization failed'; this.options.changed?.();});
      });
      entry.reason = undefined; entry.effectiveRoot = root; return root;
    } catch {entry.reason = 'Selected source root unavailable; analyzing notebook only'; return undefined;}
  }
  private detachWatcher(entry: Entry<T>): void {
    ++entry.watchGeneration;
    entry.watcher?.dispose(); entry.watcher = undefined; entry.effectiveRoot = undefined;
    entry.startupEvents = []; entry.startupOverflow = false;
  }
  private release(entry: Entry<T>): Promise<void> {
    ++entry.generation; this.detachWatcher(entry); this.options.clearDiagnostics?.(entry.uri);
    if (entry.closing) return entry.closing;
    const session = entry.session ?? entry.starting;
    if (!session) return Promise.resolve();
    entry.closing = session.close().then(() => {
      this.owned.delete(session); entry.session = undefined; entry.starting = undefined; entry.closing = undefined; this.options.changed?.();
    }).catch(error => {entry.closing = undefined; throw error;});
    return entry.closing;
  }
}
