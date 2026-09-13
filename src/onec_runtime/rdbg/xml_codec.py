from __future__ import annotations

import base64
from collections.abc import Iterable
from uuid import UUID
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from onec_runtime.errors import ProtocolError
from onec_runtime.rdbg.models import (
    CollectionCell,
    CollectionRow,
    DebugTarget,
    EvaluationResult,
    FrameVariable,
    LocalVariablesResult,
    ModuleLocation,
    ModifyResult,
    StackFrame,
    StopEvent,
    TargetId,
)


RDBG_NS = "http://v8.1c.ru/8.3/debugger/debugRDBGRequestResponse"
BASE_NS = "http://v8.1c.ru/8.3/debugger/debugBaseData"
BP_NS = "http://v8.1c.ru/8.3/debugger/debugBreakpoints"
CALC_NS = "http://v8.1c.ru/8.3/debugger/debugCalculations"
AUTO_ATTACH_NS = "http://v8.1c.ru/8.3/debugger/debugAutoAttach"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

ElementTree.register_namespace("rdbg", RDBG_NS)
ElementTree.register_namespace("bd", BASE_NS)
ElementTree.register_namespace("bp", BP_NS)
ElementTree.register_namespace("calc", CALC_NS)
ElementTree.register_namespace("aa", AUTO_ATTACH_NS)
ElementTree.register_namespace("xsi", XSI_NS)


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local_name(child) == name]


def _descendants(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element.iter() if _local_name(child) == name]


def _required_text(element: ElementTree.Element, name: str) -> str:
    matches = _descendants(element, name)
    if not matches or matches[0].text is None or not matches[0].text.strip():
        raise ProtocolError(f"RDBG XML is missing required {name}")
    return matches[0].text.strip()


def _optional_text(element: ElementTree.Element, name: str) -> str | None:
    matches = _descendants(element, name)
    if not matches or matches[0].text is None:
        return None
    value = matches[0].text.strip()
    return value or None


def _target_id(element: ElementTree.Element) -> TargetId:
    seance_id = _optional_text(element, "seanceId")
    seance_no = _optional_text(element, "seanceNo")
    infobase_instance_id = _optional_text(element, "infoBaseInstanceID")
    return TargetId(
        UUID(_required_text(element, "id")),
        _required_text(element, "infoBaseAlias"),
        UUID(seance_id) if seance_id is not None else None,
        int(seance_no) if seance_no is not None else None,
        UUID(infobase_instance_id) if infobase_instance_id is not None else None,
        _optional_text(element, "configVersion") or "",
    )


def _optional_bool(element: ElementTree.Element, name: str) -> bool | None:
    matches = _descendants(element, name)
    if not matches or matches[0].text is None:
        return None
    value = matches[0].text.strip().casefold()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ProtocolError(f"RDBG XML has invalid {name}: {matches[0].text!r}")


def _parse(payload: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise ProtocolError(f"Malformed RDBG XML: {error}") from error


def parse_ping_document(payload: bytes) -> ElementTree.Element:
    """Parse a ping response once for the specialized event extractors below."""
    return _parse(payload)


def validate_command_acknowledgement(payload: bytes, *, command: str) -> None:
    """Accept only the two success forms emitted by RDBG command endpoints."""
    if not payload.strip():
        return
    try:
        root = _parse(payload)
    except ProtocolError as error:
        raise ProtocolError(f"Invalid {command} acknowledgement") from error
    results = _children(root, "result")
    if (
        _local_name(root) != "response"
        or len(results) != 1
        or (results[0].text or "").strip().casefold() != "success"
        or len(results[0]) != 0
    ):
        raise ProtocolError(f"Invalid {command} acknowledgement")


def _serialize(root: ElementTree.Element) -> bytes:
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _request(alias: str, ui_id: UUID) -> ElementTree.Element:
    root = ElementTree.Element(_tag(RDBG_NS, "request"))
    ElementTree.SubElement(root, _tag(RDBG_NS, "infoBaseAlias")).text = alias
    ElementTree.SubElement(root, _tag(RDBG_NS, "idOfDebuggerUI")).text = str(ui_id)
    return root


def _target_id_element(parent: ElementTree.Element, name: str, target: TargetId) -> None:
    target_element = ElementTree.SubElement(
        parent,
        _tag(RDBG_NS, name),
        {_tag(XSI_NS, "type"): "bd:DebugTargetIdLight"},
    )
    ElementTree.SubElement(target_element, _tag(BASE_NS, "id")).text = str(target.id)


def build_attach_request(alias: str, ui_id: UUID) -> bytes:
    root = _request(alias, ui_id)
    options = ElementTree.SubElement(root, _tag(RDBG_NS, "options"))
    ElementTree.SubElement(options, _tag(RDBG_NS, "foregroundAbility")).text = "true"
    return _serialize(root)


def build_detach_request(alias: str, ui_id: UUID) -> bytes:
    return _serialize(_request(alias, ui_id))


def build_terminate_request(
    alias: str, ui_id: UUID, targets: tuple[TargetId, ...], states_payload: bytes,
    *, target_type: str = "Server",
) -> bytes:
    """Terminate requires full native identities, unlike attach/step Light IDs."""
    if target_type not in {"Server", "ManagedClient"}:
        raise ValueError("Unsupported termination target type")
    document = _parse(states_payload)
    root = _request(alias, ui_id)
    for target in targets:
        matches = [
            node for node in _descendants(document, "targetID")
            if _target_id(node) == target
            and _required_text(node, "targetType") == target_type
        ]
        if len(matches) != 1:
            raise ProtocolError(f"{target_type} termination requires one exact target identity")
        identity = ElementTree.fromstring(ElementTree.tostring(matches[0]))
        identity.tag = _tag(RDBG_NS, "targetID")
        root.append(identity)
    if not targets:
        raise ValueError(f"{target_type} termination requires at least one target")
    return _serialize(root)


def build_init_settings_request(
    alias: str, ui_id: UUID, *, break_on_next: bool = False
) -> bytes:
    root = _request(alias, ui_id)
    data = ElementTree.SubElement(root, _tag(RDBG_NS, "data"))
    ElementTree.SubElement(data, _tag(RDBG_NS, "breakOnNextLine")).text = str(
        break_on_next
    ).lower()
    return _serialize(root)


def build_auto_attach_request(
    alias: str,
    ui_id: UUID,
    *,
    target_types: Iterable[str] = ("ServerEmulation", "ManagedClient"),
) -> bytes:
    root = _request(alias, ui_id)
    settings = ElementTree.SubElement(
        root,
        _tag(RDBG_NS, "autoAttachSettings"),
        {_tag(XSI_NS, "type"): "aa:DebugAutoAttachSettings"},
    )
    for target_type in target_types:
        ElementTree.SubElement(
            settings, _tag(AUTO_ATTACH_NS, "targetType")
        ).text = target_type
    ElementTree.SubElement(settings, _tag(AUTO_ATTACH_NS, "areaName")).text = ""
    return _serialize(root)


def build_get_targets_request(alias: str, ui_id: UUID) -> bytes:
    return _serialize(_request(alias, ui_id))


def build_clear_break_request(alias: str, ui_id: UUID) -> bytes:
    return _serialize(_request(alias, ui_id))


def build_attach_target_request(
    alias: str, ui_id: UUID, target: TargetId, *, attach: bool
) -> bytes:
    root = _request(alias, ui_id)
    ElementTree.SubElement(root, _tag(RDBG_NS, "attach")).text = str(attach).lower()
    _target_id_element(root, "id", target)
    return _serialize(root)


def build_call_stack_request(alias: str, ui_id: UUID, target: TargetId) -> bytes:
    root = _request(alias, ui_id)
    outer = ElementTree.SubElement(root, _tag(RDBG_NS, "id"))
    ElementTree.SubElement(outer, _tag(BASE_NS, "id")).text = str(target.id)
    return _serialize(root)


def build_breakpoint_request(
    alias: str, ui_id: UUID, location: ModuleLocation
) -> bytes:
    return build_breakpoints_request(alias, ui_id, (location,))


def build_breakpoints_request(
    alias: str,
    ui_id: UUID,
    locations: Iterable[ModuleLocation],
) -> bytes:
    location_items = tuple(locations)
    if not location_items:
        raise ValueError("At least one breakpoint location is required")
    fields: Iterable[tuple[str, str]] = (
        ("isActive", "true"),
        ("breakOnCondition", "false"),
        ("breakOnParentMethod", "false"),
        ("breakOnHitCount", "false"),
        ("hitCountVariant", "0"),
        ("hitCount", "1"),
        ("showOutputMessage", "false"),
        ("putStackTrace", "false"),
        ("putHitCount", "false"),
        ("continueExecution", "false"),
        ("temp", "false"),
        ("user", "true"),
    )
    common_fields = "".join(
        f"<{name}>{escape(value)}</{name}>" for name, value in fields
    )

    def module_key(location: ModuleLocation) -> tuple[object, ...]:
        return (
            location.module_type,
            location.url,
            location.extension_name,
            location.object_id,
            location.property_id,
            location.ext_id,
        )

    grouped: dict[tuple[object, ...], list[ModuleLocation]] = {}
    for location in location_items:
        grouped.setdefault(module_key(location), []).append(location)

    def module_breakpoint(module_locations: list[ModuleLocation]) -> str:
        location = module_locations[0]
        bp_items = "".join(
            f"<bpInfo><line>{item.line}</line>{common_fields}</bpInfo>"
            for item in module_locations
        )
        return (
            f'<moduleBPInfo xmlns="{BP_NS}"><id>'
            f'<type xmlns="{BASE_NS}">{escape(location.module_type)}</type>'
            f'<URL xmlns="{BASE_NS}">{escape(location.url)}</URL>'
            f'<extensionName xmlns="{BASE_NS}">{escape(location.extension_name)}</extensionName>'
            f'<objectID xmlns="{BASE_NS}">{location.object_id}</objectID>'
            f'<propertyID xmlns="{BASE_NS}">{location.property_id}</propertyID>'
            f'<extId xmlns="{BASE_NS}">{location.ext_id}</extId>'
            f"</id>{bp_items}</moduleBPInfo>"
        )

    breakpoint_items = "".join(
        module_breakpoint(module_locations)
        for module_locations in grouped.values()
    )
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<request>"
        f'<infoBaseAlias xmlns="{RDBG_NS}">{escape(alias)}</infoBaseAlias>'
        f'<idOfDebuggerUI xmlns="{RDBG_NS}">{ui_id}</idOfDebuggerUI>'
        f'<bpWorkspace xmlns="{RDBG_NS}">'
        f"{breakpoint_items}"
        "</bpWorkspace></request>"
    )
    return xml.encode("utf-8")


def _calculation_storage(
    parent: ElementTree.Element,
    element_name: str,
    expression: str,
    result_id: UUID,
    *,
    max_text_size: int = 307_200,
    interface: str = "context",
    start_index: int | None = None,
    page_size: int | None = None,
    stack_level: int = 0,
) -> None:
    storage = ElementTree.SubElement(
        parent,
        _tag(RDBG_NS, element_name),
        {_tag(XSI_NS, "type"): "calc:CalculationSourceDataStorage"},
    )
    ElementTree.SubElement(storage, _tag(CALC_NS, "stackLevel")).text = str(
        stack_level
    )
    source = ElementTree.SubElement(storage, _tag(CALC_NS, "srcCalcInfo"))
    ElementTree.SubElement(source, _tag(CALC_NS, "expressionResultID")).text = str(result_id)
    item = ElementTree.SubElement(source, _tag(CALC_NS, "calcItem"))
    ElementTree.SubElement(item, _tag(CALC_NS, "itemType")).text = "expression"
    ElementTree.SubElement(item, _tag(CALC_NS, "expression")).text = expression
    ElementTree.SubElement(item, _tag(CALC_NS, "property")).text = ""
    ElementTree.SubElement(source, _tag(CALC_NS, "interfaces")).text = interface
    if start_index is not None:
        ElementTree.SubElement(source, _tag(CALC_NS, "startIndex")).text = str(
            start_index
        )
    if page_size is not None:
        ElementTree.SubElement(source, _tag(CALC_NS, "pageSize")).text = str(page_size)
    options = ElementTree.SubElement(storage, _tag(CALC_NS, "presOptions"))
    ElementTree.SubElement(options, _tag(CALC_NS, "maxTextSize")).text = str(
        max_text_size
    )
    ElementTree.SubElement(options, _tag(CALC_NS, "stopOnFirstEOL")).text = "false"


def build_eval_request(
    alias: str,
    ui_id: UUID,
    target: TargetId,
    expression: str,
    result_id: UUID,
    *,
    max_text_size: int = 307_200,
    stack_level: int = 0,
) -> bytes:
    if max_text_size <= 0:
        raise ValueError("max_text_size must be positive")
    if stack_level < 0:
        raise ValueError("stack_level must be non-negative")
    root = _request(alias, ui_id)
    # This is a server-side batching delay, not the HTTP timeout. Large values
    # add directly to every variable read even when calculation is immediate.
    ElementTree.SubElement(root, _tag(RDBG_NS, "calcWaitingTime")).text = "25"
    _target_id_element(root, "targetID", target)
    _calculation_storage(
        root,
        "expr",
        expression,
        result_id,
        max_text_size=max_text_size,
        stack_level=stack_level,
    )
    return _serialize(root)


def build_collection_eval_request(
    alias: str,
    ui_id: UUID,
    target: TargetId,
    expression: str,
    result_id: UUID,
    *,
    start_index: int,
    page_size: int = 2400,
    max_text_size: int = 4096,
    stack_level: int = 0,
) -> bytes:
    if start_index < 0:
        raise ValueError("start_index must be non-negative")
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if max_text_size <= 0:
        raise ValueError("max_text_size must be positive")
    if stack_level < 0:
        raise ValueError("stack_level must be non-negative")
    root = _request(alias, ui_id)
    ElementTree.SubElement(root, _tag(RDBG_NS, "calcWaitingTime")).text = "25"
    _target_id_element(root, "targetID", target)
    _calculation_storage(
        root,
        "expr",
        expression,
        result_id,
        max_text_size=max_text_size,
        interface="collection",
        start_index=start_index,
        page_size=page_size,
        stack_level=stack_level,
    )
    return _serialize(root)


def build_local_variables_request(
    alias: str,
    ui_id: UUID,
    target: TargetId,
    stack_level: int,
    result_id: UUID,
    *,
    max_text_size: int = 307_200,
) -> bytes:
    if type(max_text_size) is not int or max_text_size <= 0:
        raise ValueError("max_text_size must be positive")
    root = _request(alias, ui_id)
    ElementTree.SubElement(root, _tag(RDBG_NS, "calcWaitingTime")).text = "25"
    _target_id_element(root, "targetID", target)
    storage = ElementTree.SubElement(
        root,
        _tag(RDBG_NS, "expr"),
        {_tag(XSI_NS, "type"): "calc:CalculationSourceDataStorage"},
    )
    ElementTree.SubElement(storage, _tag(CALC_NS, "stackLevel")).text = str(
        stack_level
    )
    source = ElementTree.SubElement(storage, _tag(CALC_NS, "srcCalcInfo"))
    ElementTree.SubElement(source, _tag(CALC_NS, "expressionResultID")).text = str(
        result_id
    )
    ElementTree.SubElement(source, _tag(CALC_NS, "interfaces")).text = "context"
    options = ElementTree.SubElement(storage, _tag(CALC_NS, "presOptions"))
    ElementTree.SubElement(options, _tag(CALC_NS, "maxTextSize")).text = str(max_text_size)
    ElementTree.SubElement(options, _tag(CALC_NS, "stopOnFirstEOL")).text = "false"
    return _serialize(root)


def build_modify_request(
    alias: str,
    ui_id: UUID,
    target: TargetId,
    variable: str,
    value_expression: str,
    result_id: UUID,
) -> bytes:
    root = _request(alias, ui_id)
    _target_id_element(root, "targetID", target)
    _calculation_storage(root, "modifyDataPath", variable, result_id)
    value = ElementTree.SubElement(
        root,
        _tag(RDBG_NS, "newValueInfo"),
        {_tag(XSI_NS, "type"): "calc:NewValueInfo"},
    )
    ElementTree.SubElement(value, _tag(CALC_NS, "variant")).text = "expr"
    ElementTree.SubElement(value, _tag(CALC_NS, "valueExpression")).text = value_expression
    ElementTree.SubElement(root, _tag(RDBG_NS, "timeout")).text = "60000"
    return _serialize(root)


def build_step_request(alias: str, ui_id: UUID, target: TargetId) -> bytes:
    root = _request(alias, ui_id)
    _target_id_element(root, "targetID", target)
    ElementTree.SubElement(root, _tag(RDBG_NS, "action")).text = "Continue"
    ElementTree.SubElement(root, _tag(RDBG_NS, "simple")).text = "false"
    return _serialize(root)


def _module_location(frame: ElementTree.Element) -> ModuleLocation:
    module_matches = _descendants(frame, "moduleID")
    if not module_matches:
        raise ProtocolError("RDBG XML is missing required moduleID")
    module = module_matches[0]
    try:
        type_nodes = _descendants(module, "type")
        module_type = (
            (type_nodes[0].text or "").strip() if type_nodes else "ConfigModule"
        )
        if not module_type:
            module_type = "ConfigModule"
        url_nodes = _descendants(module, "URL")
        extension_nodes = _descendants(module, "extensionName")
        ext_id_nodes = _descendants(module, "extId")
        return ModuleLocation(
            module_type=module_type,
            url=(url_nodes[0].text or "").strip() if url_nodes else "",
            object_id=UUID(_required_text(module, "objectID")),
            property_id=UUID(_required_text(module, "propertyID")),
            line=int(_required_text(frame, "lineNo")),
            extension_name=(extension_nodes[0].text or "").strip()
            if extension_nodes
            else "",
            ext_id=int((ext_id_nodes[0].text or "0").strip())
            if ext_id_nodes
            else 0,
        )
    except ValueError as error:
        raise ProtocolError(f"Invalid module location: {error}") from error


def parse_targets(payload: bytes) -> list[DebugTarget]:
    root = _parse(payload)
    targets: list[DebugTarget] = []
    for item in _descendants(root, "item"):
        target_nodes = _children(item, "targetID")
        if not target_nodes:
            raise ProtocolError("RDBG XML is missing required targetID")
        target_node = target_nodes[0]
        try:
            target_id = _target_id(target_node)
            state_number_text = _required_text(item, "stateNum")
            targets.append(
                DebugTarget(
                    target_id=target_id,
                    target_type=_required_text(target_node, "targetType"),
                    state=_required_text(item, "state"),
                    state_number=int(state_number_text),
                )
            )
        except ValueError as error:
            raise ProtocolError(f"Invalid target state: {error}") from error
    return targets


def parse_call_stack(payload: bytes, target: TargetId) -> list[StackFrame]:
    root = _parse(payload)
    frames: list[StackFrame] = []
    protocol_frames = _descendants(root, "callStack")
    for level, frame in enumerate(reversed(protocol_frames)):
        if not (
            _descendants(frame, "objectID")
            and _descendants(frame, "propertyID")
        ):
            continue
        frames.append(StackFrame(target, level, _module_location(frame)))
    return frames


def parse_ping_events(payload: bytes) -> list[StopEvent]:
    return parse_ping_events_from_document(parse_ping_document(payload))


def parse_ping_events_from_document(
    root: ElementTree.Element,
) -> list[StopEvent]:
    events: list[StopEvent] = []
    for item in _children(root, "result"):
        commands = _children(item, "cmdID")
        if not commands or not commands[0].text:
            continue
        command = commands[0].text.strip()
        if command != "callStackFormed":
            continue
        target_nodes = _children(item, "targetID")
        stack_nodes = _children(item, "callStack")
        if not target_nodes or not stack_nodes:
            raise ProtocolError("Stop event is missing targetID or callStack")
        target_node = target_nodes[0]
        target = _target_id(target_node)
        runtime_errors = _descendants(item, "exceptionStr")
        runtime_error = (
            _decode_presentation(runtime_errors[0].text or "")
            if runtime_errors
            else ""
        )
        addressable_frames: list[StackFrame] = []
        for level, frame in enumerate(reversed(stack_nodes)):
            if not (
                _descendants(frame, "objectID")
                and _descendants(frame, "propertyID")
            ):
                continue
            addressable_frames.append(StackFrame(target, level, _module_location(frame)))
        if not addressable_frames:
            raise ProtocolError("Stop event has no addressable stack frame")
        stack = tuple(frame.location for frame in addressable_frames)
        events.append(
            StopEvent(
                target,
                stack[0],
                command,
                stop_by_breakpoint=_optional_bool(item, "stopByBP"),
                suspended_by_other=_optional_bool(item, "suspendedByOther"),
                stack=stack,
                runtime_error=runtime_error,
                stack_frames=tuple(addressable_frames),
            )
        )
    return events


def parse_ping_target_events(payload: bytes) -> list[DebugTarget]:
    return parse_ping_target_events_from_document(parse_ping_document(payload))


def parse_ping_target_events_from_document(
    root: ElementTree.Element,
) -> list[DebugTarget]:
    targets: list[DebugTarget] = []
    for item in _children(root, "result"):
        commands = _children(item, "cmdID")
        if not commands or (commands[0].text or "").strip() != "targetStarted":
            continue
        target_nodes = _children(item, "targetID")
        if not target_nodes:
            raise ProtocolError("Target-started event is missing targetID")
        target_node = target_nodes[0]
        targets.append(
            DebugTarget(
                _target_id(target_node),
                _required_text(target_node, "targetType"),
                "Started",
            )
        )
    return targets


def _decode_presentation(value: str) -> str:
    try:
        compact = "".join(value.split())
        return base64.b64decode(compact, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return value


def _preferred(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    direct = _children(element, name)
    return direct if direct else _descendants(element, name)


def _required_preferred_text(element: ElementTree.Element, name: str) -> str:
    matches = _children(element, name)
    if matches and matches[0].text is not None and matches[0].text.strip():
        return matches[0].text.strip()
    return _required_text(element, name)


def _parse_evaluation_node(result: ElementTree.Element) -> EvaluationResult:
    base_nodes = _children(result, "evalExprResBaseData")
    container = base_nodes[0] if base_nodes else result
    value_nodes = _preferred(container, "resultValueInfo")
    if not value_nodes:
        raise ProtocolError("RDBG XML is missing required resultValueInfo")
    value = value_nodes[0]
    error_nodes = _preferred(container, "errorOccurred")
    error_occurred = bool(error_nodes and (error_nodes[0].text or "").lower() == "true")
    error_text_nodes = _preferred(container, "exceptionStr")
    error_text = _decode_presentation(error_text_nodes[0].text or "") if error_text_nodes else ""
    type_nodes = _preferred(value, "typeName")
    if error_occurred and not type_nodes:
        type_name = "Ошибка"
    else:
        type_name = _required_text(value, "typeName")
    presentation_nodes = _preferred(value, "pres")
    type_code_nodes = _preferred(value, "typeCode")
    value_string_nodes = _preferred(value, "valueString")
    value_decimal_nodes = _preferred(value, "valueDecimal")
    collection_size_nodes = _preferred(value, "collectionSize")
    if error_occurred and not presentation_nodes:
        presentation = error_text
    else:
        presentation = (
            _decode_presentation(presentation_nodes[0].text or "")
            if presentation_nodes
            else (
                (value_decimal_nodes[0].text or "").strip()
                if value_decimal_nodes
                else type_name
            )
        )
    collection_rows: list[CollectionRow] = []
    calculation_nodes = _children(container, "calculationResult")
    row_nodes = (
        _children(calculation_nodes[0], "valueOfCollectionInfo")
        if calculation_nodes
        else _descendants(container, "valueOfCollectionInfo")
    )
    for row_index, row_node in enumerate(row_nodes):
        cells: list[CollectionCell] = []
        cell_values: list[tuple[str, ElementTree.Element]] = []
        for property_node in _children(row_node, "valueOfContextPropInfo"):
            property_info_nodes = _children(property_node, "propInfo")
            name_nodes = (
                _children(property_info_nodes[0], "propName")
                if property_info_nodes
                else _descendants(property_node, "propName")
            )
            cell_value_nodes = _children(property_node, "valueInfo")
            if not cell_value_nodes:
                continue
            cell_values.append(
                (
                    (
                        (name_nodes[0].text or "").strip()
                        if name_nodes
                        else "Значение"
                    ),
                    cell_value_nodes[0],
                )
            )
        if not cell_values:
            cell_values.extend(
                ("Значение", cell_value)
                for cell_value in _children(row_node, "valueInfo")
            )
        for cell_name, cell_value in cell_values:
            cell_type = _required_text(cell_value, "typeName")
            cell_presentations = _children(cell_value, "pres")
            cell_strings = _children(cell_value, "valueString")
            cell_decimals = _children(cell_value, "valueDecimal")
            cell_dates = _children(cell_value, "valueDateTime")
            cell_booleans = _children(cell_value, "valueBoolean")
            cell_presentation = (
                _decode_presentation(cell_presentations[0].text or "")
                if cell_presentations
                else cell_type
            )
            cells.append(
                CollectionCell(
                    name=cell_name,
                    type_name=cell_type,
                    presentation=cell_presentation,
                    value_string=(
                        _decode_presentation(cell_strings[0].text or "")
                        if cell_strings
                        else ""
                    ),
                    value_decimal=(
                        (cell_decimals[0].text or "").strip()
                        if cell_decimals
                        else ""
                    ),
                    value_date_time=(
                        (cell_dates[0].text or "").strip() if cell_dates else ""
                    ),
                    value_boolean=(
                        (cell_booleans[0].text or "").strip().casefold() == "true"
                        if cell_booleans
                        else None
                    ),
                )
            )
        collection_rows.append(CollectionRow(row_index, tuple(cells)))
    return EvaluationResult(
        result_id=UUID(_required_preferred_text(container, "expressionResultID")),
        type_name=type_name,
        presentation=presentation,
        error_occurred=error_occurred,
        error_text=error_text,
        type_code=(
            int((type_code_nodes[0].text or "").strip())
            if type_code_nodes and (type_code_nodes[0].text or "").strip()
            else None
        ),
        value_string=(
            _decode_presentation(value_string_nodes[0].text or "")
            if value_string_nodes
            else ""
        ),
        collection_size=(
            int((collection_size_nodes[0].text or "").strip())
            if collection_size_nodes
            and (collection_size_nodes[0].text or "").strip()
            else None
        ),
        collection_rows=tuple(collection_rows),
        value_decimal=(
            (value_decimal_nodes[0].text or "").strip()
            if value_decimal_nodes
            else ""
        ),
    )


def parse_eval_response(payload: bytes) -> EvaluationResult | None:
    root = _parse(payload)
    result_nodes = _children(root, "result")
    if not result_nodes:
        if not list(root) and not (root.text or "").strip():
            return None
        raise ProtocolError("RDBG XML is missing required result")
    return _parse_evaluation_node(result_nodes[0])


def parse_eval_result(payload: bytes) -> EvaluationResult:
    result = parse_eval_response(payload)
    if result is None:
        raise ProtocolError("RDBG XML is missing required result")
    return result


def parse_modify_result(payload: bytes) -> ModifyResult:
    root = _parse(payload)
    state_nodes = _children(root, "newValueState")
    if not state_nodes:
        raise ProtocolError("RDBG XML is missing required newValueState")
    state = state_nodes[0]
    state_text = _required_text(state, "evalResultState")
    error_nodes = _descendants(state, "errorOccurred")
    error_text_nodes = _descendants(state, "exceptionStr")
    error_text = (
        _decode_presentation(error_text_nodes[0].text or "")
        if error_text_nodes
        else ""
    )
    error_occurred = (
        state_text.casefold() != "correctly"
        or bool(
            error_nodes
            and (error_nodes[0].text or "").strip().casefold() == "true"
        )
        or bool(error_text)
    )
    value_nodes = _descendants(state, "resultValueInfo")
    if error_occurred:
        type_name = "Ошибка"
        presentation = error_text or state_text
    elif not value_nodes:
        raise ProtocolError("RDBG XML is missing required resultValueInfo")
    else:
        value = value_nodes[0]
        type_name = _required_text(value, "typeName")
        type_code_nodes = _descendants(value, "typeCode")
        value_string_nodes = _descendants(value, "valueString")
        presentation_nodes = _descendants(value, "pres")
        presentation = (
            _decode_presentation(presentation_nodes[0].text or "")
            if presentation_nodes
            else type_name
        )
    return ModifyResult(
        result_id=UUID(_required_text(state, "expressionResultID")),
        type_name=type_name,
        presentation=presentation,
        error_occurred=error_occurred,
        error_text=error_text,
        type_code=(
            int((type_code_nodes[0].text or "").strip())
            if not error_occurred
            and type_code_nodes
            and (type_code_nodes[0].text or "").strip()
            else None
        ),
        value_string=(
            _decode_presentation(value_string_nodes[0].text or "")
            if not error_occurred and value_string_nodes
            else ""
        ),
    )


def _frame_variable(
    node: ElementTree.Element,
    *,
    name_field: str,
    value_field: str,
) -> FrameVariable | None:
    name_nodes = _descendants(node, name_field)
    value_nodes = _descendants(node, value_field)
    if not name_nodes or not (name_nodes[0].text or "").strip() or not value_nodes:
        return None
    value = value_nodes[0]
    type_name = _required_text(value, "typeName")
    presentation_nodes = _descendants(value, "pres")
    presentation = (
        _decode_presentation(presentation_nodes[0].text or "")
        if presentation_nodes
        else type_name
    )
    size_nodes = _descendants(value, "collectionSize")
    return FrameVariable(
        name=(name_nodes[0].text or "").strip(),
        type_name=type_name,
        presentation=presentation,
        collection_size=(
            int((size_nodes[0].text or "").strip())
            if size_nodes and (size_nodes[0].text or "").strip()
            else None
        ),
    )


def _parse_local_variables_node(result: ElementTree.Element) -> LocalVariablesResult:
    variables: list[FrameVariable] = []
    for node in _descendants(result, "valueOfContextPropInfo"):
        variable = _frame_variable(
            node, name_field="propName", value_field="valueInfo"
        )
        if variable is not None:
            variables.append(variable)
    for node in _descendants(result, "localVariables"):
        variable = _frame_variable(
            node,
            name_field="localVariableName",
            value_field="resultValueInfo",
        )
        if variable is not None:
            variables.append(variable)
    error_nodes = _descendants(result, "errorOccurred")
    error_occurred = bool(
        error_nodes and (error_nodes[0].text or "").strip().lower() == "true"
    )
    error_text_nodes = _descendants(result, "exceptionStr")
    error_text = (
        _decode_presentation(error_text_nodes[0].text or "")
        if error_text_nodes
        else ""
    )
    return LocalVariablesResult(
        result_id=UUID(_required_text(result, "expressionResultID")),
        variables=tuple(variables),
        error_occurred=error_occurred,
        error_text=error_text,
    )


def parse_local_variables_result(payload: bytes) -> LocalVariablesResult:
    root = _parse(payload)
    result_nodes = _children(root, "result")
    if not result_nodes:
        raise ProtocolError("RDBG XML is missing required result")
    return _parse_local_variables_node(result_nodes[0])


def parse_ping_local_variables(payload: bytes) -> list[LocalVariablesResult]:
    return parse_ping_local_variables_from_document(parse_ping_document(payload))


def parse_ping_local_variables_from_document(
    root: ElementTree.Element,
) -> list[LocalVariablesResult]:
    results: list[LocalVariablesResult] = []
    for item in _children(root, "result"):
        commands = _children(item, "cmdID")
        if not commands or (commands[0].text or "").strip() != "exprEvaluated":
            continue
        if _descendants(item, "valueOfCollectionInfo"):
            continue
        results.append(_parse_local_variables_node(item))
    return results


def parse_ping_evaluations(payload: bytes) -> list[EvaluationResult]:
    return parse_ping_evaluations_from_document(parse_ping_document(payload))


def parse_ping_evaluations_from_document(
    root: ElementTree.Element,
) -> list[EvaluationResult]:
    evaluations: list[EvaluationResult] = []
    for item in _children(root, "result"):
        commands = _children(item, "cmdID")
        if not commands or (commands[0].text or "").strip() != "exprEvaluated":
            continue
        collection_nodes = _descendants(item, "valueOfCollectionInfo")
        if not collection_nodes and (
            _descendants(item, "valueOfContextPropInfo")
            or _descendants(item, "localVariables")
        ):
            continue
        evaluations.append(_parse_evaluation_node(item))
    return evaluations
