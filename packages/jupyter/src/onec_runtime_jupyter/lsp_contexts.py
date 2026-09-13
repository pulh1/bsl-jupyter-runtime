"""Authoritative notebook/kernel bindings, separate from document URI mapping."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import asyncio
from math import isfinite
from pathlib import Path, PurePosixPath
from threading import RLock
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .lsp_kernel_client import KernelProjectClient, _await
from .lsp_cleanup import drain, finish
from .lsp_project import ProjectConfig, ProjectLimits, encode_config


DEFAULT_BINDING_LEASE_SECONDS = 300


async def authorize_association(handler, notebook_path, kernel_id):
    """Check the installed Jupyter authorizer and real session/contents services."""
    if not handler.current_user:
        raise PermissionError('context-access-denied')
    permissions = (
        (('read', 'contents'),)
        if kernel_id is None
        else (('execute', 'kernels'), ('read', 'contents'))
    )
    for action, resource in permissions:
        if not await _await(handler.authorizer.is_authorized(
            handler, handler.current_user, action, resource,
        )):
            raise PermissionError('context-access-denied')
    model = await _await(handler.contents_manager.get(notebook_path, content=False))
    if kernel_id is None:
        if model.get('type') != 'notebook':
            raise PermissionError('context-association-unavailable')
        return
    sessions = await _await(handler.session_manager.list_sessions())
    if model.get('type') != 'notebook' or not any(
        session.get('path') == notebook_path
        and session.get('kernel', {}).get('id') == kernel_id
        for session in sessions
    ):
        raise PermissionError('context-association-unavailable')


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def normalize_source_root(root):
    """Only normalize the explicit runtime root; reject remote and linked layouts."""
    if root is None:
        return None, 'source-root-missing'
    root = Path(root)
    if not root.is_absolute() or str(root).startswith(('\\\\', '//')):
        return None, 'source-root-unavailable'
    try:
        if any(_linked(path) for path in (root, *root.parents)):
            return None, 'source-root-unsafe'
        resolved = root.resolve(strict=True)
        direct, nested = resolved / 'CommonModules', resolved / 'src' / 'CommonModules'
        if any(_linked(path) for path in (direct, resolved / 'src', nested)):
            return None, 'source-root-unsafe'
        if direct.is_dir() and nested.is_dir():
            return None, 'source-root-ambiguous'
        selected = resolved if direct.is_dir() else resolved / 'src'
        modules = selected / 'CommonModules'
        if not modules.is_dir():
            return None, 'source-root-unavailable'
        import os
        with os.scandir(modules) as entries:
            next(entries, None)
        return selected, None
    except (OSError, ValueError):
        return None, 'source-root-unavailable'


def parse_binding_request(data):
    if type(data) is not dict or set(data) != {'notebook_path', 'kernel_id', 'document_uri'}:
        raise ValueError('invalid-context-association')
    if any(
        type(value) is not str or not value or len(value) > 8192 or '\x00' in value
        for key, value in data.items()
        if not (key == 'kernel_id' and value is None)
    ):
        raise ValueError('invalid-context-association')
    path = data['notebook_path']
    parsed = urlsplit(data['document_uri'])
    if (
        path.startswith('/') or '\\' in path
        or any(part in ('.', '..') for part in path.split('/'))
        or PurePosixPath(path).suffix != '.ipynb'
    ):
        raise ValueError('invalid-context-association')
    if (
        parsed.scheme not in ('file', 'untitled') or parsed.query or parsed.fragment
        or parsed.username or parsed.password
    ):
        raise ValueError('invalid-context-association')
    return data


@dataclass(frozen=True)
class ContextStatus:
    binding_id: str
    epoch: int
    mode: str
    reason: str | None
    analysis_state: str = 'unknown'
    analysis_reason: str | None = None
    installation_id: str | None = None

    def to_wire(self):
        result = asdict(self)
        if self.analysis_state == 'unavailable' and self.analysis_reason in {
            'workspace-unsafe', 'workspace-safety-limit', 'workspace-unavailable',
            'workspace-replaced', 'workspace-notification-unavailable', 'workspace-stopped',
        }:
            result.update(mode='virtual-only', reason=self.analysis_reason)
        return result


@dataclass(repr=False)
class _Binding:
    owner: str
    notebook_path: str
    kernel_id: str | None
    document_uri: str
    client: KernelProjectClient
    status: ContextStatus
    deadline: float
    config: ProjectConfig | None = None
    source_root: Path | None = None
    incarnation: str | None = None


class ContextRegistry:
    def __init__(self, kernel_manager=None, *, limits=ProjectLimits(), max_bindings=256,
                 lease_seconds=DEFAULT_BINDING_LEASE_SECONDS, clock=time.monotonic):
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError('invalid-binding-lease')
        if (
            isinstance(max_bindings, bool)
            or not isinstance(max_bindings, int)
            or max_bindings <= 0
        ):
            raise ValueError('invalid-context-binding-limit')
        if not isinstance(limits, ProjectLimits):
            raise ValueError('invalid-project-limits')
        self.kernel_manager, self.limits, self.max_bindings = kernel_manager, limits, max_bindings
        self.lease_seconds, self.clock = float(lease_seconds), clock
        self._bindings = {}
        self._lock = RLock()
        self._connections = {}
        self.revision = 0
        self._closing = set()
        self._close_error = None
        self._close_secondary = 0

    def bind(self, owner, notebook_path, kernel_id, document_uri):
        parse_binding_request(dict(
            notebook_path=notebook_path, kernel_id=kernel_id, document_uri=document_uri,
        ))
        with self._lock:
            self.prune()
            # Concurrent tabs can POST before either socket claims its identity.
            # Tuple equality never grants authority over an outstanding POST.
            if len(self._bindings) >= self.max_bindings:
                raise ValueError('context-binding-limit')
            binding = uuid4().hex
            client = KernelProjectClient(
                self.kernel_manager,
                kernel_id,
                lambda client, incarnation, config: self.accept_config(
                    binding, incarnation, config, client=client,
                ),
                lambda client, reason: self.degrade(binding, reason, client=client),
                self.limits,
            )
            self._bindings[binding] = _Binding(
                owner, notebook_path, kernel_id, document_uri, client,
                ContextStatus(binding, 1, 'virtual-only', 'runtime-unavailable'),
                self.clock() + self.lease_seconds,
            )
            self.revision += 1
            return binding

    def client(self, binding):
        return self._bindings[binding].client

    async def start(self, binding):
        if self._bindings[binding].kernel_id is not None:
            await self.client(binding).start()

    def invalidate_kernel(self, kernel_id):
        with self._lock:
            for binding, current in tuple(self._bindings.items()):
                if current.kernel_id == kernel_id:
                    self._close_client(current.client)
                    self.degrade(binding, 'kernel-incarnation-changed', client=current.client)

    def accept_config(self, binding, incarnation, config, *, client=None):
        with self._lock:
            self.prune()
            current = self._bindings.get(binding)
            if (
                current is None or client is not current.client or client.closed
                or incarnation != client.incarnation
                or not isinstance(config, ProjectConfig)
                or config.installation_id != client.installation_id
            ):
                return False
            if current.config == config and current.incarnation == incarnation:
                return False
            try:
                encode_config(config, self.limits)
            except ValueError:
                self.degrade(binding, 'project-config-invalid-or-limited', client=client)
                return False
            root, reason = normalize_source_root(config.source_root)
            current.config, current.source_root, current.incarnation = config, root, incarnation
            current.status = ContextStatus(
                binding,
                current.status.epoch + 1,
                'project' if root is not None else 'virtual-only',
                reason,
                installation_id=config.installation_id,
            )
            self.revision += 1
            return True

    def degrade(self, binding, reason, *, client):
        with self._lock:
            self.prune()
            current = self._bindings.get(binding)
            if current is None or client is not current.client:
                return False
            if (
                current.config is None and current.source_root is None
                and current.status.mode == 'virtual-only'
                and current.status.reason == reason
                and current.status.installation_id is None
            ):
                return False
            current.config = current.source_root = current.incarnation = None
            current.status = ContextStatus(
                binding, current.status.epoch + 1, 'virtual-only', reason,
            )
            self.revision += 1
            return True

    def attach(self, connection_id, *, owner, binding):
        """Called only after the authenticated handler checks the association."""
        with self._lock:
            self.select(connection_id, owner=owner, bindings={
                *self._connections.get((owner, connection_id), ()), binding,
            })

    def selectable(self, connection_id, *, owner, binding):
        """Read-only admission; a live socket's selection cannot be stolen."""
        with self._lock:
            self._owned(binding, owner)
            return not any(binding in selected for key, selected in self._connections.items()
                           if key != (owner, connection_id))

    def detach(self, connection_id, *, owner):
        with self._lock:
            for binding in self._connections.pop((owner, connection_id), set()):
                if binding in self._bindings:
                    current = self._bindings[binding]
                    current.status = replace(
                        current.status, analysis_state='unknown', analysis_reason=None,
                    )
            self.revision += 1

    def select(self, connection_id, *, owner, bindings):
        """Atomically replace a socket's server-validated association selection."""
        with self._lock:
            incoming = set(bindings)
            for binding in incoming:
                if not self.selectable(connection_id, owner=owner, binding=binding):
                    raise KeyError('context-already-selected')
            if len({self._bindings[binding].document_uri for binding in incoming}) != len(incoming):
                raise KeyError('context-document-ambiguous')
            key = (owner, connection_id)
            previous = self._connections.get(key, set())
            if previous == incoming:
                return
            for binding in previous - incoming:
                if binding in self._bindings:
                    current = self._bindings[binding]
                    current.status = replace(
                        current.status, analysis_state='unknown', analysis_reason=None,
                    )
            self._connections[key] = incoming
            self.revision += 1

    def connection_contexts(self, connection_id, *, owner):
        with self._lock:
            self.prune()
            return [
                self._context(binding, self._bindings[binding])
                for binding in sorted(self._connections.get((owner, connection_id), ()))
                if binding in self._bindings and self._bindings[binding].owner == owner
            ]

    def acknowledge(self, connection_id, *, owner, statuses):
        """Analysis status cannot modify project identity, root or associations."""
        states = {'unknown', 'indexing', 'updating', 'ready', 'unavailable'}
        reasons = {
            None, 'child-synchronization-failed', 'child-capacity-unavailable',
            'workspace-unsafe', 'workspace-safety-limit', 'workspace-unavailable',
            'child-unavailable', 'control-unavailable',
            'workspace-replaced', 'workspace-recovered', 'workspace-layout-changed',
            'workspace-unlocated-change', 'workspace-event-limit', 'workspace-stopped',
            'workspace-notification-unavailable',
            'index-convergence-unconfirmed',
        }
        with self._lock:
            if type(statuses) is not list or len(statuses) > self.max_bindings:
                return False
            admitted = []
            for status in statuses:
                if type(status) is not dict or set(status) != {
                    'binding_id', 'epoch', 'state', 'reason',
                }:
                    return False
                if (
                    type(status['binding_id']) is not str
                    or type(status['epoch']) is not int
                    or type(status['state']) is not str
                    or (status['reason'] is not None and type(status['reason']) is not str)
                ):
                    return False
                binding = status['binding_id']
                if (
                    binding not in self._connections.get((owner, connection_id), ())
                    or binding not in self._bindings
                ):
                    return False
                current = self._owned(binding, owner)
                if (
                    status['epoch'] != current.status.epoch
                    or status['state'] not in states
                    or status['reason'] not in reasons
                ):
                    return False
                admitted.append((current, status))
            for current, status in admitted:
                current.status = replace(
                    current.status,
                    analysis_state=status['state'],
                    analysis_reason=status['reason'],
                )
            return True

    def _owned(self, binding, owner):
        self.prune()
        current = self._bindings[binding]
        if current.owner != owner:
            raise KeyError('context-unavailable')
        return current

    def status(self, binding, *, owner=None):
        with self._lock:
            self.prune()
            current = self._bindings[binding] if owner is None else self._owned(binding, owner)
            return current.status

    def association(self, binding, *, owner):
        """Authorize status/source access without exposing project configuration."""
        with self._lock:
            current = self._owned(binding, owner)
            return {
                'binding_id': binding,
                'document_uri': current.document_uri,
                'notebook_path': current.notebook_path,
                'kernel_id': current.kernel_id,
            }

    def context(self, binding, *, owner):
        with self._lock:
            current = self._owned(binding, owner)
            return self._context(binding, current)

    @staticmethod
    def _context(binding, current):
        return {
            'binding_id': binding,
            'epoch': current.status.epoch,
            'kernel_id': current.kernel_id,
            'kernel_incarnation': current.incarnation,
            'installation_id': current.status.installation_id,
            'source_root': (
                str(current.source_root) if current.source_root is not None else None
            ),
            'document_uri': current.document_uri,
        }

    def contexts(self, *, owner):
        with self._lock:
            self.prune()
            return [
                self._context(key, value)
                for key, value in self._bindings.items()
                if value.owner == owner
            ]

    def associations(self, *, owner):
        """Configuration-free metadata for WebSocket association discovery."""
        with self._lock:
            self.prune()
            return [
                {
                    'binding_id': key,
                    'document_uri': value.document_uri,
                    'notebook_path': value.notebook_path,
                    'kernel_id': value.kernel_id,
                }
                for key, value in self._bindings.items()
                if value.owner == owner
            ]

    def unbind(self, binding, *, owner):
        with self._lock:
            self._owned(binding, owner)
            self._remove(binding)

    def _remove(self, binding):
        current = self._bindings.pop(binding)
        for selected in self._connections.values():
            selected.discard(binding)
        self.revision += 1
        self._close_client(current.client)

    def _close_client(self, client):
        completion = client.close()
        self._closing.add(completion)
        def observed(future):
            error = future.exception()
            with self._lock:
                self._closing.discard(future)
                if error is not None and self._close_error is None:
                    self._close_error = error
                elif error is not None and self._close_secondary < 8:
                    self._close_error.add_note('Additional client cleanup failure: ' + type(error).__name__)
                    self._close_secondary += 1
        completion.add_done_callback(observed)

    async def wait_closed(self):
        # Snapshot only under the registry lock: client completion may call back
        # into the registry from its owning loop.
        with self._lock:
            pending = tuple(self._closing)
        if pending:
            await finish(asyncio.create_task(drain([asyncio.wrap_future(f) for f in pending], timeout=3)))
        with self._lock:
            error = self._close_error
        if error is not None:
            raise error

    def prune(self):
        """Bounded metadata scan; kernel/control activity never renews browser leases."""
        with self._lock:
            expired = [
                key for key, current in self._bindings.items()
                if current.deadline <= self.clock()
            ]
            for key in expired:
                self._remove(key)
            return len(expired)

    def renew(self, binding, *, owner):
        """Call only after an authenticated browser action passes association checks."""
        with self._lock:
            self._owned(binding, owner).deadline = self.clock() + self.lease_seconds

    def close(self):
        with self._lock:
            for current in self._bindings.values():
                self._close_client(current.client)
            self._bindings.clear()
            self._connections.clear()
            self.revision += 1
