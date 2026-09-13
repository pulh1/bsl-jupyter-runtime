import * as vscode from 'vscode';
import {realpath} from 'node:fs/promises';
import path from 'node:path';
import {validateSourceRoot} from './projectRoot';
import {NotebookCoordinator} from './notebookCoordinator';
import {NotebookSnapshot} from './notebookModel';
import {SharedBslSession} from './sharedBslSession';
import {registerBslFeatures, BslFeatures} from './editorFeatures';
import {watchProject} from './fileChanges';

type SourceDirectoryPicker = () => Thenable<readonly vscode.Uri[] | undefined>;
type SelectionErrorReporter = (message: string) => void;
type RootSelection = {set(uri: string, root: string): void | Promise<void>};
let coordinator: NotebookCoordinator<SharedBslSession> | undefined;
let statusItem: vscode.StatusBarItem | undefined;
let features: BslFeatures | undefined;
let timer: NodeJS.Timeout | undefined;

export function activate(context: vscode.ExtensionContext): void {
  statusItem = vscode.window.createStatusBarItem('onec-bsl.notebook', vscode.StatusBarAlignment.Right, 50);
  statusItem.name = 'BSL notebook sources'; statusItem.command = 'onec-bsl.selectSources';
  coordinator = new NotebookCoordinator((uri, snapshot, root) => SharedBslSession.open(uri, snapshot, root), {
    watch: (root, listener) => watchProject(vscode.Uri.file(root), listener),
    checkRoot: async root => {await validateSourceRoot(root);},
    refreshDiagnostics: uri => features?.refreshDiagnostics(uri),
    clearDiagnostics: uri => features?.clearDiagnostics(uri), changed: renderStatus,
  });
  const owner = coordinator;
  // Raw editor diagnostic invalidation is registered before synchronization handlers.
  features = registerBslFeatures(context, uri => owner.sessionFor(uri), uri => owner.snapshotFor(uri), uri => owner.ensureSession(uri));
  const synchronize = (notebook: vscode.NotebookDocument): void => {
    if (supported(notebook)) run(owner.update(notebook.uri.toString(), snapshot(notebook), startupMode() === 'onNotebookOpen'));
  };
  const select = (): Promise<void> => selectSourceRootForNotebook(vscode.window.activeNotebookEditor?.notebook);
  context.subscriptions.push(statusItem,
    vscode.commands.registerCommand('onec-bsl.selectSources', select),
    vscode.commands.registerCommand('onec-bsl.changeSources', select),
    vscode.commands.registerCommand('onec-bsl.clearSources', () => {
      const notebook = vscode.window.activeNotebookEditor?.notebook;
      if (notebook) return owner.setRoot(notebook.uri.toString(), undefined, startupMode() === 'onNotebookOpen');
    }),
    vscode.workspace.onDidChangeConfiguration(event => {
      if (!event.affectsConfiguration('onecBsl.serverStartup')) return;
      if (startupMode() === 'onNotebookOpen') {
        for (const notebook of vscode.workspace.notebookDocuments) synchronize(notebook);
      }
      renderStatus();
    }),
    vscode.workspace.onDidOpenNotebookDocument(synchronize),
    vscode.workspace.onDidChangeNotebookDocument(event => synchronize(event.notebook)),
    vscode.workspace.onDidChangeTextDocument(event => {
      if (event.document.uri.scheme !== 'vscode-notebook-cell') return;
      const notebook = vscode.workspace.notebookDocuments.find(notebook => notebook.getCells().some(cell => cell.document === event.document));
      if (notebook) synchronize(notebook);
    }),
    vscode.workspace.onDidCloseNotebookDocument(notebook => run(owner.close(notebook.uri.toString()))),
    vscode.workspace.onDidRenameFiles(event => {
      for (const file of event.files) run(owner.rename(file.oldUri.toString(), file.newUri.toString()).then(() => {
        const renamed = vscode.workspace.notebookDocuments.find(notebook => notebook.uri.toString() === file.newUri.toString());
        if (renamed) synchronize(renamed);
      }));
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => {
      for (const notebook of vscode.workspace.notebookDocuments) {
        if (!supported(notebook)) continue;
        const key = notebook.uri.toString();
        run((async () => {
          const root = owner.rootFor(key);
          const eager = startupMode() === 'onNotebookOpen';
          if (root && !await insidePrimaryWorkspace(root)) await owner.setRoot(key, undefined, eager);
          await owner.restart(key, eager);
        })());
      }
      renderStatus();
    }),
    vscode.window.onDidChangeActiveNotebookEditor(editor => {
      if (editor && supported(editor.notebook) && startupMode() === 'onNotebookOpen') run(owner.ensureSession(editor.notebook.uri.toString()));
      renderStatus();
    }),
    {dispose: () => {clearInterval(timer); timer = undefined;}},
  );
  for (const notebook of vscode.workspace.notebookDocuments) synchronize(notebook);
  let ticks = 0;
  timer = setInterval(() => {
    renderStatus(); // Session availability can change independently of editor events.
    if (++ticks % 30 === 0) run(owner.expireIdle().then(() => owner.recheckRoots()));
  }, 1000);
  renderStatus();
}

export async function deactivate(): Promise<void> {
  clearInterval(timer); timer = undefined;
  features?.dispose(); features = undefined; statusItem?.dispose(); statusItem = undefined;
  const owner = coordinator; coordinator = undefined; await owner?.dispose();
}
export function getNotebookCoordinator(): NotebookCoordinator<SharedBslSession> | undefined {return coordinator;}
export function getOpenNotebookRoot(notebookUri: string): string | undefined {return coordinator?.rootFor(notebookUri);}

export async function selectSourceRootForNotebook(
  notebook: vscode.NotebookDocument | undefined,
  notebookRoots?: RootSelection,
  pickDirectory: SourceDirectoryPicker = showDirectoryPicker,
  reportError: SelectionErrorReporter = showSelectionError,
): Promise<void> {
  if (!notebook) {reportError('Open a notebook before selecting project sources.'); return;}
  if (!supported(notebook)) {reportError('Project sources can be selected only for a local Jupyter notebook.'); return;}
  const [picked] = (await pickDirectory()) ?? [];
  if (!picked) return;
  if (picked.scheme !== 'file') {reportError('Project sources must be selected from the local file system.'); return;}
  try {
    const selected = await validateSourceRoot(picked.fsPath);
    if (!await insidePrimaryWorkspace(selected.canonicalPath)) {
      reportError('Open the folder containing these sources as the first VS Code workspace folder. The shared BSL server cannot index sources outside it.');
      return;
    }
    if (notebook.isClosed) return; // A picker can outlive its notebook.
    if (notebookRoots) await notebookRoots.set(notebook.uri.toString(), selected.canonicalPath);
    else await coordinator?.setRoot(notebook.uri.toString(), selected.canonicalPath, startupMode() === 'onNotebookOpen');
  } catch (error) {reportError(error instanceof Error ? error.message : 'Project sources could not be validated.');}
}
function supported(notebook: vscode.NotebookDocument): boolean {return notebook.notebookType === 'jupyter-notebook' && notebook.uri.scheme === 'file';}
function startupMode(): 'onDemand' | 'onNotebookOpen' {
  return vscode.workspace.getConfiguration('onecBsl').get('serverStartup') === 'onNotebookOpen' ? 'onNotebookOpen' : 'onDemand';
}
function snapshot(notebook: vscode.NotebookDocument): NotebookSnapshot {
  return NotebookSnapshot.fromCells(notebook.getCells().map(cell => ({uri: cell.document.uri.toString(),
    kind: cell.kind === vscode.NotebookCellKind.Code ? 'code' : 'markup', languageId: cell.document.languageId, text: cell.document.getText()})), notebook.version);
}
function renderStatus(): void {
  const notebook = vscode.window.activeNotebookEditor?.notebook;
  if (!statusItem || !coordinator || !notebook || !supported(notebook)) {statusItem?.hide(); return;}
  const state = coordinator.statusFor(notebook.uri.toString());
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri;
  const label = state.status === 'no root' ? workspaceRoot?.scheme === 'file' ? 'workspace' : 'no workspace' : state.status;
  statusItem.text = `BSL: ${label}${state.root ? ` | ${state.root}` : ''}`;
  statusItem.tooltip = `Shared BSL server workspace: ${workspaceRoot?.scheme === 'file' ? workspaceRoot.fsPath : 'none'}\nSelected sources: ${state.root ?? 'none'}\nStartup: ${startupMode() === 'onDemand' ? 'on first BSL request' : 'on notebook open'}\n${state.reason ?? (state.status === 'idle' ? 'Use BSL completion, hover, signature help, or definition to start language services.' : state.status === 'ready' || state.status === 'no root' ? 'Suggestions come from the installed BSL extension; indexing may still be running.' : state.status)}\nClick to select project sources within the first workspace folder.`;
  statusItem.show();
}
function run(work: Promise<void>): void {void work.catch(() => {void vscode.window.showErrorMessage('BSL notebook service could not finish cleanup.');});}
function showDirectoryPicker(): Thenable<readonly vscode.Uri[] | undefined> {
  return vscode.window.showOpenDialog({canSelectFolders: true, canSelectFiles: false, canSelectMany: false});
}
function showSelectionError(message: string): void {void vscode.window.showErrorMessage(message);}
async function insidePrimaryWorkspace(source: string): Promise<boolean> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder || folder.uri.scheme !== 'file') return false;
  try {
    const root = await realpath(folder.uri.fsPath);
    const relative = path.relative(root, source);
    return relative === '' || relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
  } catch {return false;}
}
