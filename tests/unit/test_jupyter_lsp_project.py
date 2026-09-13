import json
from importlib import import_module, util
from pathlib import Path

import pytest


def api():
    assert util.find_spec('onec_runtime_jupyter.lsp_project'), 'project config codec missing'
    return import_module('onec_runtime_jupyter.lsp_project')


def test_project_config_roundtrip_is_configuration_only(tmp_path):
    m = api()
    root = tmp_path.resolve()
    config = m.ProjectConfig('1' * 32, str(root))
    encoded = m.encode_config(config, m.ProjectLimits())
    assert encoded == {'installation_id': '1' * 32, 'source_root': str(root)}
    assert m.decode_config(encoded, m.ProjectLimits()) == config
    assert not ({'active_generation', 'retained_generations', 'operation_generation',
                 'runtime_id', 'sequence', 'snapshot', 'source', 'Worker'} & set(encoded))


@pytest.mark.parametrize('mutation', [
    lambda value: value.update(active_generation={}),
    lambda value: value.update(installation_id='not-a-uuid'),
    lambda value: value.update(installation_id='A' * 32),
    lambda value: value.update(source_root='relative/project'),
    lambda value: value.update(source_root='C:\\project\nsecret'),
    lambda value: value.update(source_root=1),
])
def test_project_config_rejects_unknown_or_invalid_fields(tmp_path, mutation):
    m = api()
    encoded = {'installation_id': '1' * 32, 'source_root': str(tmp_path.resolve())}
    mutation(encoded)
    with pytest.raises(ValueError, match='^invalid-project-config$'):
        m.decode_config(encoded, m.ProjectLimits())


@pytest.mark.parametrize('changes', [
    {'max_message_bytes': 0}, {'max_message_bytes': True},
    {'max_comms': 0}, {'max_comms': 1.5},
    {'max_path_chars': 0}, {'max_path_chars': True},
])
def test_project_limits_require_positive_integer_bounds(changes):
    m = api()
    with pytest.raises(ValueError, match='^invalid-project-limits$'):
        m.ProjectLimits(**changes)


def test_project_config_rejects_path_and_message_oversize(tmp_path):
    m = api()
    root = str(tmp_path.resolve())
    config = m.ProjectConfig('1' * 32, root)
    with pytest.raises(ValueError, match='^project-path-limit$'):
        m.encode_config(config, m.ProjectLimits(max_path_chars=len(root) - 1))
    encoded_size = len(json.dumps(
        {'installation_id': '1' * 32, 'source_root': root},
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8'))
    with pytest.raises(ValueError, match='^project-message-limit$'):
        m.encode_config(config, m.ProjectLimits(max_message_bytes=encoded_size - 1))


@pytest.mark.parametrize('config, reason', [
    ({'installation_id': '1' * 32, 'source_root': None}, None),
    (None, 'runtime-unavailable'),
])
def test_project_envelope_enforces_complete_utf8_message_boundary(config, reason):
    m = api()
    epoch = 9007199254740991
    envelope = {
        'version': 2, 'bridge_epoch': epoch, 'config': config, 'reason': reason,
    }
    size = len(json.dumps(
        envelope, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8'))
    limits = m.ProjectLimits(max_message_bytes=size)
    project = None if config is None else m.ProjectConfig('1' * 32, None)
    assert m.encode_envelope(epoch, project, reason, limits) == envelope
    assert m.decode_envelope(envelope, limits) == (epoch, project, reason)
    too_small = m.ProjectLimits(max_message_bytes=size - 1)
    with pytest.raises(ValueError, match='^project-message-limit$'):
        m.encode_envelope(epoch, project, reason, too_small)
    with pytest.raises(ValueError, match='^project-message-limit$'):
        m.decode_envelope(envelope, too_small)


@pytest.mark.parametrize('epoch', [-1, 2**53, True])
def test_project_envelope_rejects_non_json_safe_epoch(epoch):
    m = api()
    envelope = {
        'version': 2, 'bridge_epoch': epoch, 'config': None,
        'reason': 'runtime-unavailable',
    }
    with pytest.raises(ValueError, match='^invalid-project-envelope$'):
        m.decode_envelope(envelope)


@pytest.mark.parametrize('payload', [
    None, [], {'installation_id': '1' * 32},
    {'installation_id': '1' * 32, 'source_root': None, 'version': 2},
])
def test_project_config_rejects_wrong_shape_or_embedded_version(payload):
    m = api()
    with pytest.raises(ValueError, match='^invalid-project-config$'):
        m.decode_config(payload, m.ProjectLimits())


def test_project_config_is_immutable():
    m = api()
    config = m.ProjectConfig('1' * 32, None)
    with pytest.raises((AttributeError, TypeError)):
        config.source_root = str(Path.cwd())
