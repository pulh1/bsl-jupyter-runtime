from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Protocol
from uuid import UUID

from onec_runtime.errors import (
    CommandTimeout,
    ExtensionHandshakeError,
    ProtocolError,
    UnexpectedStop,
)
from onec_runtime.extension_bundle import EXTENSION_NAME, ExtensionHandshakeEvidence
from onec_runtime.kernel import SESSION_MODULE_PROPERTY_ID
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    ModifyResult,
    ModuleLocation,
    StopEvent,
)


class BootstrapSession(Protocol):
    break_on_next: bool

    def wait_for_any_stop(
        self,
        *,
        timeout_s: float,
        on_poll: Callable[[], None] | None = None,
    ) -> StopEvent: ...

    def continue_(self) -> None: ...


class ServerBootstrapSession(Protocol):
    expected_location: ModuleLocation
    target: DebugTarget | None

    def set_service_breakpoint(self) -> None: ...

    def continue_(self) -> None: ...

    def wait_for_service_stop(self, *, timeout_s: float) -> StopEvent: ...


class ServerGuardSession(Protocol):
    def evaluate(self, expression: str) -> EvaluationResult: ...

    def modify(self, variable: str, value_expression: str) -> ModifyResult: ...


@dataclass(frozen=True, slots=True)
class ExtensionHandshakeContract:
    product_id: str
    artifact_version: str
    protocol_version: str


_HANDSHAKE_EXPRESSIONS = (
    ("product_id", "ИдентификаторПродуктаRuntime"),
    ("artifact_version", "ВерсияАртефактаRuntime"),
    ("protocol_version", "ВерсияПротоколаRuntime"),
)
_MAX_HANDSHAKE_VALUE_LENGTH = 128


def _handshake_string(result: EvaluationResult, variable: str) -> str:
    if result.error_occurred:
        raise ExtensionHandshakeError(
            f"Extension handshake evaluation failed for {variable}"
        )
    if result.type_name != "Строка":
        raise ExtensionHandshakeError(
            f"Extension handshake variable {variable} is not a string"
        )
    presentation = result.presentation
    if (
        not presentation
        or len(presentation) > _MAX_HANDSHAKE_VALUE_LENGTH
        or any(ord(character) < 32 for character in presentation)
    ):
        raise ExtensionHandshakeError(
            f"Extension handshake variable {variable} has an unsafe value"
        )
    if presentation.startswith('"') or presentation.endswith('"'):
        if not (presentation.startswith('"') and presentation.endswith('"')):
            raise ExtensionHandshakeError(
                f"Extension handshake variable {variable} has an invalid presentation"
            )
        inner = presentation[1:-1]
        if '"' in inner.replace('""', ""):
            raise ExtensionHandshakeError(
                f"Extension handshake variable {variable} has an invalid presentation"
            )
        presentation = inner.replace('""', '"')
    if not presentation:
        raise ExtensionHandshakeError(
            f"Extension handshake variable {variable} has an empty value"
        )
    if len(presentation) > _MAX_HANDSHAKE_VALUE_LENGTH:
        raise ExtensionHandshakeError(
            f"Extension handshake variable {variable} has an unsafe value"
        )
    return presentation


def observe_extension_handshake(
    session: ServerGuardSession,
    *,
    target_type: str,
    location: ModuleLocation,
) -> ExtensionHandshakeEvidence:
    values = {
        field: _handshake_string(session.evaluate(variable), variable)
        for field, variable in _HANDSHAKE_EXPRESSIONS
    }
    return ExtensionHandshakeEvidence(
        target_type=target_type,
        product_id=values["product_id"],
        artifact_version=values["artifact_version"],
        protocol_version=values["protocol_version"],
        location=location,
    )


def verify_extension_handshake(
    session: ServerGuardSession,
    contract: ExtensionHandshakeContract,
    *,
    target_type: str,
    location: ModuleLocation,
) -> ExtensionHandshakeEvidence:
    evidence = observe_extension_handshake(
        session,
        target_type=target_type,
        location=location,
    )
    expected = (
        ("ИдентификаторПродуктаRuntime", evidence.product_id, contract.product_id),
        ("ВерсияАртефактаRuntime", evidence.artifact_version, contract.artifact_version),
        ("ВерсияПротоколаRuntime", evidence.protocol_version, contract.protocol_version),
    )
    for variable, actual, required in expected:
        if actual != required:
            raise ExtensionHandshakeError(
                f"Extension handshake mismatch for {variable}"
            )
    return evidence


def verify_extension_safe_mode_disabled(session: ServerGuardSession) -> None:
    """Verify the installed extension property in the live server infobase."""
    expression = (
        'РасширенияКонфигурации.Получить(Новый Структура("Имя", '
        f'"{EXTENSION_NAME}"))[0].БезопасныйРежим'
    )
    result = session.evaluate(expression)
    if (
        result.error_occurred
        or result.type_name not in {"Булево", "Boolean"}
        or result.presentation not in {"Ложь", "False"}
    ):
        raise ExtensionHandshakeError(
            "Runtime extension safe mode is enabled or could not be verified"
        )


@dataclass(frozen=True)
class GuardValueEvidence:
    type_name: str
    presentation: str
    error_occurred: bool


@dataclass(frozen=True)
class ServerLoopGuardEvidence:
    variable: str
    before: GuardValueEvidence
    write: GuardValueEvidence
    after: GuardValueEvidence

    def as_dict(self) -> dict[str, object]:
        return {
            "variable": self.variable,
            "before": asdict(self.before),
            "write": {"expression": "Истина", **asdict(self.write)},
            "after": asdict(self.after),
        }


def _guard_value(result: EvaluationResult | ModifyResult) -> GuardValueEvidence:
    return GuardValueEvidence(
        type_name=result.type_name,
        presentation=result.presentation,
        error_occurred=result.error_occurred,
    )


def _is_boolean(result: EvaluationResult | ModifyResult, presentation: str) -> bool:
    return (
        not result.error_occurred
        and result.type_name == "Булево"
        and result.presentation == presentation
    )


def enable_server_kernel_loop(
    session: ServerGuardSession,
) -> ServerLoopGuardEvidence:
    variable = "ПродолжатьЦикл"
    before = session.evaluate(variable)
    if not _is_boolean(before, "Ложь"):
        raise ProtocolError("Server KernelLoop guard was not Ложь before bootstrap")
    write = session.modify(variable, "Истина")
    if not _is_boolean(write, "Истина"):
        detail = f": {write.error_text}" if write.error_text else ""
        raise ProtocolError(f"Unable to enable server KernelLoop guard{detail}")
    after = session.evaluate(variable)
    if not _is_boolean(after, "Истина"):
        raise ProtocolError("Server KernelLoop guard readback was not Истина")
    return ServerLoopGuardEvidence(
        variable=variable,
        before=_guard_value(before),
        write=_guard_value(write),
        after=_guard_value(after),
    )


def _is_known_break_on_next_transient(
    session: BootstrapSession,
    location: ModuleLocation,
    expected: ModuleLocation,
) -> bool:
    if not session.break_on_next:
        return False
    return bool(
        (
            location.module_type == "ConfigModule"
            and location.property_id == UUID(SESSION_MODULE_PROPERTY_ID)
            and location.line > 0
        )
        or (
            location.module_type == expected.module_type
            and location.url == expected.url
            and location.object_id == expected.object_id
            and location.property_id == expected.property_id
            and location.extension_name == expected.extension_name
            and location.ext_id == expected.ext_id
            and 0 < location.line < expected.line
        )
    )


def wait_for_managed_startup_stop(
    session: BootstrapSession,
    expected: ModuleLocation,
    *,
    timeout_s: float,
    on_poll: Callable[[], None] | None = None,
) -> StopEvent:
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        remaining = max(0.1, deadline - monotonic())
        if on_poll is None:
            stop = session.wait_for_any_stop(timeout_s=remaining)
        else:
            stop = session.wait_for_any_stop(timeout_s=remaining, on_poll=on_poll)
        if stop.location == expected:
            return stop
        if _is_known_break_on_next_transient(session, stop.location, expected):
            session.continue_()
            continue
        raise UnexpectedStop(f"Unexpected managed bootstrap stop: {stop.location!r}")
    raise CommandTimeout("Timed out waiting for managed startup breakpoint")


def wait_for_server_entry_then_service(
    session: ServerBootstrapSession,
    entry_location: ModuleLocation,
    service_location: ModuleLocation,
    *,
    timeout_s: float,
    on_entry: Callable[[ServerBootstrapSession], None] | None = None,
    server_target_type: str = "ServerEmulation",
) -> tuple[StopEvent, StopEvent]:
    deadline = monotonic() + timeout_s

    def remaining() -> float:
        return max(0.1, deadline - monotonic())

    session.expected_location = entry_location
    session.set_service_breakpoint()
    session.continue_()
    entry_stop = session.wait_for_service_stop(timeout_s=remaining())
    if session.target is None or session.target.target_type != server_target_type:
        raise ProtocolError(f"Runtime server entry did not stop on {server_target_type}")
    if on_entry is not None:
        on_entry(session)

    session.expected_location = service_location
    session.set_service_breakpoint()
    session.continue_()
    service_stop = session.wait_for_service_stop(timeout_s=remaining())
    if session.target is None or session.target.target_type != server_target_type:
        raise ProtocolError(f"Runtime service loop did not stop on {server_target_type}")
    return entry_stop, service_stop
