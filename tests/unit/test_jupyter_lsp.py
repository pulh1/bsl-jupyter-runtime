import io
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from onec_runtime_jupyter.lsp import language_server_spec
from onec_runtime_jupyter.lsp_proxy import read_message, write_message


def test_extension_registers_project_bridge_before_runtime_install(monkeypatch):
    from onec_runtime_jupyter import lsp_kernel
    from onec_runtime_jupyter.extension import load_ipython_extension

    calls = []
    shell = SimpleNamespace(user_ns={}, register_magics=lambda _magics: None)
    monkeypatch.setattr(
        lsp_kernel, 'install_project_bridge',
        lambda target, runtime: calls.append((target, runtime)),
    )
    load_ipython_extension(shell)
    assert calls == [(shell, None)]


def test_discovery_uses_executable_only_and_never_discloses_or_discovers_root(tmp_path, monkeypatch):
    manager = SimpleNamespace(log=logging.getLogger(__name__))
    monkeypatch.setenv('ONEC_BSL_LANGUAGE_SERVER', sys.executable)
    monkeypatch.delenv('ONEC_BSL_SOURCE_ROOT', raising=False)
    first = language_server_spec(manager)
    assert 'onec-bsl' in first
    monkeypatch.setenv('ONEC_BSL_SOURCE_ROOT', str(tmp_path))
    assert language_server_spec(manager) == first
    spec = first['onec-bsl']
    assert spec['requires_documents_on_disk'] is False
    assert '--source-root' not in spec['argv'] and str(tmp_path) not in str(spec)
    monkeypatch.setenv('ONEC_BSL_LANGUAGE_SERVER', str(tmp_path / 'missing'))
    assert language_server_spec(manager) == {}


def test_stdio_fallback_without_private_control_fails_closed(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith('ONEC_BSL_CONTROL_')}
    env['ONEC_BSL_SOURCE_ROOT'] = str(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'onec_runtime_jupyter.lsp_proxy', '--', sys.executable],
                            env=env, input=b'', capture_output=True, timeout=10)
    assert result.returncode != 0
    assert result.stderr.strip() == b'private-control-unavailable'
    assert list(tmp_path.iterdir()) == []


def test_lsp_framing_counts_utf8_bytes_and_reads_multiple_messages():
    stream = io.BytesIO()
    message = {'jsonrpc': '2.0', 'method': 'test', 'params': 'Привет 😀'}
    write_message(stream, message); write_message(stream, {'id': 2, 'result': None})
    stream.seek(0)
    assert read_message(stream) == message
    assert read_message(stream) == {'id': 2, 'result': None}
    assert read_message(stream) is None


def test_lsp_framing_rejects_truncated_payload():
    with pytest.raises(EOFError):
        read_message(io.BytesIO(b'Content-Length: 100\r\n\r\n{}'))
def test_stdio_dispatch_applies_backpressure_before_creating_unbounded_tasks(monkeypatch):
    import asyncio
    from onec_runtime_jupyter import lsp_proxy, lsp_gateway
    active = 0; maximum = 0
    class SlowGateway:
        closed = False
        def __init__(self, *args, **kwargs): pass
        async def accept_contexts(self, contexts): pass
        async def cleanup_idle(self): pass
        async def _error(self, *args): pass
        async def close(self): self.closed = True
        async def handle(self, message):
            nonlocal active, maximum
            active += 1; maximum = max(maximum, active)
            await asyncio.sleep(.03)
            active -= 1
    class Control:
        def request(self, **kwargs): return {'contexts': [], 'revision': 1}
        def close(self): pass
    stream = io.BytesIO()
    for i in range(10): write_message(stream, {'id': i, 'method': 'initialize', 'params': {}})
    stream.seek(0)
    monkeypatch.setattr(lsp_proxy, 'DEFAULT_MAX_INFLIGHT_MESSAGES', 2, raising=False)
    monkeypatch.setattr(lsp_gateway, 'Gateway', SlowGateway)
    monkeypatch.setattr(lsp_proxy.sys, 'stdin', SimpleNamespace(buffer=stream))
    asyncio.run(lsp_proxy.run([], Control()))
    assert 1 <= maximum <= 2
