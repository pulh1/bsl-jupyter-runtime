"""Saved CAPTURE stack inventory can serve the existing public stack view."""

from uuid import UUID

import pytest

from onec_runtime.bsl.module_syntax import ModuleSyntaxRegistry
from onec_runtime.capture_inspection import LocalStackAdapter, RuntimeFrameMarker
from onec_runtime.errors import CaptureSourceUnavailableError, ProtocolError, StaleCaptureError
from onec_runtime.execution.capture.scope import CaptureScope
from onec_runtime.rdbg.models import ModuleLocation, StackFrame, StopEvent, TargetId


TARGET = TargetId(UUID(int=1), "test")
BUSINESS = ModuleLocation("ConfigModule", "file:///private/business.bsl", UUID(int=2), UUID(int=3), 10)
KERNEL = ModuleLocation("ExtensionModule", "file:///private/kernel.bsl", UUID(int=4), UUID(int=5), 20)
CALLER = ModuleLocation("ConfigModule", "file:///private/caller.bsl", UUID(int=6), UUID(int=7), 30)
STOP = StopEvent(
    TARGET,
    BUSINESS,
    "callStackFormed",
    stack=(BUSINESS, KERNEL, CALLER),
    stack_frames=(
        StackFrame(TARGET, 0, BUSINESS),
        StackFrame(TARGET, 2, KERNEL),
        StackFrame(TARGET, 3, CALLER),
    ),
)


def ready_scope() -> CaptureScope:
    scope = CaptureScope.from_stop(7, 42, STOP, 3)
    scope.record_locals(())
    scope.record_transfer("temporary-address")
    scope.record_kernel_frame(2)
    assert scope.record_main_command(42)
    scope.record_context_begun()
    scope.mark_ready()
    return scope


def test_saved_scope_stack_keeps_physical_levels_and_masks_kernel_frame() -> None:
    from onec_runtime.execution.capture.stack import CaptureStackInventoryAdapter

    scope = ready_scope()
    fence = object()
    checks: list[object] = []
    backend = CaptureStackInventoryAdapter(
        scope, fence, validate_current=lambda actual: checks.append(actual)
    )
    stack = LocalStackAdapter(
        backend,
        fence,
        resolve_sources=lambda frames: (None,) * len(frames),
        is_runtime_frame=backend.is_runtime_frame,
        registry=ModuleSyntaxRegistry(),
        command_timeout_s=1,
    ).stack

    visible = stack[:2]
    native = stack.native[:4]

    assert visible.total == 2
    assert [item.native_level for item in visible.frames if hasattr(item, "native_level")] == [0, 3]
    assert any(isinstance(item, RuntimeFrameMarker) for item in visible.frames)
    assert native.total == 4
    assert [item.native_level for item in native.frames] == [0, 2, 3]
    kernel = native.frames[1]
    assert kernel.runtime_kernel
    assert kernel.line is None
    assert kernel.physical is None
    assert kernel._value_scope is None
    with pytest.raises(CaptureSourceUnavailableError):
        _ = kernel.variables
    assert "private" not in str(visible) + str(native)
    assert checks == [fence] * 4


def test_stack_inventory_rejects_stale_fence_and_closed_scope_before_frames() -> None:
    from onec_runtime.execution.capture.stack import CaptureStackInventoryAdapter

    scope = ready_scope()
    fence = object()
    backend = CaptureStackInventoryAdapter(
        scope, fence, validate_current=lambda actual: None
    )

    with pytest.raises(StaleCaptureError):
        backend.read_stack(object())
    assert backend.read_stack(fence) == STOP.stack_frames
    scope.mark_closed()
    with pytest.raises(StaleCaptureError):
        backend.read_stack(fence)


def test_stack_inventory_rejects_incoherent_saved_levels_without_private_details() -> None:
    from onec_runtime.execution.capture.stack import CaptureStackInventoryAdapter

    scope = ready_scope()
    scope.stack_frames = (
        StackFrame(TARGET, 0, BUSINESS),
        StackFrame(TARGET, 0, KERNEL),
    )
    fence = object()
    backend = CaptureStackInventoryAdapter(
        scope, fence, validate_current=lambda actual: None
    )

    with pytest.raises(ProtocolError, match="stack inventory is invalid") as caught:
        backend.read_stack(fence)
    assert "private" not in str(caught.value)
