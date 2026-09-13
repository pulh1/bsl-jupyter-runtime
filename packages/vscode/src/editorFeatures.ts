import * as vscode from 'vscode';
import {fileURLToPath} from 'node:url';
import {NotebookSnapshot, Range} from './notebookModel';
import {SessionDiagnostic, SessionEpoch} from './notebookSession';
import {isKernelContextReceiver, LspCompletion, LspDiagnostic, mapCompletion, mapDefinition, mapDiagnostic, Markup, validRange} from './featureMapping';

export type BslEditorSession = {
  readonly snapshot: NotebookSnapshot;
  readonly epoch: SessionEpoch;
  readonly virtualUri: string;
  query<T>(method: string, cellUri: string, position: {line: number; character: number}, signal?: AbortSignal): Promise<T | undefined>;
  diagnose(): Promise<readonly SessionDiagnostic[] | undefined>;
  onDiagnostics(listener: (items: readonly SessionDiagnostic[]) => void): {dispose(): void};
};
type GetSession = (notebookUri: string) => BslEditorSession | undefined;
type GetSnapshot = (notebookUri: string) => NotebookSnapshot | undefined;
type EnsureSession = (notebookUri: string) => Promise<void>;
type CompletionReply = LspCompletion[] | {items: LspCompletion[]; isIncomplete?: boolean; itemDefaults?: {
  editRange?: Range | {insert: Range; replace: Range}; insertTextFormat?: number;
}};
type HoverReply = {contents: Markup | {language: string; value: string} | (Markup | {language: string; value: string})[]; range?: Range};
type SignatureReply = {signatures: {label: string; documentation?: Markup; parameters?: {label: string | [number, number]; documentation?: Markup}[]; activeParameter?: number}[]; activeSignature?: number; activeParameter?: number};
type DefinitionTarget = {uri: string; range: Range} | {targetUri: string; targetRange: Range; targetSelectionRange: Range};

export interface BslFeatures extends vscode.Disposable {
  /** Call after the coordinator finishes opening/updating a session or sources. */
  refreshDiagnostics(notebookUri: string): void;
  /** Detach diagnostics immediately on close, rename, or session eviction. */
  clearDiagnostics(notebookUri: string): void;
}

export function createBslProviders(getSession: GetSession, getSnapshot: GetSnapshot, ensureSession?: EnsureSession): {
  completion: vscode.CompletionItemProvider;
  hover: vscode.HoverProvider;
  signature: vscode.SignatureHelpProvider;
  definition: vscode.DefinitionProvider;
} {
  const capture = async (document: vscode.TextDocument, position: vscode.Position, token: vscode.CancellationToken) => {
    const notebook = notebookFor(document);
    if (!notebook || token.isCancellationRequested) return undefined;
    const key = notebook.uri.toString(), snapshot = getSnapshot(key);
    if (!snapshot || !snapshot.toVirtual(document.uri.toString(), position)) return undefined;
    const notebookVersion = notebook.version, documentVersion = document.version;
    await ensureSession?.(key);
    if (token.isCancellationRequested || document.isClosed || notebook.isClosed ||
      notebook.version !== notebookVersion || document.version !== documentVersion || getSnapshot(key) !== snapshot) return undefined;
    const session = getSession(key);
    if (!session || snapshot !== session.snapshot) return undefined;
    const epoch = session.epoch;
    const current = () => !token.isCancellationRequested && !document.isClosed && !notebook.isClosed &&
      notebook.version === notebookVersion && document.version === documentVersion &&
      getSession(key) === session && getSnapshot(key) === snapshot && session.snapshot === snapshot && sameEpoch(epoch, session.epoch);
    const query = async <T>(method: string): Promise<T | undefined> => {
      const abort = new AbortController();
      const subscription = token.onCancellationRequested(() => abort.abort());
      try {
        const reply = await session.query<T>(method, document.uri.toString(), position, abort.signal);
        return current() ? reply : undefined;
      } finally { subscription.dispose(); }
    };
    return {session, snapshot, epoch, current, query};
  };
  return {
    completion: {async provideCompletionItems(document, position, token) {
      if (position.line > 0 && isKernelContextReceiver(document.getText(new vscode.Range(new vscode.Position(1, 0), position)))) return undefined;
      const request = await capture(document, position, token);
      if (!request) return undefined;
      const reply = await request.query<CompletionReply>('textDocument/completion');
      if (!reply) return undefined;
      const defaults = Array.isArray(reply) ? undefined : reply.itemDefaults;
      const items = (Array.isArray(reply) ? reply : reply.items).flatMap(raw => {
        const item = {...raw, insertTextFormat: raw.insertTextFormat ?? defaults?.insertTextFormat};
        if (!item.textEdit && defaults?.editRange) {
          const edit = defaults.editRange;
          item.textEdit = {...('start' in edit ? {range: edit} : edit), newText: item.textEditText ?? item.insertText ?? item.label};
        }
        const defaultRange = document.getWordRangeAtPosition(position) ?? new vscode.Range(position, position);
        const mapped = mapCompletion(request.snapshot, document.uri.toString(), position, item, defaultRange);
        return mapped ? [completionItem(mapped)] : [];
      });
      return request.current() ? new vscode.CompletionList(items, !Array.isArray(reply) && !!reply.isIncomplete) : undefined;
    }},
    hover: {async provideHover(document, position, token) {
      const request = await capture(document, position, token);
      if (!request) return undefined;
      const reply = await request.query<HoverReply>('textDocument/hover');
      if (!reply) return undefined;
      const mapped = reply.range && validRange(reply.range) ? request.snapshot.toCell(reply.range) : undefined;
      if (reply.range && mapped?.uri !== document.uri.toString()) return undefined;
      const contents = (Array.isArray(reply.contents) ? reply.contents : [reply.contents]).map(content =>
        typeof content === 'object' && 'language' in content ? new vscode.MarkdownString().appendCodeblock(content.value, content.language) : markdown(content));
      return request.current() ? new vscode.Hover(contents, mapped ? vscodeRange(mapped.range) : undefined) : undefined;
    }},
    signature: {async provideSignatureHelp(document, position, token) {
      const request = await capture(document, position, token);
      if (!request) return undefined;
      const reply = await request.query<SignatureReply>('textDocument/signatureHelp');
      if (!reply) return undefined;
      const help = new vscode.SignatureHelp();
      help.signatures = reply.signatures.map(signature => {
        const result = new vscode.SignatureInformation(signature.label, signature.documentation === undefined ? undefined : markdown(signature.documentation));
        result.parameters = (signature.parameters ?? []).map(parameter => new vscode.ParameterInformation(parameter.label, parameter.documentation === undefined ? undefined : markdown(parameter.documentation)));
        result.activeParameter = signature.activeParameter;
        return result;
      });
      help.activeSignature = reply.activeSignature ?? 0; help.activeParameter = reply.activeParameter ?? 0;
      return request.current() ? help : undefined;
    }},
    definition: {async provideDefinition(document, position, token) {
      const request = await capture(document, position, token);
      if (!request) return undefined;
      const reply = await request.query<DefinitionTarget | DefinitionTarget[]>('textDocument/definition');
      if (!reply) return undefined;
      const targets = await Promise.all((Array.isArray(reply) ? reply : [reply]).map(async entry => {
        const target = 'targetUri' in entry ? {uri: entry.targetUri, range: entry.targetSelectionRange} : entry;
        if (!validRange(target.range)) return undefined;
        const mapped = target.uri === request.session.virtualUri ? request.snapshot.toCell(target.range)
          : request.epoch.rootKey ? await mapDefinition(request.snapshot, fileURLToPath(request.epoch.rootKey), target) : undefined;
        return mapped ? new vscode.Location(vscode.Uri.parse(mapped.uri), vscodeRange(mapped.range)) : undefined;
      }));
      return request.current() ? targets.filter((target): target is vscode.Location => !!target) : undefined;
    }},
  };
}

export function registerBslFeatures(context: Pick<vscode.ExtensionContext, 'subscriptions'>, getSession: GetSession, getSnapshot: GetSnapshot, ensureSession?: EnsureSession): BslFeatures {
  const providers = createBslProviders(getSession, getSnapshot, ensureSession);
  // With notebookType, VS Code matches scheme against the notebook URI.
  const selector: vscode.DocumentSelector = [{notebookType: 'jupyter-notebook', language: 'python', scheme: 'file'}];
  const diagnostics = vscode.languages.createDiagnosticCollection('onec-bsl-notebook');
  type Binding = {session: BslEditorSession; subscription: {dispose(): void}; timer?: NodeJS.Timeout; cells: Set<string>};
  const bindings = new Map<string, Binding>();
  let disposed = false;
  const erase = (binding: Binding) => { for (const cell of binding.cells) diagnostics.delete(vscode.Uri.parse(cell)); binding.cells.clear(); };
  const clearDiagnostics = (key: string) => {
    const binding = bindings.get(key);
    if (!binding) return;
    clearTimeout(binding.timer); binding.subscription.dispose(); erase(binding); bindings.delete(key);
  };
  const refreshDiagnostics = (key: string) => {
    clearDiagnostics(key);
    if (disposed) return;
    const session = getSession(key);
    if (!session) return;
    const publish = (items: readonly LspDiagnostic[]) => {
      erase(binding);
      const snapshot = getSnapshot(key);
      if (disposed || getSession(key) !== session || !snapshot || snapshot !== session.snapshot) return;
      const grouped = new Map<string, vscode.Diagnostic[]>();
      for (const item of items) {
        const mapped = mapDiagnostic(snapshot, item);
        if (!mapped) continue;
        const diagnostic = new vscode.Diagnostic(vscodeRange(mapped.range), mapped.message, mapped.severity && mapped.severity >= 1 && mapped.severity <= 4 ? mapped.severity - 1 : vscode.DiagnosticSeverity.Error);
        diagnostic.source = 'BSL';
        const entries = grouped.get(mapped.uri) ?? [];
        entries.push(diagnostic); grouped.set(mapped.uri, entries);
      }
      for (const [uri, entries] of grouped) { binding.cells.add(uri); diagnostics.set(vscode.Uri.parse(uri), entries); }
    };
    const binding: Binding = {session, cells: new Set(), subscription: session.onDiagnostics(publish)};
    bindings.set(key, binding);
    binding.timer = setTimeout(() => {
      binding.timer = undefined;
      if (bindings.get(key) !== binding || getSession(key) !== session || getSnapshot(key) !== session.snapshot) return;
      void session.diagnose(); // Session fences notebook/root/process/project revisions before publication.
    }, 150);
  };
  const disposables = [
    diagnostics,
    vscode.languages.registerCompletionItemProvider(selector, providers.completion, '.'),
    vscode.languages.registerHoverProvider(selector, providers.hover),
    vscode.languages.registerSignatureHelpProvider(selector, providers.signature, '(', ','),
    vscode.languages.registerDefinitionProvider(selector, providers.definition),
    vscode.workspace.onDidCloseNotebookDocument(notebook => clearDiagnostics(notebook.uri.toString())),
    // Raw editor events can precede the coordinator's asynchronous snapshot update.
    // Detach now; only an explicit post-synchronization refresh may pull/publish again.
    vscode.workspace.onDidChangeNotebookDocument(event => clearDiagnostics(event.notebook.uri.toString())),
    vscode.workspace.onDidChangeTextDocument(event => {
      const notebook = notebookFor(event.document);
      if (notebook) clearDiagnostics(notebook.uri.toString());
    }),
  ];
  const registration: BslFeatures = {refreshDiagnostics, clearDiagnostics, dispose() {
    if (disposed) return;
    disposed = true;
    for (const key of bindings.keys()) clearDiagnostics(key);
    for (const disposable of disposables) disposable.dispose();
  }};
  context.subscriptions.push(registration);
  for (const notebook of vscode.workspace.notebookDocuments) refreshDiagnostics(notebook.uri.toString());
  return registration;
}

function notebookFor(document: vscode.TextDocument): vscode.NotebookDocument | undefined {
  if (document.languageId !== 'python' || document.uri.scheme !== 'vscode-notebook-cell') return undefined;
  return vscode.workspace.notebookDocuments.find(notebook => notebook.notebookType === 'jupyter-notebook' && notebook.uri.scheme === 'file' &&
    notebook.getCells().some(cell => cell.kind === vscode.NotebookCellKind.Code && cell.document.uri.toString() === document.uri.toString()));
}
function sameEpoch(a: SessionEpoch, b: SessionEpoch): boolean {
  return a.notebookVersion === b.notebookVersion && a.rootKey === b.rootKey && a.processId === b.processId && a.projectRevision === b.projectRevision;
}
function vscodeRange(range: Range): vscode.Range { return new vscode.Range(range.start.line, range.start.character, range.end.line, range.end.character); }
function markdown(content: Markup): vscode.MarkdownString {
  if (typeof content === 'string') return new vscode.MarkdownString(content);
  return content.kind === 'markdown' ? new vscode.MarkdownString(content.value) : new vscode.MarkdownString().appendText(content.value);
}
function completionItem(item: LspCompletion): vscode.CompletionItem {
  const result = new vscode.CompletionItem(item.labelDetails ? {label: item.label, ...item.labelDetails} : item.label, item.kind === undefined ? undefined : item.kind - 1);
  result.detail = item.detail; result.sortText = item.sortText; result.filterText = item.filterText;
  result.preselect = item.preselect; result.commitCharacters = item.commitCharacters;
  result.command = item.command; result.keepWhitespace = item.keepWhitespace;
  if (item.documentation !== undefined) result.documentation = markdown(item.documentation);
  if (item.tags?.includes(1)) result.tags = [vscode.CompletionItemTag.Deprecated];
  const text = item.textEdit?.newText ?? item.insertText ?? item.label;
  result.insertText = item.insertTextFormat === 2 ? new vscode.SnippetString(text) : text;
  if (item.textEdit) result.range = 'range' in item.textEdit ? vscodeRange(item.textEdit.range)
    : {inserting: vscodeRange(item.textEdit.insert), replacing: vscodeRange(item.textEdit.replace)};
  result.additionalTextEdits = item.additionalTextEdits?.map(edit => new vscode.TextEdit(vscodeRange(edit.range), edit.newText));
  return result;
}
