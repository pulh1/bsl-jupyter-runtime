"""Fail-closed index admission and the complete untimed-setup/timed-load loop."""
import asyncio
import json
from importlib import import_module, util
from pathlib import Path
import sys

import pytest


@pytest.fixture(autouse=True)
def script_imports(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))


def api():
    assert util.find_spec('tools.check_jupyter_lsp_zup'), 'source-based live acceptance tool is missing'
    return import_module('tools.check_jupyter_lsp_zup')


def event(probe, token, kind, **fields):
    probe.observe('$/progress', {'token': token, 'value': {'kind': kind, **fields}})


def create(probe, token):
    probe.observe('window/workDoneProgress/create', {'token': token})


def initialized(probe, version='1.0.7'):
    probe.observe('initialize-result', {'serverInfo': {'name': 'BSL Language Server', 'version': version}})


def find(probe, language='en'):
    create(probe, 'find')
    event(probe, 'find', 'begin', title=('Finding files to populate context...' if language == 'en'
                                       else 'Ищем файлы для наполнения контекста...'))
    event(probe, 'find', 'end', message='')


def populate(probe, language='en', end=True):
    create(probe, 'population')
    event(probe, 'population', 'begin', title=('Populating context...' if language == 'en' else 'Наполняем контекст...'))
    # Native reports can be out of order and tick before the actual work.
    event(probe, 'population', 'report', percentage=100, message='2/2 files')
    event(probe, 'population', 'report', percentage=50, message='1/2 files')
    if end:
        event(probe, 'population', 'end', message=('Context populated.' if language == 'en'
                                                  else 'Наполнение контекста завершено.'))


@pytest.mark.parametrize('language', ['en', 'ru'])
def test_only_matched_initial_population_end_admits_same_identity(language):
    m = api()
    now = [1.0]
    probe = m.InitialPopulation(lambda: ('same-child', 'normalized-root', 0), clock=lambda: now[0])
    initialized(probe)
    find(probe, language)
    now[0] = 3.0
    populate(probe, language)
    evidence = probe.admit()
    assert evidence['initial_population_ms'] == 2000
    assert evidence['created_tokens'] == 2 and evidence['reports'] == 2
    assert evidence['initialization']['version'] == '1.0.7'
    assert evidence['locale'] == language


@pytest.mark.parametrize('bad', [
    'foreign-end', 'unrelated-end', 'percentage-only', 'missing-find', 'unknown-locale',
    'wrong-end-locale', 'duplicate-create', 'duplicate-begin', 'duplicate-end',
    'oversized-token', 'oversized-title', 'malformed', 'ninth-token', 'unknown-version',
    'missing-initialize', 'identity-replaced', 'timeout', 'invalid-marker',
])
def test_failed_progress_or_identity_never_admits_measurement(bad):
    m = api()
    now, state = [0.0], [('child', 'root', 0)]
    probe = m.InitialPopulation(lambda: state[0], clock=lambda: now[0], timeout=10)
    if bad != 'missing-initialize':
        initialized(probe, '2.0.0' if bad == 'unknown-version' else '1.0.7')
    if bad != 'missing-find':
        find(probe)
    if bad == 'foreign-end':
        event(probe, 'foreign', 'end', message='Context populated.')
    elif bad == 'unrelated-end':
        create(probe, 'metadata')
        event(probe, 'metadata', 'begin', title='Computing configuration metadata...')
        event(probe, 'metadata', 'end', message='Configuration metadata computing is finished.')
    elif bad == 'unknown-locale':
        create(probe, 'population')
        event(probe, 'population', 'begin', title='Unknown localized title')
    elif bad == 'wrong-end-locale':
        populate(probe, end=False)
        event(probe, 'population', 'end', message='Наполнение контекста завершено.')
    elif bad == 'oversized-token':
        create(probe, 'x' * 161)
    elif bad == 'oversized-title':
        create(probe, 'population')
        event(probe, 'population', 'begin', title='x' * 161)
    elif bad == 'malformed':
        probe.observe('$/progress', {'token': [], 'value': None})
    elif bad == 'invalid-marker':
        probe.observe('$/progress', {'invalid': True})
    elif bad == 'ninth-token':
        for i in range(8):
            create(probe, str(i))
    else:
        populate(probe, end=bad not in ('percentage-only', 'timeout'))
        if bad == 'duplicate-create': create(probe, 'population')
        if bad == 'duplicate-begin': event(probe, 'population', 'begin', title='Populating context...')
        if bad == 'duplicate-end': event(probe, 'population', 'end', message='Context populated.')
        if bad == 'identity-replaced': state[0] = ('replacement', 'root', 0)
        if bad == 'timeout': now[0] = 11.0
    with pytest.raises(m.MeasurementAdmissionError):
        probe.admit()
    assert len(probe.tokens) <= 8


def test_wait_fails_on_timeout_and_child_failure_without_population_end():
    async def run():
        m = api()
        probe = m.InitialPopulation(lambda: ('child',), timeout=.01)
        initialized(probe); find(probe); populate(probe, end=False)
        with pytest.raises(m.MeasurementAdmissionError, match='timeout'):
            await probe.wait()
        alive = [True]
        probe = m.InitialPopulation(lambda: ('child', alive[0]))
        initialized(probe); find(probe); populate(probe, end=False)
        alive[0] = False
        with pytest.raises(m.MeasurementAdmissionError, match='identity'):
            await probe.wait()
    asyncio.run(run())


@pytest.mark.parametrize('completed_at,accepted', [(9.999, True), (10.0, False), (11.0, False)])
def test_initial_population_uses_recorded_completion_deadline_not_later_check_time(completed_at, accepted):
    """A late end between checks must fail; a timely end stays valid afterward."""
    m = api()
    now = [0.0]
    probe = m.InitialPopulation(lambda: ('same-child',), clock=lambda: now[0], timeout=10)
    initialized(probe); find(probe); populate(probe, end=False)
    now[0] = 9.0
    probe.check()
    now[0] = completed_at
    event(probe, 'population', 'end', message='Context populated.')
    now[0] = 100.0
    if accepted:
        assert probe.admit()['initial_population_ms'] == pytest.approx(9999.0)
    else:
        with pytest.raises(m.MeasurementAdmissionError, match='^initial-index-timeout$'):
            probe.admit()


def test_initial_population_timeout_cannot_be_revived_by_late_end():
    m = api()
    now = [0.0]
    probe = m.InitialPopulation(lambda: ('same-child',), clock=lambda: now[0], timeout=10)
    initialized(probe); find(probe); populate(probe, end=False)
    now[0] = 10.0
    with pytest.raises(m.MeasurementAdmissionError, match='^initial-index-timeout$'):
        probe.check()
    now[0] = 11.0
    event(probe, 'population', 'end', message='Context populated.')
    with pytest.raises(m.MeasurementAdmissionError, match='^initial-index-timeout$'):
        probe.admit()


def test_full_counterbalanced_loop_releases_real_runtime_handles_outside_timer(tmp_path):
    async def run():
        from types import SimpleNamespace
        from test_runtime_api import _semantic_snapshot_runtime, _common_module_catalog, _worker_module_unit
        from onec_runtime.performance_profile import PhaseRecorder
        from onec_runtime.session import RuntimeSessionConfig
        from hashlib import sha256
        m = api()
        assert hasattr(m, 'run_pairs'), 'full off/on lifecycle loop is missing'
        catalog = _common_module_catalog('ModuleA', 'ModuleB')
        runtime = _semantic_snapshot_runtime(tmp_path, catalog)
        units = tuple(_worker_module_unit(name, 1, catalog) for name in ('ModuleA', 'ModuleB'))
        root = Path(__file__).resolve().parents[1] / 'fixtures/onec/JupyterBslTestFixture'
        module = root / 'CommonModules/JupyterBslFixtureCalleeServer/Ext/Module.bsl'
        def tree():
            return {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest() if p.is_file() else None
                    for p in root.rglob('*')}
        original = tree()
        configured = SimpleNamespace(config=RuntimeSessionConfig(None, tmp_path / 'evidence', source_root=root))
        state = {'prepared': None, 'held': None, 'loads': 0, 'releases': 0}
        schedule, closed = [], []
        checkpoints = []
        class Arm(m.LspArm):
            def __init__(self, enabled):
                super().__init__(enabled, configured, [sys.executable, '-c', FAKE_LS, module.as_uri()], 'Probe', 'DiskMethod')
            async def prepare(self):
                assert state['prepared'] is None and state['held'] is None
                state['prepared'] = self.enabled
                schedule.append(self.enabled)
                return await super().prepare()
            def check(self):
                assert state['prepared'] is self.enabled
                super().check()
            async def close(self):
                assert state['held'] is None
                state['prepared'] = None
                closed.append(self.enabled)
                return await super().close()
        def load(candidate, *, profiler):
            assert state['prepared'] in (True, False) and state['held'] is None
            assert candidate is units and isinstance(profiler, PhaseRecorder)
            state['loads'] += 1
            state['held'] = runtime.load_worker_modules(candidate, common_modules=catalog, profiler=profiler)
            return state['held']
        def release(handle):
            assert handle is state['held']
            runtime.release_worker_generation(handle)
            state['held'] = None
            state['releases'] += 1
            inventory = runtime._worker_universe._confirmed_live_inventory()
            assert len(inventory.manifest_sha256s) == 1
            assert len(inventory.artifact_identities) == 2
        try:
            results = await m.run_pairs(SimpleNamespace(load_worker_modules=load, release_worker_generation=release),
                                        units, Arm, progress=checkpoints.append)
        finally:
            runtime.close()
        assert state == {'prepared': None, 'held': None, 'loads': 46, 'releases': 46}
        assert len(results['timings'][False]) == len(results['timings'][True]) == 20
        assert schedule == [mode for pair in range(23) for mode in ((False, True) if pair % 2 == 0 else (True, False))]
        assert closed == schedule
        assert len(checkpoints) == 46 and checkpoints[-1]['pair'] == 22
        assert tree() == original
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['prepare', 'load', 'admission-after-load'])
def test_failed_arm_stops_schedule_and_releases_owned_handle(failure):
    async def run():
        from types import SimpleNamespace
        m = api()
        assert hasattr(m, 'run_pairs'), 'full off/on lifecycle loop is missing'
        closed, loaded, released = [], [], []
        class Arm:
            def __init__(self, enabled): pass
            async def prepare(self):
                if failure == 'prepare': raise m.MeasurementAdmissionError('planned-prepare')
                return {}
            def check(self):
                if failure == 'admission-after-load' and loaded: raise m.MeasurementAdmissionError('planned-degradation')
            async def close(self): closed.append(True); return {}
        def load(units, *, profiler):
            if failure == 'load': raise m.MeasurementAdmissionError('planned-load')
            loaded.append('handle'); return 'handle'
        with pytest.raises(m.MeasurementAdmissionError):
            await m.run_pairs(SimpleNamespace(load_worker_modules=load, release_worker_generation=released.append), (), Arm)
        assert len(closed) == 1
        assert released == loaded
    asyncio.run(run())


FAKE_LS = r'''
import json,sys
def read():
    line=sys.stdin.buffer.readline()
    if not line: return None
    n=int(line.split(b':')[1]);sys.stdin.buffer.readline()
    return json.loads(sys.stdin.buffer.read(n))
def write(m):
    p=json.dumps(m).encode();sys.stdout.buffer.write(b'Content-Length: '+str(len(p)).encode()+b'\r\n\r\n'+p);sys.stdout.buffer.flush()
def progress(token,kind,**fields):
    write({'method':'$/progress','params':{'token':token,'value':{'kind':kind,**fields}}})
while (m:=read()) is not None:
    method=m.get('method')
    if method=='initialize':
        assert m['params']['capabilities']['window']['workDoneProgress']
        write({'id':m['id'],'result':{'capabilities':{},'serverInfo':{'name':'BSL Language Server','version':'1.0.7'}}})
    elif method=='initialized':
        for token,title,end in [('find','Finding files to populate context...',''),('population','Populating context...','Context populated.')]:
            write({'id':100,'method':'window/workDoneProgress/create','params':{'token':token}})
            progress(token,'begin',title=title)
            progress(token,'end',message=end)
    elif method and 'id' in m:
        result=({'kind':'full','items':[]} if method=='textDocument/diagnostic' else
                [{'label':'DiskMethod'}] if method=='textDocument/completion' else
                [{'uri':sys.argv[1],'range':{'start':{'line':0,'character':0},'end':{'line':0,'character':9}}}])
        write({'id':m['id'],'result':result})
'''


@pytest.mark.parametrize('change', ['revision', 'child-replacement'])
def test_indexed_arm_owns_watcher_child_and_rejects_replacement(tmp_path, change):
    async def run():
        from types import SimpleNamespace
        from onec_runtime.session import RuntimeSessionConfig
        from hashlib import sha256
        m = api()
        assert hasattr(m, 'LspArm'), 'enabled/disabled resource lifecycle is missing'
        # Immutable existing fixture keeps native setup notifications out of this
        # controlled-protocol lifecycle test. Copied-layout native probes are separate.
        root = Path(__file__).resolve().parents[1] / 'fixtures/onec/JupyterBslTestFixture'
        module = root / 'CommonModules/JupyterBslFixtureCalleeServer/Ext/Module.bsl'
        def tree():
            return {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest() if p.is_file() else None
                    for p in root.rglob('*')}
        original = tree()
        session = SimpleNamespace(config=RuntimeSessionConfig(None, tmp_path / 'evidence', source_root=root))
        off = m.LspArm(False, session, [sys.executable, '-c', FAKE_LS, module.as_uri()], 'Probe', 'DiskMethod')
        assert await off.prepare() == {'enabled': False, 'child_count': 0, 'watcher_count': 0, 'payload_metadata_bytes': 0}
        off.check()
        assert off.gateway is None
        assert await off.close() == {'remaining_children': 0, 'remaining_watchers': 0}
        arm = m.LspArm(True, session, [sys.executable, '-c', FAKE_LS, module.as_uri()], 'Probe', 'DiskMethod')
        try:
            result = await arm.prepare()
            assert result['child_count'] == result['watcher_count'] == 1
            assert result['payload_metadata_bytes'] > 0
            assert result['index']['initialization']['version'] == '1.0.7'
            arm.check()
            child = arm.child
            assert child.transport.process.poll() is None
            assert len(arm.gateway.workspace.watches) == 1
            if change == 'revision':
                arm.gateway.contexts[arm.binding]['analysis_revision'] += 1
                with pytest.raises(m.MeasurementAdmissionError):
                    arm.check()
            else:
                with pytest.raises(m.MeasurementAdmissionError, match='child-replaced'):
                    await arm.gateway.accept_contexts([{**arm.context, 'installation_id': 'replacement'}])
                assert arm.child is child, 'replacement must stop before allocating another child'
        finally:
            assert await arm.close() == {'remaining_children': 0, 'remaining_watchers': 0}
        assert child.transport.process.poll() is not None and child.transport.reader.done()
        assert not arm.gateway.workspace.tasks
        assert tree() == original
    asyncio.run(run())


def test_unknown_native_binary_cannot_enter_fixture_or_live_measurement(tmp_path):
    m = api()
    assert hasattr(m, 'verify_server_binary'), 'pinned binary admission missing'
    server = tmp_path / 'unknown.exe'; server.write_bytes(b'not the pinned binary')
    with pytest.raises(m.MeasurementAdmissionError, match='binary'):
        m.verify_server_binary(server)


def test_measurement_identity_covers_gateway_workspace_and_bridge_not_only_child():
    m = api()
    identity = m.implementation_identity()
    assert set(identity.get('product_sha256', {})) == {
        'lsp_child', 'lsp_gateway', 'lsp_workspace', 'lsp_contexts', 'lsp_kernel', 'lsp_project',
        'lsp_process', 'lsp_process_windows', 'lsp_process_guardian'}
    assert all(len(value) == 64 for value in identity['product_sha256'].values())


@pytest.mark.parametrize('owner', ['lsp_process', 'lsp_process_windows', 'lsp_process_guardian'])
@pytest.mark.parametrize('kind', ['initial', 'protocol'])
def test_changed_installed_owner_bytes_invalidate_native_proof(tmp_path, monkeypatch, owner, kind):
    from importlib import import_module
    m = api()
    layouts = ('Designer', 'EDT-parent', 'EDT-src')
    proof = {'status': 'PASS', 'binary_sha256': m.PINNED_LS_SHA256,
             'implementation': m.implementation_identity() if kind == 'initial' else m.protocol_identity()}
    if kind == 'initial':
        proof['layouts'] = [{'layout': layout, 'setup': {'child_count': 1, 'watcher_count': 1,
            'index': {'initialization': {'name': 'BSL Language Server', 'version': '1.0.7'},
                      'initial_population_ms': 10, 'created_tokens': 3, 'locale': 'en'}},
            'cleanup': {'remaining_children': 0, 'remaining_watchers': 0}} for layout in layouts]
        validator = m._native_proof
    else:
        proof.update(source_tree_unchanged=True, measurements=[{'layout': layout, 'warmup': 3, 'samples': 20,
            'source_viewer_excluded_from_gateway_and_children': True,
            'save_to_observer_detection': {}, 'save_to_iteration_signature': {}} for layout in layouts])
        validator = m._protocol_proof
    path = tmp_path / 'proof.json'; path.write_text(json.dumps(proof))
    validator(path)
    changed = tmp_path / 'changed-owner.py'; changed.write_bytes(b'changed installed ownership behavior')
    monkeypatch.setattr(import_module('onec_runtime_jupyter.' + owner), '__file__', str(changed))
    with pytest.raises(m.MeasurementAdmissionError, match='incompatible'):
        validator(path)


@pytest.mark.parametrize('change', ['none', 'version', 'protocol', 'product', 'missing-server', 'manual-false'])
def test_live_handshake_requires_exact_manual_two_target_identity(change):
    from types import SimpleNamespace
    m = api()
    assert hasattr(m, 'verify_manual_handshake'), 'manual handshake admission missing'
    manifest = SimpleNamespace(product_id='expected-product', artifact_version='expected-release', protocol_version='expected-protocol')
    payload = {'status': 'PASS', 'manual_extension': {'version_matches': True}, 'handshakes': [
        {'target_type': 'ManagedClient', 'product_id': 'expected-product', 'artifact_version': 'expected-release', 'protocol_version': 'expected-protocol'},
        {'target_type': 'ServerEmulation', 'product_id': 'expected-product', 'artifact_version': 'expected-release', 'protocol_version': 'expected-protocol'}]}
    if change == 'version': payload['handshakes'][1]['artifact_version'] = 'old'
    if change == 'protocol': payload['handshakes'][1]['protocol_version'] = 'old'
    if change == 'product': payload['handshakes'][1]['product_id'] = 'foreign'
    if change == 'missing-server': payload['handshakes'].pop()
    if change == 'manual-false': payload['manual_extension']['version_matches'] = False
    if change == 'none':
        assert m.verify_manual_handshake(payload, manifest) == {'compatible': True, 'targets': 2}
    else:
        with pytest.raises(m.MeasurementAdmissionError): m.verify_manual_handshake(payload, manifest)


@pytest.mark.parametrize('on_ms,want', [(100, 'PASS'), (105, 'PASS'), (106, 'FAIL')])
def test_budget_uses_measured_p95_of_twenty_samples_per_arm(on_ms, want):
    m = api()
    assert hasattr(m, 'summarize_pairs'), 'measured budget summarizer missing'
    result = m.summarize_pairs({'timings': {False: [.1] * 20, True: [on_ms / 1000] * 20}, 'arms': [{}] * 46})
    assert result['budget_status'] == want
    assert result['reload']['lsp_off']['p95_ms'] == 100
    assert result['reload']['lsp_on']['p95_ms'] == on_ms
    with pytest.raises(m.MeasurementAdmissionError):
        m.summarize_pairs({'timings': {False: [.1], True: [.1]}, 'arms': []})


def test_live_default_does_not_read_target_or_start_session(tmp_path, monkeypatch):
    m = api()
    assert hasattr(m, 'run_live'), 'separately gated live route missing'
    monkeypatch.delenv('ONEC_RUN_JUPYTER_LSP_INTEGRATION', raising=False)
    with pytest.raises(m.MeasurementAdmissionError, match='opt-in'):
        asyncio.run(m.run_live(tmp_path / 'not-a-server', tmp_path, tmp_path / 'missing-proof', tmp_path / 'missing-protocol'))
    assert list(tmp_path.iterdir()) == []


def test_cli_failed_admission_reports_no_private_exception(tmp_path, monkeypatch, capsys):
    m = api()
    async def fail(*args): raise ValueError('private-export-path raw source bytes')
    monkeypatch.setattr(m, 'run_live', fail)
    monkeypatch.setattr(sys, 'argv', ['check', '--live', '--server', 'missing', '--initial-index-proof', 'missing',
                                    '--protocol-proof', 'missing', '--output', str(tmp_path)])
    with pytest.raises(SystemExit): m.main()
    captured = capsys.readouterr()
    assert 'private-export-path' not in captured.out + captured.err
    result = json.loads((tmp_path / 'evidence.json').read_text())
    assert result == {'status': 'FAIL', 'budget_status': 'UNMEASURED', 'phase': 'admission', 'failure_type': 'ValueError'}


@pytest.mark.parametrize('failure', ['incomplete', 'unknown-version', 'remaining-child', 'foreign-code'])
def test_native_proof_cannot_admit_missing_or_foreign_index_evidence(tmp_path, failure):
    m = api()
    proof = {'status': 'PASS', 'binary_sha256': m.PINNED_LS_SHA256, 'implementation': m.implementation_identity(),
             'layouts': [{'layout': layout, 'setup': {'child_count': 1, 'watcher_count': 1,
                 'index': {'initialization': {'name': 'BSL Language Server', 'version': '1.0.7'},
                           'initial_population_ms': 10, 'created_tokens': 3, 'locale': 'ru'}},
                 'cleanup': {'remaining_children': 0, 'remaining_watchers': 0}}
                for layout in ('Designer', 'EDT-parent', 'EDT-src')]}
    if failure == 'incomplete': del proof['layouts'][0]['setup']['index']['initial_population_ms']
    if failure == 'unknown-version': proof['layouts'][0]['setup']['index']['initialization']['version'] = 'unknown'
    if failure == 'remaining-child': proof['layouts'][0]['cleanup']['remaining_children'] = 1
    if failure == 'foreign-code': proof['implementation']['tool_sha256'] = 'f' * 64
    path = tmp_path / 'proof.json'; path.write_text(json.dumps(proof))
    with pytest.raises(m.MeasurementAdmissionError): m._native_proof(path)


@pytest.mark.parametrize('failure', ['none', 'missing-status', 'foreign-tool', 'missing-samples', 'duplicate-layout', 'missing-viewer-proof'])
def test_protocol_proof_requires_successful_same_code_all_layout_run(tmp_path, failure):
    m = api()
    assert hasattr(m, '_protocol_proof'), 'strict protocol proof admission missing'
    proof = {'status': 'PASS', 'source_tree_unchanged': True, 'binary_sha256': m.PINNED_LS_SHA256,
             'implementation': m.protocol_identity(), 'measurements': [
                 {'layout': layout, 'warmup': 3, 'samples': 20,
                  'source_viewer_excluded_from_gateway_and_children': True,
                  'save_to_observer_detection': {}, 'save_to_iteration_signature': {}}
                 for layout in ('Designer', 'EDT-parent', 'EDT-src')]}
    if failure == 'missing-status': del proof['status']
    if failure == 'foreign-tool': proof['implementation']['tool_sha256'] = 'f' * 64
    if failure == 'missing-samples': proof['measurements'][0]['samples'] = 19
    if failure == 'duplicate-layout': proof['measurements'].append(proof['measurements'][0])
    if failure == 'missing-viewer-proof': del proof['measurements'][0]['source_viewer_excluded_from_gateway_and_children']
    path = tmp_path / 'protocol.json'; path.write_text(json.dumps(proof))
    if failure == 'none':
        assert m._protocol_proof(path) == proof
    else:
        with pytest.raises(m.MeasurementAdmissionError): m._protocol_proof(path)


@pytest.mark.parametrize('failure', ['none', 'startup', 'handshake', 'target-identity', 'pairs', 'cleanup', 'identity-unreadable'])
def test_live_orchestrator_stays_manual_and_fails_closed_with_cleanup(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    import onec_runtime.session as sessions
    import onec_runtime.extension_bundle as extension
    from tools import minimal_worker_reload_benchmark as frozen
    from integration import zup_worker_universe_acceptance as target
    from integration.support import zup_sources
    m = api()
    root = tmp_path / 'export'; root.mkdir()
    module = root / 'Module.bsl'; module.write_bytes(b'unchanged export')
    config = SimpleNamespace(runtime_dir=tmp_path / 'private', infobase_dir=tmp_path / 'db')
    events, state = [], {'alive': False}
    manifest = SimpleNamespace(product_id='product', artifact_version='release', protocol_version=1)
    def admit():
        events.append('admit')
        assert not state['alive']
        return config, root, ('real-unit-placeholder',), {'database_file_identity_sha256': 'a' * 64}
    def identity(path):
        if failure == 'identity-unreadable': raise OSError('private path must not escape')
        return 'b' * 64 if failure == 'target-identity' else 'a' * 64
    def close():
        events.append('close'); state['alive'] = failure == 'cleanup'
    def start(selected):
        events.append('start')
        assert selected.extension_mode is sessions.ExtensionMode.MANUAL
        assert selected.source_root == root
        if failure == 'startup': raise RuntimeError('private source must not escape')
        state['alive'] = True
        selected.evidence_root.mkdir()
        payload = {'status': 'PASS', 'manual_extension': {'version_matches': failure != 'handshake'},
                   'handshakes': [{'target_type': kind, **vars(manifest)} for kind in ('ManagedClient', 'ServerEmulation')]}
        (selected.evidence_root / 'bootstrap.json').write_text(json.dumps(payload))
        return SimpleNamespace(config=selected, artifacts=SimpleNamespace(run_dir=selected.evidence_root), close=close)
    async def pairs(session, units, arm_factory, *, progress):
        events.append('pairs')
        if failure == 'pairs': raise RuntimeError('private source must not escape')
        assert units == ('real-unit-placeholder',)
        assert arm_factory(False).enabled is False and arm_factory(True).enabled is True
        return {'timings': {False: [.1] * 20, True: [.1] * 20}, 'arms': [{}] * 46}
    monkeypatch.setenv('ONEC_RUN_JUPYTER_LSP_INTEGRATION', '1')
    monkeypatch.setattr(m, 'verify_server_binary', lambda _: m.PINNED_LS_SHA256)
    monkeypatch.setattr(m, '_native_proof', lambda _: 'c' * 64)
    monkeypatch.setattr(m, '_protocol_proof', lambda _: {'measurements': []})
    monkeypatch.setattr(frozen, '_exact_live_inputs', admit)
    monkeypatch.setattr(frozen, 'canonical_database_identity', identity)
    monkeypatch.setattr(frozen, '_verify_semantic_canary', lambda session, phase: events.append('canary'))
    monkeypatch.setattr(target, '_matching_target_process_count', lambda _: int(state['alive']))
    monkeypatch.setattr(zup_sources, 'admit_zup_source_bundle', lambda _: SimpleNamespace(units=[
        SimpleNamespace(name='Module', source='Function DiskMethod() Export\nEndFunction')]))
    monkeypatch.setattr(extension, 'packaged_extension_bundle', lambda _: SimpleNamespace(manifest=manifest))
    monkeypatch.setattr(sessions.RuntimeSession, 'start', start)
    monkeypatch.setattr(m, 'run_pairs', pairs)
    proof = tmp_path / 'proof'; proof.write_text('{}')
    output = tmp_path / 'output'
    if failure == 'none':
        result = asyncio.run(m.run_live(tmp_path / 'server', output, proof, proof))
        assert result['budget_status'] == result['status'] == 'PASS'
        assert events == ['admit', 'start', 'pairs', 'canary', 'close', 'admit']
    else:
        with pytest.raises(m.MeasurementAdmissionError):
            asyncio.run(m.run_live(tmp_path / 'server', output, proof, proof))
        result = json.loads((output / 'evidence.json').read_text())
        assert result['status'] == 'FAIL' and result['budget_status'] == 'UNMEASURED'
        assert events.count('start') == 1 and events.count('admit') == 1
        assert events.count('close') == (failure != 'startup')
        if failure in ('startup', 'handshake', 'target-identity', 'identity-unreadable'):
            assert 'pairs' not in events
    assert module.read_bytes() == b'unchanged export'
    assert 'private path' not in (output / 'evidence.json').read_text()


def failure_payload(error):
    payload = getattr(error, 'failure_evidence', None)
    assert isinstance(payload, dict), 'first failure and secondary cleanup evidence were discarded'
    return payload


@pytest.mark.parametrize('faults,expected', [
    ({'inventory'}, [('Exception', 'unclassified')]),
    ({'bridge'}, [('RuntimeError', 'unclassified')]),
    ({'gateway'}, [('OSError', 'unclassified')]),
    ({'gateway-cancel'}, [('CancelledError', 'unclassified')]),
    ({'resources'}, [('MeasurementAdmissionError', 'lsp-resource-cleanup-failed')]),
    ({'process'}, [('OSError', 'unclassified')]),
    ({'inventory', 'bridge', 'gateway', 'resources', 'process'}, [
        ('Exception', 'unclassified'), ('RuntimeError', 'unclassified'),
        ('OSError', 'unclassified'), ('MeasurementAdmissionError', 'lsp-resource-cleanup-failed'),
        ('OSError', 'unclassified')]),
    ({'bridge', 'gateway-cancel', 'process'}, [
        ('RuntimeError', 'unclassified'), ('CancelledError', 'unclassified'),
        ('OSError', 'unclassified')]),
])
def test_lsp_arm_close_attempts_independent_cleanup_and_verification(monkeypatch, faults, expected):
    """An inventory/close failure must not strand later owned cleanup or hide errors."""
    import psutil
    from types import SimpleNamespace
    from tools.minimal_worker_reload_benchmark import validate_public_evidence
    m = api()
    arm = m.LspArm(True, None, [], 'module', 'method')
    arm.processes = [(42, 1.0)]
    events = []
    resources = {'bridge_open': True, 'gateway_open': True}

    def inventory():
        events.append('inventory')
        if 'inventory' in faults:
            raise psutil.AccessDenied(42, name='PRIVATE INVENTORY')
        return []

    def bridge_close():
        events.append('bridge')
        resources['bridge_open'] = False
        if 'bridge' in faults:
            raise RuntimeError('PRIVATE BRIDGE')

    async def gateway_close():
        events.append('gateway')
        resources['gateway_open'] = False
        if 'gateway' in faults:
            raise OSError('PRIVATE GATEWAY')
        if 'gateway-cancel' in faults:
            raise asyncio.CancelledError('PRIVATE GATEWAY CANCEL')

    class Gateway:
        close = staticmethod(gateway_close)
        workspace = SimpleNamespace(watches={}, tasks=set())

        @property
        def children(self):
            events.append('resources')
            return {'remaining': object()} if 'resources' in faults else {}

    def process(pid):
        assert pid == 42
        events.append('process')
        if 'process' in faults:
            raise OSError('PRIVATE PROCESS')
        raise psutil.NoSuchProcess(pid)

    arm.bridge, arm.gateway = SimpleNamespace(close=bridge_close), Gateway()
    monkeypatch.setattr(arm, '_current_processes', inventory)
    monkeypatch.setattr(psutil, 'Process', process)
    error_type = asyncio.CancelledError if 'gateway-cancel' in faults else m.MeasurementAdmissionError
    with pytest.raises(error_type) as raised:
        asyncio.run(arm.close())
    assert events == ['inventory', 'bridge', 'gateway', 'resources', 'process']
    assert resources == {'bridge_open': False, 'gateway_open': False}
    payload = failure_payload(raised.value)
    assert payload == {'entries': [{'phase': 'arm-close', 'type': kind, 'code': code}
                                   for kind, code in expected], 'overflow': False}
    validate_public_evidence(payload)
    assert 'PRIVATE' not in json.dumps(payload) + str(raised.value)


def test_lsp_arm_close_cancellation_finishes_gateway_then_verifies_each_owned_process(monkeypatch):
    """Caller cancellation cannot cancel gateway cleanup or skip later identities."""
    import psutil
    from types import SimpleNamespace
    m = api()
    async def run():
        arm = m.LspArm(True, None, [], 'module', 'method')
        arm.processes = [(42, 1.0), (43, 2.0)]
        entered, finish = asyncio.Event(), asyncio.Event()
        events, verified = [], []
        monkeypatch.setattr(arm, '_current_processes', lambda: [])

        async def close_gateway():
            entered.set()
            await finish.wait()
            events.append('gateway-finished')

        arm.gateway = SimpleNamespace(close=close_gateway, children={},
                                      workspace=SimpleNamespace(watches={}, tasks=set()))
        def process(pid):
            assert events == ['gateway-finished']
            verified.append(pid)
            raise OSError('PRIVATE QUERY')
        monkeypatch.setattr(psutil, 'Process', process)
        task = asyncio.create_task(arm.close())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel('PRIVATE CALLER CANCEL')
            finish.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() and sorted(verified) == [42, 43]
        payload = failure_payload(raised.value)
        assert payload == {'entries': [
            {'phase': 'arm-close', 'type': 'CancelledError', 'code': 'unclassified'},
            {'phase': 'arm-close', 'type': 'OSError', 'code': 'unclassified'},
            {'phase': 'arm-close', 'type': 'OSError', 'code': 'unclassified'}], 'overflow': False}
        assert 'PRIVATE' not in json.dumps(payload) + str(raised.value)
    asyncio.run(run())


@pytest.mark.parametrize('body,release,close,phases', [
    ('prepare', False, True, ['arm-prepare', 'arm-close']),
    ('load', False, True, ['arm-load', 'arm-close']),
    ('postcheck', True, True, ['arm-check-after', 'handle-release', 'arm-close']),
    (None, True, False, ['handle-release']),
    (None, False, True, ['arm-close']),
])
def test_pair_failure_preservation_keeps_primary_and_attempts_each_cleanup(body, release, close, phases):
    async def run():
        from types import SimpleNamespace
        m = api()
        events, progress = [], []
        class Arm:
            def __init__(self, enabled): self.checked = False
            async def prepare(self):
                events.append('prepare')
                if body == 'prepare': raise m.MeasurementAdmissionError('initial-index-timeout')
                return {}
            def check(self):
                if self.checked and body == 'postcheck': raise m.MeasurementAdmissionError('lsp-degraded')
                self.checked = True
            async def close(self):
                events.append('close')
                if close: raise m.MeasurementAdmissionError('lsp-owned-process-survived')
                return {'remaining_children': 0, 'remaining_watchers': 0}
        def load(units, *, profiler):
            events.append('load')
            if body == 'load': raise TimeoutError('PRIVATE SOURCE')
            return 'owned-handle'
        def release_handle(handle):
            assert handle == 'owned-handle'
            events.append('release')
            if release: raise OSError('PRIVATE PATH')
        with pytest.raises(m.MeasurementAdmissionError) as raised:
            await m.run_pairs(SimpleNamespace(load_worker_modules=load, release_worker_generation=release_handle),
                              (), Arm, progress=progress.append)
        payload = failure_payload(raised.value)
        assert [item['phase'] for item in payload['entries']] == phases
        assert all((item['pair'], item['enabled'], item['warmup']) == (0, False, True)
                   for item in payload['entries'])
        assert events == (['prepare', 'close'] if body == 'prepare' else
                          ['prepare', 'load', 'close'] if body == 'load' else
                          ['prepare', 'load', 'release', 'close'])
        assert not progress and payload['overflow'] is False
        assert 'PRIVATE' not in json.dumps(payload)
    asyncio.run(run())


def test_failure_preservation_bounds_entries_and_never_exports_unknown_names_or_strings():
    m = api()
    assert hasattr(m, 'FailureEvidence'), 'bounded failure evidence is missing'
    failures = m.FailureEvidence()
    private_error = type('PRIVATE_CLASS', (RuntimeError,), {})
    failures.record(private_error('PRIVATE SOURCE'), 'PRIVATE PHASE', pair='PRIVATE', enabled='PRIVATE')
    failures.record(m.MeasurementAdmissionError('private-token'), 'arm-prepare', pair=0, enabled=True)
    failures.record(m.MeasurementAdmissionError('initial-index-timeout'), 'arm-prepare', pair=0, enabled=True)
    assert failures.payload['entries'] == [
        {'phase': 'unknown', 'type': 'Exception', 'code': 'unclassified'},
        {'phase': 'arm-prepare', 'type': 'MeasurementAdmissionError', 'code': 'unclassified',
         'pair': 0, 'enabled': True, 'warmup': True},
        {'phase': 'arm-prepare', 'type': 'MeasurementAdmissionError', 'code': 'initial-index-timeout',
         'pair': 0, 'enabled': True, 'warmup': True},
    ]
    for _ in range(100):
        failures.record(OSError('PRIVATE PATH'), 'arm-close')
    assert len(failures.payload['entries']) == 6 and failures.payload['overflow'] is True
    assert 'PRIVATE' not in json.dumps(failures.payload) and 'private-token' not in json.dumps(failures.payload)
    with pytest.raises(m.MeasurementAdmissionError) as raised:
        failures.raise_if_failed()
    assert failure_payload(raised.value)['overflow'] is True


def test_pair_failure_preservation_keeps_second_cancellation_visible_and_finishes_cleanup():
    async def run():
        from types import SimpleNamespace
        m = api()
        entered, closing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []
        class Arm:
            def __init__(self, enabled): pass
            async def prepare(self):
                entered.set()
                await asyncio.Future()
            def check(self): raise AssertionError('not reached')
            async def close(self):
                events.append('close-start')
                closing.set()
                await finish.wait()
                events.append('close-finished')
                return {}
        task = asyncio.create_task(m.run_pairs(SimpleNamespace(), (), Arm))
        await entered.wait()
        task.cancel('PRIVATE FIRST CANCEL')
        await closing.wait()
        task.cancel('PRIVATE SECOND CANCEL')
        finish.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert task.cancelled() and task.cancelling() == 2
        assert events == ['close-start', 'close-finished']
        entries = failure_payload(raised.value)['entries']
        assert [(item['phase'], item['type']) for item in entries] == [
            ('arm-prepare', 'CancelledError'), ('arm-close', 'CancelledError')]
        assert 'PRIVATE' not in json.dumps(entries)
    asyncio.run(run())


def test_pair_failure_preservation_cancellation_recovers_inflight_load_handle_for_release():
    async def run():
        from types import SimpleNamespace
        from threading import Event
        m = api()
        started, finish = Event(), Event()
        events = []
        class Arm:
            def __init__(self, enabled): pass
            async def prepare(self): return {}
            def check(self): pass
            async def close(self): events.append('close'); return {}
        def load(units, *, profiler):
            events.append('load')
            started.set()
            assert finish.wait(5), 'test control failed to release worker'
            events.append('returned')
            return 'owned-handle'
        def release(handle):
            assert handle == 'owned-handle'
            events.append('release')
        task = asyncio.create_task(m.run_pairs(SimpleNamespace(load_worker_modules=load,
                                      release_worker_generation=release), (), Arm))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel('PRIVATE CANCEL')
            finish.set()
            with pytest.raises(asyncio.CancelledError): await task
        finally:
            finish.set()
        assert task.cancelled() and events == ['load', 'returned', 'release', 'close']
    asyncio.run(run())


def live_failure_fixture(tmp_path, monkeypatch):
    """Only external target/native boundaries are replaced; orchestration is real."""
    from types import SimpleNamespace
    import onec_runtime.session as sessions
    import onec_runtime.extension_bundle as extension
    from tools import minimal_worker_reload_benchmark as frozen
    from integration import zup_worker_universe_acceptance as target
    from integration.support import zup_sources
    from check_jupyter_bsl_lsp import SourceWriteOracle
    m = api()
    faults, events, state = {}, [], {'alive': False}
    root = tmp_path / 'export'; root.mkdir()
    (root / 'Module.bsl').write_bytes(b'unchanged export')
    config = SimpleNamespace(runtime_dir=tmp_path / 'private', infobase_dir=tmp_path / 'db')
    manifest = SimpleNamespace(product_id='product', artifact_version='release', protocol_version=1)
    def fail(phase):
        if phase in faults: raise faults[phase]
    def admit():
        events.append('admit')
        assert not state['alive']
        return config, root, ('unit',), {'database_file_identity_sha256': 'a' * 64}
    def identity(path):
        if not state['alive']:
            events.append('identity-verification'); fail('identity-verification')
        return 'a' * 64
    def process_count(path):
        if not state['alive']:
            events.append('process-verification'); fail('process-verification')
        return int(state['alive'])
    def close():
        events.append('session-close'); state['alive'] = False
        if 'close-control' in state: state['close-control']()
        fail('session-close')
    def load(units, *, profiler):
        assert units == ('unit',)
        events.append('load'); fail('load')
        return 'owned-handle'
    def release(handle):
        assert handle == 'owned-handle'
        events.append('release'); fail('release')
    def start(selected):
        events.append('startup'); fail('startup')
        assert selected.extension_mode is sessions.ExtensionMode.MANUAL and selected.source_root == root
        if 'startup-control' in state: state['startup-control']()
        state['alive'] = True
        selected.evidence_root.mkdir()
        payload = {'status': 'PASS', 'manual_extension': {'version_matches': True},
                   'handshakes': [{'target_type': kind, **vars(manifest)} for kind in ('ManagedClient', 'ServerEmulation')]}
        (selected.evidence_root / 'bootstrap.json').write_text(json.dumps(payload))
        events.append('startup-returned')
        return SimpleNamespace(config=selected, artifacts=SimpleNamespace(run_dir=selected.evidence_root),
                               close=close, load_worker_modules=load, release_worker_generation=release)
    class Arm:
        def __init__(self, enabled, *args): pass
        async def prepare(self): fail('prepare'); return {}
        def check(self): pass
        async def close(self): events.append('arm-close'); fail('arm-close'); return {}
    class Oracle(SourceWriteOracle):
        def check(self):
            events.append('source-verification'); fail('source-verification')
            super().check()
    monkeypatch.setenv('ONEC_RUN_JUPYTER_LSP_INTEGRATION', '1')
    monkeypatch.setattr(m, 'verify_server_binary', lambda _: m.PINNED_LS_SHA256)
    monkeypatch.setattr(m, '_native_proof', lambda _: 'c' * 64)
    monkeypatch.setattr(m, '_protocol_proof', lambda _: {'measurements': []})
    monkeypatch.setattr(m, 'LspArm', Arm)
    monkeypatch.setattr(frozen, '_exact_live_inputs', admit)
    monkeypatch.setattr(frozen, 'canonical_database_identity', identity)
    monkeypatch.setattr(frozen, '_verify_semantic_canary', lambda session, phase: events.append('canary'))
    monkeypatch.setattr(target, '_matching_target_process_count', process_count)
    monkeypatch.setattr(zup_sources, 'admit_zup_source_bundle', lambda _: SimpleNamespace(units=[
        SimpleNamespace(name='Module', source='Function DiskMethod() Export\nEndFunction')]))
    monkeypatch.setattr(extension, 'packaged_extension_bundle', lambda _: SimpleNamespace(manifest=manifest))
    monkeypatch.setattr(sessions.RuntimeSession, 'start', start)
    monkeypatch.setattr(import_module('check_jupyter_bsl_lsp'), 'SourceWriteOracle', Oracle)
    proof = tmp_path / 'proof'; proof.write_text('{}')
    output = tmp_path / 'output'
    return SimpleNamespace(api=m, faults=faults, state=state, events=events, output=output,
        run=lambda: m.run_live(tmp_path / 'server', output, proof, proof))


@pytest.mark.parametrize('body', ['prepare', 'load', 'startup'])
def test_live_failure_preservation_crosses_session_close_and_independent_final_verification(tmp_path, monkeypatch, body):
    fixture = live_failure_fixture(tmp_path, monkeypatch)
    fixture.faults[body] = (fixture.api.MeasurementAdmissionError('initial-index-timeout') if body == 'prepare'
                            else RuntimeError('PRIVATE BODY SOURCE'))
    fixture.faults['session-close'] = OSError('PRIVATE CLOSE PATH')
    fixture.faults['source-verification'] = AssertionError('PRIVATE SOURCE TREE')
    fixture.faults['identity-verification'] = OSError('PRIVATE IDENTITY PATH')
    fixture.faults['process-verification'] = OSError('PRIVATE PROCESS COMMAND')
    with pytest.raises(fixture.api.MeasurementAdmissionError): asyncio.run(fixture.run())
    evidence = json.loads((fixture.output / 'evidence.json').read_text())
    expected = ['startup'] if body == 'startup' else [
        'arm-prepare' if body == 'prepare' else 'arm-load', 'session-close']
    expected += ['source-verification', 'identity-verification', 'process-verification']
    assert [item['phase'] for item in evidence.get('failure_evidence', {}).get('entries', [])] == expected
    assert evidence['status'] == 'FAIL' and evidence['budget_status'] == 'UNMEASURED'
    assert evidence['remaining_owned_target_processes'] is None
    assert evidence['database_file_identity_stable'] is None
    assert fixture.events[-3:] == ['source-verification', 'identity-verification', 'process-verification']
    assert not fixture.state['alive'] and 'canary' not in fixture.events
    assert 'PRIVATE' not in json.dumps(evidence)


def test_live_failure_preservation_cancelled_startup_recovers_session_then_closes_it(tmp_path, monkeypatch):
    from threading import Event
    fixture = live_failure_fixture(tmp_path, monkeypatch)
    started, finish = Event(), Event()
    def startup_control():
        started.set()
        assert finish.wait(5), 'test control failed to release startup'
    fixture.state['startup-control'] = startup_control
    async def run():
        task = asyncio.create_task(fixture.run())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel('PRIVATE STARTUP CANCEL')
            finish.set()
            with pytest.raises(asyncio.CancelledError): await task
        finally:
            finish.set()
        assert task.cancelled()
    asyncio.run(run())
    evidence = json.loads((fixture.output / 'evidence.json').read_text())
    assert fixture.events == ['admit', 'startup', 'startup-returned', 'session-close',
                              'source-verification', 'identity-verification', 'process-verification']
    assert not fixture.state['alive']
    assert evidence['failure_evidence']['entries'] == [
        {'phase': 'startup', 'type': 'CancelledError', 'code': 'unclassified'}]
    assert evidence['budget_status'] == 'UNMEASURED' and evidence['remaining_owned_target_processes'] == 0


def test_cli_failure_preservation_does_not_publish_dynamic_exception_class_names(tmp_path, monkeypatch, capsys):
    m = api()
    private_error = type('PRIVATE_CLASS_NAME', (Exception,), {})
    async def fail(*args): raise private_error('PRIVATE SOURCE')
    monkeypatch.setattr(m, 'run_live', fail)
    monkeypatch.setattr(sys, 'argv', ['check', '--live', '--server', 'missing', '--initial-index-proof', 'missing',
                                    '--protocol-proof', 'missing', '--output', str(tmp_path)])
    with pytest.raises(SystemExit): m.main()
    evidence = json.loads((tmp_path / 'evidence.json').read_text())
    captured = capsys.readouterr()
    assert evidence['failure_type'] == 'Exception'
    assert 'PRIVATE' not in json.dumps(evidence) + captured.out + captured.err


def test_live_failure_preservation_cancelled_canary_finishes_before_session_close(tmp_path, monkeypatch):
    from threading import Event
    from tools import minimal_worker_reload_benchmark as frozen
    fixture = live_failure_fixture(tmp_path, monkeypatch)
    started, finish, closed = Event(), Event(), Event()
    def canary(session, phase):
        assert phase == 'IDLE'
        fixture.events.append('canary-started'); started.set()
        assert finish.wait(5), 'test control failed to release canary'
        fixture.events.append('canary-returned')
    monkeypatch.setattr(frozen, '_verify_semantic_canary', canary)
    fixture.state['close-control'] = closed.set
    async def run():
        task = asyncio.create_task(fixture.run())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel('PRIVATE CANARY CANCEL')
            premature_close = await asyncio.to_thread(closed.wait, .1)
            finish.set()
            with pytest.raises(asyncio.CancelledError): await task
        finally:
            finish.set()
        assert task.cancelled() and not premature_close
    asyncio.run(run())
    evidence = json.loads((fixture.output / 'evidence.json').read_text())
    assert fixture.events.count('canary-started') == fixture.events.count('canary-returned') == 1
    assert fixture.events.index('canary-returned') < fixture.events.index('session-close')
    assert evidence['failure_evidence']['entries'] == [
        {'phase': 'canary', 'type': 'CancelledError', 'code': 'unclassified'}]
    assert evidence['budget_status'] == 'UNMEASURED' and evidence.get('canary_passed') is not True


def test_cli_failure_preservation_cancellation_is_source_free_and_exits_cancelled(tmp_path, monkeypatch, capsys):
    m = api()
    async def fail(*args): raise asyncio.CancelledError('PRIVATE CANCEL SOURCE')
    monkeypatch.setattr(m, 'run_live', fail)
    monkeypatch.setattr(sys, 'argv', ['check', '--live', '--server', 'missing', '--initial-index-proof', 'missing',
                                    '--protocol-proof', 'missing', '--output', str(tmp_path)])
    with pytest.raises(SystemExit) as raised: m.main()
    evidence = json.loads((tmp_path / 'evidence.json').read_text())
    captured = capsys.readouterr()
    assert raised.value.code == 130 and evidence['failure_type'] == 'CancelledError'
    assert evidence['status'] == 'FAIL' and evidence['budget_status'] == 'UNMEASURED'
    assert 'PRIVATE' not in json.dumps(evidence) + captured.out + captured.err


@pytest.mark.parametrize('fault,identity,execution,closed', [
    (None, 'matched', 'terminated', 'closed'),
    ('running', 'matched', 'running', 'closed'),
    ('absent', 'unavailable', 'unknown', 'not-opened'),
    ('denied', 'unavailable', 'unknown', 'not-opened'),
    ('pid-reused', 'changed', 'unknown', 'closed'),
    ('creation-changed', 'changed', 'unknown', 'closed'),
    ('query-failed', 'unavailable', 'unknown', 'closed'),
    ('query-raised', 'unavailable', 'unknown', 'closed'),
    ('wait-failed', 'matched', 'unknown', 'closed'),
    ('wait-raised', 'matched', 'unknown', 'closed'),
    ('close-failed', 'matched', 'terminated', 'failed'),
    ('close-raised', 'matched', 'terminated', 'failed'),
])
def test_cleanup_failure_diagnostic_is_exact_zero_wait_and_always_closes(monkeypatch, fault, identity, execution, closed):
    m = api()
    assert hasattr(m, 'process_failure_diagnostic'), 'fail-only process diagnostic missing'
    calls = []
    ticks = 134334285049890917
    expected_created = float(ticks - 116444736000000000) / 10000000
    class Native:
        def OpenProcess(self, rights, inherit, pid):
            calls.append('open')
            assert (rights, inherit, pid) == (0x100000 | 0x1000, False, 42)
            if fault == 'denied': raise OSError('PRIVATE API PATH')
            return None if fault == 'absent' else 99
        def GetProcessId(self, handle):
            assert handle == 99
            calls.append('pid')
            return 43 if fault == 'pid-reused' else 42
        def GetProcessTimes(self, handle, created, exited, kernel, user):
            assert handle == 99
            calls.append('times')
            if fault == 'query-raised': raise OSError('PRIVATE QUERY')
            actual = ticks + (10000000 if fault == 'creation-changed' else 0)
            created._obj.dwLowDateTime = actual & 0xFFFFFFFF
            created._obj.dwHighDateTime = actual >> 32
            return fault != 'query-failed'
        def WaitForSingleObject(self, handle, timeout):
            assert (handle, timeout) == (99, 0)
            calls.append('wait')
            if fault == 'wait-raised': raise OSError('PRIVATE WAIT')
            return 258 if fault == 'running' else 0xFFFFFFFF if fault == 'wait-failed' else 0
        def CloseHandle(self, handle):
            assert handle == 99
            calls.append('close')
            if fault == 'close-raised': raise OSError('PRIVATE CLOSE')
            return fault != 'close-failed'
    monkeypatch.setattr(m, 'windows_cleanup_api', Native)
    result = m.process_failure_diagnostic(42, expected_created, 42, platform='nt')
    assert result == {'role': 'root', 'identity': identity, 'execution': execution, 'handle_close': closed}
    assert calls.count('open') == 1 and calls.count('wait') <= 1
    assert calls.count('close') == (closed != 'not-opened')
    if identity != 'matched': assert 'wait' not in calls
    assert 'PRIVATE' not in json.dumps(result)


def test_cleanup_failure_diagnostic_unsupported_platform_never_opens(monkeypatch):
    m = api()
    assert hasattr(m, 'process_failure_diagnostic'), 'fail-only process diagnostic missing'
    def forbidden(): raise AssertionError('must not open')
    monkeypatch.setattr(m, 'windows_cleanup_api', forbidden)
    assert m.process_failure_diagnostic(42, 1.0, 43, platform='posix') == {
        'role': 'descendant', 'identity': 'unavailable', 'execution': 'unknown', 'handle_close': 'not-opened'}


@pytest.mark.parametrize('survivor', [False, True])
@pytest.mark.parametrize('root', [False, True])
@pytest.mark.parametrize('execution', ['terminated', 'running', 'unknown'])
def test_cleanup_failure_diagnostic_is_fail_only_and_never_changes_assertion(monkeypatch, survivor, root, execution):
    import psutil
    from types import SimpleNamespace
    m = api()
    assert hasattr(m, 'process_failure_diagnostic'), 'fail-only process diagnostic missing'
    arm = m.LspArm(True, None, [], 'module', 'method')
    arm.child = SimpleNamespace(transport=SimpleNamespace(process=SimpleNamespace(pid=42 if root else 43)))
    arm.processes = [(42, 1.0)]
    monkeypatch.setattr(arm, '_current_processes', lambda: [])
    def process(pid):
        assert pid == 42
        if not survivor: raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(create_time=lambda: 1.0)
    monkeypatch.setattr(psutil, 'Process', process)
    calls = []
    expected = {'role': 'root' if root else 'descendant', 'identity': 'matched',
                'execution': execution, 'handle_close': 'closed'}
    def diagnose(pid, created, root_pid):
        calls.append((pid, created, root_pid)); return dict(expected)
    monkeypatch.setattr(m, 'process_failure_diagnostic', diagnose)
    if survivor:
        with pytest.raises(m.MeasurementAdmissionError) as raised:
            asyncio.run(arm.close())
        assert failure_payload(raised.value) == {'entries': [
            {'phase': 'arm-close', 'type': 'MeasurementAdmissionError',
             'code': 'lsp-owned-process-survived', 'process_diagnostic': expected}], 'overflow': False}
        ledger = m.FailureEvidence()
        ledger.record(m.MeasurementAdmissionError('initial-index-timeout'), 'arm-prepare', pair=0, enabled=True)
        ledger.record(raised.value, 'arm-close', pair=0, enabled=True)
        assert ledger.payload['entries'][0]['code'] == 'initial-index-timeout'
        assert ledger.payload['entries'][1] == {
            'phase': 'arm-close', 'type': 'MeasurementAdmissionError',
            'code': 'lsp-owned-process-survived', 'process_diagnostic': expected,
            'pair': 0, 'enabled': True, 'warmup': True}
        assert calls == [(42, 1.0, 42 if root else 43)]
    else:
        assert asyncio.run(arm.close()) == {'remaining_children': 0, 'remaining_watchers': 0}
        assert not calls


@pytest.mark.parametrize('mutation', ['extra-key', 'private-value', 'non-string', 'nested-mutation', 'wrong-phase', 'wrong-code'])
def test_cleanup_failure_diagnostic_aggregation_revalidates_mutable_metadata(mutation):
    m = api()
    error = m.MeasurementAdmissionError('lsp-owned-process-survived')
    error.process_diagnostic = {'role': 'root', 'identity': 'matched', 'execution': 'terminated', 'handle_close': 'closed'}
    ledger = m.FailureEvidence()
    if mutation == 'extra-key': error.process_diagnostic['PRIVATE KEY'] = 'PRIVATE VALUE'
    if mutation == 'private-value': error.process_diagnostic['role'] = 'PRIVATE ROLE'
    if mutation == 'non-string': error.process_diagnostic['identity'] = ['PRIVATE LIST']
    if mutation == 'wrong-code': error = m.MeasurementAdmissionError('lsp-degraded')
    ledger.record(error, 'arm-prepare' if mutation == 'wrong-phase' else 'arm-close')
    if mutation == 'nested-mutation':
        assert 'process_diagnostic' in ledger.entries[0], 'sanitized metadata was not recorded'
        ledger.entries[0]['process_diagnostic']['role'] = 'PRIVATE MUTATION'
    with pytest.raises(m.MeasurementAdmissionError) as raised: ledger.raise_if_failed()
    outer = m.FailureEvidence(); outer.record(raised.value, 'pairs')
    assert 'process_diagnostic' not in outer.payload['entries'][0]
    assert 'PRIVATE' not in json.dumps(outer.payload) + json.dumps(ledger.payload)
