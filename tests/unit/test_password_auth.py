from dataclasses import asdict, replace
from pathlib import Path
import subprocess
import traceback

import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import ProcessStartError
from onec_runtime.processes import FileModeProcesses, debuggee_command
from onec_runtime import toolchain


SECRET = '  пароль "quoted" & /P tail  '


@pytest.fixture
def config(tmp_path):
    platform = tmp_path / 'bin'
    platform.mkdir()
    for name in ('1cv8.exe', '1cv8c.exe', 'dbgs.exe'):
        (platform / name).touch()
    return RuntimeConfig(tmp_path, platform)


@pytest.mark.parametrize('thick', [False, True])
def test_client_passes_password_as_one_unchanged_argument(config, thick):
    secured = replace(config, username='Test user', password=SECRET)
    command = debuggee_command(secured, 1550, thick_client=thick)
    assert command[command.index('/P') + 1] == SECRET
    assert command[command.index('/N') + 1] == 'Test user'
    assert SECRET not in repr(secured)


@pytest.mark.parametrize('name,arity', [
    ('dump_target_extension_files_command', 2),
    ('dump_target_extension_cfe_command', 2),
    ('load_target_extension_cfe_command', 2),
    ('apply_product_extension_command', 1),
    ('load_target_extension_source_command', 1),
    ('deploy_extension_command', 0),
    ('update_extension_command', 0),
    ('apply_extension_command', 0),
    ('apply_target_extension_source_command', 1),
])
def test_target_designer_commands_use_password(config, name, arity):
    secured = replace(config, password=SECRET)
    args = [config.workspace / f'path{i}' for i in range(arity)]
    command = getattr(toolchain, name)(secured, *args)
    assert command[command.index('/P') + 1] == SECRET


def test_empty_password_is_backwards_compatible(config):
    assert debuggee_command(config, 1550)[-1] == ''
    command = toolchain.deploy_extension_command(config)
    assert command[command.index('/P') + 1] == ''


def test_build_database_does_not_receive_target_credentials(config):
    secured = replace(config, username='Target user', password=SECRET)
    for command in (
        toolchain.load_extension_source_command(secured, config.workspace),
        toolchain.dump_extension_command(secured, config.workspace / 'extension.cfe'),
        toolchain.build_external_processor_command(secured),
    ):
        assert SECRET not in command
        assert 'Target user' not in command


def test_tool_executes_raw_password_but_returns_redacted_result(config, monkeypatch):
    command = ['1cv8.exe', 'DESIGNER', '/P', SECRET]
    def run(actual, **kwargs):
        assert actual == command
        assert kwargs['shell'] is False
        return subprocess.CompletedProcess(actual, 0)
    monkeypatch.setattr(toolchain.subprocess, 'run', run)
    result = toolchain.run_tool_command(command, config.workspace / 'tool.log')
    assert result.returncode == 0
    assert result.command[-1] == '<redacted>'
    assert SECRET not in repr(asdict(result))
    assert command[-1] == SECRET


@pytest.mark.parametrize('runner', ['tool', 'client'])
def test_launch_error_does_not_expose_password(config, monkeypatch, runner):
    command = ['1cv8.exe', '/P', SECRET]
    def fail(*args, **kwargs):
        raise OSError(f'cannot start {SECRET}')
    if runner == 'tool':
        monkeypatch.setattr(toolchain.subprocess, 'run', fail)
        launch = lambda: toolchain.run_tool_command(command, config.workspace / 'tool.log')
    else:
        monkeypatch.setattr('onec_runtime.processes.subprocess.Popen', fail)
        launch = lambda: FileModeProcesses(config)._spawn(command, 'test')
    with pytest.raises(ProcessStartError) as caught:
        launch()
    rendered = ''.join(traceback.format_exception(caught.value))
    assert SECRET not in rendered


def test_tool_result_masks_password_even_when_serialized(config):
    result = toolchain.ToolResult(('1cv8.exe', '/P', SECRET), 0, config.workspace)
    assert asdict(result)['command'] == ('1cv8.exe', '/P', '<redacted>')


def test_owned_process_repr_does_not_include_launch_arguments(config):
    from onec_runtime.processes import OwnedProcess

    class ProcessWithArguments:
        def __repr__(self):
            return f'Popen(args={SECRET!r})'

    owned = OwnedProcess(ProcessWithArguments(), config.workspace, config.workspace, ())
    assert SECRET not in repr(owned)
    assert 'пароль' not in repr(owned)


def test_username_looking_like_password_switch_does_not_defeat_redaction(config):
    result = toolchain.ToolResult(
        ('1cv8.exe', '/N', '/P', '/P', SECRET), 0, config.workspace
    )
    assert result.command == ('1cv8.exe', '/N', '/P', '/P', '<redacted>')
    assert SECRET not in repr(asdict(result))


def test_cleanup_timeout_does_not_expose_password(config):
    from onec_runtime.errors import TargetLost
    from onec_runtime.processes import OwnedProcess

    class StuckProcess:
        pid = 123
        def poll(self):
            return None
        def terminate(self):
            pass
        def kill(self):
            pass
        def wait(self, timeout):
            raise subprocess.TimeoutExpired(['1cv8.exe', '/P', SECRET], timeout)

    owned = OwnedProcess(StuckProcess(), config.workspace, config.workspace, ())
    with pytest.raises(TargetLost) as caught:
        owned.close(timeout_s=0)
    assert SECRET not in ''.join(traceback.format_exception(caught.value))
