"""Bounded Session/MCP stack and frame metadata over a fenced CAPTURE stop."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from time import monotonic
from typing import Protocol

from onec_runtime.capture_inspection import DebugFrame
from onec_runtime.capture_values import MAX_TYPE_CHARS
from onec_runtime.errors import (
    ProtocolError, StaleCaptureError,
)
from onec_runtime.execution.capture.inspection import (
    MAX_NATIVE_VARIABLE_INVENTORY, TypedNativeVariable, TypedNativeVariablePage,
)
from onec_runtime.execution.capture.public_inspection import (
    CaptureInspection, CaptureInspectionBridge,
)
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.execution.local_wait import (
    LocalWaitStatus, validate_local_wait_timeout, wait_initiator_locally,
)
from onec_runtime.rdbg.models import FrameVariable


class _CaptureTicket(Protocol):
    def wait_initiator(self, timeout: float | None = None) -> object: ...
    def detach_waiter(self) -> None: ...
    def status(self) -> LocalWaitStatus: ...


class _CaptureScopeOwner(Protocol):
    capture_scope: CaptureScope | None

    def request_stop(self, ticket: _CaptureTicket) -> object: ...

    def submit_capture_variable(
        self, name: str, *, stack_level: int = 0,
    ) -> _CaptureTicket: ...

    def submit_capture_typed_variable_page(
        self, *, stack_level: int, start: int, stop: int,
    ) -> _CaptureTicket: ...


class SessionCaptureInspectionAdapter:
    """Map saved stack metadata without exposing RDBG URLs or presentations.

    RuntimeSession must check its external CaptureFence before delegation.
    The bridge and controller recheck their exact internal stop on each read.
    ``command_timeout_s`` bounds only the caller's local wait, with the same
    default used by the current Session; it never cancels an admitted ticket.
    """

    def __init__(
        self,
        controller: _CaptureScopeOwner,
        bridge: CaptureInspectionBridge,
        *,
        command_timeout_s: float = 90.0,
        clock: Callable[[], float] = monotonic,
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        if not isinstance(bridge, CaptureInspectionBridge):
            raise TypeError("CAPTURE inspection bridge is required")
        if not callable(clock):
            raise TypeError("CAPTURE inspection clock is invalid")
        if not callable(wait_handoff):
            raise TypeError("CAPTURE ticket wait handoff is invalid")
        try:
            selected_timeout = validate_local_wait_timeout(command_timeout_s)
        except ValueError as error:
            raise ValueError("command_timeout_s must be finite and positive") from error
        if selected_timeout is None:
            raise ValueError("command_timeout_s must be finite and positive")
        self._controller = controller
        self._bridge = bridge
        self._clock = clock
        self._wait_handoff = wait_handoff
        self._command_timeout_s = selected_timeout

    def capture_stack(
        self, *, cursor: int, limit: int, timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        """Page physical frames; mask all runtime-kernel coordinates."""

        _page(cursor, limit, "stack")
        deadline = self._deadline(timeout_s)
        scope = self._scope()
        inspection = self._bridge.current()
        frames = scope.stack_frames
        if cursor > len(frames):
            raise ProtocolError("capture stack cursor exceeds frames")
        page = frames[cursor:cursor + limit]
        result = tuple(
            _frame_wire(inspection.stack.native[frame.level])
            for frame in page
        )
        self._require_scope(scope)
        self._check_deadline(deadline)
        next_cursor = cursor + len(page)
        return {
            "frames": result,
            "total": len(frames),
            "next_cursor": next_cursor if next_cursor < len(frames) else None,
        }

    def capture_frame_variables(
        self, *, filters: Mapping[str, object], cursor: int, limit: int,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        """Page saved root-frame metadata with safe Context handles."""

        if not isinstance(filters, Mapping) or set(filters) - {"name", "type", "role"}:
            raise ProtocolError("capture frame filters are invalid")
        expected = {
            key: value.casefold()
            for key, value in filters.items()
            if isinstance(value, str) and value
        }
        if len(expected) != len(filters):
            raise ProtocolError("capture frame filters must be non-empty strings")
        _page(cursor, limit, "frame")
        deadline = self._deadline(timeout_s)
        scope = self._scope()
        inspection = self._bridge.current()
        level = scope.frame_stack_level
        if level is None:
            raise ProtocolError("CAPTURE root frame is unavailable")
        inspection.frame(level)
        items = tuple(
            metadata
            for metadata in _saved_root_metadata(scope)
            if (
                ("name" not in expected or expected["name"] in str(metadata["name"]).casefold())
                and ("type" not in expected or expected["type"] in str(metadata["type_name"]).casefold())
                and ("role" not in expected or expected["role"] == "local")
            )
        )
        if cursor > len(items):
            raise ProtocolError("capture frame cursor exceeds metadata")
        page = items[cursor:cursor + limit]
        self._require_scope(scope)
        self._check_deadline(deadline)
        next_cursor = cursor + len(page)
        return {
            "items": tuple({
                **item,
                "role": "local",
                "handle": "e1cRuntimeКонтекст.КонтекстОтладки." + str(item["name"]),
            } for item in page),
            "total": len(items),
            "next_cursor": next_cursor if next_cursor < len(items) else None,
        }

    def capture_frame(
        self,
        *,
        level: int,
        cursor: int,
        limit: int,
        name: str | None = None,
        timeout_s: float | None = None,
    ) -> Mapping[str, object]:
        """Read saved root metadata or one named variable through a ticket."""

        if type(level) is not int or level < 0:
            raise ProtocolError("capture frame level is invalid")
        _page(cursor, limit, "frame")
        deadline = self._deadline(timeout_s)
        scope = self._scope()
        inspection = self._bridge.current()
        try:
            frame = inspection.frame(level)
        except IndexError as error:
            raise ProtocolError("capture frame level is unavailable") from error
        if frame.runtime_kernel:
            raise ProtocolError("runtime kernel frame variables are unavailable")
        if name is not None:
            return self._named_frame_variable(
                scope, inspection, frame, name, cursor, deadline,
            )
        if level != scope.frame_stack_level:
            return self._typed_frame_page(
                scope, inspection, frame, cursor, limit, deadline,
            )
        variables = _saved_root_metadata(scope)
        if cursor > len(variables):
            raise ProtocolError("capture frame cursor exceeds variables")
        page = variables[cursor:cursor + limit]
        self._require_scope(scope)
        self._check_deadline(deadline)
        next_cursor = cursor + len(page)
        return {
            "frame": _frame_wire(frame),
            "variables": page,
            "total": len(variables),
            "next_cursor": next_cursor if next_cursor < len(variables) else None,
        }

    def _named_frame_variable(
        self,
        scope: CaptureScope,
        inspection: CaptureInspection,
        frame: DebugFrame,
        name: str,
        cursor: int,
        deadline: float | None,
    ) -> Mapping[str, object]:
        if not name or len(name) > 256 or not name.isidentifier():
            raise ProtocolError("capture variable name is invalid")
        if cursor != 0:
            raise ProtocolError("named capture variable cursor must be zero")
        self._require_scope(scope)
        ticket = self._controller.submit_capture_variable(
            name, stack_level=frame.native_level,
        )
        try:
            remaining = self._remaining(deadline)
        except TimeoutError:
            if not ticket.status().settled:
                ticket.detach_waiter()
            raise
        result = wait_initiator_locally(
            ticket,
            timeout_s=remaining,
            wait_handoff=self._wait_handoff,
            request_stop=lambda: self._controller.request_stop(ticket),
        )
        # The ticket may settle after the stop was released. Recheck the
        # bridge's stop fence before returning any of its private result.
        inspection.frame(frame.native_level)
        self._require_scope(scope)
        self._check_deadline(deadline)
        if not isinstance(result, FrameVariable):
            raise ProtocolError("capture variable result is invalid")
        variable = _variable_wire(result)
        if result.name.casefold() != name.casefold():
            raise ProtocolError("capture variable result is invalid")
        size = result.collection_size
        if size is not None:
            if type(size) is not int or not 0 <= size <= 10_000:
                raise ProtocolError("capture variable result is invalid")
            variable = {**variable, "collection_size": size}
        return {
            "frame": _frame_wire(frame),
            "variables": (variable,),
            "total": 1,
            "next_cursor": None,
        }

    def _typed_frame_page(
        self,
        scope: CaptureScope,
        inspection: CaptureInspection,
        frame: DebugFrame,
        cursor: int,
        limit: int,
        deadline: float | None,
    ) -> Mapping[str, object]:
        self._require_scope(scope)
        ticket = self._controller.submit_capture_typed_variable_page(
            stack_level=frame.native_level, start=cursor, stop=cursor + limit,
        )
        try:
            remaining = self._remaining(deadline)
        except TimeoutError:
            if not ticket.status().settled:
                ticket.detach_waiter()
            raise
        result = wait_initiator_locally(
            ticket, timeout_s=remaining, wait_handoff=self._wait_handoff,
            request_stop=lambda: self._controller.request_stop(ticket),
        )
        inspection.frame(frame.native_level)
        self._require_scope(scope)
        self._check_deadline(deadline)
        if (
            not isinstance(result, TypedNativeVariablePage)
            or type(result.total) is not int
            or not 0 <= result.total <= MAX_NATIVE_VARIABLE_INVENTORY
            or cursor > result.total
            or type(result.variables) is not tuple
            or len(result.variables) > limit
        ):
            raise ProtocolError("capture typed frame page is invalid")
        variables = tuple(_typed_variable_wire(item) for item in result.variables)
        next_cursor = cursor + len(variables)
        expected_next = next_cursor if next_cursor < result.total else None
        if result.next_cursor != expected_next:
            raise ProtocolError("capture typed frame page is invalid")
        return {
            "frame": _frame_wire(frame),
            "variables": variables,
            "total": result.total,
            "next_cursor": expected_next,
        }

    def _scope(self) -> CaptureScope:
        scope = self._controller.capture_scope
        if not isinstance(scope, CaptureScope):
            raise ProtocolError("CAPTURE inspection requires an active scope")
        return scope

    def _require_scope(self, scope: CaptureScope) -> None:
        if self._controller.capture_scope is not scope:
            raise StaleCaptureError()

    def _deadline(self, timeout_s: float | None) -> float | None:
        selected = validate_local_wait_timeout(timeout_s)
        if selected is None:
            selected = self._command_timeout_s
        return self._clock() + min(selected, self._command_timeout_s)

    def _check_deadline(self, deadline: float | None) -> None:
        if deadline is not None and self._clock() >= deadline:
            raise TimeoutError("Local CAPTURE inspection interval elapsed")

    def _remaining(self, deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("Local CAPTURE inspection interval elapsed")
        return remaining


def _page(cursor: int, limit: int, label: str) -> None:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ProtocolError(f"capture {label} page is invalid")


def _frame_wire(frame: DebugFrame) -> Mapping[str, object]:
    if not isinstance(frame, DebugFrame):
        raise ProtocolError("capture frame metadata is invalid")
    if frame.runtime_kernel:
        return {
            "level": frame.native_level,
            "runtime_kernel": True,
            "module_type": None,
            "object_id": None,
            "property_id": None,
            "line": None,
            "extension_name": None,
        }
    physical = frame.physical
    if physical is None:
        raise ProtocolError("capture frame coordinates are unavailable")
    return {
        "level": frame.native_level,
        "runtime_kernel": False,
        "module_type": physical.module_type,
        "object_id": str(physical.object_id),
        "property_id": str(physical.property_id),
        "line": frame.line,
        "extension_name": physical.extension_name,
    }


def _variable_wire(variable: FrameVariable) -> Mapping[str, object]:
    if (
        not isinstance(variable, FrameVariable)
        or not isinstance(variable.name, str)
        or not variable.name.isidentifier()
        or len(variable.name) > 256
        or not isinstance(variable.type_name, str)
        or not 0 < len(variable.type_name) <= 4096
    ):
        raise ProtocolError("capture frame variable metadata is invalid")
    return {"name": variable.name, "type_name": variable.type_name}


def _saved_root_metadata(scope: CaptureScope) -> tuple[Mapping[str, object], ...]:
    variables = scope.frame_variables
    if type(variables) is not tuple or len(variables) > MAX_NATIVE_VARIABLE_INVENTORY:
        raise ProtocolError("capture root frame inventory is invalid")
    metadata = tuple(_variable_wire(variable) for variable in variables)
    names = tuple(str(item["name"]).casefold() for item in metadata)
    if len(set(names)) != len(names):
        raise ProtocolError("capture root frame inventory is invalid")
    return metadata


def _typed_variable_wire(variable: TypedNativeVariable) -> Mapping[str, object]:
    if (
        not isinstance(variable, TypedNativeVariable)
        or not isinstance(variable.name, str)
        or not variable.name.isidentifier()
        or len(variable.name) > 256
        or not isinstance(variable.type_name, str)
        or not 0 < len(variable.type_name) <= MAX_TYPE_CHARS
        or variable.collection_size is not None
        and (
            type(variable.collection_size) is not int
            or not 0 <= variable.collection_size <= MAX_NATIVE_VARIABLE_INVENTORY
        )
    ):
        raise ProtocolError("capture typed frame variable is invalid")
    result: dict[str, object] = {
        "name": variable.name,
        "type_name": variable.type_name,
    }
    if variable.collection_size is not None:
        result["collection_size"] = variable.collection_size
    return result
