from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
from threading import Lock
from time import monotonic, sleep
from uuid import UUID, uuid4

from onec_runtime.errors import (
    CommandTimeout,
    ProtocolError,
    RdbgDebugUiNotRegistered,
    TransportRecoveryError,
    TargetLost,
    UnexpectedStop,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import (
    DebugTarget,
    EvaluationResult,
    LocalVariablesResult,
    ModuleLocation,
    ModifyResult,
    PendingEvaluation,
    StopEvent,
    TargetId,
)
from onec_runtime.rdbg.transport import RdbgTransport
from onec_runtime.rdbg.xml_codec import (
    build_attach_request,
    build_attach_target_request,
    build_auto_attach_request,
    build_breakpoints_request,
    build_call_stack_request,
    build_collection_eval_request,
    build_clear_break_request,
    build_detach_request,
    build_eval_request,
    build_get_targets_request,
    build_init_settings_request,
    build_local_variables_request,
    build_modify_request,
    build_step_request,
    build_terminate_request,
    parse_eval_result,
    parse_eval_response,
    parse_call_stack,
    parse_local_variables_result,
    parse_modify_result,
    parse_ping_document,
    parse_ping_evaluations_from_document,
    parse_ping_events_from_document,
    parse_ping_local_variables_from_document,
    parse_ping_target_events_from_document,
    parse_targets,
    validate_command_acknowledgement,
)


class SessionState(Enum):
    DETACHED = "detached"
    ATTACHED = "attached"
    READY = "ready"
    EXECUTING = "executing"
    FAILED = "failed"


@dataclass(slots=True)
class _PendingEvaluationState:
    capability: PendingEvaluation
    suspended_stop: StopEvent | None = None
    collection_start_index: int = 0


class RdbgSession:
    def __init__(
        self,
        transport: RdbgTransport,
        expected_location: ModuleLocation,
        *,
        alias: str = "DefAlias",
        ui_id: UUID | None = None,
        break_on_next: bool = False,
        server_target_type: str = "ServerEmulation",
    ) -> None:
        if server_target_type not in {"ServerEmulation", "Server"}:
            raise ValueError("Unsupported server target type")
        self.transport = transport
        self.expected_location = expected_location
        self.alias = alias
        self.ui_id = ui_id or uuid4()
        self.break_on_next = break_on_next
        self.server_target_type = server_target_type
        self._preexisting_target_ids: set[UUID] = set()
        self._managed_client_id: UUID | None = None
        self._bound_client_target: TargetId | None = None
        self.state = SessionState.DETACHED
        self.target: DebugTarget | None = None
        self.attached_targets: dict[UUID, DebugTarget] = {}
        self._breakpoint_installed = False
        self._breakpoint_locations: tuple[ModuleLocation, ...] = ()
        self._pending_evaluations: dict[UUID, EvaluationResult] = {}
        self._pending_local_variables: dict[UUID, LocalVariablesResult] = {}
        self._event_queue: deque[StopEvent | EvaluationResult] = deque()
        self._evaluation_owner = object()
        self._pending_evaluation_states: dict[int, _PendingEvaluationState] = {}
        self._request_admission_lock = Lock()
        self._requests_invalidated = False
        self.profiler: PhaseRecorder | None = None

    def _profile(self, phase: str, operation, **metadata):  # type: ignore[no-untyped-def]
        if self.profiler is None:
            return operation()
        return self.profiler.measure(phase, operation, **metadata)

    def _require(self, *states: SessionState) -> None:
        if self.state not in states:
            expected = ", ".join(state.value for state in states)
            raise ProtocolError(
                f"RDBG operation requires state {expected}; current state is {self.state.value}"
            )

    def _request(
        self,
        command: str,
        payload: bytes = b"",
        **options: object,
    ) -> bytes:
        """Admit one normal request atomically against session invalidation.

        The gate protects only admission. Network I/O remains concurrent and
        an admitted request may finish after invalidation; its owner must then
        retain or quarantine any unsettled capability.
        """
        with self._request_admission_lock:
            if self._requests_invalidated:
                raise ProtocolError("RDBG session was invalidated")
        return self.transport.request(command, payload, **options)

    def _teardown_request(
        self,
        command: str,
        payload: bytes = b"",
        **options: object,
    ) -> bytes:
        """Send an explicit close-owned request after normal admission closes."""
        return self.transport.request(command, payload, **options)

    def initialize(self) -> None:
        self._require(SessionState.DETACHED)
        self._request(
            "attachDebugUI", build_attach_request(self.alias, self.ui_id)
        )
        # Registration already succeeded; later initialization failures must
        # still allow cleanup of our UI on an externally owned debugger.
        self.state = SessionState.ATTACHED
        if self.server_target_type == "Server":
            self._preexisting_target_ids = {
                target.target_id.id for target in self.list_targets()
            }
        self._request(
            "initSettings",
            build_init_settings_request(
                self.alias, self.ui_id, break_on_next=self.break_on_next
            ),
        )
        self._request(
            "setAutoAttachSettings",
            build_auto_attach_request(
                self.alias, self.ui_id, target_types=self._auto_attach_target_types()
            ),
        )

    def _auto_attach_target_types(self) -> tuple[str, ...]:
        if self.server_target_type == "Server" and self._bound_client_target is None:
            return ("ManagedClient",)
        return self.server_target_type, "ManagedClient"

    def bind_server_session(self, *, launch_token: str) -> None:
        """Allow server subjects only for the managed client we stopped in."""
        self._require(SessionState.READY)
        if (
            self.target is None
            or self.target.target_type != "ManagedClient"
            or self.target.target_id.seance_id is None
        ):
            raise ProtocolError("Server bootstrap requires a managed client session identity")
        if self._bound_client_target is not None and self._bound_client_target != self.target.target_id:
            raise ProtocolError("Server bootstrap cannot change its managed client session")
        # A fresh subject is not necessarily ours. Read the launch argument
        # before binding a server session or changing any extension guard.
        if not launch_token:
            raise ProtocolError("Server bootstrap requires a launch identity")
        launch = self.evaluate("ПараметрЗапуска")
        if (
            launch.error_occurred
            or launch.type_name != "Строка"
            or launch.presentation not in (launch_token, '"' + launch_token + '"')
        ):
            raise ProtocolError("Managed client launch identity does not match the owned process")
        self._bound_client_target = self.target.target_id
        self._request(
            "setAutoAttachSettings",
            build_auto_attach_request(
                self.alias, self.ui_id, target_types=self._auto_attach_target_types()
            ),
        )
        # A cluster registers its server subject before the managed client.
        # Its earlier targetStarted event was deliberately ignored until we
        # could authenticate the client and correlate the session identity.
        self._attach_discovered_targets(self.list_targets())

    def _attachable_targets(self, targets: list[DebugTarget]) -> list[DebugTarget]:
        if self.server_target_type != "Server":
            return targets
        candidates = [
            target for target in targets
            if target.target_id.infobase_alias.casefold() == self.alias.casefold()
            and target.target_id.id not in self._preexisting_target_ids
            and target.target_type in self._auto_attach_target_types()
        ]
        client = self._bound_client_target
        if client is not None:
            return [
                target for target in candidates
                if target.target_id.seance_id == client.seance_id
                and (
                    client.infobase_instance_id is None
                    or target.target_id.infobase_instance_id is None
                    or target.target_id.infobase_instance_id == client.infobase_instance_id
                )
                and (target.target_type != "ManagedClient" or target.target_id.id == client.id)
            ]
        client_ids = {target.target_id.id for target in candidates}
        if self._managed_client_id is not None:
            client_ids.add(self._managed_client_id)
        if len(client_ids) > 1:
            raise ProtocolError("Multiple new managed client targets make bootstrap ambiguous")
        if client_ids:
            self._managed_client_id = next(iter(client_ids))
        return candidates

    def list_targets(self) -> list[DebugTarget]:
        payload = self._request(
            "getDbgAllTargetStates", build_get_targets_request(self.alias, self.ui_id)
        )
        return parse_targets(payload)

    def terminate_bound_server_session(self) -> bool:
        """Stop our server calls, then client; return whether native client exit was requested."""
        if self.server_target_type != "Server" or self._bound_client_target is None:
            return False
        payload = self._teardown_request(
            "getDbgAllTargetStates", build_get_targets_request(self.alias, self.ui_id),
            timeout_s=10.0,
        )
        targets = tuple(
            target.target_id for target in self._attachable_targets(parse_targets(payload))
            if target.target_type == "Server"
        )
        if targets:
            response = self._teardown_request(
                "terminateDbgTarget",
                build_terminate_request(self.alias, self.ui_id, targets, payload),
                timeout_s=10.0,
            )
            validate_command_acknowledgement(response, command="terminateDbgTarget")

        # Hard-killing a managed client leaves a stale subject in the shared
        # debugger. Let 1C remove its own subject after the server calls stop.
        # Rediscover the full identity: the server termination may change it.
        payload = self._teardown_request(
            "getDbgAllTargetStates", build_get_targets_request(self.alias, self.ui_id),
            timeout_s=10.0,
        )
        client = self._bound_client_target
        if not any(target.target_id.id == client.id for target in parse_targets(payload)):
            return False
        response = self._teardown_request(
            "terminateDbgTarget",
            build_terminate_request(
                self.alias, self.ui_id, (client,), payload, target_type="ManagedClient",
            ),
            timeout_s=10.0,
        )
        validate_command_acknowledgement(response, command="terminateDbgTarget")
        return True

    def discover_server_emulation_target(self, *, timeout_s: float = 30.0) -> DebugTarget:
        self._require(SessionState.ATTACHED)
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            matches = [
                target
                for target in self.list_targets()
                if target.target_type == "ServerEmulation"
                and target.target_id.infobase_alias == self.alias
            ]
            if len(matches) == 1:
                self.target = matches[0]
                return matches[0]
            if len(matches) > 1:
                raise ProtocolError(
                    f"Expected one ServerEmulation target, found {len(matches)}"
                )
            sleep(0.1)
        raise CommandTimeout("Timed out waiting for the ServerEmulation target")

    def attach_target(self, target: DebugTarget) -> None:
        self._require(SessionState.ATTACHED)
        self._request(
            "clearBreakOnNextStatement",
            build_clear_break_request(self.alias, self.ui_id),
        )
        self._request(
            "attachDetachDbgTargets",
            build_attach_target_request(
                self.alias, self.ui_id, target.target_id, attach=True
            ),
        )
        self.target = target
        self.attached_targets[target.target_id.id] = target

    def set_service_breakpoint(self) -> None:
        self.set_breakpoints((self.expected_location,))

    def verify_registration(self) -> None:
        """Probe this UI before starting the client whose first stop it owns."""
        self._require(SessionState.ATTACHED)
        self._request(
            "pingDebugUIParams",
            b"",
            timeout_s=0.2,
            dbgui=str(self.ui_id),
        )

    def set_breakpoints(self, locations: tuple[ModuleLocation, ...]) -> None:
        self._require(SessionState.ATTACHED, SessionState.READY)
        if not locations:
            raise ValueError("At least one breakpoint location is required")
        response = self._request(
            "setBreakpoints",
            build_breakpoints_request(self.alias, self.ui_id, locations),
        )
        validate_command_acknowledgement(response, command="setBreakpoints")
        self._breakpoint_installed = True
        self._breakpoint_locations = tuple(locations)

    def _poll(
        self,
        timeout_s: float,
        *,
        profile_page_start: int | None = None,
        profile_result_id: str = "",
    ) -> tuple[list[StopEvent], list[EvaluationResult]]:
        metadata = {
            "page_start": profile_page_start,
            "result_id": profile_result_id,
        }
        payload = self._profile(
            "rdbg.ping.request",
            lambda: self._request(
                "pingDebugUIParams",
                b"",
                timeout_s=timeout_s,
                dbgui=str(self.ui_id),
            ),
            output_bytes=len,
            **metadata,
        )
        if not payload.strip():
            return [], []
        document = self._profile(
            "rdbg.ping.parse_xml",
            lambda: parse_ping_document(payload),
            input_bytes=len(payload),
            **metadata,
        )
        targets = self._profile(
            "rdbg.ping.extract_targets",
            lambda: parse_ping_target_events_from_document(document),
            item_count=len,
            **metadata,
        )
        self._attach_discovered_targets(targets)
        stops = self._profile(
            "rdbg.ping.extract_stops",
            lambda: parse_ping_events_from_document(document),
            item_count=len,
            **metadata,
        )
        local_variables = self._profile(
            "rdbg.ping.extract_local_variables",
            lambda: parse_ping_local_variables_from_document(document),
            item_count=len,
            **metadata,
        )
        for result in local_variables:
            self._pending_local_variables[result.result_id] = result
        evaluations = self._profile(
            "rdbg.ping.extract_evaluations",
            lambda: parse_ping_evaluations_from_document(document),
            item_count=len,
            **metadata,
        )
        for evaluation in evaluations:
            self._pending_evaluations[evaluation.result_id] = evaluation
        return stops, evaluations

    def _attach_discovered_targets(self, targets: list[DebugTarget]) -> None:
        for target in self._attachable_targets(targets):
            if target.target_id.id in self.attached_targets:
                continue
            if not self.break_on_next:
                self._request(
                    "clearBreakOnNextStatement",
                    build_clear_break_request(self.alias, self.ui_id),
                )
            self._request(
                "attachDetachDbgTargets",
                build_attach_target_request(
                    self.alias, self.ui_id, target.target_id, attach=True
                ),
            )
            self.attached_targets[target.target_id.id] = target
            if self._breakpoint_installed:
                self._request(
                    "setBreakpoints",
                    build_breakpoints_request(
                        self.alias, self.ui_id, self._breakpoint_locations
                    ),
                )

    def _ingest_poll_events(
        self,
        stops: list[StopEvent],
        evaluations: list[EvaluationResult],
    ) -> None:
        self._event_queue.extend(stops)
        pending_result_ids = {
            state.capability.result_id
            for state in self._pending_evaluation_states.values()
        }
        self._event_queue.extend(
            result
            for result in evaluations
            if result.result_id in pending_result_ids
        )

    def _pop_queued_stop(self) -> StopEvent | None:
        queued = list(self._event_queue)
        for index, event in enumerate(queued):
            if isinstance(event, StopEvent):
                del queued[index]
                self._event_queue = deque(queued)
                return event
        return None

    def _admit_stop(self, stop: StopEvent) -> StopEvent:
        stopped_target = self.attached_targets.get(stop.target_id.id)
        if stopped_target is None or stopped_target.target_id != stop.target_id:
            raise UnexpectedStop(
                f"Stop belongs to unattached target {stop.target_id.id}"
            )
        self.target = stopped_target
        self.state = SessionState.READY
        return stop

    def wait_for_service_stop(self, *, timeout_s: float = 60.0) -> StopEvent:
        return self.wait_for_stop((self.expected_location,), timeout_s=timeout_s)

    def wait_for_stop(
        self,
        allowed_locations: tuple[ModuleLocation, ...],
        *,
        timeout_s: float = 60.0,
    ) -> StopEvent:
        if not allowed_locations:
            raise ValueError("At least one allowed stop location is required")
        allowed = frozenset(allowed_locations)
        stop = self.wait_for_any_stop(timeout_s=timeout_s)
        if stop.location not in allowed:
            raise UnexpectedStop(
                f"Stop location {stop.location!r} does not match any allowed "
                f"location: {allowed_locations!r}"
            )
        return stop

    def wait_for_any_stop(
        self,
        *,
        timeout_s: float = 60.0,
        on_poll: Callable[[], None] | None = None,
    ) -> StopEvent:
        self._require(SessionState.ATTACHED, SessionState.EXECUTING)
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            if on_poll is not None:
                on_poll()
            queued = self._pop_queued_stop()
            if queued is not None:
                return self._admit_stop(queued)
            remaining = max(0.1, min(6.0, deadline - monotonic()))
            polled = self._poll(remaining)
            self._ingest_poll_events(*polled)
            sleep(0.05)
        self.state = SessionState.FAILED
        raise CommandTimeout("Timed out waiting for a runtime stop")

    def read_current_stack(self, *, timeout_s: float = 5.0) -> StopEvent:
        self._require(SessionState.ATTACHED, SessionState.READY)
        if self.target is None:
            raise TargetLost("No target has been selected")
        payload = self._request(
            "getCallStack",
            build_call_stack_request(self.alias, self.ui_id, self.target.target_id),
            timeout_s=timeout_s,
        )
        if not payload.strip():
            raise TransportRecoveryError(
                "RDBG getCallStack returned no frame evidence"
            )
        frames = parse_call_stack(payload, self.target.target_id)
        if not frames:
            raise TransportRecoveryError("RDBG getCallStack returned an empty stack")
        locations = tuple(frame.location for frame in frames)
        self.state = SessionState.READY
        return StopEvent(
            self.target.target_id,
            locations[0],
            "recoveredCallStack",
            stack=locations,
            stack_frames=tuple(frames),
        )

    def evaluate(
        self,
        expression: str,
        *,
        timeout_s: float = 30.0,
        max_text_size: int = 307_200,
        stack_level: int = 0,
    ) -> EvaluationResult:
        pending = self.start_evaluation(
            expression,
            max_text_size=max_text_size,
            stack_level=stack_level,
            timeout_s=timeout_s,
        )
        event = self.wait_evaluation_event(pending, timeout_s=timeout_s)
        if isinstance(event, StopEvent):
            raise UnexpectedStop(
                "Expression evaluation stopped at a user breakpoint"
            )
        return event

    def start_evaluation(
        self,
        expression: str,
        *,
        max_text_size: int = 307_200,
        stack_level: int = 0,
        timeout_s: float = 30.0,
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> PendingEvaluation:
        self._require(SessionState.READY)
        if self.target is None:
            raise TargetLost("No target has been selected")
        if type(expression) is not str or not expression:
            raise ValueError("evaluation expression must be non-empty")
        if type(max_text_size) is not int or max_text_size <= 0:
            raise ValueError("max_text_size must be positive")
        if type(stack_level) is not int or stack_level < 0:
            raise ValueError("stack_level must be non-negative")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not isfinite(float(timeout_s))
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be finite and positive")
        if on_transport_dispatch is not None and not callable(on_transport_dispatch):
            raise TypeError("on_transport_dispatch must be callable")
        result_id = uuid4()
        request = build_eval_request(
            self.alias,
            self.ui_id,
            self.target.target_id,
            expression,
            result_id,
            max_text_size=max_text_size,
            stack_level=stack_level,
        )
        return self._start_evaluation_request(
            request,
            result_id,
            timeout_s=float(timeout_s),
            on_transport_dispatch=on_transport_dispatch,
        )

    def start_collection_evaluation(
        self,
        expression: str,
        *,
        start_index: int,
        page_size: int = 2400,
        timeout_s: float = 30.0,
        max_text_size: int = 4096,
        stack_level: int = 0,
        on_transport_dispatch: Callable[[], None] | None = None,
    ) -> PendingEvaluation:
        """Dispatch one collection evalExpr and return its owned capability."""

        self._require(SessionState.READY)
        if self.target is None:
            raise TargetLost("No target has been selected")
        if type(expression) is not str or not expression:
            raise ValueError("evaluation expression must be non-empty")
        if type(start_index) is not int or start_index < 0:
            raise ValueError("start_index must be non-negative")
        if type(page_size) is not int or page_size <= 0:
            raise ValueError("page_size must be positive")
        if type(max_text_size) is not int or max_text_size <= 0:
            raise ValueError("max_text_size must be positive")
        if type(stack_level) is not int or stack_level < 0:
            raise ValueError("stack_level must be non-negative")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not isfinite(float(timeout_s))
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be finite and positive")
        if on_transport_dispatch is not None and not callable(on_transport_dispatch):
            raise TypeError("on_transport_dispatch must be callable")
        result_id = uuid4()
        request = build_collection_eval_request(
            self.alias,
            self.ui_id,
            self.target.target_id,
            expression,
            result_id,
            start_index=start_index,
            page_size=page_size,
            max_text_size=max_text_size,
            stack_level=stack_level,
        )
        return self._start_evaluation_request(
            request,
            result_id,
            timeout_s=float(timeout_s),
            on_transport_dispatch=on_transport_dispatch,
            collection_start_index=start_index,
        )

    def _start_evaluation_request(
        self,
        request: bytes,
        result_id: UUID,
        *,
        timeout_s: float,
        on_transport_dispatch: Callable[[], None] | None,
        collection_start_index: int = 0,
    ) -> PendingEvaluation:
        if self.target is None:
            raise TargetLost("No target has been selected")
        if self._pending_evaluation_states:
            raise ProtocolError("Another expression evaluation is already pending")
        pending = PendingEvaluation(
            self.target.target_id,
            result_id,
            self._evaluation_owner,
        )
        self._pending_evaluation_states[id(pending)] = _PendingEvaluationState(
            pending,
            collection_start_index=collection_start_index,
        )
        try:
            if on_transport_dispatch is not None:
                on_transport_dispatch()
            response = self._request(
                "evalExpr",
                request,
                timeout_s=timeout_s,
            )
        except BaseException:
            self._pending_evaluation_states.pop(id(pending), None)
            raise
        if response.strip():
            try:
                result = parse_eval_response(response)
            except ProtocolError:
                self._pending_evaluation_states.pop(id(pending), None)
                raise
            if result is not None and result.result_id != result_id:
                self._pending_evaluation_states.pop(id(pending), None)
                raise ProtocolError("RDBG evaluation result ID mismatch")
            if result is not None:
                self._event_queue.append(result)
        return pending

    def wait_evaluation_event(
        self,
        pending: PendingEvaluation,
        *,
        timeout_s: float,
    ) -> EvaluationResult | StopEvent:
        """Consume one event; interval expiry leaves the capability registered.

        Only a matching result retires it. A coordinator may therefore repeat
        bounded waits after CommandTimeout without sending another evalExpr.
        """
        state = self._require_pending_evaluation(pending)
        if state.suspended_stop is not None:
            raise ProtocolError("Pending evaluation stop must be continued first")
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            event = self._pop_pending_evaluation_event(pending)
            if event is not None:
                if isinstance(event, StopEvent):
                    if event.target_id != pending.target_id:
                        raise UnexpectedStop(
                            "Evaluation stop belongs to a different target"
                        )
                    self._admit_stop(event)
                    state.suspended_stop = event
                    return event
                self._pending_evaluations.pop(event.result_id, None)
                del self._pending_evaluation_states[id(pending)]
                self.state = SessionState.READY
                if state.collection_start_index:
                    event = replace(
                        event,
                        collection_rows=tuple(
                            replace(
                                row,
                                index=state.collection_start_index + offset,
                            )
                            for offset, row in enumerate(event.collection_rows)
                        ),
                    )
                return event
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            polled = self._poll(min(6.0, remaining))
            self._ingest_poll_events(*polled)
        raise CommandTimeout(
            f"Timed out waiting for expression result {pending.result_id}"
        )

    def continue_evaluation(
        self,
        pending: PendingEvaluation,
        stop: StopEvent,
    ) -> None:
        state = self._require_pending_evaluation(pending)
        if state.suspended_stop is not stop:
            raise ProtocolError("Pending evaluation stop is stale or foreign")
        if stop.target_id != pending.target_id or self.target is None:
            raise ProtocolError("Pending evaluation target changed")
        self.continue_()
        state.suspended_stop = None

    def _require_pending_evaluation(
        self,
        pending: PendingEvaluation,
    ) -> _PendingEvaluationState:
        if type(pending) is not PendingEvaluation:
            raise TypeError("pending evaluation capability is required")
        state = self._pending_evaluation_states.get(id(pending))
        if (
            state is None
            or state.capability is not pending
            or pending.owner is not self._evaluation_owner
            or self.target is None
            or pending.target_id != self.target.target_id
        ):
            raise ProtocolError("Pending evaluation is stale or foreign")
        return state

    def _pop_pending_evaluation_event(
        self,
        pending: PendingEvaluation,
    ) -> EvaluationResult | StopEvent | None:
        if not self._event_queue:
            return None
        event = self._event_queue[0]
        if isinstance(event, StopEvent):
            return self._event_queue.popleft()
        if event.result_id != pending.result_id:
            raise ProtocolError("Evaluation event correlation mismatch")
        return self._event_queue.popleft()

    def evaluate_collection(
        self,
        expression: str,
        *,
        start_index: int,
        page_size: int = 2400,
        timeout_s: float = 30.0,
        max_text_size: int = 4096,
        stack_level: int = 0,
    ) -> EvaluationResult:
        self._require(SessionState.READY)
        if self.target is None:
            raise TargetLost("No target has been selected")
        if stack_level < 0:
            raise ValueError("stack_level must be non-negative")
        deadline = monotonic() + timeout_s
        result_id = uuid4()

        def remaining_timeout() -> float:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CommandTimeout(
                    f"Timed out waiting for collection result {result_id}"
                )
            return remaining

        request = build_collection_eval_request(
            self.alias,
            self.ui_id,
            self.target.target_id,
            expression,
            result_id,
            start_index=start_index,
            page_size=page_size,
            max_text_size=max_text_size,
            stack_level=stack_level,
        )
        metadata = {"page_start": start_index, "result_id": str(result_id)}
        response = self._profile(
            "rdbg.collection.eval_request",
            lambda: self._request(
                "evalExpr",
                request,
                timeout_s=remaining_timeout(),
            ),
            input_bytes=len(request),
            output_bytes=len,
            **metadata,
        )

        def parse_direct() -> EvaluationResult | None:
            remaining_timeout()
            if not response.strip():
                return None
            try:
                candidate = parse_eval_result(response)
                if candidate.result_id == result_id:
                    return candidate
            except ProtocolError:
                pass
            return None

        result = self._profile(
            "rdbg.collection.direct_parse",
            parse_direct,
            input_bytes=len(response),
            item_count=lambda candidate: int(candidate is not None),
            **metadata,
        )
        while result is None:
            remaining_timeout()
            result = self._profile(
                "rdbg.collection.pending_lookup",
                lambda: self._pending_evaluations.pop(result_id, None),
                item_count=lambda candidate: int(candidate is not None),
                **metadata,
            )
            if result is None:
                polled = self._poll(
                    min(6.0, remaining_timeout()),
                    profile_page_start=start_index,
                    profile_result_id=str(result_id),
                )
                self._ingest_poll_events(*polled)
        remaining_timeout()
        return self._profile(
            "rdbg.collection.reindex_rows",
            lambda: replace(
                result,
                collection_rows=tuple(
                    replace(row, index=start_index + offset)
                    for offset, row in enumerate(result.collection_rows)
                ),
            ),
            item_count=lambda candidate: len(candidate.collection_rows),
            **metadata,
        )

    def local_variables(
        self,
        stack_level: int = 0,
        *,
        timeout_s: float = 30.0,
        max_text_size: int = 307_200,
        retry_delays_s: tuple[float, ...] = (0.05, 0.10, 0.15),
    ) -> LocalVariablesResult:
        self._require(SessionState.READY)
        if self.target is None:
            raise TargetLost("No target has been selected")
        if stack_level < 0:
            raise ValueError("stack_level must be non-negative")

        deadline = monotonic() + timeout_s

        def request_once() -> LocalVariablesResult:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CommandTimeout("Timed out waiting for local variables")
            result_id = uuid4()
            response = self._request(
                "evalLocalVariables",
                build_local_variables_request(
                    self.alias,
                    self.ui_id,
                    self.target.target_id,
                    stack_level,
                    result_id,
                    max_text_size=max_text_size,
                ),
                timeout_s=remaining,
            )
            if response.strip():
                try:
                    direct = parse_local_variables_result(response)
                    if direct.result_id == result_id:
                        return direct
                except ProtocolError:
                    pass
            while monotonic() < deadline:
                pending = self._pending_local_variables.pop(result_id, None)
                if pending is not None:
                    return pending
                polled = self._poll(max(0.1, min(6.0, deadline - monotonic())))
                self._ingest_poll_events(*polled)
            raise CommandTimeout(
                f"Timed out waiting for local variables result {result_id}"
            )

        result = request_once()
        if result.variables or result.error_occurred:
            return result
        for delay_s in retry_delays_s:
            if monotonic() + delay_s >= deadline:
                break
            sleep(delay_s)
            result = request_once()
            if result.variables or result.error_occurred:
                return result
        return result

    def modify(self, variable: str, value_expression: str) -> ModifyResult:
        self._require(SessionState.READY)
        if self.target is None:
            raise TargetLost("No target has been selected")
        result_id = uuid4()
        response = self._request(
            "modifyValue",
            build_modify_request(
                self.alias,
                self.ui_id,
                self.target.target_id,
                variable,
                value_expression,
                result_id,
            ),
        )
        if not response.strip():
            raise ProtocolError("RDBG modifyValue returned an empty response")
        result = parse_modify_result(response)
        if result.result_id != result_id:
            raise ProtocolError(
                "RDBG modifyValue result ID mismatch: "
                f"expected {result_id}, got {result.result_id}"
            )
        return result

    def continue_(self) -> None:
        self._require(SessionState.READY)
        if self.target is None:
            raise TargetLost("No target has been selected")
        self._request(
            "step", build_step_request(self.alias, self.ui_id, self.target.target_id)
        )
        self.state = SessionState.EXECUTING

    def heartbeat(self) -> dict[str, object]:
        self._require(SessionState.READY)
        if self.target is None:
            raise TargetLost("No target has been selected")
        with self._request_admission_lock:
            if self._requests_invalidated:
                raise ProtocolError("RDBG session was invalidated")
        rtt_ms = self.transport.test_server()
        # The platform expires the registered Debug UI unless its dedicated
        # long-poll endpoint is called. Server and target probes alone do not
        # renew that lease. Bound idle empty-response waiting while RuntimeSession
        # holds its shared operation lock; returned events are still ingested.
        self._ingest_poll_events(*self._poll(0.1))
        matches = [
            target for target in self.list_targets() if target.target_id.id == self.target.target_id.id
        ]
        if len(matches) != 1:
            raise TargetLost("Selected target disappeared during heartbeat")
        return {"rtt_ms": rtt_ms, "target_state": matches[0].state}

    def invalidate(self) -> None:
        with self._request_admission_lock:
            self._requests_invalidated = True
            self.state = SessionState.FAILED
            self.target = None
            self.attached_targets.clear()
            self._pending_evaluations.clear()
            self._pending_local_variables.clear()
            self._event_queue.clear()
            self._pending_evaluation_states.clear()

    def detach(self) -> None:
        if self.state is SessionState.DETACHED:
            return
        target_errors: list[BaseException] = []
        for target in self.attached_targets.values():
            try:
                self._teardown_request(
                    "attachDetachDbgTargets",
                    build_attach_target_request(
                        self.alias, self.ui_id, target.target_id, attach=False
                    ),
                    timeout_s=10.0,
                )
            except BaseException as error:
                target_errors.append(error)
        try:
            self._teardown_request(
                "detachDebugUI",
                build_detach_request(self.alias, self.ui_id),
                timeout_s=10.0,
            )
        except RdbgDebugUiNotRegistered:
            # A lost registration is already detached on the shared server.
            pass
        except BaseException as error:
            if target_errors:
                errors = (*target_errors, error)
                raise ProtocolError(
                    "RDBG detach failed: "
                    + ", ".join(type(item).__name__ for item in errors)
                ) from error
            raise
        self.state = SessionState.DETACHED
        self.target = None
        self.attached_targets.clear()
        self._pending_evaluations.clear()
        self._pending_local_variables.clear()
        self._event_queue.clear()
        self._pending_evaluation_states.clear()
