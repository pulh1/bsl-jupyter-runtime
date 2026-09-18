"""The public runtime config is the sole project-root authority."""
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from onec_runtime.errors import ProtocolError
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from test_extension_session import _Closeable, _IdleRdbg


@pytest.mark.parametrize('ancestor', [False, True])
@pytest.mark.parametrize('kind', ['symlink', 'junction'])
def test_config_rejects_link_before_it_can_become_public(tmp_path, ancestor, kind):
    target = tmp_path / 'target'
    (target / 'project').mkdir(parents=True)
    link = tmp_path / 'owned-link'
    if kind == 'junction':
        if os.name != 'nt':
            pytest.skip('Windows junction regression')
        # Both paths are owned by this test; no shared probe or recursive cleanup.
        result = subprocess.run(['powershell', '-NoProfile', '-Command',
            'New-Item -ItemType Junction -Path $env:ONEC_TEST_LINK -Target $env:ONEC_TEST_TARGET | Out-Null'],
            env={**os.environ, 'ONEC_TEST_LINK': str(link), 'ONEC_TEST_TARGET': str(target)},
            capture_output=True)
        assert result.returncode == 0
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            if getattr(error, 'winerror', None) == 1314:
                pytest.skip('Windows symlink privilege unavailable')
            raise
    supplied = link / 'project' if ancestor else link
    try:
        with pytest.raises(ProtocolError, match='^common-module source root is unsafe$'):
            RuntimeSessionConfig(None, tmp_path / 'evidence', source_root=supplied)
    finally:
        if kind == 'junction':
            os.rmdir(link)  # Remove only this owned junction, never its target.
        else:
            link.unlink()
    assert (target / 'project').is_dir()


@pytest.mark.parametrize('layout', [None, 'Designer', 'EDT', 'EDT/src'])
def test_normal_config_root_remains_public_after_session_close(tmp_path, layout):
    source = tmp_path / layout if layout else None
    if source:
        ((source / 'src' if layout == 'EDT' else source) / 'CommonModules').mkdir(parents=True)
    config = RuntimeSessionConfig(SimpleNamespace(is_server_infobase=False), tmp_path / 'evidence', source_root=source)
    runtime = RuntimeSession(config, _Closeable(), _Closeable(), _IdleRdbg(), _Closeable(), SimpleNamespace())
    assert runtime.config is config
    assert runtime.config.source_root == source
    runtime.close()
    assert runtime.config is config
    assert runtime.config.source_root == source
