"""Authenticated read-only source access for definitions outside Jupyter's root."""

from jupyter_server.base.handlers import APIHandler
from jupyter_server.auth.decorator import authorized
from jupyter_server.utils import url_path_join
from tornado import web
from tornado.ioloop import PeriodicCallback
from tornado.routing import PathMatches, Rule
import re

from .lsp_sources import SourceStore
from .lsp_cleanup import cleanup
from .lsp_contexts import ContextRegistry, DEFAULT_BINDING_LEASE_SECONDS, authorize_association, parse_binding_request


class ContextHandler(APIHandler):
    @property
    def registry(self):
        return self.settings['onec_bsl_context_registry']

    @property
    def owner(self):
        return self.current_user.username

    async def _check(self, notebook_path, kernel_id):
        try:
            await authorize_association(self, notebook_path, kernel_id)
        except PermissionError:
            raise web.HTTPError(403, 'BSL context access denied') from None

    @web.authenticated
    async def post(self, binding=''):
        if binding:
            raise web.HTTPError(405)
        try:
            data = parse_binding_request(self.get_json_body())
        except ValueError:
            raise web.HTTPError(400, 'Invalid BSL context association') from None
        await self._check(data['notebook_path'], data['kernel_id'])
        try:
            identity = self.registry.bind(self.owner, **data)
        except ValueError:
            raise web.HTTPError(429, 'BSL context capacity unavailable') from None
        await self.registry.start(identity)
        self.set_status(201)
        self.finish(self.registry.status(identity, owner=self.owner).to_wire())

    @web.authenticated
    async def get(self, binding=''):
        try:
            context = self.registry.association(binding, owner=self.owner)
            await self._check(context['notebook_path'], context['kernel_id'])
            self.registry.renew(binding, owner=self.owner)
            self.finish(self.registry.status(binding, owner=self.owner).to_wire())
        except KeyError:
            raise web.HTTPError(404, 'BSL context unavailable') from None

    @web.authenticated
    async def delete(self, binding=''):
        try:
            # Ownership remains authoritative after notebook/session removal.
            # Requiring the obsolete association would leak its client/capacity.
            self.registry.unbind(binding, owner=self.owner)
        except KeyError:
            raise web.HTTPError(404, 'BSL context unavailable') from None
        self.set_status(204)
        self.finish()


class SourceHandler(ContextHandler):
    @property
    def source_store(self):
        return self.settings['onec_bsl_source_store']

    async def _source_check(self, path):
        association = self.registry.association(path.split('/')[0], owner=self.owner)
        await self._check(association['notebook_path'], association['kernel_id'])

    @web.authenticated
    @authorized(action="read", resource="contents")
    async def get(self, path=""):
        try:
            checkpoints = path.endswith('/checkpoints')
            source_path = path.removesuffix('/checkpoints') if checkpoints else path
            await self._source_check(source_path)
            model = self.source_store.read(source_path, owner=self.owner,
                content=not checkpoints and self.get_argument('content', '1') != '0')
        except (KeyError, ValueError, OSError):
            raise web.HTTPError(404, 'BSL source unavailable') from None
        self.finish('[]' if checkpoints else model)

    @web.authenticated
    async def post(self, path=''):
        raise web.HTTPError(405)

    @web.authenticated
    async def delete(self, path=''):
        raise web.HTTPError(405)


class FailedSourceNavigationHandler(APIHandler):
    """An upstream synthetic path must never become default-filesystem authority."""
    @web.authenticated
    @authorized(action='read', resource='contents')
    async def get(self, suffix=''):
        raise web.HTTPError(404, 'BSL source unavailable')

    head = get

    @web.authenticated
    async def post(self, suffix=''):
        raise web.HTTPError(405)

    put = patch = delete = post


def register_source_fallback(app):
    # Tornado matches raw URL paths before decoding captures. Reserve both plain
    # and percent-encoded spellings, but only this exact synthetic namespace.
    prefix = '.lsp_symlink/onec-bsl:'
    encoded = ''.join('(?:' + re.escape(char) + '|%' +
        ''.join(f'[{digit.lower()}{digit.upper()}]' if digit.isalpha() else digit
                for digit in f'{ord(char):02x}') + ')' for char in prefix)
    pattern = re.escape(url_path_join(app.settings['base_url'], 'api/contents/')) + encoded + '(.*)$'
    app.default_router.rules.insert(0, Rule(PathMatches(pattern), FailedSourceNavigationHandler))


def _jupyter_server_extension_points():
    return [{"module": "onec_runtime_jupyter.lsp_server_extension"}]


def _load_jupyter_server_extension(server_app):
    from .lsp_websocket import register_websocket
    app = server_app.web_app
    registry = app.settings['onec_bsl_context_registry'] = ContextRegistry(server_app.kernel_manager,
        lease_seconds=app.settings.get('onec_bsl_binding_lease_seconds', DEFAULT_BINDING_LEASE_SECONDS))
    reaper = app.settings['onec_bsl_binding_reaper'] = PeriodicCallback(registry.prune, min(1000, registry.lease_seconds * 500))
    reaper.start()
    app.settings['onec_bsl_source_store'] = SourceStore(app.settings['onec_bsl_context_registry'])
    pattern = url_path_join(app.settings["base_url"], r"onec-bsl/sources/(.*)")
    contexts = url_path_join(app.settings['base_url'], r'onec-bsl/contexts(?:/([a-f0-9]{32}))?')
    app.add_handlers(".*$", [(pattern, SourceHandler), (contexts, ContextHandler)])
    register_source_fallback(app)
    register_websocket(app)


async def _unload_jupyter_server_extension(server_app):
    app = server_app.web_app
    sockets = list(app.settings.get('onec_bsl_sockets', ()))
    registry = app.settings['onec_bsl_context_registry']
    await cleanup([
        app.settings['onec_bsl_binding_reaper'].stop,
        *(socket.close for socket in sockets),
        # Server unload also owns cleanup when a failed transport close never
        # delivers Tornado's callback. on_close is idempotent.
        *(socket.on_close for socket in sockets),
        *(socket.wait_closed for socket in sockets),
        registry.close,
        registry.wait_closed,
        app.settings['onec_bsl_source_store'].close,
    ])
