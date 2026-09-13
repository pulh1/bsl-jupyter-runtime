"""Bounded, launch-lifetime Windows Job process-signal certificates.

The daemon collector exclusively owns tracking handles after bootstrap. Job
notifications discover candidates; only native signals plus lifetime accounting
certify termination. Missing notifications deliberately fail closed.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import threading
import time

import psutil

MAX_TRACKED = 64
MAX_PACKETS = 64
MAX_CLOSE_BATCHES = 512
MAX_TOTAL = 0xffffffff
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 258
JOB_KEY, CONTROL_KEY = 1, 2


class _BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', w.DWORD), ('min_working_set', ctypes.c_size_t), ('max_working_set', ctypes.c_size_t),
                ('active_limit', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', _BasicLimits), ('io_counters', ctypes.c_ulonglong * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [('times', ctypes.c_longlong * 4), ('page_faults', w.DWORD), ('total', w.DWORD),
                ('active', w.DWORD), ('terminated', w.DWORD)]


class _PortAssociation(ctypes.Structure):
    _fields_ = [('key', ctypes.c_void_p), ('port', w.HANDLE)]


class _WindowsJob:
    def __init__(self):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
            'OpenThread': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'GetProcessId': ([w.HANDLE], w.DWORD),
            'GetProcessIdOfThread': ([w.HANDLE], w.DWORD),
            'ResumeThread': ([w.HANDLE], w.DWORD),
            'CloseHandle': ([w.HANDLE], w.BOOL),
            'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'IsProcessInJob': ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
            'GetProcessTimes': ([w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)], w.BOOL),
            'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
            'GetCurrentProcess': ([], w.HANDLE),
            'DuplicateHandle': ([w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD], w.BOOL),
            'CreateIoCompletionPort': ([w.HANDLE, w.HANDLE, ctypes.c_size_t, w.DWORD], w.HANDLE),
            'GetQueuedCompletionStatus': ([w.HANDLE, ctypes.POINTER(w.DWORD), ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p), w.DWORD], w.BOOL),
            'PostQueuedCompletionStatus': ([w.HANDLE, w.DWORD, ctypes.c_size_t, ctypes.c_void_p], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.port = None
        self.rejected_handle = self.bootstrap_thread = None
        self.live = {}
        self.retired = 0
        self.root_pid = None
        self.fault = False
        self.certified = False
        self.collector = None
        self.requested = threading.Event()
        self.deadline = None
        self._request_lock = threading.Lock()
        try:
            self.handle = self.api.CreateJobObjectW(None, None)
            if not self.handle: raise ValueError
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ValueError
            self.port = self.api.CreateIoCompletionPort(w.HANDLE(-1), None, 0, 1)
            if not self.port: raise ValueError
            association = _PortAssociation(JOB_KEY, self.port)
            if not self.api.SetInformationJobObject(self.handle, 7, ctypes.byref(association), ctypes.sizeof(association)):
                raise ValueError
        except Exception:
            self._dispose()
            raise ValueError('gateway-ownership-unavailable') from None

    def _identity(self, handle, pid):
        member = w.BOOL()
        times = [w.FILETIME() for _ in range(4)]
        if (self.api.GetProcessId(handle) != pid
                or not self.api.IsProcessInJob(handle, self.handle, ctypes.byref(member)) or not member.value
                or not self.api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))):
            raise ValueError('gateway-ownership-unavailable')
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def _retain(self, handle, pid):
        try:
            identity = self._identity(handle, pid)
            if self.api.WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:
                raise ValueError
            if len(self.live) >= MAX_TRACKED or self.retired + len(self.live) >= MAX_TOTAL:
                raise ValueError
            self.live[pid] = (handle, identity)
        except Exception:
            self.fault = True
            # Admission stops on this fault, so there can be only one rejected
            # candidate. It never contributes a population/retirement receipt.
            self.rejected_handle = handle
            try:
                if self.api.CloseHandle(handle): self.rejected_handle = None
            except Exception:
                pass
            raise ValueError('gateway-ownership-unavailable') from None

    def assign_and_resume(self, process):
        # Exact original CreateProcess handle; no root PID-reopen race.
        handle = w.HANDLE(int(process._handle))
        if process.poll() is not None or self.api.GetProcessId(handle) != process.pid:
            raise ValueError('gateway-ownership-unavailable')
        if not self.api.AssignProcessToJobObject(self.handle, handle):
            raise ValueError('gateway-ownership-unavailable')
        duplicate = w.HANDLE()
        current = self.api.GetCurrentProcess()
        if not self.api.DuplicateHandle(current, handle, current, ctypes.byref(duplicate), 0x100000 | 0x1000, False, 0):
            raise ValueError('gateway-ownership-unavailable')
        self._retain(duplicate.value, process.pid)
        self.root_pid = process.pid  # Popen pins this identity even after retirement.
        threads = psutil.Process(process.pid).threads()
        if len(threads) != 1: raise ValueError('gateway-primary-thread-unavailable')
        thread = self.api.OpenThread(0x0002 | 0x0800, False, threads[0].id)
        if not thread: raise ValueError('gateway-primary-thread-unavailable')
        self.bootstrap_thread = thread
        try:
            if process.poll() is not None or self.api.GetProcessIdOfThread(thread) != process.pid:
                raise ValueError('gateway-primary-thread-unavailable')
            collector = threading.Thread(target=self._collect, name='onec-owned-job', daemon=True)
            collector.start()
            self.collector = collector
            if self.api.ResumeThread(thread) != 1:
                raise ValueError('gateway-primary-thread-unavailable')
        finally:
            try:
                closed = bool(self.api.CloseHandle(thread))
            except Exception:
                closed = False
            if closed:
                self.bootstrap_thread = None
            else:
                self.fault = True
                raise ValueError('gateway-primary-thread-unavailable') from None

    def _reap(self):
        failed = False
        for pid, (handle, _) in tuple(self.live.items()):
            try:
                status = self.api.WaitForSingleObject(handle, 0)
                if status == WAIT_OBJECT_0:
                    if not self.api.CloseHandle(handle): raise ValueError
                    del self.live[pid]
                    if self.retired >= MAX_TOTAL - 1: raise ValueError
                    self.retired += 1
                elif status != WAIT_TIMEOUT:
                    raise ValueError
            except Exception:
                failed = True
        if failed: raise ValueError('gateway-termination-unconfirmed')

    def _packet(self, milliseconds):
        message, key, value = w.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
        success = self.api.GetQueuedCompletionStatus(self.port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(value), milliseconds)
        if not success:
            # FALSE/null has indeterminate message/key outputs; never parse them.
            if value.value is None and ctypes.get_last_error() == WAIT_TIMEOUT:
                return False
            raise ValueError
        if key.value == CONTROL_KEY and message.value == 0 and value.value is None:
            return True
        if key.value != JOB_KEY or message.value not in (4, 6, 7, 8):
            raise ValueError
        pid = value.value
        if message.value == 4:
            if pid is not None: raise ValueError
        elif type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise ValueError
        self._reap()
        if message.value == 6 and pid != self.root_pid and pid not in self.live:
            if len(self.live) >= MAX_TRACKED: raise ValueError
            handle = self.api.OpenProcess(0x100000 | 0x1000, False, pid)
            if not handle: raise ValueError
            self._retain(handle, pid)
        return True

    def _detach(self):
        if self.port:
            association = _PortAssociation(JOB_KEY, None)
            try:
                if not self.api.SetInformationJobObject(self.handle, 7, ctypes.byref(association), ctypes.sizeof(association)):
                    self.fault = True
            except Exception:
                self.fault = True
            try:
                if not self.api.CloseHandle(self.port): self.fault = True
                else: self.port = None
            except Exception:
                self.fault = True

    def _dispose(self):
        # Only the collector calls this after it starts; never race-close a query.
        try:
            self._detach()
        except Exception:
            self.fault = True
        for pid, (handle, _) in tuple(self.live.items()):
            try:
                if not self.api.CloseHandle(handle): self.fault = True
                else: del self.live[pid]
            except Exception:
                self.fault = True
        for field in ('rejected_handle', 'bootstrap_thread'):
            handle = getattr(self, field)
            if handle:
                try:
                    if not self.api.CloseHandle(handle): self.fault = True
                    else: setattr(self, field, None)
                except Exception:
                    self.fault = True
        if self.handle:
            try:
                if not self.api.CloseHandle(self.handle): self.fault = True
                else: self.handle = None
            except Exception:
                self.fault = True

    def _collect(self):
        try:
            while not self.requested.is_set():
                if self.fault:
                    self.requested.wait(.1)  # Same daemon retains known handles.
                    continue
                try:
                    if self._packet(100):
                        for _ in range(MAX_PACKETS - 1):
                            if self.requested.is_set() or not self._packet(0): break
                except Exception:
                    self.fault = True
                    self._detach()
            try:
                terminated = bool(self.api.TerminateJobObject(self.handle, 1))
            except Exception:
                terminated = False
            if not terminated: self.fault = True
            for _ in range(MAX_CLOSE_BATCHES):
                remaining = self.deadline - time.monotonic()
                if remaining <= 0: break
                try:
                    if self.port and not self.fault:
                        if self._packet(min(10, max(0, int(remaining * 1000)))):
                            for _ in range(MAX_PACKETS - 1):
                                if time.monotonic() >= self.deadline or not self._packet(0): break
                    self._reap()
                    if not self.live:
                        counts = _Accounting()
                        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(counts), ctypes.sizeof(counts), None):
                            raise ValueError
                        if time.monotonic() >= self.deadline: break
                        if not self.fault and terminated and counts.active == 0 and counts.total == self.retired and counts.total < MAX_TOTAL:
                            self.certified = True
                            break
                        if self.fault: break
                    if self.fault:
                        # Port has been detached: bounded native-signal recheck only
                        # during close, never routine background PID polling.
                        threading.Event().wait(min(.01, remaining))
                except Exception:
                    self.fault = True
                    self._detach()
        except Exception:
            self.fault = True
        finally:
            self._dispose()

    def terminate(self, deadline=None):
        with self._request_lock:
            if not self.requested.is_set():
                self.deadline = time.monotonic() + 5 if deadline is None else deadline
                self.requested.set()
                try:
                    if self.port and not self.api.PostQueuedCompletionStatus(self.port, 0, CONTROL_KEY, None):
                        self.fault = True
                except Exception:
                    self.fault = True
        if self.collector is None:
            # No code was resumed. This caller still owns partial bootstrap state.
            self._collect()
        else:
            self.collector.join(max(0, self.deadline - time.monotonic()))
        if time.monotonic() >= self.deadline or self.collector is not None and self.collector.is_alive():
            self.fault = True
            self.certified = False
            raise TimeoutError('gateway-termination-timeout')
        if not self.certified or self.fault:
            raise ValueError('gateway-termination-unconfirmed')

    def close(self):
        if self.collector is not None:
            if not self.requested.is_set():
                self.terminate()
            # A timed-out collector retains ownership for safe deferred cleanup.
            return
        self._dispose()
