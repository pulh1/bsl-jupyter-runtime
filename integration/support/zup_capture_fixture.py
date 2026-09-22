"""Current ZUP capture locator and live-acceptance BSL fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from uuid import UUID
from xml.etree import ElementTree

from onec_runtime.errors import ProtocolError
from onec_runtime.kernel import COMMON_MODULE_PROPERTY_ID
from onec_runtime.rdbg.models import ModuleLocation


_MODULE_NAME = "ВедомостьНаВыплатуЗарплаты"
_OUTER_DECLARATION = re.compile(r"^Функция\s+ЗарплатаКВыплате\s*\(", re.IGNORECASE)
_HELPER_DECLARATION = re.compile(
    r"^Процедура\s+СоздатьВТЗарплатаКВыплате\s*\(", re.IGNORECASE
)
_PRIMARY_CALL = "взаиморасчетыссотрудниками.создатьвтзарплатаквыплате("
_LIMIT_CALL = (
    "взаиморасчетыссотрудниками."
    "создатьвтзарплатаквыплатеограниченнаясальдофизлиц("
)
_OUTER_CALL = "создатьвтзарплатаквыплате("
_POST_CASCADE = "описательвременныхтаблиц ="


@dataclass(frozen=True, slots=True)
class ZupPayrollCapturePoints:
    module_name: str
    module_path: Path
    module_uuid: UUID
    source_sha256: str
    before_limit: ModuleLocation
    after_cascade: ModuleLocation


@dataclass(frozen=True, slots=True)
class _SourceLine:
    number: int
    text: str


def _code_lines(source: str) -> tuple[_SourceLine, ...]:
    result: list[_SourceLine] = []
    for number, raw in enumerate(source.splitlines(), start=1):
        text = raw.strip()
        if not text or text.startswith("//") or text.startswith("#"):
            continue
        result.append(_SourceLine(number, text))
    return tuple(result)


def _method_region(
    lines: tuple[_SourceLine, ...],
    declaration: re.Pattern[str],
    terminator: str,
    label: str,
) -> tuple[_SourceLine, ...]:
    starts = [index for index, line in enumerate(lines) if declaration.match(line.text)]
    if len(starts) != 1:
        raise ProtocolError(f"ZUP source must contain exactly one {label}")
    start = starts[0]
    end_matches = [
        index
        for index in range(start + 1, len(lines))
        if lines[index].text.casefold() == terminator.casefold()
    ]
    if not end_matches:
        raise ProtocolError(f"ZUP source {label} has no {terminator}")
    return lines[start : end_matches[0] + 1]


def _unique_call(
    region: tuple[_SourceLine, ...], needle: str, label: str
) -> _SourceLine:
    matches = [line for line in region if needle in line.text.casefold()]
    if len(matches) != 1:
        raise ProtocolError(f"Expected exactly one {label}")
    return matches[0]


def _next_statement_after_call(
    region: tuple[_SourceLine, ...], call: _SourceLine, label: str
) -> _SourceLine:
    call_index = region.index(call)
    statement_end: int | None = None
    for index in range(call_index, len(region)):
        if ";" in region[index].text:
            statement_end = index
            break
    if statement_end is None or statement_end + 1 >= len(region):
        raise ProtocolError(f"ZUP source {label} is not a complete call statement")
    return region[statement_end + 1]


def _module_uuid(metadata_path: Path) -> UUID:
    try:
        root = ElementTree.parse(metadata_path).getroot()
        candidates = [
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "CommonModule"
        ]
        owner = root if "uuid" in root.attrib else candidates[0]
        value = owner.attrib["uuid"]
        return UUID(value)
    except (OSError, ElementTree.ParseError, IndexError, KeyError, ValueError) as error:
        raise ProtocolError("ZUP common module metadata UUID is invalid") from error


def locate_payroll_capture_points(zup_source_root: Path) -> ZupPayrollCapturePoints:
    common_modules = zup_source_root.resolve() / "CommonModules"
    module_root = common_modules / _MODULE_NAME
    edt_metadata = module_root / f"{_MODULE_NAME}.mdo"
    edt_module = module_root / "Module.bsl"
    designer_metadata = common_modules / f"{_MODULE_NAME}.xml"
    designer_module = module_root / "Ext" / "Module.bsl"
    if edt_metadata.is_file() and edt_module.is_file():
        metadata_path, module_path = edt_metadata, edt_module
    else:
        metadata_path, module_path = designer_metadata, designer_module
    module_uuid = _module_uuid(metadata_path)
    try:
        source_bytes = module_path.read_bytes()
        source = source_bytes.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise ProtocolError("ZUP payroll common module source is invalid") from error
    lines = _code_lines(source)
    outer = _method_region(
        lines, _OUTER_DECLARATION, "КонецФункции", "payroll function"
    )
    helper = _method_region(
        lines, _HELPER_DECLARATION, "КонецПроцедуры", "payroll VT helper"
    )

    primary = _unique_call(helper, _PRIMARY_CALL, "primary payroll temporary-table call")
    limiter = _unique_call(helper, _LIMIT_CALL, "payroll debt-limit call")
    if helper.index(primary) >= helper.index(limiter):
        raise ProtocolError("Payroll temporary-table calls are in the wrong order")

    outer_call = _unique_call(outer, _OUTER_CALL, "outer payroll cascade call")
    post_cascade = _next_statement_after_call(outer, outer_call, "outer payroll cascade")
    if not post_cascade.text.casefold().startswith(_POST_CASCADE):
        raise ProtocolError("Expected exactly one post-cascade assignment")

    def location(line: int) -> ModuleLocation:
        return ModuleLocation(
            module_type="ConfigModule",
            url="",
            object_id=module_uuid,
            property_id=UUID(COMMON_MODULE_PROPERTY_ID),
            line=line,
        )

    return ZupPayrollCapturePoints(
        module_name=_MODULE_NAME,
        module_path=module_path,
        module_uuid=module_uuid,
        source_sha256=sha256(source_bytes).hexdigest(),
        before_limit=location(limiter.number),
        after_cascade=location(post_cascade.number),
    )


def _payroll_discovery_block(document_type: str, label: str) -> str:
    return f'''Если НЕ НайденаВедомостьДемо Тогда
    ЗапросВедомостейДемо = Новый Запрос;
    ЗапросВедомостейДемо.Текст =
    "ВЫБРАТЬ ПЕРВЫЕ 20
    |    Ведомости.Ссылка КАК Ссылка
    |ИЗ
    |    Документ.{document_type} КАК Ведомости
    |ГДЕ
    |    НЕ Ведомости.ПометкаУдаления
    |УПОРЯДОЧИТЬ ПО
    |    Ведомости.Дата УБЫВ";
    НачалоЗапросаВедомостиДемо = ТекущаяУниверсальнаяДатаВМиллисекундах();
    ВыборкаВедомостейДемо = ЗапросВедомостейДемо.Выполнить().Выбрать();
    ВремяЗапросовВедомостиДемо = ВремяЗапросовВедомостиДемо +
        ТекущаяУниверсальнаяДатаВМиллисекундах() - НачалоЗапросаВедомостиДемо;
    Пока ВыборкаВедомостейДемо.Следующий() Цикл
        Если НайденаВедомостьДемо Тогда
            Прервать;
        КонецЕсли;
        ПровереноВедомостейДемо = ПровереноВедомостейДемо + 1;
        НачалоПодготовкиВедомостиДемо = ТекущаяУниверсальнаяДатаВМиллисекундах();
        Попытка
            СсылкаВедомостиДемо = ВыборкаВедомостейДемо.Ссылка;
            ОбъектВедомостиДемо = СсылкаВедомостиДемо.ПолучитьОбъект();
            МенеджерВедомостиДемо = ОбщегоНазначения.МенеджерОбъектаПоСсылке(СсылкаВедомостиДемо);
            ПараметрыЗаполненияДемо = МенеджерВедомостиДемо.ПараметрыЗаполненияПоОбъекту(ОбъектВедомостиДемо);
            ВремяПодготовкиВедомостиДемо = ВремяПодготовкиВедомостиДемо
                + ТекущаяУниверсальнаяДатаВМиллисекундах() - НачалоПодготовкиВедомостиДемо;
        Исключение
            ВремяПодготовкиВедомостиДемо = ВремяПодготовкиВедомостиДемо
                + ТекущаяУниверсальнаяДатаВМиллисекундах() - НачалоПодготовкиВедомостиДемо;
            ПоследняяОшибкаВедомостиДемо = ОписаниеОшибки();
            Продолжить;
        КонецПопытки;
        НачалоРасчетаВедомостиДемо = ТекущаяУниверсальнаяДатаВМиллисекундах();
        Попытка
            ЗарплатаBaselineДемо = ВедомостьНаВыплатуЗарплаты.ЗарплатаКВыплате(
                ПараметрыЗаполненияДемо.ОписаниеОперации,
                ПараметрыЗаполненияДемо.ОтборСотрудников,
                ПараметрыЗаполненияДемо.ПараметрыРасчетаЗарплаты,
                ПараметрыЗаполненияДемо.Финансирование,
                СсылкаВедомостиДемо);
            ВремяРасчетаВедомостиДемо = ВремяРасчетаВедомостиДемо
                + ТекущаяУниверсальнаяДатаВМиллисекундах() - НачалоРасчетаВедомостиДемо;
            Если ЗарплатаBaselineДемо.Количество() > 0 Тогда
                НайденаВедомостьДемо = Истина;
                ТипВедомостиДемо = "{label}";
                ПредставлениеВедомостиДемо = Строка(СсылкаВедомостиДемо);
            КонецЕсли;
        Исключение
            ВремяРасчетаВедомостиДемо = ВремяРасчетаВедомостиДемо
                + ТекущаяУниверсальнаяДатаВМиллисекундах() - НачалоРасчетаВедомостиДемо;
            ПоследняяОшибкаВедомостиДемо = ОписаниеОшибки();
        КонецПопытки;
    КонецЦикла;
КонецЕсли;'''


PAYROLL_DISCOVERY_SOURCE = "\n".join(
    (
        '''НачалоПоискаВедомостиДемо = ТекущаяУниверсальнаяДатаВМиллисекундах();
ВремяЗапросовВедомостиДемо = 0;
ВремяПодготовкиВедомостиДемо = 0;
ВремяРасчетаВедомостиДемо = 0;
ПровереноВедомостейДемо = 0;
НайденаВедомостьДемо = Ложь;
ПоследняяОшибкаВедомостиДемо = "";''',
        _payroll_discovery_block("ВедомостьНаВыплатуЗарплатыВБанк", "ВБанк"),
        _payroll_discovery_block("ВедомостьНаВыплатуЗарплатыВКассу", "ВКассу"),
        _payroll_discovery_block(
            "ВедомостьНаВыплатуЗарплатыПеречислением", "Перечислением"
        ),
        _payroll_discovery_block(
            "ВедомостьНаВыплатуЗарплатыРаздатчиком", "Раздатчиком"
        ),
        '''Если НЕ НайденаВедомостьДемо Тогда
    ВызватьИсключение "Не найдена непустая ведомость: " + Лев(ПоследняяОшибкаВедомостиДемо, 500);
КонецЕсли;
ВремяВсегоВедомостиДемо = ТекущаяУниверсальнаяДатаВМиллисекундах() - НачалоПоискаВедомостиДемо;
ВремяПрочееВедомостиДемо = ВремяВсегоВедомостиДемо - ВремяЗапросовВедомостиДемо
    - ВремяПодготовкиВедомостиДемо - ВремяРасчетаВедомостиДемо;
Сообщить("PROFILE payroll-discovery: total_ms=" + ВремяВсегоВедомостиДемо
    + "; query_ms=" + ВремяЗапросовВедомостиДемо
    + "; prepare_ms=" + ВремяПодготовкиВедомостиДемо
    + "; calculate_ms=" + ВремяРасчетаВедомостиДемо
    + "; other_ms=" + ВремяПрочееВедомостиДемо
    + "; documents=" + ПровереноВедомостейДемо);
Сообщить("Ведомость " + ТипВедомостиДемо + ": " + ПредставлениеВедомостиДемо
    + ", строк=" + ЗарплатаBaselineДемо.Количество());
Результат = ЗарплатаBaselineДемо.Количество();''',
    )
)


PAYROLL_MAIN_SOURCE = '''ЗарплатаCaptureДемо = ВедомостьНаВыплатуЗарплаты.ЗарплатаКВыплате(
    ПараметрыЗаполненияДемо.ОписаниеОперации,
    ПараметрыЗаполненияДемо.ОтборСотрудников,
    ПараметрыЗаполненияДемо.ПараметрыРасчетаЗарплаты,
    ПараметрыЗаполненияДемо.Финансирование,
    СсылкаВедомостиДемо);
Сообщить("MAIN после CAPTURE: " + ЗарплатаCaptureДемо.Количество() + " строк");
Результат = ЗарплатаCaptureДемо.Количество();'''


CAPTURE_A_SNAPSHOT_SOURCE = '''ЗапросВТЗарплатаДемо = Новый Запрос;
ЗапросВТЗарплатаДемо.МенеджерВременныхТаблиц = КонтекстОтладки.МенеджерВременныхТаблиц;
ЗапросВТЗарплатаДемо.Текст =
"ВЫБРАТЬ
|    Зарплата.*
|ИЗ
|    ВТЗарплатаКВыплате КАК Зарплата";
ДемоВТДо = ЗапросВТЗарплатаДемо.Выполнить().Выгрузить();
Сообщить("CAPTURE A: " + ДемоВТДо.Количество() + " строк");
РезультатИнструкции = ДемоВТДо.Количество();'''
