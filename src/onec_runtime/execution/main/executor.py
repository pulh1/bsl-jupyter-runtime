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

    def modify(self, variable: str, value_expression: str) -> object: ...

    def continue_(self) -> None: ...

    def wait_for_any_stop(self, *, timeout_s: float) -> StopEvent: ...


class MainExecutor:
    def __init__(self, rdbg: MainRdbgPort, *, poll_interval_s: float = 6.0) -> None:
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
    ) -> StopEvent:
        """Write one command and wait for its next stop without an execution deadline."""

        install_workspace()
        before_command_write()
        self._checked_modify(
            self._rdbg.modify("ТекущаяИнструкция", bsl_string_literal(instruction)),
            "ТекущаяИнструкция",
        )
        self._checked_modify(
            self._rdbg.modify("ИдентификаторКоманды", str(operation.command_id)),
            "ИдентификаторКоманды",
        )
        return self.resume(operation, before_continue=before_continue)

    def resume(
        self,
        operation: MainOperation,
        *,
        before_continue: Callable[[], None] | None = None,
    ) -> StopEvent:
        """Continue the same MAIN command after a user or CAPTURE stop."""

        if before_continue is not None:
            before_continue()
        self.continue_command(operation)
        return self.await_stop()

    def continue_command(self, operation: MainOperation) -> None:
        """Issue one Continue; its acknowledgement does not finish MAIN."""

        operation.continue_requested()
        self._rdbg.continue_()
        operation.continue_acknowledged()

    def await_stop(self) -> StopEvent:
        """Observe the next stop over any number of bounded poll intervals."""

        while True:
            try:
                return self._rdbg.wait_for_any_stop(timeout_s=self._poll_interval_s)
            except StopWaitIntervalElapsed:
                # A poll interval is not an execution deadline or target-loss proof.
                continue

    @staticmethod
    def _checked_modify(result: object, variable: str) -> None:
        if getattr(result, "error_occurred", False):
            raise ProtocolError(
                f"Failed to modify {variable}: {getattr(result, 'error_text', '')}"
            )
