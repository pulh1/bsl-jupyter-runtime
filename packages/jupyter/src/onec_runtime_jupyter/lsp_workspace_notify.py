"""Coarse local Windows change signal; the owner serializes wait/rearm/close."""
from pathlib import Path
import ctypes
from ctypes import wintypes
import os


class WindowsChangeSignal:
    def __init__(self, root):
        if os.name != 'nt':
            raise OSError('workspace-notification-unsupported')
        # Admission checks all ancestors before passing the root to the native API.
        from .lsp_workspace import root_identity
        root_identity(root)
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'FindFirstChangeNotificationW': ([wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'FindNextChangeNotification': ([wintypes.HANDLE], wintypes.BOOL),
            'FindCloseChangeNotification': ([wintypes.HANDLE], wintypes.BOOL),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        path = '\\\\?\\' + str(Path(root))
        self.handle = self.api.FindFirstChangeNotificationW(path, True, 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x100)
        if self.handle in (None, ctypes.c_void_p(-1).value):
            self.handle = None
            raise OSError('workspace-notification-unavailable')
        self.signaled = False

    def wait(self, timeout_ms=200):
        if self.handle is None or self.signaled:
            raise OSError('workspace-notification-state')
        result = self.api.WaitForSingleObject(self.handle, timeout_ms)
        if result == 0:
            self.signaled = True
            return True
        if result == 258:
            return False
        raise OSError('workspace-notification-wait-failed')

    def rearm(self):
        if self.handle is None or not self.signaled:
            raise OSError('workspace-notification-state')
        if not self.api.FindNextChangeNotification(self.handle):
            raise OSError('workspace-notification-rearm-failed')
        self.signaled = False

    def close(self):
        if self.handle is not None:
            if not self.api.FindCloseChangeNotification(self.handle):
                raise OSError('workspace-notification-close-failed')
            self.handle = None
