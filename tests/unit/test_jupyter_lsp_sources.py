import os
import subprocess
from urllib.parse import quote

import pytest

from onec_runtime_jupyter import lsp_sources
from onec_runtime_jupyter.lsp_contexts import ContextRegistry
from onec_runtime_jupyter.lsp_child import context_token
from test_jupyter_lsp_contexts import admit, configured


def fixture(tmp_path):
    module = tmp_path / 'CommonModules/Example/Ext/Module.bsl'
    module.parent.mkdir(parents=True)
    (tmp_path / 'Configuration.xml').write_text('<Configuration/>')
    module.write_text('disk text')
    registry = ContextRegistry()
    binding = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    config = configured(tmp_path)
    client = admit(registry, binding, config)
    token = context_token(registry.context(binding, owner='alice'))
    path = f'{binding}/{token}/CommonModules/Example/Ext/Module.bsl'
    return registry, binding, client, config, path


def test_each_read_observes_current_file_and_deleted_file_is_unavailable(tmp_path):
    registry, binding, client, config, path = fixture(tmp_path)
    store = lsp_sources.SourceStore(registry)
    module = tmp_path / 'CommonModules/Example/Ext/Module.bsl'
    first = store.read(path, owner='alice')
    assert first['content'] == 'disk text' and first['writable'] is False
    module.write_text('Функция Новая() Экспорт\nКонецФункции', encoding='utf-8')
    assert store.read(path, owner='alice')['content'] != first['content']
    module.unlink()
    with pytest.raises(ValueError): store.read(path, owner='alice')
    registry.close()


def test_foreign_stale_and_malformed_source_paths_are_rejected(tmp_path):
    registry, binding, client, config, path = fixture(tmp_path)
    store = lsp_sources.SourceStore(registry)
    for suffix in ('../Module.bsl', '%2e%2e/Module.bsl', '%252e%252e/Module.bsl',
                   '/Module.bsl', 'C:/Module.bsl', 'Module.bsl?x', 'Module.bsl#x',
                   'file:///Module.bsl', 'secret.json', 'Module.bsl/..'):
        bad = '/'.join(path.split('/')[:2]) + '/' + suffix
        with pytest.raises(ValueError): store.read(bad, owner='alice')
    with pytest.raises(KeyError): store.read(path, owner='bob')
    admit(registry, binding, configured(tmp_path, '2' * 32))
    with pytest.raises(ValueError): store.read(path, owner='alice')
    registry.close()


def test_bounded_read_and_metadata_only_do_not_cache_bytes(tmp_path):
    registry, binding, client, config, path = fixture(tmp_path)
    with pytest.raises(ValueError): lsp_sources.SourceStore(registry, max_bytes=1).read(path, owner='alice')
    store = lsp_sources.SourceStore(registry)
    assert store.read(path, owner='alice', content=False)['content'] is None
    assert not hasattr(store, 'pin') and not hasattr(store, '_entries')
    module = tmp_path / 'CommonModules/Example/Ext/Module.bsl'
    renamed = module.with_name('Русское Имя.os'); module.rename(renamed)
    renamed.write_bytes(b'\xef\xbb\xbfnew disk')
    new_path = path.rsplit('/', 1)[0] + '/' + quote(renamed.name)
    assert store.read(new_path, owner='alice')['content'] == 'new disk'
    registry.close()


@pytest.mark.parametrize('limit', [0, -1, -2, True, 1.5])
def test_invalid_source_limit_cannot_turn_read_into_unbounded_operation(limit):
    with pytest.raises(ValueError): lsp_sources.SourceStore(None, max_bytes=limit)


def test_replaced_parent_junction_denies_current_read(tmp_path):
    registry, binding, client, config, path = fixture(tmp_path)
    store = lsp_sources.SourceStore(registry)
    assert store.read(path, owner='alice')['content'] == 'disk text'
    parent = tmp_path / 'CommonModules/Example/Ext'
    (parent / 'Module.bsl').unlink(); parent.rmdir()
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'Module.bsl').write_text('foreign')
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(parent), str(outside)], capture_output=True)
        assert result.returncode == 0
    else: parent.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError): store.read(path, owner='alice')
    finally:
        if os.name == 'nt': parent.rmdir()
        else: parent.unlink()
        registry.close()

def test_http_source_authorization_checkpoints_and_writes(tmp_path):
    pytest.importorskip('jupyter_server')
    import json
    from jupyter_server.auth import IdentityProvider, User
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application
    from onec_runtime_jupyter.lsp_server_extension import SourceHandler, ContextHandler
    registry, binding, client, config, path = fixture(tmp_path)
    store = lsp_sources.SourceStore(registry)
    class Identity(IdentityProvider):
        def get_user(self, handler):
            name = handler.request.headers.get('X-Test-User')
            return User(name) if name else None
    class Authorizer:
        def is_authorized(self, handler, user, action, resource): return user.username != 'denied'
    class Contents:
        def get(self, path, content=False): return {'type': 'notebook'}
    class Sessions:
        sessions = [{'path': 'a.ipynb', 'kernel': {'id': 'k'}}]
        def list_sessions(self): return self.sessions
    sessions = Sessions()
    class HTTP(AsyncHTTPTestCase):
        def get_app(self):
            return Application([(r'/onec-bsl/sources/(.*)', SourceHandler),
                (r'/onec-bsl/contexts/(.*)', ContextHandler)], onec_bsl_context_registry=registry,
                onec_bsl_source_store=store, identity_provider=Identity(), authorizer=Authorizer(),
                contents_manager=Contents(), session_manager=sessions, cookie_secret='test-only',
                xsrf_cookies=True, base_url='/', allow_remote_access=True)
    case = HTTP(); case.setUp()
    try:
        token = 'a' * 32
        headers = {'X-Test-User': 'alice', 'Cookie': '_xsrf=' + token, 'X-XSRFToken': token}
        url = '/onec-bsl/sources/' + path
        assert case.fetch(url).code == 403
        assert case.fetch(url, headers={**headers, 'X-Test-User': 'bob'}).code == 404
        assert case.fetch(url, headers={**headers, 'X-Test-User': 'denied'}).code == 403
        assert json.loads(case.fetch(url, headers=headers).body)['content'] == 'disk text'
        assert json.loads(case.fetch(url + '/checkpoints', headers=headers).body) == []
        assert case.fetch(url + '/checkpoints', headers={**headers, 'X-Test-User': 'bob'}).code == 404
        for method in ('PUT', 'PATCH', 'POST', 'DELETE'):
            assert case.fetch(url, method=method, body='{}' if method in ('PUT', 'PATCH', 'POST') else None, headers=headers).code == 405
        assert case.fetch(url + '/pins/editor', method='POST', body='', headers=headers).code == 405
        # Status authorization must not serialize source context.
        original = registry.context
        registry.context = lambda *_a, **_k: pytest.fail('status encoded complete sources')
        assert case.fetch('/onec-bsl/contexts/' + binding, headers=headers).code == 200
        registry.context = original
        sessions.sessions = []
        assert case.fetch(url, headers=headers).code == 403
        assert case.fetch(url + '/pins/editor', method='DELETE', headers=headers).code == 405
    finally:
        registry.close(); store.close(); case.tearDown()


def test_failed_navigation_namespace_never_reaches_default_contents_or_source_store():
    pytest.importorskip('jupyter_server')
    from jupyter_server.auth import IdentityProvider, User
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application, RequestHandler
    from onec_runtime_jupyter import lsp_server_extension as module
    assert hasattr(module, 'register_source_fallback'), 'reserved failed-navigation route missing'
    default_calls = []
    class Identity(IdentityProvider):
        def get_user(self, handler):
            name = handler.request.headers.get('X-Test-User')
            return User(name) if name else None
    class Authorizer:
        def is_authorized(self, handler, user, *args): return user.username != 'denied'
    class DefaultContents(RequestHandler):
        def get(self, path):
            default_calls.append(path); self.finish('default-filesystem-bytes')
        head = post = put = patch = delete = get
    class Store:
        def read(self, *args, **kwargs): pytest.fail('failed-navigation route read SourceStore')
    class HTTP(AsyncHTTPTestCase):
        def get_app(self):
            app = Application([], identity_provider=Identity(), authorizer=Authorizer(),
                onec_bsl_source_store=Store(), cookie_secret='test-only', xsrf_cookies=True,
                base_url='/base/', allow_remote_access=True)
            # General HostMatches route is deliberately registered first.
            app.add_handlers('.*$', [(r'/base/api/contents/(.*)', DefaultContents)])
            module.register_source_fallback(app)
            return app
    case = HTTP(); case.setUp()
    try:
        headers = {'X-Test-User':'alice', 'Cookie':'_xsrf=' + 'a' * 32, 'X-XSRFToken':'a' * 32}
        prefix = '/base/api/contents/.lsp_symlink/onec-bsl'
        for colon in (':', '%3A', '%3a'):
            for suffix in ('', 'invalid', 'a' * 32 + '/' + 'b' * 64 + '/Module.bsl',
                           '../other.py', '%2e%2e/private.json', 'bad/checkpoints', 'bad/pins/editor'):
                url = prefix + colon + suffix
                assert case.fetch(url, headers=headers).code == 404
                head = case.fetch(url, method='HEAD', headers=headers)
                assert head.code == 404 and head.body == b''
                for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                    assert case.fetch(url, method=method, body='{}' if method != 'DELETE' else None, headers=headers).code == 405
        for url in ('/base/api/contents/%2Elsp_symlink%2Fonec-bsl%3Ainvalid',
                    '/base/api/contents/.lsp_symlink/onec-bsl:invalid'):
            assert case.fetch(url).code == 403
            assert case.fetch(url, headers={**headers,'X-Test-User':'denied'}).code == 403
            assert case.fetch(url, headers=headers).code == 404
        assert default_calls == []
        assert case.fetch('/base/api/contents/.lsp_symlink/other:Module.bsl', headers=headers).body == b'default-filesystem-bytes'
        assert default_calls == ['.lsp_symlink/other:Module.bsl']
    finally: case.tearDown()
