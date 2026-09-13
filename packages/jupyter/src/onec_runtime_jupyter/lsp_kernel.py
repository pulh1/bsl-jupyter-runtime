"""Optional kernel comm bridge for static project configuration only."""

from __future__ import annotations

import logging
from threading import RLock
from uuid import uuid4

from onec_runtime.session import RuntimeSessionConfig

from .lsp_project import (
    MAX_BRIDGE_EPOCH,
    PROJECT_REASONS,
    ProjectConfig,
    ProjectLimits,
    encode_envelope,
)


TARGET = 'onec.runtime.project.v2'
_ATTRIBUTE = '_onec_runtime_project_bridge'
_LOG = logging.getLogger(__name__)


class ProjectBridge:
    """Retain and publish the latest installed runtime's static configuration."""

    def __init__(self, shell, runtime=None, limits=ProjectLimits()):
        self.shell = shell
        self.limits = limits
        self._lock = RLock()
        self._runtime = None
        self._epoch = 0
        self._comms = []
        self._closed = False
        try:
            self._message = encode_envelope(
                0, None, 'runtime-unavailable', self.limits,
            )
        except ValueError:
            self._message = None
        shell.kernel.comm_manager.register_target(TARGET, self._open)
        if runtime is not None:
            self.replace(runtime)

    @property
    def epoch(self):
        return self._epoch

    def replace(self, runtime):
        with self._lock:
            if self._closed or runtime is self._runtime:
                return False
            if self._epoch >= MAX_BRIDGE_EPOCH:
                self._message = None
                self._send_locked()
                return False
            self._runtime = runtime
            try:
                runtime_config = getattr(runtime, 'config', None)
                root = (
                    runtime_config.source_root
                    if isinstance(runtime_config, RuntimeSessionConfig)
                    else None
                )
                config = ProjectConfig(
                    uuid4().hex,
                    str(root) if root is not None else None,
                )
                reason = None
            except Exception:
                config = None
                reason = 'project-config-invalid-or-limited'
            self._epoch += 1
            try:
                self._message = encode_envelope(
                    self._epoch, config, reason, self.limits,
                )
            except Exception:
                try:
                    self._message = encode_envelope(
                        self._epoch, None,
                        'project-config-invalid-or-limited', self.limits,
                    )
                except Exception:
                    self._message = None
            self._send_locked()
            return True

    def _send_locked(self, comm=None):
        recipients = (comm,) if comm is not None else tuple(self._comms)
        for recipient in recipients:
            if self._message is None:
                if recipient in self._comms:
                    self._comms.remove(recipient)
                try:
                    recipient.close()
                except Exception:
                    pass
                continue
            try:
                recipient.send(data=self._message)
            except Exception:
                if recipient in self._comms:
                    self._comms.remove(recipient)
                _LOG.warning('BSL project bridge transport unavailable')

    def _open(self, comm, message):
        try:
            data = message.get('content', {}).get('data', {})
        except AttributeError:
            data = None
        with self._lock:
            if (
                self._closed
                or len(self._comms) >= self.limits.max_comms
                or type(data) is not dict
                or set(data) != {'version'}
                or type(data['version']) is not int
                or data['version'] != 2
            ):
                comm.close()
                return
            self._comms.append(comm)
            comm.on_close(lambda _message: self._remove(comm))
            comm.on_msg(lambda _message: self._resend(comm))
            self._send_locked(comm)

    def _resend(self, comm):
        with self._lock:
            if not self._closed and comm in self._comms:
                self._send_locked(comm)

    def _remove(self, comm):
        with self._lock:
            if comm in self._comms:
                self._comms.remove(comm)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.shell.kernel.comm_manager.unregister_target(TARGET, self._open)
            for comm in tuple(self._comms):
                try:
                    comm.close()
                except Exception:
                    pass
            self._comms.clear()
            self._runtime = None


def install_project_bridge(shell, runtime):
    kernel = getattr(shell, 'kernel', None)
    if getattr(kernel, 'comm_manager', None) is None:
        return None
    try:
        bridge = getattr(shell, _ATTRIBUTE, None)
        if bridge is None:
            bridge = ProjectBridge(shell, runtime)
            setattr(shell, _ATTRIBUTE, bridge)
        elif runtime is not None:
            bridge.replace(runtime)
        return bridge
    except Exception:
        detach_project_bridge(shell)
        _LOG.warning('BSL project bridge unavailable')
        return None


def detach_project_bridge(shell):
    bridge = getattr(shell, _ATTRIBUTE, None)
    if bridge is not None:
        try:
            bridge.close()
        except Exception:
            _LOG.warning('BSL project bridge detach unavailable')
        finally:
            delattr(shell, _ATTRIBUTE)
