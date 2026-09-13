"""Test-owned exact native process receipts; no production inspection seam."""
import ctypes
from ctypes import wintypes as w


class NativeReceipt:
    def __init__(self, pid):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'GetProcessId': ([w.HANDLE], w.DWORD),
            'GetProcessTimes': ([w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)], w.BOOL),
            'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.OpenProcess(0x100000 | 0x1000, False, pid)
        assert self.handle, 'exact test-owned process handle unavailable'
        self.pid = pid
        try:
            self.created = self.identity()
        except Exception:
            self.close()
            raise

    def identity(self):
        assert self.api.GetProcessId(self.handle) == self.pid
        times = [w.FILETIME() for _ in range(4)]
        assert self.api.GetProcessTimes(self.handle, *(ctypes.byref(value) for value in times))
        return times[0].dwHighDateTime, times[0].dwLowDateTime

    def assert_signaled(self):
        assert self.identity() == self.created
        assert self.api.WaitForSingleObject(self.handle, 0) == 0, 'owned process nonsignaled immediately after close'

    def close(self):
        if self.handle:
            assert self.api.CloseHandle(self.handle)
            self.handle = None
