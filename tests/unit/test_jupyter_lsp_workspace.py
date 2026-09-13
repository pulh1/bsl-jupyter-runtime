import asyncio
import os
from threading import Event

import pytest

from importlib import import_module, util


@pytest.fixture(autouse=True)
def workspace_api():
    global workspace
    assert util.find_spec('onec_runtime_jupyter.lsp_workspace'), 'workspace observer missing'
    workspace = import_module('onec_runtime_jupyter.lsp_workspace')


def test_snapshot_diff_real_save_create_delete_rename_and_layout(tmp_path):
    module = tmp_path / 'Module.bsl'
    module.write_text('old')
    first = workspace.scan_workspace(tmp_path)
    assert first.reason is None
    module.write_text('new saved text')
    second = workspace.scan_workspace(tmp_path)
    change = workspace.diff_snapshots(first, second)
    assert change.events == ({'uri': module.as_uri(), 'type': 2},)
    renamed = tmp_path / 'Renamed.bsl'
    module.rename(renamed)
    third = workspace.scan_workspace(tmp_path)
    assert {e['type'] for e in workspace.diff_snapshots(second, third).events} == {1, 3}
    (tmp_path / 'Configuration.xml').write_text('<Configuration/>')
    assert workspace.diff_snapshots(third, workspace.scan_workspace(tmp_path)).rescan_required
    renamed.unlink()
    assert not any(p.suffix == '.bsl' for p in tmp_path.iterdir())


@pytest.mark.parametrize('relative', ['CommonModules/Example/Ext/Module.bsl', 'CommonModules/Example/Module.bsl'])
def test_canonical_source_membership_reindexes_but_atomic_save_preserves_child_path(tmp_path, relative):
    module = tmp_path / relative; module.parent.mkdir(parents=True); module.write_text('before')
    first = workspace.scan_workspace(tmp_path)
    replacement = tmp_path / 'replacement'; replacement.write_text('atomic saved content')
    replacement.replace(module)
    saved = workspace.scan_workspace(tmp_path)
    assert not workspace.diff_snapshots(first, saved).rescan_required
    renamed = module.with_name('Renamed.bsl'); module.rename(renamed)
    moved = workspace.scan_workspace(tmp_path)
    change = workspace.diff_snapshots(saved, moved)
    assert change.rescan_required and change.reason == 'workspace-layout-changed'
    renamed.rename(module)
    assert workspace.diff_snapshots(moved, workspace.scan_workspace(tmp_path)).rescan_required


def test_scan_limits_are_explicit_and_do_not_admit_partial_snapshot(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'a/b.bsl').write_text('content')
    result = workspace.scan_workspace(tmp_path, max_entries=1)
    assert result.reason == 'workspace-safety-limit' and result.entries == {}
    stop = Event(); stop.set()
    assert workspace.scan_workspace(tmp_path, stop=stop).reason == 'workspace-stopped'
    assert workspace.scan_workspace(tmp_path, timeout=0).reason == 'workspace-safety-limit'


def test_observer_shared_baseline_and_real_saved_file(tmp_path):
    async def run():
        module = tmp_path / 'Module.bsl'; module.write_text('old')
        calls = []; changes = []; found = asyncio.Event()
        def scan(root, **kwargs):
            calls.append(root)
            return workspace.scan_workspace(root, **kwargs)
        async def changed(change):
            changes.append(change); found.set()
        service = workspace.WorkspaceWatchService(scan=scan, poll_seconds=.05)
        remove = service.subscribe(tmp_path, changed)
        other = service.subscribe(tmp_path, lambda c: None)
        try:
            assert await service.ready(tmp_path) is None
            assert len(calls) == 1 and changes == []
            module.write_text('current saved source')
            await asyncio.wait_for(found.wait(), 3)
            assert any(e['uri'] == module.as_uri() for c in changes for e in c.events)
            assert module.read_text() == 'current saved source'
        finally:
            remove(); other(); await service.close()
        assert all(task.done() for task in service.tasks)
    asyncio.run(run())


@pytest.mark.skipif(os.name != 'nt', reason='Windows native API')
def test_native_signal_real_wait_rearm_close_and_exact_handles(tmp_path):
    from onec_runtime_jupyter.lsp_workspace_notify import WindowsChangeSignal
    from windows_owned_handles import WindowsOwnedHandles
    # Warm ctypes/Windows resource loading before exact handle accounting.
    signal = WindowsChangeSignal(tmp_path); signal.close()
    handles = WindowsOwnedHandles()
    before = handles.sample().counted
    for i in range(10):
        signal = WindowsChangeSignal(tmp_path)
        assert signal.wait(1) is False
        module = tmp_path / f'{i}.bsl'; module.write_text('first')
        assert signal.wait(1000) is True
        signal.rearm()
        module.write_text('second')
        assert signal.wait(1000) is True
        signal.rearm()
        signal.close(); signal.close()
    assert handles.sample().counted == before


def test_edits_during_scan_are_observed_by_a_following_pass(tmp_path):
    async def run():
        module = tmp_path / 'Module.bsl'; module.write_text('initial')
        entered, release = Event(), Event()
        calls = []; changes = []
        def scan(root, **kwargs):
            snapshot = workspace.scan_workspace(root, **kwargs)
            calls.append(snapshot)
            if len(calls) == 2:
                entered.set()
                assert release.wait(3)
            return snapshot
        service = workspace.WorkspaceWatchService(scan=scan, poll_seconds=.05)
        remove = service.subscribe(tmp_path, changes.append)
        try:
            assert await service.ready(tmp_path) is None
            module.write_text('first saved edit')
            assert await asyncio.to_thread(entered.wait, 3)
            module.write_text('second edit while scan paused')
            release.set()
            async with asyncio.timeout(4):
                while len(changes) < 2:
                    await asyncio.sleep(.02)
            assert len(calls) >= 3
            assert all(c.events for c in changes[:2])
        finally:
            release.set(); remove(); await service.close()
    asyncio.run(run())


def test_snapshot_diff_runs_off_event_loop(tmp_path, monkeypatch):
    import threading
    async def run():
        module = tmp_path / 'Module.bsl'; module.write_text('old')
        loop_thread = threading.get_ident(); threads = []; changes = []
        original = workspace.diff_snapshots
        def diff(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)
        monkeypatch.setattr(workspace, 'diff_snapshots', diff)
        service = workspace.WorkspaceWatchService(poll_seconds=.05)
        remove = service.subscribe(tmp_path, changes.append)
        try:
            assert await service.ready(tmp_path) is None
            module.write_text('saved')
            async with asyncio.timeout(3):
                while not changes: await asyncio.sleep(.02)
            assert threads and loop_thread not in threads
        finally: remove(); await service.close()
    asyncio.run(run())


def test_root_replacement_is_unavailable_then_recovers_with_fresh_baseline(tmp_path):
    async def run():
        root = tmp_path / 'project'; root.mkdir(); (root / 'Module.bsl').write_text('old')
        service = workspace.WorkspaceWatchService(poll_seconds=.05, retry_seconds=.05)
        changes = []; remove = service.subscribe(root, changes.append)
        try:
            assert await service.ready(root) is None
            root.rename(tmp_path / 'previous')
            root.mkdir(); (root / 'Module.bsl').write_text('replacement')
            async with asyncio.timeout(5):
                while not any(c.reason == 'workspace-replaced' for c in changes):
                    await asyncio.sleep(.02)
            async with asyncio.timeout(5):
                while await service.ready(root) is not None:
                    await asyncio.sleep(.02)
            assert any(c.rescan_required and c.reason == 'workspace-recovered' for c in changes)
        finally: remove(); await service.close()
    asyncio.run(run())


def test_empty_native_diff_overflow_and_failed_scan_force_explicit_reindex(tmp_path):
    module = tmp_path / 'Module.bsl'; module.write_text('old')
    first = workspace.scan_workspace(tmp_path)
    empty = workspace.diff_snapshots(first, first, signaled=True)
    assert empty.rescan_required and empty.reason == 'workspace-unlocated-change'
    module.unlink(); (tmp_path / 'Other.bsl').write_text('new')
    change = workspace.diff_snapshots(first, workspace.scan_workspace(tmp_path), max_events=1)
    assert change.rescan_required and change.reason == 'workspace-event-limit' and not change.events
    failed = workspace.scan_workspace(tmp_path, timeout=0)
    assert workspace.diff_snapshots(first, failed).reason == 'workspace-safety-limit'


def test_notification_failure_is_visible_and_retries_are_finite(tmp_path):
    async def run():
        attempts = []; changes = []
        def fail(root):
            attempts.append(root); raise OSError('private physical path')
        service = workspace.WorkspaceWatchService(signal_factory=fail, retry_seconds=.01, max_retries=2)
        remove = service.subscribe(tmp_path, changes.append)
        try:
            assert await service.ready(tmp_path) == 'workspace-notification-unavailable'
            await asyncio.gather(*service.tasks)
            assert len(attempts) == 3
            assert changes and all(c.reason == 'workspace-notification-unavailable' for c in changes)
        finally: remove(); await service.close()
    asyncio.run(run())


def test_observer_never_publishes_private_exception_text(tmp_path):
    async def run():
        def bad_scan(root, **kwargs): raise ValueError('C:/private/source-root')
        service = workspace.WorkspaceWatchService(scan=bad_scan, max_retries=0)
        changes = []; remove = service.subscribe(tmp_path, changes.append)
        try:
            assert await service.ready(tmp_path) == 'workspace-unavailable'
            assert changes[0].reason == 'workspace-unavailable'
        finally: remove(); await service.close()
    asyncio.run(run())


def test_closed_subscription_cannot_admit_a_delayed_baseline(tmp_path):
    async def run():
        entered, release = Event(), Event()
        def scan(root, **kwargs):
            entered.set(); assert release.wait(3)
            return workspace.scan_workspace(root, **kwargs)
        service = workspace.WorkspaceWatchService(scan=scan)
        remove = service.subscribe(tmp_path, lambda _: None)
        pending = asyncio.create_task(service.ready(tmp_path))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            remove(); release.set()
            assert await pending == 'workspace-stopped'
        finally: release.set(); await service.close()
    asyncio.run(run())


def test_unsubscribe_releases_root_snapshots_without_closing_service(tmp_path):
    async def run():
        service = workspace.WorkspaceWatchService()
        try:
            for index in range(3):
                root = tmp_path / str(index); root.mkdir()
                remove = service.subscribe(root, lambda c: None)
                assert await service.ready(root) is None
                remove()
                await asyncio.gather(*service.tasks)
            assert not service.watches
        finally: await service.close()
    asyncio.run(run())


def test_resubscribe_waits_for_old_scan_and_native_idle_does_not_poll_tree(tmp_path):
    async def run():
        entered, release = Event(), Event()
        active = [0]; maximum = [0]; scans = []
        def scan(root, **kwargs):
            active[0] += 1; maximum[0] = max(maximum[0], active[0]); scans.append(root)
            try:
                if len(scans) == 1:
                    entered.set(); assert release.wait(3)
                return workspace.scan_workspace(root, **kwargs)
            finally: active[0] -= 1
        service = workspace.WorkspaceWatchService(scan=scan)
        first = service.subscribe(tmp_path, lambda _: None)
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            first()
            second = service.subscribe(tmp_path, lambda _: None)
            release.set()
            assert await service.ready(tmp_path) is None
            assert maximum[0] == 1 and len(scans) == 2
            if os.name == 'nt':
                await asyncio.sleep(2.2)  # Includes one cheap root/ancestor check interval.
                assert len(scans) == 2, 'idle native backend rescanned the whole tree'
            second()
        finally: release.set(); await service.close()
    asyncio.run(run())


def test_deleted_root_reports_unavailable_without_silent_polling(tmp_path):
    async def run():
        root = tmp_path / 'root'; root.mkdir()
        changes = []; service = workspace.WorkspaceWatchService(max_retries=0)
        remove = service.subscribe(root, changes.append)
        try:
            assert await service.ready(root) is None
            root.rmdir()
            async with asyncio.timeout(4):
                while not changes: await asyncio.sleep(.02)
            assert changes[0].rescan_required
            assert await service.ready(root) is not None
        finally: remove(); await service.close()
    asyncio.run(run())


@pytest.mark.skipif(os.name != 'nt', reason='Windows native API')
def test_native_service_burst_shutdown_and_exact_handle_growth(tmp_path):
    from windows_owned_handles import WindowsOwnedHandles
    async def run(index):
        root = tmp_path / str(index); root.mkdir()
        changes = []
        service = workspace.WorkspaceWatchService()
        remove = service.subscribe(root, changes.append)
        try:
            assert await service.ready(root) is None
            for n in range(20): (root / f'{n}.bsl').write_text('burst')
            async with asyncio.timeout(5):
                while not changes: await asyncio.sleep(.02)
            assert all(c.events or c.rescan_required for c in changes)
        finally: remove(); await service.close()
    asyncio.run(run('warmup'))
    handles = WindowsOwnedHandles(); before = handles.sample().counted
    for i in range(5): asyncio.run(run(i))
    assert handles.sample().counted == before


def test_linked_tree_denied_before_native_registration(tmp_path):
    import subprocess
    root = tmp_path / 'project'; root.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    linked = root / 'linked'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(outside)], capture_output=True)
        assert result.returncode == 0
    else: linked.symlink_to(outside, target_is_directory=True)
    try:
        assert workspace.scan_workspace(root).reason == 'workspace-unsafe'
        async def run():
            registrations = []
            def signal(root):
                registrations.append(root); pytest.fail('linked root registered')
            service = workspace.WorkspaceWatchService(signal_factory=signal, max_retries=0)
            remove = service.subscribe(linked, lambda c: None)
            try:
                assert await service.ready(linked) == 'workspace-unsafe'
                assert registrations == []
            finally: remove(); await service.close()
        asyncio.run(run())
    finally:
        if os.name == 'nt': linked.rmdir()
        else: linked.unlink()
