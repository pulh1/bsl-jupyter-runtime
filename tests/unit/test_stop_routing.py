from __future__ import annotations

from uuid import UUID

import pytest

from onec_runtime.rdbg.models import ModuleLocation, StopEvent, TargetId
from onec_runtime.stop_routing import (
    BreakpointRegistry,
    StopReason,
    classify_stop,
)


TARGET = TargetId(UUID("22222222-2222-2222-2222-222222222222"), "DefAlias")
OBJECT = UUID("cb953767-f436-4a5b-9e09-13a67d6e0201")
PROPERTY = UUID("d5963243-262e-4398-b4d7-fb16d06484f6")


def location(line: int) -> ModuleLocation:
    return ModuleLocation(
        "ExtensionModule",
        "",
        OBJECT,
        PROPERTY,
        line,
        "OnecInteractiveRuntime",
    )


SERVICE = location(77)
CAPTURE_A = location(59)
CAPTURE_B = location(62)
USER = location(70)
UNKNOWN = location(99)


def stop(
    where: ModuleLocation,
    *,
    stop_by_breakpoint: bool = True,
    suspended_by_other: bool = False,
    runtime_error: str = "",
) -> StopEvent:
    return StopEvent(
        TARGET,
        where,
        "callStackFormed",
        stop_by_breakpoint=stop_by_breakpoint,
        suspended_by_other=suspended_by_other,
        stack=(where,),
        runtime_error=runtime_error,
    )


def test_registry_rejects_overlapping_locations() -> None:
    with pytest.raises(ValueError, match="overlap"):
        BreakpointRegistry(SERVICE, captures=(CAPTURE_A,), users=(CAPTURE_A,))


@pytest.mark.parametrize(
    ("event", "expected"),
    (
        (stop(SERVICE), StopReason.MAIN_SERVICE),
        (stop(CAPTURE_A), StopReason.CAPTURE),
        (stop(USER), StopReason.USER_BREAKPOINT),
        (stop(UNKNOWN), StopReason.UNKNOWN),
        (stop(SERVICE, suspended_by_other=True), StopReason.PAUSE),
        (stop(SERVICE, runtime_error="boom"), StopReason.EXCEPTION),
        (
            stop(SERVICE, stop_by_breakpoint=False),
            StopReason.UNKNOWN,
        ),
    ),
)
def test_classifies_stop_with_protocol_reason_precedence(
    event: StopEvent,
    expected: StopReason,
) -> None:
    registry = BreakpointRegistry(SERVICE, (CAPTURE_A,), (USER,))

    classified = classify_stop(event, registry)

    assert classified.event is event
    assert classified.reason is expected


def test_evaluation_workspace_excludes_only_captures() -> None:
    registry = BreakpointRegistry(SERVICE, (CAPTURE_A, CAPTURE_B), (USER,))

    assert registry.full_locations == (SERVICE, CAPTURE_A, CAPTURE_B, USER)
    assert registry.evaluation_locations == (SERVICE, USER)
