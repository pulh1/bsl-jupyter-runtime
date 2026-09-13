import {randomUUID} from 'node:crypto';
import {mkdtemp, rmdir} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {BslTransport} from './lspTransport';
import {NotebookSnapshot, Position, Range} from './notebookModel';
import {FileChangeCoalescer, FileEvent, isProjectFile} from './fileChanges';

export type TransportFactory = {start(rootPath: string | undefined): Promise<BslTransport>};

/** The VS Code caller supplies the configured executable, or leaves PATH resolution. */
export function createTransportFactory(binaryPath = 'bsl-language-server'): TransportFactory {
  return {start: rootPath => BslTransport.start(binaryPath || 'bsl-language-server', rootPath)};
}

export type SessionEpoch = Readonly<{notebookVersion: number; rootKey: string; processId: number; projectRevision: number}>;
export type SessionStatus = 'indexing' | 'ready' | 'updating' | 'unavailable';
export type SessionDiagnostic = {range: Range; message: string; severity?: number};
type DiagnosticReport = {kind: 'full'; items: SessionDiagnostic[]} | {kind: 'unchanged'};
type Owner = {
  generation: number;
  uri: string;
  temporaryDirectory?: string;
  transport?: BslTransport;
  subscription?: {dispose(): void};
  opened: boolean;
  sentSnapshot?: NotebookSnapshot;
};

/** One immutable notebook snapshot and one owned LS document/process at a time. */
export class NotebookLspSession {
  private readonly documentName = `.onec-notebook-${randomUUID()}.bsl`;
  private currentSnapshot: NotebookSnapshot;
  private rootPath: string | undefined;
  private owner?: Owner;
  private generation = 0;
  private revision = 0;
  private sourceRevision = 0;
  private revisionCancellation = new AbortController();
  private readonly fileEvents = new FileChangeCoalescer();
  private fileTimer?: NodeJS.Timeout;
  private fileBatch?: {promise: Promise<void>; queued: boolean; resolve(): void; reject(error: unknown): void};
  private documentVersion = 0;
  private diagnosticRequest = 0;
  private currentDiagnostics: readonly SessionDiagnostic[] = [];
  private readonly diagnosticListeners = new Set<(items: readonly SessionDiagnostic[]) => void>();
  private state: SessionStatus = 'indexing';
  private unavailableReason?: string;
  private queue: Promise<void> = Promise.resolve();
  private closed = false;
  private closing?: Promise<void>;

  private constructor(
    readonly notebookUri: string,
    snapshot: NotebookSnapshot,
    rootPath: string | undefined,
    private readonly factory: TransportFactory,
  ) {
    this.currentSnapshot = snapshot;
    this.rootPath = rootPath;
  }

  static async open(notebookUri: string, snapshot: NotebookSnapshot, rootPath: string | undefined, factory: TransportFactory): Promise<NotebookLspSession> {
    const session = new NotebookLspSession(notebookUri, snapshot, rootPath, factory);
    await session.setRoot(rootPath);
    return session;
  }

  get status(): SessionStatus { return this.state; }
  get reason(): string | undefined { return this.unavailableReason; }
  get snapshot(): NotebookSnapshot { return this.currentSnapshot; }
  get virtualUri(): string { return this.owner?.uri ?? ''; }
  get diagnostics(): readonly SessionDiagnostic[] { return this.currentDiagnostics; }
  get projectRevision(): number { return this.sourceRevision; }
  get epoch(): SessionEpoch {
    return Object.freeze({
      notebookVersion: this.currentSnapshot.version,
      rootKey: this.rootPath === undefined ? '' : pathToFileURL(this.rootPath).href,
      processId: this.owner?.generation === this.generation ? this.owner.transport?.pid ?? 0 : 0,
      projectRevision: this.sourceRevision,
    });
  }

  onDiagnostics(listener: (items: readonly SessionDiagnostic[]) => void): {dispose(): void} {
    this.diagnosticListeners.add(listener);
    return {dispose: () => { this.diagnosticListeners.delete(listener); }};
  }

  update(snapshot: NotebookSnapshot): Promise<void> {
    if (this.closed || snapshot === this.currentSnapshot) return Promise.resolve();
    this.currentSnapshot = snapshot;
    this.invalidate();
    if (this.state === 'ready') this.state = 'updating';
    // Promise sequencing coalesces synchronous edits while invalidating immediately.
    return this.enqueue(() => this.synchronize());
  }

  setRoot(rootPath: string | undefined): Promise<void> {
    if (this.closed) return Promise.resolve();
    if (rootPath === this.rootPath && this.state === 'ready') return this.queue;
    this.discardFileBatch();
    this.rootPath = rootPath;
    const generation = ++this.generation;
    this.invalidate();
    this.state = 'indexing';
    this.unavailableReason = undefined;
    return this.enqueue(() => this.replace(generation, rootPath));
  }

  filesChanged(changes: readonly FileEvent[]): Promise<void> {
    if (this.closed || this.rootPath === undefined) return Promise.resolve();
    let accepted = false;
    for (const event of changes) {
      if (!isProjectFile(this.rootPath, event.uri)) continue;
      if (!accepted && !this.fileBatch) {
        ++this.sourceRevision;
        this.invalidate();
        if (this.state === 'ready') this.state = 'updating';
      }
      accepted = true;
      this.fileEvents.push(event);
    }
    if (!accepted) return Promise.resolve();
    if (!this.fileBatch) {
      let resolve!: () => void;
      let reject!: (error: unknown) => void;
      const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
      this.fileBatch = {promise, queued: false, resolve, reject};
    }
    const batch = this.fileBatch;
    if (batch.queued) return batch.promise;
    const generation = this.generation;
    clearTimeout(this.fileTimer);
    this.fileTimer = setTimeout(() => {
      this.fileTimer = undefined;
      batch.queued = true;
      void this.enqueue(async () => {
        if (this.closed || generation !== this.generation || this.fileBatch !== batch) return;
        this.fileBatch = undefined;
        const coalesced = this.fileEvents.flush();
        if (coalesced.restart) {
          this.state = 'indexing';
          await this.replace(++this.generation, this.rootPath);
          return;
        }
        const owner = this.owner;
        if (!owner?.transport || !owner.opened) return;
        try {
          await owner.transport.notify('workspace/didChangeWatchedFiles', {changes: coalesced.events});
          await this.synchronize();
        } catch { this.failed(owner); }
      }).then(batch.resolve, batch.reject);
    }, 150);
    return batch.promise;
  }

  async query<T>(method: string, cellUri: string, position: Position, signal?: AbortSignal): Promise<T | undefined> {
    const revision = this.revision;
    const snapshot = this.currentSnapshot;
    const mapped = snapshot.toVirtual(cellUri, position);
    if (!mapped || this.closed || signal?.aborted) return undefined;
    await this.queue;
    const owner = this.queryOwner(revision);
    if (!owner) return undefined;
    try {
      const cancellation = signal ? AbortSignal.any([signal, this.revisionCancellation.signal]) : this.revisionCancellation.signal;
      const reply = await owner.transport!.request<T>(method, {textDocument: {uri: owner.uri}, position: mapped}, cancellation);
      return this.isCurrent(owner, revision) && !signal?.aborted ? reply : undefined;
    } catch { return undefined; }
  }

  async diagnose(): Promise<readonly SessionDiagnostic[] | undefined> {
    const revision = this.revision;
    const request = ++this.diagnosticRequest;
    await this.queue;
    const owner = this.queryOwner(revision);
    if (!owner) return undefined;
    try {
      const report = await owner.transport!.request<DiagnosticReport>('textDocument/diagnostic', {textDocument: {uri: owner.uri}}, this.revisionCancellation.signal);
      if (!this.isCurrent(owner, revision) || request !== this.diagnosticRequest) return undefined;
      if (report.kind === 'full' && Array.isArray(report.items)) this.publishDiagnostics(Object.freeze([...report.items]));
      return this.currentDiagnostics;
    } catch { return undefined; }
  }

  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.closed = true;
    this.discardFileBatch();
    ++this.generation;
    this.invalidate();
    this.state = 'unavailable';
    this.unavailableReason = 'Notebook session closed';
    this.closing = this.enqueue(async () => {
      await this.releaseOwner();
      this.diagnosticListeners.clear();
    });
    return this.closing;
  }

  private enqueue(operation: () => Promise<void>): Promise<void> {
    const work = this.queue.then(operation);
    // Keep the serialized lifecycle usable after a cleanup error; callers still
    // receive that rejection and can report an unsuccessful shutdown.
    this.queue = work.catch(() => {});
    return work;
  }

  private invalidate(): void {
    ++this.revision;
    this.revisionCancellation.abort();
    this.revisionCancellation = new AbortController();
    this.publishDiagnostics([]);
  }

  private discardFileBatch(): void {
    clearTimeout(this.fileTimer); this.fileTimer = undefined;
    this.fileEvents.flush();
    this.fileBatch?.resolve(); this.fileBatch = undefined;
  }

  private publishDiagnostics(items: readonly SessionDiagnostic[]): void {
    this.currentDiagnostics = items;
    for (const listener of this.diagnosticListeners) {
      try { listener(items); } catch { /* A UI subscriber cannot prevent invalidation or cleanup. */ }
    }
  }

  private isCurrent(owner: Owner, revision: number): boolean {
    // Object identity plus generation also fences PID reuse and A -> B -> A roots.
    return !this.closed && revision === this.revision && this.owner === owner && owner.generation === this.generation;
  }

  private queryOwner(revision: number): Owner | undefined {
    const owner = this.owner;
    return owner?.transport && owner.opened && this.state === 'ready' && this.isCurrent(owner, revision) ? owner : undefined;
  }

  private async replace(generation: number, rootPath: string | undefined): Promise<void> {
    if (this.closed || generation !== this.generation) return;
    try { await this.releaseOwner(); }
    catch {
      if (!this.closed && generation === this.generation) this.markUnavailable();
      return;
    }
    if (this.closed || generation !== this.generation) return;
    const owner: Owner = {generation, uri: '', opened: false};
    this.owner = owner;
    try {
      const workspace = rootPath ?? (owner.temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), 'onec-bsl-workspace-')));
      owner.uri = pathToFileURL(path.join(workspace, this.documentName)).href;
      if (this.closed || generation !== this.generation) return;
      owner.transport = await this.factory.start(rootPath);
      if (this.closed || generation !== this.generation) return;
      owner.subscription = owner.transport.onFailure(() => this.failed(owner));
      const rootUri = pathToFileURL(workspace).href;
      await owner.transport.request('initialize', {
        processId: process.pid,
        rootUri,
        workspaceFolders: [{uri: rootUri, name: path.basename(workspace)}],
        capabilities: {
          general: {positionEncodings: ['utf-16']},
          workspace: {configuration: true, workspaceFolders: true},
          textDocument: {diagnostic: {relatedDocumentSupport: false}},
        },
      });
      if (this.closed || generation !== this.generation) return;
      await owner.transport.notify('initialized', {});
      if (this.closed || generation !== this.generation) return;
      const snapshot = this.currentSnapshot;
      await owner.transport.notify('textDocument/didOpen', {
        textDocument: {uri: owner.uri, languageId: 'bsl', version: ++this.documentVersion, text: snapshot.text},
      });
      owner.opened = true;
      owner.sentSnapshot = snapshot;
      if (this.closed || generation !== this.generation) return;
      await this.synchronize();
    } catch {
      if (!this.closed && generation === this.generation) this.failed(owner);
    } finally {
      if (this.closed || generation !== this.generation) await this.releaseOwner();
    }
  }

  private async synchronize(): Promise<void> {
    const owner = this.owner;
    if (this.closed || !owner?.transport || !owner.opened || owner.generation !== this.generation) return;
    try {
      while (owner.sentSnapshot !== this.currentSnapshot && !this.closed && owner.generation === this.generation) {
        const snapshot = this.currentSnapshot;
        await owner.transport.notify('textDocument/didChange', {
          textDocument: {uri: owner.uri, version: ++this.documentVersion},
          contentChanges: [{text: snapshot.text}],
        });
        owner.sentSnapshot = snapshot;
      }
      if (!this.closed && owner.generation === this.generation) this.state = this.fileBatch ? 'updating' : 'ready';
    } catch { this.failed(owner); }
  }

  private failed(owner: Owner): void {
    if (this.closed || this.owner !== owner || owner.generation !== this.generation) return;
    this.markUnavailable();
    void this.enqueue(() => this.releaseOwner()).catch(() => {});
  }

  private markUnavailable(): void {
    // A failed generation cannot deliver its pending source batch. Settle and
    // detach it now so later structural events can schedule a fresh restart.
    this.discardFileBatch();
    ++this.generation;
    this.invalidate();
    this.state = 'unavailable';
    this.unavailableReason = 'BSL language server unavailable';
  }

  private async releaseOwner(): Promise<void> {
    const owner = this.owner;
    if (!owner) return;
    owner.subscription?.dispose();
    if (owner.transport) {
      if (owner.opened) {
        try { await owner.transport.notify('textDocument/didClose', {textDocument: {uri: owner.uri}}); }
        catch { /* Process loss must still finish owned cleanup. */ }
        owner.opened = false;
      }
      await owner.transport.close();
    }
    // The workspace is intentionally empty, distinct from the transport's private
    // configuration directory. Never write a .bsl file or recursively remove a root.
    if (owner.temporaryDirectory) await rmdir(owner.temporaryDirectory);
    // Retain ownership on a failed close: subsequent transitions must not launch
    // another child while the previous child may still be alive.
    this.owner = undefined;
  }
}
