"""Real BSL LS acceptance: isolated gateway, rootless/Designer/EDT and saved-file convergence."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time

import psutil

from support.benchmark import summarize_durations

from onec_runtime_jupyter.lsp_proxy import read_message, write_message


class Client:
    def __init__(self, process):
        self.process, self.messages, self.notifications, self.serial = process, queue.Queue(), [], 0
        def receive():
            try:
                while (message := read_message(process.stdout)) is not None:
                    self.messages.put(message)
            finally:
                self.messages.put(None)
        threading.Thread(target=receive, daemon=True).start()

    def notify(self, method, params):
        write_message(self.process.stdin, {'jsonrpc': '2.0', 'method': method, 'params': params})

    def next(self, timeout):
        message = self.messages.get(timeout=timeout)
        if message is None:
            raise RuntimeError('Language server closed output')
        if 'id' in message and 'method' in message:
            result = [{} for _ in message.get('params', {}).get('items', [])] if message['method'] == 'workspace/configuration' else None
            write_message(self.process.stdin, {'jsonrpc': '2.0', 'id': message['id'], 'result': result})
        elif 'method' in message:
            self.notifications.append(message)
        return message

    def request(self, method, params, timeout=90):
        self.serial += 1
        request_id = self.serial
        write_message(self.process.stdin, {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.next(max(.1, deadline - time.monotonic()))
            if message.get('id') == request_id and 'method' not in message:
                if 'error' in message:
                    raise RuntimeError(message['error'])
                return message.get('result')
        raise TimeoutError(method)


def make_context(root, binding, epoch=1):
    return {'binding_id': binding, 'epoch': epoch, 'document_uri': f'file:///notebooks/{binding}.bsl',
            'kernel_id': 'shared-test-kernel', 'kernel_incarnation': 'test-incarnation',
            'source_root': str(root) if root else None, 'installation_id': 'test-installation'}


def fingerprints(root):
    return {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            for p in root.rglob('*')}


class SourceWriteOracle:
    """Initial fixture plus planned operations; never rebase on observed output."""
    def __init__(self, root):
        self.root = Path(root)
        self.expected = fingerprints(self.root)

    def check(self):
        assert fingerprints(self.root) == self.expected, 'unplanned source tree change'

    def save(self, path, payload, *, atomic=False):
        self.check()
        relative = path.relative_to(self.root).as_posix()
        self.expected[relative] = sha256(payload).hexdigest()
        if atomic:
            replacement = path.with_suffix('.tmp')
            replacement.write_bytes(payload)
            replacement.replace(path)
        else:
            path.write_bytes(payload)
        self.check()

    def delete(self, path):
        self.check()
        del self.expected[path.relative_to(self.root).as_posix()]
        path.unlink()
        self.check()

    def rename(self, path, target):
        self.check()
        old, new = (p.relative_to(self.root).as_posix() for p in (path, target))
        affected = [p for p in self.expected if p == old or p.startswith(old + '/')]
        assert affected
        for name in affected:
            self.expected[new + name[len(old):]] = self.expected.pop(name)
        path.rename(target)
        self.check()


def create_edt_fixture(root, module_name='ProbeServer'):
    config = root / 'src/Configuration/Configuration.mdo'
    module = root / f'src/CommonModules/{module_name}/Module.bsl'
    metadata = module.with_name(f'{module_name}.mdo')
    config.parent.mkdir(parents=True); module.parent.mkdir(parents=True)
    config.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" uuid="06ca1e23-8338-4cf8-8391-511f62e6d0db">
  <name>OnecLspProbe</name><scriptVariant>Russian</scriptVariant>
  <commonModules>CommonModule.{module_name}</commonModules>
</mdclass:Configuration>''', encoding='utf-8')
    metadata.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<mdclass:CommonModule xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" uuid="d178221d-9ed0-47dc-adc8-43a680cf1ae8">
  <name>{module_name}</name><server>true</server><serverCall>true</serverCall>
</mdclass:CommonModule>''', encoding='utf-8')
    module.write_text('Функция ДисковыйВызов(Аргумент) Экспорт\nВозврат Аргумент;\nКонецФункции\n', encoding='utf-8')



def rename_module(root, name, new_name, layout, oracle=None):
    oracle = oracle or SourceWriteOracle(root)
    directory = root / 'CommonModules' / name
    target = directory.with_name(new_name)
    oracle.rename(directory, target)
    metadata = root / 'CommonModules' / (name + '.xml') if layout == 'Designer' else target / (name + '.mdo')
    oracle.save(metadata, metadata.read_bytes().replace(name.encode(), new_name.encode()))
    oracle.rename(metadata, metadata.with_name(new_name + metadata.suffix))
    config = root / 'Configuration.xml' if layout == 'Designer' else root / 'Configuration/Configuration.mdo'
    oracle.save(config, config.read_bytes().replace(name.encode(), new_name.encode()))
    return target / ('Ext/Module.bsl' if layout == 'Designer' else 'Module.bsl')

async def exercise(server, designer, edt, *, samples=20, warmup=3):
    from onec_runtime_jupyter.lsp_gateway import Gateway
    from onec_runtime_jupyter.lsp_contexts import normalize_source_root
    from onec_runtime_jupyter.lsp_sources import SourceStore
    output, statuses, serial = [], [], 0
    measurements = []
    from onec_runtime_jupyter.lsp_workspace import WorkspaceWatchService, scan_workspace
    scans = []
    def scan(root, **kwargs):
        wall, cpu = time.perf_counter(), time.process_time()
        result = scan_workspace(root, **kwargs)
        scans.append({'wall_seconds': time.perf_counter() - wall, 'cpu_seconds': time.process_time() - cpu,
                      'entries': len(result.entries), 'reason': result.reason})
        return result
    workspace = WorkspaceWatchService(scan=scan)
    gateway = Gateway(output.append, command=[str(server)], status=statuses.append, workspace=workspace)
    async def request(method, c, *, line=0, character=0):
        nonlocal serial
        serial += 1; current = serial
        await gateway.handle({'id': current, 'method': method, 'params': {
            'textDocument': {'uri': c['document_uri']}, 'position': {'line': line, 'character': character}}})
        response = next(m for m in reversed(output) if m.get('id') == current)
        if 'error' in response:
            if response['error']['code'] in (-32800, -32801):
                return None  # File change fenced an in-flight probe; retry within deadline.
            if method == 'textDocument/definition' and response['error']['message'] == 'language-service-unavailable':
                return None  # Mapper rejects stale deleted-path results while indexing converges.
            raise AssertionError(response)
        return response['result']
    versions = {}
    async def opened(c, text):
        uri = c['document_uri']; version = versions.get(uri, 0) + 1; versions[uri] = version
        method = 'textDocument/didOpen' if version == 1 else 'textDocument/didChange'
        params = {'textDocument': {'uri': uri, 'version': version}}
        if version == 1: params['textDocument'].update(languageId='bsl', text=text)
        else: params['contentChanges'] = [{'text': text}]
        await gateway.handle({'method': method, 'params': params})
    async def eventually(method, c, predicate, *, line=0, character=0):
        deadline = time.perf_counter() + 30
        result = None
        while time.perf_counter() < deadline:
            result = await request(method, c, line=line, character=character)
            if predicate(result):
                return result
            await asyncio.sleep(.05)
        raise AssertionError((method, 'specific result did not converge', result))
    def contains(result, text):
        return text in json.dumps(result, ensure_ascii=False)
    async def completion(c, prefix, symbol):
        return await eventually('textDocument/completion', c, lambda r: contains(r, symbol), character=len(prefix))
    rootless = make_context(None, '0' * 32)
    try:
        await gateway.accept_contexts([rootless])
        await opened(rootless, 'Сообщ')
        await completion(rootless, 'Сообщ', 'Сообщить')
        # Static virtual document combines two cell bodies; no file is written.
        await opened(rootless, 'Функция МеждуЯчейками(Аргумент)\nВозврат Аргумент;\nКонецФункции\nРезультат = МеждуЯчейками(1);')
        await eventually('textDocument/signatureHelp', rootless, lambda r: contains(r, 'Аргумент'),
                         line=3, character=len('Результат = МеждуЯчейками(1'))
        print('PASS rootless built-ins and cross-cell signature', flush=True)
        for configured, module_name in ((designer, 'JupyterBslFixtureCalleeServer'),
                                        (edt, 'ProbeServer'), (edt / 'src', 'ProbeServer')):
            root, reason = normalize_source_root(configured)
            assert root and not reason
            layout = 'Designer' if configured == designer else 'EDT-parent' if configured == edt else 'EDT-src'
            module = root / 'CommonModules' / module_name / ('Ext/Module.bsl' if layout == 'Designer' else 'Module.bsl')
            oracle = SourceWriteOracle(root)
            original_module = module.read_bytes()
            def save(parameter, symbol='Наблюдаемый', *, atomic=False):
                text = f'Функция {symbol}({parameter}) Экспорт\nВозврат {parameter};\nКонецФункции\n'
                oracle.save(module, text.encode('utf-8'), atomic=atomic)
            save('Первоначальный')
            a, b = make_context(root, 'a' * 32), make_context(root, 'b' * 32)
            prefix = 'Результат = ' + module_name + '.'
            call = prefix + 'Наблюдаемый(1);'
            cold_started = time.perf_counter()
            await gateway.accept_contexts([rootless, a, b])
            await opened(a, call); await opened(b, prefix)
            await completion(a, prefix, 'Наблюдаемый')
            await completion(b, prefix, 'Наблюдаемый')
            await eventually('textDocument/signatureHelp', a, lambda r: contains(r, 'Первоначальный'), character=len(call) - 2)
            cold_seconds = time.perf_counter() - cold_started
            oracle.check()
            original_child = gateway.children[a['binding_id']]
            original_pid = original_child.transport.process.pid
            original_tree = [original_pid, *[p.pid for p in psutil.Process(original_pid).children(recursive=True)]]
            saved_seconds, detection_seconds, service_seconds, completion_seconds = [], [], [], []
            for sample in range(warmup + samples):
                parameter = f'ПараметрИтерации{sample}'
                revision = gateway.contexts[a['binding_id']]['analysis_revision']
                started = time.perf_counter()
                save(parameter, atomic=sample % 2 == 1)
                async with asyncio.timeout(30):
                    while gateway.contexts[a['binding_id']]['analysis_revision'] <= revision:
                        await asyncio.sleep(.005)
                detected = time.perf_counter()
                signature = await eventually('textDocument/signatureHelp', a,
                    lambda r: contains(r, parameter), character=len(call) - 2)
                assert parameter in json.dumps(signature, ensure_ascii=False)
                completed = time.perf_counter()  # Timer stops ONLY after iteration-specific signature.
                warm_started = time.perf_counter()
                await completion(a, prefix, 'Наблюдаемый')
                warm_elapsed = time.perf_counter() - warm_started
                assert gateway.children[a['binding_id']] is original_child, 'ordinary saved edit recreated child'
                assert gateway.children[a['binding_id']].transport.process.pid == original_pid
                if sample >= warmup:
                    saved_seconds.append(completed - started)
                    detection_seconds.append(detected - started)
                    service_seconds.append(completed - detected)
                    completion_seconds.append(warm_elapsed)
            warm_pid_after = gateway.children[a['binding_id']].transport.process.pid
            oracle.check()
            definition = await eventually('textDocument/definition', a, lambda r: contains(r, 'onec-bsl:'),
                                          character=len(prefix) + 3)
            def find_uri(value):
                if isinstance(value, list):
                    return next((found for item in value if (found := find_uri(item))), None)
                if isinstance(value, dict):
                    return value.get('uri') or value.get('targetUri')
            uri = find_uri(definition)
            assert uri and uri.startswith('onec-bsl:') and str(root) not in uri
            class Registry:
                def context(self, binding, *, owner):
                    assert owner == 'protocol-owner' and binding == a['binding_id']
                    return gateway.contexts[binding]
            store = SourceStore(Registry())
            source_path = uri.removeprefix('onec-bsl:')
            assert f'ПараметрИтерации{warmup + samples - 1}' in store.read(source_path, owner='protocol-owner')['content']
            # Real FileEditor wraps the reserved drive path in a file URI. Its
            # refresh/reopen versions must never enter notebook/child inventories.
            documents_before = deepcopy(gateway.documents)
            contexts_before = deepcopy(gateway.contexts)
            children_before = dict(gateway.children)
            opened_before = {key: deepcopy(child.opened) for key, child in gateway.children.items()}
            message_boundary = len(output)
            for alias in ('file:///fixture/' + uri, 'file:///fixture/' + uri.replace(':', '%3A', 1)):
                for cycle in range(2):
                    await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                        'uri': alias, 'languageId': 'bsl', 'version': 0, 'text': 'viewer-only'}}})
                    for version in (1, 2, 0):
                        await gateway.handle({'method': 'textDocument/didChange', 'params': {
                            'textDocument': {'uri': alias, 'version': version}, 'contentChanges': [{'text': 'viewer-refreshed'}]}})
                        assert gateway.documents == documents_before and gateway.children == children_before
                        assert {key: child.opened for key, child in gateway.children.items()} == opened_before
                    await gateway.handle({'method': 'textDocument/didClose', 'params': {'textDocument': {'uri': alias}}})
            assert not any(message.get('method') == 'window/showMessage' for message in output[message_boundary:])
            assert gateway.documents == documents_before and gateway.children == children_before
            assert gateway.contexts == contexts_before
            assert {key: child.opened for key, child in gateway.children.items()} == opened_before
            await completion(a, prefix, 'Наблюдаемый')
            oracle.check()
            # Ordinary noncanonical create/delete stays on watched-files and keeps PID.
            watched = []
            original_notify = original_child.transport.notify
            async def notify(method, params):
                if method == 'workspace/didChangeWatchedFiles': watched.extend(params['changes'])
                return await original_notify(method, params)
            original_child.transport.notify = notify
            extra = root / 'Extra.os'
            oracle.save(extra, 'Функция Свободный(НовыйАргумент) Экспорт\nКонецФункции\n'.encode('utf-8'))
            async with asyncio.timeout(30):
                while not any(e['uri'] == extra.as_uri() and e['type'] == 1 for e in watched):
                    await asyncio.sleep(.01)
            symbol_params = {'textDocument': {'uri': extra.as_uri()}}
            async def extra_symbols(predicate):
                deadline = time.perf_counter() + 30
                while time.perf_counter() < deadline:
                    symbols = await original_child.transport.request('textDocument/documentSymbol', symbol_params)
                    if predicate(symbols): return
                    await asyncio.sleep(.05)
                raise AssertionError(('noncanonical symbols', symbols))
            await extra_symbols(lambda r: contains(r, 'Свободный'))
            oracle.delete(extra)
            async with asyncio.timeout(30):
                while not any(e['uri'] == extra.as_uri() and e['type'] == 3 for e in watched):
                    await asyncio.sleep(.01)
            await extra_symbols(lambda r: not contains(r, 'Свободный'))
            assert gateway.children[a['binding_id']] is original_child
            membership_costs = []
            started = time.perf_counter()
            oracle.delete(module)
            await eventually('textDocument/completion', a,
                lambda r: r is not None and not contains(r, 'Наблюдаемый')
                    and gateway.children.get(a['binding_id']) is not original_child, character=len(prefix))
            membership_costs.append({'change': 'canonical-delete', 'seconds': time.perf_counter() - started})
            try: store.read(source_path, owner='protocol-owner')
            except ValueError: pass
            else: raise AssertionError('deleted definition retained old bytes')
            started = time.perf_counter()
            save('ПослеСоздания', 'Созданный')
            await completion(a, prefix, 'Созданный')
            membership_costs.append({'change': 'canonical-create', 'seconds': time.perf_counter() - started})
            await opened(a, prefix + 'Созданный(1);')
            prior_rename = gateway.children[a['binding_id']]
            renamed = module.with_name('Renamed.bsl')
            oracle.rename(module, renamed)
            await eventually('textDocument/completion', a,
                lambda r: r is not None and not contains(r, 'Созданный')
                    and gateway.children.get(a['binding_id']) is not prior_rename, character=len(prefix))
            try: store.read(source_path, owner='protocol-owner')
            except ValueError: pass
            else: raise AssertionError('noncanonical rename retained stale canonical source')
            oracle.rename(renamed, module)
            await completion(a, prefix, 'Созданный')
            new_name = module_name + 'Renamed'
            prior_rename = gateway.children[a['binding_id']]
            started = time.perf_counter()
            new_module = rename_module(root, module_name, new_name, layout, oracle)
            new_prefix = 'Результат = ' + new_name + '.'
            await opened(a, new_prefix + 'Созданный(1);')
            current_definition = await eventually('textDocument/definition', a,
                lambda r: contains(r, '/' + new_name + '/') and not contains(r, '/' + module_name + '/'),
                character=len(new_prefix) + 3)
            assert gateway.children[a['binding_id']] is not prior_rename
            current_uri = find_uri(current_definition)
            assert store.read(current_uri.removeprefix('onec-bsl:'), owner='protocol-owner')['content'] == new_module.read_bytes().decode('utf-8-sig')
            membership_costs.append({'change': 'valid-metadata-rename', 'seconds': time.perf_counter() - started})
            oracle.check()
            def summary(values):
                return summarize_durations(values, elapsed_s=sum(values), byte_count=0)
            measurements.append({
                'layout': layout, 'warmup': warmup, 'samples': samples,
                'backend': 'Windows native signal + metadata diff' if os.name == 'nt' else 'bounded metadata polling',
                'cold_index_and_two_completions_ms': cold_seconds * 1000,
                'save_to_iteration_signature': summary(saved_seconds),
                'save_to_observer_detection': summary(detection_seconds),
                'detection_to_iteration_signature': summary(service_seconds),
                'warm_completion': summary(completion_seconds),
                'measurement_poll_ms': {'detection': 5, 'result': 50},
                'child_pid_before': original_pid, 'child_pid_after': warm_pid_after,
                'source_viewer_excluded_from_gateway_and_children': True,
                'canonical_membership_reindex': membership_costs,
                'child_tree_rss_bytes': sum(p.memory_info().rss for p in
                    [psutil.Process(gateway.children[a['binding_id']].transport.process.pid),
                     *psutil.Process(gateway.children[a['binding_id']].transport.process.pid).children(recursive=True)]),
            })
            oracle.check()
            print(f'PASS {layout}: saved signatures, deletion/create/rename, current definition, stable PID', flush=True)
            for c in (a, b):
                await gateway.handle({'method': 'textDocument/didClose', 'params': {'textDocument': {'uri': c['document_uri']}}})
                versions.pop(c['document_uri'], None)
            await gateway.accept_contexts([rootless])
            rename_module(root, new_name, module_name, layout, oracle)
            oracle.save(module, original_module)
        return {'checks': ['rootless', 'cross-cell', 'Designer', 'EDT-parent', 'EDT-src',
                           'saved-signature', 'delete-create-rename', 'current-definition', 'stable-pid',
                           'source-viewer-excluded-from-replay', 'no-product-source-writes'],
                'measurements': measurements, 'scan_measurements': scans, 'status_count': len(statuses),
                'limits': ['No native remote-filesystem guarantee', 'metadata-preserving edits cannot be located; native empty diff reindexes',
                           'notification send is not indexing completion', 'runtime overhead budget requires separate live Task5 measurement']}
    finally:
        await gateway.close()


async def probe_rename(server, designer, edt):
    """Owned-fixture investigation; observations are not acceptance PASS results."""
    from onec_runtime_jupyter.lsp_child import ChildSession
    from onec_runtime_jupyter.lsp_gateway import Gateway
    observations = []
    for root, name, layout in ((designer, 'JupyterBslFixtureCalleeServer', 'Designer'),
                               (edt / 'src', 'ProbeServer', 'EDT')):
        module = root / 'CommonModules' / name / ('Ext/Module.bsl' if layout == 'Designer' else 'Module.bsl')
        text = 'Функция Созданный(Аргумент) Экспорт\nВозврат Аргумент;\nКонецФункции\n'
        module.write_text(text, encoding='utf-8')
        renamed = module.with_name('Renamed.bsl'); module.rename(renamed)
        c = make_context(root, 'c' * 32)
        child = ChildSession(c, lambda _: None, command=[str(server)])
        call = f'Результат = {name}.Созданный(1);'
        try:
            await child.synchronize(c)
            await child.notify('textDocument/didOpen', {'textDocument': {
                'uri': c['document_uri'], 'version': 1, 'languageId': 'bsl', 'text': call}})
            params = {'textDocument': {'uri': child.mapper.virtual_uri},
                      'position': {'line': 0, 'character': call.index('Созданный') + 3}}
            # Capture repeated bounded observations, explicitly not a sleep-based indexing proof.
            results = []
            for _ in range(3):
                raw = await child.transport.request('textDocument/definition', params)
                completion = await child.transport.request('textDocument/completion', params)
                results.append({'definition': raw, 'completion': completion})
                await asyncio.sleep(.5)
            observations.append({'layout': layout, 'probe': 'fresh-index-noncanonical-filename', 'results': results})
        finally: await child.close()
        renamed.rename(module)
        output = []; gateway = Gateway(output.append, command=[str(server)])
        try:
            await gateway.accept_contexts([c])
            await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                'uri': c['document_uri'], 'version': 1, 'languageId': 'bsl', 'text': call}}})
            original = gateway.children[c['binding_id']]
            new_name = name + 'Renamed'
            directory = module.parent.parent if layout == 'Designer' else module.parent
            target = directory.with_name(new_name)
            directory.rename(target)
            metadata = root / 'CommonModules' / (name + '.xml') if layout == 'Designer' else target / (name + '.mdo')
            metadata.write_text(metadata.read_text(encoding='utf-8').replace(name, new_name), encoding='utf-8')
            metadata.rename(metadata.with_name(new_name + metadata.suffix))
            config = root / 'Configuration.xml' if layout == 'Designer' else root / 'Configuration/Configuration.mdo'
            config.write_text(config.read_text(encoding='utf-8').replace(name, new_name), encoding='utf-8')
            await gateway.handle({'method': 'textDocument/didChange', 'params': {
                'textDocument': {'uri': c['document_uri'], 'version': 2},
                'contentChanges': [{'text': call.replace(name, new_name)}]}})
            deadline = time.perf_counter() + 30; result = None
            while time.perf_counter() < deadline:
                await gateway.handle({'id': 1, 'method': 'textDocument/definition', 'params': {
                    'textDocument': {'uri': c['document_uri']}, 'position': {
                        'line': 0, 'character': call.replace(name, new_name).index('Созданный') + 3}}})
                result = next(m for m in reversed(output) if m.get('id') == 1)
                if new_name in json.dumps(result, ensure_ascii=False): break
                await asyncio.sleep(.05)
            observations.append({'layout': layout, 'probe': 'valid-metadata-rename',
                'child_recreated': gateway.children[c['binding_id']] is not original, 'result': result})
        finally: await gateway.close()
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=Path('tests/fixtures/onec/JupyterBslTestFixture'))
    parser.add_argument('--output', type=Path, default=Path('artifacts/bsl-lsp'))
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--warmup', type=int, default=3)
    parser.add_argument('--rename-probe', action='store_true')
    args = parser.parse_args()
    if args.samples < 1 or args.warmup < 0:
        parser.error('samples must be positive and warmup nonnegative')
    args.output.mkdir(parents=True, exist_ok=True)
    original = fingerprints(args.source_root.resolve())
    with TemporaryDirectory(prefix='onec-source-protocol-') as temporary:
        owned = Path(temporary)
        designer, edt = owned / 'designer', owned / 'edt'
        shutil.copytree(args.source_root.resolve(), designer)
        create_edt_fixture(edt)
        if args.rename_probe:
            evidence = asyncio.run(probe_rename(args.server.resolve(), designer, edt))
            print(json.dumps(evidence, ensure_ascii=True), flush=True)
        else:
            evidence = asyncio.run(exercise(args.server.resolve(), designer, edt,
                samples=args.samples, warmup=args.warmup))
    assert fingerprints(args.source_root.resolve()) == original, 'read-only fixture input changed'
    if not args.rename_probe:
        from check_jupyter_lsp_zup import protocol_identity
        evidence.update(status='PASS', source_tree_unchanged=True,
                        binary_sha256=sha256(args.server.resolve().read_bytes()).hexdigest().upper(),
                        implementation=protocol_identity())
    (args.output / 'evidence.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
