"""Windows native ownership certificates, independent of PID/accounting liveness."""
import asyncio
import ctypes
import os
import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows native ownership')


def test_accounting_zero_does_not_complete_close_before_descendant_signal():
    """Removing the descendant native-signal barrier must fail this test."""
    from onec_runtime_jupyter.lsp_process import OwnedProcess

    async def run():
        owned = OwnedProcess([sys._base_executable, '-c',
            'import subprocess,sys,time;'
            'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
            'print(p.pid,flush=True);time.sleep(60)'])
        descendant = int(await asyncio.to_thread(owned.process.stdout.readline))
        api = owned.job.api
        native_wait = api.WaitForSingleObject
        native_terminate = api.TerminateJobObject
        terminated, signal, observed = threading.Event(), threading.Event(), threading.Event()
        completed = []

        def terminate(*args):
            result = native_terminate(*args)
            terminated.set()
            return result

        def wait(handle, milliseconds):
            if terminated.is_set() and api.GetProcessId(handle) == descendant:
                observed.set()
                if not signal.is_set():
                    return 258  # WAIT_TIMEOUT, regardless of accounting/root state.
            return native_wait(handle, milliseconds)

        api.TerminateJobObject = terminate
        api.WaitForSingleObject = wait
        async def close():
            try:
                await owned.close(graceful=False)
            finally:
                completed.append(True)
                observed.set()
        closing = asyncio.create_task(close())
        try:
            assert await asyncio.to_thread(observed.wait, 2), 'close never reached native barrier'
            assert not completed, 'close returned before the descendant native signal'
        finally:
            signal.set()
            await closing
        assert owned.process.poll() is not None
        assert owned.process.stdin.closed and owned.process.stdout.closed
    asyncio.run(run())


class NativeCall:
    """WinDLL-shaped boundary double; the real collector owns all policy."""
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class NativeJobFixture:
    """A deterministic kernel boundary for races unavailable on demand natively."""
    def __init__(self):
        self.processes = {10: {'identity': 100, 'signaled': False, 'member': False}}
        self.handles = {100: ('process', 10)}  # Original Popen handle, not Job-owned.
        self.next_handle = 200
        self.packets = queue.Queue()
        self.fail = {}
        self.calls = []
        self.total = 0
        self.active = 0
        self.resumed = False
        self.terminated = threading.Event()
        self.captured = threading.Event()
        self.auto_signal = True
        self.on_terminate = None
        self.max_process_handles = 0
        names = ('CreateJobObjectW SetInformationJobObject QueryInformationJobObject '
                 'AssignProcessToJobObject TerminateJobObject OpenThread GetProcessId '
                 'GetProcessIdOfThread ResumeThread CloseHandle OpenProcess IsProcessInJob '
                 'GetProcessTimes WaitForSingleObject GetCurrentProcess DuplicateHandle '
                 'CreateIoCompletionPort GetQueuedCompletionStatus PostQueuedCompletionStatus')
        for name in names.split():
            setattr(self, name, NativeCall(lambda *args, name=name: self.invoke(name, args)))

    @staticmethod
    def raw(value):
        return value.value if hasattr(value, 'value') else value

    def allocate(self, kind, identity=None):
        self.next_handle += 1
        self.handles[self.next_handle] = (kind, identity)
        self.max_process_handles = max(self.max_process_handles,
            sum(kind == 'process' for kind, _ in self.handles.values()) - 1)
        return self.next_handle

    def new(self, pid, *, identity=None, signaled=False, member=True, notify=True):
        self.processes[pid] = {'identity': identity or pid * 10, 'signaled': signaled, 'member': member}
        if member:
            self.total += 1
            if not signaled: self.active += 1
        if notify: self.packets.put((6, 1, pid))

    def invoke(self, name, args):
        self.calls.append((name, tuple(self.raw(arg) for arg in args if not hasattr(arg, '_obj'))))
        fault = self.fail.get(name)
        if fault is not None:
            return fault(*args) if callable(fault) else fault
        raw = [self.raw(arg) for arg in args]
        if name == 'CreateJobObjectW': return self.allocate('job')
        if name == 'CreateIoCompletionPort': return self.allocate('port')
        if name == 'SetInformationJobObject': return 1
        if name == 'QueryInformationJobObject':
            args[2]._obj.total, args[2]._obj.active = self.total, self.active
            return 1
        if name == 'AssignProcessToJobObject':
            self.processes[10]['member'] = True
            self.total += 1
            self.active += 1
            self.packets.put((6, 1, 10))
            return 1
        if name == 'TerminateJobObject':
            if self.on_terminate: self.on_terminate()
            if self.auto_signal:
                for process in self.processes.values():
                    if process['member']: process['signaled'] = True
            self.active = 0  # Deliberately independent from actual process signals.
            self.terminated.set()
            return 1
        if name == 'GetCurrentProcess': return -1
        if name == 'DuplicateHandle':
            args[3]._obj.value = self.allocate('process', self.handles[raw[1]][1])
            return 1
        if name == 'OpenThread': return self.allocate('thread', 10)
        if name == 'GetProcessIdOfThread': return self.handles.get(raw[0], (None, 0))[1]
        if name == 'ResumeThread':
            self.resumed = True
            return 1
        if name == 'GetProcessId': return self.handles.get(raw[0], (None, 0))[1]
        if name == 'OpenProcess':
            pid = raw[2]
            if pid not in self.processes: return 0
            return self.allocate('process', pid)
        if name == 'IsProcessInJob':
            pid = self.handles[raw[0]][1]
            args[2]._obj.value = self.processes[pid]['member']
            return 1
        if name == 'GetProcessTimes':
            pid = self.handles[raw[0]][1]
            identity = self.processes[pid]['identity']
            args[1]._obj.dwHighDateTime = identity >> 32
            args[1]._obj.dwLowDateTime = identity & 0xffffffff
            return 1
        if name == 'WaitForSingleObject':
            pid = self.handles[raw[0]][1]
            self.captured.set()
            return 0 if self.processes[pid]['signaled'] else 258
        if name == 'CloseHandle':
            return int(self.handles.pop(raw[0], None) is not None)
        if name == 'GetQueuedCompletionStatus':
            try:
                message, key, value = self.packets.get(timeout=raw[4] / 1000)
            except queue.Empty:
                args[3]._obj.value = None
                # Dirty outputs intentionally prove FALSE/null is handled first.
                args[1]._obj.value, args[2]._obj.value = 0xffffffff, 999
                ctypes.set_last_error(258)
                return 0
            args[1]._obj.value, args[2]._obj.value, args[3]._obj.value = message, key, value
            return 1
        if name == 'PostQueuedCompletionStatus':
            self.packets.put((raw[1], raw[2], raw[3]))
            return 1
        raise AssertionError(name)


@pytest.fixture
def native_job(monkeypatch):
    import onec_runtime_jupyter.lsp_process_windows as module
    native = NativeJobFixture()
    monkeypatch.setattr(module.ctypes, 'WinDLL', lambda *args, **kwargs: native)
    monkeypatch.setattr(module.psutil, 'Process', lambda pid: SimpleNamespace(threads=lambda: [SimpleNamespace(id=20)]))
    job = module._WindowsJob()
    process = SimpleNamespace(_handle=100, pid=10, poll=lambda: None)
    yield job, native, process
    try:
        if not job.requested.is_set(): job.terminate(time.monotonic() + .2)
    except (ValueError, TimeoutError):
        pass
    if job.collector: job.collector.join(1)
    job.close()


def eventually(predicate, seconds=1):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        threading.Event().wait(.001)
    assert predicate(), 'bounded fixture state was not reached'


def test_missing_root_notification_uses_exact_bootstrap(native_job):
    job, native, process = native_job
    native.AssignProcessToJobObject.function = lambda *args: (
        native.processes[10].update(member=True), setattr(native, 'total', 1), 1)[-1]
    job.assign_and_resume(process)
    job.terminate(time.monotonic() + .2)
    assert job.certified and job.retired == 1
    assert native.handles == {100: ('process', 10)}


def test_captured_churn_reclaims_more_than_capacity_and_counts_reused_pid(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    for generation in range(70):
        native.new(11, identity=1000 + generation)
        eventually(lambda: 11 in job.live)
        native.processes[11]['signaled'] = True
        native.active -= 1
        native.packets.put((7, 1, 11))
        eventually(lambda: 11 not in job.live)
    job.terminate(time.monotonic() + .2)
    assert job.certified and job.retired == 71
    assert native.max_process_handles == 2
    assert native.handles == {100: ('process', 10)}


def test_duplicate_live_and_root_notifications_do_not_invent_population(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    native.new(11)
    for _ in range(4):
        native.packets.put((6, 1, 10))
        native.packets.put((6, 1, 11))
    eventually(lambda: 11 in job.live and native.packets.empty())
    job.terminate(time.monotonic() + .2)
    assert job.certified and job.retired == 2
    assert native.max_process_handles == 2


@pytest.mark.parametrize('candidate', ['lost', 'signaled', 'absent', 'foreign', 'denied'])
def test_uncertifiable_candidate_never_becomes_assumed_retirement(native_job, candidate):
    job, native, process = native_job
    job.assign_and_resume(process)
    if candidate == 'absent': native.packets.put((6, 1, 99))
    else:
        if candidate == 'denied': native.fail['OpenProcess'] = 0
        native.new(11, notify=candidate != 'lost', signaled=candidate == 'signaled', member=candidate != 'foreign')
    if candidate != 'lost': eventually(lambda: job.fault)
    with pytest.raises((ValueError, TimeoutError), match='gateway-termination-'):
        job.terminate(time.monotonic() + .1)
    assert not job.certified
    job.collector.join(1)  # A timeout reports deferred ownership, not completed cleanup.
    assert not job.collector.is_alive()
    assert native.handles == {100: ('process', 10)}


def test_child_born_and_retired_during_termination_remains_in_lifetime_count(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    native.on_terminate = lambda: native.new(11, signaled=True, notify=False)
    with pytest.raises((ValueError, TimeoutError)):
        job.terminate(time.monotonic() + .1)
    assert not job.certified and job.retired == 1


def test_failed_wake_still_stops_dormant_collector_within_deadline(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    eventually(native.packets.empty)
    native.fail['PostQueuedCompletionStatus'] = 0
    with pytest.raises(ValueError, match='gateway-termination-unconfirmed'):
        job.terminate(time.monotonic() + .3)
    assert not job.collector.is_alive()
    assert native.terminated.is_set()
    assert native.handles == {100: ('process', 10)}


def test_native_fault_during_detach_does_not_kill_before_ordinary_close(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    def failed_detach(*args):
        raise OSError('private native error')
    native.fail['SetInformationJobObject'] = failed_detach
    native.packets.put((999, 1, None))
    eventually(lambda: job.fault)
    # A recording/detachment fault cannot become an asynchronous Job abort.
    eventually(lambda: job.port is None or not job.collector.is_alive())
    assert job.collector.is_alive(), 'fault collector abandoned lifetime ownership'
    assert not native.terminated.is_set()
    assert any(kind == 'job' for kind, _ in native.handles.values())
    with pytest.raises(ValueError): job.terminate(time.monotonic() + .2)
    assert not job.collector.is_alive()


def test_raising_control_wake_still_joins_and_cleans_collector(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    def failed_wake(*args):
        raise OSError('private native error')
    native.fail['PostQueuedCompletionStatus'] = failed_wake
    with pytest.raises(ValueError, match='^gateway-termination-unconfirmed$'):
        job.terminate(time.monotonic() + .3)
    assert not job.collector.is_alive()
    assert native.handles == {100: ('process', 10)}


def test_owned_process_still_checks_root_after_certificate_failure():
    from io import BytesIO
    from onec_runtime_jupyter.lsp_process import OwnedProcess
    waits = []
    class FailedJob:
        def terminate(self, deadline):
            raise ValueError('gateway-termination-unconfirmed')
        def close(self):
            pass
    owned = OwnedProcess.__new__(OwnedProcess)
    owned.closed, owned.job, owned._liveness = False, FailedJob(), None
    owned.process = SimpleNamespace(stdin=BytesIO(), stdout=BytesIO(),
                                    wait=lambda timeout: waits.append(timeout))
    with pytest.raises(ValueError, match='^gateway-termination-unconfirmed$'):
        owned._close(0, False)
    assert len(waits) == 1, 'certificate failure skipped independent root wait'
    assert 0 <= waits[0] <= 5
    assert owned.process.stdin.closed and owned.process.stdout.closed


@pytest.mark.parametrize('fault', ['raise', 'false'])
def test_terminate_failure_still_observes_tracked_native_handles(native_job, fault):
    job, native, process = native_job
    job.assign_and_resume(process)
    native.captured.clear()
    def terminate(*args):
        native.processes[10]['signaled'] = True
        native.active = 0
        native.calls.clear()
        if fault == 'raise': raise OSError('private error')
        return 0
    native.fail['TerminateJobObject'] = terminate
    with pytest.raises(ValueError): job.terminate(time.monotonic() + .2)
    assert any(name == 'WaitForSingleObject' for name, _ in native.calls), 'termination failure skipped native cleanup checks'
    assert native.handles == {100: ('process', 10)}


def test_fault_on_one_native_handle_does_not_skip_other_handle_checks(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    native.new(11)
    eventually(lambda: 11 in job.live)
    original = native.WaitForSingleObject.function
    checked = []
    def wait(handle, milliseconds):
        if native.terminated.is_set():
            pid = native.handles[native.raw(handle)][1]
            checked.append(pid)
            if pid == 10: return 0xffffffff
        return original(handle, milliseconds)
    native.WaitForSingleObject.function = wait
    with pytest.raises((ValueError, TimeoutError)):
        job.terminate(time.monotonic() + .1)
    job.collector.join(1)
    assert 11 in checked, 'first failed native wait masked independent descendant check'
    assert native.handles == {100: ('process', 10)}


def test_partial_owned_start_cleanup_error_cannot_skip_pipes_or_escape_sanitization(monkeypatch):
    from io import BytesIO
    import onec_runtime_jupyter.lsp_process as module
    class FailedJob:
        def assign_and_resume(self, process): raise OSError('private assignment path')
        def close(self): raise OSError('private cleanup path')
    process = SimpleNamespace(pid=10, stdin=BytesIO(), stdout=BytesIO(),
                              kill=lambda: None, wait=lambda timeout: None)
    monkeypatch.setattr(module, '_WindowsJob', FailedJob)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs: process)
    with pytest.raises(ValueError, match='^gateway-ownership-unavailable$'):
        module.OwnedProcess(['private command'])
    assert process.stdin.closed and process.stdout.closed


def test_late_native_return_keeps_job_owned_until_collector_exits(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    entered, release = threading.Event(), threading.Event()
    original = native.TerminateJobObject.function
    def terminate(*args):
        entered.set()
        assert release.wait(1)
        return original(*args)
    native.TerminateJobObject.function = terminate
    try:
        with pytest.raises(TimeoutError): job.terminate(time.monotonic() + .03)
        assert entered.is_set() and job.collector.is_alive()
        job.close()
        assert job.handle in native.handles, 'caller raced Job close against collector native call'
        assert job.live, 'caller stole collector-owned tracking handles'
    finally:
        release.set()
        job.collector.join(1)
    assert not job.collector.is_alive()
    assert native.handles == {100: ('process', 10)}


@pytest.mark.parametrize('api', ['AssignProcessToJobObject', 'DuplicateHandle', 'OpenThread', 'GetProcessIdOfThread', 'ResumeThread'])
def test_bootstrap_failures_do_not_leave_collector_or_owned_native_handles(native_job, api):
    job, native, process = native_job
    native.fail[api] = 0
    with pytest.raises(ValueError, match='gateway-'):
        job.assign_and_resume(process)
    assert not native.resumed
    native.processes[10]['signaled'] = True  # Exact suspended-root caller cleanup.
    if job.collector:
        try: job.terminate(time.monotonic() + .2)
        except ValueError: pass
        assert not job.collector.is_alive()
    job.close()
    assert native.handles == {100: ('process', 10)}


@pytest.mark.parametrize('kind', ['create-job', 'limits', 'create-port', 'associate'])
def test_constructor_failure_reclaims_partial_job_and_port(monkeypatch, kind):
    import onec_runtime_jupyter.lsp_process_windows as module
    native = NativeJobFixture()
    if kind == 'create-job': native.fail['CreateJobObjectW'] = 0
    if kind == 'create-port': native.fail['CreateIoCompletionPort'] = 0
    if kind in ('limits', 'associate'):
        native.fail['SetInformationJobObject'] = lambda handle, info, *rest: int(info != (9 if kind == 'limits' else 7))
    monkeypatch.setattr(module.ctypes, 'WinDLL', lambda *args, **kwargs: native)
    with pytest.raises(ValueError, match='^gateway-ownership-unavailable$'):
        module._WindowsJob()
    assert native.handles == {100: ('process', 10)}


@pytest.mark.parametrize('fault', ['membership', 'identity', 'wait', 'query', 'close', 'port', 'key', 'message', 'pid', 'total-overflow', 'capacity'])
def test_native_uncertainty_and_capacity_never_certify_cleanup(native_job, fault):
    job, native, process = native_job
    job.assign_and_resume(process)
    eventually(native.packets.empty)
    if fault in ('membership', 'identity', 'wait'):
        native.fail[{'membership': 'IsProcessInJob', 'identity': 'GetProcessTimes', 'wait': 'WaitForSingleObject'}[fault]] = 0 if fault != 'wait' else 0xffffffff
        native.new(11)
    elif fault == 'query': native.fail['QueryInformationJobObject'] = 0
    elif fault == 'close':
        native.fail['CloseHandle'] = lambda handle: 0 if native.handles.get(native.raw(handle), ('',))[0] == 'process' else int(native.handles.pop(native.raw(handle), None) is not None)
    elif fault == 'port':
        def failed_port(*args):
            args[3]._obj.value = None
            ctypes.set_last_error(5)
            return 0
        native.fail['GetQueuedCompletionStatus'] = failed_port
    elif fault == 'key': native.packets.put((6, 999, 11))
    elif fault == 'message': native.packets.put((999, 1, None))
    elif fault == 'pid': native.packets.put((6, 1, None))
    elif fault == 'total-overflow': native.total = 0xffffffff
    elif fault == 'capacity':
        for pid in range(11, 76): native.new(pid)
        eventually(lambda: job.fault)
        assert native.max_process_handles <= 64, 'capture exceeded the owned handle cap'
    with pytest.raises((ValueError, TimeoutError)):
        job.terminate(time.monotonic() + .15)
    job.collector.join(1)
    assert not job.certified or job.fault
    assert not job.collector.is_alive()
    if fault != 'close': assert native.handles == {100: ('process', 10)}


@pytest.mark.parametrize('inner_close', [False, True])
def test_real_nested_eight_trees_signal_before_outer_close_and_preserve_siblings(inner_close):
    import json
    import subprocess
    from lsp_native_checks import NativeReceipt
    from onec_runtime_jupyter.lsp_process import OwnedProcess

    program = '''import asyncio,json,sys
from onec_runtime_jupyter.lsp_process import OwnedProcess
children=[];identities=[]
for _ in range(8):
 child=OwnedProcess([sys.executable,'-c','import subprocess,sys,time;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);print(p.pid,flush=True);time.sleep(60)'])
 children.append(child)
 identities.extend([child.process.pid,int(child.process.stdout.readline())])
print(json.dumps(identities),flush=True)
if sys.stdin.readline().strip() == 'inner':
 async def close():
  for child in children: await child.close(graceful=False)
 asyncio.run(close())
 print(json.dumps([child.job.retired if child.job.certified else -1 for child in children]),flush=True)
sys.stdin.read()
'''
    async def run():
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)}
        owned = OwnedProcess([sys._base_executable, '-c', program], env=env)
        sibling = OwnedProcess([sys._base_executable, '-c', 'import time;time.sleep(60)'])
        foreign = subprocess.Popen([sys._base_executable, '-c', 'import time;time.sleep(60)'])
        receipts = []
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(owned.process.stdout.readline), 15)
            pids = json.loads(raw)
            assert len(pids) == 16
            receipts = [NativeReceipt(pid) for pid in [owned.process.pid, *pids]]
            await asyncio.to_thread(eventually, lambda: len(owned.job.live) == 17, 2)
            if inner_close:
                owned.process.stdin.write(b'inner\n'); owned.process.stdin.flush()
                outcomes = await asyncio.wait_for(asyncio.to_thread(owned.process.stdout.readline), 10)
                assert json.loads(outcomes) == [2] * 8
            await owned.close(graceful=False)
            for receipt in receipts: receipt.assert_signaled()
            assert owned.job.certified and owned.job.retired == 17
            assert not owned.job.collector.is_alive()
            assert owned.job.handle is None and owned.job.port is None and not owned.job.live
            assert sibling.process.poll() is None and foreign.poll() is None
        finally:
            await owned.close(graceful=False)
            await sibling.close(graceful=False)
            foreign.terminate(); foreign.wait(timeout=5)
            for receipt in receipts: receipt.close()
    asyncio.run(run())


def test_real_captured_churn_exceeds_live_capacity_without_lifetime_rejection():
    from onec_runtime_jupyter.lsp_process import OwnedProcess
    program = '''import subprocess,sys
for _ in range(70):
 child=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.read(1)'],stdin=subprocess.PIPE)
 print(child.pid,flush=True)
 sys.stdin.readline()
 child.stdin.write(b'x');child.stdin.flush();child.wait();child.stdin.close()
print('done',flush=True)
sys.stdin.readline()
'''
    async def run():
        owned = OwnedProcess([sys._base_executable, '-c', program])
        maximum = 0
        try:
            for generation in range(70):
                pid = int(await asyncio.wait_for(asyncio.to_thread(owned.process.stdout.readline), 5))
                await asyncio.to_thread(eventually, lambda: pid in owned.job.live, 2)
                maximum = max(maximum, len(owned.job.live))
                owned.process.stdin.write(b'next\n'); owned.process.stdin.flush()
            assert await asyncio.wait_for(asyncio.to_thread(owned.process.stdout.readline), 5) == b'done\r\n'
            await owned.close(graceful=False)
            assert owned.job.certified and owned.job.retired == 71
            assert maximum <= 3 and not owned.job.live
            assert not owned.job.collector.is_alive()
        finally:
            await owned.close(graceful=False)
    asyncio.run(run())


def test_close_packet_flood_is_work_bounded_even_before_deadline(native_job):
    job, native, process = native_job
    native.auto_signal = False
    def packet(port, message, key, value, timeout):
        message._obj.value, key._obj.value, value._obj.value = 4, 1, None
        return 1
    native.fail['GetQueuedCompletionStatus'] = packet
    native.on_terminate = native.calls.clear
    job.assign_and_resume(process)
    with pytest.raises(ValueError, match='gateway-termination-unconfirmed'):
        job.terminate(time.monotonic() + 3)
    packet_calls = [call for call in native.calls if call[0] == 'GetQueuedCompletionStatus']
    assert 0 < len(packet_calls) <= 512 * 64
    assert not job.certified and not job.collector.is_alive()


def test_routine_timeout_does_not_scan_handles_or_parse_dirty_outputs(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    eventually(native.packets.empty)
    native.calls.clear()
    eventually(lambda: sum(name == 'GetQueuedCompletionStatus' for name, _ in native.calls) >= 3)
    assert not job.fault
    assert not any(name in ('WaitForSingleObject', 'QueryInformationJobObject', 'OpenProcess') for name, _ in native.calls)
    assert all(args[-1] <= 100 for name, args in native.calls if name == 'GetQueuedCompletionStatus')
    job.terminate(time.monotonic() + .3)


def test_early_exit_hint_cannot_retire_a_nonsignaled_process(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    native.new(11)
    eventually(lambda: 11 in job.live)
    native.packets.put((7, 1, 11))
    native.packets.put((4, 1, None))
    eventually(native.packets.empty)
    assert job.retired == 0 and set(job.live) == {10, 11}
    # Lost EXIT later is harmless: close independently waits native signals.
    job.terminate(time.monotonic() + .3)
    assert job.certified and job.retired == 2


def test_root_wait_uses_only_remaining_forced_termination_budget(monkeypatch):
    from io import BytesIO
    import onec_runtime_jupyter.lsp_process as module
    clock, waits = [100.0], []
    class Job:
        def terminate(self, deadline):
            assert deadline == 105.0
            clock[0] = 104.75
        def close(self): pass
    owned = module.OwnedProcess.__new__(module.OwnedProcess)
    owned.closed, owned.job, owned._liveness = False, Job(), None
    owned.process = SimpleNamespace(stdin=BytesIO(), stdout=BytesIO(), wait=lambda timeout: waits.append(timeout))
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    owned._close(0, False)
    assert waits == [.25], 'root wait restarted the five-second termination budget'


def test_repeated_cancelled_close_keeps_one_collector_and_termination():
    from onec_runtime_jupyter.lsp_process import OwnedProcess
    async def run():
        owned = OwnedProcess([sys._base_executable, '-c', 'import time;time.sleep(60)'])
        collector = owned.job.collector
        api = owned.job.api
        original_wait, original_terminate = api.WaitForSingleObject, api.TerminateJobObject
        waiting, release = threading.Event(), threading.Event()
        terminations = []
        def terminate(*args):
            terminations.append(True)
            return original_terminate(*args)
        def wait(handle, timeout):
            if terminations and not release.is_set():
                waiting.set()
                return 258
            return original_wait(handle, timeout)
        api.TerminateJobObject, api.WaitForSingleObject = terminate, wait
        first = asyncio.create_task(owned.close(graceful=False))
        try:
            assert await asyncio.to_thread(waiting.wait, 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError): await first
            second = asyncio.create_task(owned.close(graceful=False))
            await asyncio.sleep(0)
            second.cancel(); second.cancel()
            with pytest.raises(asyncio.CancelledError): await second
            assert owned.job.collector is collector and collector.is_alive()
            assert terminations == [True]
        finally:
            release.set()
            await owned.close(graceful=False)
        assert not collector.is_alive() and owned.job.certified
        assert terminations == [True]
    asyncio.run(run())


def test_failed_candidate_handle_close_is_retained_for_independent_final_cleanup(native_job):
    job, native, process = native_job
    job.assign_and_resume(process)
    native.new(11, member=False, notify=False)
    failed = []
    def close(handle):
        handle = native.raw(handle)
        if native.handles.get(handle) == ('process', 11) and not failed:
            failed.append(handle)
            return 0
        return int(native.handles.pop(handle, None) is not None)
    native.fail['CloseHandle'] = close
    native.packets.put((6, 1, 11))
    eventually(lambda: job.fault)
    with pytest.raises(ValueError): job.terminate(time.monotonic() + .3)
    assert failed
    assert native.handles == {100: ('process', 10)}, 'failed candidate handle close lost cleanup ownership'


def test_failed_bootstrap_thread_handle_close_is_retried_by_final_owner(native_job):
    job, native, process = native_job
    failed = []
    def close(handle):
        handle = native.raw(handle)
        if native.handles.get(handle, ('',))[0] == 'thread' and not failed:
            failed.append(handle)
            return 0
        return int(native.handles.pop(handle, None) is not None)
    native.fail['CloseHandle'] = close
    with pytest.raises(ValueError): job.assign_and_resume(process)
    with pytest.raises(ValueError): job.terminate(time.monotonic() + .3)
    assert failed
    assert native.handles == {100: ('process', 10)}, 'failed thread close lost bootstrap cleanup ownership'


def test_late_accounting_query_cannot_leave_success_certificate(native_job, monkeypatch):
    import onec_runtime_jupyter.lsp_process_windows as module
    job, native, process = native_job
    job.assign_and_resume(process)
    clock = [time.monotonic()]
    deadline = clock[0] + .2
    original = native.QueryInformationJobObject.function
    def query(*args):
        result = original(*args)
        clock[0] = deadline + 1  # Native call returns only after the shared deadline.
        return result
    native.QueryInformationJobObject.function = query
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    try:
        job.terminate(deadline)
    except (ValueError, TimeoutError):
        pass
    job.collector.join(1)
    assert not job.collector.is_alive()
    assert not job.certified, 'late native accounting minted a success certificate'
    with pytest.raises((ValueError, TimeoutError)):
        job.terminate(deadline)  # A completed subsequent join cannot erase timeout.


def test_completed_join_returning_after_deadline_is_still_failure(native_job, monkeypatch):
    import onec_runtime_jupyter.lsp_process_windows as module
    job, native, process = native_job
    job.assign_and_resume(process)
    clock = [time.monotonic()]
    deadline = clock[0] + .2
    original = job.collector.join
    def join(timeout):
        original(timeout)
        clock[0] = deadline + 1  # Caller resumes late even though the thread ended.
    monkeypatch.setattr(job.collector, 'join', join)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    with pytest.raises(TimeoutError, match='^gateway-termination-timeout$'):
        job.terminate(deadline)
    assert not job.certified and job.fault


def test_root_wait_returning_after_deadline_cannot_complete_owned_close(monkeypatch):
    from io import BytesIO
    import onec_runtime_jupyter.lsp_process as module
    clock = [100.0]
    class Job:
        def terminate(self, deadline): clock[0] = 104.75
        def close(self): pass
    def wait(timeout):
        assert timeout == .25
        clock[0] = 105.01
    owned = module.OwnedProcess.__new__(module.OwnedProcess)
    owned.closed, owned.job, owned._liveness = False, Job(), None
    owned.process = SimpleNamespace(stdin=BytesIO(), stdout=BytesIO(), wait=wait)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    with pytest.raises(TimeoutError, match='^gateway-termination-timeout$'):
        owned._close(0, False)
    assert owned.process.stdin.closed and owned.process.stdout.closed
