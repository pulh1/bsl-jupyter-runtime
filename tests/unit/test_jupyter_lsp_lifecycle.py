"""Failure, thread-affinity and lifetime regressions without a live kernel/LS."""
import asyncio
from types import MethodType, SimpleNamespace

import pytest


def handler(registry):
    pytest.importorskip('jupyter_server')
    from onec_runtime_jupyter.lsp_websocket import BslWebSocketHandler
    class Socket(SimpleNamespace):
        __hash__ = object.__hash__
    result = Socket(registry=registry, owner='alice', connection_id='socket',
        documents=set(), selected=set(), claims={}, selection_lock=asyncio.Lock(),
        writer=asyncio.Lock(), _closed=asyncio.Event(), tasks=[], ping_callback=None,
        owned=None, control=None, settings={'onec_bsl_sockets': set()},
        current_user=SimpleNamespace(username='alice'),
        authorizer=SimpleNamespace(is_authorized=lambda *args: True),
        contents_manager=SimpleNamespace(get=lambda *args, **kwargs: {'type': 'notebook'}))
    for name in ('_select', '_claim', '_cleanup', 'on_close', 'wait_closed'):
        setattr(result, name, MethodType(getattr(BslWebSocketHandler, name), result))
    result.settings['onec_bsl_sockets'].add(result)
    return result


@pytest.mark.parametrize('failed_stage', ['detach', 'owner', 'control'])
def test_socket_cleanup_attempts_independent_stages_and_observes_failure(failed_stage):
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    async def run():
        registry = ContextRegistry()
        socket = handler(registry)
        stages = []
        failure = RuntimeError(failed_stage)
        def stage(name):
            stages.append(name)
            if failed_stage == name:
                raise failure
        async def owned_close(**kwargs): stage('owner')
        registry.detach = lambda *args, **kwargs: stage('detach')
        socket.owned = SimpleNamespace(close=owned_close)
        socket.control = SimpleNamespace(close=lambda: stage('control'))
        task = asyncio.create_task(asyncio.Event().wait())
        socket.tasks.append(task)
        socket.on_close()
        await asyncio.sleep(.05)
        try:
            assert stages == ['detach', 'owner', 'control']
            assert not socket.settings['onec_bsl_sockets']
            assert task.done()
            # Ordinary on_close must retrieve the exception even without a waiter.
            assert not socket.cleanup_task._log_traceback
            with pytest.raises(RuntimeError) as error:
                await socket.wait_closed()
            assert error.value is failure
        finally:
            await asyncio.gather(socket.cleanup_task, task, return_exceptions=True)
            registry.close()
    asyncio.run(run())


def test_control_worker_prune_closes_real_tasks_and_channels_on_owner_loop():
    from threading import get_ident
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    from onec_runtime_jupyter.lsp_control import ControlServer, ControlClient
    async def run():
        now = [0]
        registry = ContextRegistry(clock=lambda: now[0], lease_seconds=1)
        binding = registry.bind('alice', 'a.ipynb', 'kernel', 'file:///a.bsl')
        registry.attach('socket', owner='alice', binding=binding)
        client = registry.client(binding)
        await client.start()  # No kernel manager, but establishes the owning loop.
        owner = get_ident()
        operations = []
        waiter = asyncio.get_running_loop().create_future()
        async def listen():
            try:
                await waiter
            finally:
                operations.append(('task', get_ident()))
                registry.contexts(owner='alice')  # Must not deadlock behind the pruning lock.
        task = client._task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        wakeup = waiter._callbacks[0][0]
        client.comm_id = 'comm'
        client.transport = SimpleNamespace(
            session=SimpleNamespace(msg=lambda *args: args),
            shell_channel=SimpleNamespace(send=lambda message: operations.append(('comm', get_ident()))),
            stop_channels=lambda: operations.append(('channels', get_ident())))
        provider_threads = []
        def provider():
            provider_threads.append(get_ident())
            result = registry.connection_contexts('socket', owner='alice')
            assert binding not in registry._bindings and client.closed
            return {'contexts': result}
        server = ControlServer(provider)
        def request():
            with ControlClient.from_environment(server.child_environment()) as peer:
                return peer.request()
        try:
            now[0] = 2
            result = await asyncio.to_thread(request)
            assert result == {'contexts': []}
            await registry.wait_closed()
            assert task.done() and client.transport is None
            assert sorted(name for name, _ in operations) == ['channels', 'comm', 'task']
            assert all(thread == owner for _, thread in operations)
            assert provider_threads and provider_threads[0] != owner
        finally:
            # Old asyncio debug cancellation can set the future cancelled before
            # rejecting its cross-thread callback. Wake the original task honestly.
            await asyncio.to_thread(server.close)
            registry.close()
            if not task.done() and waiter.cancelled():
                # RED recovery for the C Task: restore the rejected callback.
                asyncio.get_running_loop().call_soon(wakeup, waiter)
            elif not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run(), debug=True)


def test_server_unload_attempts_all_sockets_registry_and_store_after_failure():
    pytest.importorskip('jupyter_server')
    from onec_runtime_jupyter.lsp_server_extension import _unload_jupyter_server_extension
    async def run():
        stages = []
        failure = RuntimeError('first socket')
        async def first():
            stages.append('wait-first')
            raise failure
        async def second(): stages.append('wait-second')
        async def registry_done(): pass
        settings = {
            'onec_bsl_binding_reaper': SimpleNamespace(stop=lambda: stages.append('reaper')),
            'onec_bsl_sockets': [
                SimpleNamespace(close=lambda: stages.append('close-first'), on_close=lambda: None, wait_closed=first),
                SimpleNamespace(close=lambda: stages.append('close-second'), on_close=lambda: None, wait_closed=second)],
            'onec_bsl_context_registry': SimpleNamespace(close=lambda: stages.append('registry'), wait_closed=registry_done),
            'onec_bsl_source_store': SimpleNamespace(close=lambda: stages.append('store'))}
        with pytest.raises(RuntimeError) as error:
            await _unload_jupyter_server_extension(SimpleNamespace(web_app=SimpleNamespace(settings=settings)))
        assert error.value is failure
        assert stages == ['reaper', 'close-first', 'close-second', 'wait-first', 'wait-second', 'registry', 'store']
    asyncio.run(run())


def test_expired_preopen_capacity_reclaimed_on_same_socket_without_stale_selection():
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    async def run():
        now = [0]
        registry = ContextRegistry(clock=lambda: now[0], lease_seconds=300)
        socket = handler(registry)
        try:
            for index in range(32):
                uri = f'file:///{index}.bsl'
                binding = registry.bind('alice', f'{index}.ipynb', None, uri)
                await socket._claim({'document_uri': uri, 'binding_id': binding})
            now[0] = 31
            fresh = registry.bind('alice', 'fresh.ipynb', None, 'file:///fresh.bsl')
            await socket._claim({'document_uri': 'file:///fresh.bsl', 'binding_id': fresh})
            assert set(socket.claims) == {'file:///fresh.bsl'}
            socket.documents.update({'file:///0.bsl', 'file:///fresh.bsl'})
            await socket._select()
            assert socket.selected == {fresh}
        finally:
            registry.close()
    asyncio.run(run())


def test_gateway_retires_metadata_under_sequential_binding_churn():
    from onec_runtime_jupyter.lsp_gateway import Gateway
    from test_jupyter_lsp_gateway import Child, context
    async def run():
        gateway = Gateway(lambda _: None, child_factory=Child)
        try:
            for index in range(100):
                binding = str(index)
                await gateway.accept_contexts([context(binding)])
                await gateway._child(binding)
                await gateway.accept_contexts([])
            assert not gateway.children and not gateway.applied
            assert not gateway.locks and not gateway.last_used
        finally:
            await gateway.close()
    asyncio.run(run())


def test_gateway_retirement_waits_for_users_and_reappearance_reuses_the_lock():
    from onec_runtime_jupyter.lsp_gateway import Gateway, GatewayError
    from test_jupyter_lsp_gateway import Child, context
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        built = []
        class BlockingChild(Child):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                built.append(self)
            async def synchronize(self, value):
                entered.set()
                await release.wait()
                await super().synchronize(value)
        gateway = Gateway(lambda _: None, child_factory=BlockingChild)
        work = []
        try:
            await gateway.accept_contexts([context()])
            first = asyncio.create_task(gateway._child('a'))
            work.append(first)
            await entered.wait()
            lock = gateway.locks['a']
            waiter = asyncio.create_task(gateway._child('a'))
            retired = asyncio.create_task(gateway.accept_contexts([]))
            work.extend([waiter, retired])
            await asyncio.sleep(0)
            assert not waiter.done() and not retired.done()
            cancelled = asyncio.create_task(gateway._child('a'))
            work.append(cancelled)
            await asyncio.sleep(0)
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            assert gateway.locks['a'] is lock
            reappeared = asyncio.create_task(gateway.accept_contexts([context(epoch=2)]))
            work.append(reappeared)
            await asyncio.sleep(0)
            assert gateway.locks['a'] is lock and not reappeared.done()
            release.set()
            results = await asyncio.gather(*work, return_exceptions=True)
            assert all(not isinstance(result, BaseException) for result in results if not isinstance(result, asyncio.CancelledError))
            assert len(built) == 1 and not built[0].closed
            assert gateway.children['a'] is built[0]
            assert gateway.locks['a'] is lock
            assert not gateway.lock_users
            await gateway.accept_contexts([])
            assert built[0].closed and not gateway.locks and not gateway.last_used
        finally:
            release.set()
            await asyncio.gather(*work, return_exceptions=True)
            await gateway.close()
    asyncio.run(run())


def test_gateway_retirement_reclaims_failed_creation_metadata():
    from onec_runtime_jupyter.lsp_gateway import Gateway, GatewayError
    from test_jupyter_lsp_gateway import Child, context
    async def run():
        gateway = Gateway(lambda _: None, child_factory=Child, max_children=0)
        try:
            await gateway.accept_contexts([context()])
            with pytest.raises(GatewayError):
                await gateway._child('a')
            await gateway.accept_contexts([])
            assert not gateway.locks and not gateway.last_used
        finally:
            await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize('stage_cancels', [False, True])
def test_socket_cancellation_finishes_independent_cleanup_before_propagating(stage_cancels):
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    async def run():
        registry = ContextRegistry()
        socket = handler(registry)
        entered, release = asyncio.Event(), asyncio.Event()
        stages = []
        async def owned_close(**kwargs):
            entered.set()
            await release.wait()
            stages.append('owner')
            if stage_cancels:
                raise asyncio.CancelledError()
        socket.owned = SimpleNamespace(close=owned_close)
        socket.control = SimpleNamespace(close=lambda: stages.append('control'))
        socket.on_close()
        await entered.wait()
        wait = asyncio.create_task(socket.wait_closed())
        await asyncio.sleep(0)
        if not stage_cancels:
            wait.cancel()
            socket.cleanup_task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(wait, 1)
        assert stages == ['owner', 'control']
        assert not socket.settings['onec_bsl_sockets']
        assert socket.cleanup_task.done()
        registry.close()
    asyncio.run(run())


def test_explicit_mode_preserves_selected_binding_and_failed_claim_keeps_legacy_fallback():
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    async def run():
        registry = ContextRegistry()
        socket = handler(registry)
        a = registry.bind('alice', 'a.ipynb', None, 'file:///a.bsl')
        b = registry.bind('alice', 'b.ipynb', None, 'file:///b.bsl')
        try:
            socket.authorizer.is_authorized = lambda *args: False
            with pytest.raises(PermissionError):
                await socket._claim({'document_uri': 'file:///a.bsl', 'binding_id': a})
            socket.authorizer.is_authorized = lambda *args: True
            socket.documents.add('file:///a.bsl')
            await socket._select()
            assert socket.selected == {a}
            socket.documents.add('file:///b.bsl')
            await socket._claim({'document_uri': 'file:///b.bsl', 'binding_id': b})
            assert socket.selected == {a, b}
            registry.unbind(a, owner='alice')
            registry.bind('alice', 'a.ipynb', None, 'file:///a.bsl')
            await socket._select()
            assert socket.selected == {b}
        finally:
            registry.close()
    asyncio.run(run())


def test_delayed_worker_close_drains_old_tasks_before_starting_new_incarnation():
    from threading import Thread
    from onec_runtime_jupyter.lsp_kernel_client import KernelProjectClient
    async def run():
        client = KernelProjectClient(None, 'kernel', lambda *args: None)
        await client.start()
        release = asyncio.Event()
        stopped = []
        async def old_listener():
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()
                stopped.append('old-task')
        old = client._task = asyncio.create_task(old_listener())
        client.transport = SimpleNamespace(stop_channels=lambda: stopped.append('old-channel'))
        await asyncio.sleep(0)
        worker = Thread(target=client.close)
        worker.start()
        worker.join(timeout=1)  # No owner-loop progress until start below.
        assert not worker.is_alive() and client.closed
        restart = asyncio.create_task(client.start())
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not restart.done()
            release.set()
            await asyncio.wait_for(restart, 1)
            assert old.done() and stopped == ['old-channel', 'old-task']
            assert not client.closed
            new = client._task = asyncio.create_task(asyncio.Event().wait())
            client.transport = SimpleNamespace(stop_channels=lambda: stopped.append('new-channel'))
            await asyncio.sleep(0)
            assert not new.done() and stopped == ['old-channel', 'old-task']
            client.close()
            await client.wait_closed()
            assert new.done() and stopped[-1] == 'new-channel'
        finally:
            release.set()
            await asyncio.gather(restart, return_exceptions=True)
            client.close()
            await client.wait_closed()
    asyncio.run(run(), debug=True)


def test_registry_reports_channel_close_failure_after_revoking_all_bindings():
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    async def run():
        registry = ContextRegistry()
        bindings = [registry.bind('alice', name + '.ipynb', 'kernel', 'file:///' + name + '.bsl') for name in ('a', 'b')]
        stopped = []
        failure = RuntimeError('channel failure')
        def fail():
            stopped.append('a')
            raise failure
        for index, binding in enumerate(bindings):
            client = registry.client(binding)
            await client.start()
            client.transport = SimpleNamespace(stop_channels=fail if index == 0 else lambda: stopped.append('b'))
        registry.close()
        assert registry.contexts(owner='alice') == [] and stopped == ['a', 'b']
        with pytest.raises(RuntimeError) as error:
            await registry.wait_closed()
        assert error.value is failure
    asyncio.run(run(), debug=True)


def test_unstarted_client_can_close_without_an_event_loop():
    from onec_runtime_jupyter.lsp_kernel_client import KernelProjectClient
    client = KernelProjectClient(None, 'kernel', lambda *args: None)
    completion = client.close()
    assert client.closed and completion.done() and completion.result() is None
    asyncio.run(client.wait_closed())


def test_close_drains_inflight_kernel_connect_before_restart():
    from onec_runtime_jupyter.lsp_kernel_client import KernelProjectClient
    async def run():
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Transport:
            session = SimpleNamespace(session='test')
            stopped = False
            def start_channels(self): pass
            def stop_channels(self): self.stopped = True
            def kernel_info(self): return 'probe'
            async def get_shell_msg(self, **kwargs):
                entered.set()
                try:
                    await release.wait()
                finally:
                    cancelled.set()
        transport = Transport()
        client = KernelProjectClient(SimpleNamespace(get_kernel=lambda _: SimpleNamespace(client=lambda: transport)),
                                     'kernel', lambda *args: None)
        starting = asyncio.create_task(client.start())
        try:
            await entered.wait()
            client.close()
            await client.wait_closed()
            await asyncio.sleep(0)
            assert starting.done() and cancelled.is_set() and transport.stopped
            client.kernel_manager = None
            await client.start()
            assert not client.closed and client.transport is None
        finally:
            release.set()
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
            client.close()
            await client.wait_closed()
    asyncio.run(run(), debug=True)


def test_client_task_timeout_is_bounded_observed_and_remains_a_failure():
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    async def run():
        registry = ContextRegistry()
        binding = registry.bind('alice', 'a.ipynb', 'kernel', 'file:///a.bsl')
        client = registry.client(binding)
        await client.start()
        release = asyncio.Event()
        async def delayed():
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()
                raise RuntimeError('late task failure')
        task = client._task = asyncio.create_task(delayed())
        await asyncio.sleep(0)
        registry.close()
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(registry.wait_closed(), 3)
            assert not task.done() and not registry.contexts(owner='alice')
            release.set()
            await asyncio.sleep(.02)
            assert task.done() and not task._log_traceback
            with pytest.raises(TimeoutError):
                await registry.wait_closed()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run(), debug=True)


def test_server_unload_cancellation_does_not_skip_other_resources():
    pytest.importorskip('jupyter_server')
    from onec_runtime_jupyter.lsp_server_extension import _unload_jupyter_server_extension
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        stages = []
        async def wait():
            entered.set()
            await release.wait()
            stages.append('socket')
        async def registry_done(): pass
        settings = {
            'onec_bsl_binding_reaper': SimpleNamespace(stop=lambda: None),
            'onec_bsl_sockets': [SimpleNamespace(close=lambda: None, on_close=lambda: None, wait_closed=wait)],
            'onec_bsl_context_registry': SimpleNamespace(close=lambda: stages.append('registry'), wait_closed=registry_done),
            'onec_bsl_source_store': SimpleNamespace(close=lambda: stages.append('store'))}
        unloading = asyncio.create_task(_unload_jupyter_server_extension(SimpleNamespace(web_app=SimpleNamespace(settings=settings))))
        await entered.wait()
        unloading.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(unloading, 1)
        assert stages == ['socket', 'registry', 'store']
    asyncio.run(run())


def test_server_unload_socket_close_failure_has_bounded_wait_and_releases_registry():
    pytest.importorskip('jupyter_server')
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    from onec_runtime_jupyter.lsp_server_extension import _unload_jupyter_server_extension
    async def run():
        registry = ContextRegistry()
        registry.bind('alice', 'a.ipynb', None, 'file:///a.bsl')
        socket = handler(registry)
        failure = RuntimeError('socket close failure')
        def close(): raise failure
        socket.close = close
        stored = []
        socket.settings.update(onec_bsl_context_registry=registry,
            onec_bsl_source_store=SimpleNamespace(close=lambda: stored.append('closed')),
            onec_bsl_binding_reaper=SimpleNamespace(stop=lambda: None))
        unload = asyncio.create_task(_unload_jupyter_server_extension(
            SimpleNamespace(web_app=SimpleNamespace(settings=socket.settings))))
        try:
            done, _ = await asyncio.wait([unload], timeout=3)
            assert done, 'failed socket.close must not leave unload waiting forever'
            with pytest.raises(RuntimeError) as error:
                unload.result()
            assert error.value is failure
            assert registry.contexts(owner='alice') == [] and stored == ['closed']
            assert not socket.settings['onec_bsl_sockets']
        finally:
            socket.on_close()
            await asyncio.gather(unload, socket.cleanup_task, return_exceptions=True)
    asyncio.run(run())


class StartupChannels:
    """Kernel boundary with real suspended IOPub tasks and recorded ownership."""
    def __init__(self):
        from threading import get_ident
        self.owner_thread = get_ident()
        self.operations = []
        self.transports = []
        self.live = set()
        self.maximum_live = 0

    def get_kernel(self, kernel_id):
        assert kernel_id == 'kernel'
        return self

    def client(self):
        from threading import get_ident
        boundary = self
        class Transport:
            def __init__(self):
                self.session = SimpleNamespace(session='manager-route', msg=lambda *args: args)
                self.shell_channel = SimpleNamespace(send=self.send)
                self.iopub = asyncio.Event()
                self.shell = asyncio.Queue()
            def send(self, message):
                assert get_ident() == boundary.owner_thread
            def start_channels(self):
                assert get_ident() == boundary.owner_thread
                boundary.operations.append('start')
                boundary.live.add(self)
                boundary.maximum_live = max(boundary.maximum_live, len(boundary.live))
            def stop_channels(self):
                assert get_ident() == boundary.owner_thread
                boundary.operations.append('stop')
                boundary.live.discard(self)
            def kernel_info(self):
                self.shell.put_nowait({'parent_header': {'msg_id': 'probe'}, 'header': {'session': 'kernel-incarnation'}})
                return 'probe'
            async def get_shell_msg(self, **kwargs):
                return await self.shell.get()
            async def get_iopub_msg(self, **kwargs):
                await self.iopub.wait()
        transport = Transport()
        self.transports.append(transport)
        return transport


@pytest.mark.parametrize('revoke', ['registry-close', 'unbind', 'invalidate', 'worker-close'])
def test_early_start_drain_cannot_reopen_after_authority_is_revoked(revoke):
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    from threading import Thread
    async def run():
        boundary = StartupChannels()
        registry = ContextRegistry(kernel_manager=boundary)
        binding = registry.bind('alice', 'a.ipynb', 'kernel', 'file:///a.bsl')
        client = registry.client(binding)
        starting = asyncio.create_task(registry.start(binding))
        await asyncio.sleep(0)  # The actual first close-completion await, before _connect.
        completion = client._close_future
        try:
            if revoke == 'registry-close':
                registry.close()
            elif revoke == 'unbind':
                registry.unbind(binding, owner='alice')
            elif revoke == 'invalidate':
                registry.invalidate_kernel('kernel')
            else:
                worker = Thread(target=client.close)
                worker.start()
                worker.join(timeout=1)
                assert not worker.is_alive()
            assert client._close_future is completion
            await registry.wait_closed()
            await starting
            assert client.closed and client.transport is None
            assert client._task is None and client._monitor_task is None
            assert boundary.operations == [] and not boundary.live
        finally:
            await asyncio.gather(starting, return_exceptions=True)
            registry.close()
            client.close()  # Also recover the orphan on RED after registry removal.
            await client.wait_closed()
            await registry.wait_closed()
            assert not boundary.live
        await asyncio.sleep(0)
        assert asyncio.all_tasks() == {asyncio.current_task()}
    asyncio.run(run(), debug=True)


def test_overlapping_early_starts_allocate_at_most_one_owned_incarnation():
    from onec_runtime_jupyter.lsp_kernel_client import KernelProjectClient
    async def run():
        boundary = StartupChannels()
        client = KernelProjectClient(boundary, 'kernel', lambda *args: None)
        starts = [asyncio.create_task(client.start()) for _ in range(2)]
        try:
            await asyncio.gather(*starts)
            assert boundary.operations == ['start'] and boundary.maximum_live == 1
            assert client.transport is boundary.transports[0]
            client.close()
            completion = client._close_future
            assert client.close() is completion
            await client.wait_closed()
            assert boundary.operations == ['start', 'stop'] and not boundary.live
        finally:
            await asyncio.gather(*starts, return_exceptions=True)
            client.close()
            await client.wait_closed()
            # RED may have overwritten the first transport/tasks. Recover only
            # this test's known resources and consume their actual outcomes.
            for transport in tuple(boundary.live):
                transport.stop_channels()
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert asyncio.all_tasks() == {asyncio.current_task()}
    asyncio.run(run(), debug=True)


def test_repeated_close_revokes_start_during_old_task_drain_but_allows_a_fresh_start():
    from onec_runtime_jupyter.lsp_kernel_client import KernelProjectClient
    async def run():
        boundary = StartupChannels()
        client = KernelProjectClient(boundary, 'kernel', lambda *args: None)
        await client.start()
        drained, release = asyncio.Event(), asyncio.Event()
        async def finishing():
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
                await release.wait()
        monitor = client._monitor_task
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        old = client._monitor_task = asyncio.create_task(finishing())
        await asyncio.sleep(0)
        completion = client.close()
        await drained.wait()
        restarting = asyncio.create_task(client.start())
        await asyncio.sleep(0)
        try:
            assert not completion.done() and client.close() is completion
            assert client.close() is completion
            release.set()
            await restarting
            await client.wait_closed()
            assert old.done() and client.closed and client._close_future is completion
            assert boundary.operations == ['start', 'stop'] and not boundary.live
            await client.start()  # A newly requested start has new authority.
            assert not client.closed and boundary.operations == ['start', 'stop', 'start']
            assert boundary.maximum_live == 1 and len(boundary.live) == 1
        finally:
            release.set()
            await asyncio.gather(restarting, return_exceptions=True)
            client.close()
            await client.wait_closed()
            assert not boundary.live
        assert asyncio.all_tasks() == {asyncio.current_task()}
    asyncio.run(run(), debug=True)


def test_worker_close_is_atomic_with_pending_start_check_and_reopen():
    from threading import Event, Thread
    from onec_runtime_jupyter.lsp_kernel_client import KernelProjectClient
    async def run():
        opening, attempted, closed = Event(), Event(), Event()
        boundary = StartupChannels()
        class PausedReopenClient(KernelProjectClient):
            # An interleaving seam only in this test: stop between the fence
            # check and state reopening, without replacing lifecycle behavior.
            def __setattr__(self, name, value):
                if name == '_loop' and value is not None and getattr(self, 'pause_reopen', False):
                    self.pause_reopen = False
                    opening.set()
                    assert attempted.wait(2), 'close worker did not reach the boundary'
                super().__setattr__(name, value)
        client = PausedReopenClient(boundary, 'kernel', lambda *args: None)
        client.pause_reopen = True
        errors = []
        def worker():
            try:
                assert opening.wait(2), 'start did not reach reopening'
                # Deterministically classify the boundary without a timed race.
                # If reopening is unlocked, close finishes before it resumes.
                acquired = client._lifecycle_lock.acquire(blocking=False)
                if acquired:
                    try:
                        client.close()
                    finally:
                        client._lifecycle_lock.release()
                    attempted.set()
                else:
                    attempted.set()
                    client.close()
            except BaseException as error:
                errors.append(error)
                attempted.set()
            finally:
                closed.set()
        thread = Thread(target=worker)
        thread.start()
        starting = asyncio.create_task(client.start())
        try:
            assert await asyncio.to_thread(closed.wait, 3)
            await starting
            await client.wait_closed()
            assert not errors
            assert client.closed and client.transport is None and not boundary.live
            assert client._task is None and client._monitor_task is None
            assert boundary.maximum_live <= 1
        finally:
            attempted.set()
            await asyncio.gather(starting, return_exceptions=True)
            await asyncio.to_thread(thread.join, 2)
            client.close()
            await client.wait_closed()
            assert not thread.is_alive() and not boundary.live
        assert asyncio.all_tasks() == {asyncio.current_task()}
    asyncio.run(run(), debug=True)
