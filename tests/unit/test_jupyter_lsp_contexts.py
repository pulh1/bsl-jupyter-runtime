from importlib import import_module, util
from math import inf, nan
from types import SimpleNamespace

import pytest

from onec_runtime_jupyter.lsp_project import ProjectConfig


def api():
    assert util.find_spec('onec_runtime_jupyter.lsp_contexts'), 'server-owned contexts missing'
    return import_module('onec_runtime_jupyter.lsp_contexts')


def configured(root=None, identity='1' * 32):
    return ProjectConfig(identity, None if root is None else str(root))


def admit(registry, binding, config, incarnation='i'):
    client = registry.client(binding)
    client.incarnation, client.installation_id = incarnation, config.installation_id
    assert registry.accept_config(binding, incarnation, config, client=client)
    return client


def test_binding_lease_reclaims_config_and_ignores_private_activity(monkeypatch):
    m = api()
    now = [0.0]
    registry = m.ContextRegistry(max_bindings=1, lease_seconds=5, clock=lambda: now[0])
    binding = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    config = configured()
    client = admit(registry, binding, config)
    registry.attach('socket', owner='alice', binding=binding)
    now[0] = 4
    registry.connection_contexts('socket', owner='alice')
    registry.select('socket', owner='alice', bindings=[binding])
    assert not registry.accept_config(binding, 'i', config, client=client)
    epoch, revision = registry.status(binding).epoch, registry.revision
    monkeypatch.setattr(m, 'encode_config', lambda *_: pytest.fail('heartbeat encoded config'))
    registry.renew(binding, owner='alice')
    assert registry.status(binding).epoch == epoch and registry.revision == revision
    now[0] = 8
    assert registry.association(binding, owner='alice')['kernel_id'] == 'k'
    now[0] = 9
    assert registry.prune() == 1
    assert registry.connection_contexts('socket', owner='alice') == []
    assert client.closed and registry.associations(owner='alice') == []
    assert not registry.accept_config(binding, 'i', configured(identity='2' * 32), client=client)
    with pytest.raises(KeyError):
        registry.renew(binding, owner='alice')
    replacement = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    assert replacement != binding
    registry.close()


def test_expired_selected_binding_is_pruned_before_private_context_poll():
    m = api()
    now = [0.0]
    registry = m.ContextRegistry(lease_seconds=5, clock=lambda: now[0])
    binding = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    client = registry.client(binding)
    registry.attach('socket', owner='alice', binding=binding)
    assert [context['binding_id'] for context in registry.connection_contexts(
        'socket', owner='alice',
    )] == [binding]

    now[0] = 5.0
    assert registry.connection_contexts('socket', owner='alice') == []
    assert registry.associations(owner='alice') == []
    assert client.closed


def test_concurrent_posts_allocate_fresh_bindings_and_orphans_expire_without_reuse():
    m = api()
    now = [0.0]
    registry = m.ContextRegistry(max_bindings=3, lease_seconds=5, clock=lambda: now[0])
    first = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    client = registry.client(first)
    registry.attach('old', owner='alice', binding=first)
    second = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    assert second != first
    registry.attach('new', owner='alice', binding=second)
    registry.detach('old', owner='alice')
    third = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    assert third not in (first, second)
    assert registry.client(first) is client
    with pytest.raises(ValueError, match='context-binding-limit'):
        registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    now[0] = 5
    assert registry.bind('bob', 'b.ipynb', 'k', 'file:///b.bsl')
    assert registry.associations(owner='alice') == []
    registry.close()


def test_atomic_selection_cannot_steal_another_sockets_binding():
    registry = api().ContextRegistry()
    first = registry.bind('alice', 'a.ipynb', None, 'file:///a.bsl')
    second = registry.bind('alice', 'a.ipynb', None, 'file:///a.bsl')
    assert first != second
    registry.select('first', owner='alice', bindings=[first])
    registry.select('second', owner='alice', bindings=[second])
    with pytest.raises(KeyError):
        registry.select('second', owner='alice', bindings=[first])
    assert registry.connection_contexts('second', owner='alice')[0]['binding_id'] == second
    registry.detach('first', owner='alice')
    registry.select('second', owner='alice', bindings=[first])
    assert registry.connection_contexts('second', owner='alice')[0]['binding_id'] == first
    registry.close()


@pytest.mark.parametrize('reason', ['workspace-unsafe', 'workspace-safety-limit', 'workspace-unavailable',
                                  'workspace-replaced', 'workspace-notification-unavailable', 'workspace-stopped'])
def test_degraded_virtual_child_status_preserves_project_reason_and_reports_virtual_mode(reason):
    status = api().ContextStatus('a', 2, 'project', None, 'unavailable', reason, 'i')
    assert status.to_wire() == dict(binding_id='a', epoch=2, mode='virtual-only', reason=reason,
                                   analysis_state='unavailable', analysis_reason=reason, installation_id='i')


def test_real_ipykernel_lost_browser_churn_reclaims_observers_past_comm_capacity():
    import asyncio
    import json
    import os
    from pathlib import Path
    import sys
    from jupyter_client import AsyncKernelManager
    from onec_runtime_jupyter.lsp_project import ProjectLimits
    startup = '''from pathlib import Path
from types import SimpleNamespace
from onec_runtime.session import RuntimeSessionConfig
from onec_runtime_jupyter.lsp_kernel import install_project_bridge
install_project_bridge(get_ipython(), SimpleNamespace(config=RuntimeSessionConfig(None, Path.cwd(), source_root=None)))
'''
    async def run():
        manager = AsyncKernelManager()
        workspace = Path(__file__).parents[2]
        environment = {**os.environ, 'PYTHONPATH':os.pathsep.join(str(workspace / part) for part in
            ('src', 'packages/jupyter/src', 'packages/mcp/src', '.'))}
        await manager.start_kernel(extra_arguments=['--IPKernelApp.exec_lines=' + json.dumps([startup])], env=environment)
        control = manager.client()
        control.start_channels()
        now = [0.]
        registry = api().ContextRegistry(SimpleNamespace(get_kernel=lambda _:manager),
            max_bindings=1, lease_seconds=1, clock=lambda:now[0])
        try:
            await control.wait_for_ready(timeout=20)
            installation = None
            for cycle in range(ProjectLimits().max_comms + 2):
                binding = registry.bind('alice', 'a.ipynb', 'fixture', 'file:///a.bsl')
                await registry.start(binding)
                async def configured():
                    while registry.status(binding).installation_id is None:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(configured(), 10)
                current = registry.status(binding).installation_id
                installation = installation or current
                assert current == installation
                registry.select('browser', owner='alice', bindings=[binding])
                client = registry.client(binding)
                tasks = [task for task in (client._task, client._monitor_task) if task]
                # Browser loss: neither DELETE nor private polling may renew it.
                now[0] += 1.01
                assert registry.connection_contexts('browser', owner='alice') == []
                assert client.closed and registry.associations(owner='alice') == []
                await asyncio.gather(*tasks)
                async def comms_reclaimed():
                    while True:
                        request = control.execute('', silent=True, user_expressions={
                            'count':'len(get_ipython()._onec_runtime_project_bridge._comms)'})
                        while True:
                            reply = await control.get_shell_msg(timeout=5)
                            if reply['parent_header'].get('msg_id') == request:
                                break
                        result = reply['content']['user_expressions']['count']
                        assert result['status'] == 'ok', result
                        if result['data']['text/plain'] == '0': return
                        await asyncio.sleep(.01)
                await asyncio.wait_for(comms_reclaimed(), 5)
        finally:
            registry.close()
            control.stop_channels()
            await manager.shutdown_kernel(now=True)
    loop_factory = asyncio.SelectorEventLoop if sys.platform == 'win32' else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(run())


@pytest.mark.parametrize('lease', [0, -1, True, inf, nan, '300'])
def test_binding_lease_requires_a_finite_positive_number(lease):
    with pytest.raises(ValueError, match='^invalid-binding-lease$'):
        api().ContextRegistry(lease_seconds=lease)


def test_binding_is_owner_scoped_and_only_its_client_can_configure(tmp_path):
    m = api()
    (tmp_path / 'CommonModules').mkdir()
    registry = m.ContextRegistry()
    binding = registry.bind('alice', 'book.ipynb', 'kernel', 'file:///book.ipynb.python-bsl.bsl')
    client = registry.client(binding)
    client.incarnation = 'current'
    config = configured(tmp_path)
    client.installation_id = config.installation_id
    assert not registry.accept_config(binding, 'old', config, client=client)
    assert not registry.accept_config(binding, 'current', config)
    assert registry.status(binding).mode == 'virtual-only'
    assert registry.accept_config(binding, 'current', config, client=client)
    assert registry.status(binding).mode == 'project'
    revision = registry.revision
    assert not registry.accept_config(binding, 'current', config, client=client)
    assert registry.revision == revision
    with pytest.raises(KeyError):
        registry.context(binding, owner='bob')
    private = registry.context(binding, owner='alice')
    assert private == {
        'binding_id': binding,
        'epoch': 2,
        'kernel_id': 'kernel',
        'kernel_incarnation': 'current',
        'installation_id': '1' * 32,
        'source_root': str(tmp_path),
        'document_uri': 'file:///book.ipynb.python-bsl.bsl',
    }
    assert 'snapshot' not in private and 'sequence' not in private
    registry.unbind(binding, owner='alice')
    assert not registry.accept_config(binding, 'current', configured(identity='2' * 32), client=client)


def test_project_layout_is_explicit_safe_and_normalized(tmp_path):
    m = api()
    assert m.normalize_source_root(None) == (None, 'source-root-missing')
    assert m.normalize_source_root(tmp_path / 'missing')[0] is None
    (tmp_path / 'src' / 'CommonModules').mkdir(parents=True)
    assert m.normalize_source_root(tmp_path) == (tmp_path / 'src', None)
    (tmp_path / 'CommonModules').mkdir()
    assert m.normalize_source_root(tmp_path) == (None, 'source-root-ambiguous')


def test_private_context_changes_epoch_only_on_configuration_replacement(tmp_path):
    m = api()
    (tmp_path / 'CommonModules').mkdir()
    registry = m.ContextRegistry()
    binding = registry.bind('alice', 'book.ipynb', 'kernel', 'file:///doc.bsl')
    first_config = configured(tmp_path)
    client = admit(registry, binding, first_config, 'incarnation')
    first = registry.status(binding).epoch
    assert not registry.accept_config(binding, 'incarnation', first_config, client=client)
    assert registry.status(binding).epoch == first
    second_config = configured(tmp_path, '2' * 32)
    client.installation_id = second_config.installation_id
    assert registry.accept_config(binding, 'incarnation', second_config, client=client)
    assert registry.status(binding).epoch == first + 1


def test_frontend_cannot_supply_root_or_escape_notebook_association():
    m = api()
    with pytest.raises(ValueError):
        m.parse_binding_request({'notebook_path': 'x.ipynb', 'kernel_id': 'k', 'document_uri': 'file:///x.bsl', 'source_root': 'secret'})
    with pytest.raises(ValueError):
        m.parse_binding_request({'notebook_path': '../x.ipynb', 'kernel_id': 'k', 'document_uri': 'file:///x.bsl'})


def test_binding_authorization_checks_execute_contents_and_exact_session():
    import asyncio
    m = api()
    calls = []

    class Authorizer:
        def is_authorized(self, handler, user, action, resource):
            calls.append((action, resource))
            return resource != 'denied'

    class Contents:
        def get(self, path, content):
            assert path == 'book.ipynb' and content is False
            return {'type': 'notebook', 'path': path}

    class Sessions:
        def list_sessions(self):
            return [{'path': 'book.ipynb', 'kernel': {'id': 'real-kernel'}}]

    handler = SimpleNamespace(
        authorizer=Authorizer(), current_user=SimpleNamespace(username='alice'),
        contents_manager=Contents(), session_manager=Sessions(),
    )
    asyncio.run(m.authorize_association(handler, 'book.ipynb', 'real-kernel'))
    assert calls == [('execute', 'kernels'), ('read', 'contents')]
    with pytest.raises(PermissionError):
        asyncio.run(m.authorize_association(handler, 'book.ipynb', 'other-kernel'))


def test_kernel_restart_immediately_invalidates_previous_context(tmp_path):
    m = api()
    (tmp_path / 'CommonModules').mkdir()
    registry = m.ContextRegistry()
    binding = registry.bind('alice', 'book.ipynb', 'kernel', 'file:///doc.bsl')
    config = configured(tmp_path)
    client = admit(registry, binding, config, 'old')
    registry.invalidate_kernel('kernel')
    context = registry.context(binding, owner='alice')
    assert context['source_root'] is None and context['installation_id'] is None
    assert not registry.accept_config(binding, 'old', configured(tmp_path, '2' * 32), client=client)


def test_http_context_binding_auth_xsrf_status_privacy_and_owner_scope():
    pytest.importorskip('jupyter_server')
    import json
    from jupyter_server.auth import IdentityProvider, User
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application
    from onec_runtime_jupyter.lsp_server_extension import ContextHandler
    m = api()

    class Identity(IdentityProvider):
        def get_user(self, handler):
            name = handler.request.headers.get('X-Test-User')
            return User(name) if name else None

    class Authorizer:
        def is_authorized(self, handler, user, action, resource):
            return user.username != 'denied'

    class Contents:
        def get(self, path, content=False):
            return {'type': 'notebook', 'path': path}

    class Sessions:
        def __init__(self):
            self.sessions = [{'path': 'book.ipynb', 'kernel': {'id': 'kernel'}}]

        def list_sessions(self):
            return self.sessions

    class HTTP(AsyncHTTPTestCase):
        def get_app(self):
            self.registry = m.ContextRegistry()
            self.sessions = Sessions()
            return Application(
                [(r'/onec-bsl/contexts(?:/([a-f0-9]{32}))?', ContextHandler)],
                onec_bsl_context_registry=self.registry, identity_provider=Identity(),
                authorizer=Authorizer(), contents_manager=Contents(),
                session_manager=self.sessions, cookie_secret='test-only',
                xsrf_cookies=True, base_url='/', allow_remote_access=True,
            )

    case = HTTP()
    case.setUp()
    try:
        url = '/onec-bsl/contexts'
        payload = {'notebook_path': 'book.ipynb', 'kernel_id': 'kernel', 'document_uri': 'file:///doc.bsl'}
        body = json.dumps(payload)
        token = 'a' * 32
        headers = {'X-Test-User': 'alice', 'Cookie': '_xsrf=' + token, 'X-XSRFToken': token, 'Content-Type': 'application/json'}
        assert case.fetch(url, method='POST', body=body).code == 403
        assert case.fetch(url, method='POST', body=body, headers={'X-Test-User': 'alice'}).code == 403
        assert case.fetch(url, method='POST', body=body, headers={**headers, 'X-Test-User': 'denied'}).code == 403
        assert case.fetch(url, method='POST', body=json.dumps({**payload, 'source_root': 'secret'}), headers=headers).code == 400
        response = case.fetch(url, method='POST', body=body, headers=headers)
        assert response.code == 201, response.body
        status = json.loads(response.body)
        assert set(status) == {
            'binding_id', 'epoch', 'mode', 'reason', 'analysis_state',
            'analysis_reason', 'installation_id',
        }
        assert status['mode'] == 'virtual-only'
        bound = url + '/' + status['binding_id']
        assert case.fetch(bound, headers={**headers, 'X-Test-User': 'bob'}).code == 404
        assert case.fetch(bound, headers=headers).code == 200
        case.registry.attach('socket', owner='alice', binding=status['binding_id'])
        assert case.registry.acknowledge('socket', owner='alice', statuses=[dict(
            binding_id=status['binding_id'], epoch=status['epoch'], state='indexing', reason=None,
        )])
        assert json.loads(case.fetch(bound, headers=headers).body)['analysis_state'] == 'indexing'
        case.sessions.sessions = []
        assert case.fetch(bound, method='DELETE', headers={**headers, 'X-Test-User': 'bob'}).code == 404
        assert case.fetch(bound, method='DELETE').code == 403
        assert case.fetch(bound, method='DELETE', headers={'X-Test-User': 'alice'}).code == 403
        assert case.fetch(bound, method='DELETE', headers=headers).code == 204
        assert case.fetch(bound, headers=headers).code == 404
    finally:
        case.registry.close()
        case.tearDown()


def test_connection_selection_and_fenced_status_cannot_change_config():
    m = api()
    registry = m.ContextRegistry()
    a = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    b = registry.bind('alice', 'b.ipynb', 'k', 'file:///b.bsl')
    registry.attach('socket', owner='alice', binding=a)
    assert [c['binding_id'] for c in registry.connection_contexts('socket', owner='alice')] == [a]
    current_revision = registry.revision
    ack = dict(binding_id=a, epoch=1, state='ready', reason=None)
    assert not registry.acknowledge('other-socket', owner='alice', statuses=[ack])
    assert not registry.acknowledge('socket', owner='bob', statuses=[ack])
    assert not registry.acknowledge('socket', owner='alice', statuses=[{**ack, 'epoch': 0}])
    assert not registry.acknowledge('socket', owner='alice', statuses=[{**ack, 'epoch': True}])
    assert not registry.acknowledge('socket', owner='alice', statuses=[{**ack, 'source_root': 'forged'}])
    assert not registry.acknowledge('socket', owner='alice', statuses=[{**ack, 'sequence': None}])
    assert registry.acknowledge('socket', owner='alice', statuses=[ack])
    assert registry.status(a, owner='alice').to_wire()['analysis_state'] == 'ready'
    assert registry.status(b, owner='alice').to_wire()['analysis_state'] == 'unknown'
    assert registry.revision == current_revision
    registry.detach('socket', owner='alice')
    assert registry.status(a, owner='alice').to_wire()['analysis_state'] == 'unknown'


@pytest.mark.parametrize('reason', ['workspace-replaced', 'workspace-recovered',
    'workspace-layout-changed', 'workspace-unlocated-change', 'workspace-event-limit',
    'workspace-stopped', 'workspace-notification-unavailable', 'index-convergence-unconfirmed'])
def test_observer_status_is_public_but_cannot_renew_binding_or_change_config(reason):
    registry = api().ContextRegistry()
    binding = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    registry.attach('socket', owner='alice', binding=binding)
    before = registry.context(binding, owner='alice')
    revision = registry.revision
    assert registry.acknowledge('socket', owner='alice', statuses=[dict(
        binding_id=binding, epoch=before['epoch'], state='updating', reason=reason)])
    public = registry.status(binding, owner='alice').to_wire()
    assert public['analysis_state'] == 'updating' and public['analysis_reason'] == reason
    assert registry.context(binding, owner='alice') == before and registry.revision == revision
    assert not registry.acknowledge('socket', owner='alice', statuses=[dict(
        binding_id=binding, epoch=before['epoch'], state='updating', reason='C:/private/root')])
    registry.close()


def test_null_kernel_binding_is_contents_only_and_never_opens_kernel_client():
    import asyncio
    m = api()
    calls = []

    class Authorizer:
        def is_authorized(self, handler, user, action, resource):
            calls.append(resource)
            return True

    class Contents:
        def get(self, path, content):
            return {'type': 'notebook' if path == 'book.ipynb' else 'file'}

    async def run():
        handler = SimpleNamespace(current_user=object(), authorizer=Authorizer(), contents_manager=Contents())
        await m.authorize_association(handler, 'book.ipynb', None)
        assert calls == ['contents']
        with pytest.raises(PermissionError):
            await m.authorize_association(handler, 'foreign.ipynb', None)
        registry = m.ContextRegistry()
        binding = registry.bind('alice', 'book.ipynb', None, 'file:///a.bsl')
        await registry.start(binding)
        assert registry.status(binding).mode == 'virtual-only'
        assert registry.context(binding, owner='alice')['kernel_id'] is None
        registry.close()

    asyncio.run(run())


def test_association_poll_does_not_encode_private_config(monkeypatch):
    m = api()
    registry = m.ContextRegistry()
    binding = registry.bind('alice', 'a.ipynb', None, 'file:///a.bsl')
    monkeypatch.setattr(registry, 'context', lambda *_args, **_kwargs: pytest.fail('config encoded during association poll'))
    assert registry.associations(owner='alice') == [{
        'binding_id': binding, 'document_uri': 'file:///a.bsl',
        'notebook_path': 'a.ipynb', 'kernel_id': None,
    }]


def test_status_contains_only_project_identity_and_no_worker_generation(tmp_path):
    m = api()
    (tmp_path / 'CommonModules').mkdir()
    registry = m.ContextRegistry()
    binding = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    admit(registry, binding, configured(tmp_path))
    status = registry.status(binding, owner='alice').to_wire()
    assert status == {
        'binding_id': binding, 'epoch': 2, 'mode': 'project', 'reason': None,
        'analysis_state': 'unknown', 'analysis_reason': None,
        'installation_id': '1' * 32,
    }
    assert not ({'runtime_id', 'sequence', 'active_generation',
                 'retained_generations', 'operation_generation'} & set(status))
    registry.close()


def test_unsafe_child_status_reports_virtual_only_without_mutating_authoritative_root(tmp_path):
    m = api()
    (tmp_path / 'CommonModules').mkdir()
    registry = m.ContextRegistry()
    binding = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
    admit(registry, binding, configured(tmp_path), 'inc')
    registry.attach('socket', owner='alice', binding=binding)
    assert registry.acknowledge('socket', owner='alice', statuses=[dict(
        binding_id=binding, epoch=registry.status(binding).epoch,
        state='unavailable', reason='workspace-unsafe',
    )])
    assert registry.status(binding).to_wire()['mode'] == 'virtual-only'
    assert registry.context(binding, owner='alice')['source_root'] == str(tmp_path)
    registry.close()
