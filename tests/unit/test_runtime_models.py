from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from typing import get_type_hints

import pytest

from onec_runtime.errors import ProtocolError


def test_public_contract_modules_import_without_legacy_implementations() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import onec_runtime.runtime_models; "
            "import onec_runtime.execution.continuation_models; "
            "assert 'onec_runtime.runtime_api' not in sys.modules; "
            "assert 'onec_runtime.prototype_runtime' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_public_session_and_jupyter_import_without_legacy_implementations() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from onec_runtime.session import RuntimeSession; "
            "from onec_runtime.execution.public_facade import PublicExecutionFacade; "
            "from onec_runtime_jupyter.extension import MAX_PROJECTION_POSITION; "
            "assert MAX_PROJECTION_POSITION == 10000000; "
            "assert 'onec_runtime.runtime_api' not in sys.modules; "
            "assert 'onec_runtime.prototype_runtime' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_public_contract_types_and_error_hierarchy() -> None:
    from onec_runtime.execution.continuation_models import (
        ContinuationAttemptEvidence,
        ContinuationAttemptSpec,
    )
    from onec_runtime.runtime_models import (
        CaptureCorrelationTicket,
        OperationState,
        PartialWritebackError,
        RuntimeNamespaceSnapshot,
        RuntimeReply,
        RuntimeReplyKind,
        RuntimeStatus,
    )

    assert ContinuationAttemptEvidence.__module__ == "onec_runtime.execution.continuation_models"
    assert ContinuationAttemptSpec.__module__ == "onec_runtime.execution.continuation_models"
    assert OperationState.__module__ == "onec_runtime.runtime_models"
    assert CaptureCorrelationTicket.__module__ == "onec_runtime.runtime_models"
    assert RuntimeNamespaceSnapshot.__module__ == "onec_runtime.runtime_models"
    assert RuntimeReply.__module__ == "onec_runtime.runtime_models"
    assert RuntimeReplyKind.__module__ == "onec_runtime.runtime_models"
    assert RuntimeStatus.__module__ == "onec_runtime.runtime_models"
    assert issubclass(PartialWritebackError, ProtocolError)


def test_runtime_reply_fields_and_wire_enum_values_remain_stable() -> None:
    from onec_runtime.runtime_models import OperationState, RuntimeReply, RuntimeReplyKind

    assert OperationState.MAIN_PENDING.value == "main_pending"
    assert RuntimeReplyKind.CAPTURED.value == "captured"
    assert [field.name for field in fields(RuntimeReply)] == [
        "kind", "operation_id", "state", "result", "error", "succeeded",
        "location", "stop_sequence", "messages", "changed_roots",
        "capture_ticket", "observed_command_id", "capture_dirty_roots",
        "diagnostic", "debug_stop",
    ]
    reply = RuntimeReply(RuntimeReplyKind.CAPTURED, 7, OperationState.CAPTURED)
    with pytest.raises(FrozenInstanceError):
        reply.operation_id = 8  # type: ignore[misc]


def test_public_runtime_contract_annotations_remain_resolvable() -> None:
    from onec_runtime.runtime_models import RuntimeReply, RuntimeStatus
    from onec_runtime.rdbg.models import ModuleLocation

    assert ModuleLocation in get_type_hints(RuntimeReply)["location"].__args__
    assert "worker_generation" in get_type_hints(RuntimeStatus)


@pytest.mark.parametrize(
    ("attempt_id", "generation", "operation_id", "roots"),
    [
        ("", 1, "op", ("Root",)),
        ("attempt", True, "op", ("Root",)),
        ("attempt", 0, "op", ("Root",)),
        ("attempt", 1, "", ("Root",)),
        ("attempt", 1, "op", ("Root", "root")),
        ("attempt", 1, "op", ("not a root",)),
        ("attempt", 1, "op", tuple(f"Root{i}" for i in range(101))),
    ],
)
def test_continuation_attempt_retains_bounded_validation(
    attempt_id: str,
    generation: int,
    operation_id: str,
    roots: tuple[str, ...],
) -> None:
    from onec_runtime.execution.continuation_models import ContinuationAttemptSpec

    with pytest.raises(ValueError):
        ContinuationAttemptSpec(attempt_id, generation, operation_id, roots)


def test_continuation_attempt_normalizes_root_container_to_tuple() -> None:
    from onec_runtime.execution.continuation_models import ContinuationAttemptSpec

    spec = ContinuationAttemptSpec("attempt", 1, "op", ["Root"])  # type: ignore[arg-type]
    assert spec.dirty_roots == ("Root",)


def test_namespace_snapshot_rejects_invalid_generations_and_casefold_collisions() -> None:
    from onec_runtime.runtime_models import RuntimeNamespaceSnapshot

    for generations, names in [
        ((0, 1), ("Name",)),
        ((1, 0), ("Name",)),
        ((1, 1), ("Name", "name")),
        ((1, 1), (" ",)),
    ]:
        with pytest.raises(ValueError):
            RuntimeNamespaceSnapshot(*generations, names)
