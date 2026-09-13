from __future__ import annotations

from dataclasses import asdict

import pytest

from onec_runtime_mcp.agent.observation import (
    ManagerOrigin,
    ObservationPlan,
    ObservationResult,
    ObservationSourceKind,
    SelectionKind,
)


def test_observation_defaults_to_proxy_and_rejects_expression_language() -> None:
    plan = ObservationPlan.from_wire(
        {
            "items": [
                {
                    "alias": "kadry",
                    "source": {
                        "kind": "context_binding",
                        "name": "bsl.КадровыеДанные",
                    },
                }
            ],
            "budget_profile": "agent_metadata",
        }
    )

    assert plan.items[0].result is ObservationResult.PROXY
    assert plan.items[0].source.kind is ObservationSourceKind.CONTEXT_BINDING
    with pytest.raises((TypeError, ValueError)):
        ObservationPlan.from_wire(
            {"items": [{"alias": "bad", "source": "КадровыеДанные[:10]"}]}
        )


def test_structured_selection_covers_head_columns_and_array_slice() -> None:
    plan = ObservationPlan.from_wire(
        {
            "items": [
                {
                    "alias": "rows",
                    "source": {"kind": "frame_local", "name": "КадровыеДанные"},
                    "result": "proxy",
                    "select": {
                        "kind": "table_rows",
                        "offset": 0,
                        "limit": 10,
                        "columns": ["Сотрудник", "Сумма"],
                    },
                },
                {
                    "alias": "items",
                    "source": {"kind": "context_binding", "name": "bsl.Массив"},
                    "select": {"kind": "slice", "offset": 5, "limit": 15},
                },
            ],
            "budget_profile": "agent_preview",
        }
    )

    assert plan.items[0].select.kind is SelectionKind.TABLE_ROWS
    assert plan.items[0].select.columns == ("Сотрудник", "Сумма")
    assert plan.items[1].select.offset == 5
    assert plan.items[1].select.limit == 15


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "context_binding", "name": "КадровыеДанные[0]"},
        {"kind": "frame_local", "name": "Запрос.Выполнить()"},
        {"kind": "temporary_table", "manager_id": "vtm-1", "table": ""},
        {
            "kind": "temporary_table",
            "manager_id": "vtm-1",
            "table": "ВТ",
            "origin": {"namespace": "frame", "root": "Запрос", "fields": []},
        },
    ],
)
def test_sources_fail_closed_without_expression_or_ambiguous_manager(source: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        ObservationPlan.from_wire({"items": [{"alias": "x", "source": source}]})


def test_manager_origin_is_structured_and_redacted() -> None:
    origin = ManagerOrigin.from_wire(
        {
            "namespace": "frame",
            "root": "Запрос",
            "fields": ["МенеджерВременныхТаблиц"],
        }
    )
    assert origin.fields == ("МенеджерВременныхТаблиц",)
    assert asdict(origin) == {
        "namespace": "frame",
        "root": "Запрос",
        "fields": ("МенеджерВременныхТаблиц",),
    }


def test_duplicate_aliases_and_unbounded_selection_are_rejected() -> None:
    with pytest.raises(ValueError):
        ObservationPlan.from_wire(
            {
                "items": [
                    {"alias": "x", "source": {"kind": "frame_local", "name": "A"}},
                    {"alias": "x", "source": {"kind": "frame_local", "name": "B"}},
                ]
            }
        )
    with pytest.raises(ValueError):
        ObservationPlan.from_wire(
            {
                "items": [
                    {
                        "alias": "x",
                        "source": {"kind": "frame_local", "name": "A"},
                        "select": {"kind": "slice", "offset": 0, "limit": 0},
                    }
                ]
            }
        )
