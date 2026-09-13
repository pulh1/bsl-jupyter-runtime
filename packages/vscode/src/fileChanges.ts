import {lstatSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import type * as vscode from 'vscode';

export type FileEvent = Readonly<{uri: string; type: 1 | 2 | 3}>;

/** Bounded, per-path LSP changes. Structural evidence survives cancellation. */
export class FileChangeCoalescer {
  private readonly events = new Map<string, FileEvent>();
  private restart = false;

  push(event: FileEvent): void {
    if (event.type !== 2 && /\.(xml|mdo)$/i.test(new URL(event.uri).pathname)) this.restart = true;
    const previous = this.events.get(event.uri);
    if (!previous && this.events.size >= 4096) { this.restart = true; return; }
    // Delete/create is an atomic replacement, while create/change stays a create.
    const type = previous?.type === 3 && event.type === 1 ? 2
      : previous?.type === 1 && event.type === 2 ? 1 : event.type;
    this.events.set(event.uri, Object.freeze({uri: event.uri, type}));
  }

  flush(): {events: readonly FileEvent[]; restart: boolean} {
    const events = Object.freeze([...this.events.values()]);
    // Native 1.0.7 retains canonical module exports after watched-file deletion.
    // Inspect the coalesced event so atomic delete/create saves keep their child.
    const canonicalChange = events.some(event => event.type !== 2 && /\/(?:Module|ObjectModule|ManagerModule|RecordSetModule|FormModule|CommandModule)\.bsl$/i.test(new URL(event.uri).pathname));
    const result = {events, restart: this.restart || canonicalChange};
    this.events.clear(); this.restart = false;
    return result;
  }
}

/** Inspect every existing ancestor, including deleted events' surviving parents. */
export function isProjectFile(rootPath: string, uri: string): boolean {
  try {
    if (!path.isAbsolute(rootPath)) return false;
    const parsed = new URL(uri);
    if (parsed.protocol !== 'file:' || parsed.search || parsed.hash) return false;
    const candidate = fileURLToPath(parsed);
    if (!/\.(bsl|os|xml|mdo)$/i.test(candidate)) return false;
    const relative = path.relative(rootPath, candidate);
    if (!relative || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return false;
    let current = candidate;
    while (true) {
      try {
        const stat = lstatSync(current);
        if (stat.isSymbolicLink()) return false;
        if (current === candidate && !stat.isFile()) return false;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') return false;
      }
      const parent = path.dirname(current);
      if (parent === current) return true;
      current = parent;
    }
  } catch { return false; }
}

type SharedWatcher = {
  watcher: vscode.FileSystemWatcher;
  listeners: Set<(changes: readonly FileEvent[]) => void>;
};
const watchers = new Map<string, SharedWatcher>();

/** Subscribers receive the first valid event immediately; sessions own debounce. */
export function watchProject(root: vscode.Uri, onChange: (changes: readonly FileEvent[]) => void): vscode.Disposable {
  if (root.scheme !== 'file' || root.query || root.fragment || !path.isAbsolute(root.fsPath)) {
    throw new Error('Project watcher requires an explicit local source root.');
  }
  try {
    if (!lstatSync(root.fsPath).isDirectory()) throw new Error();
    let current = root.fsPath;
    while (true) {
      if (lstatSync(current).isSymbolicLink()) throw new Error();
      const parent = path.dirname(current);
      if (parent === current) break;
      current = parent;
    }
  } catch { throw new Error('Project watcher root must be an existing unlinked directory.'); }
  // Lazy loading keeps coalescing and path checks usable in pure Node tests.
  const api: typeof vscode = require('vscode');
  const key = process.platform === 'win32' ? path.normalize(root.fsPath).toLowerCase() : path.normalize(root.fsPath);
  let shared = watchers.get(key);
  if (!shared) {
    const watcher = api.workspace.createFileSystemWatcher(new api.RelativePattern(root, '**/*.{bsl,os,xml,mdo}'));
    shared = {watcher, listeners: new Set()};
    const owned = shared;
    const dispatch = (uri: vscode.Uri, type: FileEvent['type']): void => {
      const event = Object.freeze({uri: uri.toString(), type});
      if (!isProjectFile(root.fsPath, event.uri)) return;
      for (const listener of owned.listeners) {
        try { listener([event]); } catch { /* One notebook cannot disrupt another. */ }
      }
    };
    watcher.onDidCreate(uri => dispatch(uri, 1));
    watcher.onDidChange(uri => dispatch(uri, 2));
    watcher.onDidDelete(uri => dispatch(uri, 3));
    watchers.set(key, shared);
  }
  // Each call is its own reference, even when the callback is reused.
  const listener = (changes: readonly FileEvent[]): void => onChange(changes);
  shared.listeners.add(listener);
  const owned = shared;
  let disposed = false;
  return {dispose: () => {
    if (disposed) return;
    disposed = true;
    owned.listeners.delete(listener);
    if (!owned.listeners.size) { owned.watcher.dispose(); watchers.delete(key); }
  }};
}
