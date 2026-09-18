"""CAPTURE source resolution can be rebound only between stopped scopes."""

from __future__ import annotations

import pytest

from onec_runtime.bsl.module_syntax import ModuleIdentity
from onec_runtime.bsl.source_maps import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.capture_inspection import ResolvedFrameSource
from onec_runtime.capture_source import SourceVersionRef
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.public_inspection import CaptureInspectionBridge
from onec_runtime.execution.public_facade import PublicExecutionFacade

from test_capture_inspection_bridge import _Controller
from test_capture_stack_inventory_adapter import ready_scope


def _resolved_source() -> ResolvedFrameSource:
    source = "Procedure RunFixture()\nEndProcedure"
    return ResolvedFrameSource(
        "Common.RunFixture", 1,
        ModuleIdentity("opaque", "worker", "common", "fixture", "Module"),
        SourceVersionRef.worker(
            artifact_id="bridge-rebind", generation=1, source_text=source,
        ),
    )


def test_bridge_rebinds_source_resolver_only_before_a_capture_scope() -> None:
    controller = _Controller(None)
    bridge = CaptureInspectionBridge(controller)
    resolved = _resolved_source()
    calls: list[tuple[int, ...]] = []

    def resolver(frames):
        calls.append(tuple(frame.level for frame in frames))
        return tuple(resolved if frame.level == 0 else None for frame in frames)

    bridge.configure_source_resolver(resolver)
    controller.capture_scope = ready_scope()
    assert bridge.current().stack[:1].frames[0].source == "Common.RunFixture"
    assert calls == [(0,)]

    with pytest.raises(ProtocolError, match="active CAPTURE scope"):
        bridge.configure_source_resolver(None)
    assert bridge.current().stack[:1].frames[0].source == "Common.RunFixture"

    controller.capture_scope.mark_closed()
    controller.capture_scope = None
    bridge.configure_source_resolver(None)


def test_facade_rebinds_resolver_without_creating_another_rdbg_owner() -> None:
    controller = _Controller(None)
    source = "Результат = 1;"
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL, "resolver-rebind", 1, source_sha256(source),
    )
    facade = PublicExecutionFacade(
        object(), controller, object(),
        source_unit_factory=lambda cell: unit,
        status_reader=lambda: object(),
    )
    resolved = _resolved_source()
    facade.configure_capture_source_resolver(
        lambda frames: tuple(resolved if frame.level == 0 else None for frame in frames),
    )
    controller.capture_scope = ready_scope()

    assert facade.capture_inspection().stack[:1].frames[0].source == "Common.RunFixture"
    with pytest.raises(ProtocolError, match="active CAPTURE scope"):
        facade.configure_capture_source_resolver(None)
