"""Bounded completion schemas through a controller-owned MAIN/CAPTURE ticket.

The controller chooses the confirmed stopped route and evaluates the trusted
plan through its arbiter worker. This service owns path admission, namespace
and Worker snapshot checks, private result decoding, and the caller's local
wait. No debugger session or route token is available here.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING, Protocol

from onec_runtime.capture_evaluation import AdmissionEnvelopeV1
from onec_runtime.errors import (
    CaptureValueAccessDeniedError, CaptureValueCheckError, ProtocolError,
)
from onec_runtime.execution.local_wait import (
    LocalWaitTicket, validate_local_wait_timeout, wait_initiator_locally,
)
from onec_runtime.execution.value_reference import validate_public_direct_handle
from onec_runtime.execution.worker_activation import WorkerMaterializationSnapshot
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.table_value import evaluation_to_python

if TYPE_CHECKING:
    from onec_runtime.runtime_api import RuntimeNamespaceSnapshot


MAX_COMPLETION_TEXT_CHARS = 20_000
_MAX_EXPRESSION_CHARS = 307_200
_DIRECT_COMPLETION_PATH = re.compile(
    r"Контекст\.[^\W\d]\w*(?:\.[^\W\d]\w*){0,7}\Z", re.UNICODE,
)
_FIELD_NAME = re.compile(r"[^\W\d]\w*\Z", re.UNICODE)


class CompletionHelperController(Protocol):
    """Submit one fenced helper plan on MAIN idle or CAPTURE ready.

    The implementation must select and recheck the exact route/target under
    the arbiter, call ``plan.validate_current()`` before the first remote
    effect, and apply ``plan.accept_result`` on its worker before settling the
    ticket. It must not publish a raw EvaluationResult or debugger error.
    """

    def submit_completion_helper(
        self, plan: CompletionFieldsPlan,
    ) -> LocalWaitTicket[tuple[str, ...]]: ...


@dataclass(frozen=True, slots=True)
class CompletionFieldsPlan:
    """Trusted scalar helper source and private bounded result policy."""

    expression: str = field(repr=False)
    instruction: str = field(repr=False)
    _validate_current: Callable[[], None] = field(repr=False, compare=False)
    max_text_size: int = MAX_COMPLETION_TEXT_CHARS

    def validate_current(self) -> None:
        """Recheck namespace and Worker catalog inside the admitted ticket."""

        self._validate_current()

    def accept_result(self, result: EvaluationResult) -> tuple[str, ...]:
        """Reduce the private RDBG result to admitted names on the worker."""

        if (
            not isinstance(result, EvaluationResult)
            or result.error_occurred
            or result.type_name != "Строка"
        ):
            raise ProtocolError("Invalid completion field schema")
        try:
            wire = evaluation_to_python(result)
        except Exception:
            raise ProtocolError("Invalid completion field schema") from None
        return parse_completion_fields_wire(wire)


class CompletionFieldsService:
    """One public completion entry point for both confirmed stopped routes."""

    def __init__(
        self,
        controller: CompletionHelperController,
        *,
        namespace_snapshot: Callable[[], RuntimeNamespaceSnapshot],
        worker_catalog_snapshot: Callable[[], WorkerMaterializationSnapshot],
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        if not callable(namespace_snapshot) or not callable(worker_catalog_snapshot):
            raise TypeError("Completion snapshot readers must be callable")
        if not callable(wait_handoff):
            raise TypeError("Completion wait handoff must be callable")
        self._controller = controller
        self._namespace_snapshot = namespace_snapshot
        self._worker_catalog_snapshot = worker_catalog_snapshot
        self._wait_handoff = wait_handoff

    def completion_fields(
        self, handle: str, *, table_row: bool = False, timeout_s: float = 1.0,
    ) -> tuple[str, ...]:
        """Read current admitted names; timeout limits only the local wait."""

        try:
            local_wait = validate_local_wait_timeout(timeout_s)
        except ValueError:
            raise ProtocolError(
                "command timeout must be a finite positive number"
            ) from None
        if (
            not isinstance(handle, str)
            or len(handle) > 512
            or _DIRECT_COMPLETION_PATH.fullmatch(handle) is None
            or type(table_row) is not bool
        ):
            raise ProtocolError("Completion requires a direct or dotted Context path")
        validate_public_direct_handle(handle)
        namespace = self._read_namespace()
        if handle.split(".")[1].casefold() not in {
            name.casefold() for name in namespace.names
        }:
            raise ProtocolError("Completion root is not in the current namespace")
        worker = self._read_worker_catalog()
        registrations = worker.registrations
        if any("\n" in item or "\r" in item for item in registrations):
            raise ProtocolError("Completion Worker type registrations are invalid")
        expression = (
            "RuntimeValueTransferServer."
            "СериализоватьДопущенныеИменаСвойствДляПодсказки("
            + handle
            + (", Истина, " if table_row else ", Ложь, ")
            + bsl_string_literal("\n".join(registrations))
            + ")"
        )
        if len(expression) > _MAX_EXPRESSION_CHARS:
            raise ProtocolError("Completion helper expression is too large")

        def validate_current() -> None:
            if (
                self._read_namespace() != namespace
                or self._read_worker_catalog() != worker
            ):
                raise ProtocolError("Completion namespace or Worker catalog changed")

        plan = CompletionFieldsPlan(
            expression,
            "Результат = " + expression + ";",
            validate_current,
            MAX_COMPLETION_TEXT_CHARS,
        )
        ticket = self._controller.submit_completion_helper(plan)
        result = wait_initiator_locally(
            ticket, timeout_s=local_wait, wait_handoff=self._wait_handoff,
        )
        validate_current()
        return _validate_fields(result)

    def _read_namespace(self) -> RuntimeNamespaceSnapshot:
        from onec_runtime.runtime_api import RuntimeNamespaceSnapshot

        snapshot = self._namespace_snapshot()
        if not isinstance(snapshot, RuntimeNamespaceSnapshot):
            raise ProtocolError("Completion namespace snapshot is invalid")
        return snapshot

    def _read_worker_catalog(self) -> WorkerMaterializationSnapshot:
        snapshot = self._worker_catalog_snapshot()
        if not isinstance(snapshot, WorkerMaterializationSnapshot):
            raise ProtocolError("Completion Worker catalog is invalid")
        return snapshot


def parse_completion_fields_wire(wire: object) -> tuple[str, ...]:
    """Decode the marker and at most 128 names without exposing raw rows."""

    if not isinstance(wire, str) or len(wire) > MAX_COMPLETION_TEXT_CHARS:
        raise ProtocolError("Invalid completion field schema")
    rows = wire.splitlines()
    if not 2 <= len(rows) <= 130:
        raise ProtocolError("Invalid completion field schema")
    header_kind, separator, declared_size = rows[0].partition("\t")
    if (
        header_kind != "C"
        or not separator
        or re.fullmatch(r"[1-9]\d*", declared_size) is None
    ):
        raise ProtocolError("Invalid completion field schema")
    row_count = int(declared_size)
    if not 1 <= row_count <= 129 or len(rows) - 1 != row_count:
        raise ProtocolError("Invalid completion field schema")
    names: list[str] = []
    for index, row in enumerate(rows[1:]):
        outcome, separator, name = row.partition("\t")
        if not separator:
            raise ProtocolError("Invalid completion field schema")
        if outcome == AdmissionEnvelopeV1.denied():
            raise CaptureValueAccessDeniedError(
                "Worker generation objects are not public values"
            )
        if outcome == AdmissionEnvelopeV1.failed():
            raise CaptureValueCheckError("CAPTURE value admission failed")
        if outcome != "R":
            raise ProtocolError("Invalid completion admission result")
        if index == 0:
            if name:
                raise ProtocolError("Invalid completion admission result")
        else:
            names.append(name)
    return _validate_fields(tuple(names))


def _validate_fields(fields: object) -> tuple[str, ...]:
    if type(fields) is not tuple or len(fields) > 128:
        raise ProtocolError("Invalid completion field schema")
    seen: set[str] = set()
    for name in fields:
        if (
            not isinstance(name, str)
            or len(name) > 128
            or _FIELD_NAME.fullmatch(name) is None
            or name.casefold() in seen
        ):
            raise ProtocolError("Invalid completion field name")
        seen.add(name.casefold())
    return fields
