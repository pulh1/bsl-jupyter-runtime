import * as vscode from 'vscode';
import {mkdtemp, rm, rmdir, stat, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {FileEvent} from './fileChanges';
import {NotebookSnapshot, Position, Range} from './notebookModel';
import {SessionDiagnostic, SessionEpoch, SessionStatus} from './notebookSession';

/** A notebook snapshot queried through the BSL extension already installed in VS Code. */
export class SharedBslSession {
  private currentSnapshot: NotebookSnapshot;
  private rootPath: string | undefined;
  private temporaryDirectory?: string;
  private temporaryUri?: vscode.Uri;
  private writtenText: string;
  private queue: Promise<void> = Promise.resolve();
  private revision = 0;
  private sourceRevision = 0;
  private generation = 1;
  private closed = false;
  private closing?: Promise<void>;
  private state: SessionStatus = 'indexing';
  private unavailableReason?: string;

  private constructor(readonly notebookUri: string, snapshot: NotebookSnapshot, root?: string) {
    this.currentSnapshot = snapshot;
    this.rootPath = root;
    this.writtenText = snapshot.text;
  }

  static async open(notebookUri: string, snapshot: NotebookSnapshot, root?: string): Promise<SharedBslSession> {
    const session = new SharedBslSession(notebookUri, snapshot, root);
    const extension = vscode.extensions.getExtension('1c-syntax.language-1c-bsl');
    if (!extension) throw new Error('Install the 1C BSL Language extension to provide notebook suggestions');
    await extension.activate();
    const directory = await mkdtemp(path.join(os.tmpdir(), 'onec-bsl-notebook-'));
    const uri = vscode.Uri.file(path.join(directory, 'Notebook.bsl'));
    try {await writeFile(uri.fsPath, snapshot.text, {encoding: 'utf8', mode: 0o600});}
    catch (error) {
      await rm(uri.fsPath, {force: true});
      await rmdir(directory);
      throw error;
    }
    session.temporaryDirectory = directory;
    session.temporaryUri = uri;
    session.state = 'ready';
    return session;
  }

  get snapshot(): NotebookSnapshot {return this.currentSnapshot;}
  get status(): SessionStatus {return this.state;}
  get reason(): string | undefined {return this.unavailableReason;}
  get virtualUri(): string {return this.temporaryUri?.toString() ?? '';}
  get epoch(): SessionEpoch {return Object.freeze({notebookVersion: this.currentSnapshot.version,
    rootKey: this.rootPath ? pathToFileURL(this.rootPath).href : '', processId: this.generation,
    projectRevision: this.sourceRevision});}

  // The installed client's public diagnostics have no document version. Never
  // mirror an older diagnostic into a newer notebook cell layout.
  onDiagnostics(_listener: (items: readonly SessionDiagnostic[]) => void): vscode.Disposable {return {dispose() {}};}
  async diagnose(): Promise<readonly SessionDiagnostic[]> {return [];}

  update(snapshot: NotebookSnapshot): Promise<void> {
    if (this.closed || snapshot === this.currentSnapshot) return Promise.resolve();
    this.currentSnapshot = snapshot;
    this.invalidate();
    this.state = 'updating';
    return this.enqueue(async () => {
      const uri = this.temporaryUri;
      if (this.closed || !uri) return;
      const latest = this.currentSnapshot.text;
      if (this.writtenText !== latest) {
        try {await writeFile(uri.fsPath, latest, 'utf8'); this.writtenText = latest;}
        catch (error) {this.markUnavailable(); throw error;}
      }
      if (!this.closed) this.state = 'ready';
    });
  }
  setRoot(root: string | undefined): Promise<void> {
    if (this.closed || root === this.rootPath) return Promise.resolve();
    this.rootPath = root;
    ++this.generation;
    this.invalidate();
    return Promise.resolve();
  }
  filesChanged(changes: readonly FileEvent[]): Promise<void> {
    if (this.closed || !changes.length) return Promise.resolve();
    ++this.sourceRevision;
    this.invalidate();
    return Promise.resolve();
  }

  async query<T>(method: string, cellUri: string, position: Position, signal?: AbortSignal): Promise<T | undefined> {
    const revision = this.revision, snapshot = this.currentSnapshot;
    const mapped = snapshot.toVirtual(cellUri, position);
    if (!mapped || this.closed || signal?.aborted) return undefined;
    return this.enqueue(async () => {
      const uri = this.temporaryUri;
      if (!uri || this.closed || signal?.aborted || this.state !== 'ready' || revision !== this.revision || this.writtenText !== snapshot.text) return undefined;
      if (!await this.fileAvailable(uri) || signal?.aborted) return undefined;
      const point = new vscode.Position(mapped.line, mapped.character);
      let result: unknown;
      try {
        if (method === 'textDocument/completion') {
          const reply = await vscode.commands.executeCommand<vscode.CompletionList>(
            'vscode.executeCompletionItemProvider', uri, point, undefined, 8);
          result = reply && {isIncomplete: reply.isIncomplete, items: reply.items.map(completion)};
        } else if (method === 'textDocument/hover') {
          const reply = await vscode.commands.executeCommand<vscode.Hover[]>('vscode.executeHoverProvider', uri, point);
          if (reply?.length) {
            const firstRange = reply[0].range;
            const commonRange = reply.every(hover => !hover.range || !!firstRange?.isEqual(hover.range));
            result = {contents: reply.flatMap(hover => hover.contents.map(content => markup(content))),
              range: commonRange && firstRange ? range(firstRange) : undefined};
          }
        } else if (method === 'textDocument/signatureHelp') {
          const reply = await vscode.commands.executeCommand<vscode.SignatureHelp>('vscode.executeSignatureHelpProvider', uri, point);
          result = reply && {activeSignature: reply.activeSignature, activeParameter: reply.activeParameter,
            signatures: reply.signatures.map(signature => ({label: signature.label, activeParameter: signature.activeParameter,
              documentation: signature.documentation && markup(signature.documentation),
              parameters: signature.parameters.map(parameter => ({label: parameter.label,
                documentation: parameter.documentation && markup(parameter.documentation)}))}))};
        } else if (method === 'textDocument/definition') {
          const reply = await vscode.commands.executeCommand<(vscode.Location | vscode.LocationLink)[]>('vscode.executeDefinitionProvider', uri, point);
          result = reply?.map(location => 'targetUri' in location
            ? {targetUri: location.targetUri.toString(), targetRange: range(location.targetRange),
                targetSelectionRange: range(location.targetSelectionRange ?? location.targetRange)}
            : {uri: location.uri.toString(), range: range(location.range)});
        }
      } catch {await this.fileAvailable(uri); return undefined;}
      return !this.closed && revision === this.revision && !signal?.aborted && this.temporaryUri === uri ? result as T : undefined;
    });
  }

  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.closed = true;
    this.state = 'unavailable';
    this.invalidate();
    this.closing = this.enqueue(async () => {
      const uri = this.temporaryUri, directory = this.temporaryDirectory;
      if (!uri || !directory) return;
      await rm(uri.fsPath, {force: true});
      try {await rmdir(directory);} catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      }
      this.temporaryUri = undefined;
      this.temporaryDirectory = undefined;
    }).catch(error => {this.closing = undefined; throw error;});
    return this.closing;
  }

  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const work = this.queue.then(operation);
    this.queue = work.then(() => {}, () => {});
    return work;
  }
  private async fileAvailable(uri: vscode.Uri): Promise<boolean> {
    try {if ((await stat(uri.fsPath)).isFile()) return true;} catch { /* Treat missing or unreadable files as unavailable. */ }
    this.markUnavailable();
    return false;
  }
  private markUnavailable(): void {
    if (this.closed) return;
    this.state = 'unavailable';
    this.unavailableReason = 'Temporary BSL file unavailable; reopening on the next request';
    this.invalidate();
  }
  private invalidate(): void {++this.revision;}
}

function range(value: vscode.Range): Range {return {start: {line: value.start.line, character: value.start.character},
  end: {line: value.end.line, character: value.end.character}};}
function markup(value: vscode.MarkdownString | string | {language: string; value: string}): string | {kind: string; value: string} {
  if (typeof value === 'string') return value;
  if ('language' in value) return {kind: 'markdown', value: `\`\`\`${value.language}\n${value.value}\n\`\`\``};
  return {kind: 'markdown', value: value.value};
}
function completion(item: vscode.CompletionItem): object {
  const label = typeof item.label === 'string' ? item.label : item.label.label;
  const text = item.insertText instanceof vscode.SnippetString ? item.insertText.value : item.insertText ?? label;
  const edit = item.range instanceof vscode.Range ? {range: range(item.range), newText: text}
    : item.range && {insert: range(item.range.inserting), replace: range(item.range.replacing), newText: text};
  return {label, labelDetails: typeof item.label === 'string' ? undefined : {detail: item.label.detail, description: item.label.description},
    kind: item.kind === undefined ? undefined : item.kind + 1, detail: item.detail,
    documentation: item.documentation && markup(item.documentation), sortText: item.sortText,
    filterText: item.filterText, preselect: item.preselect, commitCharacters: item.commitCharacters,
    tags: item.tags, command: item.command, keepWhitespace: item.keepWhitespace,
    insertTextFormat: item.insertText instanceof vscode.SnippetString ? 2 : 1,
    insertText: edit ? undefined : text, textEdit: edit,
    additionalTextEdits: item.additionalTextEdits?.map(value => ({range: range(value.range), newText: value.newText}))};
}
