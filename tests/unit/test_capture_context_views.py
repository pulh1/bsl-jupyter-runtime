from dataclasses import FrozenInstanceError, replace
import traceback

import pytest

from onec_runtime.errors import (
    CaptureBusyError,
    CaptureEvaluationPendingError,
    CaptureLookupError,
    CapturePathError,
    CaptureSourceUnavailableError,
    CaptureValueAccessDeniedError,
    CaptureValueCheckError,
    StaleCaptureError,
)


def api():
    from onec_runtime import capture_values
    return capture_values


class Backend:
    def __init__(self):
        self.fence = object()
        self.valid_fence = self.fence
        self.context = [
            ("Локальная", "local-old"),
            ("ВторойПараметр", "second"),
            ("ПервыйПараметр", "first"),
        ]
        self.frame = list(self.context)
        self.calls = []
        self.private = set()

    def validate_inspection(self, fence):
        self.calls.append(("validate", fence))
        if fence is not self.valid_fence:
            raise CaptureBusyError("pending", "inspection", "evaluating")

    def resolve_value(self, fence, path):
        self.calls.append(("resolve", fence, path))
        raise AssertionError("scope inventory has no value root")

    def project_values(self, fence, request):
        self.calls.append(("project", fence, request))
        assert fence is self.valid_fence
        values = self.context if request.path.root.kind.value == "context" else self.frame
        entries = [self._entry(name, preview) for name, preview in values]
        if request.role.value == "parameters":
            by_name = {entry.name.casefold(): entry for entry in entries}
            entries = [by_name[name.casefold()] for name in request.parameter_names
                       if name.casefold() in by_name]
        elif request.role.value == "locals":
            parameters = {name.casefold() for name in request.parameter_names}
            entries = [entry for entry in entries if entry.name.casefold() not in parameters]
        if request.exact is not None:
            entries = [entry for entry in entries
                       if entry.name.casefold() == request.exact.casefold()]
        total = len(entries)
        entries = entries[request.start:request.stop]
        return api().PrivateValueProjection(tuple(entries), total,
                                            request.stop if request.stop < total else None)

    @staticmethod
    def _entry(name, preview):
        return api().PrivateProjectedValue(
            name,
            lambda: api().ValueMetadata("Строка", preview, None, api().ValueShape.SCALAR),
        )


def setup_adapter(*, parameters=("ПервыйПараметр", "ВторойПараметр"), resolver=None):
    module = api()
    backend = Backend()
    if resolver is None:
        resolver = lambda root: parameters
    adapter = module.LocalCaptureValueAdapter(
        backend, backend.fence,
        policy=module.CaptureValuePolicy(),
        resolve_parameters=resolver,
    )
    return adapter, backend


def test_context_pages_are_fresh_snapshots_and_frame_is_a_distinct_live_root():
    adapter, backend = setup_adapter()
    old_context = adapter.context.locals[:20]
    old_frame = adapter.frame(0).locals[:20]
    backend.context[0] = ("Локальная", "local-new")

    new_context = adapter.context.locals[:20]
    new_frame = adapter.frame(0).locals[:20]

    assert old_context.items[0].preview == "local-old"
    assert new_context.items[0].preview == "local-new"
    assert old_frame.items[0].preview == new_frame.items[0].preview == "local-old"
    assert old_context is not new_context
    with pytest.raises(FrozenInstanceError):
        old_context.items = ()
    project_calls = [call for call in backend.calls if call[0] == "project"]
    assert [call[2].path.root.kind.value for call in project_calls] == [
        "context", "frame", "context", "frame",
    ]


def test_variables_need_no_source_and_parameters_keep_source_order_without_duplication():
    resolutions = []
    def resolve(root):
        resolutions.append(root)
        return ("ПервыйПараметр", "ВторойПараметр")
    adapter, _ = setup_adapter(resolver=resolve)

    variables = adapter.context.variables[:20]
    assert [item.name for item in variables.items] == [
        "Локальная", "ВторойПараметр", "ПервыйПараметр",
    ]
    assert resolutions == []
    assert [item.name for item in adapter.context.parameters[:20].items] == [
        "ПервыйПараметр", "ВторойПараметр",
    ]
    assert [item.name for item in adapter.context.locals[:20].items] == ["Локальная"]
    assert 1 <= len(resolutions) <= 2


def test_exact_lookup_is_case_insensitive_and_missing_or_ambiguous_is_typed():
    adapter, backend = setup_adapter()
    assert adapter.context.locals["локальная"].name == "Локальная"
    with pytest.raises(CaptureLookupError, match="not found"):
        adapter.context.locals["НетТакой"]
    backend.context.append(("ЛОКАЛЬНАЯ", "duplicate"))
    with pytest.raises(CaptureLookupError, match="ambiguous"):
        adapter.context.locals["локальная"]


def test_parameters_and_locals_direct_to_variables_when_source_is_unavailable():
    def unavailable(root):
        raise CaptureSourceUnavailableError("PRIVATE C:/customer/source/Module.bsl")
    adapter, backend = setup_adapter(resolver=unavailable)
    assert adapter.context.variables[:1].items
    with pytest.raises(CaptureSourceUnavailableError, match="variables") as parameters_error:
        adapter.context.parameters[:1]
    with pytest.raises(CaptureSourceUnavailableError, match="variables") as locals_error:
        adapter.frame(7).locals[:1]
    for raised in (parameters_error.value, locals_error.value):
        rendered = "".join(traceback.format_exception(raised))
        assert "PRIVATE" not in rendered and "customer" not in rendered
        assert "PRIVATE" not in repr(raised)
        assert raised.__cause__ is None and raised.__context__ is None
    assert sum(call[0] == "project" for call in backend.calls) == 1


def test_unexpected_parameter_resolver_failure_is_sanitized_without_a_chain():
    def broken(root):
        raise RuntimeError("PRIVATE_SOURCE_PATH_C:/customer/x.bsl")
    adapter, backend = setup_adapter(resolver=broken)

    with pytest.raises(CaptureSourceUnavailableError, match="variables") as raised:
        adapter.context.parameters[:1]

    rendered = "".join(traceback.format_exception(raised.value))
    assert "PRIVATE_SOURCE_PATH" not in rendered and "customer" not in rendered
    assert "PRIVATE_SOURCE_PATH" not in repr(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert backend.calls == [("validate", backend.fence)]


@pytest.mark.parametrize(
    "key",
    [slice(None), slice(-1, 1), slice(0, 101), slice(0, 2, 2), -1, True],
)
def test_variable_views_reject_unbounded_or_invalid_pages_after_lifecycle_check(key):
    adapter, backend = setup_adapter()
    with pytest.raises((CapturePathError, TypeError)):
        adapter.context.variables[key]
    assert backend.calls == [("validate", backend.fence)]


def test_lifecycle_error_wins_over_unsafe_lookup_and_source_resolution():
    resolved = []
    adapter, backend = setup_adapter(resolver=lambda root: resolved.append(root) or ())
    backend.valid_fence = object()
    with pytest.raises(CaptureBusyError):
        adapter.context.locals["X); ВыполнитьОпасное(); //"]
    assert resolved == []
    assert backend.calls == [("validate", backend.fence)]


@pytest.mark.parametrize("unsafe", ["X); ВыполнитьОпасное(); //", "Для", "变量"])
def test_safe_lookup_rejects_non_bsl_identifier_without_projection(unsafe):
    adapter, backend = setup_adapter()
    with pytest.raises(CapturePathError, match="identifier"):
        adapter.context.variables[unsafe]
    assert [call[0] for call in backend.calls] == ["validate"]


def test_pages_and_descriptors_render_only_saved_bounded_data():
    adapter, backend = setup_adapter()
    page = adapter.context.variables[:2]
    calls = len(backend.calls)
    rendered = repr(page) + str(page) + repr(page.items[0]) + repr(adapter.context)
    assert "КонтекстОтладки" in rendered and "Локальная" in rendered
    assert len(backend.calls) == calls
    assert "local-old" in rendered
    assert "object at" not in rendered


def test_parameter_names_are_valid_unique_bsl_identifiers():
    adapter, backend = setup_adapter(parameters=("Арг", "АРГ"))
    with pytest.raises(CaptureSourceUnavailableError, match="classification"):
        adapter.context.parameters[:20]
    assert [call[0] for call in backend.calls] == ["validate"]


def test_adapter_filters_and_orders_roles_when_backend_returns_plain_debugger_order():
    adapter, backend = setup_adapter()
    original = backend.project_values
    def ignore_role(fence, request):
        return original(
            fence,
            replace(
                request,
                role=api().VariableRole.VARIABLES,
                parameter_names=(),
            ),
        )
    backend.project_values = ignore_role

    parameters = adapter.context.parameters[:20]
    locals_page = adapter.context.locals[:20]

    assert [item.name for item in parameters.items] == [
        "ПервыйПараметр", "ВторойПараметр",
    ]
    assert [item.name for item in locals_page.items] == ["Локальная"]
    assert parameters.total == 2 and locals_page.total == 1


@pytest.mark.parametrize("page_slice", [slice(0, 1), slice(1, 2), slice(1, 20)])
def test_partial_parameter_page_rejects_role_ignoring_backend(page_slice):
    adapter, backend = setup_adapter()
    original = backend.project_values
    def ignore_role(fence, request):
        return original(
            fence,
            replace(
                request,
                role=api().VariableRole.VARIABLES,
                parameter_names=(),
            ),
        )
    backend.project_values = ignore_role

    with pytest.raises(CaptureValueCheckError, match="parameter page"):
        adapter.context.parameters[page_slice]


@pytest.mark.parametrize(
    ("page_slice", "expected", "total", "next_cursor"),
    [
        (slice(0, 1), ["ПервыйПараметр"], 2, 1),
        (slice(1, 2), ["ВторойПараметр"], 2, None),
        (slice(1, 20), ["ВторойПараметр"], 2, None),
        (slice(20, 21), [], 2, None),
    ],
)
def test_parameter_pages_have_absolute_source_order_and_inventory_cursors(
    page_slice, expected, total, next_cursor,
):
    adapter, _ = setup_adapter()

    page = adapter.context.parameters[page_slice]

    assert [item.name for item in page.items] == expected
    assert page.total == total and page.next_cursor == next_cursor


@pytest.mark.parametrize("start", [0, 3, 99])
def test_zero_width_variable_slice_is_empty_terminal_without_source_or_projection(start):
    resolutions = []
    adapter, backend = setup_adapter(
        resolver=lambda root: resolutions.append(root) or ("ПервыйПараметр",),
    )
    page = adapter.context.parameters[start:start]
    assert page.items == () and page.total == 0 and page.next_cursor is None
    assert resolutions == []
    assert backend.calls == [("validate", backend.fence)]


def test_value_adapter_can_bind_native_frame_scope_without_changing_stack_coordinates():
    from onec_runtime.capture_inspection import DebugFrame

    adapter, _ = setup_adapter()
    original = DebugFrame(native_level=7, source="Common.Module", line=12)
    frame = adapter.bind_frame(original)
    assert frame.native_level == 7 and frame.source == "Common.Module"
    assert frame.variables[:1].items[0].name == "Локальная"
    assert frame.parameters[:1].items[0].name == "ПервыйПараметр"
    assert frame.locals[:1].items[0].name == "Локальная"
    with pytest.raises(CaptureSourceUnavailableError, match="not attached"):
        original.variables[:1]


def test_current_capture_exposes_a_typed_live_context_view() -> None:
    """The public capture view owns the already-tested local context adapter."""
    from onec_runtime.capture_values import CaptureContextView
    from test_capture_control_plane import _capture_runtime, close_owner

    runtime, controller, transport = _capture_runtime()
    try:
        capture = runtime.current_capture()

        assert isinstance(capture.context, CaptureContextView)
        assert capture.context is capture.context
    finally:
        close_owner(controller, transport)


def test_runtime_context_projection_is_lazy_and_uses_one_controller_inspection_plan():
    """Public descriptors are inert; consuming a page owns one coordinator plan."""
    from onec_runtime.capture_values import (
        PrivateProjectedValue, PrivateValueProjection, ValueMetadata, ValueShape,
    )
    from onec_runtime.prototype_runtime import CaptureValueInspectionPlan
    from onec_runtime.runtime_api import PrototypeRuntimeApi
    from test_prototype_runtime import CAPTURE_A, ScriptedSession, captured_controller

    plans = []
    metadata_calls = []

    def build(**kwargs):
        plans.append(kwargs)
        return CaptureValueInspectionPlan(
            'RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(Контекст, "")',
            lambda _result: PrivateValueProjection((
                PrivateProjectedValue(
                    "Оклад",
                    lambda: metadata_calls.append("describe") or ValueMetadata(
                        "Число", "55000", None, ValueShape.SCALAR,
                    ),
                ),
            ), 1, None),
        )

    session = ScriptedSession((CAPTURE_A,))
    controller = captured_controller(
        session, capture_value_inspection_builder=build,
    )
    runtime = PrototypeRuntimeApi(controller)
    owner = controller._capture_evaluation_coordinator
    assert owner is not None
    try:
        capture = runtime.current_capture()
        descriptor = capture.context.variables
        before = len(session.calls)

        assert plans == []
        page = descriptor[:1]

        assert [item.name for item in page.items] == ["Оклад"]
        assert metadata_calls == ["describe"]
        assert repr(page.items[0])
        assert metadata_calls == ["describe"]
        assert len(plans) == 1
        planned = plans[0]
        assert planned["action"] == "project"
        assert planned["path"].root.kind.value == "context"
        assert planned["request"].start == 0 and planned["request"].stop == 1
        assert planned["limit"] is None
        starts = [call for call in session.calls[before:] if call[0] == "evaluate"]
        assert len(starts) == 1
        assert starts[0][1][1] == controller.capture_kernel_stack_level
        assert owner.status(owner._fence).can_inspect
    finally:
        owner.begin_close()
        assert owner.join(2)


def test_runtime_context_projection_fails_closed_without_a_qualified_target_plan():
    """RDBG inventory metadata is never a fallback public value descriptor."""
    from test_capture_control_plane import _capture_runtime, close_owner

    runtime, controller, transport = _capture_runtime()
    try:
        capture = runtime.current_capture()
        before = len(transport.calls)

        with pytest.raises(CaptureSourceUnavailableError, match="not qualified"):
            capture.context.variables[:1]

        assert len(transport.calls) == before
    finally:
        close_owner(controller, transport)


def test_runtime_value_binding_checks_the_fence_before_path_validation_or_planning():
    from test_capture_control_plane import _capture_runtime, close_owner

    runtime, controller, transport = _capture_runtime()
    try:
        capture = runtime.current_capture()
        controller.stop_sequence += 1
        before = len(transport.calls)

        with pytest.raises(StaleCaptureError):
            capture.context.variables["X); ВыполнитьОпасное(); //"]

        assert len(transport.calls) == before
    finally:
        close_owner(controller, transport)


def test_runtime_value_binding_rejects_fabricated_root_path_before_planning():
    from onec_runtime.capture_values import SafeValuePath, ValueRoot, ValueRootKind
    from onec_runtime.prototype_runtime import CaptureValueInspectionPlan
    from test_prototype_runtime import CAPTURE_A, ScriptedSession, captured_controller

    planned = []

    def build(**_kwargs):
        planned.append(True)
        return CaptureValueInspectionPlan("Результат = Неопределено;", lambda _result: ())

    session = ScriptedSession((CAPTURE_A,))
    controller = captured_controller(
        session, capture_value_inspection_builder=build,
    )
    owner = controller._capture_evaluation_coordinator
    assert owner is not None
    try:
        before = len(session.calls)
        with pytest.raises(CaptureValueCheckError, match="root request"):
            controller.capture_value_inspection(
                "resolve",
                path=SafeValuePath(ValueRoot(ValueRootKind.CONTEXT)),
                request=None,
                limit=None,
                worker_type_registrations=(),
            )

        assert planned == []
        assert not any(call[0] == "evaluate" for call in session.calls[before:])
    finally:
        owner.begin_close()
        assert owner.join(2)


def test_runtime_value_binding_rechecks_the_fence_after_private_target_result():
    from onec_runtime.capture_values import (
        PrivateProjectedValue, PrivateValueProjection, ValueMetadata, ValueShape,
    )
    from onec_runtime.prototype_runtime import CaptureValueInspectionPlan
    from onec_runtime.runtime_api import PrototypeRuntimeApi
    from test_prototype_runtime import CAPTURE_A, ScriptedSession, captured_controller

    metadata_calls = []
    controller_ref = []

    def build(**_kwargs):
        def decode(_result):
            controller_ref[0].stop_sequence += 1
            return PrivateValueProjection((
                PrivateProjectedValue(
                    "Секрет",
                    lambda: metadata_calls.append("describe") or ValueMetadata(
                        "Строка", "private", None, ValueShape.SCALAR,
                    ),
                ),
            ), 1, None)

        return CaptureValueInspectionPlan(
            'RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(Контекст, "")',
            decode,
        )

    session = ScriptedSession((CAPTURE_A,))
    controller = captured_controller(
        session, capture_value_inspection_builder=build,
    )
    controller_ref.append(controller)
    runtime = PrototypeRuntimeApi(controller)
    owner = controller._capture_evaluation_coordinator
    assert owner is not None
    try:
        capture = runtime.current_capture()

        with pytest.raises(StaleCaptureError):
            capture.context.variables[:1]

        assert metadata_calls == []
    finally:
        owner.begin_close()
        assert owner.join(2)


def test_runtime_value_binding_prioritizes_stale_over_private_target_error():
    from onec_runtime.prototype_runtime import CaptureValueInspectionPlan
    from onec_runtime.runtime_api import PrototypeRuntimeApi
    from test_prototype_runtime import CAPTURE_A, ScriptedSession, captured_controller

    controller_ref = []

    def build(**_kwargs):
        def decode(_result):
            controller_ref[0].stop_sequence += 1
            raise CaptureValueAccessDeniedError("capture value is private")

        return CaptureValueInspectionPlan(
            'RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(Контекст, "")',
            decode,
        )

    session = ScriptedSession((CAPTURE_A,))
    controller = captured_controller(
        session, capture_value_inspection_builder=build,
    )
    controller_ref.append(controller)
    runtime = PrototypeRuntimeApi(controller)
    owner = controller._capture_evaluation_coordinator
    assert owner is not None
    try:
        with pytest.raises(StaleCaptureError):
            runtime.current_capture().context.variables[:1]
    finally:
        owner.begin_close()
        assert owner.join(2)


def test_runtime_value_binding_preserves_a_pending_coordinator_outcome():
    from onec_runtime.prototype_runtime import CaptureValueInspectionPlan
    from onec_runtime.runtime_api import PrototypeRuntimeApi
    from test_capture_evaluation_lifecycle import ControlledCaptureSession, close_owner
    from test_prototype_runtime import captured_controller

    def decode_pending(_result):
        raise AssertionError("pending plan must not decode")

    def build(**_kwargs):
        return CaptureValueInspectionPlan(
            'RuntimeKernelServer.ВыполнитьКодВКонтекстеОтладки(Контекст, "")',
            decode_pending,
        )

    transport = ControlledCaptureSession()
    controller = captured_controller(
        transport,
        command_timeout_s=0.02,
        capture_value_inspection_builder=build,
    )
    runtime = PrototypeRuntimeApi(controller)
    try:
        capture = runtime.current_capture()

        with pytest.raises(CaptureEvaluationPendingError):
            capture.context.variables[:1]

        status = capture.status()
        assert status.pending_evaluation_id is not None
        assert status.evaluation_kind is not None
        assert status.evaluation_kind.value == "inspection"
    finally:
        close_owner(controller, transport)


def test_runtime_value_binding_uses_the_checked_in_envelope_without_an_injected_builder():
    """The normal runtime path owns admission, payload read and cleanup together."""
    from base64 import b64encode
    from hashlib import sha256
    import json

    from onec_runtime.runtime_api import PrototypeRuntimeApi
    from test_prototype_runtime import CAPTURE_A, ScriptedSession, captured_controller, evaluation

    document = {
        "v": 1,
        "action": "project",
        "entries": [{
            "name": "Оклад",
            "denied": False,
            "type_name": "Число",
            "preview": "55000",
            "size": None,
            "shape": "scalar",
            "cycle": False,
        }],
        "total": 1,
        "next": None,
    }
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    base64_payload = b64encode(payload).decode("ascii")
    envelope = "R|1|1|{}|{}|{}".format(
        len(payload), sha256(payload).hexdigest(), len(base64_payload),
    )

    class EnvelopeSession(ScriptedSession):
        def __init__(self):
            super().__init__((CAPTURE_A,))
            self.projections = 0
            self.payload_reads = 0
            self.cleanups = 0

        def evaluate(self, expression, **kwargs):  # type: ignore[no-untyped-def]
            if "СпроецироватьЗначенияИнспекции" in expression:
                self.projections += 1
                return evaluation("Строка", f'"{envelope}"')
            if "ЗабратьКомпактнуюМатериализациюИзКонтекста" in expression:
                self.payload_reads += 1
                return evaluation("Строка", f'"{base64_payload}"')
            if "УдалитьМатериализациюИзКонтекста" in expression:
                self.cleanups += 1
                return evaluation("Булево", "Истина")
            return super().evaluate(expression, **kwargs)

    session = EnvelopeSession()
    controller = captured_controller(session)
    runtime = PrototypeRuntimeApi(controller)
    owner = controller._capture_evaluation_coordinator
    assert owner is not None
    try:
        page = runtime.current_capture().context.variables[:1]

        assert [item.name for item in page.items] == ["Оклад"]
        assert page.items[0].preview == "55000"
        assert session.projections == session.payload_reads == session.cleanups == 1
        assert owner.status(owner._fence).last_evaluation_kind.value == "inspection"
    finally:
        owner.begin_close()
        assert owner.join(2)


def test_runtime_value_binding_validates_the_complete_path_and_view_grammar_before_builder():
    from onec_runtime.capture_values import (
        SafeValuePath,
        ValueInspectionRequest,
        ValuePathSegmentKind,
        ValueRoot,
        ValueRootKind,
        ValueViewKind,
    )
    from test_prototype_runtime import CAPTURE_A, ScriptedSession, captured_controller

    planned = []

    def build(**_kwargs):
        planned.append(True)
        raise AssertionError("a fabricated request reached the target builder")

    root = SafeValuePath(ValueRoot(ValueRootKind.CONTEXT))
    invalid_requests = (
        ValueInspectionRequest(
            root, ValueViewKind.ARRAY_ITEMS, 0, 1,
        ),
        ValueInspectionRequest(
            root, ValueViewKind.VARIABLES, 0, 1, exact=0,
        ),
        ValueInspectionRequest(
            root.child(ValuePathSegmentKind.VARIABLE, "X").child(
                ValuePathSegmentKind.VARIABLE, "Y",
            ),
            ValueViewKind.STRUCTURE_FIELDS,
            0,
            1,
        ),
        ValueInspectionRequest(
            root.child(ValuePathSegmentKind.VARIABLE, "X").child(
                ValuePathSegmentKind.COLUMN, "Column",
            ),
            ValueViewKind.STRUCTURE_FIELDS,
            0,
            1,
        ),
    )
    session = ScriptedSession((CAPTURE_A,))
    controller = captured_controller(session, capture_value_inspection_builder=build)
    owner = controller._capture_evaluation_coordinator
    assert owner is not None
    try:
        for request in invalid_requests:
            with pytest.raises(CaptureValueCheckError):
                controller.capture_value_inspection(
                    "project", path=None, request=request, limit=None,
                    worker_type_registrations=(),
                )

        assert planned == []
    finally:
        owner.begin_close()
        assert owner.join(2)


def test_runtime_value_binding_normalizes_a_pre_submit_builder_pending_error():
    from onec_runtime.capture_evaluation import CaptureEvaluationKind
    from onec_runtime.capture_values import (
        SafeValuePath,
        ValueInspectionRequest,
        ValueRoot,
        ValueRootKind,
        ValueViewKind,
    )
    from test_prototype_runtime import CAPTURE_A, ScriptedSession, captured_controller

    def build(**_kwargs):
        raise CaptureEvaluationPendingError("fabricated", CaptureEvaluationKind.INSPECTION)

    session = ScriptedSession((CAPTURE_A,))
    controller = captured_controller(session, capture_value_inspection_builder=build)
    owner = controller._capture_evaluation_coordinator
    assert owner is not None
    try:
        with pytest.raises(CaptureValueCheckError, match="target projection"):
            controller.capture_value_inspection(
                "project",
                path=None,
                request=ValueInspectionRequest(
                    SafeValuePath(ValueRoot(ValueRootKind.CONTEXT)),
                    ValueViewKind.VARIABLES,
                    0,
                    1,
                ),
                limit=None,
                worker_type_registrations=(),
            )
    finally:
        owner.begin_close()
        assert owner.join(2)
