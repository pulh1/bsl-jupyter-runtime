"""Separately gated exact-target runtime/LSP benchmark; evidence contains no sources."""
from __future__ import annotations

import asyncio
import argparse
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import re
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from uuid import uuid4


class MeasurementAdmissionError(ValueError):
    """Source-free failure of benchmark admission (never a budget PASS)."""


def windows_cleanup_api():
    import ctypes
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, args, result in (
        ('OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ('GetProcessId', [w.HANDLE], w.DWORD),
        ('GetProcessTimes', [w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
                             ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)], w.BOOL),
        ('WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD),
        ('CloseHandle', [w.HANDLE], w.BOOL),
    ):
        function = getattr(api, name); function.argtypes = args; function.restype = result
    return api


def process_failure_diagnostic(pid, created, root_pid, *, platform=os.name):
    """Observe only an already-failed identity; never change its cleanup verdict."""
    result = {'role': 'root' if pid == root_pid else 'descendant', 'identity': 'unavailable',
              'execution': 'unknown', 'handle_close': 'not-opened'}
    if platform != 'nt':
        return result
    handle = None
    try:
        import ctypes
        from ctypes import wintypes as w
        api = windows_cleanup_api()
        handle = api.OpenProcess(0x100000 | 0x1000, False, pid)  # synchronize + query-limited; noninheritable
        if not handle:
            return result
        actual_pid = api.GetProcessId(handle)
        if actual_pid != pid:
            result['identity'] = 'changed' if actual_pid else 'unavailable'
            return result
        times = [w.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return result
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        # psutil 7.2.2 windows/init.c _to_unix_time: integer epoch subtraction,
        # THEN cast to double, THEN division. No approximate identity matching.
        native_created = float(ticks - 116444736000000000) / 10000000
        if native_created != created:
            result['identity'] = 'changed'
            return result
        result['identity'] = 'matched'
        result['execution'] = {0: 'terminated', 258: 'running'}.get(api.WaitForSingleObject(handle, 0), 'unknown')
    except Exception:
        pass  # Fixed unknown state only; do not publish OS exception text.
    finally:
        if handle:
            try:
                result['handle_close'] = 'closed' if api.CloseHandle(handle) else 'failed'
            except Exception:
                result['handle_close'] = 'failed'
    return result


class FailureEvidence:
    """First failure plus bounded secondary failures; never retain exceptions."""
    PHASES = frozenset({'admission', 'startup', 'handshake', 'runtime-identity', 'pairs', 'canary',
        'arm-prepare', 'arm-check-before', 'arm-load', 'arm-check-after', 'handle-release',
        'arm-close', 'session-close', 'private-setup', 'private-cleanup', 'source-verification',
        'identity-verification', 'process-verification', 'readmission', 'summary'})
    TYPES = {MeasurementAdmissionError: 'MeasurementAdmissionError', asyncio.CancelledError: 'CancelledError',
             TimeoutError: 'TimeoutError', OSError: 'OSError', ValueError: 'ValueError',
             RuntimeError: 'RuntimeError', AssertionError: 'AssertionError'}
    CODES = frozenset({'lsp-binary-unavailable', 'lsp-binary-identity-mismatch',
        'initial-progress-invalid', 'initial-index-identity-changed', 'initial-index-timeout',
        'initial-index-unconfirmed', 'lsp-degraded', 'lsp-child-replaced',
        'representative-request-failed', 'project-configuration-unavailable',
        'project-root-unavailable', 'lsp-child-unavailable', 'representative-completion-unavailable',
        'representative-definition-unavailable', 'disabled-arm-has-lsp-resources',
        'enabled-arm-not-prepared', 'enabled-arm-resource-count', 'lsp-resource-cleanup-failed',
        'lsp-owned-process-survived', 'manual-handshake-incompatible', 'measurement-samples-incomplete',
        'measurement-duration-invalid', 'native-initial-index-proof-incompatible',
        'native-initial-index-proof-incomplete', 'all-layout-protocol-proof-incompatible',
        'live-opt-in-required', 'representative-disk-method-unavailable',
        'runtime-target-identity-failed', 'runtime-identity-or-cleanup-failed'})
    PROCESS_DIAGNOSTIC = {'role': frozenset({'root', 'descendant'}),
        'identity': frozenset({'matched', 'changed', 'unavailable'}),
        'execution': frozenset({'terminated', 'running', 'unknown'}),
        'handle_close': frozenset({'closed', 'failed', 'not-opened'})}

    def __init__(self):
        self.entries, self.overflow, self.cancelled = [], False, False

    @property
    def payload(self):
        return {'entries': [self._bounded_entry(item) for item in self.entries], 'overflow': self.overflow}

    def _bounded_entry(self, item):
        def selected(key, allowed, fallback):
            value = item.get(key)
            return value if type(value) is str and value in allowed else fallback
        clean = {'phase': selected('phase', self.PHASES, 'unknown'),
                 'type': selected('type', self.TYPES.values(), 'Exception'),
                 'code': selected('code', self.CODES, 'unclassified')}
        pair, enabled = item.get('pair'), item.get('enabled')
        if type(pair) is int and 0 <= pair < 23 and type(enabled) is bool:
            clean.update(pair=pair, enabled=enabled, warmup=pair < 3)
        diagnostic = item.get('process_diagnostic')
        if (clean['phase'] == 'arm-close' and clean['type'] == 'MeasurementAdmissionError'
                and clean['code'] == 'lsp-owned-process-survived'
                and type(diagnostic) is dict and set(diagnostic) == set(self.PROCESS_DIAGNOSTIC)
                and all(type(diagnostic[key]) is str and diagnostic[key] in allowed
                        for key, allowed in self.PROCESS_DIAGNOSTIC.items())):
            clean['process_diagnostic'] = dict(diagnostic)
        return clean

    def record(self, error, phase, *, pair=None, enabled=None):
        nested = getattr(error, '_measurement_failures', None)
        if type(error) in (MeasurementAdmissionError, asyncio.CancelledError) and isinstance(nested, FailureEvidence):
            if nested is self:
                return
            self.cancelled |= nested.cancelled
            self.overflow |= nested.overflow
            for item in nested.entries:
                item = dict(item)
                if ('pair' not in item and type(pair) is int and 0 <= pair < 23
                        and type(enabled) is bool):
                    item.update(pair=pair, enabled=enabled)
                self._append(item)
            return
        self.cancelled |= isinstance(error, asyncio.CancelledError)
        code = (error.args[0] if type(error) is MeasurementAdmissionError and len(error.args) == 1
                and type(error.args[0]) is str and error.args[0] in self.CODES else 'unclassified')
        item = {'phase': phase if type(phase) is str and phase in self.PHASES else 'unknown',
                'type': self.TYPES.get(type(error), 'Exception'), 'code': code}
        if type(pair) is int and 0 <= pair < 23 and type(enabled) is bool:
            item.update(pair=pair, enabled=enabled, warmup=pair < 3)
        if type(error) is MeasurementAdmissionError:
            item['process_diagnostic'] = getattr(error, 'process_diagnostic', None)
        self._append(item)

    def _append(self, item):
        if len(self.entries) < 6:
            self.entries.append(self._bounded_entry(item))
        else:
            self.overflow = True

    def raise_if_failed(self):
        if self.entries or self.overflow:
            error = asyncio.CancelledError() if self.cancelled else MeasurementAdmissionError('measurement-failed')
            error.failure_evidence = self.payload
            error._measurement_failures = self
            raise error from None


async def owned_operation(awaitable, failures, phase, *, pair=None, enabled=None):
    """Finish this same operation despite caller cancellation, then preserve it."""
    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.done() and task.cancelled():
                try:
                    task.result()
                except asyncio.CancelledError as operation_error:
                    failures.record(operation_error, phase, pair=pair, enabled=enabled)
                return None
            # A later caller cancellation remains visible; it cannot cancel the
            # underlying release/close or discard a returned session/handle.
            failures.record(error, phase, pair=pair, enabled=enabled)
        except Exception as error:
            failures.record(error, phase, pair=pair, enabled=enabled)
            return None


PINNED_LS_SHA256 = '143324F254383BC61B3281FB93AD72381818CA6A58BE3B3DC2F6FE5B87308EB4'


def verify_server_binary(server):
    try:
        digest = sha256(Path(server).read_bytes()).hexdigest().upper()
    except OSError:
        raise MeasurementAdmissionError('lsp-binary-unavailable') from None
    if digest != PINNED_LS_SHA256:
        raise MeasurementAdmissionError('lsp-binary-identity-mismatch')
    return digest


def implementation_identity():
    from importlib import import_module
    from onec_runtime_jupyter import lsp_child
    modules = ('lsp_child', 'lsp_gateway', 'lsp_workspace', 'lsp_contexts', 'lsp_kernel', 'lsp_project',
               'lsp_process', 'lsp_process_windows', 'lsp_process_guardian')
    return {'tool_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
            'child_sha256': sha256(Path(lsp_child.__file__).read_bytes()).hexdigest(),
            'product_sha256': {name: sha256(Path(import_module('onec_runtime_jupyter.' + name).__file__).read_bytes()).hexdigest()
                               for name in modules}}


def protocol_identity():
    return {**implementation_identity(),
            'tool_sha256': sha256(Path(__file__).with_name('check_jupyter_bsl_lsp.py').read_bytes()).hexdigest()}


# Exact BSL LS v1.0.7 ServerContext resource pairs. Reports tick before work;
# only the population end after the submitted work's get() is the cold barrier.
_PHASES = {
    'en': {
        'find': ('Finding files to populate context...', ''),
        'population': ('Populating context...', 'Context populated.'),
        'metadata': ('Computing configuration metadata...', 'Configuration metadata computing is finished.'),
    },
    'ru': {
        'find': ('Ищем файлы для наполнения контекста...', ''),
        'population': ('Наполняем контекст...', 'Наполнение контекста завершено.'),
        'metadata': ('Рассчитываем данные конфигурации...', 'Расчет метаданных конфигурации завершен.'),
    },
}


class InitialPopulation:
    """Bounded metadata state machine for one fresh immutable-root child only."""
    def __init__(self, identity, *, clock=time.perf_counter, timeout=600, expected_identity=None):
        self.identity = identity
        self.expected_identity = identity() if expected_identity is None else expected_identity
        self.clock, self.started, self.timeout = clock, clock(), timeout
        self.tokens, self.phases, self.times = {}, set(), {}
        self.reports, self.locale, self.initialization, self.failure = 0, None, None, None

    def observe(self, method, params):
        # This callback never waits, reads files, logs, or retains arbitrary messages.
        if self.failure:
            return
        try:
            self._observe(method, params)
        except (ValueError, TypeError, KeyError):
            self.failure = 'initial-progress-invalid'

    def _observe(self, method, params):
        if type(params) is not dict or params.get('invalid'):
            raise ValueError
        if method == 'initialize-result':
            info = params.get('serverInfo')
            if self.initialization is not None or type(info) is not dict or info != {
                'name': 'BSL Language Server', 'version': '1.0.7'}:
                raise ValueError
            self.initialization = dict(info)
            self.times['initialize'] = self.clock() - self.started
            return
        token = params.get('token')
        if type(token) is not str or not 0 < len(token) <= 160:
            raise ValueError
        if method == 'window/workDoneProgress/create':
            if set(params) != {'token'} or token in self.tokens or len(self.tokens) >= 8:
                raise ValueError
            self.tokens[token] = {'phase': None, 'ended': False}
            return
        if method != '$/progress' or set(params) != {'token', 'value'} or token not in self.tokens:
            raise ValueError
        value, state = params['value'], self.tokens[token]
        if type(value) is not dict or set(value) - {'kind', 'title', 'message', 'percentage', 'cancellable'}:
            raise ValueError
        if any(type(value[k]) is not str or len(value[k]) > 160 for k in ('kind', 'title', 'message') if k in value):
            raise ValueError
        if ('percentage' in value and (type(value['percentage']) is not int or not 0 <= value['percentage'] <= 100)
                or 'cancellable' in value and type(value['cancellable']) is not bool or state['ended']):
            raise ValueError
        kind = value.get('kind')
        if kind == 'begin':
            matches = [(locale, phase) for locale, phases in _PHASES.items()
                       for phase, pair in phases.items() if pair[0] == value.get('title')]
            if state['phase'] or len(matches) != 1:
                raise ValueError
            locale, phase = matches[0]
            if self.locale not in (None, locale) or phase in self.phases:
                raise ValueError
            if phase == 'population' and 'find_end' not in self.times:
                raise ValueError
            self.locale = locale
            self.phases.add(phase)
            state['phase'] = phase
            self.times[phase + '_begin'] = self.clock() - self.started
        elif kind == 'report':
            if state['phase'] is None:
                raise ValueError
            self.reports += 1
        elif kind == 'end':
            phase = state['phase']
            if phase is None or value.get('message') != _PHASES[self.locale][phase][1]:
                raise ValueError
            state['ended'] = True
            self.times[phase + '_end'] = self.clock() - self.started
        else:
            raise ValueError

    def check(self):
        if self.failure:
            raise MeasurementAdmissionError(self.failure)
        try:
            unchanged = self.identity() == self.expected_identity
        except Exception:
            unchanged = False
        if not unchanged:
            raise MeasurementAdmissionError('initial-index-identity-changed')
        elapsed = self.times.get('population_end')
        if elapsed is None:
            elapsed = self.clock() - self.started
        if elapsed >= self.timeout:
            self.failure = 'initial-index-timeout'
            raise MeasurementAdmissionError(self.failure)

    def admit(self):
        self.check()
        if self.initialization is None or 'population_end' not in self.times:
            raise MeasurementAdmissionError('initial-index-unconfirmed')
        return {'initialization': self.initialization, 'locale': self.locale,
                'created_tokens': len(self.tokens), 'reports': self.reports,
                'initial_population_ms': self.times['population_end'] * 1000,
                'phase_ms': {phase: value * 1000 for phase, value in self.times.items()}}

    async def wait(self):
        while True:
            self.check()
            if 'population_end' in self.times:
                return self.admit()
            await asyncio.sleep(.01)


async def measured_reload(session, units, profiler):
    started = time.perf_counter()
    handle = await asyncio.to_thread(session.load_worker_modules, units, profiler=profiler)
    return handle, time.perf_counter() - started


async def run_pairs(session, units, arm_factory, *, progress=None):
    """Fixed 3+20 counterbalanced pairs; no setup/release enters load timing."""
    from onec_runtime.performance_profile import PhaseRecorder, summarize_phases
    timings, arms = {False: [], True: []}, []
    failures = FailureEvidence()
    for pair in range(23):
        for enabled in ((False, True) if pair % 2 == 0 else (True, False)):
            arm, handle = arm_factory(enabled), None
            profiler = PhaseRecorder()
            started = time.perf_counter()
            phase = 'arm-prepare'
            try:
                setup = await arm.prepare()
                setup_seconds = time.perf_counter() - started
                phase = 'arm-check-before'
                arm.check()
                phase = 'arm-load'
                measured = await owned_operation(measured_reload(session, units, profiler), failures, phase,
                                                 pair=pair, enabled=enabled)
                if measured is not None:
                    handle, duration = measured
                failures.raise_if_failed()
                phase = 'arm-check-after'
                arm.check()
            except asyncio.CancelledError as error:
                failures.record(error, phase, pair=pair, enabled=enabled)
            except Exception as error:
                failures.record(error, phase, pair=pair, enabled=enabled)
            finally:
                if handle is not None:
                    await owned_operation(asyncio.to_thread(session.release_worker_generation, handle), failures,
                                          'handle-release', pair=pair, enabled=enabled)
                teardown_started = time.perf_counter()
                cleanup = await owned_operation(arm.close(), failures, 'arm-close', pair=pair, enabled=enabled)
                teardown_seconds = time.perf_counter() - teardown_started
            failures.raise_if_failed()
            if pair >= 3:
                timings[enabled].append(duration)
            arms.append({'pair': pair, 'enabled': enabled, 'warmup': pair < 3,
                         'reload_ms': duration * 1000, 'setup_ms': setup_seconds * 1000,
                         'teardown_ms': teardown_seconds * 1000, 'setup': setup, 'cleanup': cleanup,
                         'phases': summarize_phases(profiler.events)})
            if progress:
                progress(arms[-1])
    return {'timings': timings, 'arms': arms}


class LspArm:
    """Actual gateway, watcher and owned child; config comm sink is in-process."""
    def __init__(self, enabled, session, command, module_name, method, *, timeout=600):
        self.enabled, self.session, self.command = enabled, session, command
        self.module_name, self.method, self.timeout = module_name, method, timeout
        self.binding, self.gateway, self.child, self.population = uuid4().hex, None, None, None
        self.bridge, self.envelope, self.failure, self.root = None, None, None, None
        self.responses, self.serial = {}, 0
        self.processes = []

    def _emit(self, message):
        if 'id' in message:
            self.responses[message['id']] = message

    def _status(self, message):
        if message['state'] in ('unavailable', 'virtual-only') or message.get('reason') not in (None, 'index-convergence-unconfirmed'):
            self.failure = 'lsp-degraded'

    def _identity(self):
        from onec_runtime_jupyter.lsp_gateway import fence
        from onec_runtime_jupyter.lsp_workspace import root_identity
        current = self.gateway.contexts.get(self.binding)
        return (self.gateway.children.get(self.binding), fence(current) if current else None,
                root_identity(self.root), self.child.degraded_reason, self.child.available, self.failure)

    def _factory(self, context, emit, **kwargs):
        from onec_runtime_jupyter.lsp_child import ChildSession
        from onec_runtime_jupyter.lsp_gateway import fence
        from onec_runtime_jupyter.lsp_workspace import root_identity
        if self.child is not None:
            self.failure = 'lsp-child-replaced'
            raise MeasurementAdmissionError(self.failure)
        child = ChildSession(context, emit, **kwargs,
                             on_progress=lambda method, params: self.population.observe(method, params))
        self.child = child
        self.population = InitialPopulation(self._identity, timeout=self.timeout,
            expected_identity=(child, fence(context), root_identity(self.root), None, True, None))
        return child

    async def _request(self, method, character):
        self.serial += 1
        await self.gateway.handle({'id': self.serial, 'method': method, 'params': {
            'textDocument': {'uri': self.context['document_uri']}, 'position': {'line': 0, 'character': character}}})
        result = self.responses.pop(self.serial, None)
        if result is None or 'error' in result:
            raise MeasurementAdmissionError('representative-request-failed')
        return result.get('result')

    async def prepare(self):
        if not self.enabled:
            self.check()
            return {'enabled': False, 'child_count': 0, 'watcher_count': 0, 'payload_metadata_bytes': 0}
        from onec_runtime_jupyter.lsp_contexts import normalize_source_root
        from onec_runtime_jupyter.lsp_gateway import Gateway
        from onec_runtime_jupyter.lsp_kernel import ProjectBridge, TARGET
        from onec_runtime_jupyter.lsp_project import decode_envelope
        targets = {}
        manager = SimpleNamespace(register_target=lambda name, callback: targets.update({name: callback}),
                                  unregister_target=lambda name, callback: targets.pop(name, None))
        shell = SimpleNamespace(kernel=SimpleNamespace(comm_manager=manager))
        owner = self
        class Comm:
            def send(self, data): owner.envelope = data
            def on_msg(self, callback): pass
            def on_close(self, callback): pass
            def close(self): pass
        self.bridge = ProjectBridge(shell, self.session)
        targets[TARGET](Comm(), {'content': {'data': {'version': 2}}})
        epoch, config, reason = decode_envelope(self.envelope)
        if reason or config is None or config.source_root is None:
            raise MeasurementAdmissionError('project-configuration-unavailable')
        self.root, reason = normalize_source_root(config.source_root)
        if reason or self.root is None:
            raise MeasurementAdmissionError('project-root-unavailable')
        self.context = {'binding_id': self.binding, 'epoch': epoch,
                        'document_uri': 'file:///benchmark/notebook.bsl',
                        'kernel_id': 'in-process-benchmark-comm', 'kernel_incarnation': uuid4().hex,
                        'installation_id': config.installation_id, 'source_root': str(self.root)}
        self.gateway = Gateway(self._emit, child_factory=self._factory, command=self.command, status=self._status)
        await self.gateway.accept_contexts([self.context])
        prefix = 'Результат = ' + self.module_name + '.'
        text = prefix + self.method + '();'
        await self.gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
            'uri': self.context['document_uri'], 'version': 1, 'languageId': 'bsl', 'text': text}}})
        if self.population is None:
            raise MeasurementAdmissionError('lsp-child-unavailable')
        index = await self.population.wait()
        self.check()
        completed = time.perf_counter()
        completion = await self._request('textDocument/completion', len(prefix))
        completion_ms = (time.perf_counter() - completed) * 1000
        items = completion.get('items', []) if isinstance(completion, dict) else completion
        if not isinstance(items, list) or not any(self.method in item.get('label', '') for item in items):
            raise MeasurementAdmissionError('representative-completion-unavailable')
        defined = time.perf_counter()
        definitions = await self._request('textDocument/definition', len(prefix) + 2)
        definition_ms = (time.perf_counter() - defined) * 1000
        if isinstance(definitions, dict): definitions = [definitions]
        if not isinstance(definitions, list) or not any(
            str(item.get('uri') or item.get('targetUri') or '').startswith('onec-bsl:' + self.binding + '/')
            for item in definitions):
            raise MeasurementAdmissionError('representative-definition-unavailable')
        self.check()
        processes = self._current_processes()
        return {'enabled': True, 'child_count': len(self.gateway.children),
                'watcher_count': len(self.gateway.workspace.watches),
                'payload_metadata_bytes': len(json.dumps(self.envelope, separators=(',', ':'), ensure_ascii=False).encode()),
                'index': index, 'completion_ms': completion_ms, 'definition_ms': definition_ms,
                'child_process_tree_count': len(processes),
                'child_process_tree_rss_bytes': sum(process.memory_info().rss for process in processes)}

    def _current_processes(self):
        import psutil
        if self.child is None or self.child.transport.process is None:
            return []
        try:
            parent = psutil.Process(self.child.transport.process.pid)
            processes = [parent, *parent.children(recursive=True)]
            self.processes.extend((p.pid, p.create_time()) for p in processes)
            return processes
        except psutil.NoSuchProcess:
            return []

    def check(self):
        if not self.enabled:
            if self.gateway is not None or self.child is not None or self.bridge is not None:
                raise MeasurementAdmissionError('disabled-arm-has-lsp-resources')
            return
        if self.population is None or self.gateway is None:
            raise MeasurementAdmissionError('enabled-arm-not-prepared')
        self.population.admit()
        if len(self.gateway.children) != 1 or len(self.gateway.workspace.watches) != 1:
            raise MeasurementAdmissionError('enabled-arm-resource-count')

    async def close(self):
        import psutil
        failures = FailureEvidence()
        try:
            self._current_processes()
        except (Exception, asyncio.CancelledError) as error:
            failures.record(error, 'arm-close')
        if self.bridge is not None:
            try:
                self.bridge.close()
                self.bridge = None
            except (Exception, asyncio.CancelledError) as error:
                failures.record(error, 'arm-close')
        if self.gateway is not None:
            try:
                await owned_operation(self.gateway.close(), failures, 'arm-close')
            except (Exception, asyncio.CancelledError) as error:
                failures.record(error, 'arm-close')
            try:
                if self.gateway.children or self.gateway.workspace.watches or self.gateway.workspace.tasks:
                    raise MeasurementAdmissionError('lsp-resource-cleanup-failed')
            except (Exception, asyncio.CancelledError) as error:
                failures.record(error, 'arm-close')
        for pid, created in set(self.processes):
            try:
                if psutil.Process(pid).create_time() == created:
                    error = MeasurementAdmissionError('lsp-owned-process-survived')
                    error.process_diagnostic = process_failure_diagnostic(pid, created, self.child.transport.process.pid)
                    raise error
            except psutil.NoSuchProcess:
                pass
            except (Exception, asyncio.CancelledError) as error:
                failures.record(error, 'arm-close')
        failures.raise_if_failed()
        return {'remaining_children': 0, 'remaining_watchers': 0}


async def probe_fixtures(server, fixture):
    from onec_runtime.session import RuntimeSessionConfig
    from check_jupyter_bsl_lsp import create_edt_fixture, fingerprints
    binary = verify_server_binary(server)
    original, layouts = fingerprints(fixture), []
    with TemporaryDirectory(prefix='onec-initial-index-fixtures-') as temporary:
        owned = Path(temporary)
        designer, edt = owned / 'designer', owned / 'edt'
        shutil.copytree(fixture, designer)
        create_edt_fixture(edt)
        for layout, configured, module, method in (
            ('Designer', designer, 'JupyterBslFixtureCalleeServer', 'ВыполнитьШаг'),
            ('EDT-parent', edt, 'ProbeServer', 'ДисковыйВызов'),
            ('EDT-src', edt / 'src', 'ProbeServer', 'ДисковыйВызов'),
        ):
            expected = fingerprints(configured)
            session = SimpleNamespace(config=RuntimeSessionConfig(None, owned / 'evidence', source_root=configured))
            arm = LspArm(True, session, [str(server)], module, method)
            try:
                result = await arm.prepare()
            finally:
                cleanup = await arm.close()
                assert fingerprints(configured) == expected, 'fixture source tree changed'
            layouts.append({'layout': layout, 'setup': result, 'cleanup': cleanup})
            print('PASS initial population + completion/definition: ' + layout, flush=True)
    assert fingerprints(fixture) == original, 'input source tree changed'
    return {'status': 'PASS', 'binary_sha256': binary, 'layouts': layouts,
            'implementation': implementation_identity(),
            'config_transport': 'in-process ProjectBridge; native browser transport tested separately',
            'scope': 'cold initial population only; watched-update convergence remains unconfirmed'}


def verify_manual_handshake(payload, manifest):
    expected = {'product_id': manifest.product_id, 'artifact_version': manifest.artifact_version,
                'protocol_version': manifest.protocol_version}
    handshakes = payload.get('handshakes', [])
    if (payload.get('status') != 'PASS' or payload.get('manual_extension', {}).get('version_matches') is not True
            or len(handshakes) != 2 or {item.get('target_type') for item in handshakes} != {'ManagedClient', 'ServerEmulation'}
            or any(any(item.get(key) != value for key, value in expected.items()) for item in handshakes)):
        raise MeasurementAdmissionError('manual-handshake-incompatible')
    return {'compatible': True, 'targets': 2}


def summarize_pairs(result):
    from tools.minimal_worker_reload_benchmark import nearest_rank_summary
    if len(result['arms']) != 46 or any(len(result['timings'][mode]) != 20 for mode in (False, True)):
        raise MeasurementAdmissionError('measurement-samples-incomplete')
    summary = {('lsp_on' if mode else 'lsp_off'): nearest_rank_summary([value * 1000 for value in values])
               for mode, values in result['timings'].items()}
    off, on = summary['lsp_off']['p95_ms'], summary['lsp_on']['p95_ms']
    if off <= 0:
        raise MeasurementAdmissionError('measurement-duration-invalid')
    return {'reload': summary, 'p95_regression': on / off - 1, 'budget': .05,
            'budget_status': 'PASS' if on <= off * 1.05 else 'FAIL', 'arms': result['arms']}


def _native_proof(path):
    proof = json.loads(Path(path).read_text(encoding='utf-8'))
    if (proof.get('status') != 'PASS' or proof.get('binary_sha256') != PINNED_LS_SHA256
            or proof.get('implementation') != implementation_identity()
            or {item.get('layout') for item in proof.get('layouts', [])} != {'Designer', 'EDT-parent', 'EDT-src'}
            or len(proof['layouts']) != 3):
        raise MeasurementAdmissionError('native-initial-index-proof-incompatible')
    for item in proof['layouts']:
        setup, cleanup = item.get('setup', {}), item.get('cleanup', {})
        index = setup.get('index', {})
        if (setup.get('child_count') != 1 or setup.get('watcher_count') != 1
                or cleanup != {'remaining_children': 0, 'remaining_watchers': 0}
                or index.get('initialization') != {'name': 'BSL Language Server', 'version': '1.0.7'}
                or index.get('locale') not in ('en', 'ru')
                or type(index.get('initial_population_ms')) not in (float, int)
                or index['initial_population_ms'] <= 0
                or type(index.get('created_tokens')) is not int or not 2 <= index['created_tokens'] <= 8):
            raise MeasurementAdmissionError('native-initial-index-proof-incomplete')
    return sha256(Path(path).read_bytes()).hexdigest()


def _protocol_proof(path):
    proof = json.loads(Path(path).read_text(encoding='utf-8'))
    measurements = proof.get('measurements', [])
    if (proof.get('status') != 'PASS' or proof.get('source_tree_unchanged') is not True
            or proof.get('binary_sha256') != PINNED_LS_SHA256
            or proof.get('implementation') != protocol_identity()
            or len(measurements) != 3
            or {item.get('layout') for item in measurements} != {'Designer', 'EDT-parent', 'EDT-src'}
            or any(item.get('warmup') != 3 or item.get('samples') != 20
                   or item.get('source_viewer_excluded_from_gateway_and_children') is not True
                   or any(key not in item for key in ('save_to_observer_detection', 'save_to_iteration_signature'))
                   for item in measurements)):
        raise MeasurementAdmissionError('all-layout-protocol-proof-incompatible')
    return proof


async def run_live(server, output, initial_proof, protocol_proof):
    if os.environ.get('ONEC_RUN_JUPYTER_LSP_INTEGRATION') != '1':
        raise MeasurementAdmissionError('live-opt-in-required')
    from tools.minimal_worker_reload_benchmark import (
        _exact_live_inputs, _verify_semantic_canary, canonical_database_identity, validate_public_evidence)
    from integration.zup_worker_universe_acceptance import _matching_target_process_count
    from integration.support.zup_sources import admit_zup_source_bundle
    from onec_runtime.session import RuntimeSession, RuntimeSessionConfig, ExtensionMode
    from onec_runtime.extension_bundle import packaged_extension_bundle
    from check_jupyter_bsl_lsp import SourceWriteOracle
    binary, proof_hash = verify_server_binary(server), _native_proof(initial_proof)
    protocol = _protocol_proof(protocol_proof)
    config, source_root, units, reference = _exact_live_inputs()
    expected_identity = reference['database_file_identity_sha256']
    source_oracle = SourceWriteOracle(source_root)
    bundle = admit_zup_source_bundle(source_root)
    module_name = bundle.units[0].name
    match = re.search(r'(?im)^\s*(?:Функция|Процедура|Function|Procedure)\s+(\w+)\s*\([^)]*\)\s*(?:Экспорт|Export)\b', bundle.units[0].source)
    if match is None:
        raise MeasurementAdmissionError('representative-disk-method-unavailable')
    method = match.group(1)
    del bundle, match
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    evidence = {'status': 'RUNNING', 'budget_status': 'UNMEASURED', 'phase': 'startup',
                'binary_sha256': binary, 'initial_index_proof_sha256': proof_hash,
                'protocol_proof_sha256': sha256(Path(protocol_proof).read_bytes()).hexdigest(),
                'implementation': implementation_identity(), 'reference': reference,
                'warmup_pairs': 3, 'measured_pairs': 20, 'extension_mode': 'manual',
                'config_transport': 'in-process ProjectBridge; real browser/kernel gate is separate',
                'file_observation': [{key: item[key] for key in ('layout', 'save_to_observer_detection', 'save_to_iteration_signature')}
                                     for item in protocol['measurements']]}
    def write():
        validate_public_evidence(evidence)
        (output / 'evidence.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    write()
    session = None
    failures = FailureEvidence()
    private_parent = config.runtime_dir / 'source-lsp-benchmark-private'
    phase = 'private-setup'
    try:
        private_parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='admitted-', dir=private_parent) as temporary, open(os.devnull, 'w') as sink:
            private = Path(temporary)
            try:
                phase = 'startup'
                started = time.perf_counter()
                with redirect_stdout(sink), redirect_stderr(sink):
                    session = await owned_operation(asyncio.to_thread(RuntimeSession.start,
                        RuntimeSessionConfig(config, private / 'evidence', source_root=source_root,
                                             extension_mode=ExtensionMode.MANUAL)), failures, phase)
                failures.raise_if_failed()
                evidence['startup_ms'] = (time.perf_counter() - started) * 1000
                phase = 'handshake'
                evidence['handshake'] = verify_manual_handshake(
                    json.loads((session.artifacts.run_dir / 'bootstrap.json').read_text(encoding='utf-8')),
                    packaged_extension_bundle(private).manifest)
                phase = 'runtime-identity'
                if canonical_database_identity(config.infobase_dir) != expected_identity or _matching_target_process_count(config.infobase_dir) <= 0:
                    raise MeasurementAdmissionError('runtime-target-identity-failed')
                evidence['phase'] = 'measured-pairs'; write()
                evidence['completed_arms'] = []
                def progress(arm):
                    evidence['completed_arms'].append(arm)
                    write()
                with redirect_stdout(sink), redirect_stderr(sink):
                    phase = 'pairs'
                    result = await run_pairs(session, units,
                        lambda enabled: LspArm(enabled, session, [str(server)], module_name, method), progress=progress)
                    evidence['phase'] = 'canary'; write()
                    phase = 'canary'
                    await owned_operation(asyncio.to_thread(_verify_semantic_canary, session, 'IDLE'), failures, phase)
                    failures.raise_if_failed()
                evidence['canary_passed'] = True
            except asyncio.CancelledError as error:
                failures.record(error, phase)
            except Exception as error:
                failures.record(error, phase)
            finally:
                if session is not None:
                    with redirect_stdout(sink), redirect_stderr(sink):
                        await owned_operation(asyncio.to_thread(session.close), failures, 'session-close')
                    session = None
            phase = 'private-cleanup'
    except asyncio.CancelledError as error:
        failures.record(error, phase)
    except Exception as error:
        failures.record(error, phase)

    # Each final verification is independent, even if the body or another
    # cleanup failed. Unknown identity/process state is never represented as 0.
    source_unchanged = identity_stable = remaining = None
    try:
        source_oracle.check()
        source_unchanged = True
    except AssertionError as error:
        source_unchanged = False
        failures.record(error, 'source-verification')
    except Exception as error:
        failures.record(error, 'source-verification')
    try:
        identity_stable = canonical_database_identity(config.infobase_dir) == expected_identity
        if not identity_stable:
            raise MeasurementAdmissionError('runtime-identity-or-cleanup-failed')
    except Exception as error:
        failures.record(error, 'identity-verification')
    try:
        remaining = _matching_target_process_count(config.infobase_dir)
        if type(remaining) is not int or remaining < 0:
            remaining = None
            raise MeasurementAdmissionError('runtime-identity-or-cleanup-failed')
        if remaining != 0:
            raise MeasurementAdmissionError('runtime-identity-or-cleanup-failed')
    except Exception as error:
        failures.record(error, 'process-verification')
    evidence.update(source_tree_unchanged=source_unchanged, database_file_identity_stable=identity_stable,
                    remaining_owned_target_processes=remaining)
    if not failures.entries and not failures.overflow:
        # Re-admit the frozen raw source/platform/extension identities only after
        # the owned target is closed; never install/repair or rewrite the export.
        phase = 'readmission'
        try:
            _exact_live_inputs()
            phase = 'summary'
            evidence.update(summarize_pairs(result))
            evidence.pop('completed_arms', None)
        except Exception as error:
            failures.record(error, phase)
    if failures.entries or failures.overflow:
        primary = failures.entries[0]
        evidence.update(status='FAIL', budget_status='UNMEASURED', failure_type=primary['type'],
                        failure_code=primary['code'], failure_evidence=failures.payload)
        write()
        failures.raise_if_failed()
    evidence.update(status=evidence['budget_status'], phase='complete')
    write()
    return evidence


def main():
    # Checkout-only benchmark helpers; runtime/Jupyter still resolve from the
    # installed wheel when PYTHONPATH is cleared (no src directories inserted).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--probe-fixtures', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--initial-index-proof', type=Path)
    parser.add_argument('--protocol-proof', type=Path)
    parser.add_argument('--fixture', type=Path, default=Path('tests/fixtures/onec/JupyterBslTestFixture'))
    parser.add_argument('--output', type=Path, default=Path('artifacts/source-lsp-initial-index'))
    args = parser.parse_args()
    if args.probe_fixtures == args.live:
        parser.error('select exactly one of --probe-fixtures or --live')
    if args.live and (not args.initial_index_proof or not args.protocol_proof):
        parser.error('--live requires --initial-index-proof and --protocol-proof')
    if (args.output / 'evidence.json').exists():
        parser.error('output already contains evidence; choose a new output directory')
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        result = asyncio.run(run_live(args.server.resolve(), args.output, args.initial_index_proof, args.protocol_proof)
                             if args.live else probe_fixtures(args.server.resolve(), args.fixture.resolve()))
    except (Exception, asyncio.CancelledError) as error:
        # Native/runtime exception strings can include private paths or sources.
        if not (args.output / 'evidence.json').exists():
            (args.output / 'evidence.json').write_text(json.dumps({
                'status': 'FAIL', 'budget_status': 'UNMEASURED', 'phase': 'admission',
                'failure_type': FailureEvidence.TYPES.get(type(error), 'Exception')}, indent=2), encoding='utf-8')
        print('FAIL: admission or measurement stopped; see source-free evidence', flush=True)
        raise SystemExit(130 if isinstance(error, asyncio.CancelledError) else 1) from None
    (args.output / 'evidence.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    if result['status'] != 'PASS':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
