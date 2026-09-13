import asyncio
from importlib import import_module
import json
import os
from pathlib import Path
from queue import Empty
from types import SimpleNamespace

import pytest

from onec_runtime.session import RuntimeSessionConfig


def kernel_api():
    return import_module('onec_runtime_jupyter.lsp_kernel')


def client_api():
    return import_module('onec_runtime_jupyter.lsp_kernel_client')


class Comm:
    def __init__(self):
        self.messages = []
        self.closed = False

    def send(self, data):
        self.messages.append(data)

    def on_msg(self, callback):
        self.message_callback = callback

    def on_close(self, callback):
        self.close_callback = callback

    def close(self):
        self.closed = True


class CommManager:
    def register_target(self, name, callback):
        self.target_name, self.callback = name, callback

    def unregister_target(self, name, callback):
        assert (name, callback) == (self.target_name, self.callback)
        self.callback = None


class WeakReferenceableShell:
    def __init__(self, manager):
        self.kernel = SimpleNamespace(comm_manager=manager)
        self.user_ns = {}


def shell():
    manager = CommManager()
    return SimpleNamespace(kernel=SimpleNamespace(comm_manager=manager), user_ns={}), manager


def runtime_config(tmp_path, root=None):
    return RuntimeSessionConfig(None, tmp_path / 'evidence', source_root=root)


def test_extension_unload_closes_project_comms_and_unregisters_target():
    from onec_runtime_jupyter.extension import unload_ipython_extension

    m = kernel_api()
    manager = CommManager()
    target_shell = WeakReferenceableShell(manager)
    bridge = m.install_project_bridge(target_shell, None)
    assert bridge is not None
    comm = Comm()
    manager.callback(comm, {'content': {'data': {'version': 2}}})

    unload_ipython_extension(target_shell)

    assert comm.closed
    assert manager.callback is None
    assert not hasattr(target_shell, '_onec_runtime_project_bridge')


def test_configuration_bridge_registers_before_install_and_late_clients_get_current_config(tmp_path):
    m = kernel_api()
    target_shell, manager = shell()
    bridge = m.install_project_bridge(target_shell, None)
    assert manager.target_name == 'onec.runtime.project.v2'
    waiting = Comm()
    manager.callback(waiting, {'content': {'data': {'version': 2}}})
    assert waiting.messages == [{
        'version': 2, 'bridge_epoch': 0, 'config': None,
        'reason': 'runtime-unavailable',
    }]

    root = tmp_path / 'project'
    root.mkdir()
    config = runtime_config(tmp_path, root)

    class Runtime:
        def __init__(self):
            self.config = config
            self.closed = False

        def source_snapshot(self):
            raise AssertionError('source snapshots must not be touched')

        def subscribe_sources(self):
            raise AssertionError('source subscriptions must not be touched')

        def status(self):
            raise AssertionError('Worker status must not be touched')

        def close(self):
            self.closed = True

    runtime = Runtime()
    assert bridge.replace(runtime)
    published = waiting.messages[-1]
    assert published['version'] == 2 and published['bridge_epoch'] == 1
    assert published['reason'] is None
    assert published['config']['source_root'] == str(root.resolve())
    assert len(published['config']['installation_id']) == 32

    runtime.close()
    late = Comm()
    manager.callback(late, {'content': {'data': {'version': 2}}})
    assert late.messages == [published]
    late.message_callback({})
    assert late.messages[-1] == published
    assert bridge.epoch == 1
    m.detach_project_bridge(target_shell)
    assert manager.callback is None and waiting.closed and late.closed


def test_install_replaces_configuration_including_none_and_adapter_is_virtual_only(tmp_path):
    m = kernel_api()
    target_shell, manager = shell()
    root = tmp_path / 'project'
    root.mkdir()
    first = SimpleNamespace(config=runtime_config(tmp_path, root))
    bridge = m.install_project_bridge(target_shell, first)
    comm = Comm()
    manager.callback(comm, {'content': {'data': {'version': 2}}})
    old = comm.messages[-1]
    assert m.install_project_bridge(target_shell, first) is bridge
    assert bridge.epoch == old['bridge_epoch']

    second = SimpleNamespace(config=runtime_config(tmp_path, None))
    assert m.install_project_bridge(target_shell, second) is bridge
    assert comm.messages[-1]['config']['source_root'] is None
    assert comm.messages[-1]['config']['installation_id'] != old['config']['installation_id']

    adapter = SimpleNamespace(config=SimpleNamespace(source_root=root))
    bridge.replace(adapter)
    assert comm.messages[-1]['config']['source_root'] is None
    assert comm.messages[-1]['reason'] is None
    m.detach_project_bridge(target_shell)


def test_bridge_is_noop_without_kernel_and_bounds_comms(tmp_path):
    m = kernel_api()
    assert m.install_project_bridge(SimpleNamespace(), object()) is None
    assert m.install_project_bridge(SimpleNamespace(kernel=object()), object()) is None
    target_shell, manager = shell()
    bridge = m.ProjectBridge(target_shell, None, m.ProjectLimits(max_comms=1))
    first, excess = Comm(), Comm()
    request = {'content': {'data': {'version': 2}}}
    manager.callback(first, request)
    manager.callback(excess, request)
    assert first.messages and excess.closed and not excess.messages
    bridge.close()


def test_bridge_enforces_full_envelope_limit_for_config_and_null_publication(tmp_path):
    m = kernel_api()
    config = runtime_config(tmp_path, None)
    configured = {
        'version': 2, 'bridge_epoch': 1,
        'config': {'installation_id': '1' * 32, 'source_root': None},
        'reason': None,
    }
    configured_size = len(json.dumps(
        configured, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8'))

    exact_shell, exact_manager = shell()
    exact = m.ProjectBridge(
        exact_shell, SimpleNamespace(config=config),
        m.ProjectLimits(max_message_bytes=configured_size),
    )
    exact_comm = Comm()
    exact_manager.callback(exact_comm, {'content': {'data': {'version': 2}}})
    assert exact_comm.messages[-1]['config'] is not None
    assert len(json.dumps(
        exact_comm.messages[-1], ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8')) == configured_size

    oversize_shell, oversize_manager = shell()
    oversize = m.ProjectBridge(
        oversize_shell, None,
        m.ProjectLimits(max_message_bytes=configured_size - 1),
    )
    oversize_comm = Comm()
    oversize_manager.callback(oversize_comm, {'content': {'data': {'version': 2}}})
    assert oversize.replace(SimpleNamespace(config=config))
    assert oversize_comm.messages[-1]['config'] is None
    assert oversize_comm.messages[-1]['reason'] == 'project-config-invalid-or-limited'

    null_message = {
        'version': 2, 'bridge_epoch': 0, 'config': None,
        'reason': 'runtime-unavailable',
    }
    null_size = len(json.dumps(
        null_message, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8'))
    limited_shell, limited_manager = shell()
    limited = m.ProjectBridge(
        limited_shell, None, m.ProjectLimits(max_message_bytes=null_size - 1),
    )
    limited_comm = Comm()
    limited_manager.callback(limited_comm, {'content': {'data': {'version': 2}}})
    assert limited_comm.closed and limited_comm.messages == []
    exact.close()
    oversize.close()
    limited.close()


def test_bridge_epoch_exhaustion_disconnects_without_wrapping_or_raising(tmp_path):
    m = kernel_api()
    target_shell, manager = shell()
    bridge = m.ProjectBridge(target_shell)
    comm = Comm()
    manager.callback(comm, {'content': {'data': {'version': 2}}})
    bridge._epoch = m.MAX_BRIDGE_EPOCH - 1
    assert bridge.replace(SimpleNamespace(config=runtime_config(tmp_path, None)))
    assert comm.messages[-1]['bridge_epoch'] == m.MAX_BRIDGE_EPOCH
    messages_at_limit = list(comm.messages)

    assert not bridge.replace(SimpleNamespace(config=runtime_config(tmp_path, tmp_path)))
    assert bridge.epoch == m.MAX_BRIDGE_EPOCH
    assert comm.closed and comm.messages == messages_at_limit
    late = Comm()
    manager.callback(late, {'content': {'data': {'version': 2}}})
    assert late.closed and late.messages == []
    bridge.close()


def test_kernel_client_correlates_incarnation_and_accepts_only_v2_config(tmp_path):
    project = import_module('onec_runtime_jupyter.lsp_project')
    m = client_api()
    sent, received, degraded = [], [], []

    class Session:
        session = 'client-route'

        def msg(self, kind, content):
            return {'header': {'msg_type': kind}, 'content': content}

    client = m.KernelProjectClient(
        None, 'kernel', lambda *args: received.append(args),
        lambda *args: degraded.append(args),
    )
    client.transport = SimpleNamespace(shell_channel=SimpleNamespace(send=sent.append), session=Session())
    client.closed = False
    client.info_request_id = 'request'
    assert client.accept_info({
        'header': {'session': 'kernel-incarnation'},
        'parent_header': {'msg_id': 'request'},
    })
    assert sent[-1]['content']['target_name'] == 'onec.runtime.project.v2'
    assert sent[-1]['content']['data'] == {'version': 2}
    config = project.ProjectConfig('1' * 32, str(tmp_path.resolve()))
    envelope = {
        'version': 2, 'bridge_epoch': 1,
        'config': project.encode_config(config), 'reason': None,
    }

    def message(data, *, incarnation='kernel-incarnation', comm_id=None):
        return {
            'header': {'session': incarnation, 'msg_type': 'comm_msg'},
            'content': {'comm_id': comm_id or client.comm_id, 'data': data},
        }

    assert client.accept_message(message(envelope))
    assert received == [(client, 'kernel-incarnation', config)]
    assert client.installation_id == config.installation_id
    assert client.accept_message(message(envelope))
    assert len(received) == 1, 'metadata heartbeat re-published an equal config'
    assert not client.accept_message(message({**envelope, 'bridge_epoch': 0}))
    assert not client.accept_message(message({
        **envelope,
        'config': project.encode_config(project.ProjectConfig('2' * 32, str(tmp_path.resolve()))),
    }))
    assert not client.accept_message(message(envelope, incarnation='old'))
    assert not client.accept_message(message(envelope, comm_id='stale'))


def test_malformed_message_does_not_discard_admitted_config_on_same_epoch_resend(tmp_path):
    contexts = import_module('onec_runtime_jupyter.lsp_contexts')
    project = import_module('onec_runtime_jupyter.lsp_project')
    registry = contexts.ContextRegistry()
    binding = registry.bind('alice', 'book.ipynb', 'kernel', 'file:///book.bsl')
    client = registry.client(binding)
    client.incarnation, client.comm_id = 'current', 'comm'
    (tmp_path / 'CommonModules').mkdir()
    config = project.ProjectConfig('1' * 32, str(tmp_path.resolve()))
    envelope = {
        'version': 2, 'bridge_epoch': 1,
        'config': project.encode_config(config), 'reason': None,
    }

    def message(data):
        return {
            'header': {'session': 'current', 'msg_type': 'comm_msg'},
            'content': {'comm_id': 'comm', 'data': data},
        }

    assert client.accept_message(message(envelope))
    assert not client.accept_message(message({**envelope, 'version': 1}))
    assert client.accept_message(message(envelope))
    assert registry.context(binding, owner='alice')['installation_id'] == '1' * 32
    registry.close()


@pytest.mark.parametrize('data', [
    {
        'version': 2, 'bridge_epoch': 1,
        'config': {'installation_id': '1' * 32, 'source_root': None},
        'reason': None,
    },
    {
        'version': 2, 'bridge_epoch': 1, 'config': None,
        'reason': 'runtime-unavailable',
    },
])
def test_kernel_client_rejects_complete_envelope_over_message_limit(data):
    m = client_api()
    size = len(json.dumps(
        data, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8'))
    reasons = []
    client = m.KernelProjectClient(
        None, 'kernel', lambda *_args: pytest.fail('oversize config accepted'),
        lambda _client, reason: reasons.append(reason),
        m.ProjectLimits(max_message_bytes=size - 1),
    )
    client.incarnation, client.comm_id = 'current', 'comm'
    message = {
        'header': {'session': 'current', 'msg_type': 'comm_msg'},
        'content': {'comm_id': 'comm', 'data': data},
    }
    assert not client.accept_message(message)
    assert reasons == ['project-config-invalid-or-limited']


@pytest.mark.parametrize('data', [
    {'version': 1, 'bridge_epoch': 1, 'config': None, 'reason': 'runtime-unavailable'},
    {'version': 2, 'bridge_epoch': True, 'config': None, 'reason': 'runtime-unavailable'},
    {'version': 2, 'bridge_epoch': 2**53, 'config': None, 'reason': 'runtime-unavailable'},
    {'version': 2, 'bridge_epoch': 1, 'config': None, 'reason': 'private-path'},
    {'version': 2, 'bridge_epoch': 1, 'config': None, 'reason': None},
    {'version': 2, 'bridge_epoch': 1, 'config': {'installation_id': 'bad', 'source_root': None}, 'reason': None},
    {'version': 2, 'bridge_epoch': 1, 'config': None, 'reason': 'runtime-unavailable', 'snapshot': {}},
])
def test_kernel_client_rejects_invalid_envelope_without_disclosing_payload(data):
    m = client_api()
    reasons = []
    client = m.KernelProjectClient(
        None, 'kernel', lambda *_args: pytest.fail('invalid config accepted'),
        lambda _client, reason: reasons.append(reason),
    )
    client.closed = False
    client.incarnation, client.comm_id = 'current', 'comm'
    message = {
        'header': {'session': 'current', 'msg_type': 'comm_msg'},
        'content': {'comm_id': 'comm', 'data': data},
    }
    assert not client.accept_message(message)
    assert reasons == ['project-config-invalid-or-limited']
    assert 'private-path' not in str(reasons)


def test_client_reports_runtime_unavailable_and_reconnects_new_incarnation():
    m = client_api()
    sent, degraded = [], []

    class Session:
        session = 'client-route'

        def msg(self, kind, content):
            return {'header': {'msg_type': kind}, 'content': content}

    client = m.KernelProjectClient(
        None, 'kernel', lambda *_args: None,
        lambda _client, reason: degraded.append(reason),
    )
    client.transport = SimpleNamespace(shell_channel=SimpleNamespace(send=sent.append), session=Session())
    client.closed = False
    client.info_request_id = 'first'
    assert client.accept_info({'header': {'session': 'old'}, 'parent_header': {'msg_id': 'first'}})
    old_comm = client.comm_id
    unavailable = {
        'header': {'session': 'old', 'msg_type': 'comm_msg'},
        'content': {'comm_id': old_comm, 'data': {
            'version': 2, 'bridge_epoch': 0, 'config': None,
            'reason': 'runtime-unavailable',
        }},
    }
    assert not client.accept_message(unavailable)
    assert degraded[-1] == 'runtime-unavailable'
    client.info_request_id = 'second'
    assert client.accept_info({'header': {'session': 'new'}, 'parent_header': {'msg_id': 'second'}})
    assert any(
        item['header']['msg_type'] == 'comm_close' and item['content']['comm_id'] == old_comm
        for item in sent
    )
    assert degraded[-1] == 'kernel-incarnation-changed'


@pytest.mark.parametrize('message', [
    None,
    [],
    {'parent_header': [], 'header': {'session': 'kernel'}},
    {'parent_header': {'msg_id': 'request'}, 'header': []},
])
def test_kernel_client_rejects_malformed_kernel_info_without_exception(message):
    client = client_api().KernelProjectClient(None, 'kernel', lambda *_args: None)
    client.info_request_id = 'request'
    assert not client.accept_info(message)


def test_initial_busy_kernel_keeps_transport_and_close_is_finite():
    m = client_api()

    class Transport:
        session = SimpleNamespace(session='manager-identity')

        def start_channels(self):
            pass

        def stop_channels(self):
            self.stopped = True

        def kernel_info(self):
            return 'pending'

        async def get_shell_msg(self, timeout):
            raise Empty

        async def get_iopub_msg(self, timeout):
            await asyncio.Event().wait()

    transport = Transport()
    client = m.KernelProjectClient(
        SimpleNamespace(get_kernel=lambda _id: SimpleNamespace(client=lambda: transport)),
        'kernel', lambda *_args: None,
    )

    async def run():
        await client.start()
        assert client.transport is transport and client._monitor_task is not None
        client.close()
        await asyncio.sleep(0)
        assert client.closed and client._task is None and client._monitor_task is None
        assert transport.stopped

    asyncio.run(run())


def test_shared_kernel_clients_use_unique_router_identities():
    m = client_api()
    transports = []

    class Session:
        session = 'manager-route'

        def msg(self, kind, content):
            return {'header': {'msg_type': kind}, 'content': content}

    class Transport:
        def __init__(self):
            self.session = Session()
            self.shell_channel = SimpleNamespace(send=lambda _message: None)

        def start_channels(self):
            pass

        def stop_channels(self):
            pass

        def kernel_info(self):
            return 'request'

        async def get_shell_msg(self, timeout):
            return {
                'header': {'session': 'shared-kernel-incarnation'},
                'parent_header': {'msg_id': 'request'},
            }

    def new_transport():
        transport = Transport()
        transports.append(transport)
        return transport

    manager = SimpleNamespace(client=new_transport)
    mapping = SimpleNamespace(get_kernel=lambda _kernel_id: manager)
    clients = [m.KernelProjectClient(mapping, 'shared', lambda *_args: None) for _ in range(2)]

    async def run():
        await asyncio.gather(*(client._connect() for client in clients))
        assert clients[0].incarnation == clients[1].incarnation == 'shared-kernel-incarnation'
        assert len({client.transport.session.session for client in clients}) == 2
        for client in clients:
            client.close()

    asyncio.run(run())


def test_real_ipykernel_late_install_restart_and_closed_runtime_keep_config():
    from jupyter_client import AsyncKernelManager

    m = client_api()
    workspace = Path(__file__).parents[2]
    startup = r'''
from pathlib import Path
from onec_runtime.session import RuntimeSessionConfig
from onec_runtime_jupyter.extension import load_ipython_extension
from onec_runtime_jupyter.lsp_kernel import install_project_bridge
from tornado.ioloop import IOLoop

class ConfiguredRuntime:
    def __init__(self):
        self.config = RuntimeSessionConfig(None, Path.cwd(), source_root=None)
        self.closed = False
    def close(self):
        self.closed = True

_project_runtime = ConfiguredRuntime()
load_ipython_extension(get_ipython())
def _late_install():
    install_project_bridge(get_ipython(), _project_runtime)
    _project_runtime.close()
IOLoop.current().call_later(0.2, _late_install)
'''

    async def run():
        environment = os.environ.copy()
        environment['PYTHONPATH'] = os.pathsep.join((
            str(workspace / 'src'), str(workspace / 'packages/jupyter/src'),
            str(workspace / 'packages/mcp/src'), str(workspace),
        ))
        manager = AsyncKernelManager()
        await manager.start_kernel(
            extra_arguments=['--IPKernelApp.exec_lines=' + json.dumps([startup])],
            env=environment,
        )
        configured = asyncio.Event()
        received = []

        def capture(_client, incarnation, config):
            received.append((incarnation, config))
            configured.set()

        client = m.KernelProjectClient(
            SimpleNamespace(get_kernel=lambda _kernel_id: manager), 'fixture', capture,
        )
        late_client = None
        try:
            await client.start()
            await asyncio.wait_for(configured.wait(), 10)
            first_incarnation, first = received[-1]
            assert first.source_root is None
            assert first_incarnation != client.transport.session.session

            late_configured = asyncio.Event()
            late_received = []

            def capture_after_close(_client, incarnation, config):
                late_received.append((incarnation, config))
                late_configured.set()

            late_client = m.KernelProjectClient(
                SimpleNamespace(get_kernel=lambda _kernel_id: manager),
                'fixture', capture_after_close,
            )
            await late_client.start()
            await asyncio.wait_for(late_configured.wait(), 10)
            assert late_received[-1][1] == first

            configured.clear()
            await manager.restart_kernel(now=True)
            await asyncio.wait_for(configured.wait(), 15)
            second_incarnation, second = received[-1]
            assert second_incarnation != first_incarnation
            assert second.installation_id != first.installation_id
        finally:
            client.close()
            if late_client is not None:
                late_client.close()
            await manager.shutdown_kernel(now=True)

    import sys
    loop_factory = asyncio.SelectorEventLoop if sys.platform == 'win32' else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(run())
