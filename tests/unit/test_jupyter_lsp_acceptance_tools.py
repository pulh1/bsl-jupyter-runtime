"""Acceptance must detect writes independently of the tree it observes."""
from importlib import import_module
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def script_imports(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))


@pytest.mark.parametrize('mutation', ['extra-file', 'changed-content', 'extra-directory'])
def test_write_oracle_rejects_unplanned_mutation_after_planned_save(tmp_path, mutation):
    module = tmp_path / 'Module.bsl'
    module.write_bytes(b'original\r\n')
    other = tmp_path / 'Other.bsl'
    other.write_bytes(b'unchanged\r\n')
    api = import_module('tools.check_jupyter_bsl_lsp')
    assert hasattr(api, 'SourceWriteOracle'), 'independent source write oracle is missing'
    oracle = api.SourceWriteOracle(tmp_path)
    oracle.save(module, b'planned\r\n')
    if mutation == 'extra-file':
        (tmp_path / 'unexpected.bsl').write_bytes(b'unexpected')
    elif mutation == 'changed-content':
        other.write_bytes(b'corrupted')
    else:
        (tmp_path / 'unexpected-directory').mkdir()
    with pytest.raises(AssertionError, match='unplanned source tree change'):
        oracle.check()


def test_write_oracle_planned_rename_create_delete_preserves_exact_bytes(tmp_path):
    module = tmp_path / 'Module.bsl'
    module.write_bytes(b'original\r\n')
    api = import_module('tools.check_jupyter_bsl_lsp')
    assert hasattr(api, 'SourceWriteOracle'), 'independent source write oracle is missing'
    oracle = api.SourceWriteOracle(tmp_path)
    oracle.save(module, b'changed\r\n', atomic=True)
    target = tmp_path / 'Renamed.bsl'
    oracle.rename(module, target)
    oracle.check()
    assert target.read_bytes() == b'changed\r\n'
    oracle.delete(target)
    oracle.save(module, b'original\r\n')
    oracle.check()
    assert module.read_bytes() == b'original\r\n'


def test_browser_reload_code_uses_public_generations_and_releases_handles(tmp_path):
    from tools.lsp_public_fixture import (
        LspFailureTarget, make_lsp_session, reload_code,
        worker_module_source_unit,
    )
    source_root = tmp_path / 'project'
    modules = source_root / 'CommonModules'
    modules.mkdir(parents=True)
    (modules / 'JupyterBslFixtureCalleeServer.xml').write_text(
        '<MetaDataObject><CommonModule><Properties>'
        '<Name>JupyterBslFixtureCalleeServer</Name><Global>false</Global>'
        '<Server>true</Server><ClientManagedApplication>false</ClientManagedApplication>'
        '<ClientOrdinaryApplication>false</ClientOrdinaryApplication>'
        '</Properties></CommonModule></MetaDataObject>', encoding='utf-8',
    )
    target = LspFailureTarget()
    session = make_lsp_session(tmp_path / 'work', source_root, target)
    namespace = {
        '_lsp_runtime': session, '_lsp_target': target,
        '_worker_module_source_unit': worker_module_source_unit,
    }
    try:
        exec(reload_code('InMemoryA', 1), namespace)
        first = session.status().worker_generation
        assert first is not None
        exec(reload_code('InMemoryB', 2), namespace)
        second = session.status().worker_generation
        assert second is not first
        exec(reload_code('Failed', 3, failure='swap_guard'), namespace)
        assert session.status().worker_generation is second
        assert target.failure is None
        assert session.runtime_api.confirmed_worker_module_units(second)
    finally:
        session.close()


@pytest.mark.parametrize('layout', ['designer', 'edt-parent', 'edt-src'])
def test_browser_owned_source_copies_preserve_configured_topology_without_siblings(tmp_path, layout):
    pytest.importorskip('playwright.sync_api')
    browser = import_module('tools.check_jupyter_lsp_runtime')
    assert hasattr(browser, 'copy_source_fixture'), 'owned copies must preserve the configured root basename'
    fixture = tmp_path / 'input' / 'export'
    if layout == 'designer':
        source = fixture
        relative = Path('CommonModules/Probe/Ext/Module.bsl')
    else:
        source = fixture / 'src' if layout == 'edt-src' else fixture
        relative = Path('CommonModules/Probe/Module.bsl' if layout == 'edt-src'
                        else 'src/CommonModules/Probe/Module.bsl')
    (source / relative).parent.mkdir(parents=True)
    (source / relative).write_bytes(b'exact\r\nbytes\r\n')
    sibling = source.parent / 'not-selected-secret.bsl'
    sibling.write_bytes(b'not copied')
    snapshots = import_module('tools.check_jupyter_bsl_lsp')
    initial = snapshots.fingerprints(source.parent)
    expected = snapshots.fingerprints(source)
    first = browser.copy_source_fixture(source, tmp_path / 'owned-a')
    second = browser.copy_source_fixture(first, tmp_path / 'owned-b')
    assert first.name == second.name == source.name
    assert len({source.resolve(), first.resolve(), second.resolve()}) == 3
    assert snapshots.fingerprints(first) == snapshots.fingerprints(second) == expected
    assert snapshots.fingerprints(source.parent) == initial
    assert not (first.parent / sibling.name).exists() and not (second.parent / sibling.name).exists()
    assert set(first.parent.iterdir()) == {first} and set(second.parent.iterdir()) == {second}
    with pytest.raises(FileExistsError):
        browser.copy_source_fixture(source, first.parent)
    assert snapshots.fingerprints(first) == expected


def test_hover_failure_summary_is_bounded_and_contains_only_request_classes():
    pytest.importorskip('playwright.sync_api')
    browser = import_module('tools.check_jupyter_lsp_runtime')
    assert hasattr(browser, 'hover_failure_summary')
    frames = []
    for request_id, response in [('private-id-1', {'result': None}),
                                 ('private-id-2', {'result': {'contents': 'PRIVATE SOURCE'}}),
                                 ('private-id-3', {'error': {'code': -32001, 'message': 'PRIVATE PATH'}})]:
        frames.extend([
            {'direction': 'client', 'message': {'id': request_id, 'method': 'textDocument/hover',
                                               'params': {'textDocument': {'uri': 'PRIVATE URI'}}}},
            {'direction': 'server', 'message': {'id': request_id, **response}}])
    summary = browser.hover_failure_summary(frames, 4)
    assert [item.get('response') for item in summary if item['kind'] == 'response'] == ['null', 'nonempty', 'error']
    assert summary[-1]['error_code'] == -32001 and summary[-1]['phase'] == 'attempt'
    assert summary[0]['phase'] == 'before-attempt'
    assert [item['request'] for item in summary] == [0, 0, 1, 1, 2, 2]
    assert 'PRIVATE' not in str(summary) and 'private-id' not in str(summary)
    assert len(browser.hover_failure_summary(frames * 1000, 4000)) <= 64


def diagnostic_window():
    pytest.importorskip('playwright.sync_api')
    api = import_module('tools.check_jupyter_lsp_browser')
    assert hasattr(api, 'DiagnosticWindow'), 'current-anchored diagnostic isolation is missing'
    return api.DiagnosticWindow()


def diagnostic_wire(uri='file:///notebook.bsl', version=25, *, empty=False, padding=''):
    import json
    return json.dumps({'method': 'textDocument/publishDiagnostics', 'params': {
        'uri': uri, 'version': version, 'diagnostics': [] if empty else [
            {'message': 'current' + padding, 'code': 'Fixture', 'severity': 2,
             'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 1}}}]}}, ensure_ascii=False)


class DiagnosticSocket:
    def __init__(self, deliveries, label):
        self.deliveries, self.label = deliveries, label

    def send(self, raw):
        self.deliveries.append((self.label, raw))


def diagnostic_version(window, socket, uri='file:///notebook.bsl', version=25):
    window.client(socket, {'method': 'textDocument/didChange', 'params': {
        'textDocument': {'uri': uri, 'version': version}}})


def test_diagnostic_window_anchors_exact_socket_document_and_current_version_then_drains_once():
    window = diagnostic_window()
    deliveries = []
    a, b = DiagnosticSocket(deliveries, 'a'), DiagnosticSocket(deliveries, 'b')
    diagnostic_version(window, a)
    diagnostic_version(window, b)
    window.begin(a, 'file:///notebook.bsl')
    originals = [(b, diagnostic_wire()), (a, diagnostic_wire('file:///other.bsl')),
                 (a, diagnostic_wire(version=20)), (a, diagnostic_wire(empty=True)), (a, diagnostic_wire())]
    for socket, raw in originals:
        assert window.server(socket, raw)
    anchor = window.anchor()
    assert anchor['version'] == 25 and anchor['uri'] == 'file:///notebook.bsl'
    assert deliveries == [('a', originals[-1][1])]
    assert window.anchor() == anchor and len(deliveries) == 1
    window.finish()
    assert deliveries[1:] == [(socket.label, raw) for socket, raw in originals[:-1]]
    assert window.queued_count == window.queued_bytes == 0
    window.finish()
    assert len(deliveries) == len(originals)


def test_diagnostic_window_uses_already_delivered_anchor_without_replaying_it():
    window = diagnostic_window()
    deliveries = []
    socket = DiagnosticSocket(deliveries, 'a')
    diagnostic_version(window, socket)
    raw = diagnostic_wire()
    assert not window.server(socket, raw)
    socket.send(raw)
    window.begin(socket, 'file:///notebook.bsl')
    assert window.anchor()['version'] == 25
    window.finish()
    assert deliveries == [('a', raw)]


def test_diagnostic_window_absent_anchor_and_version_change_fail_without_losing_queued_messages():
    window = diagnostic_window()
    deliveries = []
    socket = DiagnosticSocket(deliveries, 'a')
    diagnostic_version(window, socket)
    window.begin(socket, 'file:///notebook.bsl')
    raw = diagnostic_wire(version=20)
    assert window.server(socket, raw)
    assert window.anchor() is None
    diagnostic_version(window, socket, version=26)
    with pytest.raises(AssertionError, match='document-version-changed'):
        window.check()
    window.finish()
    assert deliveries == [('a', raw)]


@pytest.mark.parametrize('boundary', ['count', 'bytes'])
def test_diagnostic_window_queue_bounds_include_empty_messages_and_utf8_wire_bytes(boundary):
    window = diagnostic_window()
    deliveries = []
    socket = DiagnosticSocket(deliveries, 'a')
    diagnostic_version(window, socket)
    window.begin(socket, 'file:///notebook.bsl')
    raw = diagnostic_wire(empty=True)
    if boundary == 'count':
        for _ in range(64):
            assert window.server(socket, raw)
        assert window.queued_count == 64
    else:
        raw = diagnostic_wire(padding='Ж' * 200000)
        while window.queued_bytes + len(raw.encode('utf-8')) <= 2 * 1024 * 1024:
            assert window.server(socket, raw)
    before = window.queued_count
    with pytest.raises(AssertionError, match='diagnostic-queue-limit'):
        window.server(socket, raw)
    window.finish()
    assert len(deliveries) == before and window.queued_bytes == 0


@pytest.mark.parametrize('invalid', [None, True, '25'])
def test_diagnostic_window_rejects_malformed_anchor_version(invalid):
    window = diagnostic_window()
    deliveries = []
    socket = DiagnosticSocket(deliveries, 'a')
    diagnostic_version(window, socket)
    window.begin(socket, 'file:///notebook.bsl')
    with pytest.raises(AssertionError, match='diagnostic-message-invalid'):
        window.server(socket, diagnostic_wire(version=invalid))
    window.finish()


def test_diagnostic_window_finally_drains_real_messages_on_failed_transient_assertion():
    window = diagnostic_window()
    deliveries = []
    socket = DiagnosticSocket(deliveries, 'a')
    diagnostic_version(window, socket)
    window.begin(socket, 'file:///notebook.bsl')
    raw = diagnostic_wire(empty=True)
    with pytest.raises(AssertionError, match='transient-failed'):
        try:
            assert window.server(socket, raw)
            raise AssertionError('transient-failed')
        finally:
            window.finish()
    assert deliveries == [('a', raw)] and window.queued_count == 0


def test_diagnostic_window_identity_comes_from_the_actual_held_message_not_a_context_response():
    window = diagnostic_window()
    assert hasattr(window, 'begin_held'), 'public context replies do not supply document_uri'
    deliveries = []
    socket = DiagnosticSocket(deliveries, 'a')
    diagnostic_version(window, socket, uri='file:///actual-notebook.bsl')
    original = ('diagnostics', socket, diagnostic_wire('file:///actual-notebook.bsl', 20))
    window.begin_held([('completion', socket, '{"id":17,"result":[]}'), original])
    assert window.target == (socket, 'file:///actual-notebook.bsl', 25)
    window.finish()
    for held in ([], [original, original]):
        with pytest.raises(AssertionError, match='held-diagnostic-identity'):
            window.begin_held(held)
