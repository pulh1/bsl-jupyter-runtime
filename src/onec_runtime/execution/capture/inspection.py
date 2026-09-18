"""Private CAPTURE inspection calls through one owned RDBG worker port."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from onec_runtime.capture_values import (
    MAX_PAGE_ITEMS,
    MAX_TYPE_CHARS,
    SafePathSegment,
    ValuePathSegmentKind,
    VariableRole,
)
from onec_runtime.errors import CapturePathError, ProtocolError
from onec_runtime.execution.capture.scope import (
    CaptureContextState,
    CaptureFrameIdentity,
    CaptureScope,
)
from onec_runtime.execution.evaluation import EvaluationPort, evaluate_until_result
from onec_runtime.rdbg.models import (
    EvaluationResult,
    FrameVariable,
    LocalVariablesResult,
    ModuleLocation,
)


class CaptureInspectionPort(EvaluationPort, Protocol):
    """Worker-confined local read and expression evaluation capabilities."""

    def local_variables(
        self, stack_level: int = 0, *, timeout_s: float = 30.0
    ) -> LocalVariablesResult: ...


class CaptureInspectionUnavailable(ProtocolError):
    """A confirmed inspection failure that does not invalidate the frame."""


MAX_NATIVE_VARIABLE_INVENTORY = 10_000


@dataclass(frozen=True, slots=True)
class NativeVariablePage:
    """Only safe variable names, never RDBG value presentations."""

    names: tuple[str, ...]
    total: int
    next_cursor: int | None


@dataclass(frozen=True, slots=True)
class TypedNativeVariable:
    """Bounded frame metadata with no RDBG value presentation."""

    name: str
    type_name: str | None
    collection_size: int | None


@dataclass(frozen=True, slots=True)
class TypedNativeVariablePage:
    """One bounded page of typed metadata from a user frame."""

    variables: tuple[TypedNativeVariable, ...]
    total: int
    next_cursor: int | None


class CaptureInspectionExecutor:
    """Read private frame data while route admission belongs to the caller."""

    def __init__(
        self, *, request_timeout_s: float = 30.0, wait_interval_s: float = 6.0
    ) -> None:
        self._request_timeout_s = request_timeout_s
        self._wait_interval_s = wait_interval_s

    def read_variable(
        self,
        scope: CaptureScope,
        name: str,
        *,
        stack_level: int,
        port: CaptureInspectionPort,
    ) -> FrameVariable:
        """Read one named variable afresh; callers must bound public values.

        The controller/arbiter validates the route fence before this worker
        plan starts. A confirmed RDBG error remains a request failure; an
        ambiguous transport exception propagates so the ticket keeps owner.
        """

        self._require_frame(scope, stack_level, allow_kernel=False)
        if not isinstance(name, str) or len(name) > 256 or not name.isidentifier():
            raise ValueError("CAPTURE variable name is invalid")
        response = port.local_variables(
            stack_level=stack_level, timeout_s=self._request_timeout_s
        )
        if not isinstance(response, LocalVariablesResult) or response.error_occurred:
            raise CaptureInspectionUnavailable("CAPTURE frame variables are unavailable")
        matches = tuple(
            variable for variable in response.variables
            if isinstance(variable, FrameVariable)
            and variable.name.casefold() == name.casefold()
        )
        if len(matches) != 1:
            raise CaptureInspectionUnavailable("CAPTURE variable is unavailable")
        return matches[0]

    def read_variable_page(
        self,
        scope: CaptureScope,
        *,
        stack_level: int,
        start: int,
        stop: int,
        port: CaptureInspectionPort,
        role: VariableRole = VariableRole.VARIABLES,
        parameter_names: tuple[str, ...] = (),
    ) -> NativeVariablePage:
        """Read one bounded page of safe native-frame variable names.

        The complete RDBG inventory is private input and capped before any
        names are exposed. The checks here compare saved scope/frame/target
        fields; they do not query the remote target to prove the same stop.
        The owner must admit this plan through the arbiter's route fence.
        A confirmed RDBG error only settles this inspection request.
        """

        self._require_page_request(scope, stack_level, start, stop)
        checked_parameters = self._checked_role_parameters(role, parameter_names)
        _variables, names = self._read_inventory(scope, stack_level, port)
        names = self._names_for_role(names, role, checked_parameters)
        selected = names[start:stop]
        return NativeVariablePage(
            selected,
            len(names),
            stop if selected and stop < len(names) else None,
        )

    @staticmethod
    def _checked_role_parameters(
        role: VariableRole, parameter_names: tuple[str, ...],
    ) -> tuple[str, ...]:
        if type(role) is not VariableRole:
            raise TypeError("CAPTURE variable role is invalid")
        if (
            type(parameter_names) is not tuple
            or len(parameter_names) > MAX_NATIVE_VARIABLE_INVENTORY
        ):
            raise ValueError("CAPTURE parameter inventory is invalid")
        try:
            checked_parameters = tuple(
                SafePathSegment(ValuePathSegmentKind.VARIABLE, name).key
                for name in parameter_names
            )
        except CapturePathError as error:
            raise ValueError("CAPTURE parameter inventory is invalid") from error
        folded = tuple(name.casefold() for name in checked_parameters)
        if len(set(folded)) != len(folded):
            raise ValueError("CAPTURE parameter inventory is ambiguous")
        if role is VariableRole.VARIABLES:
            if folded:
                raise ValueError("Unclassified CAPTURE page cannot name parameters")
        return folded

    @staticmethod
    def _names_for_role(
        names: tuple[str, ...], role: VariableRole,
        folded: tuple[str, ...],
    ) -> tuple[str, ...]:
        if role is VariableRole.VARIABLES:
            return names
        by_name = {name.casefold(): name for name in names}
        if any(name not in by_name for name in folded):
            raise CaptureInspectionUnavailable("CAPTURE method parameters are unavailable")
        if role is VariableRole.PARAMETERS:
            return tuple(by_name[name] for name in folded)
        excluded = set(folded)
        return tuple(name for name in names if name.casefold() not in excluded)

    def read_typed_variable_page(
        self,
        scope: CaptureScope,
        *,
        stack_level: int,
        start: int,
        stop: int,
        port: CaptureInspectionPort,
        role: VariableRole = VariableRole.VARIABLES,
        parameter_names: tuple[str, ...] = (),
    ) -> TypedNativeVariablePage:
        """Read bounded name/type/size metadata; keep presentations private."""

        self._require_page_request(scope, stack_level, start, stop)
        checked_parameters = self._checked_role_parameters(role, parameter_names)
        variables, names = self._read_inventory(scope, stack_level, port)
        selected_names = self._names_for_role(names, role, checked_parameters)
        by_name = {name.casefold(): variable for name, variable in zip(names, variables)}
        selected: list[TypedNativeVariable] = []
        for name in selected_names[start:stop]:
            variable = by_name[name.casefold()]
            type_name = variable.type_name
            size = variable.collection_size
            if (
                not isinstance(type_name, str)
                or not 0 < len(type_name) <= MAX_TYPE_CHARS
                or size is not None
                and (type(size) is not int or not 0 <= size <= MAX_NATIVE_VARIABLE_INVENTORY)
            ):
                selected.append(TypedNativeVariable(name, None, None))
            else:
                selected.append(TypedNativeVariable(name, type_name, size))
        return TypedNativeVariablePage(
            tuple(selected),
            len(selected_names),
            stop if selected and stop < len(selected_names) else None,
        )

    @staticmethod
    def _require_page_request(
        scope: CaptureScope, stack_level: int, start: int, stop: int,
    ) -> None:
        CaptureInspectionExecutor._require_frame(scope, stack_level, allow_kernel=False)
        if (
            type(start) is not int
            or type(stop) is not int
            or start < 0
            or stop < start
        ):
            raise ValueError("CAPTURE variable pages require nonnegative bounds")
        if stop - start > MAX_PAGE_ITEMS:
            raise ValueError("CAPTURE variable pages require at most 100 names")

    def _read_inventory(
        self, scope: CaptureScope, stack_level: int, port: CaptureInspectionPort,
    ) -> tuple[tuple[FrameVariable, ...], tuple[str, ...]]:
        response = port.local_variables(
            stack_level=stack_level, timeout_s=self._request_timeout_s
        )
        self._require_frame(scope, stack_level, allow_kernel=False)
        if not isinstance(response, LocalVariablesResult) or response.error_occurred:
            raise CaptureInspectionUnavailable("CAPTURE frame variables are unavailable")
        variables = response.variables
        if (
            type(variables) is not tuple
            or len(variables) > MAX_NATIVE_VARIABLE_INVENTORY
            or any(not isinstance(item, FrameVariable) for item in variables)
        ):
            raise CaptureInspectionUnavailable("CAPTURE frame inventory is unavailable")
        try:
            names = tuple(
                SafePathSegment(ValuePathSegmentKind.VARIABLE, item.name).key
                for item in variables
            )
        except CapturePathError as error:
            raise CaptureInspectionUnavailable(
                "CAPTURE frame inventory is unavailable"
            ) from error
        if len({name.casefold() for name in names}) != len(names):
            raise CaptureInspectionUnavailable("CAPTURE frame inventory is unavailable")
        return variables, names

    def evaluate_helper(
        self,
        scope: CaptureScope,
        source: str,
        *,
        stack_level: int,
        max_text_size: int = 307_200,
        port: CaptureInspectionPort,
        result_policy: Callable[[EvaluationResult], object],
    ) -> object:
        """Run one trusted helper expression and apply policy on the worker.

        The source must come from the CAPTURE inspection policy, which limits
        paths and payload size. Confirmed BSL errors are rejected here; the
        supplied pure policy decodes a successful result and bounds public values.
        Empty wait intervals reuse one pending capability without a BSL
        execution deadline. A stop or unknown outcome propagates with arbiter
        ownership intact.
        """

        self._require_frame(scope, stack_level, allow_kernel=True)
        if not isinstance(source, str) or not source:
            raise ValueError("CAPTURE helper source is invalid")
        if type(max_text_size) is not int or max_text_size <= 0:
            raise ValueError("CAPTURE helper text limit is invalid")
        if not callable(result_policy):
            raise TypeError("CAPTURE helper result policy is required")
        result = evaluate_until_result(
            port,
            source,
            stack_level=stack_level,
            max_text_size=max_text_size,
            request_timeout_s=self._request_timeout_s,
            wait_interval_s=self._wait_interval_s,
        )
        if result.error_occurred:
            raise CaptureInspectionUnavailable("CAPTURE helper evaluation failed")
        return result_policy(result)

    @staticmethod
    def _require_frame(
        scope: CaptureScope, stack_level: int, *, allow_kernel: bool
    ) -> None:
        if (
            scope.context_state is not CaptureContextState.READY
            or scope.frame_identity is not CaptureFrameIdentity.CONFIRMED
            or scope.inspection_target_id != scope.identity.target_id
        ):
            raise RuntimeError("CAPTURE scope is not ready for inspection")
        if type(stack_level) is not int:
            raise ValueError("CAPTURE frame level is unavailable")
        frame = next(
            (
                item for item in scope.stack_frames
                if item.level == stack_level
                and item.target_id == scope.identity.target_id
            ),
            None,
        )
        if frame is None:
            raise ValueError("CAPTURE frame level is unavailable")
        if not allow_kernel:
            kernel = next(
                (
                    item for item in scope.stack_frames
                    if item.level == scope.kernel_stack_level
                ),
                None,
            )
            if kernel is None or CaptureInspectionExecutor._same_module(
                frame.location, kernel.location
            ):
                raise ValueError("Runtime kernel variables are private")

    @staticmethod
    def _same_module(left: ModuleLocation, right: ModuleLocation) -> bool:
        return (
            left.module_type == right.module_type
            and left.url == right.url
            and left.object_id == right.object_id
            and left.property_id == right.property_id
            and left.extension_name == right.extension_name
            and left.ext_id == right.ext_id
        )
