from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
import json

import pytest

from onec_runtime.errors import (
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
    MaterializationLimitError,
    ProtocolError,
)
from onec_runtime.value_materialization import MaterializationOptions, ONEC_NULL
from onec_runtime.value_transfer_backend import (
    VALUE_CONTEXT_KEY_PREFIX,
    RuntimeValueTransfer,
    build_value_transfer_instruction,
)


KEY = VALUE_CONTEXT_KEY_PREFIX + "0123456789abcdef0123456789abcdef"


def test_capture_transfer_registers_key_before_dispatch_and_owns_cleanup():
    from onec_runtime.capture_evaluation import (
        CaptureEvaluationCoordinator,
        CaptureEvaluationKind,
    )
    from test_capture_evaluation_coordinator import Driver, FENCE

    payload = _payload()
    encoded = b64encode(payload).decode("ascii")
    drivers = [Driver(), Driver(), Driver()]
    coordinator = CaptureEvaluationCoordinator(FENCE, poll_interval_s=0.01)
    caller_cleanups = []
    def execute_plan(plan, evaluation_kind):
        assert evaluation_kind is CaptureEvaluationKind.MATERIALIZATION_HELPER
        assert plan.private_key == KEY
        def step(source):
            from onec_runtime.capture_evaluation import CaptureRemoteStep
            driver = drivers[2] if "Удалить" in source else drivers[0]
            if driver is drivers[2]:
                assert "Результат = Истина;" in source, "cleanup must satisfy the Worker instruction result contract"
            return CaptureRemoteStep(driver.dispatch, driver.poll, driver.restore)
        def read(context, key, maximum):
            from onec_runtime.capture_evaluation import CaptureRemoteStep
            assert key == KEY and maximum == 5464
            return context.execute_inline(CaptureRemoteStep(drivers[1].dispatch, drivers[1].poll)).presentation
        request = plan.capture_request(
            FENCE,
            step_factory=step,
            read=read,
            evaluation_kind=CaptureEvaluationKind.MATERIALIZATION_HELPER,
        )
        assert request.cleanup_leases[0].private_key == KEY
        ticket = coordinator.submit_evaluation(request)
        for driver, value in zip(drivers, [f"R|3|5|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}", encoded, ""]):
            from onec_runtime.rdbg.models import EvaluationResult
            driver.events.put(EvaluationResult(driver.pending.result_id, "Строка", value, False))
        return ticket.wait_initiator(1)
    try:
        transfer = RuntimeValueTransfer(
            lambda source: pytest.fail("caller dispatched CAPTURE"),
            lambda key, maximum: pytest.fail("caller read CAPTURE"),
            context_cleaner=caller_cleanups.append, runtime_generation=lambda: 3,
            context_generation=5, key_factory=lambda: KEY, capture_executor=execute_plan,
        )
        assert transfer.payload("Контекст.Значение", MaterializationOptions(max_bytes=1024)) == payload
        assert not caller_cleanups
        assert drivers[2].consumed == 1
    finally:
        coordinator.begin_close()
        assert coordinator.join(2)


def _payload() -> bytes:
    return json.dumps(
        {"version": 1, "root": {"t": "null"}},
        separators=(",", ":"),
    ).encode("utf-8")


def test_builds_bounded_recursive_value_instruction_for_safe_path() -> None:
    source = build_value_transfer_instruction(
        "Контекст.Документ.Товары",
        MaterializationOptions(
            refs="both", max_depth=9, max_items=123, max_bytes=4567
        ),
        KEY,
        runtime_generation=3,
        context_generation=5,
    )

    assert source.count("RuntimeValueTransferServer.СериализоватьЗначение(") == 1
    assert "Контекст.Документ.Товары, \"both\", 9, 123, 4567" in source
    assert f'Контекст.Вставить("{KEY}"' in source
    assert 'Формат(3, "ЧГ=0; ЧДЦ=0")' in source
    assert 'Формат(5, "ЧГ=0; ЧДЦ=0")' in source


def test_value_instruction_builds_protocol_two_admission_before_publication() -> None:
    source = build_value_transfer_instruction(
        "Контекст.Документ.Товары",
        MaterializationOptions(max_bytes=4567),
        KEY,
        runtime_generation=3,
        context_generation=5,
        worker_type_registrations=("Worker.Extension",),
    )

    assert 'ВнешниеОбработки.Создать("Worker.Extension", Ложь)' in source
    assert source.index("ТипыОбъектовWorker") < source.index(
        "RuntimeValueTransferServer.СериализоватьЗначение("
    )
    assert "4567, ТипыОбъектовWorker" in source
    assert source.index("Если Не МатериализацияЗначения.Доступ Тогда") < source.index(
        f'Контекст.Вставить("{KEY}"'
    )
    assert 'Результат = "D|worker_generation_value"' in source
    assert 'Результат = "E|value_admission_failed"' in source
    assert '"R|" + Формат(3, "ЧГ=0; ЧДЦ=0")' in source


@pytest.mark.parametrize(
    ("metadata", "error_type"),
    [
        ("D|worker_generation_value", CaptureValueAccessDeniedError),
        ("E|value_admission_failed", CaptureValueCheckError),
        ("3|5|33|" + "0" * 64 + "|44", CaptureValueCheckError),
    ],
)
def test_nonready_or_predecessor_value_metadata_never_fetches_payload(
    metadata: str, error_type: type[Exception]
) -> None:
    reads: list[str] = []
    transfer = RuntimeValueTransfer(
        lambda _source: metadata,
        lambda key, _maximum: reads.append(key) or "private-payload",
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
        context_cleaner=lambda _key: None,
    )

    with pytest.raises(error_type):
        transfer.payload("Контекст.Значение", MaterializationOptions())

    assert reads == []


@pytest.mark.parametrize(
    "handle",
    [
        "Таблица",
        "Контекст",
        "Контекст.Таблица[0]",
        "Контекст.Таблица.Метод()",
        "Контекст.Таблица; Сообщить(1)",
        "Контекст. Таблица",
        "Контекст.Таблица // comment",
    ],
)
def test_rejects_executable_or_non_context_value_paths(handle: str) -> None:
    with pytest.raises(ProtocolError, match="handle"):
        build_value_transfer_instruction(
            handle,
            MaterializationOptions(),
            KEY,
            runtime_generation=1,
            context_generation=1,
        )


def test_reads_one_atomic_context_value_and_decodes_snapshot() -> None:
    payload = _payload()
    encoded = b64encode(payload).decode("ascii")
    calls: list[str] = []
    reads: list[tuple[str, int]] = []
    transfer = RuntimeValueTransfer(
        lambda source: calls.append(source)
        or f"R|3|5|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}",
        lambda key, maximum: reads.append((key, maximum)) or encoded,
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
        context_cleaner=lambda _key: None,
    )

    result = transfer.materialize(
        "Контекст.Значение", MaterializationOptions(max_bytes=1024)
    )

    assert result is ONEC_NULL
    assert len(calls) == 1
    assert reads == [(KEY, 5464)]


def test_small_byte_limit_still_decodes_bounded_error_envelope() -> None:
    payload = json.dumps(
        {
            "version": 1,
            "error": {"kind": "byte_limit", "path": "$", "limit": 64},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = b64encode(payload).decode("ascii")
    reads: list[int] = []
    transfer = RuntimeValueTransfer(
        lambda _source: (
            f"R|1|1|{len(payload)}|{sha256(payload).hexdigest()}|{len(encoded)}"
        ),
        lambda _key, maximum: reads.append(maximum) or encoded,
        runtime_generation=lambda: 1,
        context_generation=1,
        key_factory=lambda: KEY,
        context_cleaner=lambda _key: None,
    )

    with pytest.raises(MaterializationLimitError, match="bytes"):
        transfer.materialize(
            "Контекст.Значение", MaterializationOptions(max_bytes=64)
        )

    assert reads == [5464]


@pytest.mark.parametrize(
    "metadata",
    [
        "3|5|33|bad|44",
        "3|5|0|" + "0" * 64 + "|44",
        "2|5|33|" + "0" * 64 + "|44",
        "3|4|33|" + "0" * 64 + "|44",
        "3|5|33|" + "0" * 64 + "|0",
        "not|numbers|here|" + "0" * 64 + "|44",
    ],
)
def test_rejects_invalid_transfer_metadata_after_atomic_take(metadata: str) -> None:
    reads: list[str] = []
    transfer = RuntimeValueTransfer(
        lambda _source: metadata,
        lambda key, _maximum: reads.append(key) or b64encode(_payload()).decode(),
        runtime_generation=lambda: 3,
        context_generation=5,
        key_factory=lambda: KEY,
        context_cleaner=lambda _key: None,
    )

    with pytest.raises(CaptureValueCheckError, match="CAPTURE value admission"):
        transfer.materialize("Контекст.Значение", MaterializationOptions())

    assert reads == []


def test_rejects_payload_hash_mismatch() -> None:
    payload = _payload()
    encoded = b64encode(payload).decode("ascii")
    transfer = RuntimeValueTransfer(
        lambda _source: f"R|1|1|{len(payload)}|{'0' * 64}|{len(encoded)}",
        lambda _key, _maximum: encoded,
        runtime_generation=lambda: 1,
        context_generation=1,
        key_factory=lambda: KEY,
        context_cleaner=lambda _key: None,
    )

    with pytest.raises(ProtocolError, match="integrity"):
        transfer.materialize("Контекст.Значение", MaterializationOptions())


def test_rejects_runtime_generation_change_before_instruction() -> None:
    generations = iter((3, 4))
    calls: list[str] = []
    transfer = RuntimeValueTransfer(
        lambda source: calls.append(source),
        lambda _key, _maximum: "",
        runtime_generation=lambda: next(generations),
        context_generation=5,
        key_factory=lambda: KEY,
        context_cleaner=lambda _key: None,
    )

    with pytest.raises(ProtocolError, match="generation is stale"):
        transfer.materialize("Контекст.Значение", MaterializationOptions())

    assert calls == []


def test_cleans_context_token_when_execute_outcome_is_unknown() -> None:
    cleaned: list[str] = []
    transfer = RuntimeValueTransfer(
        lambda _source: (_ for _ in ()).throw(ProtocolError("lost response")),
        lambda _key, _maximum: "",
        context_cleaner=cleaned.append,
        runtime_generation=lambda: 1,
        context_generation=1,
        key_factory=lambda: KEY,
    )

    with pytest.raises(ProtocolError, match="lost response"):
        transfer.materialize("Контекст.Значение", MaterializationOptions())

    assert cleaned == [KEY]


def test_preserves_primary_error_when_context_cleanup_also_fails() -> None:
    def fail_cleanup(_key: str) -> None:
        raise ProtocolError("cleanup failed")

    transfer = RuntimeValueTransfer(
        lambda _source: (_ for _ in ()).throw(ProtocolError("primary failed")),
        lambda _key, _maximum: "",
        context_cleaner=fail_cleanup,
        runtime_generation=lambda: 1,
        context_generation=1,
        key_factory=lambda: KEY,
    )

    with pytest.raises(ProtocolError, match="primary failed") as captured:
        transfer.materialize("Контекст.Значение", MaterializationOptions())

    assert any("cleanup failed" in note for note in captured.value.__notes__)
