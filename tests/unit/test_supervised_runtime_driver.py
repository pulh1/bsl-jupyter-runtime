from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

import pytest

from onec_runtime.config import RuntimeConfig
from onec_runtime.controller_worker import PhaseBarrier
from onec_runtime.errors import ProtocolError
from onec_runtime.fault_injection import FaultPoint
from onec_runtime.prototype_runtime import OperationState
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    ModifyResult,
    StopEvent,
    TargetId,
)
from onec_runtime.rdbg.transport import TranscriptEntry
from integration.support.supervised_runtime_driver import OneCRuntimeDriver
from onec_runtime.supervisor_protocol import MessageKind


TARGET_ID = TargetId(UUID("11111111-1111-1111-1111-111111111111"), "DefAlias")


def runtime_config(tmp_path: Path) -> RuntimeConfig:
    platform = tmp_path / "bin"
    platform.mkdir()
    for name in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform / name).touch()
    return RuntimeConfig(Path(__file__).parents[2], platform)


class FakeTransport:
    def __init__(
        self,
        events: list[object],
        transcript,  # type: ignore[no-untyped-def]
    ) -> None:
        self.events = events
        self.transcript = transcript

    def close(self) -> None:
        self.events.append("transport.close")


class FakeSession:
    def __init__(
        self,
        transport: FakeTransport,
        expected_location,  # type: ignore[no-untyped-def]
        events,
    ) -> None:
        self.transport = transport
        self.expected_location = expected_location
        self.events = events
        self.break_on_next = True
        self.target: DebugTarget | None = None
        self.guard_reads = 0

    def initialize(self) -> None:
        self.events.append("session.initialize")

    def set_service_breakpoint(self) -> None:
        self.events.append(("session.breakpoint", self.expected_location))

    def wait_for_any_stop(self, *, timeout_s: float) -> StopEvent:
        self.events.append(("session.wait_managed", timeout_s))
        self.target = DebugTarget(TARGET_ID, "ManagedClient", "stopped")
        return StopEvent(TARGET_ID, self.expected_location, "callStackFormed")

    def modify(self, name: str, expression: str) -> ModifyResult:
        self.events.append(("session.modify", name, expression))
        return ModifyResult(UUID(int=1), "Булево", "Истина", False)

    def evaluate(self, expression: str) -> EvaluationResult:
        self.guard_reads += 1
        presentation = "Ложь" if self.guard_reads == 1 else "Истина"
        self.events.append(("session.evaluate", expression, presentation))
        return EvaluationResult(UUID(int=1), "Булево", presentation, False)

    def continue_(self) -> None:
        self.events.append("session.continue")

    def wait_for_service_stop(self, *, timeout_s: float) -> StopEvent:
        self.events.append(("session.wait_server", timeout_s))
        self.target = DebugTarget(TARGET_ID, "ServerEmulation", "stopped")
        return StopEvent(TARGET_ID, self.expected_location, "callStackFormed")

    def detach(self) -> None:
        self.events.append("session.detach")


class WrongServerSession(FakeSession):
    def wait_for_service_stop(self, *, timeout_s: float) -> StopEvent:
        stop = super().wait_for_service_stop(timeout_s=timeout_s)
        self.target = DebugTarget(TARGET_ID, "ManagedClient", "stopped")
        return stop


class EnableLoopFailureSession(FakeSession):
    def modify(self, name: str, expression: str) -> ModifyResult:
        self.events.append(("session.modify", name, expression))
        return ModifyResult(UUID(int=1), "Ошибка", "", True, "loop rejected")


class FakeController:
    def __init__(self, events: list[object], **kwargs: object) -> None:
        self.events = events
        self.kwargs = kwargs
        self.state = OperationState.IDLE

    def execute_main(self, source: str, *, capture_points=()):  # type: ignore[no-untyped-def]
        self.events.append(("controller.execute_main", source, capture_points))
        journal = self.kwargs["journal"]
        journal.record("write-journal.jsonl", "captured")  # type: ignore[attr-defined]
        journal.flush()  # type: ignore[attr-defined]
        self.state = OperationState.CAPTURED
        self.kwargs["fault_hook"](FaultPoint.AFTER_CAPTURE_CHECKPOINT)  # type: ignore[operator]
        return object()

    def resume(self, *, dirty_roots=()):  # type: ignore[no-untyped-def]
        self.events.append(("controller.resume", dirty_roots))
        if dirty_roots:
            self.kwargs["fault_hook"](FaultPoint.AFTER_FIRST_ROOT_WRITE)  # type: ignore[operator]
        self.kwargs["fault_hook"](FaultPoint.AFTER_CONTINUE_ACK)  # type: ignore[operator]
        self.state = OperationState.MAIN_PENDING
        return object()


def make_driver(
    tmp_path: Path,
    *,
    session_type: type[FakeSession] = FakeSession,
) -> tuple[OneCRuntimeDriver, list[object], list[FakeController]]:
    events: list[object] = []
    controllers: list[FakeController] = []

    def transport_factory(host: str, port: int, *, transcript):  # type: ignore[no-untyped-def]
        events.append(("transport.create", host, port))
        return FakeTransport(events, transcript)

    def session_factory(transport, location, *, break_on_next):  # type: ignore[no-untyped-def]
        events.append(("session.create", break_on_next))
        return session_type(transport, location, events)

    def controller_factory(session, location, **kwargs):  # type: ignore[no-untyped-def]
        events.append(("controller.create", session, location, kwargs))
        controller = FakeController(events, **kwargs)
        controllers.append(controller)
        return controller

    driver = OneCRuntimeDriver(
        runtime_config(tmp_path),
        generation_id=17,
        debug_port=1550,
        run_dir=tmp_path,
        transport_factory=transport_factory,
        session_factory=session_factory,
        controller_factory=controller_factory,
    )
    return driver, events, controllers


def test_prepare_debug_ui_initializes_only_ui_and_startup_breakpoint(
    tmp_path: Path,
) -> None:
    driver, events, _ = make_driver(tmp_path)

    driver.prepare_debug_ui()

    assert [event[0] if isinstance(event, tuple) else event for event in events] == [
        "transport.create",
        "session.create",
        "session.initialize",
        "session.breakpoint",
    ]


def test_attach_runtime_enables_server_loop_and_requires_server_target(
    tmp_path: Path,
) -> None:
    driver, events, _ = make_driver(tmp_path)
    driver.prepare_debug_ui()

    driver.attach_runtime()

    names = [event[0] if isinstance(event, tuple) else event for event in events]
    assert names[-10:] == [
        "session.wait_managed",
        "session.evaluate",
        "session.modify",
        "session.evaluate",
        "session.breakpoint",
        "session.continue",
        "session.wait_server",
        "session.breakpoint",
        "session.continue",
        "session.wait_server",
    ]
    assert ("session.modify", "ПродолжатьЦикл", "Истина") in events
    startup_breakpoint = events[3][1]  # type: ignore[index]
    entry_breakpoint = events[8][1]  # type: ignore[index]
    server_breakpoint = events[11][1]  # type: ignore[index]
    assert startup_breakpoint != entry_breakpoint != server_breakpoint


def test_attach_runtime_rejects_a_non_server_emulation_target(tmp_path: Path) -> None:
    driver, _, _ = make_driver(tmp_path, session_type=WrongServerSession)
    driver.prepare_debug_ui()

    with pytest.raises(ProtocolError, match="ServerEmulation"):
        driver.attach_runtime()


def test_attach_runtime_rejects_failed_server_loop_enable(tmp_path: Path) -> None:
    driver, events, _ = make_driver(tmp_path, session_type=EnableLoopFailureSession)
    driver.prepare_debug_ui()

    with pytest.raises(ProtocolError, match="loop rejected"):
        driver.attach_runtime()

    assert "session.continue" not in events


@pytest.mark.parametrize(
    ("point", "expected_resume"),
    [
        (FaultPoint.AFTER_CAPTURE_CHECKPOINT, None),
        (FaultPoint.AFTER_FIRST_ROOT_WRITE, ("Скаляр", "Результат")),
        (FaultPoint.AFTER_CONTINUE_ACK, ()),
    ],
)
def test_execute_phase_reaches_the_exact_controller_fault_hook(
    tmp_path: Path,
    point: FaultPoint,
    expected_resume: tuple[str, ...] | None,
) -> None:
    driver, events, _ = make_driver(tmp_path)
    driver.prepare_debug_ui()
    driver.attach_runtime()
    reached: list[object] = []
    barrier = PhaseBarrier(
        point=point,
        publish=lambda kind, payload: reached.append((kind, payload)),
        flush=lambda: reached.append("flushed"),
        wait=lambda: reached.append("waiting"),
    )

    result = driver.execute_phase(point, barrier)

    assert result is None
    assert reached == [
        "flushed",
        (MessageKind.PHASE_REACHED, {"point": point.value}),
        "waiting",
    ]
    resume_calls = [
        event
        for event in events
        if isinstance(event, tuple) and event[0] == "controller.resume"
    ]
    assert resume_calls == (
        [] if expected_resume is None else [("controller.resume", expected_resume)]
    )
    execute = next(
        event
        for event in events
        if isinstance(event, tuple) and event[0] == "controller.execute_main"
    )
    assert execute[1] == "СинтетическийРезультат = СинтетическийCapture(40);"
    assert len(execute[2]) == 2
    controller_create = next(
        event
        for event in events
        if isinstance(event, tuple) and event[0] == "controller.create"
    )
    assert controller_create[3]["command_timeout_s"] == 90.0
    assert controller_create[3]["runtime_generation"] == 17

    operation = json.loads(
        (tmp_path / "operation-events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert operation["original_stream"] == "write-journal.jsonl"
    assert operation["generation"] == 17
    assert operation["phase"] == point.value


def test_rdbg_transcript_persists_only_hashes_and_safe_metadata(tmp_path: Path) -> None:
    driver, _, _ = make_driver(tmp_path)
    request = b"<password>secret-request-value</password>"
    response = b"<token>secret-response-value</token>"

    driver._transport.transcript(  # type: ignore[attr-defined]
        TranscriptEntry(10, "test", request, 200, response, 2.0, "secret-error")
    )

    evidence = (tmp_path / "rdbg-transcript.jsonl").read_text(encoding="utf-8")
    payload = json.loads(evidence)
    assert payload | {"timestamp": "ignored"} == {
        "timestamp": "ignored",
        "monotonic_ns": 10,
        "event": "rdbg_exchange",
        "command": "test",
        "status_code": 200,
        "duration_ms": 2.0,
        "error_present": True,
        "request_bytes": len(request),
        "request_sha256": sha256(request).hexdigest(),
        "response_bytes": len(response),
        "response_sha256": sha256(response).hexdigest(),
    }
    assert "secret-request-value" not in evidence
    assert "secret-response-value" not in evidence
    assert "secret-error" not in evidence


def test_close_detaches_and_closes_only_rdbg_resources(tmp_path: Path) -> None:
    driver, events, _ = make_driver(tmp_path)

    driver.close()
    driver.close()

    assert events[-2:] == ["session.detach", "transport.close"]
    assert events.count("session.detach") == 1
    assert events.count("transport.close") == 1


def test_status_never_exposes_an_arbitrary_runtime_object(tmp_path: Path) -> None:
    driver, _, _ = make_driver(tmp_path)
    secret = object()
    driver._controller = type(  # type: ignore[attr-defined]
        "RawController", (), {"state": secret}
    )()

    assert driver.status() == {"state": "unknown"}
