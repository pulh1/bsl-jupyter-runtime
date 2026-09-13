import asyncio
from copy import deepcopy
from hashlib import sha256
from importlib import import_module, util
from pathlib import Path

import pytest

from test_jupyter_lsp_gateway import context


def api():
    assert util.find_spec('onec_runtime_jupyter.lsp_child'), 'isolated child missing'
    return import_module('onec_runtime_jupyter.lsp_child')


def project_context(root, epoch=1):
    return {**context(epoch=epoch), 'source_root': str(root)}


class Transport:
    def __init__(self): self.events = []; self.barrier_result = []; self.closed = False
    async def start(self, root): self.root = root
    async def notify(self, method, params): self.events.append((method, deepcopy(params)))
    async def request(self, method, params):
        self.events.append((method, deepcopy(params)))
        if method == 'initialize': return {'capabilities': {}}
        return self.barrier_result
    async def close(self): self.closed = True


@pytest.mark.parametrize('observe', [False, True])
def test_progress_capability_is_opt_in_and_metadata_uses_owned_reader(tmp_path, observe):
    async def run():
        import sys
        m = api()
        program = '''import sys,json
def read():
    n=int(sys.stdin.buffer.readline().split(b":")[1]);sys.stdin.buffer.readline()
    return json.loads(sys.stdin.buffer.read(n))
def write(m):
    p=json.dumps(m).encode();sys.stdout.buffer.write(b"Content-Length: "+str(len(p)).encode()+b"\\r\\n\\r\\n"+p);sys.stdout.buffer.flush()
request=read()
assert request['params']['capabilities'].get('window',{}).get('workDoneProgress',False) == (sys.argv[1]=='True')
write({'method':'window/workDoneProgress/create','id':50,'params':{'token':'owned'}})
assert read()['id']==50
write({'method':'$/progress','params':{'token':'owned','value':{'kind':'begin','title':'Populating context...'}}})
write({'method':'window/logMessage','params':{'message':'private source'}})
write({'method':'$/progress','params':{'token':'x'*161,'value':{'kind':'end','message':'private'*100}}})
write({'id':request['id'],'result':{'capabilities':{},'serverInfo':{'name':'BSL Language Server','version':'1.0.7'}}})
sys.stdin.buffer.read()
'''
        events = []
        child = m.ChildSession(context(), lambda _: None,
            command=[sys.executable, '-c', program, str(observe)],
            on_progress=(lambda method, params: events.append((method, params))) if observe else None)
        try:
            await child.synchronize(context())
            if observe:
                assert events[:3] == [
                    ('window/workDoneProgress/create', {'token': 'owned'}),
                    ('$/progress', {'token': 'owned', 'value': {'kind': 'begin', 'title': 'Populating context...'}}),
                    ('$/progress', {'invalid': True})]
                assert not any('private' in str(event) for event in events)
                assert events[3:] == [('initialize-result', {'serverInfo': {
                    'name': 'BSL Language Server', 'version': '1.0.7'}})]
            else:
                assert events == []
        finally:
            await child.close()
        assert child.transport.process.poll() is not None
        assert child.transport.reader.done()
        assert not child.transport.pending
    asyncio.run(run())




def test_workspace_check_rejects_linked_descendants_and_bounds_scan(tmp_path):
    m = api()
    (tmp_path / 'CommonModules').mkdir()
    (tmp_path / 'CommonModules/file.bsl').write_text('')
    assert m.check_workspace(tmp_path, max_entries=1) == 'workspace-safety-limit'
    outside = tmp_path.parent / (tmp_path.name + '-outside.bsl'); outside.write_text('secret')
    link = tmp_path / 'CommonModules/escape.bsl'
    try: link.symlink_to(outside)
    except OSError: pytest.skip('symlink privilege unavailable')
    assert m.check_workspace(tmp_path) == 'workspace-unsafe'


def test_rootless_initialize_and_private_child_environment_are_closed(tmp_path, monkeypatch):
    m = api()
    import os
    if os.name == 'nt':
        monkeypatch.setenv('PROGRAMFILES', r'C:\Program Files')
        monkeypatch.setenv('PROGRAMFILES(X86)', r'C:\Program Files (x86)')
        monkeypatch.setenv('PROGRAMW6432', r'C:\Program Files')
    monkeypatch.setenv('ONEC_BSL_CONTROL_KEY', 'secret')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'source-private')
    monkeypatch.setenv('JAVA_TOOL_OPTIONS', '-Duser.home=project')
    env = m.child_environment(tmp_path)
    assert not any(k in env for k in ('ONEC_BSL_CONTROL_KEY', 'AWS_SECRET_ACCESS_KEY', 'JAVA_TOOL_OPTIONS'))
    if os.name == 'nt':
        assert {key: env[key] for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432')} == {
            'PROGRAMFILES': r'C:\Program Files',
            'PROGRAMFILES(X86)': r'C:\Program Files (x86)',
            'PROGRAMW6432': r'C:\Program Files',
        }
    mapper = m.WorkspaceMapper(None, 'file:///a.bsl', tmp_path)
    params = mapper.initialize_params()
    assert params['rootUri'] is None and params['rootPath'] is None and params['workspaceFolders'] == []
    text = 'Сообщить("file:///a.bsl");'
    mapped = mapper.to_server({'params': {'textDocument': {'uri': 'file:///a.bsl', 'text': text}}})
    assert mapped['params']['textDocument']['text'] == text
    assert list(tmp_path.iterdir()) == []



def test_unsafe_workspace_falls_back_to_rootless_without_project_initialization(tmp_path):
    async def run():
        import subprocess
        import os
        m = api()
        outside = tmp_path / 'outside'; outside.mkdir()
        (outside / 'Module.bsl').write_text('escaping module source')
        project = tmp_path / 'project'; project.mkdir()
        (project / 'CommonModules').mkdir()
        link = project / 'CommonModules/Escape'
        if os.name == 'nt':
            # A directory junction needs no symlink privilege and must never be indexed.
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            assert result.returncode == 0
        else: link.symlink_to(outside, target_is_directory=True)
        try:
            assert m.check_workspace(project) == 'workspace-unsafe'
            assert m.check_workspace(link) == 'workspace-unsafe'
            t = Transport(); child = m.ChildSession(project_context(project), lambda _: None, transport=t)
            await child.synchronize(project_context(project))
            assert child.degraded_reason == 'workspace-unsafe'
            init = next(p for method, p in t.events if method == 'initialize')
            assert init['rootUri'] is None and init['workspaceFolders'] == []
            await child.close()
        finally:
            if os.name == 'nt': link.rmdir()
            else: link.unlink()
    asyncio.run(run())


def test_process_server_requests_are_answered_only_on_originating_transport(tmp_path):
    async def run():
        import sys
        m = api()
        program = '''import sys,json,os
assert not os.listdir(os.getcwd()), "child cwd must be empty"
def read():
    n=int(sys.stdin.buffer.readline().split(b":")[1]);sys.stdin.buffer.readline()
    return json.loads(sys.stdin.buffer.read(n))
def write(m):
    p=json.dumps(m).encode();sys.stdout.buffer.write(b"Content-Length: "+str(len(p)).encode()+b"\\r\\n\\r\\n"+p);sys.stdout.buffer.flush()
request=read()
write({"id":1,"method":"workspace/configuration","params":{"items":[{}]}})
reply=read()
assert reply["id"]==1 and reply["result"][0]["sendErrors"]=="never"
write({"id":1,"method":"workspace/applyEdit","params":{"edit":{"changes":{}}}})
reply=read();assert reply["id"]==1 and reply["error"]["code"]==-32601
write({"id":request["id"],"result":{"label":sys.argv[1]}})
sys.stdin.buffer.read()
'''
        transports = []
        for name in ('one', 'two'):
            directory = tmp_path / name; directory.mkdir()
            t = m.ProcessTransport([sys.executable, '-c', program, name], directory)
            transports.append(t); await t.start(None)
        try:
            assert await asyncio.gather(*(t.request('initialize', {}) for t in transports)) == [{'label': 'one'}, {'label': 'two'}]
        finally:
            await asyncio.gather(*(t.close() for t in transports))
    asyncio.run(run())
def test_diagnostic_result_is_discarded_after_notebook_version_changes():
    async def run():
        m = api(); output = []; gate = asyncio.Event(); entered = asyncio.Event()
        class Delayed(Transport):
            async def request(self, method, params):
                if method == 'textDocument/diagnostic':
                    entered.set()
                    try: await gate.wait()
                    except asyncio.CancelledError: await gate.wait()
                    return {'kind': 'full', 'items': [{'message': 'old-version', 'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 1}}}]}
                return await super().request(method, params)
        c = context(); child = m.ChildSession(c, output.append, transport=Delayed())
        await child.synchronize(c)
        await child.notify('textDocument/didOpen', {'textDocument': {'uri': c['document_uri'], 'version': 1, 'languageId': 'bsl', 'text': 'one'}})
        await entered.wait()
        # Fence changes immediately even if cancellation cannot stop the peer response.
        child.context = {**c, 'epoch': 2}
        gate.set()
        await child.diagnostics[c['document_uri']]
        assert output == []
        await child.close()
    asyncio.run(run())



def test_child_exit_reports_unavailable_without_forwarding_child_logs(tmp_path):
    async def run():
        import sys
        m = api(); statuses = []; messages = []
        program = '''import sys,json,time
n=int(sys.stdin.buffer.readline().split(b":")[1]);sys.stdin.buffer.readline();request=json.loads(sys.stdin.buffer.read(n))
def write(m):
 p=json.dumps(m).encode();sys.stdout.buffer.write(b"Content-Length: "+str(len(p)).encode()+b"\\r\\n\\r\\n"+p);sys.stdout.buffer.flush()
write({"id":request["id"],"result":{"capabilities":{}}})
write({"method":"window/logMessage","params":{"message":"private source must not escape"}})
time.sleep(.2)
'''
        c = context()
        child = m.ChildSession(c, messages.append, command=[sys.executable, '-c', program],
            status=lambda context, state, reason: statuses.append((state, reason)))
        try:
            await child.synchronize(c)
            await asyncio.wait_for(child.transport.reader, 5)
            assert statuses == [('unavailable', 'child-unavailable')]
            assert 'private source' not in str(messages)
        finally: await child.close()
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['request', 'cancel', 'close', 'queued', 'pending', 'reader-failure', 'reader-close'])
def test_owned_child_pipe_deadline_reclaims_descendants_and_preserves_sibling(tmp_path, operation):
    """A dead launcher or nonreading peer must not retain pipes past its deadline."""
    import subprocess
    import sys
    import time
    import psutil

    async def run():
        m = api()
        pidfile = tmp_path / 'descendant.pid'
        program = '''import subprocess,sys,time
p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
open(sys.argv[1],'w').write(str(p.pid))
if sys.argv[2].startswith('reader-'):
 time.sleep(.15);sys.stdout.buffer.write(b'Content-Length: invalid\\r\\n\\r\\n');sys.stdout.buffer.flush()
if sys.argv[2] != 'close': time.sleep(60)
'''
        directory = tmp_path / 'child'; directory.mkdir()
        transport = m.ProcessTransport([sys._base_executable, '-c', program, str(pidfile), operation], directory, timeout=.05)
        if operation.startswith('reader-'):
            transport.timeout = 5  # Reader failure must abort a write before its own deadline.
        failures = []
        async def failed():
            failures.append(True)
            if operation == 'reader-close': await transport.close()
        transport.on_failure = failed
        sibling_dir = tmp_path / 'sibling'; sibling_dir.mkdir()
        sibling = m.ProcessTransport([sys._base_executable, '-c', 'import time; time.sleep(60)'], sibling_dir)
        foreign = subprocess.Popen([sys._base_executable, '-c', 'import time; time.sleep(60)'])
        await transport.start(None); await sibling.start(None)
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        descendant = psutil.Process(int(pidfile.read_text()))
        receipt = None
        if sys.platform == 'win32':
            from lsp_native_checks import NativeReceipt
            receipt = NativeReceipt(descendant.pid)
        # Bounded test rescue retains exact process objects created by this probe.
        async def rescue():
            await asyncio.sleep(.8)
            if descendant.is_running(): descendant.kill()
            if transport.process.poll() is None: transport.process.kill()
        rescuer = asyncio.create_task(rescue())
        try:
            if operation == 'close':
                await asyncio.to_thread(transport.process.wait, timeout=5)
            started = time.monotonic()
            if operation == 'request':
                with pytest.raises((TimeoutError, ValueError, OSError)):
                    await transport.request('blocked', {'payload': 'x' * 1024 * 1024})
            elif operation == 'cancel':
                pending = asyncio.create_task(transport.request('blocked', {'payload': 'x' * 1024 * 1024}))
                await asyncio.sleep(.02); pending.cancel()
                with pytest.raises(asyncio.CancelledError): await pending
            elif operation in ('queued', 'pending', 'reader-failure', 'reader-close'):
                first = asyncio.create_task(transport.request('first', {} if operation == 'pending' else {'text': 'x' * 1_000_000}))
                await asyncio.sleep(.01)
                second = asyncio.create_task(transport.request('second', {'text': 'x' * 1_000_000} if operation == 'pending' else {}))
                results = await asyncio.gather(first, second, return_exceptions=True)
                assert all(isinstance(result, (TimeoutError, ValueError, OSError)) for result in results)
            await transport.close()
            assert time.monotonic() - started < .6, 'pipe operation exceeded deadline until test rescue'
            if receipt:
                receipt.assert_signaled()  # No post-close wait grants extra termination time.
            else:
                descendant.wait(timeout=.2)
            assert transport.reader.done() and not transport.pending
            assert not transport.io_tasks
            assert transport.process.stdin.closed and transport.process.stdout.closed
            assert failures == ([] if operation == 'close' else [True])
            assert sibling.process.poll() is None and foreign.poll() is None
        finally:
            await rescuer
            await transport.close(); await sibling.close()
            foreign.terminate(); foreign.wait(timeout=5)
            if receipt: receipt.close()
    asyncio.run(run())


@pytest.mark.parametrize('abort', [False, True])
def test_owned_process_graceful_deadline_includes_blocked_buffered_close(tmp_path, abort):
    """A pipe lock cannot prevent the grace deadline or a concurrent abort."""
    import sys
    import time
    from onec_runtime_jupyter.lsp_process import OwnedProcess
    from onec_runtime_jupyter.lsp_proxy import write_message

    async def run():
        owned = OwnedProcess([sys._base_executable, '-c', 'import time;time.sleep(60)'])
        writer = asyncio.create_task(asyncio.to_thread(write_message, owned.process.stdin, {'text': 'x' * 1_000_000}))
        await asyncio.sleep(.05)
        assert not writer.done(), 'real OS pipe must be blocked before closing'
        rescued = []
        async def rescue():
            await asyncio.sleep(.8)
            if owned.process.poll() is None:
                rescued.append(True)
                owned.process.kill()
        rescuer = asyncio.create_task(rescue())
        started = time.monotonic()
        closing = asyncio.create_task(owned.close(timeout=5 if abort else .05))
        try:
            if abort:
                await asyncio.sleep(.02)
                await owned.close(graceful=False)
            await closing
            assert time.monotonic() - started < .6, 'graceful pipe lock waited until test rescue'
            assert not rescued
            assert owned.process.poll() is not None
            result = await asyncio.gather(writer, return_exceptions=True)
            assert isinstance(result[0], OSError) and not writer.cancelled()
            assert owned.process.stdin.closed and owned.process.stdout.closed
        finally:
            await rescuer
            await asyncio.gather(closing, writer, return_exceptions=True)
            await owned.close(graceful=False)
    asyncio.run(run())


def test_transport_shutdown_releases_real_pipe_threads_and_windows_handles(tmp_path):
    import gc
    import os
    import subprocess
    import sys
    import threading
    if os.environ.get('ONEC_TEST_TRANSPORT_HANDLE_CHILD') != '1':
        result = subprocess.run([sys.executable, '-m', 'pytest',
            str(Path(__file__).resolve()) + '::test_transport_shutdown_releases_real_pipe_threads_and_windows_handles', '-q', '-s'],
            env={**os.environ, 'ONEC_TEST_TRANSPORT_HANDLE_CHILD': '1'},
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return
    if os.name == 'nt':
        from windows_owned_handles import WindowsOwnedHandles
        counter = WindowsOwnedHandles()
    else:
        import psutil
        counter = None
    m = api()  # Import dependencies before measuring transport resource changes.
    # Prime only stdlib Popen/asyncio initialization before the exact baseline.
    neutral = subprocess.Popen([sys._base_executable, '-c', 'pass'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    neutral.wait(timeout=5); neutral.stdin.close(); neutral.stdout.close()
    del neutral
    async def prime(): await asyncio.to_thread(lambda: None)
    asyncio.run(prime()); gc.collect()
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    baseline = counter.sample().counted if counter else psutil.Process().num_fds()
    async def run(directory):
        transport = m.ProcessTransport([sys._base_executable, '-c', 'import time;time.sleep(60)'], directory, timeout=.05)
        await transport.start(None)
        try:
            with pytest.raises((TimeoutError, ValueError, OSError)):
                await transport.request('blocked', {'text': 'x' * 1_000_000})
        finally:
            await transport.close()
        assert not transport.pending and not transport.io_tasks and transport.reader.done()
        assert transport.process.poll() is not None
    for index in range(3):
        directory = tmp_path / str(index); directory.mkdir()
        asyncio.run(run(directory)); gc.collect()
        current = counter.sample().counted if counter else psutil.Process().num_fds()
        assert current <= baseline
        assert {thread.ident for thread in threading.enumerate()} == baseline_threads
        print(f'cycle={index + 1} handles/fds={current} baseline={baseline} extra_threads=0')


def test_close_bounds_reader_failure_callback_after_real_pipe_failure(tmp_path):
    """A blocked failure consumer cannot make joining the reader unbounded."""
    import sys
    import time
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def failure():
            entered.set()
            await release.wait()
        transport = api().ProcessTransport([sys._base_executable, '-c',
            'import sys;sys.stdout.buffer.write(b"Content-Length: invalid\\r\\n\\r\\n");sys.stdout.buffer.flush()'],
            tmp_path, timeout=.05, on_failure=failure)
        await transport.start(None)
        await asyncio.wait_for(entered.wait(), 5)
        rescued = []
        async def rescue():
            await asyncio.sleep(.8)
            rescued.append(True); release.set()
        rescuer = asyncio.create_task(rescue())
        try:
            started = time.monotonic()
            await transport.close()
            assert time.monotonic() - started < .6, 'reader join waited for test rescue'
            assert not rescued and transport.reader.done()
            assert not transport.io_tasks and not transport.pending
            assert transport.process.poll() is not None
        finally:
            release.set()
            await rescuer
            await transport.close()
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['reply-timeout', 'write-timeout', 'write-cancel', 'write-error', 'close', 'close-writing'])
@pytest.mark.parametrize('callback_closes', [False, True])
def test_process_abort_notifies_failure_once_and_intentional_close_does_not(tmp_path, operation, callback_closes):
    """Real pipe failures publish unavailable even if the callback closes itself."""
    import sys
    import time
    async def run():
        ready = tmp_path / 'peer-ready'
        program = '''import os,sys,time
if sys.argv[2] == 'write-error': os.close(0)
open(sys.argv[1],'w').write('ready')
time.sleep(60)
'''
        failures = []
        async def failure():
            failures.append('unavailable')
            if callback_closes: await transport.close()
        transport = api().ProcessTransport([sys._base_executable, '-c', program, str(ready), operation],
            tmp_path, timeout=5 if operation in ('close-writing', 'write-cancel') else .05, on_failure=failure)
        await transport.start(None)
        request = None
        try:
            async def peer_ready():
                while not ready.exists(): await asyncio.sleep(.005)
            await asyncio.wait_for(peer_ready(), 5)
            started = time.monotonic()
            if operation == 'close':
                await transport.close()
            elif operation in ('close-writing', 'write-cancel'):
                request = asyncio.create_task(transport.request('blocked', {'text': 'x' * 1_000_000}))
                await asyncio.sleep(.02)
                assert transport.writer.locked() and not request.done()
                if operation == 'write-cancel':
                    request.cancel()
                    with pytest.raises(asyncio.CancelledError): await request
                else:
                    await transport.close()
                    result = await asyncio.gather(request, return_exceptions=True)
                    assert isinstance(result[0], (OSError, ValueError))
            else:
                with pytest.raises((TimeoutError, ValueError, OSError)):
                    await transport.request('probe', {} if operation == 'reply-timeout' else {'text': 'x' * 1_000_000})
            await asyncio.wait_for(asyncio.shield(transport.reader), .6)
            assert time.monotonic() - started < .6
            assert failures == ([] if operation.startswith('close') else ['unavailable'])
            assert transport.process.poll() is not None
            assert not transport.pending and not transport.io_tasks
            await transport.close()  # Repeated cleanup cannot duplicate the callback.
            assert failures == ([] if operation.startswith('close') else ['unavailable'])
        finally:
            await transport.owned.close(graceful=False)
            await transport.close()
            if request is not None: await asyncio.gather(request, return_exceptions=True)
    asyncio.run(run())

def test_project_initialization_never_opens_disk_overlays_and_regular_change_notifies(tmp_path):
    async def run():
        m = api()
        module = tmp_path / 'Module.bsl'; module.write_text('saved')
        c = project_context(tmp_path)
        t = Transport(); child = m.ChildSession(c, lambda _: None, transport=t)
        try:
            await child.synchronize(c)
            assert not any(method.startswith('textDocument/') for method, _ in t.events)
            from onec_runtime_jupyter.lsp_workspace import WorkspaceChange
            change = WorkspaceChange(({'uri': module.as_uri(), 'type': 2},))
            updated = {**c, 'analysis_revision': 1}
            await child.files_changed(updated, change)
            assert t.events[-1] == ('workspace/didChangeWatchedFiles', {'changes': list(change.events)})
            assert child.applied_fence == m.fence(updated)
            assert module.read_text() == 'saved'
        finally: await child.close()
    asyncio.run(run())


def test_definition_uri_is_current_context_path_and_deleted_file_is_unavailable(tmp_path):
    m = api()
    p = tmp_path / 'Module.bsl'; p.write_text('disk')
    c = project_context(tmp_path)
    mapper = m.WorkspaceMapper(tmp_path, c['document_uri'], tmp_path, c)
    expected = f"onec-bsl:{c['binding_id']}/{m.context_token(c)}/Module.bsl"
    assert mapper.to_client({'targetUri': p.as_uri()}) == {'targetUri': expected}
    p.write_text('new disk')
    assert mapper.to_client({'targetUri': p.as_uri()}) == {'targetUri': expected}
    c2 = {**c, 'epoch': 10}
    assert m.context_token(c2) == m.context_token(c)
    assert m.context_token({**c2, 'installation_id': 'other'}) != m.context_token(c)
    p.unlink()
    for uri in (p.as_uri(), (tmp_path.parent / 'outside.bsl').as_uri(),
                tmp_path.as_uri() + '/CommonModules/../outside.bsl'):
        with pytest.raises((ValueError, OSError)): mapper.to_client({'uri': uri})


def test_admitted_shared_baseline_is_not_scanned_again(tmp_path, monkeypatch):
    async def run():
        m = api()
        monkeypatch.setattr(m, 'check_workspace', lambda root: pytest.fail('duplicate safety scan'))
        c = project_context(tmp_path)
        child = m.ChildSession(c, lambda _: None, transport=Transport(),
                               workspace_checked=True, workspace_reason=None)
        try:
            await child.synchronize(c)
            assert child.mapper.root == tmp_path
        finally: await child.close()
    asyncio.run(run())


def test_file_change_cancels_stale_diagnostic_before_waiting_on_child_lock(tmp_path):
    from onec_runtime_jupyter.lsp_workspace import WorkspaceChange
    async def run():
        entered = asyncio.Event(); cancelled = asyncio.Event()
        class Delayed(Transport):
            async def request(self, method, params):
                if method == 'textDocument/diagnostic':
                    entered.set()
                    try: await asyncio.Future()
                    except asyncio.CancelledError: cancelled.set(); raise
                return await super().request(method, params)
        c = project_context(tmp_path); child = api().ChildSession(c, lambda _: None, transport=Delayed())
        try:
            await child.synchronize(c)
            await child.notify('textDocument/didOpen', {'textDocument': {
                'uri': c['document_uri'], 'languageId': 'bsl', 'version': 1, 'text': 'x'}})
            await entered.wait()
            change = WorkspaceChange(({'uri': (tmp_path / 'Module.bsl').as_uri(), 'type': 2},))
            await asyncio.wait_for(child.files_changed({**c, 'analysis_revision': 1}, change), .5)
            assert cancelled.is_set()
            assert any(method == 'workspace/didChangeWatchedFiles' for method, _ in child.transport.events)
        finally: await child.close()
    asyncio.run(run())
