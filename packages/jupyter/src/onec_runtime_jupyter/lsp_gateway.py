"""One authenticated transport; isolated, lazily allocated notebook children."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
import inspect
import os
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

DEFAULT_MAX_CHILDREN = 8
DEFAULT_MAX_REQUESTS = 128
DEFAULT_MAX_RESOLVE_ITEMS = 4096
DEFAULT_IDLE_SECONDS = 300
DEFAULT_MAX_DOCUMENTS = 32
DEFAULT_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
READ_METHODS = frozenset({
    'textDocument/completion', 'textDocument/hover', 'textDocument/signatureHelp',
    'textDocument/definition', 'textDocument/declaration', 'textDocument/typeDefinition',
    'textDocument/implementation', 'textDocument/references', 'textDocument/documentSymbol',
    'textDocument/documentHighlight', 'textDocument/foldingRange',
    'textDocument/semanticTokens/full', 'textDocument/semanticTokens/range',
})
DOCUMENT_NOTIFICATIONS = {'textDocument/didOpen', 'textDocument/didChange', 'textDocument/didClose'}


class GatewayError(Exception):
    def __init__(self, reason, code=-32001):
        self.reason, self.code = reason, code


def source_viewer_document(uri):
    """Lexical exclusion, not source authorization or filesystem resolution.

    Jupyter's FileEditor adapter prefixes the reserved drive path with a file
    URI. Its local version counter may restart when the viewer reopens. Neither
    that alias nor the original drive URI is a notebook document to replay.
    """
    if type(uri) is not str or len(uri) > 8192:
        raise GatewayError('document-uri-unavailable')
    if uri.startswith('onec-bsl:'):
        return True
    try:
        parsed = urlsplit(uri)
    except ValueError:
        raise GatewayError('document-uri-unavailable') from None
    return parsed.scheme == 'file' and any(
        unquote(segment).startswith('onec-bsl:') for segment in parsed.path.split('/'))


def fence(context):
    return (context['epoch'], child_identity(context), context.get('analysis_revision', 0))


def child_identity(context):
    return (context['binding_id'], context.get('kernel_id'), context.get('kernel_incarnation'),
            context.get('installation_id'), context.get('source_root'))


async def deliver(callback, value):
    result = callback(value)
    if inspect.isawaitable(result):
        await result


class Gateway:
    def __init__(self, emit, *, child_factory=None, command=None, status=None,
                 max_children=DEFAULT_MAX_CHILDREN, max_requests=DEFAULT_MAX_REQUESTS,
                 max_resolve_items=DEFAULT_MAX_RESOLVE_ITEMS, idle_seconds=DEFAULT_IDLE_SECONDS,
                 workspace=None):
        if child_factory is None:
            from .lsp_child import ChildSession
            child_factory = ChildSession
        self.emit, self.factory, self.command, self.status_sink = emit, child_factory, command, status
        self.max_children, self.max_requests = max_children, max_requests
        self.max_resolve_items, self.idle_seconds = max_resolve_items, idle_seconds
        self.contexts, self.children, self.documents = {}, {}, {}
        self.locks, self.requests, self.resolves, self.last_used = {}, {}, {}, {}
        self.lock_users = {}
        self.applied = {}
        from .lsp_workspace import WorkspaceWatchService
        self.workspace = workspace or WorkspaceWatchService()
        self.subscriptions, self.workspace_reasons, self.file_changes, self.update_tasks = {}, {}, {}, {}
        self.request_bindings = {}
        self.closed = False

    async def status(self, context, state, reason=None):
        current = self.contexts.get(context['binding_id'])
        if current is None or fence(current) != fence(context) or child_identity(current) != child_identity(context):
            return
        if self.status_sink:
            await deliver(self.status_sink, dict(binding_id=context['binding_id'], epoch=context['epoch'],
                state=state, reason=reason))

    def _new_child(self, context):
        self.resolves = {token: value for token, value in self.resolves.items()
                         if value[0] != context['binding_id']}
        async def emit(message):
            current = self.contexts.get(context['binding_id'])
            binding = context['binding_id']
            if (self.closed or current is None or self.children.get(binding) is not child
                    or child_identity(child.context) != child_identity(current)
                    or fence(child.context) != fence(current)
                    or self.applied.get(binding) != (child_identity(current), fence(current))
                    or getattr(child, 'applied_fence', fence(current)) != fence(current)):
                return
            if message.get('method') == 'textDocument/publishDiagnostics':
                params = message.get('params', {})
                uri = params.get('uri')
                document = self.documents.get(uri)
                if (uri != current['document_uri'] or document is None
                        or params.get('version') != document['version']):
                    return
            await deliver(self.emit, message)
        child = self.factory(context, emit, command=self.command, status=self.status,
            workspace_checked=True, workspace_reason=self.workspace_reasons.get(context.get('source_root')))
        return child

    async def _workspace_ready(self, context):
        root = context.get('source_root')
        if not root:
            return
        key = os.path.normcase(os.path.abspath(root))
        if key not in self.subscriptions:
            token = object()
            remove = self.workspace.subscribe(root, lambda change: self._files_changed(key, token, change))
            self.subscriptions[key] = (token, remove)
        subscription = self.subscriptions[key]
        reason = await self.workspace.ready(root)
        current = self.contexts.get(context['binding_id'])
        if (self.closed or self.subscriptions.get(key) is not subscription
                or current is None or child_identity(current) != child_identity(context)
                or fence(current) != fence(context)):
            return False
        if reason is None:
            from .lsp_contexts import normalize_source_root
            selected, layout_reason = normalize_source_root(root)
            if layout_reason or selected is None or os.path.normcase(str(selected)) != key:
                reason = ('workspace-unavailable' if layout_reason == 'source-root-unavailable'
                          else 'workspace-unsafe')
        self.workspace_reasons[root] = reason
        return True

    def _files_changed(self, key, token, change):
        """Fence every consumer immediately; bounded coalesced updates run separately."""
        if self.closed or self.subscriptions.get(key, (None,))[0] is not token:
            return
        from .lsp_workspace import WorkspaceChange, DEFAULT_MAX_EVENTS
        affected = []
        for binding, context in list(self.contexts.items()):
            root = context.get('source_root')
            if not root or os.path.normcase(os.path.abspath(root)) != key:
                continue
            self.contexts[binding] = {**context, 'analysis_revision': context.get('analysis_revision', 0) + 1}
            affected.append(binding)
            old = self.file_changes.get(binding)
            if old is not None:
                events = old.events + change.events
                overflow = len(events) > DEFAULT_MAX_EVENTS
                combined = WorkspaceChange(() if overflow else events,
                    old.rescan_required or change.rescan_required or overflow,
                    'workspace-event-limit' if overflow else change.reason or old.reason)
            else:
                combined = change
            self.file_changes[binding] = combined
        for request_id, binding in list(self.request_bindings.items()):
            if binding in affected and request_id in self.requests:
                self.requests[request_id].cancel()
        self._expire_resolves()
        for binding in affected:
            if binding not in self.update_tasks:
                self.update_tasks[binding] = asyncio.create_task(self._update_files(binding))

    async def _update_files(self, binding):
        try:
            while not self.closed and binding in self.contexts and binding in self.file_changes:
                if binding not in self.children and self.contexts[binding]['document_uri'] not in self.documents:
                    self.file_changes.pop(binding, None)
                    return
                await self._child(binding)
        except (GatewayError, KeyError):
            pass
        finally:
            self.update_tasks.pop(binding, None)

    async def accept_contexts(self, contexts):
        incoming = {c['binding_id']: deepcopy(c) for c in contexts}
        for binding, context in incoming.items():
            old = self.contexts.get(binding)
            context['analysis_revision'] = (old.get('analysis_revision', 0)
                if old and child_identity(old) == child_identity(context) else 0)
            if not old or child_identity(old) != child_identity(context):
                self.file_changes.pop(binding, None)
        self.contexts = incoming  # fence old responses immediately; dispatch checks applied below
        roots = {os.path.normcase(os.path.abspath(c['source_root'])) for c in incoming.values() if c.get('source_root')}
        self.workspace_reasons = {root: reason for root, reason in self.workspace_reasons.items()
                                  if os.path.normcase(os.path.abspath(root)) in roots}
        for key, (_, remove) in list(self.subscriptions.items()):
            if key not in roots:
                remove()
                del self.subscriptions[key]
        for binding in list(self.file_changes):
            if binding not in incoming:
                self.file_changes.pop(binding, None)
        for binding in dict.fromkeys((*self.children, *self.locks)):
            current = incoming.get(binding)
            if current is None:
                async with self._binding_lock(binding):
                    if binding in self.contexts:
                        continue  # A newer context update restored this binding while waiting.
                    child = self.children.pop(binding, None)
                    self.applied.pop(binding, None)
                    if child: await child.close()
            elif binding in self.children:
                try:
                    await self._child(binding)
                except GatewayError as error:
                    await self._error(None, error.reason, error.code)
        self._expire_resolves()
        for binding, context in incoming.items():
            if binding not in self.children and context['document_uri'] in self.documents:
                try:
                    await self._child(binding)
                except GatewayError as error:
                    await self._error(None, error.reason, error.code)

    def context_for(self, uri):
        matches = [binding for binding, c in self.contexts.items() if c['document_uri'] == uri]
        if len(matches) != 1:
            raise GatewayError('document-binding-unavailable')
        return matches[0]

    async def _sync(self, child, context):
        await self.status(context, 'updating')
        try:
            await child.synchronize(context)
        except Exception:
            # Recover a failed child once, replaying only notebook text.
            await child.close()
            child = self._new_child(context)
            self.children[context['binding_id']] = child
            try:
                await child.synchronize(context)
                document = self.documents.get(context['document_uri'])
                if document:
                    await child.notify('textDocument/didOpen', {'textDocument': deepcopy(document)})
            except Exception:
                await child.close()
                self.children.pop(context['binding_id'], None)
                self.applied.pop(context['binding_id'], None)
                await self.status(context, 'unavailable', 'child-synchronization-failed')
                raise GatewayError('child-synchronization-failed') from None
        self.applied[context['binding_id']] = (child_identity(context), fence(context))
        await self._child_status(child, context)

    async def _child_status(self, child, context):
        # Query availability is not proof that asynchronous LS indexing finished.
        reason = getattr(child, 'degraded_reason', None)
        await self.status(context, 'unavailable' if reason else 'ready', reason or
            ('index-convergence-unconfirmed' if context.get('source_root') else None))

    @asynccontextmanager
    async def _binding_lock(self, binding):
        lock = self.locks.setdefault(binding, asyncio.Lock())
        self.lock_users[binding] = self.lock_users.get(binding, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self.lock_users[binding] -= 1
            if not self.lock_users[binding]:
                self.lock_users.pop(binding)
                if binding not in self.contexts and binding not in self.children:
                    self.locks.pop(binding, None)
                    self.last_used.pop(binding, None)

    async def _child(self, binding):
        async with self._binding_lock(binding):
            while True:
                if self.closed or binding not in self.contexts:
                    raise GatewayError('document-binding-unavailable')
                context = self.contexts[binding]
                target = (child_identity(context), fence(context))
                change = self.file_changes.pop(binding, None)
                if change and change.rescan_required and binding in self.children:
                    await self.status(context, 'updating', change.reason)
                    await self.children.pop(binding).close()
                    self.applied.pop(binding, None)
                if binding in self.children and (not getattr(self.children[binding], 'available', True)
                        or self.applied.get(binding, (None,))[0] != target[0]):
                    await self.children.pop(binding).close()
                    self.applied.pop(binding, None)
                if binding not in self.children:
                    if context.get('source_root') and not await self._workspace_ready(context):
                        continue
                    await self.cleanup_idle()
                    if len(self.children) >= self.max_children:
                        await self.status(context, 'unavailable', 'child-capacity-unavailable')
                        raise GatewayError('child-capacity-unavailable')
                    child = self._new_child(context)
                    self.children[binding] = child
                    await self.status(context, 'indexing')
                    try:
                        await self._sync(child, context)
                        child = self.children[binding]
                        document = self.documents.get(context['document_uri'])
                        if document:
                            await child.notify('textDocument/didOpen', {'textDocument': deepcopy(document)})
                    except BaseException:
                        await child.close()
                        self.children.pop(binding, None)
                        self.applied.pop(binding, None)
                        raise
                elif change is not None:
                    child = self.children[binding]
                    await self.status(context, 'updating', change.reason)
                    try:
                        await child.files_changed(context, change)
                    except Exception:
                        await child.close()
                        self.children.pop(binding, None)
                        self.applied.pop(binding, None)
                        continue
                    self.applied[binding] = target
                    await self._child_status(child, context)
                elif self.applied.get(binding) != target:
                    child = self.children[binding]
                    await self._sync(child, context)
                current = self.contexts.get(binding)
                if current is None:
                    raise GatewayError('document-binding-unavailable')
                if self.applied.get(binding) == (child_identity(current), fence(current)):
                    self.last_used[binding] = time.monotonic()
                    return self.children[binding]

    async def cleanup_idle(self):
        for binding, child in list(self.children.items()):
            if binding not in self.contexts:
                continue
            uri = self.contexts[binding]['document_uri']
            if uri not in self.documents and time.monotonic() - self.last_used.get(binding, 0) > self.idle_seconds:
                await child.close()
                self.children.pop(binding, None)
                self.applied.pop(binding, None)

    def _expire_resolves(self):
        self.resolves = {token: value for token, value in self.resolves.items()
                        if value[0] in self.contexts and value[1] == fence(self.contexts[value[0]])
                        and value[3] == self.documents.get(self.contexts[value[0]]['document_uri'], {}).get('version')}

    def _remember(self, document):
        uri = document['uri']
        if (type(uri) is not str or len(uri) > 8192
                or (uri not in self.documents and len(self.documents) >= DEFAULT_MAX_DOCUMENTS)
                or sum(len(d['text'].encode()) for key, d in self.documents.items() if key != uri)
                + len(document['text'].encode()) > DEFAULT_MAX_DOCUMENT_BYTES):
            raise GatewayError('document-capacity-unavailable')
        self.documents[uri] = deepcopy(document)
        self._expire_resolves()

    async def handle(self, message):
        request_id = message.get('id')
        method, params = message.get('method'), message.get('params') or {}
        task = asyncio.current_task()
        registered = False
        try:
            if self.closed:
                raise GatewayError('gateway-closed')
            if method == '$/cancelRequest':
                pending = self.requests.get(params.get('id'))
                if pending:
                    pending.cancel()
                return
            if request_id is not None:
                if request_id in self.requests or len(self.requests) >= self.max_requests:
                    raise GatewayError('request-capacity-unavailable')
                self.requests[request_id], registered = task, True
            if method == 'initialize':
                result = {'capabilities': {'textDocumentSync': {'openClose': True, 'change': 1},
                    'completionProvider': {'resolveProvider': True, 'triggerCharacters': ['.']},
                    'hoverProvider': True, 'definitionProvider': True, 'referencesProvider': True,
                    'documentSymbolProvider': True,
                    'signatureHelpProvider': {'triggerCharacters': ['(', ',']}}}
            elif method == 'shutdown':
                result = None
            elif method in ('initialized', '$/setTrace'):
                return
            elif (method == 'workspace/didChangeConfiguration' and 'id' not in message
                    and type(message.get('params')) is dict and 'settings' in params
                    and not (type(params['settings']) is dict and 'onecProjectBinding' in params['settings'])):
                # Ordinary LSP client settings are not authoritative child settings.
                # Private project claims remain the authenticated WebSocket's job.
                return
            elif method == 'exit':
                await self.close()
                return
            else:
                if method in DOCUMENT_NOTIFICATIONS or method in READ_METHODS:
                    if source_viewer_document(params['textDocument']['uri']):
                        if method in DOCUMENT_NOTIFICATIONS:
                            return  # Service viewer text never enters notebook replay or a child.
                        raise GatewayError('source-viewer-read-only')
                if method in DOCUMENT_NOTIFICATIONS:
                    document = params['textDocument']; uri = document['uri']
                    if method.endswith('didChange') and uri in self.documents:
                        from .lsp_child import changed_document
                        if changed_document(self.documents[uri], params) is self.documents[uri]:
                            return  # Validated identical duplicate; no child work or status churn.
                    if method.endswith('didOpen'):
                        if (document.get('languageId') != 'bsl' or type(document.get('text')) is not str
                                or type(document.get('version')) is not int):
                            raise GatewayError('document-language-unavailable')
                        if ((uri not in self.documents and len(self.documents) >= DEFAULT_MAX_DOCUMENTS)
                                or sum(len(d['text'].encode()) for key, d in self.documents.items() if key != uri)
                                + len(document['text'].encode()) > DEFAULT_MAX_DOCUMENT_BYTES):
                            raise GatewayError('document-capacity-unavailable')
                    if not any(c['document_uri'] == uri for c in self.contexts.values()):
                        if method.endswith('didOpen'):
                            self._remember(document)
                        elif method.endswith('didClose'):
                            self.documents.pop(uri, None)
                        else:
                            from .lsp_child import changed_document
                            self._remember(changed_document(self.documents[uri], params))
                        return  # bounded in-memory replay; no child or project access
                if method == 'completionItem/resolve':
                    token = (params.get('data') or {}).get('onec')
                    stored = self.resolves.get(token)
                    if not stored:
                        raise GatewayError('resolve-context-unavailable')
                    binding, expected, params, expected_version = stored
                    if binding not in self.contexts or fence(self.contexts[binding]) != expected:
                        raise GatewayError('content-modified', -32801)
                elif method in READ_METHODS or method in DOCUMENT_NOTIFICATIONS:
                    binding = self.context_for(params['textDocument']['uri'])
                    expected = fence(self.contexts[binding])
                    expected_version = self.documents.get(self.contexts[binding]['document_uri'], {}).get('version')
                else:
                    raise GatewayError('method-not-supported', -32601)
                if registered:
                    self.request_bindings[request_id] = binding
                child = await self._child(binding)
                if method in DOCUMENT_NOTIFICATIONS:
                    document = params['textDocument']
                    uri = document['uri']
                    if method.endswith('didOpen'):
                        if document.get('languageId') != 'bsl':
                            raise GatewayError('document-language-unavailable')
                        self._remember(document)
                    elif method.endswith('didClose'):
                        self.documents.pop(uri, None)
                        self._expire_resolves()
                    else:
                        from .lsp_child import changed_document
                        self._remember(changed_document(self.documents[uri], params))
                    await child.notify(method, params)
                    return
                result = await child.request(method, deepcopy(params))
                if (binding not in self.contexts or fence(self.contexts[binding]) != expected
                        or self.children.get(binding) is not child
                        or self.documents.get(self.contexts[binding]['document_uri'], {}).get('version') != expected_version):
                    raise GatewayError('content-modified', -32801)
                if method == 'textDocument/completion':
                    items = result.get('items', []) if isinstance(result, dict) else result or []
                    self._expire_resolves()
                    if len(items) > self.max_resolve_items:
                        raise GatewayError('resolve-capacity-unavailable')
                    while len(items) + len(self.resolves) > self.max_resolve_items:
                        self.resolves.pop(next(iter(self.resolves)))
                    for item in items:
                        token = uuid4().hex
                        self.resolves[token] = (binding, expected, deepcopy(item), expected_version)
                        item['data'] = {'onec': token}
            if request_id is not None:
                await deliver(self.emit, {'jsonrpc': '2.0', 'id': request_id, 'result': result})
        except asyncio.CancelledError:
            if not self.closed:
                if method == 'textDocument/hover' and type(request_id) in (int, str):
                    await deliver(self.emit, {'jsonrpc': '2.0', 'id': request_id, 'result': None})
                else:
                    await self._error(request_id, 'request-cancelled', -32800)
        except GatewayError as error:
            # Native hover consumers can retain rejected promises. An expected
            # discarded hover is the protocol's empty result, never stale data.
            if (method == 'textDocument/hover' and type(request_id) in (int, str)
                    and type(error) is GatewayError
                    and (error.reason, error.code) == ('content-modified', -32801)):
                if not self.closed:
                    await deliver(self.emit, {'jsonrpc': '2.0', 'id': request_id, 'result': None})
            else:
                await self._error(request_id, error.reason, error.code)
        except Exception:
            await self._error(request_id, 'language-service-unavailable', -32001)
        finally:
            if registered and self.requests.get(request_id) is task:
                self.requests.pop(request_id, None)
                self.request_bindings.pop(request_id, None)

    async def _error(self, request_id, reason, code):
        if request_id is None:
            await deliver(self.emit, {'jsonrpc': '2.0', 'method': 'window/showMessage',
                                     'params': {'type': 2, 'message': reason}})
        else:
            await deliver(self.emit, {'jsonrpc': '2.0', 'id': request_id,
                                     'error': {'code': code, 'message': reason}})

    async def close(self):
        self.closed = True
        for _, remove in self.subscriptions.values():
            remove()
        self.subscriptions.clear()
        for task in list(self.update_tasks.values()):
            task.cancel()
        await asyncio.gather(*self.update_tasks.values(), return_exceptions=True)
        for task in list(self.requests.values()):
            if task is not asyncio.current_task():
                task.cancel()
        # Observer timeout must not bypass independent owned-process cleanup.
        children = list(self.children.items())
        results = await asyncio.gather(self.workspace.close(),
            *(child.close() for _, child in children), return_exceptions=True)
        for (binding, _), result in zip(children, results[1:]):
            if not isinstance(result, BaseException):
                self.children.pop(binding, None)
        self.applied.clear()
        self.resolves.clear()
        for result in results:
            if isinstance(result, BaseException):
                raise result
