from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol
from uuid import UUID

import pandas as pd

from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.rdbg.models import CollectionCell, EvaluationResult
from onec_runtime.rdbg.session import RdbgSession
from onec_runtime.table_materialization import (
    ReferenceMode,
    ReferencePolicy,
    TableMaterializationError,
)


class TableFrameMaterializer(Protocol):
    def to_df(self, handle: str, policy: ReferencePolicy) -> Any: ...


class CollectionEvaluationSession(Protocol):
    def evaluate_collection(
        self,
        expression: str,
        *,
        start_index: int,
        page_size: int,
    ) -> EvaluationResult: ...


def _number(value: str, *, exact_decimal: str = "") -> int | float:
    if exact_decimal:
        decimal = Decimal(exact_decimal.strip().replace(",", "."))
        return int(decimal) if decimal == decimal.to_integral() else float(decimal)
    normalized = "".join(character for character in value if character.isdecimal() or character in ",.-+")
    if "," in normalized and "." not in normalized:
        # RDBG presentations use comma both as a locale decimal separator and
        # as an en-US group separator. Integer groups of three are IDs/counts.
        left, right = normalized.rsplit(",", 1)
        normalized = left + right if len(right) == 3 else left + "." + right
    elif "," in normalized:
        normalized = normalized.replace(",", "")
    decimal = Decimal(normalized)
    return int(decimal) if decimal == decimal.to_integral() else float(decimal)


def evaluation_to_python(result: EvaluationResult) -> Any:
    value = result.presentation.strip()
    if result.type_name == "Неопределено":
        return None
    if result.type_name == "Число":
        return _number(value, exact_decimal=result.value_decimal)
    if result.type_name == "Булево":
        return value.lower() in {"истина", "true"}
    if result.type_name == "Строка":
        if result.value_string:
            return result.value_string
        return value[1:-1].replace('""', '"') if value.startswith('"') and value.endswith('"') else value
    return value


def _reference_mode(value: str | ReferenceMode) -> ReferenceMode:
    try:
        return ReferenceMode(value)
    except ValueError as error:
        raise TableMaterializationError(f"unknown reference mode: {value}") from error


def _is_reference(cell: CollectionCell) -> bool:
    return "Ссылка." in cell.type_name


def _scalar_cell(cell: CollectionCell) -> object:
    if cell.type_name in {"Неопределено", "Null"}:
        return None
    if cell.type_name == "Число":
        source = cell.value_decimal or cell.presentation
        try:
            value = Decimal(source.replace(",", "."))
        except InvalidOperation as error:
            raise TableMaterializationError(
                f"RDBG number is invalid in column {cell.name}: {source}"
            ) from error
        return int(value) if value == value.to_integral() else float(value)
    if cell.type_name == "Булево":
        if cell.value_boolean is not None:
            return cell.value_boolean
        return cell.presentation.strip().casefold() in {"истина", "true"}
    if cell.type_name == "Строка":
        if cell.value_string:
            return cell.value_string
        value = cell.presentation
        return (
            value[1:-1].replace('""', '"')
            if value.startswith('"') and value.endswith('"')
            else value
        )
    if cell.type_name == "Дата":
        source = cell.value_date_time or cell.presentation
        try:
            return datetime.fromisoformat(source)
        except ValueError:
            return source
    return cell.presentation


class RdbgCollectionMaterializer:
    def __init__(
        self,
        session: CollectionEvaluationSession,
        *,
        page_size: int = 2400,
        profiler: PhaseRecorder | None = None,
    ) -> None:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self.session = session
        self.page_size = page_size
        self.profiler = profiler

    def _profile(self, phase: str, operation, **metadata):  # type: ignore[no-untyped-def]
        if self.profiler is None:
            return operation()
        return self.profiler.measure(phase, operation, **metadata)

    def to_df(self, handle: str, policy: ReferencePolicy) -> pd.DataFrame:
        if not handle:
            raise TableMaterializationError("table handle must not be empty")
        default_mode = _reference_mode(policy.refs)
        overrides = dict(policy.ref_columns or {})
        if not policy.uuid_suffix:
            raise TableMaterializationError("UUID suffix must not be empty")
        expected_columns: tuple[str, ...] | None = None
        reference_columns: set[str] = set()
        values: dict[str, list[object]] = {}
        uuid_values: dict[str, list[object]] = {}
        total: int | None = None
        start_index = 0
        while total is None or start_index < total:
            page = self.session.evaluate_collection(
                handle,
                start_index=start_index,
                page_size=self.page_size,
            )
            if page.error_occurred:
                raise TableMaterializationError(page.error_text or page.presentation)
            if page.collection_size is None or page.collection_size < 0:
                raise TableMaterializationError(
                    "RDBG collection result has no valid collection size"
                )
            if total is None:
                total = page.collection_size
            elif page.collection_size != total:
                raise TableMaterializationError(
                    "RDBG collection size changed during materialization"
                )
            expected_count = min(self.page_size, total - start_index)
            if len(page.collection_rows) != expected_count:
                raise TableMaterializationError(
                    "RDBG collection page row count mismatch: "
                    f"start={start_index}, expected={expected_count}, "
                    f"observed={len(page.collection_rows)}"
                )
            def convert_page() -> int:
                nonlocal expected_columns, values, reference_columns
                cell_count = 0
                for offset, row in enumerate(page.collection_rows):
                    expected_index = start_index + offset
                    if row.index != expected_index:
                        raise TableMaterializationError(
                            f"RDBG collection row index mismatch: expected {expected_index}, "
                            f"got {row.index}"
                        )
                    names = tuple(cell.name for cell in row.cells)
                    if len(set(names)) != len(names):
                        raise TableMaterializationError(
                            f"RDBG collection row {row.index} has duplicate columns"
                        )
                    if expected_columns is None:
                        expected_columns = names
                        values = {name: [] for name in names}
                        reference_columns = {
                            cell.name for cell in row.cells if _is_reference(cell)
                        }
                        unknown = set(overrides) - reference_columns
                        if unknown:
                            raise TableMaterializationError(
                                "unknown reference column: " + ", ".join(sorted(unknown))
                            )
                        for name in reference_columns:
                            mode = _reference_mode(overrides.get(name, default_mode))
                            if mode is ReferenceMode.BOTH:
                                generated = name + policy.uuid_suffix
                                if generated in names:
                                    raise TableMaterializationError(
                                        f"generated UUID column collision: {generated}"
                                    )
                            uuid_values[name] = []
                    elif names != expected_columns:
                        raise TableMaterializationError(
                            f"RDBG collection schema changed at row {row.index}"
                        )
                    cell_count += len(row.cells)
                    for cell in row.cells:
                        if not _is_reference(cell):
                            values[cell.name].append(_scalar_cell(cell))
                            continue
                        mode = _reference_mode(overrides.get(cell.name, default_mode))
                        values[cell.name].append(cell.presentation)
                        if mode in {ReferenceMode.UUID, ReferenceMode.BOTH}:
                            if not cell.value_string:
                                raise TableMaterializationError(
                                    f"RDBG did not return UUID for reference column {cell.name}"
                                )
                            try:
                                identifier = UUID(cell.value_string)
                            except ValueError as error:
                                raise TableMaterializationError(
                                    f"RDBG returned invalid UUID for reference column {cell.name}"
                                ) from error
                            uuid_values[cell.name].append(identifier)
                return cell_count

            self._profile(
                "dataframe.convert_page",
                convert_page,
                page_start=start_index,
                item_count=lambda count: count,
            )
            start_index += len(page.collection_rows)
            if total == 0:
                break

        def build_dataframe() -> pd.DataFrame:
            result: dict[str, pd.Series] = {}
            for name in expected_columns or ():
                if name not in reference_columns:
                    result[name] = pd.Series(values[name])
                    continue
                mode = _reference_mode(overrides.get(name, default_mode))
                if mode is ReferenceMode.UUID:
                    result[name] = pd.Series(uuid_values[name], dtype="object")
                else:
                    result[name] = pd.Series(values[name], dtype="string")
                    if mode is ReferenceMode.BOTH:
                        result[name + policy.uuid_suffix] = pd.Series(
                            uuid_values[name], dtype="object"
                        )
            frame = pd.DataFrame(result)
            frame.attrs["onec_transport"] = "rdbg-collection"
            frame.attrs["rdbg_page_size"] = self.page_size
            frame.attrs["reference_modes"] = {
                name: _reference_mode(overrides.get(name, default_mode)).value
                for name in reference_columns
            }
            return frame

        return self._profile(
            "dataframe.build",
            build_dataframe,
            item_count=lambda frame: len(frame.index),
        )


@dataclass(frozen=True)
class OnecTableValue:
    session: RdbgSession
    expression: str
    materializer: TableFrameMaterializer | None = None

    def _eval(self, suffix: str = "") -> Any:
        result = self.session.evaluate(self.expression + suffix)
        if result.error_occurred:
            raise ValueError(result.error_text)
        return evaluation_to_python(result)

    def to_df(
        self,
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: Mapping[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
    ) -> Any:
        materializer = self.materializer or RdbgCollectionMaterializer(self.session)
        return materializer.to_df(
            self.expression,
            ReferencePolicy(refs, ref_columns, uuid_suffix),
        )

    def to_df_legacy(self):  # type: ignore[no-untyped-def]
        """Compatibility-only RDBG cell snapshot; use ``to_df`` for server data."""
        import pandas as pd

        column_count = int(self._eval(".Колонки.Количество()"))
        row_count = int(self._eval(".Количество()"))
        columns = [
            str(self._eval(f".Колонки[{index}].Имя"))
            for index in range(column_count)
        ]
        rows: list[dict[str, Any]] = []
        for row_index in range(row_count):
            row: dict[str, Any] = {}
            for column in columns:
                escaped = column.replace('"', '""')
                row[column] = self._eval(f'[{row_index}]["{escaped}"]')
            rows.append(row)
        return pd.DataFrame(rows, columns=columns)
