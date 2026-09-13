"""Private BSL process, virtual documents, current-file URIs and watched-file updates."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit
from urllib.request import url2pathname

from .lsp_gateway import deliver, fence
from .lsp_process import DEFAULT_TERMINATION_SECONDS, OwnedProcess

DEFAULT_MAX_WORKSPACE_ENTRIES = 100_000
DEFAULT_WORKSPACE_SCAN_SECONDS = 10
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
DEFAULT_DIAGNOSTIC_DELAY_SECONDS = 0.15
URI_FIELDS = {'uri', 'targetUri', 'oldUri', 'newUri', 'scopeUri', 'documentUri'}
PRIVATE_CONFIGURATION = {'sendErrors': 'never', 'traceLog': None,
                         'diagnostics': {'computeTrigger': 'onType'}}


def _linked(path):
    from .lsp_workspace import linked
    return linked(path.lstat())


def check_workspace(root, *, max_entries=DEFAULT_MAX_WORKSPACE_ENTRIES,
                    timeout=DEFAULT_WORKSPACE_SCAN_SECONDS):
    from .lsp_workspace import scan_workspace
    return scan_workspace(root, max_entries=max_entries, timeout=timeout).reason


def child_environment(temporary):
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'PATHEXT', 'COMSPEC', 'LANG', 'LC_ALL'}
    if os.name == 'nt':
        # BSL LS uses these standard Windows locations to discover the installed
        # 1C syntax helper and its platform type/member catalog.
        allowed.update({'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432'})
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({key: str(temporary) for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP')})
    return env



def context_token(context):
    fields = [context['binding_id'], context.get('kernel_incarnation'), context.get('installation_id'), context.get('source_root')]
    return sha256(json.dumps(fields, separators=(',', ':')).encode()).hexdigest()



def _translate(value, translate, key=''):
    if isinstance(value, dict):
        return {translate(k) if key == 'changes' else k: _translate(v, translate, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_translate(v, translate, key) for v in value]
    if isinstance(value, str) and key in URI_FIELDS:
        return translate(value)
    return value


class WorkspaceMapper:
    def __init__(self, root, document_uri, temporary, context=None):
        self.root = Path(root) if root else None
        self.document_uri, self.context = document_uri, context
        location = self.root if self.root else Path(temporary)
        self.virtual_uri = (location / '.onec-jupyter' / (sha256(document_uri.encode()).hexdigest() + '.bsl')).as_uri()

    def initialize_params(self):
        return {'processId': None, 'rootUri': self.root.as_uri() if self.root else None,
                'rootPath': str(self.root) if self.root else None,
                'workspaceFolders': [{'uri': self.root.as_uri(), 'name': self.root.name}] if self.root else [],
                'capabilities': {'textDocument': {'diagnostic': {'dynamicRegistration': False}},
                                 'workspace': {'configuration': True}}, 'trace': 'off'}

    def to_server(self, value):
        return _translate(value, lambda uri: self.virtual_uri if uri == self.document_uri else uri)

    def to_client(self, value):
        def translate(uri):
            if uri == self.virtual_uri:
                return self.document_uri
            if not uri.startswith('file:'):
                return uri
            if not self.root or not self.context:
                raise ValueError('source-uri-unavailable')
            parsed = urlsplit(uri)
            if parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError('source-uri-unavailable')
            path = Path(url2pathname(('//' + parsed.netloc if parsed.netloc else '') + parsed.path))
            if not path.is_absolute() or '..' in path.parts or path.suffix.lower() not in ('.bsl', '.os'):
                raise ValueError('source-uri-unavailable')
            relative = path.relative_to(self.root).as_posix()  # lexical containment before any file access
            for candidate in (path, *path.parents):
                if _linked(candidate):
                    raise ValueError('source-uri-unavailable')
            path.resolve(strict=True).relative_to(self.root.resolve(strict=True))
            if not path.is_file() or path.stat().st_size > DEFAULT_MAX_DOCUMENT_BYTES:
                raise ValueError('source-size-limit')
            return f"onec-bsl:{self.context['binding_id']}/{context_token(self.context)}/{quote(relative, safe='/')}"
        return _translate(value, translate)


def changed_document(document, params):
    version = params['textDocument']['version']
    if type(version) is not int or version < document['version']:
        raise ValueError('document-version-unavailable')
    changes = params['contentChanges']
    # Gateway advertises full sync. Reject unexpected ranged edits; never corrupt host offsets.
    if not changes or any('range' in change or type(change.get('text')) is not str for change in changes):
        raise ValueError('document-change-unavailable')
    text = changes[-1]['text']
    if len(text.encode('utf-8')) > DEFAULT_MAX_DOCUMENT_BYTES:
        raise ValueError('document-size-limit')
    if version == document['version']:
        if text == document['text']:
            return document  # Jupyter sendOpen then initial full change uses the same version.
        raise ValueError('document-version-unavailable')
    return {**document, 'version': version, 'text': text}


class ProcessTransport:
    """Child request IDs are private to this process, including server requests."""
    def __init__(self, command, temporary, *, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS, on_failure=None,
                 on_progress=None):
        self.command, self.temporary, self.timeout = command, Path(temporary), timeout
        self.pending, self.serial, self.process = {}, 0, None
        self.writer = asyncio.Lock()
        self.on_failure, self.closed = on_failure, False
        self.io_tasks = set()
        self.owned = None
        self._terminating = None
        self._termination_failed = False
        self.reader = None
        # Optional benchmark observer: synchronous, nonblocking metadata only.
        self.on_progress = on_progress

    def _progress(self, method, params):
        if self.on_progress is None:
            return
        valid = isinstance(params, dict) and set(params) <= {'token', 'value'}
        if valid:
            token = params.get('token')
            valid = type(token) is str and 0 < len(token) <= 160
        if valid and method == '$/progress':
            value = params.get('value')
            valid = isinstance(value, dict) and set(value) <= {'kind', 'title', 'message', 'percentage', 'cancellable'}
            if valid:
                valid = (all(type(value[k]) is str and len(value[k]) <= 160
                             for k in ('kind', 'title', 'message') if k in value)
                         and ('percentage' not in value or type(value['percentage']) is int
                              and 0 <= value['percentage'] <= 100)
                         and ('cancellable' not in value or type(value['cancellable']) is bool))
        elif valid:
            valid = set(params) == {'token'}
        self.on_progress(method, deepcopy(params) if valid else {'invalid': True})

    async def start(self, root):
        config = self.temporary / 'bsl-config.json'
        config.write_text(json.dumps(PRIVATE_CONFIGURATION), encoding='utf-8')
        command = [*self.command, '--configuration=' + str(config)]
        cwd = self.temporary / 'cwd'
        cwd.mkdir()
        self.owned = OwnedProcess(command, cwd=cwd, env=child_environment(self.temporary))
        self.process = self.owned.process
        await self.owned.wait_ready()
        self.reader = asyncio.create_task(self._read())

    async def _io(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self.io_tasks.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self.io_tasks.discard(task)
                if not task.cancelled(): task.exception()

    async def send(self, message):
        from .lsp_proxy import write_message
        try:
            async with asyncio.timeout(self.timeout):
                async with self.writer:
                    if self.closed: raise ValueError('child-unavailable')
                    await self._io(write_message, self.process.stdin, message)
        except (TimeoutError, asyncio.CancelledError, OSError):
            await self._terminate()
            raise

    async def notify(self, method, params):
        await self.send({'jsonrpc': '2.0', 'method': method, 'params': params})

    async def request(self, method, params):
        self.serial += 1
        request_id = self.serial
        future = self.pending[request_id] = asyncio.get_running_loop().create_future()
        sent = False
        try:
            async with asyncio.timeout(self.timeout):
                await self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
                sent = True
                return await future
        except TimeoutError:
            await self._terminate()
            raise
        except asyncio.CancelledError:
            if sent and not self.closed:
                try: await self.notify('$/cancelRequest', {'id': request_id})
                except (OSError, ValueError, TimeoutError): pass
            raise
        finally:
            self.pending.pop(request_id, None)
            if not future.done(): future.cancel()
            elif not future.cancelled(): future.exception()

    async def _read(self):
        from .lsp_proxy import read_message
        try:
            while not self.closed and (message := await self._io(read_message, self.process.stdout)) is not None:
                if 'method' in message:
                    if message['method'] in ('window/workDoneProgress/create', '$/progress'):
                        self._progress(message['method'], message.get('params'))
                    if 'id' in message:
                        method = message['method']
                        if method == 'workspace/configuration':
                            items = message.get('params', {}).get('items', [])
                            if len(items) > 128:
                                raise ValueError('configuration-limit')
                            result = [deepcopy(PRIVATE_CONFIGURATION) for _ in items]
                        elif method in ('window/workDoneProgress/create', 'client/registerCapability'):
                            result = None
                        else:
                            await self.send({'jsonrpc': '2.0', 'id': message['id'],
                                'error': {'code': -32601, 'message': 'method-not-supported'}})
                            continue
                        await self.send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
                    # Ignore unsolicited diagnostics/logs. Only fenced pull results are published.
                    continue
                future = self.pending.get(message.get('id'))
                if future is not None and not future.done():
                    if 'error' in message:
                        future.set_exception(ValueError('child-request-failed'))
                    else:
                        future.set_result(message.get('result'))
        except (OSError, ValueError, EOFError, TimeoutError):
            pass
        finally:
            await self._terminate()
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ValueError('child-unavailable'))
            if self._termination_failed and self.on_failure:
                try:
                    async with asyncio.timeout(self.timeout):
                        await self.on_failure()
                except TimeoutError:
                    pass  # The failure consumer must not retain the closed reader.

    async def _terminate(self, *, failed=True):
        if not self.closed:
            # The first termination cause survives later cleanup calls and the
            # reader's EOF. Only the reader delivers the bounded callback once.
            self._termination_failed = failed
        self.closed = True
        if self.owned is None: return
        if self._terminating is None:
            async def terminate():
                # Tree abort releases the actual blocking pipe operations before
                # their tasks are joined. Cancellation alone cannot stop threads.
                await self.owned.close(graceful=False)
                if self.io_tasks:
                    done, unfinished = await asyncio.wait(self.io_tasks, timeout=DEFAULT_TERMINATION_SECONDS)
                    await asyncio.gather(*done, return_exceptions=True)
                    self.io_tasks.difference_update(done)
                    if unfinished:
                        # Keep actual I/O visible; cancelling to_thread tasks would
                        # hide live threads instead of terminating their operations.
                        raise TimeoutError('child-io-termination-timeout')
            self._terminating = asyncio.create_task(terminate())
        await asyncio.shield(self._terminating)

    async def close(self):
        await self._terminate(failed=False)
        if self.reader is not None and self.reader is not asyncio.current_task():
            _, unfinished = await asyncio.wait({self.reader}, timeout=max(self.timeout, DEFAULT_TERMINATION_SECONDS))
            if unfinished:
                self.reader.cancel()
                raise TimeoutError('child-reader-termination-timeout')
            await self.reader


class ChildSession:
    def __init__(self, context, emit, *, command=None, transport=None, status=None,
                 workspace_checked=False, workspace_reason=None, on_progress=None):
        self.context, self.emit = deepcopy(context), emit
        self.temporary = TemporaryDirectory(prefix='onec-bsl-child-')
        self.status_sink = status
        self.on_progress = on_progress
        self.transport = transport or ProcessTransport(command, self.temporary.name, on_failure=self._failed,
                                                       on_progress=on_progress)
        self.mapper = None
        self.lock = asyncio.Lock()
        self.opened, self.diagnostics = {}, {}
        self.workspace_checked, self.workspace_reason = workspace_checked, workspace_reason
        self.started, self.closed, self.degraded_reason = False, False, None
        self.applied_fence = None

    @property
    def available(self):
        process = getattr(self.transport, 'process', None)
        return not self.closed and (process is None or process.poll() is None)

    async def _failed(self):
        if self.closed:
            return
        if self.status_sink:
            import inspect
            result = self.status_sink(self.context, 'unavailable', 'child-unavailable')
            if inspect.isawaitable(result): await result
        await deliver(self.emit, {'jsonrpc': '2.0', 'method': 'window/showMessage',
                                 'params': {'type': 2, 'message': 'child-unavailable'}})

    async def synchronize(self, context):
        self.context = deepcopy(context)  # Fence diagnostics before any asynchronous work.
        async with self.lock:
            root = context.get('source_root')
            if not self.started:
                self.degraded_reason = (self.workspace_reason if self.workspace_checked else
                    await asyncio.to_thread(check_workspace, root)) if root else None
                if self.degraded_reason:
                    root = None
                self.mapper = WorkspaceMapper(root, context['document_uri'], self.temporary.name, context)
                await self.transport.start(root)
                params = self.mapper.initialize_params()
                if self.on_progress is not None:
                    params['capabilities']['window'] = {'workDoneProgress': True}
                initialized = await self.transport.request('initialize', params)
                if self.on_progress is not None:
                    info = initialized.get('serverInfo') if isinstance(initialized, dict) else None
                    valid = isinstance(info, dict) and all(
                        type(info.get(key)) is str and 0 < len(info[key]) <= 160
                        for key in ('name', 'version'))
                    metadata = {'serverInfo': {key: info[key] for key in ('name', 'version')}} if valid else {'invalid': True}
                    self.on_progress('initialize-result', metadata)
                await self.transport.notify('initialized', {})
                self.started = True
            self.mapper.context = context
            self.applied_fence = fence(context)
            for uri in self.opened:
                self._diagnose(uri)

    async def files_changed(self, context, change):
        self.context = deepcopy(context)
        for task in self.diagnostics.values():
            task.cancel()
        async with self.lock:
            if self.mapper.root and change.events:
                await self.transport.notify('workspace/didChangeWatchedFiles',
                                            {'changes': list(change.events)})
            self.applied_fence = fence(context)
            for uri in self.opened:
                self._diagnose(uri)

    async def notify(self, method, params):
        async with self.lock:
            document = params['textDocument']; uri = document['uri']
            if method.endswith('didOpen'):
                if len(document['text'].encode()) > DEFAULT_MAX_DOCUMENT_BYTES:
                    raise ValueError('document-size-limit')
                self.opened[uri] = deepcopy(document)
            elif method.endswith('didClose'):
                self.opened.pop(uri, None)
                if uri in self.diagnostics:
                    self.diagnostics.pop(uri).cancel()
            else:
                self.opened[uri] = changed_document(self.opened[uri], params)
            await self.transport.notify(method, self.mapper.to_server(params))
            if uri in self.opened:
                self._diagnose(uri)

    async def request(self, method, params):
        async with self.lock:
            expected = fence(self.context)
            if expected != self.applied_fence:
                raise ValueError('analysis-not-synchronized')
            translated = self.mapper.to_server(params)
        result = await self.transport.request(method, translated)
        if expected != fence(self.context):
            from .lsp_gateway import GatewayError
            raise GatewayError('content-modified', -32801)
        return self.mapper.to_client(result)

    def _diagnose(self, uri):
        old = self.diagnostics.pop(uri, None)
        if old:
            old.cancel()
        self.diagnostics[uri] = asyncio.create_task(self._diagnostic(uri))

    async def _diagnostic(self, uri):
        try:
            await asyncio.sleep(DEFAULT_DIAGNOSTIC_DELAY_SECONDS)
            expected, version = fence(self.context), self.opened[uri]['version']
            async with self.lock:
                result = await self.transport.request('textDocument/diagnostic', self.mapper.to_server({'textDocument': {'uri': uri}}))
            if (not self.closed and expected == fence(self.context) and uri in self.opened
                    and version == self.opened[uri]['version'] and isinstance(result, dict) and result.get('kind') == 'full'):
                await deliver(self.emit, {'jsonrpc': '2.0', 'method': 'textDocument/publishDiagnostics',
                    'params': {'uri': uri, 'version': version, 'diagnostics': self.mapper.to_client(result['items'])}})
        except (Exception, asyncio.CancelledError):
            return

    async def close(self):
        self.closed = True
        tasks = list(self.diagnostics.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.transport.close()
        self.temporary.cleanup()
