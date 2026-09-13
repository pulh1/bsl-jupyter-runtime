import asyncio
from importlib import import_module, util
import json
import sys

import pytest


def test_dedicated_route_preserves_generic_route_auth_origin_and_connection_outputs(monkeypatch):
    assert util.find_spec('onec_runtime_jupyter.lsp_websocket'), 'dedicated websocket missing'
    pytest.importorskip('jupyter_server')
    from jupyter_server.auth import IdentityProvider, User
    from tornado.httpclient import HTTPRequest, HTTPClientError
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application, RequestHandler
    from tornado.websocket import websocket_connect
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    m = import_module('onec_runtime_jupyter.lsp_websocket')
    monkeypatch.setenv('ONEC_BSL_LANGUAGE_SERVER', sys.executable)
    class Identity(IdentityProvider):
        def get_user(self, handler):
            name = handler.request.headers.get('X-Test-User')
            return User(name) if name else None
    class Authorizer:
        def is_authorized(self, handler, user, action, resource): return user.username != 'denied'
    class Generic(RequestHandler):
        def get(self, language): self.finish('upstream-' + language)
    class HTTP(AsyncHTTPTestCase):
        def get_app(self):
            self.registry = ContextRegistry()
            app = Application([(r'/base/lsp/ws/(.*)', Generic)], onec_bsl_context_registry=self.registry,
                identity_provider=Identity(), authorizer=Authorizer(), cookie_secret='test-only',
                base_url='/base/', allow_remote_access=True)
            m.register_websocket(app)
            return app
    case = HTTP(); case.setUp()
    async def check():
        url = case.get_url('/base/lsp/ws/onec-bsl').replace('http:', 'ws:')
        for headers in ({}, {'X-Test-User': 'denied'}, {'X-Test-User': 'alice', 'Origin': 'https://foreign.example'}):
            with pytest.raises(HTTPClientError) as error:
                await websocket_connect(HTTPRequest(url, headers=headers))
            assert error.value.code == 403
        one = await websocket_connect(HTTPRequest(url, headers={'X-Test-User': 'alice'}))
        two = await websocket_connect(HTTPRequest(url, headers={'X-Test-User': 'bob'}))
        for ws in (one, two): ws.write_message(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}))
        first, second = await asyncio.gather(one.read_message(), two.read_message())
        assert json.loads(first)['id'] == json.loads(second)['id'] == 1
        assert 'capabilities' in json.loads(first)['result']
        sessions = list(case._app.settings['onec_bsl_sockets'])
        assert len(sessions) == 2 and sessions[0].process.pid != sessions[1].process.pid
        one.close(); two.close()
        await asyncio.gather(*(session.wait_closed() for session in sessions))
        assert all(session.process.poll() is not None for session in sessions)
        assert not case._app.settings['onec_bsl_sockets']
    try:
        assert case.fetch('/base/lsp/ws/python-lsp').body == b'upstream-python-lsp'
        case.io_loop.run_sync(check, timeout=20)
    finally:
        case.registry.close(); case.tearDown()
def test_real_bsl_socket_late_binding_status_and_two_owner_local_documents(monkeypatch):
    pytest.importorskip('jupyter_server')
    import os
    from pathlib import Path
    from types import SimpleNamespace
    server = os.environ.get('ONEC_BSL_TEST_SERVER')
    if not server: pytest.skip('set ONEC_BSL_TEST_SERVER for real protocol integration')
    from jupyter_server.auth import IdentityProvider, User
    from jupyter_lsp.handlers import add_handlers as add_upstream
    from tornado.httpclient import HTTPRequest
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application
    from tornado.websocket import websocket_connect
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    from onec_runtime_jupyter.lsp_websocket import register_websocket
    monkeypatch.setenv('ONEC_BSL_LANGUAGE_SERVER', str(Path(server).resolve()))
    class Identity(IdentityProvider):
        def get_user(self, handler):
            name = handler.request.headers.get('X-Test-User')
            return User(name) if name else None
    class Authorizer:
        def is_authorized(self, *args): return True
    class Contents:
        def get(self, path, content=False): return {'type': 'notebook'}
    class HTTP(AsyncHTTPTestCase):
        def get_app(self):
            self.registry = ContextRegistry()
            app = Application([], onec_bsl_context_registry=self.registry, identity_provider=Identity(),
                authorizer=Authorizer(), contents_manager=Contents(), cookie_secret='test-only',
                base_url='/base/', allow_remote_access=True)
            # Match the installed upstream route, including its outer HostMatches wrapper.
            add_upstream(SimpleNamespace(web_app=app, base_url='/base/', language_server_manager=object()))
            register_websocket(app)
            return app
    case = HTTP(); case.setUp()
    async def check():
        sockets = []
        async def reply(ws, request_id):
            while True:
                value = json.loads(await ws.read_message())
                if value.get('id') == request_id: return value
        try:
            for user in ('alice', 'bob'):
                ws = await websocket_connect(HTTPRequest(case.get_url('/base/lsp/ws/onec-bsl').replace('http:', 'ws:'), headers={'X-Test-User': user}))
                sockets.append(ws)
                ws.write_message(json.dumps({'id': 1, 'method': 'initialize', 'params': {}}))
                assert 'capabilities' in (await reply(ws, 1))['result']
                # didOpen precedes the authorized association deliberately.
                text = f'Процедура Только{user}()\nКонецПроцедуры\nТолько'
                ws.write_message(json.dumps({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                    'uri': 'file:///shared-uri.bsl', 'languageId': 'bsl', 'version': 1, 'text': text}}}))
            await asyncio.sleep(.3)
            bindings = [case.registry.bind(user, user + '.ipynb', None, 'file:///shared-uri.bsl') for user in ('alice', 'bob')]
            async def wait_ready():
                while any(case.registry.status(binding).analysis_state != 'ready' for binding in bindings):
                    await asyncio.sleep(.1)
            await asyncio.wait_for(wait_ready(), 45)
            for ws in sockets:
                ws.write_message(json.dumps({'id': 2, 'method': 'textDocument/completion', 'params': {
                    'textDocument': {'uri': 'file:///shared-uri.bsl'}, 'position': {'line': 2, 'character': 5}}}))
            results = await asyncio.gather(*(reply(ws, 2) for ws in sockets))
            for user, result in zip(('alice', 'bob'), results):
                assert 'error' not in result, result
                items = result['result']['items'] if isinstance(result['result'], dict) else result['result']
                assert any('Только' + user in i['label'] for i in items)
                other = 'bob' if user == 'alice' else 'alice'
                assert not any('Только' + other in i['label'] for i in items)
        finally:
            handlers = list(case._app.settings['onec_bsl_sockets'])
            for ws in sockets: ws.close()
            await asyncio.gather(*(handler.wait_closed() for handler in handlers))
    try:
        case.io_loop.run_sync(check, timeout=60)
    finally:
        case.registry.close(); case.tearDown()
def test_gateway_shutdown_reaps_children_even_if_gateway_exits_first():
    import subprocess
    import psutil
    assert util.find_spec('onec_runtime_jupyter.lsp_process'), 'launch-time process ownership missing'
    from onec_runtime_jupyter.lsp_process import OwnedGateway
    program = 'import subprocess,sys;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);print(p.pid,flush=True)'
    owned = OwnedGateway([sys._base_executable, '-c', program])
    process = owned.process
    child = psutil.Process(int(process.stdout.readline()))
    foreign = subprocess.Popen([sys._base_executable, '-c', 'import time;time.sleep(60)'])
    receipt = None
    try:
        if sys.platform == 'win32':
            from lsp_native_checks import NativeReceipt
            receipt = NativeReceipt(child.pid)
        process.wait(timeout=5)  # Parent is already dead BEFORE cleanup starts.
        assert child.is_running()
        async def close_and_check():
            await owned.close(timeout=.5)
            if receipt is not None:
                receipt.assert_signaled()  # Before loop teardown or any psutil wait.
        asyncio.run(close_and_check())
        child.wait(timeout=5)  # Wait on the exact child handle; Windows can retain a terminated PID briefly.
        assert not child.is_running()
        assert foreign.poll() is None
    finally:
        if receipt is not None: receipt.close()
        if child.is_running(): child.kill(); child.wait(timeout=5)
        if process.poll() is None: process.kill(); process.wait()
        asyncio.run(owned.close(timeout=.5))
        foreign.terminate(); foreign.wait(timeout=5)
        process.stdout.close()


@pytest.mark.parametrize('abort_outer', [False, True])
def test_nested_child_abort_preserves_outer_gateway_sibling_and_foreign_process(abort_outer):
    """Nested jobs/sessions must reclaim one dead launcher's tree independently."""
    import subprocess
    import psutil
    from onec_runtime_jupyter.lsp_process import OwnedGateway
    program = '''import sys,json,asyncio
sys.path[:0] = json.loads(sys.argv[1])
from onec_runtime_jupyter.lsp_process import OwnedProcess
async def run():
 child = OwnedProcess([sys.executable,'-c','import subprocess,sys;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);print(p.pid,flush=True)'])
 sibling = OwnedProcess([sys.executable,'-c','import time;time.sleep(60)'])
 descendant = int(await asyncio.to_thread(child.process.stdout.readline))
 receipts=[]
 try:
  if sys.platform == 'win32':
   from lsp_native_checks import NativeReceipt
   for pid in (child.process.pid,descendant): receipts.append(NativeReceipt(pid))
  await asyncio.to_thread(child.process.wait,timeout=5)
  print(json.dumps({'descendant':descendant,'sibling':sibling.process.pid,'child_group':child.process.pid}),flush=True)
  await asyncio.to_thread(sys.stdin.buffer.readline)
  await child.close(graceful=False)
  for receipt in receipts: receipt.assert_signaled()
  print(json.dumps({'closed':child.process.stdout.closed,'sibling_alive':sibling.process.poll() is None}),flush=True)
  await asyncio.to_thread(sys.stdin.buffer.read)
 finally:
  try:
   await child.close(graceful=False)
  finally:
   try:
    await sibling.close(graceful=False)
   finally:
    for receipt in receipts: receipt.close()
asyncio.run(run())
'''
    async def run():
        outer = OwnedGateway([sys._base_executable, '-c', program, json.dumps(sys.path)])
        foreign = subprocess.Popen([sys._base_executable, '-c', 'import time;time.sleep(60)'])
        state = None
        receipts = []
        try:
            await outer.wait_ready()
            state = json.loads(await asyncio.wait_for(asyncio.to_thread(outer.process.stdout.readline), 5))
            descendant, sibling = psutil.Process(state['descendant']), psutil.Process(state['sibling'])
            assert descendant.is_running() and sibling.is_running()
            if sys.platform == 'win32':
                from lsp_native_checks import NativeReceipt
                for pid in (outer.process.pid, descendant.pid, sibling.pid):
                    receipts.append(NativeReceipt(pid))
            if abort_outer:
                await outer.close(graceful=False)
                for receipt in receipts: receipt.assert_signaled()
                descendant.wait(timeout=1); sibling.wait(timeout=1)
                assert not descendant.is_running() and not sibling.is_running()
                assert foreign.poll() is None
                return
            outer.process.stdin.write(b'abort\n'); outer.process.stdin.flush()
            result = json.loads(await asyncio.wait_for(asyncio.to_thread(outer.process.stdout.readline), 5))
            assert result == {'closed': True, 'sibling_alive': True}
            descendant.wait(timeout=1)
            assert not descendant.is_running()
            assert outer.process.poll() is None and sibling.is_running() and foreign.poll() is None
            await outer.close(timeout=.5)
            for receipt in receipts: receipt.assert_signaled()
            sibling.wait(timeout=1)
            assert not sibling.is_running() and foreign.poll() is None
        finally:
            try:
                await outer.close(graceful=False)
            finally:
                for receipt in receipts: receipt.close()
            # Exact launch-time groups supplied by this probe, never PID-tree scans.
            # On POSIX this also bounds emergency cleanup if nested ownership fails.
            import os
            import signal
            if os.name != 'nt' and state:
                for process, group in ((descendant, state['child_group']), (sibling, state['sibling'])):
                    try:
                        if process.is_running() and os.getpgid(process.pid) == group:
                            os.killpg(group, signal.SIGKILL)
                    except (psutil.NoSuchProcess, ProcessLookupError): pass
            foreign.terminate(); foreign.wait(timeout=5)
    asyncio.run(run())


def test_owned_process_reports_ready_without_changing_target_pid():
    from onec_runtime_jupyter.lsp_process import OwnedProcess
    async def run():
        owned = OwnedProcess([sys._base_executable, '-c', 'import os,sys;print(os.getpid(),flush=True);sys.stdin.buffer.read()'])
        try:
            await owned.wait_ready()
            pid = int(await asyncio.to_thread(owned.process.stdout.readline))
            assert pid == owned.process.pid
        finally:
            await owned.close(timeout=.5)
    asyncio.run(run())


def test_posix_exec_failure_is_sanitized_and_reclaimed(tmp_path):
    import os
    if os.name == 'nt': pytest.skip('POSIX guardian requires fork/exec')
    from onec_runtime_jupyter.lsp_process import OwnedProcess
    async def run():
        owned = OwnedProcess([str(tmp_path / 'private-missing-executable')])
        try:
            with pytest.raises(ValueError, match='^gateway-ownership-unavailable$'):
                await owned.wait_ready()
            assert owned.process.poll() is not None
            assert owned.process.stdin.closed and owned.process.stdout.closed
        finally:
            await owned.close(graceful=False)
    asyncio.run(run())


def test_posix_guardian_does_not_retain_protocol_pipe_after_launcher_exit():
    import os
    if os.name == 'nt': pytest.skip('POSIX guardian requires fork/exec')
    from onec_runtime_jupyter.lsp_process import OwnedProcess
    async def run():
        owned = OwnedProcess([sys._base_executable, '-c', 'pass'])
        try:
            await owned.wait_ready()
            await asyncio.to_thread(owned.process.wait, timeout=1)
            assert await asyncio.wait_for(asyncio.to_thread(owned.process.stdout.read), 1) == b''
        finally:
            await owned.close(graceful=False)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['stall', 'eof', 'cancel'])
def test_posix_startup_handshake_is_bounded_and_does_not_block_loop(tmp_path, monkeypatch, mode):
    import os
    if os.name == 'nt': pytest.skip('POSIX guardian requires fork/exec')
    import time
    import onec_runtime_jupyter.lsp_process as m
    # Real faulty launcher processes: premature EOF or a never-ending handshake.
    helper = tmp_path / 'faulty-helper.py'
    helper.write_text('import os,sys,time\nos.write(int(sys.argv[2]),b"READY\\n")\ntime.sleep(60)\n' if mode != 'eof' else 'pass\n')
    monkeypatch.setattr(m, 'POSIX_GUARDIAN_PATH', helper)
    monkeypatch.setattr(m, 'DEFAULT_STARTUP_SECONDS', .1)
    async def run():
        owned = m.OwnedProcess([sys._base_executable, '-c', 'pass'])
        ticks = []
        async def ticker():
            while True:
                ticks.append(True); await asyncio.sleep(.005)
        ticker_task = asyncio.create_task(ticker())
        started = time.monotonic()
        try:
            ready = asyncio.create_task(owned.wait_ready())
            if mode == 'cancel':
                await asyncio.sleep(.02); ready.cancel()
            with pytest.raises(asyncio.CancelledError if mode == 'cancel' else ValueError):
                await ready
            assert time.monotonic() - started < .6
            assert ticks and owned.process.poll() is not None
            assert owned.process.stdin.closed and owned.process.stdout.closed
        finally:
            ticker_task.cancel(); await asyncio.gather(ticker_task, return_exceptions=True)
            await owned.close(graceful=False)
    asyncio.run(run())


def test_owned_gateway_releases_handles_across_repeated_launch_and_failed_assignment(monkeypatch, tmp_path):
    import gc
    import os
    import psutil
    assert util.find_spec('onec_runtime_jupyter.lsp_process'), 'launch-time process ownership missing'
    m = import_module('onec_runtime_jupyter.lsp_process')
    if os.name != 'nt': pytest.skip('Windows job handle lifecycle')
    if os.environ.get('ONEC_TEST_OWNED_HANDLE_CHILD') != '1':
        # num_handles is process-wide. Other suite tests leave asynchronous
        # kernel/source-consumer threads finishing independently of this test.
        # Keep the strict zero-growth assertion in an otherwise idle process.
        import subprocess
        from pathlib import Path
        result = subprocess.run([sys.executable, '-m', 'pytest',
            str(Path(__file__).resolve()) + '::test_owned_gateway_releases_handles_across_repeated_launch_and_failed_assignment', '-q'],
            env={**os.environ, 'ONEC_TEST_OWNED_HANDLE_CHILD':'1'},
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return
    async def run():
        await asyncio.to_thread(lambda: None)  # Initialize the executor before strict baseline.
        # Windows stdlib Popen first-use initialization retains two handles, independent
        # of ownership. Establish that neutral baseline, not arbitrary leak slack.
        import subprocess
        neutral = subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        await asyncio.to_thread(neutral.wait, timeout=5)
        neutral.stdin.close(); neutral.stdout.close()
        del neutral
        gc.collect(); process = psutil.Process(); baseline = process.num_handles()
        for _ in range(3):
            owned = m.OwnedGateway([sys.executable, '-c', 'import sys;sys.stdin.buffer.read()'])
            await owned.close(timeout=1)
            assert owned.process.poll() is not None
            del owned; gc.collect()
            assert process.num_handles() <= baseline
        rejected = []
        original = m._WindowsJob.assign_and_resume
        def reject(self, process):
            rejected.append(process)
            raise ValueError('test-assignment-failed')
        monkeypatch.setattr(m._WindowsJob, 'assign_and_resume', reject)
        marker = tmp_path / 'must-not-run'
        command = [sys.executable, '-c', 'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text("ran")', str(marker)]
        with pytest.raises(ValueError, match='gateway-ownership-unavailable'):
            m.OwnedGateway(command)
        assert rejected[0].poll() is not None
        assert not marker.exists()
        rejected.clear()
        gc.collect()
        assert process.num_handles() <= baseline
        def reject_thread(self, child):
            rejected.append(child)
            self.api.GetProcessIdOfThread = lambda handle: 0
            original(self, child)
        monkeypatch.setattr(m._WindowsJob, 'assign_and_resume', reject_thread)
        with pytest.raises(ValueError, match='gateway-ownership-unavailable'):
            m.OwnedGateway(command)
        assert rejected[0].poll() is not None
        assert not marker.exists()
        rejected.clear(); gc.collect()
        assert process.num_handles() <= baseline
    asyncio.run(run())
def test_selection_preserves_current_binding_and_rechecks_authorization_and_kernel_association():
    pytest.importorskip('jupyter_server')
    from types import SimpleNamespace
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    from onec_runtime_jupyter.lsp_websocket import BslWebSocketHandler
    async def run():
        registry = ContextRegistry()
        allowed = [True]; kernel = ['k']
        handler = SimpleNamespace(registry=registry, owner='alice', connection_id='socket',
            current_user=SimpleNamespace(username='alice'), documents={'file:///a.bsl'}, selected=set(),
            selection_lock=asyncio.Lock(), claims={},
            authorizer=SimpleNamespace(is_authorized=lambda *args: allowed[0]),
            contents_manager=SimpleNamespace(get=lambda *args, **kwargs: {'type': 'notebook'}),
            session_manager=SimpleNamespace(list_sessions=lambda: [{'path': 'a.ipynb', 'kernel': {'id': kernel[0]}}]))
        first = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
        try:
            await BslWebSocketHandler._select(handler)
            assert [c['binding_id'] for c in registry.connection_contexts('socket', owner='alice')] == [first]
            duplicate = registry.bind('alice', 'a.ipynb', 'k', 'file:///a.bsl')
            await BslWebSocketHandler._select(handler)
            assert handler.selected == {first}
            registry.unbind(duplicate, owner='alice')
            await BslWebSocketHandler._select(handler)
            assert handler.selected == {first}
            allowed[0] = False
            await BslWebSocketHandler._select(handler)
            assert not handler.selected and registry.connection_contexts('socket', owner='alice') == []
            allowed[0] = True
            await BslWebSocketHandler._select(handler)
            kernel[0] = 'replacement'
            await BslWebSocketHandler._select(handler)
            assert registry.connection_contexts('socket', owner='alice') == []
        finally: registry.close()
    asyncio.run(run())


def test_real_socket_ordinary_settings_coexist_with_http_binding_and_private_claim(monkeypatch):
    pytest.importorskip('jupyter_server')
    from jupyter_server.auth import IdentityProvider, User
    from tornado.httpclient import HTTPRequest
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application
    from tornado.websocket import websocket_connect
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    from onec_runtime_jupyter.lsp_server_extension import ContextHandler
    from onec_runtime_jupyter.lsp_websocket import register_websocket
    monkeypatch.setenv('ONEC_BSL_LANGUAGE_SERVER', sys.executable)
    class Identity(IdentityProvider):
        def get_user(self, handler): return User('alice') if handler.request.headers.get('X-Test-User') == 'alice' else None
    class Authorizer:
        def is_authorized(self, *args): return True
    class Contents:
        def get(self, path, content=False): return {'type': 'notebook', 'path': path}
    class HTTP(AsyncHTTPTestCase):
        def get_app(self):
            self.registry = ContextRegistry()
            app = Application([(r'/onec-bsl/contexts(?:/([a-f0-9]{32}))?', ContextHandler)],
                onec_bsl_context_registry=self.registry, identity_provider=Identity(),
                authorizer=Authorizer(), contents_manager=Contents(), cookie_secret='test-only',
                xsrf_cookies=True, base_url='/', allow_remote_access=True)
            register_websocket(app)
            return app
    case = HTTP(); case.setUp()
    async def run():
        headers = {'X-Test-User': 'alice', 'Cookie': '_xsrf=' + 'a' * 32, 'X-XSRFToken': 'a' * 32,
                   'Content-Type': 'application/json'}
        reply = await case.http_client.fetch(case.get_url('/onec-bsl/contexts'), method='POST', headers=headers,
            body=json.dumps({'notebook_path': 'a.ipynb', 'kernel_id': None, 'document_uri': 'file:///a.bsl'}))
        assert reply.code == 201
        binding = json.loads(reply.body)['binding_id']
        ws = await websocket_connect(HTTPRequest(case.get_url('/lsp/ws/onec-bsl').replace('http:', 'ws:'), headers=headers))
        def send(message): ws.write_message(json.dumps(message))
        async def barrier(request_id):
            # Proxy notifications are handled in order before this response.
            send({'id': request_id, 'method': 'initialize', 'params': {}})
            response = json.loads(await asyncio.wait_for(ws.read_message(), 5))
            assert response.get('id') == request_id, response
            assert 'capabilities' in response['result']
        try:
            await barrier(1)
            handler, = case._app.settings['onec_bsl_sockets']
            send({'method': 'workspace/didChangeConfiguration', 'params': {'settings': {}}})
            await barrier(2)
            assert not handler.selected and not handler.claims
            send({'method': 'workspace/didChangeConfiguration', 'params': {'settings': {
                'onecProjectBinding': {'document_uri': 'file:///a.bsl', 'binding_id': binding}}}})
            send({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                'uri': 'file:///a.bsl', 'languageId': 'bsl', 'version': 1, 'text': 'Сообщить(1);'}}})
            send({'method': 'workspace/didChangeConfiguration', 'params': {'settings': {
                'source_root': 'file:///untrusted', 'diagnostics': {'enabled': False}}}})
            await barrier(3)
            assert handler.selected == {binding}
            assert handler.claims == {'file:///a.bsl': (binding, None)}
            selected, = case.registry.connection_contexts(handler.connection_id, owner='alice')
            assert selected['binding_id'] == binding and selected['source_root'] is None
            send({'method': 'workspace/didChangeConfiguration', 'params': {'settings': {'onecProjectBinding': {}}}})
            assert await asyncio.wait_for(ws.read_message(), 5) is None
            assert ws.close_code == 1008
        finally:
            handlers = list(case._app.settings['onec_bsl_sockets'])
            ws.close()
            await asyncio.gather(*(handler.wait_closed() for handler in handlers))
    try: case.io_loop.run_sync(run, timeout=30)
    finally:
        case.registry.close(); case.tearDown()


@pytest.mark.parametrize('ordering', ['post-before-detach', 'socket-before-post', 'concurrent-tabs', 'different-notebooks'])
def test_explicit_claim_selects_frontend_binding_without_stealing(ordering):
    pytest.importorskip('jupyter_server')
    from types import SimpleNamespace, MethodType
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    from onec_runtime_jupyter.lsp_websocket import BslWebSocketHandler
    async def run():
        registry = ContextRegistry()
        def handler(name):
            result = SimpleNamespace(registry=registry, owner='alice', connection_id=name,
                current_user=SimpleNamespace(username='alice'), documents={'file:///a.bsl'}, selected=set(),
                selection_lock=asyncio.Lock(), claims={},
                authorizer=SimpleNamespace(is_authorized=lambda *args: True),
                contents_manager=SimpleNamespace(get=lambda *args, **kwargs: {'type':'notebook'}))
            result._select = MethodType(BslWebSocketHandler._select, result)
            return result
        old, new = handler('old'), handler('new')
        first = registry.bind('alice', 'a.ipynb', None, 'file:///a.bsl')
        if ordering != 'concurrent-tabs':
            await old._select()
        if ordering == 'socket-before-post':
            registry.detach('old', owner='alice')
            await new._select()
            assert new.selected == {first}
        second = registry.bind('alice', 'b.ipynb' if ordering == 'different-notebooks' else 'a.ipynb', None, 'file:///a.bsl')
        try:
            assert first != second
            assert hasattr(BslWebSocketHandler, '_claim'), 'explicit binding claim handler missing'
            await BslWebSocketHandler._claim(new, {'document_uri':'file:///a.bsl', 'binding_id':second})
            assert new.selected == {second}
            assert [c['binding_id'] for c in registry.connection_contexts('new', owner='alice')] == [second]
            if ordering != 'socket-before-post':
                await BslWebSocketHandler._claim(old, {'document_uri':'file:///a.bsl', 'binding_id':first})
                assert old.selected == {first}
                with pytest.raises((KeyError, ValueError)):
                    await BslWebSocketHandler._claim(new, {'document_uri':'file:///a.bsl', 'binding_id':first})
                assert new.selected == {second}
            registry.detach('old', owner='alice')
            await new._select()
            assert new.selected == {second}
        finally: registry.close()
    asyncio.run(run())


def test_pending_claim_is_revalidated_and_expiration_never_renews_an_orphan():
    pytest.importorskip('jupyter_server')
    from types import SimpleNamespace, MethodType
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    from onec_runtime_jupyter.lsp_websocket import BslWebSocketHandler
    async def run():
        now = [0.]
        registry = ContextRegistry(lease_seconds=5, clock=lambda:now[0])
        allowed = [True]
        handler = SimpleNamespace(registry=registry, owner='alice', connection_id='s', documents=set(),
            current_user=SimpleNamespace(username='alice'), selected=set(), selection_lock=asyncio.Lock(), claims={},
            authorizer=SimpleNamespace(is_authorized=lambda *args:allowed[0]),
            contents_manager=SimpleNamespace(get=lambda *args, **kwargs:{'type':'notebook'}))
        handler._select = MethodType(BslWebSocketHandler._select, handler)
        binding = registry.bind('alice', 'a.ipynb', None, 'file:///a.bsl')
        try:
            assert hasattr(BslWebSocketHandler, '_claim'), 'explicit binding claim handler missing'
            for invalid in ({'document_uri':'file:///a.bsl','binding_id':binding,'source_root':'secret'},
                            {'document_uri':'file:///other.bsl','binding_id':binding},
                            {'document_uri':'file:///a.bsl','binding_id':1}):
                with pytest.raises((ValueError, KeyError)): await BslWebSocketHandler._claim(handler, invalid)
            await BslWebSocketHandler._claim(handler, {'document_uri':'file:///a.bsl','binding_id':binding})
            assert not handler.selected
            allowed[0] = False; handler.documents.add('file:///a.bsl')
            await handler._select(); assert not handler.selected
            now[0] = 5
            with pytest.raises(KeyError): registry.status(binding, owner='alice')
            allowed[0] = True
            with pytest.raises(KeyError):
                await BslWebSocketHandler._claim(handler, {'document_uri':'file:///a.bsl','binding_id':binding})
            assert not handler.selected
        finally: registry.close()
    asyncio.run(run())


def test_claim_namespace_is_consumed_and_only_valid_document_changes_renew_browser_lease():
    pytest.importorskip('jupyter_server')
    from io import BytesIO
    from types import SimpleNamespace, MethodType
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    from onec_runtime_jupyter.lsp_websocket import BslWebSocketHandler
    async def run():
        now = [0.]
        registry = ContextRegistry(lease_seconds=5, clock=lambda:now[0])
        closed = []
        pipe = BytesIO()
        handler = SimpleNamespace(registry=registry, owner='alice', connection_id='s', documents=set(),
            current_user=SimpleNamespace(username='alice'), selected=set(), selection_lock=asyncio.Lock(), claims={},
            authorizer=SimpleNamespace(is_authorized=lambda *args:True),
            contents_manager=SimpleNamespace(get=lambda *args, **kwargs:{'type':'notebook'}),
            process=SimpleNamespace(stdin=pipe), writer=asyncio.Lock(), close=lambda *args:closed.append(args))
        for method in ('_select', '_claim', 'on_message'):
            setattr(handler, method, MethodType(getattr(BslWebSocketHandler, method), handler))
        binding = registry.bind('alice', 'a.ipynb', None, 'file:///a.bsl')
        try:
            claim = {'method':'workspace/didChangeConfiguration', 'params':{'settings':{'onecProjectBinding':{
                'document_uri':'file:///a.bsl', 'binding_id':binding}}}}
            await handler.on_message(json.dumps(claim))
            assert pipe.getvalue() == b'' and not closed
            now[0] = 4
            await handler.on_message(json.dumps({'method':'textDocument/didOpen', 'params':{'textDocument':{
                'uri':'file:///a.bsl','languageId':'bsl','version':1,'text':'Сообщить(1);'}}}))
            assert registry.connection_contexts('s', owner='alice')[0]['binding_id'] == binding
            now[0] = 8
            await handler.on_message(json.dumps({'method':'textDocument/didChange', 'params':{
                'textDocument':{'uri':'file:///a.bsl','version':'invalid'}, 'contentChanges':[{'text':'x'}]}}))
            now[0] = 9
            with pytest.raises(KeyError): registry.status(binding, owner='alice')
        finally: registry.close()
    asyncio.run(run())


@pytest.mark.parametrize('ordering', ['post-before-detach', 'socket-before-post', 'concurrent-tabs', 'different-notebooks'])
def test_real_http_and_websocket_claims_agree_with_returned_frontend_binding(monkeypatch, ordering):
    pytest.importorskip('jupyter_server')
    from jupyter_server.auth import IdentityProvider, User
    from tornado.httpclient import HTTPRequest
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application
    from tornado.websocket import websocket_connect
    from onec_runtime_jupyter.lsp_contexts import ContextRegistry
    from onec_runtime_jupyter.lsp_server_extension import ContextHandler
    from onec_runtime_jupyter.lsp_websocket import register_websocket
    monkeypatch.setenv('ONEC_BSL_LANGUAGE_SERVER', sys.executable)
    class Identity(IdentityProvider):
        def get_user(self, handler): return User('alice') if handler.request.headers.get('X-Test-User') == 'alice' else None
    class Authorizer:
        def is_authorized(self, *args): return True
    class Contents:
        def get(self, path, content=False): return {'type':'notebook', 'path':path}
    class HTTP(AsyncHTTPTestCase):
        def get_app(self):
            self.registry = ContextRegistry()
            app = Application([(r'/onec-bsl/contexts(?:/([a-f0-9]{32}))?', ContextHandler)],
                onec_bsl_context_registry=self.registry, identity_provider=Identity(),
                authorizer=Authorizer(), contents_manager=Contents(), cookie_secret='test-only',
                xsrf_cookies=True, base_url='/', allow_remote_access=True)
            register_websocket(app)
            return app
    case = HTTP(); case.setUp()
    async def run():
        headers = {'X-Test-User':'alice', 'Cookie':'_xsrf=' + 'a' * 32, 'X-XSRFToken':'a' * 32,
                   'Content-Type':'application/json'}
        sockets = []
        async def post(path='a.ipynb'):
            reply = await case.http_client.fetch(case.get_url('/onec-bsl/contexts'), method='POST', headers=headers,
                body=json.dumps({'notebook_path':path, 'kernel_id':None, 'document_uri':'file:///a.bsl'}))
            assert reply.code == 201
            return json.loads(reply.body)['binding_id']
        async def connect():
            prior = set(case._app.settings['onec_bsl_sockets'])
            ws = await websocket_connect(HTTPRequest(case.get_url('/lsp/ws/onec-bsl').replace('http:', 'ws:'), headers=headers))
            sockets.append(ws)
            ws.write_message(json.dumps({'id':1, 'method':'initialize', 'params':{}}))
            assert 'capabilities' in json.loads(await ws.read_message())['result']
            handler, = set(case._app.settings['onec_bsl_sockets']) - prior
            return ws, handler
        def did_open(ws):
            ws.write_message(json.dumps({'method':'textDocument/didOpen','params':{'textDocument':{
                'uri':'file:///a.bsl', 'languageId':'bsl', 'version':1, 'text':'Сообщить(1);'}}}))
        def claim(ws, binding):
            ws.write_message(json.dumps({'method':'workspace/didChangeConfiguration','params':{
                'settings':{'onecProjectBinding':{'document_uri':'file:///a.bsl','binding_id':binding}}}}))
        async def selected(handler, binding):
            async def wait():
                while handler.selected != {binding}: await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), 5)
            assert case.registry.connection_contexts(handler.connection_id, owner='alice')[0]['binding_id'] == binding
        try:
            first = await post()
            if ordering == 'concurrent-tabs':
                second = await post()
            old, old_handler = await connect()
            claim(old, first); did_open(old)
            await selected(old_handler, first)
            if ordering == 'socket-before-post':
                old.close(); await old_handler.wait_closed()
                new, new_handler = await connect()
                did_open(new); await selected(new_handler, first)
                second = await post()
            else:
                if ordering != 'concurrent-tabs': second = await post('b.ipynb' if ordering == 'different-notebooks' else 'a.ipynb')
                new, new_handler = await connect()
            assert second != first
            claim(new, second); did_open(new)
            await selected(new_handler, second)
            if ordering != 'socket-before-post':
                await selected(old_handler, first)
                old.close(); await old_handler.wait_closed()
                await selected(new_handler, second)
            response = await case.http_client.fetch(case.get_url('/onec-bsl/contexts/' + second), headers=headers)
            assert json.loads(response.body)['binding_id'] == second
        finally:
            handlers = list(case._app.settings['onec_bsl_sockets'])
            for ws in sockets: ws.close()
            await asyncio.gather(*(handler.wait_closed() for handler in handlers))
    try: case.io_loop.run_sync(run, timeout=30)
    finally:
        case.registry.close(); case.tearDown()
