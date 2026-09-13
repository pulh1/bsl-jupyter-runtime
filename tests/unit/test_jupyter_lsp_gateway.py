"""Isolation tests use a controllable child transport, not a shared fake gateway."""
import asyncio
from copy import deepcopy
from importlib import import_module, util

import pytest


def api():
    assert util.find_spec('onec_runtime_jupyter.lsp_gateway'), 'isolated gateway missing'
    return import_module('onec_runtime_jupyter.lsp_gateway')


def context(binding='a', epoch=1, text='A'):
    return dict(binding_id=binding, epoch=epoch, document_uri=f'file:///{binding}.bsl',
                kernel_id='shared', kernel_incarnation='inc', source_root=None,
                installation_id='runtime', marker=text)


class Child:
    def __init__(self, context, emit, **kwargs):
        self.context = context
        self.emit = emit
        self.requests = []
        self.notifications = []
        self.pending = {}
        self.closed = False

    async def synchronize(self, context): self.context = deepcopy(context)
    async def files_changed(self, context, change):
        self.context = deepcopy(context)
        self.notifications.append(('workspace/didChangeWatchedFiles', {'changes': list(change.events)}))
    async def notify(self, method, params): self.notifications.append((method, params))
    async def request(self, method, params):
        self.requests.append((method, params))
        if method == 'textDocument/hover':
            future = asyncio.get_running_loop().create_future()
            self.pending[method] = future
            return await future
        if method == 'textDocument/completion': return [{'label': self.context['marker'], 'data': {'child': self.context['marker']}}]
        if method == 'completionItem/resolve': return {**params, 'detail': params['data']['child']}
        return []
    async def close(self): self.closed = True


@pytest.mark.parametrize('settings', [
    {}, {'source_root': 'file:///untrusted', 'diagnostics': {'enabled': False}},
    None, True, 7, 'client-preference', ['client-preference'],
])
def test_ordinary_configuration_notifications_are_consumed_without_changing_children(settings):
    async def run():
        output = []
        gateway = api().Gateway(output.append, child_factory=Child)
        notification = {'method': 'workspace/didChangeConfiguration', 'params': {'settings': settings}}
        try:
            await gateway.accept_contexts([context()])
            original_contexts = deepcopy(gateway.contexts)
            await gateway.handle(notification)
            assert output == [], 'ordinary settings must not produce a modal or response'
            assert not gateway.children and not gateway.documents and not gateway.requests
            assert gateway.contexts == original_contexts

            await gateway.handle({'id': 1, 'method': 'initialize', 'params': {}})
            assert output[-1]['id'] == 1 and 'capabilities' in output[-1]['result']
            await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                'uri': 'file:///a.bsl', 'version': 1, 'languageId': 'bsl', 'text': 'A'}}})
            child = gateway.children['a']
            forwarded = deepcopy(child.notifications)
            output.clear()
            await gateway.handle(notification)
            assert output == [] and child.notifications == forwarded
            assert gateway.children == {'a': child} and gateway.contexts == original_contexts
            assert gateway.documents['file:///a.bsl']['text'] == 'A'
            await gateway.handle({'id': 2, 'method': 'textDocument/completion',
                                  'params': {'textDocument': {'uri': 'file:///a.bsl'}}})
            assert output[-1]['id'] == 2 and output[-1]['result'][0]['label'] == 'A'
            assert not gateway.requests
        finally:
            await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize('message', [
    {'method': 'workspace/didChangeConfiguration'},
    {'method': 'workspace/didChangeConfiguration', 'params': None},
    {'method': 'workspace/didChangeConfiguration', 'params': []},
    {'method': 'workspace/didChangeConfiguration', 'params': {}},
    {'method': 'workspace/didChangeConfiguration', 'params': {'settings': {'onecProjectBinding': {}}}},
    {'id': 7, 'method': 'workspace/didChangeConfiguration', 'params': {'settings': {}}},
    {'id': 'request', 'method': 'workspace/didChangeConfiguration', 'params': {'settings': {}}},
    {'id': None, 'method': 'workspace/didChangeConfiguration', 'params': {'settings': {}}},
    {'method': 'workspace/unknown'},
    {'id': 9, 'method': 'workspace/unknown'},
])
def test_configuration_handling_preserves_malformed_private_request_and_unknown_rejections(message):
    async def run():
        output = []
        gateway = api().Gateway(output.append, child_factory=Child)
        try:
            await gateway.handle(message)
            if message.get('id') is not None:
                assert output == [{'jsonrpc': '2.0', 'id': message['id'],
                                   'error': {'code': -32601, 'message': 'method-not-supported'}}]
            else:
                assert output == [{'jsonrpc': '2.0', 'method': 'window/showMessage',
                                   'params': {'type': 2, 'message': 'method-not-supported'}}]
            assert not gateway.children and not gateway.documents and not gateway.contexts
            assert not gateway.requests
        finally:
            await gateway.close()
    asyncio.run(run())


def test_observer_shutdown_timeout_still_reclaims_owned_children_and_requests():
    import sys
    from onec_runtime_jupyter.lsp_child import ChildSession
    from onec_runtime_jupyter.lsp_workspace import WorkspaceWatchService

    async def run():
        error = TimeoutError('workspace-shutdown-timeout')
        class FailingObserver(WorkspaceWatchService):
            async def close(self):
                await super().close()
                raise error
        gateway = api().Gateway(lambda _: None, workspace=FailingObserver())
        children = [ChildSession(context(name), lambda _: None,
            command=[sys._base_executable, '-c', 'import time; time.sleep(60)'])
            for name in ('a', 'b')]
        pending = asyncio.create_task(asyncio.Event().wait())
        try:
            for child in children:
                await child.transport.start(None)
                gateway.children[child.context['binding_id']] = child
            gateway.requests[1] = pending
            gateway.applied['a'] = ('retained',)
            gateway.resolves['retained'] = ('a',)
            with pytest.raises(TimeoutError, match='workspace-shutdown-timeout') as raised:
                await gateway.close()
            assert raised.value is error
            assert all(child.transport.process.poll() is not None for child in children)
            assert all(child.closed and not child.transport.io_tasks for child in children)
            assert pending.cancelled() or pending.cancelling()
            assert not gateway.children and not gateway.applied and not gateway.resolves
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await asyncio.gather(*(child.close() for child in children))
    asyncio.run(run())


@pytest.mark.parametrize('observer_fails', [False, True])
def test_child_shutdown_failure_remains_owned_without_bypassing_other_children(observer_fails):
    from onec_runtime_jupyter.lsp_workspace import WorkspaceWatchService

    async def run():
        error = TimeoutError('child-io-termination-timeout')
        observer_error = TimeoutError('workspace-shutdown-timeout')
        class FailingChild(Child):
            async def close(self):
                raise error
        class Observer(WorkspaceWatchService):
            async def close(self):
                await super().close()
                if observer_fails:
                    raise observer_error
        gateway = api().Gateway(lambda _: None, child_factory=Child, workspace=Observer())
        failed = FailingChild(context(), lambda _: None)
        sibling = Child(context('b'), lambda _: None)
        gateway.children.update(a=failed, b=sibling)
        expected_error = observer_error if observer_fails else error
        with pytest.raises(TimeoutError, match=str(expected_error)) as raised:
            await gateway.close()
        assert raised.value is expected_error
        assert sibling.closed
        assert gateway.children == {'a': failed}
    asyncio.run(run())


def test_saved_file_reuses_child_and_shared_observer_then_layout_recreates(tmp_path):
    from onec_runtime_jupyter.lsp_workspace import WorkspaceWatchService, scan_workspace
    async def run():
        output = []; scans = []; statuses = []
        def scan(root, **kwargs):
            scans.append(root); return scan_workspace(root, **kwargs)
        service = WorkspaceWatchService(scan=scan, poll_seconds=.05)
        gateway = api().Gateway(output.append, child_factory=Child, workspace=service, status=statuses.append)
        module = tmp_path / 'Module.bsl'; module.write_text('first')
        a, b = {**context(), 'source_root': str(tmp_path)}, {**context('b'), 'source_root': str(tmp_path)}
        try:
            await gateway.accept_contexts([a, b])
            for c in (a, b):
                await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                    'uri': c['document_uri'], 'version': 1, 'languageId': 'bsl', 'text': c['binding_id']}}})
            assert len(scans) == 1
            assert statuses[-1]['state'] == 'ready' and statuses[-1]['reason'] == 'index-convergence-unconfirmed'
            first = dict(gateway.children)
            await gateway.accept_contexts([a, b])
            assert len(scans) == 1
            module.write_text('saved file second')
            async with asyncio.timeout(5):
                while not all(any(m == 'workspace/didChangeWatchedFiles' for m, _ in c.notifications) for c in first.values()):
                    await asyncio.sleep(.02)
            assert gateway.children == first
            assert statuses[-1]['state'] == 'ready' and statuses[-1]['reason'] == 'index-convergence-unconfirmed'
            assert gateway.contexts['a']['analysis_revision'] == gateway.contexts['b']['analysis_revision'] == 1
            (tmp_path / 'Configuration.xml').write_text('<Configuration/>')
            async with asyncio.timeout(5):
                while any(gateway.children.get(key) is c for key, c in first.items()):
                    await asyncio.sleep(.02)
            assert all(c.closed for c in first.values())
            assert all(any(m == 'textDocument/didOpen' for m, _ in c.notifications) for c in gateway.children.values())
            assert set(p.name for p in tmp_path.iterdir()) == {'Module.bsl', 'Configuration.xml'}
        finally: await gateway.close()
    asyncio.run(run())


def test_source_viewer_cannot_become_project_overlay():
    async def run():
        gateway = api().Gateway(lambda _: None, child_factory=Child)
        try:
            await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                'uri': 'onec-bsl:binding/token/Module.bsl', 'version': 1, 'languageId': 'bsl', 'text': 'source'}}})
            assert gateway.documents == {} and gateway.children == {}
        finally: await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize('uri', [
    'onec-bsl:binding/token/Module.bsl',
    'file:///fixture/onec-bsl:binding/token/CommonModules/Test/Module.bsl',
    'file:///fixture/onec-bsl%3Abinding/token/CommonModules/Test/Module.bsl',
    'file:///fixture/onec%2Dbsl%3abinding/token/Module.bsl',
    'file:///fixture/onec-bsl:/missing-token.bsl',
    'file:///fixture/onec-bsl:malformed/%ZZ/../Module.bsl',
    'file:///fixture/onec-bsl%3Abad/%2e%2e/Module.bsl?ignored#ignored',
])
def test_reserved_viewer_alias_never_enters_document_replay(uri):
    async def run():
        messages = []
        gateway = api().Gateway(messages.append, child_factory=Child)
        try:
            for cycle in range(2):
                await gateway.handle({'method': 'textDocument/didOpen', 'params': {
                    'textDocument': {'uri': uri, 'languageId': 'bsl', 'version': 0, 'text': 'A'}}})
                assert not gateway.documents and not gateway.children and not messages
                for version in (1, 2, 0):
                    await gateway.handle({'method': 'textDocument/didChange', 'params': {
                        'textDocument': {'uri': uri, 'version': version}, 'contentChanges': [{'text': 'B'}]}})
                    assert not gateway.documents and not gateway.children and not messages
                await gateway.handle({'method': 'textDocument/didClose', 'params': {'textDocument': {'uri': uri}}})
            assert not gateway.documents and not gateway.children and not messages
            assert not gateway.subscriptions and not gateway.workspace.watches
            # Even an incorrectly supplied reserved context cannot grant reads.
            await gateway.accept_contexts([{**context(), 'document_uri': uri}])
            await gateway.handle({'id': 1, 'method': 'textDocument/completion',
                                  'params': {'textDocument': {'uri': uri}}})
            assert messages[-1]['error']['message'] == 'source-viewer-read-only'
            assert not gateway.documents and not gateway.children
        finally:
            await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize('uri', [
    'file:///fixture/xonec-bsl:binding/Module.bsl',
    'file:///fixture/onec-bsl-extra:binding/Module.bsl',
    'file:///fixture/onec-bsl/Module.bsl',
    'file:///fixture/onec-bsl%253Abinding/Module.bsl',
    'file:///fixture/ordinary.bsl?label=onec-bsl:binding',
])
def test_near_match_notebook_uri_keeps_replay_and_strict_versions(uri):
    async def run():
        messages = []
        gateway = api().Gateway(messages.append, child_factory=Child)
        document = {'uri': uri, 'languageId': 'bsl', 'version': 2, 'text': 'current'}
        try:
            await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': document}})
            assert gateway.documents[uri] == document
            for bound in (False, True):
                if bound:
                    await gateway.accept_contexts([{**context(), 'document_uri': uri}])
                messages.clear()
                for version, text in ((2, 'current'), (1, 'stale'), (2, 'conflicting')):
                    await gateway.handle({'method': 'textDocument/didChange', 'params': {
                        'textDocument': {'uri': uri, 'version': version}, 'contentChanges': [{'text': text}]}})
                assert gateway.documents[uri] == document
                assert len(messages) == 2, 'only identical duplicate is a no-op; stale/conflicting remain rejected'
            assert len(gateway.children) == 1
        finally:
            await gateway.close()
    asyncio.run(run())


def test_recreated_child_cannot_return_old_same_epoch_request_or_resolve():
    async def run():
        output = []; gateway = api().Gateway(output.append, child_factory=Child)
        try:
            await gateway.accept_contexts([context()])
            await gateway.handle({'id': 1, 'method': 'textDocument/completion', 'params': {
                'textDocument': {'uri': 'file:///a.bsl'}}})
            item = deepcopy(output[-1]['result'][0])
            pending = asyncio.create_task(gateway.handle({'id': 2, 'method': 'textDocument/hover',
                'params': {'textDocument': {'uri': 'file:///a.bsl'}}}))
            await asyncio.sleep(0)
            old = gateway.children['a']; old.available = False
            await gateway.handle({'id': 3, 'method': 'textDocument/completion', 'params': {
                'textDocument': {'uri': 'file:///a.bsl'}}})
            old.pending['textDocument/hover'].set_result({'contents': 'old process'})
            await pending
            assert next(m for m in output if m.get('id') == 2) == {'jsonrpc': '2.0', 'id': 2, 'result': None}
            await gateway.handle({'id': 4, 'method': 'completionItem/resolve', 'params': item})
            assert 'error' in output[-1]
        finally: await gateway.close()
    asyncio.run(run())


def test_config_identity_fences_response_before_delayed_other_child_sync(tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class Delayed(Child):
            async def synchronize(self, current):
                if current['binding_id'] == 'a' and current['epoch'] == 2:
                    entered.set(); await release.wait()
                await super().synchronize(current)
        output = []; gateway = api().Gateway(output.append, child_factory=Delayed)
        try:
            await gateway.accept_contexts([context(), context('b')])
            for name in ('a', 'b'):
                await gateway.handle({'id': name, 'method': 'textDocument/completion', 'params': {
                    'textDocument': {'uri': f'file:///{name}.bsl'}}})
            pending = asyncio.create_task(gateway.handle({'id': 1, 'method': 'textDocument/hover',
                'params': {'textDocument': {'uri': 'file:///b.bsl'}}}))
            await asyncio.sleep(0)
            old = gateway.children['b']
            updating = asyncio.create_task(gateway.accept_contexts([
                context(epoch=2), {**context('b'), 'source_root': str(tmp_path)}]))
            await entered.wait()
            old.pending['textDocument/hover'].set_result({'contents': 'old config'})
            await pending
            assert next(m for m in output if m.get('id') == 1) == {'jsonrpc': '2.0', 'id': 1, 'result': None}
            release.set(); await updating
        finally: release.set(); await gateway.close()
    asyncio.run(run())


def test_new_ambiguous_metadata_layout_revalidates_before_reindexing(tmp_path):
    from onec_runtime_jupyter.lsp_child import ChildSession
    from test_jupyter_lsp_child import Transport
    async def run():
        (tmp_path / 'CommonModules').mkdir()
        source = tmp_path / 'extra.os'
        source.write_text('initial source')
        statuses = []
        c = {**context(), 'source_root': str(tmp_path)}
        def factory(context, emit, **kwargs):
            kwargs.pop('command', None)
            return ChildSession(context, emit, transport=Transport(), **kwargs)
        gateway = api().Gateway(lambda _: None, child_factory=factory, status=statuses.append)
        try:
            await gateway.accept_contexts([c])
            await gateway.handle({'id': 1, 'method': 'textDocument/completion', 'params': {
                'textDocument': {'uri': c['document_uri']}}})
            old = gateway.children['a']
            assert old.mapper.root == tmp_path
            (tmp_path / 'src/CommonModules').mkdir(parents=True)
            async with asyncio.timeout(4):
                while gateway.children.get('a') is old:
                    await asyncio.sleep(.02)
                while gateway.applied.get('a') is None:
                    await asyncio.sleep(.02)
            assert gateway.children['a'].mapper.root is None
            assert gateway.children['a'].degraded_reason == 'workspace-unsafe'
            unavailable = {'binding_id': 'a', 'epoch': 1, 'state': 'unavailable', 'reason': 'workspace-unsafe'}
            assert statuses[-1] == unavailable
            fallback = gateway.children['a']
            revision = gateway.contexts['a']['analysis_revision']
            source.write_text('saved ordinary source after layout fallback')
            async with asyncio.timeout(4):
                while gateway.contexts['a']['analysis_revision'] == revision or gateway.update_tasks:
                    await asyncio.sleep(.02)
            assert gateway.children['a'] is fallback and fallback.mapper.root is None
            assert not any(method == 'workspace/didChangeWatchedFiles' for method, _ in fallback.transport.events)
            assert statuses[-1] == unavailable
        finally: await gateway.close()
    asyncio.run(run())


def test_real_child_diagnostics_are_fenced_globally_before_sequential_sync():
    from onec_runtime_jupyter.lsp_child import ChildSession
    from test_jupyter_lsp_child import Transport

    async def run():
        output = []; entered = asyncio.Event(); release = asyncio.Event()
        class DiagnosticTransport(Transport):
            async def request(self, method, params):
                if method == 'textDocument/diagnostic':
                    if params['textDocument']['uri'].endswith('.bsl') and not release.is_set():
                        entered.set(); await release.wait()
                    return {'kind': 'full', 'items': [{'message': 'current-or-stale'}]}
                return await super().request(method, params)
        def factory(c, emit, **kwargs):
            return ChildSession(c, emit, transport=DiagnosticTransport())
        gateway = api().Gateway(output.append, child_factory=factory)
        try:
            await gateway.accept_contexts([context(), context('b')])
            for name in ('a', 'b'):
                await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                    'uri': f'file:///{name}.bsl', 'languageId': 'bsl', 'version': 1, 'text': name}}})
            # Pause A's synchronization while B still has its old applied source fence.
            await entered.wait()
            a, b = gateway.children['a'], gateway.children['b']
            old_diagnostic = b.diagnostics['file:///b.bsl']
            update = asyncio.create_task(gateway.accept_contexts([context(epoch=2), context('b', epoch=2)]))
            await asyncio.sleep(0)
            assert gateway.contexts['b']['epoch'] == 2 and b.context['epoch'] == 1
            release.set()
            await old_diagnostic
            assert not output, 'B emitted old diagnostics while A synchronization was pending'
            await update
            await asyncio.gather(*(c.diagnostics[c.context['document_uri']] for c in (a, b)))
            assert {m['params']['uri'] for m in output} == {'file:///a.bsl', 'file:///b.bsl'}
            old_emit = b.emit
            event = deepcopy(output[-1]); event['params']['uri'] = 'file:///b.bsl'
            output.clear()
            await gateway.accept_contexts([context(epoch=2)])
            await old_emit(event)
            assert not output, 'removed producer emitted'
            await gateway.accept_contexts([context(epoch=2), context('b', epoch=2)])
            await old_emit(event)
            assert not output, 'recreated binding accepted its old child producer'
        finally:
            release.set()
            await gateway.close()
    asyncio.run(run())


def test_same_root_contexts_and_resolve_are_isolated_and_unbound_fails_closed():
    async def run():
        output = []
        gateway = api().Gateway(output.append, child_factory=Child)
        await gateway.accept_contexts([context(), context('b', text='B')])
        for name in ('a', 'b'):
            await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                'uri': f'file:///{name}.bsl', 'languageId': 'bsl', 'version': 1, 'text': name}}})
            await gateway.handle({'id': name, 'method': 'textDocument/completion', 'params': {'textDocument': {'uri': f'file:///{name}.bsl'}}})
        assert output[0]['result'][0]['label'] == 'A'
        assert output[1]['result'][0]['label'] == 'B'
        assert gateway.context_for('file:///a.bsl') != gateway.context_for('file:///b.bsl')
        await gateway.handle({'id': 3, 'method': 'completionItem/resolve', 'params': output[0]['result'][0]})
        assert output[-1]['result']['detail'] == 'A'
        for method, params in [('textDocument/hover', {'textDocument': {'uri': 'file:///forged.bsl'}}),
                               ('workspace/executeCommand', {'command': 'write'}),
                               ('completionItem/resolve', {'data': {'onec': 'forged'}})]:
            await gateway.handle({'id': 4, 'method': method, 'params': params})
            assert 'error' in output[-1]
        await gateway.close()
    asyncio.run(run())


def test_epoch_fences_late_results_without_restarting_same_runtime_child_and_cancel_routes():
    async def run():
        output = []
        gateway = api().Gateway(output.append, child_factory=Child)
        await gateway.accept_contexts([context()])
        await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
            'uri': 'file:///a.bsl', 'languageId': 'bsl', 'version': 1, 'text': 'local'}}})
        request = dict(id=1, method='textDocument/hover', params={'textDocument': {'uri': 'file:///a.bsl'}})
        pending = asyncio.create_task(gateway.handle(request))
        await asyncio.sleep(0)
        child = gateway.children['a']
        await gateway.accept_contexts([context(epoch=2)])
        child.pending['textDocument/hover'].set_result({'contents': 'stale'})
        await pending
        assert output[-1] == {'jsonrpc': '2.0', 'id': 1, 'result': None}
        assert gateway.children['a'] is child
        pending = asyncio.create_task(gateway.handle({**request, 'id': 2}))
        await asyncio.sleep(0)
        await gateway.handle({'method': '$/cancelRequest', 'params': {'id': 2}})
        await pending
        assert output[-1] == {'jsonrpc': '2.0', 'id': 2, 'result': None}
        assert not gateway.requests and not gateway.request_bindings
        pending = asyncio.create_task(gateway.handle({**request, 'id': 3}))
        await asyncio.sleep(0)
        child.pending['textDocument/hover'].set_result({'contents': 'current'})
        await pending
        assert output[-1] == {'jsonrpc': '2.0', 'id': 3, 'result': {'contents': 'current'}}
        await gateway.close()
    asyncio.run(run())


def test_child_limit_fails_visibly_and_shared_kernel_notebook_locals_stay_separate():
    async def run():
        output = []
        gateway = api().Gateway(output.append, child_factory=Child, max_children=1)
        await gateway.accept_contexts([context(), context('b')])
        for name in ('a', 'b'):
            await gateway.handle({'id': name, 'method': 'textDocument/completion', 'params': {'textDocument': {'uri': f'file:///{name}.bsl'}}})
        assert output[-1]['error']['message'] == 'child-capacity-unavailable'
        assert len(gateway.children) == 1
        await gateway.close()
    asyncio.run(run())
def test_didopen_before_binding_is_replayed_when_authority_arrives():
    async def run():
        output = []
        gateway = api().Gateway(output.append, child_factory=Child)
        await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
            'uri': 'file:///a.bsl', 'languageId': 'bsl', 'version': 1, 'text': 'pending exact'}}})
        assert not gateway.children
        await gateway.accept_contexts([context()])
        assert gateway.children['a'].notifications[-1][1]['textDocument']['text'] == 'pending exact'
        await gateway.close()
    asyncio.run(run())

def test_jupyter_initial_identical_change_is_idempotent_but_conflicting_versions_fail():
    async def run():
        output = []; gateway = api().Gateway(output.append, child_factory=Child)
        document = {'uri': 'file:///a.bsl', 'languageId': 'bsl', 'version': 0, 'text': 'current'}
        await gateway.handle({'method':'textDocument/didOpen', 'params':{'textDocument':document}})
        change = {'method':'textDocument/didChange', 'params':{'textDocument':{'uri':document['uri'], 'version':0},
                  'contentChanges':[{'text':'current'}]}}
        await gateway.handle(change)
        assert output == []
        await gateway.accept_contexts([context()]); child = gateway.children['a']
        before = len(child.notifications)
        await gateway.handle(change)
        assert output == [] and len(child.notifications) == before
        assert gateway.documents[document['uri']]['version'] == 0
        for version, text in [(0, 'conflicting'), (-1, 'current')]:
            await gateway.handle({'method':'textDocument/didChange', 'params':{'textDocument':{'uri':document['uri'], 'version':version},
                                 'contentChanges':[{'text':text}]}})
            assert output[-1]['method'] == 'window/showMessage'
            assert gateway.documents[document['uri']]['text'] == 'current'
        await gateway.close()
    asyncio.run(run())
def test_failed_barrier_recreates_and_replays_only_affected_child():
    async def run():
        class FailingOnce(Child):
            async def synchronize(self, current):
                if self.context['epoch'] != current['epoch']:
                    raise ValueError('barrier-failed')
                await super().synchronize(current)
        gateway = api().Gateway(lambda _: None, child_factory=FailingOnce)
        await gateway.accept_contexts([context(), context('b')])
        for name in ('a', 'b'):
            await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                'uri': f'file:///{name}.bsl', 'languageId': 'bsl', 'version': 1, 'text': name + '-local'}}})
        a, b = gateway.children['a'], gateway.children['b']
        await gateway.accept_contexts([context(epoch=2), context('b')])
        assert a.closed and gateway.children['a'] is not a
        assert gateway.children['b'] is b and not b.closed
        assert gateway.children['a'].notifications[-1][1]['textDocument']['text'] == 'a-local'
        await gateway.close()
    asyncio.run(run())
def test_notebook_version_fences_hover_while_epoch_is_unchanged():
    async def run():
        output = []; gateway = api().Gateway(output.append, child_factory=Child)
        await gateway.accept_contexts([context()])
        await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
            'uri': 'file:///a.bsl', 'languageId': 'bsl', 'version': 1, 'text': 'one'}}})
        task = asyncio.create_task(gateway.handle({'id': 1, 'method': 'textDocument/hover',
            'params': {'textDocument': {'uri': 'file:///a.bsl'}}}))
        await asyncio.sleep(0)
        await gateway.handle({'method': 'textDocument/didChange', 'params': {'textDocument': {
            'uri': 'file:///a.bsl', 'version': 2}, 'contentChanges': [{'text': 'two'}]}})
        gateway.children['a'].pending['textDocument/hover'].set_result({'contents': 'old-text'})
        await task
        assert output[-1] == {'jsonrpc': '2.0', 'id': 1, 'result': None}
        await gateway.close()
    asyncio.run(run())
@pytest.mark.parametrize('method', ['textDocument/hover', 'textDocument/definition', 'textDocument/signatureHelp'])
@pytest.mark.parametrize('invalidation', ['document', 'context', 'child', 'cancel', 'close'])
def test_nullable_hover_preserves_other_method_fences_and_closed_delivery(method, invalidation):
    async def run():
        entered = asyncio.Event()
        pending_result = asyncio.get_running_loop().create_future()
        class Waiting(Child):
            async def request(self, requested, params):
                entered.set()
                return await pending_result
        output = []
        gateway = api().Gateway(output.append, child_factory=Waiting)
        try:
            await gateway.accept_contexts([context()])
            await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                'uri': 'file:///a.bsl', 'languageId': 'bsl', 'version': 1, 'text': 'one'}}})
            pending = asyncio.create_task(gateway.handle({'id': 17, 'method': method,
                'params': {'textDocument': {'uri': 'file:///a.bsl'}}}))
            await entered.wait()
            if invalidation == 'document':
                await gateway.handle({'method': 'textDocument/didChange', 'params': {
                    'textDocument': {'uri': 'file:///a.bsl', 'version': 2}, 'contentChanges': [{'text': 'two'}]}})
            elif invalidation == 'context':
                await gateway.accept_contexts([context(epoch=2)])
            elif invalidation == 'child':
                gateway.children['a'].available = False
                await gateway._child('a')
            elif invalidation == 'cancel':
                await gateway.handle({'method': '$/cancelRequest', 'params': {'id': 17}})
            else:
                await gateway.close()
            if invalidation not in ('cancel', 'close'):
                pending_result.set_result({'contents': 'must never escape'})
            await pending
            assert not gateway.requests and not gateway.request_bindings
            if invalidation == 'close':
                assert output == []
            elif method == 'textDocument/hover':
                assert output == [{'jsonrpc': '2.0', 'id': 17, 'result': None}]
            else:
                assert len(output) == 1 and 'result' not in output[0]
                assert output[0]['error']['code'] == (-32800 if invalidation == 'cancel' else -32801)
        finally:
            await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['generic', 'timeout', 'code-only', 'wrong-code', 'cancel-code',
                                 'context', 'source', 'auth'])
def test_nullable_hover_does_not_suppress_genuine_or_code_only_errors(kind):
    async def run():
        errors = {'generic': ValueError('content-modified'), 'timeout': TimeoutError(),
            'code-only': api().GatewayError('other', -32801),
            'wrong-code': api().GatewayError('content-modified', -32001),
            'cancel-code': api().GatewayError('request-cancelled', -32800),
            'context': api().GatewayError('document-binding-unavailable'),
            'source': api().GatewayError('source-viewer-read-only'),
            'auth': api().GatewayError('source-access-denied')}
        class Failing(Child):
            async def request(self, method, params):
                raise errors[kind]
        output = []
        gateway = api().Gateway(output.append, child_factory=Failing)
        try:
            await gateway.accept_contexts([context()])
            await gateway.handle({'id': 'hover', 'method': 'textDocument/hover',
                'params': {'textDocument': {'uri': 'file:///a.bsl'}}})
            assert len(output) == 1 and 'result' not in output[0] and output[0]['id'] == 'hover'
            assert output[0]['error']['code'] == getattr(errors[kind], 'code', -32001)
            assert not gateway.requests and not gateway.request_bindings
        finally:
            await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize('request_id', [None, False, 1.5])
@pytest.mark.parametrize('cancelled', [False, True])
def test_nullable_hover_requires_a_real_request_id(request_id, cancelled):
    async def run():
        class Failing(Child):
            async def request(self, method, params):
                if cancelled:
                    raise asyncio.CancelledError()
                raise api().GatewayError('content-modified', -32801)
        output = []
        gateway = api().Gateway(output.append, child_factory=Failing)
        try:
            await gateway.accept_contexts([context()])
            await gateway.handle({'id': request_id, 'method': 'textDocument/hover',
                'params': {'textDocument': {'uri': 'file:///a.bsl'}}})
            assert len(output) == 1 and 'result' not in output[0]
            assert not gateway.requests and not gateway.request_bindings
        finally:
            await gateway.close()
    asyncio.run(run())


def test_idle_child_is_released_but_open_notebook_is_preserved():
    async def run():
        gateway = api().Gateway(lambda _: None, child_factory=Child, idle_seconds=0)
        await gateway.accept_contexts([context()])
        await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
            'uri': 'file:///a.bsl', 'languageId': 'bsl', 'version': 1, 'text': 'one'}}})
        child = gateway.children['a']
        await gateway.cleanup_idle()
        assert not child.closed
        await gateway.handle({'method': 'textDocument/didClose', 'params': {'textDocument': {'uri': 'file:///a.bsl'}}})
        await gateway.cleanup_idle()
        assert child.closed and not gateway.children
        await gateway.close()
    asyncio.run(run())
def test_pending_document_changes_cannot_bypass_total_memory_limit(monkeypatch):
    async def run():
        m = api(); monkeypatch.setattr(m, 'DEFAULT_MAX_DOCUMENT_BYTES', 6)
        messages = []; gateway = m.Gateway(messages.append, child_factory=Child)
        for name in ('a', 'b'):
            await gateway.handle({'method': 'textDocument/didOpen', 'params': {'textDocument': {
                'uri': f'file:///{name}.bsl', 'languageId': 'bsl', 'version': 1, 'text': '123'}}})
        await gateway.handle({'method': 'textDocument/didChange', 'params': {'textDocument': {
            'uri': 'file:///a.bsl', 'version': 2}, 'contentChanges': [{'text': '1234'}]}})
        assert messages[-1]['params']['message'] == 'document-capacity-unavailable'
        assert gateway.documents['file:///a.bsl']['text'] == '123'
        await gateway.close()
    asyncio.run(run())
def test_new_epoch_never_uses_unsynchronized_second_child_while_first_barrier_waits():
    async def run(replace_identity):
        entered, release = asyncio.Event(), asyncio.Event()
        class DelayedA(Child):
            async def synchronize(self, current):
                if current['binding_id'] == 'a' and current['epoch'] == 2:
                    entered.set(); await release.wait()
                await super().synchronize(current)
        output = []; gateway = api().Gateway(output.append, child_factory=DelayedA)
        await gateway.accept_contexts([context(), context('b', text='OLD')])
        for name in ('a', 'b'):
            await gateway.handle({'id': name, 'method': 'textDocument/completion', 'params': {'textDocument': {'uri': f'file:///{name}.bsl'}}})
        original = gateway.children['b']
        next_b = context('b', epoch=2, text='NEW')
        if replace_identity: next_b['installation_id'] = 'replacement'
        updating = asyncio.create_task(gateway.accept_contexts([context(epoch=2), next_b]))
        await entered.wait()
        pending = asyncio.create_task(gateway.handle({'id': 10, 'method': 'textDocument/completion',
            'params': {'textDocument': {'uri': 'file:///b.bsl'}}}))
        await asyncio.sleep(.03)
        release.set()
        await asyncio.gather(updating, pending)
        response = next(m for m in output if m.get('id') == 10)
        assert response['result'][0]['label'] == 'NEW'
        assert (gateway.children['b'] is original) is (not replace_identity)
        if replace_identity: assert original.closed
        await gateway.close()
    asyncio.run(run(False)); asyncio.run(run(True))


def test_completion_retention_expires_edits_and_evicts_old_resolves_without_disabling_results():
    async def run():
        output = []; gateway = api().Gateway(output.append, child_factory=Child, max_resolve_items=2)
        await gateway.accept_contexts([context()])
        first_item = None
        for version in range(1, 6):
            params = {'textDocument': {'uri': 'file:///a.bsl', 'version': version}}
            if version == 1:
                params['textDocument'].update(languageId='bsl', text='first')
            else: params['contentChanges'] = [{'text': 'edited' + str(version)}]
            await gateway.handle({'method': 'textDocument/didOpen' if version == 1 else 'textDocument/didChange', 'params': params})
            if version > 1: assert not gateway.resolves
            await gateway.handle({'id': version, 'method': 'textDocument/completion', 'params': {'textDocument': {'uri': 'file:///a.bsl'}}})
            assert 'error' not in output[-1]
            first_item = first_item or deepcopy(output[-1]['result'][0])
        for request_id in range(10, 14):
            await gateway.handle({'id': request_id, 'method': 'textDocument/completion', 'params': {'textDocument': {'uri': 'file:///a.bsl'}}})
            assert 'error' not in output[-1] and len(gateway.resolves) <= 2
        newest_item = deepcopy(output[-1]['result'][0])
        await gateway.handle({'id': 20, 'method': 'completionItem/resolve', 'params': newest_item})
        assert output[-1]['result']['detail'] == 'A'
        await gateway.handle({'id': 21, 'method': 'completionItem/resolve', 'params': first_item})
        assert output[-1]['error']['message'] == 'resolve-context-unavailable'
        await gateway.close()
    asyncio.run(run())
