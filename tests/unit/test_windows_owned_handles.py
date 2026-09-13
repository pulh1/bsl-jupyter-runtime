import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows handle accounting')


@pytest.mark.parametrize('kind', ['pipe', 'event', 'file', 'same-basename'])
def test_non_exempt_owned_handle_leaks_remain_visible(tmp_path, kind):
    from windows_owned_handles import WindowsOwnedHandles
    counter = WindowsOwnedHandles()
    before = counter.sample()
    if kind == 'pipe':
        from multiprocessing import Pipe
        handles = Pipe()
        close = lambda: [handle.close() for handle in handles]
        count = 2
    elif kind == 'event':
        handle = counter.kernel.CreateEventW(None, True, False, None)
        assert handle
        close = lambda: counter.kernel.CloseHandle(handle)
        count = 1
    else:
        path = tmp_path / ('kernel32.dll.mui' if kind == 'same-basename' else 'owned.txt')
        handle = path.open('wb')
        close = handle.close
        count = 1
    try:
        assert counter.sample().counted == before.counted + count
    finally:
        close()
    assert counter.sample().counted == before.counted


def test_only_canonical_system_message_resource_identity_is_excluded():
    from windows_owned_handles import WindowsOwnedHandles
    counter = WindowsOwnedHandles()
    before = counter.sample()
    with Path(counter.resource_path).open('rb'):
        after = counter.sample()
        assert after.total == before.total + 1
        assert after.excluded == before.excluded + 1
        assert after.counted == before.counted
