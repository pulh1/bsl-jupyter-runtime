from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class TargetId:
    id: UUID
    infobase_alias: str
    seance_id: UUID | None = None
    seance_no: int | None = None
    infobase_instance_id: UUID | None = None
    config_version: str = ""


@dataclass(frozen=True)
class DebugTarget:
    target_id: TargetId
    target_type: str
    state: str
    state_number: int | None = None


@dataclass(frozen=True)
class ModuleLocation:
    module_type: str
    url: str
    object_id: UUID
    property_id: UUID
    line: int
    extension_name: str = ""
    ext_id: int = 0


@dataclass(frozen=True)
class StackFrame:
    target_id: TargetId
    level: int
    location: ModuleLocation


@dataclass(frozen=True)
class StopEvent:
    target_id: TargetId
    location: ModuleLocation
    reason: str
    stop_by_breakpoint: bool | None = None
    suspended_by_other: bool | None = None
    stack: tuple[ModuleLocation, ...] = ()
    runtime_error: str = ""
    stack_frames: tuple[StackFrame, ...] = ()


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class PendingEvaluation:
    target_id: TargetId
    result_id: UUID
    owner: object

    def __post_init__(self) -> None:
        if (
            type(self.target_id) is not TargetId
            or type(self.result_id) is not UUID
        ):
            raise ValueError("pending evaluation identity is invalid")

    def __repr__(self) -> str:
        return (
            "PendingEvaluation(target_id=<redacted>, "
            "result_id=<redacted>, owner=<redacted>)"
        )


@dataclass(frozen=True)
class CollectionCell:
    name: str
    type_name: str
    presentation: str
    value_string: str = ""
    value_decimal: str = ""
    value_date_time: str = ""
    value_boolean: bool | None = None


@dataclass(frozen=True)
class CollectionRow:
    index: int
    cells: tuple[CollectionCell, ...]


@dataclass(frozen=True)
class EvaluationResult:
    result_id: UUID
    type_name: str
    presentation: str
    error_occurred: bool
    error_text: str = ""
    type_code: int | None = None
    value_string: str = ""
    collection_size: int | None = None
    collection_rows: tuple[CollectionRow, ...] = ()
    value_decimal: str = ""


@dataclass(frozen=True)
class ModifyResult:
    result_id: UUID
    type_name: str
    presentation: str
    error_occurred: bool
    error_text: str = ""
    type_code: int | None = None
    value_string: str = ""


@dataclass(frozen=True)
class FrameVariable:
    name: str
    type_name: str
    presentation: str
    collection_size: int | None = None


@dataclass(frozen=True)
class LocalVariablesResult:
    result_id: UUID
    variables: tuple[FrameVariable, ...]
    error_occurred: bool = False
    error_text: str = ""
