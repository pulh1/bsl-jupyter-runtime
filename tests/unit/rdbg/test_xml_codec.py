import base64
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree

import pytest

from onec_runtime.rdbg import xml_codec
from onec_runtime.errors import ProtocolError
from onec_runtime.rdbg.models import DebugTarget, ModuleLocation, TargetId
from onec_runtime.rdbg.xml_codec import (
    AUTO_ATTACH_NS,
    BASE_NS,
    BP_NS,
    CALC_NS,
    RDBG_NS,
    build_attach_request,
    build_attach_target_request,
    build_auto_attach_request,
    build_breakpoint_request,
    build_call_stack_request,
    build_collection_eval_request,
    build_detach_request,
    build_eval_request,
    build_get_targets_request,
    build_init_settings_request,
    build_local_variables_request,
    build_modify_request,
    build_step_request,
    parse_call_stack,
    parse_eval_result,
    parse_local_variables_result,
    parse_modify_result,
    parse_ping_events,
    parse_ping_evaluations,
    parse_ping_local_variables,
    parse_ping_target_events,
    parse_targets,
)
from onec_runtime.table_value import evaluation_to_python


UI_ID = UUID("11111111-1111-1111-1111-111111111111")
TARGET_UUID = UUID("22222222-2222-2222-2222-222222222222")
TARGET = TargetId(TARGET_UUID, "DefAlias")


def test_numeric_evaluation_without_presentation_preserves_exact_decimal() -> None:
    payload = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID>33333333-3333-3333-3333-333333333333</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>Число</typeName>
        <valueDecimal>2.0260816E+7</valueDecimal></resultValueInfo>
      <errorOccurred>false</errorOccurred></result></response>""".encode()

    evaluation = parse_eval_result(payload)

    assert evaluation.value_decimal == "2.0260816E+7"
    assert evaluation_to_python(evaluation) == 20260816


def test_ping_command_must_be_a_direct_result_child() -> None:
    payload = f"""<response xmlns="{RDBG_NS}"><result><wrapper>
      <cmdID>exprEvaluated</cmdID></wrapper></result></response>""".encode()

    assert parse_ping_target_events(payload) == []
    assert parse_ping_events(payload) == []
    assert parse_ping_local_variables(payload) == []
    assert parse_ping_evaluations(payload) == []


def test_parses_successful_modify_value_state() -> None:
    result_id = UUID("44444444-4444-4444-4444-444444444444")
    payload = f"""<response xmlns="{BASE_NS}" xmlns:rdbg="{RDBG_NS}"
      xmlns:calc="{CALC_NS}"><rdbg:newValueState>
      <calc:evalResultState>correctly</calc:evalResultState>
      <calc:expressionResultID>{result_id}</calc:expressionResultID>
      <calc:resultValueInfo><calc:typeName>Строка</calc:typeName>
      <calc:pres>IklSX01BUktFUiI=</calc:pres></calc:resultValueInfo>
      </rdbg:newValueState></response>""".encode()

    result = parse_modify_result(payload)

    assert result.result_id == result_id
    assert result.type_name == "Строка"
    assert result.presentation == '"IR_MARKER"'
    assert result.error_occurred is False
    assert result.error_text == ""


def test_preserves_raw_value_fields_separately_from_debug_result_id() -> None:
    result_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    raw_value = base64.b64encode(
        b"01234567-89ab-cdef-0123-456789abcdef"
    ).decode()
    presentation = base64.b64encode("Иванов И.И.".encode()).decode()
    payload = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}">
        <typeCode>97</typeCode>
        <typeName>СправочникСсылка.Сотрудники</typeName>
        <valueString>{raw_value}</valueString>
        <pres>{presentation}</pres>
      </resultValueInfo><errorOccurred>false</errorOccurred>
    </result></response>""".encode()

    result = parse_eval_result(payload)

    assert result.result_id == result_id
    assert result.type_code == 97
    assert result.value_string == "01234567-89ab-cdef-0123-456789abcdef"
    assert result.presentation == "Иванов И.И."
    assert result.value_string != str(result.result_id)


def test_parses_failed_modify_value_state_without_value_info() -> None:
    result_id = UUID("55555555-5555-5555-5555-555555555555")
    error_text = base64.b64encode("Переменная не определена".encode()).decode()
    payload = f"""<response xmlns="{BASE_NS}" xmlns:rdbg="{RDBG_NS}"
      xmlns:calc="{CALC_NS}"><rdbg:newValueState>
      <calc:evalResultState>withErrors</calc:evalResultState>
      <calc:expressionResultID>{result_id}</calc:expressionResultID>
      <calc:resultValueInfo>
      <calc:presProcessedCorrectly>false</calc:presProcessedCorrectly>
      </calc:resultValueInfo>
      <calc:errorOccurred>true</calc:errorOccurred>
      <calc:exceptionStr>{error_text}</calc:exceptionStr>
      </rdbg:newValueState></response>""".encode()

    result = parse_modify_result(payload)

    assert result.result_id == result_id
    assert result.type_name == "Ошибка"
    assert result.presentation == "Переменная не определена"
    assert result.error_occurred is True
    assert result.error_text == "Переменная не определена"
OBJECT_UUID = UUID("8fc91d24-20f5-4da4-8ff7-7a7c682f80f5")
PROPERTY_UUID = UUID("a637f77f-3840-441d-a1c3-699c8c5cb7e0")
LOCATION = ModuleLocation(
    module_type="ExtMDModule",
    url="file:///C:/runtime%20build/Kernel.epf",
    object_id=OBJECT_UUID,
    property_id=PROPERTY_UUID,
    line=14,
    ext_id=1,
)
CAPTURE_A = ModuleLocation(
    module_type="ExtensionModule",
    url="",
    object_id=UUID("cb953767-f436-4a5b-9e09-13a67d6e0201"),
    property_id=UUID("d5963243-262e-4398-b4d7-fb16d06484f6"),
    line=73,
    extension_name="OnecInteractiveRuntime",
)
CAPTURE_B = ModuleLocation(
    module_type=CAPTURE_A.module_type,
    url=CAPTURE_A.url,
    object_id=CAPTURE_A.object_id,
    property_id=CAPTURE_A.property_id,
    line=74,
    extension_name=CAPTURE_A.extension_name,
)


def local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def descendants(root: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [item for item in root.iter() if local_name(item) == name]


def test_builds_session_and_target_control_requests() -> None:
    attach = ElementTree.fromstring(build_attach_request("DefAlias", UI_ID))
    init = ElementTree.fromstring(build_init_settings_request("DefAlias", UI_ID))
    auto = ElementTree.fromstring(
        build_auto_attach_request(
            "DefAlias", UI_ID, target_types=("ServerEmulation", "ManagedClient")
        )
    )
    targets = ElementTree.fromstring(build_get_targets_request("DefAlias", UI_ID))
    attach_target = ElementTree.fromstring(
        build_attach_target_request("DefAlias", UI_ID, TARGET, attach=True)
    )
    stack = ElementTree.fromstring(build_call_stack_request("DefAlias", UI_ID, TARGET))
    detach = ElementTree.fromstring(build_detach_request("DefAlias", UI_ID))

    for root in (attach, init, auto, targets, attach_target, stack, detach):
        assert root.tag == f"{{{RDBG_NS}}}request"
        assert descendants(root, "idOfDebuggerUI")[0].text == str(UI_ID)
    assert descendants(attach, "foregroundAbility")[0].text == "true"
    assert descendants(init, "breakOnNextLine")[0].text == "false"
    assert descendants(auto, "targetType")[0].tag == f"{{{AUTO_ATTACH_NS}}}targetType"
    assert [item.text for item in descendants(auto, "targetType")] == [
        "ServerEmulation",
        "ManagedClient",
    ]
    assert descendants(attach_target, "attach")[0].text == "true"
    assert descendants(attach_target, "id")[-1].text == str(TARGET_UUID)
    assert descendants(stack, "id")[-1].text == str(TARGET_UUID)


def test_builds_external_service_breakpoint_with_stable_identity() -> None:
    root = ElementTree.fromstring(build_breakpoint_request("DefAlias", UI_ID, LOCATION))

    assert root.tag == "request"
    assert descendants(root, "moduleBPInfo")[0].tag == f"{{{BP_NS}}}moduleBPInfo"
    assert descendants(root, "type")[0].tag == f"{{{BASE_NS}}}type"
    assert descendants(root, "type")[0].text == "ExtMDModule"
    assert descendants(root, "URL")[0].text == LOCATION.url
    assert descendants(root, "objectID")[0].text == str(OBJECT_UUID)
    assert descendants(root, "propertyID")[0].text == str(PROPERTY_UUID)
    assert descendants(root, "extId")[0].text == "1"
    assert descendants(root, "line")[0].text == "14"
    assert descendants(root, "isActive")[0].text == "true"
    assert descendants(root, "continueExecution")[0].text == "false"
    assert descendants(root, "user")[0].text == "true"


def test_builds_one_workspace_with_multiple_module_breakpoints() -> None:
    root = ElementTree.fromstring(
        xml_codec.build_breakpoints_request(
            "DefAlias", UI_ID, (LOCATION, CAPTURE_A, CAPTURE_B)
        )
    )

    modules = descendants(root, "moduleBPInfo")
    assert len(modules) == 2
    assert [[line.text for line in descendants(item, "line")] for item in modules] == [
        ["14"],
        ["73", "74"],
    ]
    assert [descendants(item, "objectID")[0].text for item in modules] == [
        str(LOCATION.object_id),
        str(CAPTURE_A.object_id),
    ]


def test_builds_eval_modify_and_continue_requests() -> None:
    result_id = UUID("33333333-3333-3333-3333-333333333333")
    eval_root = ElementTree.fromstring(
        build_eval_request("DefAlias", UI_ID, TARGET, "e1cRuntimeКонтекст.Счетчик", result_id)
    )
    modify_root = ElementTree.fromstring(
        build_modify_request(
            "DefAlias", UI_ID, TARGET, "ТекущаяИнструкция", '"А = 1;"', result_id
        )
    )
    step_root = ElementTree.fromstring(build_step_request("DefAlias", UI_ID, TARGET))

    assert descendants(eval_root, "expression")[0].text == "e1cRuntimeКонтекст.Счетчик"
    assert descendants(eval_root, "expressionResultID")[0].text == str(result_id)
    assert descendants(modify_root, "modifyDataPath")[0].tag == f"{{{RDBG_NS}}}modifyDataPath"
    assert descendants(modify_root, "stackLevel")[0].tag == f"{{{CALC_NS}}}stackLevel"
    assert descendants(modify_root, "expression")[0].text == "ТекущаяИнструкция"
    assert descendants(modify_root, "variant")[0].text == "expr"
    assert descendants(modify_root, "valueExpression")[0].text == '"А = 1;"'
    assert descendants(modify_root, "timeout")[0].text == "60000"
    assert descendants(step_root, "action")[0].text == "Continue"


def test_builds_eval_request_with_bounded_presentation_size() -> None:
    result_id = UUID("33333333-3333-3333-3333-333333333333")

    root = ElementTree.fromstring(
        build_eval_request(
            "DefAlias",
            UI_ID,
            TARGET,
            "e1cRuntimeКонтекст.Ссылка",
            result_id,
            max_text_size=4096,
        )
    )

    assert descendants(root, "maxTextSize")[0].text == "4096"


def test_builds_eval_request_for_selected_stack_level() -> None:
    result_id = UUID("33333333-3333-3333-3333-333333333333")

    root = ElementTree.fromstring(
        build_eval_request(
            "DefAlias",
            UI_ID,
            TARGET,
            "e1cRuntimeКонтекст.Счетчик",
            result_id,
            stack_level=2,
        )
    )

    assert descendants(root, "stackLevel")[0].text == "2"


def test_builds_collection_eval_request_for_requested_page() -> None:
    result_id = UUID("33333333-3333-3333-3333-333333333333")

    root = ElementTree.fromstring(
        build_collection_eval_request(
            "DefAlias",
            UI_ID,
            TARGET,
            "e1cRuntimeКонтекст.ZupMaterializationTable",
            result_id,
            start_index=4800,
            page_size=2400,
            max_text_size=4096,
            stack_level=3,
        )
    )

    assert descendants(root, "expression")[0].text == "e1cRuntimeКонтекст.ZupMaterializationTable"
    assert descendants(root, "interfaces")[0].text == "collection"
    assert descendants(root, "startIndex")[0].text == "4800"
    assert descendants(root, "pageSize")[0].text == "2400"
    assert descendants(root, "maxTextSize")[0].text == "4096"
    assert descendants(root, "stackLevel")[0].text == "3"


@pytest.mark.parametrize(
    ("start_index", "page_size", "message"),
    ((-1, 2400, "start_index"), (0, 0, "page_size")),
)
def test_rejects_invalid_collection_page(
    start_index: int, page_size: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_collection_eval_request(
            "DefAlias",
            UI_ID,
            TARGET,
            "e1cRuntimeКонтекст.Таблица",
            UUID("33333333-3333-3333-3333-333333333333"),
            start_index=start_index,
            page_size=page_size,
        )


def test_builds_local_variables_request_for_selected_stack_level() -> None:
    result_id = UUID("99999999-9999-9999-9999-999999999999")

    root = ElementTree.fromstring(
        build_local_variables_request(
            "DefAlias", UI_ID, TARGET, stack_level=2, result_id=result_id
        )
    )

    assert descendants(root, "calcWaitingTime")[0].text == "25"
    assert descendants(root, "stackLevel")[0].text == "2"
    assert descendants(root, "expressionResultID")[0].text == str(result_id)
    assert descendants(root, "interfaces")[0].text == "context"
    assert descendants(root, "maxTextSize")[0].text == "307200"
    assert descendants(root, "stopOnFirstEOL")[0].text == "false"
    assert descendants(root, "calcItem") == []


def test_local_variables_request_can_bound_each_presentation() -> None:
    result_id = UUID("99999999-9999-9999-9999-999999999999")

    root = ElementTree.fromstring(
        build_local_variables_request(
            "DefAlias", UI_ID, TARGET, stack_level=1, result_id=result_id,
            max_text_size=512,
        )
    )

    assert descendants(root, "maxTextSize")[0].text == "512"


def test_parses_context_properties_as_frame_variables() -> None:
    result_id = UUID("99999999-9999-9999-9999-999999999999")
    payload = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <calculationResult xmlns="{CALC_NS}">
        <valueOfContextPropInfo><propInfo><propName>Документы</propName></propInfo>
          <valueInfo><typeName>Массив</typeName><collectionSize>4</collectionSize><pres>0JzQsNGB0YHQuNCy</pres></valueInfo>
        </valueOfContextPropInfo>
        <valueOfContextPropInfo><propInfo><propName>Результат</propName></propInfo>
          <valueInfo><typeName>Массив</typeName><pres>W10=</pres></valueInfo>
        </valueOfContextPropInfo>
      </calculationResult><errorOccurred>false</errorOccurred>
    </result></response>""".encode()

    result = parse_local_variables_result(payload)

    assert result.result_id == result_id
    assert [(item.name, item.type_name, item.presentation) for item in result.variables] == [
        ("Документы", "Массив", "Массив"),
        ("Результат", "Массив", "[]"),
    ]
    assert result.error_occurred is False
    assert [item.collection_size for item in result.variables] == [4, None]


def test_parses_collection_rows_with_typed_values_and_reference_presentation() -> None:
    result_id = UUID("99999999-9999-9999-9999-999999999999")
    employee = base64.b64encode("Иванов И.И.".encode()).decode()
    row_two = base64.b64encode("Петров П.П.".encode()).decode()
    payload = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>ТаблицаЗначений</typeName>
        <collectionSize>10000</collectionSize><pres>0KLQsNCx0LvQuNGG0LDQl9C90LDRh9C10L3QuNC5</pres>
      </resultValueInfo>
      <calculationResult xmlns="{CALC_NS}"><viewInterface>collection</viewInterface>
        <valueOfCollectionInfo><valueOfContextPropInfo>
          <propInfo><propName>Номер</propName></propInfo>
          <valueInfo><typeName>Число</typeName><valueDecimal>1</valueDecimal><pres>MQ==</pres></valueInfo>
        </valueOfContextPropInfo><valueOfContextPropInfo>
          <propInfo><propName>Сотрудник</propName></propInfo>
          <valueInfo><typeName>СправочникСсылка.Сотрудники</typeName><valueString></valueString><pres>{employee}</pres></valueInfo>
        </valueOfContextPropInfo></valueOfCollectionInfo>
        <valueOfCollectionInfo><valueOfContextPropInfo>
          <propInfo><propName>Номер</propName></propInfo>
          <valueInfo><typeName>Число</typeName><valueDecimal>2</valueDecimal><pres>Mg==</pres></valueInfo>
        </valueOfContextPropInfo><valueOfContextPropInfo>
          <propInfo><propName>Сотрудник</propName></propInfo>
          <valueInfo><typeName>СправочникСсылка.Сотрудники</typeName><valueString></valueString><pres>{row_two}</pres></valueInfo>
        </valueOfContextPropInfo></valueOfCollectionInfo>
      </calculationResult><errorOccurred>false</errorOccurred>
    </result></response>""".encode()

    result = parse_eval_result(payload)

    assert result.type_name == "ТаблицаЗначений"
    assert result.collection_size == 10000
    assert len(result.collection_rows) == 2
    assert result.collection_rows[0].cells[0].name == "Номер"
    assert result.collection_rows[0].cells[0].value_decimal == "1"
    assert result.collection_rows[0].cells[1].type_name == "СправочникСсылка.Сотрудники"
    assert result.collection_rows[0].cells[1].presentation == "Иванов И.И."
    assert result.collection_rows[0].cells[1].value_string == ""
    assert result.collection_rows[1].cells[1].presentation == "Петров П.П."


def test_parses_scalar_array_rows_without_named_property_metadata() -> None:
    result_id = UUID("99999999-9999-9999-9999-999999999998")
    first = base64.b64encode("first".encode()).decode()
    empty = base64.b64encode("".encode()).decode()
    payload = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>Массив</typeName>
        <collectionSize>2</collectionSize><pres>0JzQsNGB0YHQuNCy</pres>
      </resultValueInfo>
      <calculationResult xmlns="{CALC_NS}"><viewInterface>collection</viewInterface>
        <valueOfCollectionInfo><valueOfContextPropInfo>
          <valueInfo><typeName>Строка</typeName><valueString>{first}</valueString><pres>{first}</pres></valueInfo>
        </valueOfContextPropInfo></valueOfCollectionInfo>
        <valueOfCollectionInfo><valueOfContextPropInfo>
          <valueInfo><typeName>Строка</typeName><valueString>{empty}</valueString><pres>{empty}</pres></valueInfo>
        </valueOfContextPropInfo></valueOfCollectionInfo>
      </calculationResult><errorOccurred>false</errorOccurred>
    </result></response>""".encode()

    result = parse_eval_result(payload)

    assert result.collection_size == 2
    assert [row.index for row in result.collection_rows] == [0, 1]
    assert [row.cells[0].name for row in result.collection_rows] == ["Значение"] * 2
    assert [row.cells[0].value_string for row in result.collection_rows] == ["first", ""]


def test_parses_scalar_array_rows_with_direct_value_info() -> None:
    result_id = UUID("99999999-9999-9999-9999-999999999997")
    value = base64.b64encode("only".encode()).decode()
    payload = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>Массив</typeName>
        <collectionSize>1</collectionSize><pres>0JzQsNGB0YHQuNCy</pres>
      </resultValueInfo>
      <calculationResult xmlns="{CALC_NS}"><viewInterface>collection</viewInterface>
        <valueOfCollectionInfo><valueInfo><typeName>Строка</typeName>
          <valueString>{value}</valueString><pres>{value}</pres>
        </valueInfo></valueOfCollectionInfo>
      </calculationResult><errorOccurred>false</errorOccurred>
    </result></response>""".encode()

    result = parse_eval_result(payload)

    assert result.collection_rows[0].cells[0].name == "Значение"
    assert result.collection_rows[0].cells[0].value_string == "only"


def test_parses_alternate_local_variables_shape_from_ping() -> None:
    result_id = UUID("aaaaaaaa-9999-9999-9999-999999999999")
    payload = f"""<response xmlns="{RDBG_NS}"><result><cmdID>exprEvaluated</cmdID>
      <evalExprResBaseData><expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
        <localVariables xmlns="{CALC_NS}">
          <localVariableName>ШаблонЗапроса</localVariableName>
          <resultValueInfo><typeName>Строка</typeName><pres>0KLQtdC60YHRgg==</pres></resultValueInfo>
        </localVariables><errorOccurred>false</errorOccurred>
      </evalExprResBaseData></result></response>""".encode()

    results = parse_ping_local_variables(payload)

    assert len(results) == 1
    assert results[0].result_id == result_id
    assert results[0].variables[0].name == "ШаблонЗапроса"
    assert results[0].variables[0].type_name == "Строка"
    assert results[0].variables[0].presentation == "Текст"


def test_parses_file_mode_target_stop_stack_and_evaluation() -> None:
    targets_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <response xmlns="{RDBG_NS}"><result>success</result><item>
      <targetIDStr>target</targetIDStr><targetID xmlns="{BASE_NS}">
        <id>{TARGET_UUID}</id><infoBaseAlias>DefAlias</infoBaseAlias>
        <seanceId>aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa</seanceId>
        <seanceNo>12</seanceNo>
        <infoBaseInstanceID>bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb</infoBaseInstanceID>
        <configVersion>2c7765a4ea462c49a0a8db8fcc60b2ea00000000</configVersion>
        <targetType>ServerEmulation</targetType></targetID>
      <stateNum>1</stateNum><state>stopped</state>
    </item></response>""".encode()
    stack_xml = f"""<response xmlns="{RDBG_NS}"><result>success</result><callStack>
      <moduleID xmlns="{BASE_NS}"><type>ExtMDModule</type><URL>{LOCATION.url}</URL>
      <objectID>{OBJECT_UUID}</objectID><propertyID>{PROPERTY_UUID}</propertyID><extId>1</extId></moduleID>
      <lineNo>14</lineNo></callStack></response>""".encode()
    ping_xml = f"""<response xmlns="{RDBG_NS}"><result><cmdID>callStackFormed</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_UUID}</id><infoBaseAlias>DefAlias</infoBaseAlias>
      <seanceId>aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa</seanceId><seanceNo>12</seanceNo>
      <infoBaseInstanceID>bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb</infoBaseInstanceID>
      <configVersion>2c7765a4ea462c49a0a8db8fcc60b2ea00000000</configVersion></targetID>
      <callStack><moduleID><type>ExtMDModule</type><URL>{LOCATION.url}</URL>
      <objectID>{OBJECT_UUID}</objectID><propertyID>{PROPERTY_UUID}</propertyID><extId>1</extId></moduleID>
      <lineNo>14</lineNo></callStack></result></response>""".encode()
    eval_xml = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID>33333333-3333-3333-3333-333333333333</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>Число</typeName><pres>NDI=</pres></resultValueInfo>
      <errorOccurred>false</errorOccurred></result></response>""".encode()

    targets = parse_targets(targets_xml)
    stack = parse_call_stack(stack_xml, TARGET)
    events = parse_ping_events(ping_xml)
    evaluation = parse_eval_result(eval_xml)

    assert targets[0].target_id.id == TARGET.id
    assert targets[0].target_id.infobase_alias == TARGET.infobase_alias
    assert targets[0].target_id.seance_id == UUID(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    assert targets[0].target_id.seance_no == 12
    assert targets[0].target_id.infobase_instance_id == UUID(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    )
    assert (
        targets[0].target_id.config_version
        == "2c7765a4ea462c49a0a8db8fcc60b2ea00000000"
    )
    assert targets[0].target_type == "ServerEmulation"
    assert targets[0].state == "stopped"
    assert stack[0].location == LOCATION
    assert events[0].target_id == targets[0].target_id
    assert events[0].location == LOCATION
    assert evaluation.result_id == UUID("33333333-3333-3333-3333-333333333333")
    assert evaluation.type_name == "Число"
    assert evaluation.presentation == "42"


def test_call_stack_keeps_physical_levels_when_protocol_frame_is_unaddressable() -> None:
    payload = f"""<response xmlns="{RDBG_NS}"><result>success</result>
      <callStack><moduleID><objectID>{OBJECT_UUID}</objectID>
      <propertyID>{PROPERTY_UUID}</propertyID></moduleID><lineNo>14</lineNo></callStack>
      <callStack><moduleID/><lineNo>15</lineNo></callStack>
      <callStack><moduleID><type>{CAPTURE_A.module_type}</type><extensionName>{CAPTURE_A.extension_name}</extensionName><objectID>{CAPTURE_A.object_id}</objectID>
      <propertyID>{CAPTURE_A.property_id}</propertyID></moduleID><lineNo>{CAPTURE_A.line}</lineNo></callStack>
      </response>""".encode()

    frames = parse_call_stack(payload, TARGET)

    assert [frame.level for frame in frames] == [0, 2]
    assert frames[0].location == CAPTURE_A
    assert frames[1].location.line == 14


def test_rejects_missing_required_target_fields() -> None:
    malformed = f'<response xmlns="{RDBG_NS}"><item><state>stopped</state></item></response>'.encode()

    with pytest.raises(ProtocolError, match="targetID"):
        parse_targets(malformed)


def test_parses_extension_stop_without_url() -> None:
    location = ModuleLocation(
        module_type="ExtensionModule",
        url="",
        object_id=OBJECT_UUID,
        property_id=PROPERTY_UUID,
        line=16,
        extension_name="OnecInteractiveRuntime",
    )
    payload = f"""<response xmlns="{RDBG_NS}"><result><cmdID>callStackFormed</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_UUID}</id><infoBaseAlias>DefAlias</infoBaseAlias></targetID>
      <callStack><moduleID><type>ExtensionModule</type><extensionName>OnecInteractiveRuntime</extensionName>
      <objectID>{OBJECT_UUID}</objectID><propertyID>{PROPERTY_UUID}</propertyID></moduleID>
      <lineNo>16</lineNo></callStack></result></response>""".encode()

    assert parse_ping_events(payload)[0].location == location


def test_parses_config_module_stop_when_platform_omits_type_and_url() -> None:
    location = ModuleLocation(
        module_type="ConfigModule",
        url="",
        object_id=OBJECT_UUID,
        property_id=PROPERTY_UUID,
        line=875,
    )
    payload = f"""<response xmlns="{RDBG_NS}"><result><cmdID>callStackFormed</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_UUID}</id><infoBaseAlias>DefAlias</infoBaseAlias></targetID>
      <callStack><moduleID><objectID>{OBJECT_UUID}</objectID>
      <propertyID>{PROPERTY_UUID}</propertyID></moduleID><lineNo>875</lineNo></callStack>
      </result></response>""".encode()

    assert parse_ping_events(payload)[0].location == location


def test_uses_last_protocol_stack_item_as_current_frame() -> None:
    caller_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    payload = f"""<response xmlns="{RDBG_NS}"><result><cmdID>callStackFormed</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_UUID}</id><infoBaseAlias>DefAlias</infoBaseAlias></targetID>
      <callStack><moduleID><objectID>{caller_id}</objectID>
      <propertyID>{PROPERTY_UUID}</propertyID></moduleID><lineNo>374</lineNo></callStack>
      <callStack><moduleID><objectID>{OBJECT_UUID}</objectID>
      <propertyID>{PROPERTY_UUID}</propertyID></moduleID><lineNo>875</lineNo></callStack>
      </result></response>""".encode()

    event = parse_ping_events(payload)[0]

    assert event.location.object_id == OBJECT_UUID
    assert event.location.line == 875


def test_parses_raw_stop_flags_and_complete_stack() -> None:
    caller_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    payload = f"""<response xmlns="{RDBG_NS}"><result><cmdID>callStackFormed</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_UUID}</id><infoBaseAlias>DefAlias</infoBaseAlias></targetID>
      <stopByBP>true</stopByBP><suspendedByOther>false</suspendedByOther>
      <callStack><moduleID><objectID>{caller_id}</objectID>
      <propertyID>{PROPERTY_UUID}</propertyID></moduleID><lineNo>374</lineNo></callStack>
      <callStack><moduleID><type>ExtensionModule</type><extensionName>OnecInteractiveRuntime</extensionName>
      <objectID>{CAPTURE_A.object_id}</objectID><propertyID>{CAPTURE_A.property_id}</propertyID>
      </moduleID><lineNo>{CAPTURE_A.line}</lineNo></callStack>
      </result></response>""".encode()

    event = parse_ping_events(payload)[0]

    assert event.location == CAPTURE_A
    assert event.stop_by_breakpoint is True
    assert event.suspended_by_other is False
    assert event.stack[0] == CAPTURE_A
    assert event.stack[1].object_id == caller_id
    assert event.stack[1].line == 374


def test_skips_unaddressable_protocol_stack_frame() -> None:
    payload = f"""<response xmlns="{RDBG_NS}"><result><cmdID>callStackFormed</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_UUID}</id><infoBaseAlias>DefAlias</infoBaseAlias></targetID>
      <stopByBP>true</stopByBP><suspendedByOther>false</suspendedByOther>
      <callStack><moduleID><type>ExtensionModule</type><extensionName>OnecInteractiveRuntime</extensionName>
      <objectID>{CAPTURE_B.object_id}</objectID><propertyID>{CAPTURE_B.property_id}</propertyID>
      </moduleID><lineNo>{CAPTURE_B.line}</lineNo></callStack>
      <callStack><moduleID/><lineNo>1</lineNo></callStack>
      <callStack><moduleID><type>ExtensionModule</type><extensionName>OnecInteractiveRuntime</extensionName>
      <objectID>{CAPTURE_A.object_id}</objectID><propertyID>{CAPTURE_A.property_id}</propertyID>
      </moduleID><lineNo>{CAPTURE_A.line}</lineNo></callStack>
      </result></response>""".encode()

    event = parse_ping_events(payload)[0]

    assert event.location == CAPTURE_A
    assert event.stack == (CAPTURE_A, CAPTURE_B)
    assert [frame.level for frame in event.stack_frames] == [0, 2]


def test_parses_runtime_error_from_stop_event() -> None:
    encoded_error = base64.b64encode("planned-runtime-error".encode()).decode()
    payload = f"""<response xmlns="{RDBG_NS}"><result><cmdID>callStackFormed</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_UUID}</id><infoBaseAlias>DefAlias</infoBaseAlias></targetID>
      <stopByBP>false</stopByBP><suspendedByOther>false</suspendedByOther>
      <exceptionStr xmlns="{CALC_NS}">{encoded_error}</exceptionStr>
      <callStack><moduleID><type>ExtMDModule</type><URL>{LOCATION.url}</URL>
      <objectID>{LOCATION.object_id}</objectID><propertyID>{LOCATION.property_id}</propertyID>
      <extId>{LOCATION.ext_id}</extId></moduleID><lineNo>{LOCATION.line}</lineNo></callStack>
      </result></response>""".encode()

    event = parse_ping_events(payload)[0]

    assert event.runtime_error == "planned-runtime-error"


def test_parses_target_started_event() -> None:
    payload = f"""<response xmlns="{RDBG_NS}"><result><cmdID>targetStarted</cmdID>
      <targetID xmlns="{BASE_NS}"><id>{TARGET_UUID}</id><infoBaseAlias>DefAlias</infoBaseAlias>
      <targetType>ManagedClient</targetType></targetID></result></response>""".encode()

    targets = parse_ping_target_events(payload)

    assert targets == [DebugTarget(TARGET, "ManagedClient", "Started")]


def test_parses_undefined_evaluation_without_presentation() -> None:
    result_id = UUID("44444444-4444-4444-4444-444444444444")
    payload = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>Неопределено</typeName></resultValueInfo>
    </result></response>""".encode()

    result = parse_eval_result(payload)

    assert result.presentation == "Неопределено"
    assert result.type_name == "Неопределено"


def test_parses_evaluation_error_without_type_name() -> None:
    result_id = UUID("66666666-6666-6666-6666-666666666666")
    error_text = "Переменная не определена (НесуществующийМодуль)"
    encoded_error = base64.b64encode(error_text.encode()).decode()
    payload = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><presProcessedCorrectly>false</presProcessedCorrectly></resultValueInfo>
      <errorOccurred xmlns="{CALC_NS}">true</errorOccurred>
      <exceptionStr xmlns="{CALC_NS}">{encoded_error}</exceptionStr></result></response>""".encode()

    result = parse_eval_result(payload)

    assert result.error_occurred is True
    assert result.error_text == error_text
    assert result.type_name == "Ошибка"
    assert result.presentation == error_text


def test_parses_wrapped_base64_presentation() -> None:
    result_id = UUID("55555555-5555-5555-5555-555555555555")
    payload = f"""<response xmlns="{RDBG_NS}"><result>
      <expressionResultID xmlns="{CALC_NS}">{result_id}</expressionResultID>
      <resultValueInfo xmlns="{CALC_NS}"><typeName>Строка</typeName>
      <pres>0J/RgNC40LLQtdGC
      LCDQvNC40YA=</pres></resultValueInfo>
      <errorOccurred>false</errorOccurred></result></response>""".encode()

    result = parse_eval_result(payload)

    assert result.presentation == "Привет, мир"
