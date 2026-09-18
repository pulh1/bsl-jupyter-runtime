"""Ordered MAIN command protocol, independent of stop routing and notebooks."""

from __future__ import annotations

from collections.abc import Callable
from math import isfinite
from typing import Protocol

from onec_runtime.errors import ProtocolError, StopWaitIntervalElapsed
from onec_runtime.execution.main.operation import MainOperation
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.rdbg.models import StopEvent


class MainRdbgPort(Protocol):
    """Methods needed by MAIN; arbiter owns the concrete session in the final route."""

    def modify(
        self, variable: str, value_expression: str, *,
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> object: ...

    def continue_(
        self, *, on_transport_dispatch: Callable[[], None] | None = None
    ) -> None: ...

    def wait_for_any_stop(self, *, timeout_s: float) -> StopEvent: ...


class MainExecutor:
    def __init__(
        self, rdbg: MainRdbgPort | None = None, *, poll_interval_s: float = 6.0
    ) -> None:
        if (
            isinstance(poll_interval_s, bool)
            or not isinstance(poll_interval_s, (int, float))
            or not isfinite(float(poll_interval_s))
            or poll_interval_s <= 0
        ):
            raise ValueError("MAIN stop poll interval must be finite and positive")
        self._rdbg = rdbg
        self._poll_interval_s = float(poll_interval_s)

    def dispatch(
        self,
        operation: MainOperation,
        instruction: str,
        *,
        install_workspace: Callable[[], None],
        before_command_write: Callable[[], None],
        before_continue: Callable[[], None],
        port: MainRdbgPort | None = None,
    ) -> StopEvent:
        """Write one command and wait for its next stop without an execution deadline."""

        rdbg = self._port(port)
        install_workspace()
        before_command_write()
        for variable, expression in (
            ("ТекущаяИнструкция", bsl_string_literal(instruction)),
            ("ИдентификаторКоманды", str(operation.command_id)),
        ):
            result = rdbg.modify(
                variable, expression,
                on_transport_dispatch=operation.command_write_requested,
            )
            operation.command_write_acknowledged()
            self._checked_modify(result, variable)
        return self.resume(operation, before_continue=before_continue, port=rdbg)

    def resume(
        self,
        operation: MainOperation,
        *,
        before_continue: Callable[[], None] | None = None,
        port: MainRdbgPort | None = None,
    ) -> StopEvent:
        """Continue the same MAIN command after a user or CAPTURE stop."""

        rdbg = self._port(port)
        if before_continue is not None:
            before_continue()
        self.continue_command(operation, port=rdbg)
        return self.await_stop(port=rdbg)

    def continue_command(
        self, operation: MainOperation, *, port: MainRdbgPort | None = None
    ) -> None:
        """Issue one Continue; its acknowledgement does not finish MAIN."""

        if operation.terminal:
            raise RuntimeError("MAIN operation is already terminal")
        self._port(port).continue_(on_transport_dispatch=operation.continue_requested)
        operation.continue_acknowledged()

    def await_stop(self, *, port: MainRdbgPort | None = None) -> StopEvent:
        """Observe the next stop over any number of bounded poll intervals."""

        rdbg = self._port(port)
        while True:
            try:
                return rdbg.wait_for_any_stop(timeout_s=self._poll_interval_s)
            except StopWaitIntervalElapsed:
                # A poll interval is not an execution deadline or target-loss proof.
                continue

    def _port(self, port: MainRdbgPort | None) -> MainRdbgPort:
        selected = self._rdbg if port is None else port
        if selected is None:
            raise RuntimeError("MAIN RDBG port is not bound")
        return selected

    @staticmethod
    def _checked_modify(result: object, variable: str) -> None:
        if getattr(result, "error_occurred", False):
            raise ProtocolError(
                f"Failed to modify {variable}: {getattr(result, 'error_text', '')}"
            )
