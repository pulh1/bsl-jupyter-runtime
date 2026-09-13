from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from integration.support.bootstrap_evidence import verify_server_bootstrap_evidence
from onec_runtime.errors import ProtocolError


def _location(line: int) -> dict[str, object]:
    return {
        "module_type": "ExtensionModule",
        "url": "",
        "object_id": "cb953767-f436-4a5b-9e09-13a67d6e0201",
        "property_id": "d5963243-262e-4398-b4d7-fb16d06484f6",
        "line": line,
        "extension_name": "OnecInteractiveRuntime",
        "ext_id": 0,
    }


def _target(target_id: str, target_type: str) -> dict[str, object]:
    return {
        "target_id": {
            "id": target_id,
            "infobase_alias": "DefAlias",
            "seance_id": "22222222-2222-2222-2222-222222222222",
            "seance_no": 1,
            "infobase_instance_id": "33333333-3333-3333-3333-333333333333",
            "config_version": "synthetic-version",
        },
        "target_type": target_type,
        "state": "stopped",
        "state_number": None,
    }


def _stop(target: dict[str, object], location: dict[str, object]) -> dict[str, object]:
    return {
        "target_id": deepcopy(target["target_id"]),
        "location": deepcopy(location),
        "reason": "breakpoint",
        "stop_by_breakpoint": True,
        "suspended_by_other": False,
        "stack": [],
        "runtime_error": "",
    }


def complete_evidence() -> tuple[dict[str, object], ...]:
    startup = {
        **_location(4),
        "object_id": "883af47f-bd19-491f-8dfa-3bd4ff6e0cfa",
        "property_id": "d22e852a-cf8a-4f77-8ccb-3548e7792bea",
    }
    entry = _location(132)
    service = _location(134)
    client = _target("44444444-4444-4444-4444-444444444444", "ManagedClient")
    server = _target("11111111-1111-1111-1111-111111111111", "ServerEmulation")
    environment = {
        "managed_startup": startup,
        "server_entry": entry,
        "server_service": service,
        "client_target": client,
        "target": server,
    }
    guard = {
        "variable": "ПродолжатьЦикл",
        "before": {
            "type_name": "Булево",
            "presentation": "Ложь",
            "error_occurred": False,
        },
        "write": {
            "expression": "Истина",
            "type_name": "Булево",
            "presentation": "Истина",
            "error_occurred": False,
        },
        "after": {
            "type_name": "Булево",
            "presentation": "Истина",
            "error_occurred": False,
        },
        "transcript_interval": {"after_sequence": 0, "end_sequence": 9},
    }
    transcript = [
        {"sequence": sequence, "command": command}
        for sequence, command in enumerate(
            (
                "evalExpr",
                "modifyValue",
                "evalExpr",
                "setBreakpoints",
                "step",
                "pingDebugUIParams",
                "setBreakpoints",
                "step",
                "pingDebugUIParams",
            ),
            start=1,
        )
    ]
    return (
        environment,
        guard,
        _stop(client, startup),
        _stop(server, entry),
        _stop(server, service),
        transcript,
    )


def test_complete_two_stage_bootstrap_evidence_is_accepted() -> None:
    verify_server_bootstrap_evidence(*complete_evidence())


@pytest.mark.parametrize(
    "mutation",
    (
        "guard_default",
        "same_subject",
        "different_session",
        "entry_stop_subject",
        "entry_not_breakpoint",
        "service_stop_location",
        "environment_entry",
        "missing_server_continue",
        "reordered_transitions",
    ),
)
def test_bootstrap_evidence_mutations_are_rejected(mutation: str) -> None:
    values = [deepcopy(value) for value in complete_evidence()]
    environment, guard, _managed, entry_stop, service_stop, transcript = values
    if mutation == "guard_default":
        guard["before"]["presentation"] = "Истина"
    elif mutation == "same_subject":
        environment["client_target"]["target_id"]["id"] = environment["target"][
            "target_id"
        ]["id"]
    elif mutation == "different_session":
        environment["target"]["target_id"]["seance_id"] = str(UUID(int=9))
    elif mutation == "entry_stop_subject":
        entry_stop["target_id"]["id"] = str(UUID(int=8))
    elif mutation == "entry_not_breakpoint":
        entry_stop["stop_by_breakpoint"] = False
    elif mutation == "service_stop_location":
        service_stop["location"] = deepcopy(environment["server_entry"])
    elif mutation == "environment_entry":
        environment["server_entry"]["line"] = 131
    elif mutation == "missing_server_continue":
        transcript[7]["command"] = "pingDebugUIParams"
    elif mutation == "reordered_transitions":
        transcript[3]["command"], transcript[4]["command"] = (
            transcript[4]["command"],
            transcript[3]["command"],
        )

    with pytest.raises(ProtocolError):
        verify_server_bootstrap_evidence(*values)
