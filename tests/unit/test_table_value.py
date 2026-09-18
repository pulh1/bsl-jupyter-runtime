from collections import deque
from uuid import uuid4

import pytest

from onec_runtime.rdbg.models import (
    CollectionCell,
    CollectionRow,
    EvaluationResult,
)
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.table_materialization import ReferencePolicy, TableMaterializationError
from onec_runtime.table_value import OnecTableValue, evaluation_to_python


def result(type_name: str, presentation: str) -> EvaluationResult:
    return EvaluationResult(uuid4(), type_name, presentation, False)


class FakeSession:
    def __init__(self) -> None:
        self.values = deque(
            [
                result("Число", "2"),
                result("Число", "2"),
                result("Строка", '"Имя"'),
                result("Строка", '"Сумма"'),
                result("Строка", '"А"'),
                result("Число", "10"),
                result("Строка", '"Б"'),
                result("Число", "20"),
            ]
        )

    def evaluate(self, _: str) -> EvaluationResult:
        return self.values.popleft()


def test_legacy_snapshot_materializes_rows_and_columns() -> None:
    table = OnecTableValue(FakeSession(), "e1cRuntimeКонтекст.Таблица")  # type: ignore[arg-type]

    frame = table.to_df_legacy()

    assert frame.to_dict(orient="records") == [
        {"Имя": "А", "Сумма": 10},
        {"Имя": "Б", "Сумма": 20},
    ]


class FakeMaterializer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ReferencePolicy]] = []

    def to_df(self, handle: str, policy: ReferencePolicy):  # type: ignore[no-untyped-def]
        self.calls.append((handle, policy))
        return "frame"


def test_to_df_delegates_reference_policy_to_server_materializer() -> None:
    materializer = FakeMaterializer()
    table = OnecTableValue(
        FakeSession(),  # type: ignore[arg-type]
        "notebook-table-7",
        materializer=materializer,
    )

    frame = table.to_df(
        refs="uuid",
        ref_columns={"Сотрудник": "both"},
        uuid_suffix="__id",
    )

    assert frame == "frame"
    assert materializer.calls == [
        (
            "notebook-table-7",
            ReferencePolicy(
                refs="uuid",
                ref_columns={"Сотрудник": "both"},
                uuid_suffix="__id",
            ),
        )
    ]


class FakeCollectionSession:
    def __init__(self, row_count: int = 10_000) -> None:
        self.row_count = row_count
        self.calls: list[tuple[str, int, int]] = []

    def evaluate_collection(
        self, expression: str, *, start_index: int, page_size: int
    ) -> EvaluationResult:
        self.calls.append((expression, start_index, page_size))
        stop = min(start_index + page_size, self.row_count)
        rows = tuple(
            CollectionRow(
                index,
                (
                    CollectionCell("Номер", "Число", str(index + 1), value_decimal=str(index + 1)),
                    CollectionCell(
                        "Сотрудник",
                        "СправочникСсылка.Сотрудники",
                        f"Сотрудник {index + 1}",
                    ),
                ),
            )
            for index in range(start_index, stop)
        )
        return EvaluationResult(
            uuid4(),
            "ТаблицаЗначений",
            "ТаблицаЗначений",
            False,
            collection_size=self.row_count,
            collection_rows=rows,
        )


def test_to_df_reads_stopped_frame_in_2400_row_collection_pages() -> None:
    session = FakeCollectionSession()
    table = OnecTableValue(
        session,  # type: ignore[arg-type]
        "e1cRuntimeКонтекст.ZupMaterializationTable",
    )

    frame = table.to_df()

    assert frame.shape == (10_000, 2)
    assert frame.iloc[0].to_dict() == {"Номер": 1, "Сотрудник": "Сотрудник 1"}
    assert frame.iloc[-1].to_dict() == {
        "Номер": 10_000,
        "Сотрудник": "Сотрудник 10000",
    }
    assert session.calls == [
        ("e1cRuntimeКонтекст.ZupMaterializationTable", 0, 2400),
        ("e1cRuntimeКонтекст.ZupMaterializationTable", 2400, 2400),
        ("e1cRuntimeКонтекст.ZupMaterializationTable", 4800, 2400),
        ("e1cRuntimeКонтекст.ZupMaterializationTable", 7200, 2400),
        ("e1cRuntimeКонтекст.ZupMaterializationTable", 9600, 2400),
    ]


def test_collection_materializer_profiles_page_conversion_and_dataframe_build() -> None:
    from onec_runtime.table_value import RdbgCollectionMaterializer

    session = FakeCollectionSession()
    recorder = PhaseRecorder()

    frame = RdbgCollectionMaterializer(
        session,  # type: ignore[arg-type]
        page_size=2400,
        profiler=recorder,
    ).to_df("e1cRuntimeКонтекст.ZupMaterializationTable", ReferencePolicy())

    assert frame.shape == (10_000, 2)
    page_events = [
        event for event in recorder.events if event.phase == "dataframe.convert_page"
    ]
    assert [event.page_start for event in page_events] == [0, 2400, 4800, 7200, 9600]
    assert [event.item_count for event in page_events] == [4800, 4800, 4800, 4800, 800]
    dataframe_events = [
        event for event in recorder.events if event.phase == "dataframe.build"
    ]
    assert len(dataframe_events) == 1
    assert dataframe_events[0].item_count == 10_000


def test_maps_basic_rdbg_values() -> None:
    assert evaluation_to_python(result("Неопределено", "Неопределено")) is None
    assert evaluation_to_python(result("Булево", "Истина")) is True
    assert evaluation_to_python(result("Число", "1,000")) == 1000


def test_numeric_evaluation_prefers_exact_decimal_over_presentation() -> None:
    evaluation = EvaluationResult(
        uuid4(),
        "Число",
        "Число",
        False,
        value_decimal="2.0260816E+7",
    )

    assert evaluation_to_python(evaluation) == 20260816


def test_string_evaluation_prefers_exact_value_over_debugger_presentation() -> None:
    value = '["capture:first","","capture:last"]'
    evaluation = EvaluationResult(
        uuid4(),
        "Строка",
        '"["capture:first","","capture:last"]"',
        False,
        value_string=value,
    )

    assert evaluation_to_python(evaluation) == value
