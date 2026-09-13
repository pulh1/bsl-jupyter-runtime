from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from enum import Enum
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
from time import monotonic
from uuid import UUID

from onec_runtime.config import RuntimeConfig
from onec_runtime.errors import (
    CommandTimeout,
    InvalidMessageSequence,
    ProcessStartError,
)
from onec_runtime.processes import FileModeProcesses
from onec_runtime.supervisor_protocol import (
    ControlMessage,
    MessageKind,
    MessageReceiver,
)


def _is_data_only(value: object, seen: set[int] | None = None) -> bool:
    value_type = type(value)
    if value_type in {
        type(None),
        bool,
        int,
        float,
        str,
        bytes,
        type(Path()),
        UUID,
    }:
        return True

    seen = seen if seen is not None else set()
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    try:
        if value_type is dict:
            return all(
                _is_data_only(key, seen) and _is_data_only(item, seen)
                for key, item in value.items()
            )
        if value_type in {list, tuple, set, frozenset}:
            return all(_is_data_only(item, seen) for item in value)
        if value_type is RuntimeConfig:
            return all(
                _is_data_only(getattr(value, field.name), seen)
                for field in fields(RuntimeConfig)
            )
        return False
    finally:
        seen.remove(identity)


class _ControllerStartupState(Enum):
    WAITING_FOR_DEBUG_READY = "waiting_for_debug_ready"
    READY = "ready"
    FAILED = "failed"


class SupervisedGenerationProcesses:
    """Parent-owned OS processes and Controller IPC for one generation."""

    def __init__(
        self,
        config: RuntimeConfig,
        generation_id: int,
        worker_target: Callable[..., None],
        worker_args: tuple[object, ...],
    ) -> None:
        if not _is_data_only(worker_args):
            raise TypeError("Worker arguments must contain data-only driver inputs")
        self._config = config
        self._generation_id = generation_id
        self._worker_target = worker_target
        self._worker_args = worker_args
        self._file_processes = FileModeProcesses(config)
        self._context = multiprocessing.get_context("spawn")
        self._parent_connection, self._child_connection = self._context.Pipe()
        self._controller: multiprocessing.Process | None = None
        self._controller_started = False
        self._controller_exit_code: int | None = None
        self._startup_state = _ControllerStartupState.WAITING_FOR_DEBUG_READY
        self._receiver = MessageReceiver(generation_id)

    @property
    def controller_pid(self) -> int | None:
        if self._controller is None or not self._controller.is_alive():
            return None
        return self._controller.pid

    @property
    def dbgs_pid(self) -> int | None:
        owned = self._file_processes.debug_server
        return owned.pid if owned is not None else None

    @property
    def onec_pid(self) -> int | None:
        owned = self._file_processes.debuggee
        return owned.pid if owned is not None else None

    def start_debug_server(self) -> int:
        return self._file_processes.start_debug_server()

    def start_controller(self, debug_port: int, run_dir: Path) -> None:
        if self._controller_started:
            raise ProcessStartError("Controller is already owned by this generation")
        process = self._context.Process(
            target=self._worker_target,
            args=(
                self._generation_id,
                self._child_connection,
                debug_port,
                run_dir,
                *self._worker_args,
            ),
            name=f"onec-controller-{self._generation_id}",
        )
        self._controller = process
        try:
            process.start()
            self._controller_started = True
        except BaseException:
            self._controller = None
            raise
        finally:
            self._child_connection.close()

    def start_debuggee(self, debug_port: int) -> None:
        if self._startup_state is not _ControllerStartupState.READY:
            raise InvalidMessageSequence(
                "Controller must publish DEBUG_READY before the debuggee starts"
            )
        self._file_processes.start_debuggee(
            debug_port,
            execute_external=False,
            thick_client=False,
        )

    def send(self, message: ControlMessage) -> None:
        self._parent_connection.send(message)

    def receive(self, timeout_s: float) -> ControlMessage:
        if not self._parent_connection.poll(timeout_s):
            raise CommandTimeout(
                f"Controller did not publish a message within {timeout_s} seconds"
            )
        message = self._parent_connection.recv()
        if not isinstance(message, ControlMessage):
            self._fail_startup()
            raise InvalidMessageSequence("Controller sent a non-protocol message")
        try:
            self._receiver.accept(message)
        except InvalidMessageSequence:
            self._fail_startup()
            raise
        if self._startup_state is _ControllerStartupState.FAILED:
            raise InvalidMessageSequence("Controller startup already failed")
        if self._startup_state is _ControllerStartupState.WAITING_FOR_DEBUG_READY:
            if message.kind is not MessageKind.DEBUG_READY:
                self._startup_state = _ControllerStartupState.FAILED
                raise InvalidMessageSequence(
                    "Controller first message must be DEBUG_READY"
                )
            self._startup_state = _ControllerStartupState.READY
        elif message.kind is MessageKind.DEBUG_READY:
            raise InvalidMessageSequence("Controller sent duplicate DEBUG_READY")
        return message

    def controller_exitcode(self) -> int | None:
        self._reap_controller()
        return (
            self._controller_exit_code
            if self._controller is None
            else self._controller.exitcode
        )

    def terminate_controller(self, timeout_s: float) -> None:
        deadline = monotonic() + max(0.0, timeout_s)
        process = self._controller
        if process is not None:
            if process.is_alive():
                process.terminate()
                process.join(max(0.0, deadline - monotonic()))
            if process.is_alive():
                process.kill()
                process.join(max(0.0, deadline - monotonic()))
            self._reap_controller()
        self._close_connections()

    def terminate_onec(self, timeout_s: float) -> None:
        self._file_processes.close_debuggee(timeout_s)

    def terminate_dbgs(self, timeout_s: float) -> None:
        self._file_processes.close_debug_server(timeout_s)

    def all_stopped(self) -> bool:
        self._reap_controller()
        return (
            self._controller is None
            and self._file_processes.debuggee is None
            and self._file_processes.debug_server is None
        )

    def _reap_controller(self) -> None:
        process = self._controller
        if process is None or process.exitcode is None:
            return
        process.join(timeout=0)
        self._controller_exit_code = process.exitcode
        process.close()
        self._controller = None
        self._close_connections()

    def _close_connections(self) -> None:
        for connection in (self._parent_connection, self._child_connection):
            try:
                connection.close()
            except OSError:
                pass

    def _fail_startup(self) -> None:
        if self._startup_state is _ControllerStartupState.WAITING_FOR_DEBUG_READY:
            self._startup_state = _ControllerStartupState.FAILED
