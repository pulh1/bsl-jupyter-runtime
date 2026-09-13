"""Dedicated authenticated BSL transport; never joins jupyter-lsp broadcasts."""
from __future__ import annotations

import asyncio
import json
import os
import re
from uuid import uuid4

from jupyter_server.base.handlers import JupyterHandler
from jupyter_server.base.websocket import WebSocketMixin
from jupyter_server.utils import url_path_join
from tornado import web
from tornado.routing import PathMatches, Rule
from tornado.websocket import WebSocketHandler

from .lsp import language_server_spec
from .lsp_contexts import authorize_association
from .lsp_cleanup import cleanup, drain, finish, observe
from .lsp_control import ControlServer
from .lsp_kernel_client import _await
from .lsp_process import OwnedGateway
from .lsp_proxy import DEFAULT_MAX_MESSAGE_BYTES, read_message, write_message

DEFAULT_MAX_SOCKETS = 32
DEFAULT_ASSOCIATION_POLL_SECONDS = 0.2
DEFAULT_PENDING_CLAIM_SECONDS = 30
DEFAULT_MAX_DOCUMENTS = 32


class BslWebSocketHandler(WebSocketMixin, WebSocketHandler, JupyterHandler):
    auth_resource = 'lsp'

    @property
    def registry(self): return self.settings['onec_bsl_context_registry']

    async def get(self, *args, **kwargs):
        user = self.current_user
        if user is None or not await _await(self.authorizer.is_authorized(self, user, 'execute', 'lsp')):
            raise web.HTTPError(403)
        await super().get(*args, **kwargs)

    async def open(self):
        self.owner, self.connection_id = self.current_user.username, uuid4().hex
        self.documents = set()
        self.selected = set()
        self.claims = {}
        self.process = self.control = self.owned = None
        self._closed = asyncio.Event()
        self.writer = asyncio.Lock()
        self.selection_lock = asyncio.Lock()
        self.tasks = []
        sockets = self.settings.setdefault('onec_bsl_sockets', set())
        if len(sockets) >= self.settings.get('onec_bsl_max_sockets', DEFAULT_MAX_SOCKETS):
            self.close(1013, 'gateway-capacity-unavailable')
            return
        sockets.add(self)
        try:
            spec = language_server_spec(self).get('onec-bsl')
            if not spec:
                raise ValueError('language-server-unavailable')
            self.control = ControlServer(
                lambda: {'owner': self.owner, 'connection_id': self.connection_id,
                         'contexts': self.registry.connection_contexts(self.connection_id, owner=self.owner)},
                revision=lambda: self.registry.revision,
                consumer=lambda statuses: self.registry.acknowledge(self.connection_id, owner=self.owner, statuses=statuses))
            environment = {**os.environ, **self.control.child_environment()}
            self.owned = OwnedGateway(spec['argv'], env=environment)
            self.process = self.owned.process
            await self.owned.wait_ready()
            if self._closed.is_set(): return
            self.tasks = [asyncio.create_task(self._receive()), asyncio.create_task(self._associations())]
            super().open()
        except Exception:
            self.close(1011, 'gateway-unavailable')

    async def _select(self):
        # Polling is read-only lease activity. Preserve current authority until an
        # explicit validated claim replaces it; never infer authority from recency.
        async with self.selection_lock:
            self.claims = {uri: claim for uri, claim in self.claims.items()
                           if claim[1] is None or claim[1] > self.registry.clock()}
            contexts = self.registry.associations(owner=self.owner)
            candidates = []
            for uri in self.documents:
                matches = [c for c in contexts if c['document_uri'] == uri]
                claim = self.claims.get(uri)
                if claim:
                    binding, deadline = claim
                    if deadline is not None and deadline <= self.registry.clock():
                        continue
                    matches = [c for c in matches if c['binding_id'] == binding]
                else:
                    current = [c for c in matches if c['binding_id'] in self.selected]
                    matches = current
                    if not matches and not getattr(self, 'explicit_claims', False):
                        matches = [c for c in contexts if c['document_uri'] == uri and self.registry.selectable(
                            self.connection_id, owner=self.owner, binding=c['binding_id'])]
                if len(matches) == 1:
                    candidates.append(matches[0])
            admitted = set()
            for context in candidates:
                binding = context['binding_id']
                try:
                    await authorize_association(self, context['notebook_path'], context['kernel_id'])
                    # Authorization awaited external services: recheck exact metadata
                    # and live socket ownership atomically before selection below.
                    if self.registry.association(binding, owner=self.owner) != context:
                        continue
                    if context['document_uri'] not in self.documents:
                        continue
                    if not self.registry.selectable(self.connection_id, owner=self.owner, binding=binding):
                        continue
                except Exception:
                    continue
                admitted.add(binding)
            self.registry.select(self.connection_id, owner=self.owner, bindings=admitted)
            self.selected = admitted
            for uri, (binding, deadline) in self.claims.items():
                if binding in admitted:
                    self.claims[uri] = (binding, None)

    async def _claim(self, claim):
        if type(claim) is not dict or set(claim) != {'document_uri', 'binding_id'}:
            raise ValueError('invalid-context-claim')
        uri, binding = claim['document_uri'], claim['binding_id']
        if (type(uri) is not str or not uri or len(uri) > 8192
                or type(binding) is not str or not re.fullmatch('[0-9a-f]{32}', binding)):
            raise ValueError('invalid-context-claim')
        async with self.selection_lock:
            self.claims = {uri: claim for uri, claim in self.claims.items()
                           if claim[1] is None or claim[1] > self.registry.clock()}
            if uri not in self.claims and len(self.claims) >= DEFAULT_MAX_DOCUMENTS:
                raise ValueError('context-claim-limit')
            context = self.registry.association(binding, owner=self.owner)
            if context['document_uri'] != uri:
                raise ValueError('context-document-mismatch')
            await authorize_association(self, context['notebook_path'], context['kernel_id'])
            if (self.registry.association(binding, owner=self.owner) != context or
                    not self.registry.selectable(self.connection_id, owner=self.owner, binding=binding)):
                raise KeyError('context-unavailable')
            self.claims[uri] = (binding, self.registry.clock() + DEFAULT_PENDING_CLAIM_SECONDS)
            self.explicit_claims = True
        await self._select()

    async def _associations(self):
        try:
            while not self._closed.is_set():
                await self._select()
                await asyncio.sleep(DEFAULT_ASSOCIATION_POLL_SECONDS)
        except asyncio.CancelledError:
            return
        except Exception:
            self.close(1008, 'context-access-denied')

    async def on_message(self, message):
        try:
            if not isinstance(message, str) or len(message.encode('utf-8')) > DEFAULT_MAX_MESSAGE_BYTES:
                raise ValueError
            value = json.loads(message)
            if type(value) is not dict:
                raise ValueError
            if value.get('method') == 'workspace/didChangeConfiguration':
                params = value.get('params')
                settings = params.get('settings') if type(params) is dict else None
                if type(settings) is dict and 'onecProjectBinding' in settings:
                    if set(params) != {'settings'} or set(settings) != {'onecProjectBinding'} or 'id' in value:
                        raise ValueError('invalid-context-claim')
                    await self._claim(settings['onecProjectBinding'])
                    return  # Private association metadata never reaches the child.
            if value.get('method') == 'textDocument/didOpen':
                document = value['params']['textDocument']
                uri = document['uri']
                if type(uri) is not str or len(uri) > 8192 or (uri not in self.documents and len(self.documents) >= DEFAULT_MAX_DOCUMENTS):
                    raise ValueError
                if (document.get('languageId') != 'bsl' or type(document.get('text')) is not str
                        or type(document.get('version')) is not int):
                    raise ValueError
                self.documents.add(uri)
                await self._select()
            elif value.get('method') == 'textDocument/didChange':
                document = value['params']['textDocument']
                changes = value['params']['contentChanges']
                if (type(document.get('version')) is not int or type(changes) is not list or not changes
                        or any(type(change) is not dict or type(change.get('text')) is not str or 'range' in change
                               for change in changes)):
                    raise ValueError
            elif value.get('method') == 'textDocument/didClose':
                uri = value['params']['textDocument']['uri']
                self.documents.discard(uri)
                self.claims.pop(uri, None)
                await self._select()
            document = value.get('params', {}).get('textDocument', {})
            uri = document.get('uri') if type(document) is dict else None
            if value.get('method') in ('textDocument/didOpen', 'textDocument/didChange') and uri in self.documents:
                await self._select()
                for context in self.registry.connection_contexts(self.connection_id, owner=self.owner):
                    if context['document_uri'] == uri:
                        self.registry.renew(context['binding_id'], owner=self.owner)
            async with self.writer:
                await asyncio.to_thread(write_message, self.process.stdin, value)
        except Exception:
            self.close(1008, 'gateway-message-unavailable')

    async def _receive(self):
        try:
            while (message := await asyncio.to_thread(read_message, self.process.stdout)) is not None:
                await self.write_message(json.dumps(message, ensure_ascii=False))
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self.close()

    def on_close(self):
        if not hasattr(self, '_closed') or self._closed.is_set():
            return
        self._closed.set()
        self.cleanup_task = asyncio.create_task(self._cleanup())
        self.cleanup_task.add_done_callback(observe)

    async def _cleanup(self):
        await cleanup([
            *([self.ping_callback.stop] if self.ping_callback else []),
            *(task.cancel for task in self.tasks),
            lambda: self.registry.detach(self.connection_id, owner=self.owner),
            *([lambda: self.owned.close(graceful=not self.writer.locked())] if self.owned else []),
            *([lambda: asyncio.to_thread(self.control.close)] if self.control else []),
            lambda: drain(self.tasks),
            lambda: self.settings['onec_bsl_sockets'].discard(self),
        ])

    async def wait_closed(self):
        await self._closed.wait()
        await finish(self.cleanup_task)


def register_websocket(app):
    pattern = re.escape(url_path_join(app.settings['base_url'], 'lsp/ws/onec-bsl')) + '$'
    # Exact PathMatches must be on the outer router before any generic HostMatches.
    # A nested catch-all Application would capture unrelated language routes.
    app.default_router.rules.insert(0, Rule(PathMatches(pattern), BslWebSocketHandler))
    app.settings.setdefault('onec_bsl_sockets', set())
