from __future__ import annotations


MAIN_STATEMENT_SOURCE = '''Счетчик = 5;
Вложенные = Новый Структура;
Числа = Новый Массив;
Числа.Добавить(3);
Числа.Добавить(5);
Вложенные.Вставить("Числа", Числа);
Вложенные.Вставить("Пусто", Неопределено);
Вложенные.Вставить("Флаг", Истина);
Вложенные.Вставить("Момент", Дата(2026, 8, 27, 12, 30, 0));
ТаблицаДанных = Новый ТаблицаЗначений;
ТаблицаДанных.Колонки.Добавить("Код");
ТаблицаДанных.Колонки.Добавить("Сумма");
СтрокаДанных = ТаблицаДанных.Добавить();
СтрокаДанных.Код = "A";
СтрокаДанных.Сумма = 10;
СтрокаДанных = ТаблицаДанных.Добавить();
СтрокаДанных.Код = "B";
СтрокаДанных.Сумма = 20;
СтрокаДанных = ТаблицаДанных.Добавить();
СтрокаДанных.Код = "C";
СтрокаДанных.Сумма = 30;
СостояниеМетода = Новый Структура("Результат", 0);
СостояниеMixed = Новый Структура("Результат", 0);
Сообщить("main-ready");
Результат = Счетчик;'''


MAIN_METHOD_SOURCE = '''Процедура FixtureMainWorker(Состояние, Значение)
    Состояние.Результат = Значение * 2;
КонецПроцедуры'''


MAIN_METHOD_CALL_SOURCE = '''FixtureMainWorker(СостояниеМетода, 6);
MainMethodResult = СостояниеМетода.Результат;
Результат = MainMethodResult;'''


MAIN_MIXED_SOURCE = '''Процедура FixtureMixedMain(Состояние, Значение)
    Состояние.Результат = Значение * 3;
КонецПроцедуры;
FixtureMixedMain(СостояниеMixed, 7);
MainMixedResult = СостояниеMixed.Результат;
Результат = MainMixedResult;'''


MAIN_ERROR_SOURCE = '''Счетчик = 999;
ВызватьИсключение "fixture-main-error";'''


MAIN_RECOVERY_SOURCE = '''Сообщить("main-recovery:" + Счетчик + ":" + MainMethodResult + ":" + MainMixedResult);
Результат = "" + Счетчик + "|" + MainMethodResult + "|" + MainMixedResult;'''


CAPTURE_MAIN_SOURCE = '''РезультатFixture = JupyterBslFixtureCallerServer.ВыполнитьСценарий();
Результат = РезультатFixture;'''


CAPTURE_SNAPSHOT_SOURCE = '''CaptureCounter = КонтекстОтладки.ЛокальныйСчетчик;
CaptureNested = КонтекстОтладки.ЛокальнаяСтруктура;
CaptureTable = КонтекстОтладки.ЛокальнаяТаблица;
CaptureWorkerState = Новый Структура("Результат", 0);
РезультатИнструкции = CaptureCounter;'''


CAPTURE_METHOD_SOURCE = '''Процедура FixtureCaptureWorker(Состояние, Значение)
    Состояние.Результат = Значение + 5;
КонецПроцедуры'''


CAPTURE_METHOD_CALL_SOURCE = '''FixtureCaptureWorker(CaptureWorkerState, КонтекстОтладки.ЛокальныйСчетчик);
CaptureMethodResult = CaptureWorkerState.Результат;
РезультатИнструкции = CaptureMethodResult;'''


CAPTURE_MIXED_SOURCE = '''Процедура FixtureMixedCapture(Состояние, Значение)
    Состояние.Результат = Значение * 2;
КонецПроцедуры;
CaptureMixedState = Новый Структура("Результат", 0);
FixtureMixedCapture(CaptureMixedState, КонтекстОтладки.ЛокальныйСчетчик);
КонтекстОтладки.ЛокальныйMixed = CaptureMixedState.Результат;
CaptureMixedResult = КонтекстОтладки.ЛокальныйMixed;
РезультатИнструкции = CaptureMixedResult;'''


CAPTURE_MIXED_ERROR_SOURCE = '''Процедура FixtureMixedCaptureAfterError(Состояние, Значение)
    Состояние.Результат = Значение + 1;
КонецПроцедуры;
КонтекстОтладки.ЛокальныйСчетчик = 901;
ОшибкаMixedCapture = 1 / 0;'''


CAPTURE_MIXED_ERROR_RECOVERY_SOURCE = '''CaptureMixedErrorState = Новый Структура("Результат", 0);
FixtureMixedCaptureAfterError(CaptureMixedErrorState, КонтекстОтладки.ЛокальныйСчетчик);
CaptureMixedErrorRecovery = CaptureMixedErrorState.Результат;
РезультатИнструкции = CaptureMixedErrorRecovery;'''


CAPTURE_ERROR_SOURCE = '''КонтекстОтладки.ЛокальныйСчетчик = 900;
ВызватьИсключение "fixture-capture-error";'''


CAPTURE_ERROR_RECOVERY_SOURCE = '''Сообщить("capture-recovery:" + КонтекстОтладки.ЛокальныйСчетчик);
РезультатИнструкции = КонтекстОтладки.ЛокальныйСчетчик;'''


CAPTURE_WRITE_SOURCE = '''КонтекстОтладки.ЛокальныйСчетчик = 40;
КонтекстОтладки.ЛокальнаяСтруктура.Метка = "после";
РезультатИнструкции = КонтекстОтладки.ЛокальныйСчетчик;'''


CAPTURE_STACK_SOURCE = '''StackValue = КонтекстОтладки.РезультатПодчиненного.Счетчик;
StackMarker = КонтекстОтладки.РезультатПодчиненного.Метка;
StackMixed = КонтекстОтладки.РезультатПодчиненного.Mixed;
Сообщить("capture-stack:" + StackMarker + ":mixed=" + StackMixed);
РезультатИнструкции = StackValue;'''


def visible_bsl_sources() -> tuple[str, ...]:
    return (
        MAIN_STATEMENT_SOURCE,
        MAIN_METHOD_SOURCE,
        MAIN_METHOD_CALL_SOURCE,
        MAIN_MIXED_SOURCE,
        MAIN_ERROR_SOURCE,
        MAIN_RECOVERY_SOURCE,
        CAPTURE_MAIN_SOURCE,
        CAPTURE_SNAPSHOT_SOURCE,
        CAPTURE_METHOD_SOURCE,
        CAPTURE_METHOD_CALL_SOURCE,
        CAPTURE_MIXED_SOURCE,
        CAPTURE_MIXED_ERROR_SOURCE,
        CAPTURE_MIXED_ERROR_RECOVERY_SOURCE,
        CAPTURE_ERROR_SOURCE,
        CAPTURE_ERROR_RECOVERY_SOURCE,
        CAPTURE_WRITE_SOURCE,
        CAPTURE_STACK_SOURCE,
    )

