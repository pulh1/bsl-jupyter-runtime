"""Worker debug views admitted through the current universe registry."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from collections import deque
from dataclasses import replace

from onec_runtime.bsl import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
    SourceUnitKind,
    SourceUnitRef,
    VisibleSourceContext,
    WorkerModuleUnit,
    mapped_visible_source,
    source_sha256,
)
from onec_runtime.bsl.full_ast_worker_projection import parse_full_ast_module
from onec_runtime.bsl.module_universe import lower_resolved_worker_module
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.worker_dependency_resolver import resolve_worker_dependencies
from onec_runtime.execution.common import NotebookCommonParser
from onec_runtime.execution.snapshot_binding import RoutePreparationSnapshot, SnapshotRouteBinding
from onec_runtime.execution.worker_activation import WorkerUniverseActivationAdapter
from onec_runtime.execution.post_bootstrap import compose_fresh_post_bootstrap_execution
from onec_runtime.errors import BslExecutionError
from onec_runtime.experiment import bsl_string_literal
from onec_runtime.rdbg.models import EvaluationResult
from onec_runtime.worker_universe import ServerWorkerUniverseRegistry, WorkerUniverseRegistry

from test_worker_universe import _UniverseTargetExecutor, _builder, _notebook_builder
from test_execution_controller_routes import CompleteSession
from test_execution_route_sequence import BUSINESS, CAPTURE_STOP, KERNEL, MAIN_STOP


def worker_debug_view(
    tmp_path: Path,
    *,
    name: str = "МодульА",
    revision: int = 17,
    source: str | None = None,
):
    """Return a confirmed target-bound view for one ordinary common module."""
    if source is None:
        source = (
            "Функция Версия() Экспорт\n"
            f'    Возврат "{name}-{revision}";\n'
            "КонецФункции\n"
        )
    reference = SourceUnitRef(SourceUnitKind.MODULE, name, revision, source_sha256(source))
    catalog = CommonModuleCatalogSnapshot.create(
        profile="server-test",
        preprocessor_profile="server",
        revision=1,
        modules=(CommonModuleDescriptor(name, CommonModuleScope.SERVER),),
    )
    lowered = lower_resolved_worker_module(
        WorkerModuleUnit(name, "module", revision, mapped_visible_source(source, reference)),
        resolve_worker_dependencies(parse_full_ast_module(source), catalog),
    )
    context = VisibleSourceContext({reference: source})
    fixture_root = tmp_path / f"worker-view-{uuid4().hex}"
    fixture_root.mkdir()
    builder, _, _ = _builder(fixture_root)
    artifact = builder.build(lowered, visible_source_context=context)
    host = WorkerUniverseRegistry(runtime_generation=1, context_generation=1)
    target = _UniverseTargetExecutor()
    registry = ServerWorkerUniverseRegistry(host, target)
    candidate = host.prepare((artifact,))
    target.acknowledge(candidate)
    registry.promote(candidate)
    pin = host.pin_active()
    try:
        return host._operation_debug_view(pin)
    finally:
        registry.release_pin(pin)


def notebook_debug_views(
    tmp_path: Path,
    cells: tuple[tuple[str, SourceUnitRef], ...],
):
    """Publish notebook cells through the current Worker activation route."""
    fixture_root = tmp_path / f"notebook-views-{uuid4().hex}"
    fixture_root.mkdir()
    parser = PythonParserTarget.from_generated()
    host = WorkerUniverseRegistry(runtime_generation=1, context_generation=1)
    target = _UniverseTargetExecutor()
    port = object()

    def execute(supplied_port: object, source: str):
        assert supplied_port is port
        candidate = host._pending
        if candidate is not None:
            target.acknowledge(candidate)
        return target(source)

    adapter = WorkerUniverseActivationAdapter(
        host,
        notebook_builder=_notebook_builder(fixture_root),
        instruction_runner=execute,
        worker_breakpoints_present=lambda: False,
        target_profile="server-test",
    )
    owner = object()
    views = []
    for version, (source, reference) in enumerate(cells, 1):
        published = adapter.snapshot()
        common = NotebookCommonParser(parser).prepare(source, reference)
        snapshot = RoutePreparationSnapshot(
            owner, version, (), published.worker_exports, published.active_methods,
            published.active_handle,
        )
        intent = SnapshotRouteBinding(parser, owner=owner, version=version).worker_intent(
            common, snapshot.for_pipeline(),
        )
        assert intent is not None
        adapter.activate(intent, port=port)
        pin = host.pin_active()
        try:
            views.append(host._operation_debug_view(pin))
        finally:
            # The saved debug view owns immutable generation metadata.
            host.release_pin(pin)
    return tuple(views)


class PublicNotebookSession(CompleteSession):
    """Script public MAIN/CAPTURE replies and exact Worker mutation receipts."""

    def __init__(self, *, captured: bool = False) -> None:
        super().__init__(capture_count=0)
        main = replace(MAIN_STOP, stop_by_breakpoint=True)
        capture = replace(CAPTURE_STOP, stop_by_breakpoint=True)
        self.stops = deque(((capture,) if captured else ()) + (main,) * 24)
        self.worker_target = _UniverseTargetExecutor()
        self.worker_host: WorkerUniverseRegistry | None = None
        self.worker_instructions: list[str] = []
        self.main_completions = 0
        self.message_values: tuple[str, ...] = ()
        self.capture_error: str | None = None
        self.lose_capture_reply = False
        self.command_writes: list[tuple[str, str]] = []

    def modify(self, variable, value_expression, *, on_transport_dispatch):
        self.command_writes.append((variable, value_expression))
        return super().modify(
            variable, value_expression, on_transport_dispatch=on_transport_dispatch,
        )

    def wait_evaluation_event(self, pending, *, timeout_s, on_transport_dispatch):
        expression = self.expression
        instruction = self._worker_instruction(expression)
        if instruction is not None:
            host = self.worker_host
            assert host is not None
            candidate = host._pending
            if candidate is not None:
                self.worker_target.acknowledge(candidate)
            self.worker_instructions.append(instruction)
            try:
                value = self.worker_target(instruction)
            except BslExecutionError as error:
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Ошибка", "", True, str(error),
                )
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            if type(value) is bool:
                return EvaluationResult(
                    pending.result_id, "Булево", "Истина" if value else "Ложь", False,
                )
            return EvaluationResult(
                pending.result_id, "Строка", bsl_string_literal(str(value)), False,
            )
        if expression == "ЗавершеннаяКоманда":
            self.main_completions += 1
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Число", str(self.main_completions), False,
            )
        if expression == "ИдентификаторКоманды":
            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Число", str(self.main_completions + 1), False,
            )
        if expression.startswith(
            "RuntimeKernelServer.ЗабратьСообщенияЯчейкиИзКонтекста("
        ):
            import json

            on_transport_dispatch()
            self._record("wait_eval")
            self.pending = None
            return EvaluationResult(
                pending.result_id, "Строка",
                bsl_string_literal(json.dumps(self.message_values, ensure_ascii=False)),
                False,
            )
        if expression.startswith(
            "RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки("
        ):
            if self.lose_capture_reply:
                on_transport_dispatch()
                raise OSError("scripted CAPTURE response loss")
            if self.capture_error is not None:
                error = self.capture_error
                self.capture_error = None
                on_transport_dispatch()
                self._record("wait_eval")
                self.pending = None
                return EvaluationResult(
                    pending.result_id, "Ошибка", "", True, error,
                )
        return super().wait_evaluation_event(
            pending, timeout_s=timeout_s,
            on_transport_dispatch=on_transport_dispatch,
        )

    @staticmethod
    def _worker_instruction(expression: str) -> str | None:
        if "onec-worker-" not in expression:
            return None
        for prefix in (
            "RuntimeKernelServer.ВыполнитьКодВКонтекстеMain(e1cRuntimeКонтекст, ",
            "RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(e1cRuntimeКонтекст, ",
        ):
            if expression.startswith(prefix):
                literal = expression[len(prefix):]
                assert literal.startswith('"') and literal.endswith('")')
                return literal[1:-2].replace('""', '"')
        return None


def public_notebook_runtime(tmp_path: Path, *, captured: bool = False):
    """Compose one real public Worker and notebook route over a scripted target."""
    fixture_root = tmp_path / f"public-notebook-{uuid4().hex}"
    fixture_root.mkdir()
    session = PublicNotebookSession(captured=captured)
    composed = compose_fresh_post_bootstrap_execution(
        session, KERNEL, runtime_generation=1,
        stopped_target=session.target,
        capture_locations=(BUSINESS,) if captured else (),
        notebook_builder=_notebook_builder(fixture_root),
    )
    session.worker_host = composed.worker_universe
    return composed.execution.facade, composed, session
