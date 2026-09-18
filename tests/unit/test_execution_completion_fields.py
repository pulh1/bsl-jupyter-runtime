"""Completion schema policy stays private behind one controller ticket port."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from onec_runtime.errors import (
    CaptureValueAccessDeniedError, CaptureValueCheckError, ProtocolError,
)
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.runtime_models import RuntimeNamespaceSnapshot


def schema(*names: str) -> str:
    rows = ("R\t",) + tuple(f"R\t{name}" for name in names)
    return f"C\t{len(rows)}\n" + "\n".join(rows)


class Ticket:
    def __init__(self, result: object) -> None:
        self.result = result
        self.timeout: float | None = None
        self.detached = False
        self.settled = False

    def wait_initiator(self, timeout: float | None = None) -> object:
        self.timeout = timeout
        if isinstance(self.result, BaseException):
            raise self.result
        self.settled = True
        return self.result

    def detach_waiter(self) -> None:
        self.detached = True

    def status(self) -> object:
        return SimpleNamespace(settled=self.settled)


class Controller:
    def __init__(self, wire: str) -> None:
        self.wire = wire
        self.plans: list[object] = []

    def submit_completion_helper(self, plan: object) -> Ticket:
        self.plans.append(plan)
        plan.validate_current()  # type: ignore[attr-defined]
        result = EvaluationResult(
            UUID(int=1), "Строка", '"private debugger presentation"', False,
            value_string=self.wire,
        )
        return Ticket(plan.accept_result(result))  # type: ignore[attr-defined]


def service(controller: Controller, *, namespace=None, worker=None):
    from onec_runtime.execution.completion_fields import CompletionFieldsService

    namespace = namespace or (lambda: RuntimeNamespaceSnapshot(1, 2, ("Данные",)))
    worker = worker or (lambda: WorkerMaterializationSnapshot(3, ("worker-address",)))
    return CompletionFieldsService(
        controller,
        namespace_snapshot=namespace,
        worker_catalog_snapshot=worker,
    )


def test_completion_service_submits_one_trusted_bounded_schema_plan() -> None:
    controller = Controller(schema("Номер", "Название"))

    fields = service(controller).completion_fields(
        "e1cRuntimeКонтекст.Данные.Вложенные", table_row=True, timeout_s=0.25,
    )

    assert fields == ("Номер", "Название")
    assert len(controller.plans) == 1
    plan = controller.plans[0]
    assert plan.expression.startswith(
        "RuntimeValueTransferServer."
        "СериализоватьДопущенныеИменаСвойствДляПодсказки("
        "e1cRuntimeКонтекст.Данные.Вложенные, Истина, "
    )
    assert '"worker-address"' in plan.expression
    assert plan.instruction == "Результат = " + plan.expression + ";"
    assert plan.max_text_size >= len(schema(*(f"F{i}" for i in range(128))))
    assert "worker-address" not in repr(plan)


@pytest.mark.parametrize("handle", [
    "e1cRuntimeКонтекст.Данные[0]", "e1cRuntimeКонтекст.Данные.Удалить()",
    "e1cRuntimeКонтекст.Данные;Удалить()", "e1cRuntimeКонтекст.Несуществующая",
    "e1cRuntimeКонтекст.RuntimeWorkerActiveGeneration", "e1cRuntimeКонтекст.Данные." + "А" * 510,
])
def test_completion_service_rejects_unsafe_or_unpublished_paths_before_submission(
    handle: str,
) -> None:
    controller = Controller(schema("Номер"))
    with pytest.raises(ProtocolError):
        service(controller).completion_fields(handle)
    assert controller.plans == []


def test_completion_service_rejects_invalid_row_option_before_submission() -> None:
    controller = Controller(schema("Номер"))
    with pytest.raises(ProtocolError):
        service(controller).completion_fields("e1cRuntimeКонтекст.Данные", table_row=1)  # type: ignore[arg-type]
    assert controller.plans == []


def test_completion_preserves_128_names_and_rejects_truncated_marker_page() -> None:
    names = tuple(f"Поле{index}" for index in range(128))
    controller = Controller(schema(*names))
    client = service(controller)
    assert client.completion_fields("e1cRuntimeКонтекст.Данные") == names

    controller.wire = "C\t129\n" + "\n".join(
        ("R\t",) + tuple(f"R\t{name}" for name in names[:127])
    )
    with pytest.raises(ProtocolError, match="Invalid completion field schema"):
        client.completion_fields("e1cRuntimeКонтекст.Данные")


@pytest.mark.parametrize("wire, expected", [
    ("C\t1\nD|worker_generation_value\tprivate", CaptureValueAccessDeniedError),
    ("C\t1\nE|value_admission_failed\tprivate", CaptureValueCheckError),
    ("C\t2\nR\t\nR\tИмя\nR\tЛишнее", ProtocolError),
    (schema("Имя", "имя"), ProtocolError),
    (schema("Invalid Name"), ProtocolError),
    ("private target payload", ProtocolError),
])
def test_completion_schema_failures_do_not_expose_private_wire(
    wire: str, expected: type[Exception],
) -> None:
    controller = Controller(wire)
    with pytest.raises(expected) as failure:
        service(controller).completion_fields("e1cRuntimeКонтекст.Данные")
    assert "private" not in str(failure.value)
    assert len(controller.plans) == 1


def test_completion_result_policy_rejects_debugger_error_without_raw_text() -> None:
    controller = Controller(schema("Номер"))
    service(controller).completion_fields("e1cRuntimeКонтекст.Данные")
    plan = controller.plans[0]
    result = EvaluationResult(
        UUID(int=2), "Строка", "private presentation", True,
        "private target error",
    )
    with pytest.raises(ProtocolError) as failure:
        plan.accept_result(result)
    assert "private" not in str(failure.value)


def test_completion_plan_rechecks_namespace_and_worker_before_remote_effect() -> None:
    controller = Controller(schema("Номер"))
    current = [RuntimeNamespaceSnapshot(1, 2, ("Данные",))]
    worker = [WorkerMaterializationSnapshot(3, ())]
    client = service(
        controller, namespace=lambda: current[0], worker=lambda: worker[0],
    )
    assert client.completion_fields("e1cRuntimeКонтекст.Данные") == ("Номер",)
    plan = controller.plans[-1]

    current[0] = RuntimeNamespaceSnapshot(1, 3, ("Данные",))
    with pytest.raises(ProtocolError, match="changed"):
        plan.validate_current()
    current[0] = RuntimeNamespaceSnapshot(1, 2, ("Данные",))
    worker[0] = WorkerMaterializationSnapshot(4, ())
    with pytest.raises(ProtocolError, match="changed"):
        plan.validate_current()


def test_completion_timeout_detaches_only_initiating_waiter() -> None:
    from onec_runtime.execution.completion_fields import CompletionFieldsService

    class PendingController:
        def __init__(self) -> None:
            self.ticket = Ticket(TimeoutError("local deadline"))

        def submit_completion_helper(self, plan):
            return self.ticket

    controller = PendingController()
    client = CompletionFieldsService(
        controller,
        namespace_snapshot=lambda: RuntimeNamespaceSnapshot(1, 2, ("Данные",)),
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    with pytest.raises(TimeoutError):
        client.completion_fields("e1cRuntimeКонтекст.Данные", timeout_s=0.1)
    assert controller.ticket.timeout == 0.1
    assert controller.ticket.detached
    assert not controller.ticket.settled


def test_completion_invalid_local_timeout_rejects_before_submission() -> None:
    controller = Controller(schema("Номер"))
    with pytest.raises(ProtocolError, match="finite positive"):
        service(controller).completion_fields("e1cRuntimeКонтекст.Данные", timeout_s=0)
    assert controller.plans == []


def test_completion_refuses_snapshot_change_while_ticket_waits() -> None:
    current = [RuntimeNamespaceSnapshot(1, 2, ("Данные",))]

    class ChangingController(Controller):
        def submit_completion_helper(self, plan):
            ticket = super().submit_completion_helper(plan)
            current[0] = RuntimeNamespaceSnapshot(1, 3, ("Данные",))
            return ticket

    controller = ChangingController(schema("Номер"))
    client = service(controller, namespace=lambda: current[0])
    with pytest.raises(ProtocolError, match="changed"):
        client.completion_fields("e1cRuntimeКонтекст.Данные")


def test_completion_rejects_raw_ticket_result_without_leaking_it() -> None:
    class RawController:
        def submit_completion_helper(self, plan):
            return Ticket("private target payload")

    from onec_runtime.execution.completion_fields import CompletionFieldsService

    client = CompletionFieldsService(
        RawController(),
        namespace_snapshot=lambda: RuntimeNamespaceSnapshot(1, 2, ("Данные",)),
        worker_catalog_snapshot=lambda: WorkerMaterializationSnapshot(0, ()),
    )
    with pytest.raises(ProtocolError) as failure:
        client.completion_fields("e1cRuntimeКонтекст.Данные")
    assert "private" not in str(failure.value)
