from importlib import import_module, util
import os

import pytest


def api():
    assert util.find_spec('onec_runtime_jupyter.lsp_control'), 'private control channel missing'
    return import_module('onec_runtime_jupyter.lsp_control')


def test_real_authenticated_local_json_round_trip_is_server_scoped():
    m = api()
    original = dict(os.environ)
    with m.ControlServer(lambda: {'owner': 'alice', 'connection_id': 'server-choice', 'contexts': []}) as server:
        environment = server.child_environment()
        with m.ControlClient.from_environment(environment) as client:
            assert client.request() == {'owner': 'alice', 'connection_id': 'server-choice', 'contexts': []}
            with pytest.raises(ValueError): client.request({'owner': 'bob'})
    assert dict(os.environ) == original


def test_control_overflow_returns_degraded_ack_without_source():
    m = api()
    with m.ControlServer(lambda: {'secret': 'private' * 1000}, max_payload_bytes=512) as server:
        with m.ControlClient.from_environment(server.child_environment()) as client:
            assert client.request() == {'mode': 'virtual-only', 'reason': 'control-payload-limit'}


def test_control_limits_include_ack_envelope():
    m = api()
    with m.ControlServer(lambda: {'value': 'x' * 490}, max_payload_bytes=512) as server:
        with m.ControlClient.from_environment(server.child_environment()) as client:
            assert client.request() == {'mode': 'virtual-only', 'reason': 'control-payload-limit'}


def test_failed_authentication_never_reaches_provider_and_next_client_can_connect():
    from multiprocessing import AuthenticationError
    from multiprocessing.connection import Client
    m = api()
    calls = []
    def provider():
        calls.append(True)
        return {'contexts': []}
    with m.ControlServer(provider) as server:
        with pytest.raises(AuthenticationError):
            Client(server.address, family=server.family, authkey=b'incorrect-test-key')
        assert not calls
        with m.ControlClient.from_environment(server.child_environment()) as client:
            assert client.request() == {'contexts': []}
        assert calls == [True]


def _exercise_control_shutdown(result, mode):
    """Child containment lets RED terminate the old unbounded shutdown safely."""
    import gc
    from multiprocessing.connection import Client
    from threading import enumerate as threads, Thread
    from time import monotonic
    import psutil
    m = api()
    # CPython 3.12 initializes a process-wide thread handle on first use.
    warmup = Thread(target=lambda: None); warmup.start(); warmup.join(); del warmup
    gc.collect()
    baseline_threads = set(threads())
    process = psutil.Process()
    if os.name == 'nt':
        from windows_owned_handles import WindowsOwnedHandles
        counter = WindowsOwnedHandles()
        samples = [counter.sample()]
        baseline_handles = samples[0].counted
    else:
        baseline_handles = process.num_fds()
    cycle_handles = []
    try:
        for _ in range(3):
            server = m.ControlServer(lambda: {'contexts': []})
            peer = None
            try:
                if mode == 'stalled-auth':
                    peer = Client(server.address, family=server.family, authkey=None)
                    assert peer.poll(1), 'server must begin real authentication'
                    peer.recv_bytes(256)  # Deliberately never answer its challenge.
                elif mode == 'authenticated':
                    peer = m.ControlClient.from_environment(server.child_environment())
                    assert peer.request() == {'contexts': []}
                started = monotonic()
                server.close()
                elapsed = monotonic() - started
                assert elapsed < 2, ('shutdown-seconds', elapsed)
                assert not server._thread.is_alive(), 'control thread survived close'
                assert server._connection is None, 'control connection survived close'
            finally:
                if peer is not None: peer.close()
                # Closed synchronization objects themselves own handles on Python 3.12.
                # Measure transport leaks after disposing those objects, on every cycle.
                del peer, server
            gc.collect()
            if os.name == 'nt':
                samples.append(counter.sample())
                current_handles = samples[-1].counted
            else:
                current_handles = process.num_fds()
            cycle_handles.append(current_handles)
        gc.collect()
        assert set(threads()) == baseline_threads, 'threads survived close'
        handles = counter.sample().counted if os.name == 'nt' else process.num_fds()
        assert max(*cycle_handles, handles) <= baseline_handles, ('handles', baseline_handles, cycle_handles, handles)
        if os.name == 'nt':
            print('CONTROL HANDLE ACCOUNTING', {'mode':mode, 'exact_exempt_resource':str(counter.resource_path),
                'samples':[{'total':s.total, 'excluded':s.excluded, 'counted':s.counted} for s in samples]}, flush=True)
        result.put({'ok': True})
    except Exception as error:
        result.put({'ok': False, 'error': type(error).__name__, 'detail': str(error)})


@pytest.mark.parametrize('mode', ['idle', 'stalled-auth', 'authenticated'])
def test_real_local_shutdown_is_bounded_and_releases_threads_and_handles(mode):
    import multiprocessing
    from queue import Empty
    context = multiprocessing.get_context('spawn')
    result = context.Queue()
    process = context.Process(target=_exercise_control_shutdown, args=(result, mode))
    process.start()
    try:
        try:
            outcome = result.get(timeout=6)
        except Empty:
            outcome = {'ok': False, 'error': 'shutdown-did-not-complete'}
        assert outcome == {'ok': True}
    finally:
        process.join(timeout=1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        process.close()
        result.close()
        result.join_thread()
def test_revision_poll_skips_config_encoding_and_status_ack_is_separate():
    from onec_runtime_jupyter.lsp_control import ControlServer, ControlClient
    calls, acks = [], []
    revision = [4]
    def provide():
        calls.append(True)
        return {'owner': 'alice', 'connection_id': 'socket', 'contexts': [{'installation_id': 'private'}]}
    with ControlServer(provide, revision=lambda: revision[0], consumer=acks.append) as server:
        with ControlClient.from_environment(server.child_environment()) as client:
            first = client.request(since_revision=None)
            assert first['revision'] == 4 and len(calls) == 1
            unchanged = client.request(since_revision=4, statuses=[{'binding_id': 'b', 'epoch': 1,
                'state': 'ready', 'reason': None}])
            assert unchanged == {'revision': 4, 'unchanged': True}
            assert len(calls) == 1 and acks[-1][0]['state'] == 'ready'
            revision[0] = 5
            assert client.request(since_revision=4)['revision'] == 5
            assert len(calls) == 2
