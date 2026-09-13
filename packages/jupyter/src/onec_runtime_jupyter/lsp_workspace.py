"""Bounded no-follow metadata observation. Never reads or writes source text.

Windows scans on coarse native signals, checking root identities every 2s.
Other platforms poll 2s after each completed pass, with higher idle scan cost.
Metadata-preserving changes cannot be located; an empty native diff reindexes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
import os
from pathlib import Path
import stat
from threading import Event
import time

DEFAULT_MAX_WORKSPACE_ENTRIES = 100_000
DEFAULT_WORKSPACE_SCAN_SECONDS = 10
DEFAULT_MAX_EVENTS = 4096
SCAN_REASONS = frozenset({'workspace-unsafe', 'workspace-unavailable', 'workspace-replaced',
                         'workspace-safety-limit', 'workspace-stopped'})


def _scan_reason(error):
    return str(error) if str(error) in SCAN_REASONS else 'workspace-unavailable'


def linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def root_identity(root):
    root = Path(root)
    if not root.is_absolute() or str(root).startswith(('\\\\', '//')):
        raise ValueError('workspace-unsafe')
    identity = []
    for path in (root, *root.parents):
        info = path.lstat()
        if linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('workspace-unsafe')
        identity.append((info.st_dev, info.st_ino))
    return tuple(identity)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    root: Path
    entries: dict
    identity: tuple = ()
    reason: str | None = None


@dataclass(frozen=True)
class WorkspaceChange:
    events: tuple = ()
    rescan_required: bool = False
    reason: str | None = None


def scan_workspace(root, *, stop=None, max_entries=DEFAULT_MAX_WORKSPACE_ENTRIES,
                   timeout=DEFAULT_WORKSPACE_SCAN_SECONDS):
    root = Path(root)
    deadline, result = time.monotonic() + timeout, {}
    def check():
        if stop is not None and stop.is_set():
            raise ValueError('workspace-stopped')
        if time.monotonic() >= deadline or len(result) >= max_entries:
            raise ValueError('workspace-safety-limit')
    try:
        check()
        identity = root_identity(root)
        pending = [root]
        while pending:
            check()
            directory = pending.pop()
            before = directory.lstat()
            if linked(before):
                raise ValueError('workspace-unsafe')
            with os.scandir(directory) as entries:
                for entry in entries:
                    check()
                    info = entry.stat(follow_symlinks=False)
                    if linked(info):
                        raise ValueError('workspace-unsafe')
                    kind = stat.S_IFMT(info.st_mode)
                    if kind not in (stat.S_IFREG, stat.S_IFDIR):
                        raise ValueError('workspace-unsafe')
                    path = Path(entry.path)
                    result[path.relative_to(root).as_posix()] = (
                        kind, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    if kind == stat.S_IFDIR:
                        pending.append(path)
            after = directory.lstat()
            if linked(after) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError('workspace-replaced')
        if root_identity(root) != identity:
            raise ValueError('workspace-replaced')
        return WorkspaceSnapshot(root, result, identity)
    except ValueError as error:
        return WorkspaceSnapshot(root, {}, reason=_scan_reason(error))
    except OSError:
        return WorkspaceSnapshot(root, {}, reason='workspace-unavailable')


def diff_snapshots(old, new, *, max_events=DEFAULT_MAX_EVENTS, signaled=False):
    if new.reason:
        return WorkspaceChange(rescan_required=True, reason=new.reason)
    if old.reason or old.identity != new.identity:
        return WorkspaceChange(rescan_required=True, reason='workspace-replaced')
    events = []
    structural = False
    for path in old.entries.keys() | new.entries.keys():
        before, after = old.entries.get(path), new.entries.get(path)
        if before == after:
            continue
        # Directory mtime changes are consequences of ordinary file create/delete.
        if before and after and before[0] == after[0] == stat.S_IFDIR and before[:3] == after[:3]:
            continue
        if Path(path).suffix.lower() not in ('.bsl', '.os'):
            structural = True
        parts = path.casefold().split('/')
        canonical_module = (len(parts) in (3, 4) and parts[0] == 'commonmodules'
                            and parts[-1] == 'module.bsl'
                            and (len(parts) == 3 or parts[2] == 'ext'))
        if canonical_module and (before is None or after is None):
            # Module membership comes from Designer/EDT metadata. BSL LS can
            # retain stale canonical associations after a noncanonical rename.
            structural = True
        if len(events) >= max_events:
            return WorkspaceChange(rescan_required=True, reason='workspace-event-limit')
        events.append({'uri': (new.root / path).as_uri(), 'type': 1 if before is None else 3 if after is None else 2})
    if structural:
        return WorkspaceChange(tuple(events), True, 'workspace-layout-changed')
    if signaled and not events:
        return WorkspaceChange(rescan_required=True, reason='workspace-unlocated-change')
    return WorkspaceChange(tuple(events))


@dataclass
class _Watch:
    root: Path
    ready: asyncio.Future
    callbacks: set = field(default_factory=set)
    stop: Event = field(default_factory=Event)
    task: asyncio.Task | None = None
    snapshot: WorkspaceSnapshot | None = None


class WorkspaceWatchService:
    def __init__(self, *, scan=scan_workspace, poll_seconds=2, signal_factory=None,
                 retry_seconds=2, max_retries=3):
        if signal_factory is None and os.name == 'nt':
            from .lsp_workspace_notify import WindowsChangeSignal
            signal_factory = WindowsChangeSignal
        self.scan, self.poll_seconds, self.signal_factory = scan, poll_seconds, signal_factory
        self.retry_seconds, self.max_retries = retry_seconds, max_retries
        self.watches, self.tasks, self.closed = {}, set(), False

    @staticmethod
    def _key(root):
        return os.path.normcase(os.path.abspath(root))

    def subscribe(self, root, callback):
        if self.closed:
            raise ValueError('workspace-stopped')
        key = self._key(root)
        previous = self.watches.get(key)
        watch = previous
        if watch is None or watch.stop.is_set():
            watch = _Watch(Path(root), asyncio.get_running_loop().create_future())
            self.watches[key] = watch
            watch.task = asyncio.create_task(self._run(watch, previous))
            self.tasks.add(watch.task)
            watch.task.add_done_callback(lambda task: self._finished(watch))
        watch.callbacks.add(callback)
        def unsubscribe():
            watch.callbacks.discard(callback)
            if not watch.callbacks:
                watch.stop.set()
                if watch.task.done():
                    self._finished(watch)
        return unsubscribe

    def _finished(self, watch):
        self.tasks.discard(watch.task)
        if watch.stop.is_set():
            watch.snapshot = None
            key = self._key(watch.root)
            if self.watches.get(key) is watch:
                self.watches.pop(key)

    async def ready(self, root):
        watch = self.watches.get(self._key(root))
        if watch is None:
            return 'workspace-stopped'
        await asyncio.shield(watch.ready)
        if self.closed or watch.stop.is_set() or self.watches.get(self._key(root)) is not watch:
            return 'workspace-stopped'
        if watch.snapshot and watch.snapshot.reason:
            return watch.snapshot.reason
        try:
            if watch.snapshot and root_identity(root) != watch.snapshot.identity:
                return 'workspace-replaced'
        except (OSError, ValueError):
            return 'workspace-unavailable'
        return None

    async def _publish(self, watch, change):
        for callback in tuple(watch.callbacks):
            if watch.stop.is_set():
                break
            result = callback(change)
            if inspect.isawaitable(result):
                await result

    async def _run(self, watch, previous):
        if previous is not None:
            await previous.task
        for attempt in range(self.max_retries + 1):
            if watch.stop.is_set():
                break
            try:
                await self._observe(watch, recovered=attempt > 0)
                break
            except (OSError, ValueError) as error:
                reason = _scan_reason(error) if isinstance(error, ValueError) else 'workspace-notification-unavailable'
                watch.snapshot = WorkspaceSnapshot(watch.root, {}, reason=reason)
                if not watch.ready.done():
                    watch.ready.set_result(reason)
                await self._publish(watch, WorkspaceChange(rescan_required=True, reason=reason))
            if attempt == self.max_retries or await asyncio.to_thread(watch.stop.wait, self.retry_seconds):
                break
        if not watch.ready.done():
            watch.ready.set_result('workspace-stopped')

    async def _observe(self, watch, *, recovered):
        signal = None
        try:
            root_identity(watch.root)
            if self.signal_factory:
                signal = self.signal_factory(watch.root)
            snapshot = await asyncio.to_thread(self.scan, watch.root, stop=watch.stop)
            watch.snapshot = snapshot
            if snapshot.reason:
                raise ValueError(snapshot.reason)
            if not watch.ready.done():
                watch.ready.set_result(None)
            if recovered:
                await self._publish(watch, WorkspaceChange(rescan_required=True, reason='workspace-recovered'))
            next_identity = time.monotonic() + 2
            while not watch.stop.is_set():
                if signal:
                    signaled = await asyncio.to_thread(signal.wait, 200)
                    if signaled:
                        signal.rearm()  # Rearm before scan; edits during scan remain pending.
                        # One save can signal truncate, write and close separately.
                        # Bound coalescing even under a continuous writer.
                        until = time.monotonic() + .2
                        while not watch.stop.is_set() and time.monotonic() < until:
                            if not await asyncio.to_thread(signal.wait, 40):
                                break
                            signal.rearm()
                    if time.monotonic() >= next_identity:
                        if root_identity(watch.root) != snapshot.identity:
                            raise ValueError('workspace-replaced')
                        next_identity = time.monotonic() + 2
                    if not signaled:
                        continue
                else:
                    if await asyncio.to_thread(watch.stop.wait, self.poll_seconds):
                        break
                    signaled = False
                if watch.stop.is_set():
                    break
                current = await asyncio.to_thread(self.scan, watch.root, stop=watch.stop)
                if current.reason:
                    raise ValueError(current.reason)
                if current.identity != snapshot.identity:
                    raise ValueError('workspace-replaced')
                change = await asyncio.to_thread(diff_snapshots, snapshot, current, signaled=signaled)
                snapshot = watch.snapshot = current
                if change.events or change.rescan_required:
                    await self._publish(watch, change)
        finally:
            # All wait/scan calls have completed before closing the native handle.
            if signal:
                signal.close()

    async def close(self):
        self.closed = True
        for watch in self.watches.values():
            watch.stop.set()
        if self.tasks:
            done, pending = await asyncio.wait(self.tasks, timeout=DEFAULT_WORKSPACE_SCAN_SECONDS + 1)
            await asyncio.gather(*done)
            if pending:
                raise TimeoutError('workspace-shutdown-timeout')
        self.watches.clear()
