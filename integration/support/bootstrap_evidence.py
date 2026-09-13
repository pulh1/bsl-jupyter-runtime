from __future__ import annotations

from typing import cast

from onec_runtime.errors import ProtocolError


_FALSE_VALUE = {
    "type_name": "Булево",
    "presentation": "Ложь",
    "error_occurred": False,
}
_TRUE_VALUE = {
    "type_name": "Булево",
    "presentation": "Истина",
    "error_occurred": False,
}
_WRITE_VALUE = {"expression": "Истина", **_TRUE_VALUE}
_SEMANTIC_COMMANDS = (
    "evalExpr",
    "modifyValue",
    "evalExpr",
    "setBreakpoints",
    "step",
    "setBreakpoints",
    "step",
)


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProtocolError(f"Bootstrap evidence {field} must be an object")
    return cast(dict[str, object], value)


def _target_id(target: dict[str, object], field: str) -> dict[str, object]:
    target_id = _object(target.get("target_id"), f"{field}.target_id")
    required = {
        "id",
        "infobase_alias",
        "seance_id",
        "seance_no",
        "infobase_instance_id",
        "config_version",
    }
    if set(target_id) != required:
        raise ProtocolError(f"Bootstrap evidence {field}.target_id is incomplete")
    return target_id


def _verify_stop(
    stop: dict[str, object],
    *,
    target_id: dict[str, object],
    location: dict[str, object],
    field: str,
) -> None:
    if stop.get("target_id") != target_id or stop.get("location") != location:
        raise ProtocolError(f"Bootstrap evidence {field} identity/location mismatch")
    if stop.get("reason") not in {"breakpoint", "callStackFormed"}:
        raise ProtocolError(f"Bootstrap evidence {field} is not a breakpoint stop")
    if (
        stop.get("stop_by_breakpoint") is not True
        or stop.get("suspended_by_other") is not False
        or stop.get("runtime_error") != ""
    ):
        raise ProtocolError(f"Bootstrap evidence {field} is not a clean breakpoint stop")


def verify_server_bootstrap_evidence(
    environment: dict[str, object],
    guard: dict[str, object],
    managed_stop: dict[str, object],
    entry_stop: dict[str, object],
    service_stop: dict[str, object],
    transcript: list[dict[str, object]],
) -> None:
    managed_startup = _object(environment.get("managed_startup"), "managed_startup")
    server_entry = _object(environment.get("server_entry"), "server_entry")
    server_service = _object(environment.get("server_service"), "server_service")
    client_target = _object(environment.get("client_target"), "client_target")
    server_target = _object(environment.get("target"), "target")
    if client_target.get("target_type") != "ManagedClient":
        raise ProtocolError("Bootstrap client target is not ManagedClient")
    if server_target.get("target_type") != "ServerEmulation":
        raise ProtocolError("Bootstrap server target is not ServerEmulation")

    client_id = _target_id(client_target, "client_target")
    server_id = _target_id(server_target, "target")
    if client_id["id"] == server_id["id"]:
        raise ProtocolError("Bootstrap client and server subjects must be distinct")
    session_fields = (
        "infobase_alias",
        "seance_id",
        "seance_no",
        "infobase_instance_id",
        "config_version",
    )
    if any(client_id[name] != server_id[name] for name in session_fields):
        raise ProtocolError("Bootstrap client/server subjects are not one base session")

    location_identity = (
        "module_type",
        "url",
        "object_id",
        "property_id",
        "extension_name",
        "ext_id",
    )
    if any(server_entry.get(name) != server_service.get(name) for name in location_identity):
        raise ProtocolError("Bootstrap entry and service markers are not one module")
    entry_line = server_entry.get("line")
    service_line = server_service.get("line")
    if (
        type(entry_line) is not int
        or type(service_line) is not int
        or cast(int, entry_line) >= cast(int, service_line)
    ):
        raise ProtocolError("Bootstrap entry marker must precede the service marker")

    _verify_stop(
        managed_stop,
        target_id=client_id,
        location=managed_startup,
        field="managed-startup-stop",
    )
    _verify_stop(
        entry_stop,
        target_id=server_id,
        location=server_entry,
        field="server-entry-stop",
    )
    _verify_stop(
        service_stop,
        target_id=server_id,
        location=server_service,
        field="server-start-stop",
    )

    if (
        set(guard)
        != {"variable", "before", "write", "after", "transcript_interval"}
        or guard.get("variable") != "ПродолжатьЦикл"
        or guard.get("before") != _FALSE_VALUE
        or guard.get("write") != _WRITE_VALUE
        or guard.get("after") != _TRUE_VALUE
    ):
        raise ProtocolError("Bootstrap guard is not exact false-to-true-to-true evidence")
    interval = _object(guard.get("transcript_interval"), "transcript_interval")
    if set(interval) != {"after_sequence", "end_sequence"}:
        raise ProtocolError("Bootstrap transcript interval is incomplete")
    after_sequence = interval.get("after_sequence")
    end_sequence = interval.get("end_sequence")
    if (
        type(after_sequence) is not int
        or type(end_sequence) is not int
        or cast(int, after_sequence) < 0
        or cast(int, end_sequence) <= cast(int, after_sequence)
    ):
        raise ProtocolError("Bootstrap transcript interval is invalid")
    if [row.get("sequence") for row in transcript] != list(
        range(1, len(transcript) + 1)
    ):
        raise ProtocolError("Bootstrap transcript sequence is incomplete")
    interval_rows = [
        row
        for row in transcript
        if cast(int, after_sequence) < cast(int, row["sequence"]) <= cast(int, end_sequence)
    ]
    if not interval_rows or interval_rows[-1]["sequence"] != end_sequence:
        raise ProtocolError("Bootstrap transcript interval is outside the transcript")
    semantic = tuple(
        cast(str, row.get("command"))
        for row in interval_rows
        if row.get("command") in {"evalExpr", "modifyValue", "setBreakpoints", "step"}
    )
    if semantic != _SEMANTIC_COMMANDS:
        raise ProtocolError("Bootstrap transcript does not contain two ordered transitions")
    commands = [row.get("command") for row in interval_rows]
    first_step = commands.index("step")
    second_breakpoint = commands.index("setBreakpoints", first_step + 1)
    second_step = commands.index("step", second_breakpoint + 1)
    if (
        "pingDebugUIParams" not in commands[first_step + 1 : second_breakpoint]
        or "pingDebugUIParams" not in commands[second_step + 1 :]
    ):
        raise ProtocolError("Bootstrap transcript does not prove both breakpoint stops")
