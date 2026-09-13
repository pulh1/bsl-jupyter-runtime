"""Windows test-only accounting, excluding exactly the OS message resource.

No name queries on arbitrary objects: only FILE_TYPE_DISK handles are queried
for file identity. Unknown/unqueryable handles remain counted (fail closed).
"""
import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass
from pathlib import Path


class _Entry(c.Structure):
    _fields_ = [('handle', c.c_size_t), ('count', c.c_size_t), ('pointers', c.c_size_t),
        ('access', w.DWORD), ('type', w.DWORD), ('flags', w.DWORD), ('reserved', w.DWORD)]


class _FileId(c.Structure):
    _fields_ = [('volume', c.c_uint64), ('identity', c.c_ubyte * 16)]


@dataclass(frozen=True)
class HandleSample:
    total: int
    excluded: int

    @property
    def counted(self):
        return self.total - self.excluded


class WindowsOwnedHandles:
    def __init__(self):
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'GetSystemDirectoryW': ([w.LPWSTR, w.UINT], w.UINT),
            'GetSystemDefaultUILanguage': ([], w.WORD),
            'LCIDToLocaleName': ([w.DWORD, w.LPWSTR, c.c_int, w.DWORD], c.c_int),
            'CreateFileW': ([w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            'CreateEventW': ([c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
            'CloseHandle': ([w.HANDLE], w.BOOL),
            'GetFileType': ([w.HANDLE], w.DWORD),
            'GetFileInformationByHandleEx': ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.query = c.WinDLL('ntdll').NtQueryInformationProcess
        self.query.argtypes = [w.HANDLE, w.ULONG, c.c_void_p, w.ULONG, c.POINTER(w.ULONG)]
        self.query.restype = w.LONG
        directory, locale = c.create_unicode_buffer(32768), c.create_unicode_buffer(85)
        assert 0 < self.kernel.GetSystemDirectoryW(directory, len(directory)) < len(directory)
        assert self.kernel.LCIDToLocaleName(self.kernel.GetSystemDefaultUILanguage(), locale, len(locale), 0)
        self.resource_path = (Path(directory.value) / locale.value / 'kernel32.dll.mui').resolve(strict=True)
        # Open attributes only, permit normal OS sharing, and close before any
        # measurement. No read/write of Windows resources is performed.
        handle = self.kernel.CreateFileW(str(self.resource_path), 0x80, 7, None, 3, 0, None)
        assert handle not in (None, c.c_void_p(-1).value), 'system resource identity unavailable'
        try:
            self._resource_identity = self._disk_identity(handle)
            assert self._resource_identity is not None, 'system resource identity unavailable'
        finally:
            self.kernel.CloseHandle(handle)

    def _disk_identity(self, handle):
        if self.kernel.GetFileType(handle) != 1:  # FILE_TYPE_DISK; never query pipes/devices.
            return None
        info = _FileId()
        if not self.kernel.GetFileInformationByHandleEx(handle, 18, c.byref(info), c.sizeof(info)):
            return None
        return info.volume, bytes(info.identity)

    def sample(self):
        # Bounded snapshot of this test process only; fail instead of truncating.
        buffer = c.create_string_buffer(262144)
        status = self.query(c.c_void_p(-1), 51, buffer, len(buffer), None)
        assert status == 0, 'process handle snapshot unavailable'
        count = c.c_size_t.from_buffer(buffer).value
        assert 2 * c.sizeof(c.c_size_t) + count * c.sizeof(_Entry) <= len(buffer)
        entries = (_Entry * count).from_buffer(buffer, 2 * c.sizeof(c.c_size_t))
        excluded = sum(self._disk_identity(entry.handle) == self._resource_identity for entry in entries)
        return HandleSample(count, excluded)
