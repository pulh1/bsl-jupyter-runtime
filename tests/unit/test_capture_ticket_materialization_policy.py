"""Value and table transfer policy over one CAPTURE controller ticket."""

from base64 import b64encode
from decimal import Decimal
from hashlib import sha256
import json

import pytest

from onec_runtime.capture_evaluation import AdmissionEnvelopeV1
from onec_runtime.errors import CaptureValueCheckError, ProtocolError
from onec_runtime.table_materialization import ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions

from test_compact_table import compact_payload
from test_capture_stack_inventory_adapter import ready_scope
from test_capture_ticket_data_plane import Ticket, TicketController


class TransferPort:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.plans = []
        self.error: BaseException | None = None
        self.runtime_generation = 7
        self.context_generation = 4

    def materialize_private_payload(self, plan):
        self.plans.append(plan)
        if self.error is not None:
            raise self.error
        encoded = b64encode(self.payload).decode("ascii")
        envelope = AdmissionEnvelopeV1(
            self.runtime_generation,
            self.context_generation,
            len(self.payload),
            sha256(self.payload).hexdigest(),
            len(encoded),
        ).encode()
        plan.admit_metadata(envelope)
        return plan.decode(envelope, encoded)


def _policy(port):
    from onec_runtime.execution.capture.ticket_materialization import (
        CaptureTicketMaterializationPolicy,
    )

    return CaptureTicketMaterializationPolicy(
        port,
        runtime_generation=7,
        context_generation=4,
        worker_type_registrations=lambda: (),
    )


def test_direct_context_value_uses_integrity_checked_ticket_plan_and_decoder():
    payload = json.dumps({
        "version": 1, "root": {"t": "number", "v": "12.50"},
    }).encode("utf-8")
    port = TransferPort(payload)

    value = _policy(port).materialize_value(
        "Контекст.Сумма",
        MaterializationOptions(max_depth=4, max_items=20, max_bytes=4096),
    )

    assert value == Decimal("12.50")
    assert len(port.plans) == 1
    assert "Контекст.Сумма" in port.plans[0].instruction
    assert "СериализоватьЗначение" in port.plans[0].instruction
    assert port.plans[0].private_key in port.plans[0].cleanup_instruction


def test_direct_context_table_materializes_dataframe_on_same_ticket_port():
    port = TransferPort(compact_payload())

    frame = _policy(port).to_df(
        "Контекст.Таблица",
        ReferencePolicy(refs="uuid", ref_columns={"Employee": "both"}),
        max_rows=100,
        max_bytes=32_768,
    )

    assert frame["Name"].tolist() == ["Alice", "Bob"]
    assert "Контекст.Таблица" in port.plans[0].instruction
    assert "СериализоватьКомпактнуюТаблицу" in port.plans[0].instruction
    assert port.plans[0].private_key in port.plans[0].cleanup_instruction


@pytest.mark.parametrize("handle", ["capture_table_deferred", "Контекст.Сумма; Выполнить(Код)"])
def test_untrusted_or_deferred_handles_fail_before_ticket_dispatch(handle):
    port = TransferPort(b"unused")
    policy = _policy(port)

    with pytest.raises(ProtocolError):
        policy.materialize_value(handle, MaterializationOptions(max_bytes=4096))
    with pytest.raises(ProtocolError):
        policy.to_df(handle, ReferencePolicy(), max_rows=10, max_bytes=4096)
    assert port.plans == []


@pytest.mark.parametrize("handle", [
    "Контекст.RuntimeWorkerActiveGeneration",
    "Контекст.RuntimeWorkerPinnedOperationGeneration.Modules",
])
def test_private_worker_roots_fail_before_materialization_ticket(handle):
    port = TransferPort(b"unused")
    policy = _policy(port)

    with pytest.raises(ProtocolError, match="Worker generation"):
        policy.materialize_value(handle)
    with pytest.raises(ProtocolError, match="Worker generation"):
        policy.to_df(handle, max_rows=10)
    assert port.plans == []


def test_confirmed_transfer_error_allows_correction_and_retry_on_same_policy():
    payload = json.dumps({
        "version": 1, "root": {"t": "boolean", "v": True},
    }).encode("utf-8")
    port = TransferPort(payload)
    policy = _policy(port)
    port.error = CaptureValueCheckError("CAPTURE value admission failed")

    with pytest.raises(CaptureValueCheckError):
        policy.materialize_value("Контекст.Флаг")
    port.error = None
    assert policy.materialize_value("Контекст.Флаг") is True
    assert len(port.plans) == 2
    assert port.plans[0].private_key != port.plans[1].private_key


def test_generation_mismatch_is_rejected_by_plan_before_payload_decode():
    payload = json.dumps({
        "version": 1, "root": {"t": "boolean", "v": True},
    }).encode("utf-8")
    port = TransferPort(payload)
    port.context_generation = 5

    with pytest.raises(CaptureValueCheckError, match="admission"):
        _policy(port).materialize_value("Контекст.Флаг")


def test_value_policy_dispatches_through_fenced_controller_ticket_adapter():
    from onec_runtime.execution.capture.data_plane import CaptureTicketDataPlane

    payload = json.dumps({
        "version": 1, "root": {"t": "number", "v": "9"},
    }).encode("utf-8")
    scope = ready_scope()
    controller = TicketController(scope)
    controller.materialization_ticket = Ticket(payload)
    data_plane = CaptureTicketDataPlane(controller, scope)

    value = _policy(data_plane).materialize_value(
        "Контекст.Сумма", MaterializationOptions(max_bytes=4096),
    )

    assert value == Decimal("9")
    assert len(controller.requested) == 1
    assert controller.requested[0][0] == "materialization"
    assert "СериализоватьЗначение" in controller.requested[0][1].instruction


def test_bound_capture_transfer_rechecks_exact_worker_catalog_before_eval():
    from onec_runtime.execution.capture.ticket_materialization import (
        WorkerTransferCatalog, bind_capture_ticket_materialization,
    )

    payload = json.dumps({
        "version": 1, "root": {"t": "number", "v": "9"},
    }).encode("utf-8")
    scope = ready_scope()
    controller = TicketController(scope)
    controller.materialization_ticket = Ticket(payload)
    initial = WorkerTransferCatalog(4, ("registered-worker-type",))
    current = [initial]
    guards = []

    def submit(plan, *, _before_first_effect=None):
        guards.append(_before_first_effect)
        assert _before_first_effect is not None
        _before_first_effect()
        return controller.materialization_ticket

    controller.submit_capture_materialization = submit
    policy = bind_capture_ticket_materialization(
        controller, scope,
        runtime_generation=7, context_generation=4,
        worker_catalog_snapshot=lambda: current[0],
    )

    current[0] = WorkerTransferCatalog(5, ("registered-worker-type",))
    with pytest.raises(ProtocolError, match="Worker catalog changed"):
        policy.materialize_value("Контекст.Сумма")
    assert len(guards) == 1
    assert controller.capture_scope is scope
    current[0] = initial
    assert policy.materialize_value("Контекст.Сумма") == Decimal("9")
