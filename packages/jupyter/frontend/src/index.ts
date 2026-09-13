import { bsl } from '@1c-syntax/codemirror-lang-bsl';
import { EditorState, Prec } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import type { JupyterFrontEndPlugin } from '@jupyterlab/application';
import {
  EditorExtensionRegistry,
  IEditorExtensionRegistry,
  IEditorLanguageRegistry
} from '@jupyterlab/codemirror';
import { bslMagic } from './magic';
import { ILSPCodeExtractorsManager, ILSPDocumentConnectionManager, IWidgetLSPAdapterTracker, ILSPFeatureManager,
  type WidgetLSPAdapter, type VirtualDocument } from '@jupyterlab/lsp';
import { INotebookTracker, type NotebookPanel } from '@jupyterlab/notebook';
import { ServerConnection } from '@jupyterlab/services';
import { IDocumentManager, IDocumentWidgetOpener } from '@jupyterlab/docmanager';
import { bslExtractor } from './extractor';
import { NotebookRuntimeBinding, findNotebookAdapter, type Endpoint } from './runtimeBinding';
import { formatRuntimeStatus } from './runtimeStatus';
import { Widget } from '@lumino/widgets';
import { ICompletionProviderManager, type CompletionProviderManager } from '@jupyterlab/completer';
import { RuntimeResponseFence, fenceCompletionProvider } from './runtimeFence';
import { withBslKernelCompletion } from './kernelCompletion';
import type { FileEditor } from '@jupyterlab/fileeditor';
import type { IDocumentWidget } from '@jupyterlab/docregistry';
import { SourceViewer, SourceDrive, sourceViewerPath, SOURCE_FALLBACK_PREFIX } from './sourceViewer';
import { disconnectedHoverGuard } from './hoverGuard';
import { guardDisposedBslDiagnostics } from './diagnosticGuard';
import type { Document as LSPDocument, IEditorPosition } from '@jupyterlab/lsp';

const plugin: JupyterFrontEndPlugin<void> = {
  id: '@onec-interactive/jupyter-bsl:highlighting',
  autoStart: true,
  requires: [IEditorLanguageRegistry, IEditorExtensionRegistry],
  activate: (_app, languages: IEditorLanguageRegistry, extensions: IEditorExtensionRegistry) => {
    if (!languages.findByMIME('text/x-bsl')) {
      languages.addLanguage({
        name: 'bsl',
        displayName: 'BSL (1C / OneScript)',
        mime: 'text/x-bsl',
        extensions: ['bsl', 'os'],
        support: bsl()
      });
    }
    extensions.addExtension({
      name: 'onec-bsl-magic',
      factory: ({model}) => {
        // Shared code cells include notebook and console cells, but not
        // Markdown/raw cells or file editors containing literal magic text.
        const shared = model.sharedModel;
        if (!('cell_type' in shared) || shared.cell_type !== 'code') {
          return null;
        }
        return EditorExtensionRegistry.createImmutableExtension(bslMagic(shared.getSource()));
      }
    });
  }
};

const lspPlugin: JupyterFrontEndPlugin<void> = {
  id: '@onec-interactive/jupyter-bsl:lsp',
  autoStart: true,
  optional: [ILSPCodeExtractorsManager, ILSPDocumentConnectionManager, INotebookTracker, IDocumentManager,
    IWidgetLSPAdapterTracker, ILSPFeatureManager, ICompletionProviderManager, IDocumentWidgetOpener],
  activate: (app, extractors: ILSPCodeExtractorsManager | null,
             connections: ILSPDocumentConnectionManager | null, notebooks: INotebookTracker | null,
             documents: IDocumentManager | null, adapters: IWidgetLSPAdapterTracker | null,
             features: ILSPFeatureManager | null, completions: ICompletionProviderManager | null,
             opener: IDocumentWidgetOpener | null) => {
    extractors?.register(bslExtractor, 'python');
    const services = documents?.services ?? app.serviceManager;
    const endpoint: Endpoint = async (method, path, body, signal) => {
      const url = services.serverSettings.baseUrl.replace(/\/$/, '') + '/' + path;
      const response = await ServerConnection.makeRequest(url,
        {method, body: body === undefined ? undefined : JSON.stringify(body), signal}, services.serverSettings);
      if (!response.ok) throw Object.assign(new Error('BSL service unavailable'), {status: response.status});
      return response.status === 204 ? null : response.json();
    };
    const viewers = new Map<object, {viewer: SourceViewer; canonical: string | null}>();
    services.contents.addDrive(new SourceDrive({
      name: 'onec-bsl', apiEndpoint: 'onec-bsl/sources',
      serverSettings: services.serverSettings
    }, canonical => {
      for (const entry of viewers.values()) if (entry.canonical === canonical) entry.viewer.unavailable();
    }));
    opener?.opened.connect((_sender, widget) => { void viewers.get(widget)?.viewer.refresh(); });
    app.docRegistry.addWidgetExtension('Editor', {
      createNew(widget, context) {
        const fallback = context.path.startsWith(SOURCE_FALLBACK_PREFIX);
        if (!context.path.startsWith('onec-bsl:') && !fallback) return {isDisposed: false, dispose() {}};
        const source = sourceViewerPath(context.path);
        const editor = (widget as IDocumentWidget<FileEditor>).content.editor;
        context.model.readOnly = true;
        editor.injectExtension(Prec.highest([EditorState.readOnly.of(true), EditorView.editable.of(false)]));
        const status = new Widget({node: document.createElement('span')});
        status.addClass('onec-bsl-source-status'); status.node.setAttribute('role', 'status');
        (widget as IDocumentWidget<FileEditor>).toolbar.insertItem(0, 'onec-bsl-source-status', status);
        if (fallback) {
          status.node.textContent = 'BSL: source unavailable · read-only';
          if (source) for (const entry of viewers.values()) {
            if (entry.canonical === source.canonical) entry.viewer.unavailable();
          }
          // The reserved server route always rejects this synthetic path. Never
          // issue an extra default-drive read or try to dispose its pending context.
          return {isDisposed:false, dispose() {}};
        }
        const viewer = new SourceViewer(context, state => {
          status.node.textContent = state === 'available' ? 'BSL: current file · read-only' :
            state === 'pending' ? 'BSL: refreshing current file' :
            'BSL: source unavailable · displayed content is not current';
        });
        viewers.set(widget, {viewer, canonical:source?.canonical ?? null});
        widget.disposed.connect(() => { viewer.dispose(); viewers.delete(widget); });
        void viewer.refresh();
        return viewer;
      }
    });
    if (connections && notebooks && adapters) {
      const guardDiagnostics = () => {
        const feature = features?.features.find(item => item.id.endsWith(':diagnostics')) as
          {handleDiagnostic?: (...args: any[]) => any} | undefined;
        if (typeof feature?.handleDiagnostic === 'function') {
          guardDisposedBslDiagnostics(feature as {handleDiagnostic: (...args: any[]) => any});
        }
      };
      guardDiagnostics();
      features?.featureRegistered.connect(guardDiagnostics);
      const guardedEditors = new WeakSet<object>();
      const hoverGuard = (adapter: WidgetLSPAdapter, accessor: LSPDocument.IEditor) =>
        Prec.highest(EditorView.domEventHandlers({mousemove: event => {
          const editor = accessor.getEditor();
          const root = adapter.virtualDocument;
          if (!editor || !root || root.isDisposed) return false;
          const position = editor.getPositionForCoordinate({left: event.clientX, right: event.clientX,
            top: event.clientY, bottom: event.clientY});
          const source = position && root.transformFromEditorToRoot(accessor,
            {line: position.line, ch: position.column} as IEditorPosition);
          const document = source && root.documentAtSourcePosition(source);
          return disconnectedHoverGuard({notebook: notebooks.has(adapter.widget as NotebookPanel),
            buttons: event.buttons, uri: document?.uri ?? null,
            ready: document ? !!connections.connections.get(document.uri)?.isReady : false});
        }}));
      features?.register({id: '@onec-interactive/jupyter-bsl:hover-guard', extensionFactory: {
        name: 'onec-bsl-hover-guard', factory: ({widgetAdapter, editor}) => {
          const codeEditor = editor.getEditor();
          if (codeEditor) guardedEditors.add(codeEditor);
          return EditorExtensionRegistry.createImmutableExtension(hoverGuard(widgetAdapter, editor));
        }
      }});
      const controllers = new Map<NotebookPanel, NotebookRuntimeBinding>();
      const fences = new Map<string, RuntimeResponseFence>();
      let decorated = false;
      const decorate = () => {
        const manager = completions as (ICompletionProviderManager & Partial<Pick<CompletionProviderManager, 'getProviders'>>) | null;
        if (decorated || typeof manager?.getProviders !== 'function') return;
        const providers = manager.getProviders();
        const provider = providers.get('lsp');
        const kernelProvider = providers.get('CompletionProvider:kernel');
        if (!provider || !kernelProvider) return;
        providers.set('lsp', fenceCompletionProvider(provider, context => {
          const source = context.editor?.model.sharedModel;
          return source && 'cell_type' in source && source.cell_type === 'code' &&
            bslExtractor.hasForeignCode(source.getSource(), 'code') ? fences.get(context.widget.id) : undefined;
        }));
        providers.set('CompletionProvider:kernel', withBslKernelCompletion(kernelProvider));
        decorated = true;
        notebooks.forEach(notebook => {
          if (!notebook.isDisposed) void manager.updateCompleter({widget: notebook,
            editor: notebook.content.activeCell?.editor, session: notebook.sessionContext.session});
        });
      };
      const initialize = (notebook: NotebookPanel) => {
        if (controllers.has(notebook)) return;
        const status = new Widget({node: document.createElement('span')});
        status.addClass('onec-bsl-runtime-status'); status.node.setAttribute('role', 'status');
        notebook.toolbar.insertItem(0, 'onec-bsl-runtime-status', status);
        let adapter: WidgetLSPAdapter | undefined;
        let root: VirtualDocument | null = null;
        const responseFence = new RuntimeResponseFence();
        fences.set(notebook.id, responseFence);
        const diagnosticListeners = new Map<VirtualDocument, () => void>();
        const diagnosticFeature = () => features?.features.find(f => f.id.endsWith(':diagnostics')) as
          {handleDiagnostic?: (response: any, document: VirtualDocument, adapter: WidgetLSPAdapter) => unknown} | undefined;
        const clearDiagnostics = (doc: VirtualDocument, response: any) => {
          const feature = diagnosticFeature();
          if (adapter && typeof feature?.handleDiagnostic === 'function') {
            void feature.handleDiagnostic(response, doc, adapter);
          } else {
            status.node.textContent = 'BSL: unavailable · diagnostics integration unsupported';
          }
        };
        const bslDocuments = () => {
          const result: VirtualDocument[] = [];
          const visit = (doc: VirtualDocument) => {
            if (doc.language === 'bsl' && !doc.isDisposed) result.push(doc);
            doc.foreignDocuments.forEach(visit);
          };
          if (adapter?.virtualDocument) visit(adapter.virtualDocument);
          return result;
        };
        const fence = (authorityReady: boolean) => {
          responseFence.advance(authorityReady);
          // Public document version advance fences gateway requests without touching
          // the shared connection. Preserve the latest virtual text during rebinding.
          for (const doc of bslDocuments()) {
            const connection = connections.connections.get(doc.uri);
            if (connection?.isReady) {
              // A just-opened document can still have its initial sent version.
              // Skip a number so this is always an actual server version fence.
              doc.documentInfo.version++;
              connection.sendFullTextChange(doc.value, doc.documentInfo);
            }
            clearDiagnostics(doc, {uri: doc.documentInfo.uri, version: doc.documentInfo.version - 1, diagnostics: []});
          }
          if (decorated && completions) void completions.updateCompleter({widget: notebook,
            editor: notebook.content.activeCell?.editor, session: notebook.sessionContext.session});
        };
        const controller = new NotebookRuntimeBinding(notebook.id, endpoint, fence, () => {
          decorate();
          const unsupported = !decorated ? 'completion' :
            typeof diagnosticFeature()?.handleDiagnostic !== 'function' ? 'diagnostics' : null;
          status.node.textContent = unsupported ? `BSL: unavailable · ${unsupported} integration unsupported` :
            formatRuntimeStatus(controller.status);
        }, undefined, (uri, binding_id) => {
          const doc = bslDocuments().find(doc => doc.documentInfo.uri === uri);
          const connection = doc && connections.connections.get(doc.uri);
          // Public sending is a no-op before ready. Authorized status polling retries
          // on this document's current connection after initialization/reconnect.
          if (connection?.isReady) connection.sendConfigurationChange({settings:{onecProjectBinding:{document_uri:uri, binding_id}}});
        });
        controllers.set(notebook, controller);
        const refresh = (force = false) => {
          if (notebook.isDisposed) return;
          if (!adapter) { void attach().catch(() => {}); return; }
          decorate();
          for (const [doc, disconnect] of diagnosticListeners) {
            if (doc.isDisposed || !bslDocuments().includes(doc)) {
              disconnect(); diagnosticListeners.delete(doc);
            }
          }
          for (const doc of bslDocuments()) {
            if (diagnosticListeners.has(doc)) continue;
            const connection = connections.connections.get(doc.uri);
            if (!connection) continue;
            const signal = connection.serverNotifications['textDocument/publishDiagnostics'];
            const listener = (_sender: unknown, response: any) => {
              if (response.uri === doc.documentInfo.uri) responseFence.diagnostic(response,
                () => doc.documentInfo.version - 1, accepted => clearDiagnostics(doc, accepted));
            };
            signal.connect(listener);
            diagnosticListeners.set(doc, () => signal.disconnect(listener));
          }
          void controller.update(notebook.context.path, notebook.sessionContext.session?.kernel?.id ?? null,
            bslDocuments().map(doc => doc.documentInfo.uri).filter(Boolean), force);
        };
        const documentChanged = () => refresh();
        const attach = async () => {
          await notebook.context.ready;
          if (notebook.isDisposed) return;
          // jupyterlab-lsp 5.3 registers only in the older public manager map.
          // Its keys are paths; always match widget identity for this fallback.
          const candidate = findNotebookAdapter(notebook, adapters, connections.adapters);
          if (!candidate) return;
          await candidate.ready;
          if (notebook.isDisposed || candidate.isDisposed) return;
          adapter = candidate;
          // Public injection also covers adapters/editors created before our
          // feature registered. It does not repair an already-poisoned hover
          // instance; upgrades require the documented browser reload.
          for (const {ceEditor: accessor} of candidate.editors) {
            const editor = accessor.getEditor();
            if (editor && !guardedEditors.has(editor)) {
              editor.injectExtension(hoverGuard(candidate, accessor));
              guardedEditors.add(editor);
            }
          }
          if (root !== candidate.virtualDocument) {
            root?.foreignDocumentOpened.disconnect(documentChanged);
            root?.foreignDocumentClosed.disconnect(documentChanged);
            root = candidate.virtualDocument;
            root?.foreignDocumentOpened.connect(documentChanged);
            root?.foreignDocumentClosed.connect(documentChanged);
          }
          // Bootstrap saved BSL cells even when there is no Python language server.
          await candidate.updateDocuments(); refresh();
        };
        const adapterChanged = (_sender: unknown, candidate: WidgetLSPAdapter) => {
          if (candidate.widget === notebook) void attach().catch(() => {});
        };
        const kernelChanged = () => refresh(true);
        const pathChanged = () => { refresh(true); void attach().catch(() => {}); };
        const kernelStatus = (_sender: unknown, value: string) => {
          if (['restarting', 'autorestarting', 'dead', 'terminating'].includes(value)) refresh(true);
        };
        adapters.adapterAdded.connect(adapterChanged); adapters.adapterUpdated.connect(adapterChanged);
        connections.documentsChanged.connect(documentChanged);
        connections.connected.connect(documentChanged);
        connections.initialized.connect(documentChanged);
        notebook.context.pathChanged.connect(pathChanged);
        notebook.sessionContext.kernelChanged.connect(kernelChanged);
        notebook.sessionContext.statusChanged.connect(kernelStatus);
        notebook.disposed.connect(() => {
          controller.dispose(); controllers.delete(notebook);
          responseFence.dispose(); fences.delete(notebook.id);
          diagnosticListeners.forEach(disconnect => disconnect()); diagnosticListeners.clear();
          adapters.adapterAdded.disconnect(adapterChanged); adapters.adapterUpdated.disconnect(adapterChanged);
          connections.documentsChanged.disconnect(documentChanged);
          connections.connected.disconnect(documentChanged);
          connections.initialized.disconnect(documentChanged);
          notebook.context.pathChanged.disconnect(pathChanged);
          notebook.sessionContext.kernelChanged.disconnect(kernelChanged);
          notebook.sessionContext.statusChanged.disconnect(kernelStatus);
          root?.foreignDocumentOpened.disconnect(documentChanged);
          root?.foreignDocumentClosed.disconnect(documentChanged);
        });
        void attach().catch(() => {});
      };
      notebooks.widgetAdded.connect((_tracker, notebook) => initialize(notebook));
      notebooks.forEach(initialize);
    }
  }
};

export default [plugin, lspPlugin];
