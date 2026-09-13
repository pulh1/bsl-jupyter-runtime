"""Build the two reader demo notebooks from reviewable cell sources."""
from pathlib import Path
import nbformat as nbf

ROOT = Path(__file__).resolve().parents[1]
DEMO_DEST = ROOT / 'notebooks' / 'demo'

OVERVIEW_SETUP = '''from IPython.display import display
from onec_runtime.config import RuntimeConfig
from onec_runtime.session import ExtensionMode, RuntimeSessionConfig
from onec_runtime_jupyter import InteractiveRuntimeSession

PLATFORM_BIN = r'C:\\Program Files\\1cv8\\8.5.1.1529\\bin'
CONNECTION_STRING = r'File="C:\\demo\\ЗУП";'
SOURCE_ROOT = r'C:\\exports\\ЗУП'
EXTENSION_MODE = ExtensionMode.AUTO  # MANUAL для ИБ с заранее установленным расширением.

runtime = InteractiveRuntimeSession.start(
    RuntimeSessionConfig(
        runtime=RuntimeConfig(
            platform_bin=PLATFORM_BIN,
            connection_string=CONNECTION_STRING,
            username='Савинская З.Ю. (Системный программист)',
        ),
        source_root=SOURCE_ROOT,
        extension_mode=EXTENSION_MODE,
    )
)'''

OVERVIEW_EMPLOYEES = '''ДатаФОТ = Дата(2021, 8, 1);
ПараметрыСотрудников = КадровыйУчет.ПараметрыПолученияСотрудниковОрганизацийПоСпискуФизическихЛиц();
ПараметрыСотрудников.НачалоПериода = ДатаФОТ;
ПараметрыСотрудников.ОкончаниеПериода = ДатаФОТ;
ПараметрыСотрудников.РаботникиПоТрудовымДоговорам = Истина;

СписокСотрудников = КадровыйУчет.СотрудникиОрганизации(
    Истина, ПараметрыСотрудников).Скопировать(, "Сотрудник");
СписокСотрудников.Свернуть("Сотрудник");
СписокСотрудников.Колонки.Добавить("Период", Новый ОписаниеТипов("Дата"));
Для Каждого СтрокаСотрудника Из СписокСотрудников Цикл
    СтрокаСотрудника.Период = ДатаФОТ;
КонецЦикла;'''

OVERVIEW_PLAN = '''ПлановыйФот = ПлановыеНачисленияСотрудников.ТекущиеДанныеОплатыТрудаСотрудников(
    Неопределено, СписокСотрудников);
ПланФОТ = ПлановыйФот.Скопировать(, "Сотрудник,ФОТ");'''

CAPTURE_ARM = '''from pathlib import Path

MODULE_PATH = r'CommonModules\\КадровыйУчет\\Ext\\Module.bsl'
lines = (Path(SOURCE_ROOT) / MODULE_PATH).read_text(encoding='utf-8-sig').splitlines()
starts = [index for index, line in enumerate(lines)
          if line.startswith('Функция КадровыеДанныеСотрудников(')]
if len(starts) != 1:
    raise ValueError('Проверьте выгрузку: метод КадровыеДанныеСотрудников не найден однозначно')
ends = [index for index in range(starts[0] + 1, len(lines))
        if lines[index].startswith('КонецФункции')]
if not ends:
    raise ValueError('Проверьте выгрузку: конец метода не найден')
end = ends[0]

def line_of(statement):
    found = [index + 1 for index in range(starts[0], end)
             if lines[index].strip() == statement]
    if len(found) != 1:
        raise ValueError(f'Проверьте исходный код метода: {statement}')
    return found[0]

CAPTURE_LINE_A = line_of('КадровыеДанныеСотрудников = Запрос.Выполнить().Выгрузить();')
CAPTURE_LINE_B = line_of('Возврат КадровыеДанныеСотрудников;')
runtime.add_capture_point(MODULE_PATH, CAPTURE_LINE_A)
runtime.add_capture_point(MODULE_PATH, CAPTURE_LINE_B)
print('Точки A/B:', CAPTURE_LINE_A, CAPTURE_LINE_B)'''

CAPTURE_CALL = '''ПланПовтор = КадровыйУчет.КадровыеДанныеСотрудников(
    Истина, СотрудникиДемо,
    "ФОТ,Подразделение,ГоловнаяОрганизация,Организация", ДатаФОТ);
Результат = ПланПовтор.Итог("ФОТ");'''

OVERVIEW_CAPTURE_CALL = '''ПланПовтор = ПлановыеНачисленияСотрудников.ТекущиеДанныеОплатыТрудаСотрудников(
    Неопределено, СписокСотрудников);'''

CAPTURE_READ = '''ЗапросДемоВТ = Новый Запрос;
ЗапросДемоВТ.МенеджерВременныхТаблиц = КонтекстОтладки.Запрос.МенеджерВременныхТаблиц;
ЗапросДемоВТ.Текст = "ВЫБРАТЬ Сотрудник, Организация, ФОТ ИЗ ВТКадровыеДанныеСотрудников";
СнимокВТ = ЗапросДемоВТ.Выполнить().Выгрузить();'''

CAPTURE_OUTPUT = '''СнимокВыхода = КонтекстОтладки.КадровыеДанныеСотрудников.Скопировать(
    , "Сотрудник,Организация,ФОТ");'''

OVERVIEW_MATERIALIZE = '''# Переменные из %%bsl доступны в Python по тем же именам.
plan = ПланФОТ.to_df(refs='presentation')
display(plan)
print('Сотрудников с данными:', len(plan))
print('Плановый ФОТ:', plan['ФОТ'].sum(), '₽')'''

OVERVIEW_PLAN_CHART = '''import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(9, max(5, len(plan) * 0.32)))
ax.barh(plan['Сотрудник'], plan['ФОТ'].map(float))
ax.invert_yaxis()
ax.set_xlabel('Плановый ФОТ, ₽')
ax.set_title('Плановый ФОТ работающих сотрудников на 01.08.2021')
fig.tight_layout()
display(fig)
plt.close(fig)'''

OVERVIEW_CAPTURE_ARM = '''MODULE_PATH = r'CommonModules\\ПлановыеНачисленияСотрудников\\Ext\\Module.bsl'
CAPTURE_LINE = 745  # Строка Возврат ЗначенияДанныхОплатыТруда; в вашей выгрузке.
runtime.add_capture_point(MODULE_PATH, CAPTURE_LINE)'''

OVERVIEW_CAPTURE_READ = '''captured_plan = СнимокВызова.to_df(refs='presentation')
display(captured_plan)'''

OVERVIEW_CAPTURE_FINISH = '''completed = runtime.resume_capture()
runtime.clear_capture_points()
print('Состояние вызова:', completed.state.value)'''

OVERVIEW_CAPTURE_RESULT = '''resumed_plan = ПланПовторКратко.to_df(refs='presentation')
display(resumed_plan)'''

OVERVIEW_RELOADED_METHOD = '''Функция ТекущиеДанныеОплатыТрудаСотрудников(Ссылка, СотрудникиДаты) Экспорт
    ПараметрыПостроения = ЗарплатаКадрыОбщиеНаборыДанных.ПараметрыПостроенияДляСоздатьВТИмяРегистраСрез();
    ПараметрыПостроения.ФормироватьСПериодичностьДень = Ложь;
    ЗарплатаКадрыОбщиеНаборыДанных.ДобавитьВКоллекциюОтбор(
        ПараметрыПостроения.Отборы, "Регистратор", "<>", Ссылка);
    Запрос = Новый Запрос;
    Запрос.МенеджерВременныхТаблиц = Новый МенеджерВременныхТаблиц;
    ЗарплатаКадрыОбщиеНаборыДанных.СоздатьВТИмяРегистраСрезПоследних(
        "ПлановыйФОТИтоги",
        Запрос.МенеджерВременныхТаблиц,
        Истина,
        ЗарплатаКадрыОбщиеНаборыДанных.ОписаниеФильтраДляСоздатьВТИмяРегистра(
            СотрудникиДаты, "Сотрудник"),
        ПараметрыПостроения);
    Запрос.Текст =
        "ВЫБРАТЬ
        |   ЗначенияСовокупныхТарифныхСтавок.Сотрудник,
        |   ЗначенияСовокупныхТарифныхСтавок.Период,
        |   ЗначенияСовокупныхТарифныхСтавок.СовокупнаяТарифнаяСтавка КАК СовокупнаяТарифнаяСтавка,
        |   ЗначенияСовокупныхТарифныхСтавок.ВидТарифнойСтавки,
        |   ЗначенияСовокупныхТарифныхСтавок.ФОТ КАК ФОТ,
        |   ЗначенияСовокупныхТарифныхСтавок.ФОТ * 1.30 КАК ФОТСоСтраховыми
        |ИЗ
        |   ВТПлановыйФОТИтогиСрезПоследних КАК ЗначенияСовокупныхТарифныхСтавок";
    ЗначенияДанныхОплатыТруда = Запрос.Выполнить().Выгрузить();
    Возврат ЗначенияДанныхОплатыТруда;
КонецФункции'''

def md(text): return nbf.v4.new_markdown_cell(text)
def py(source): return nbf.v4.new_code_cell(source)
def bsl(source): return py('%%bsl\n' + source)

def opening(title, goal, *, setup=OVERVIEW_SETUP, setup_note='', snapshot_note='ЗУП КОРП 3.1.38.92, платформа 8.5.1.1529, дата данных 01.08.2021. '):
    return [md('# '+title+'\n\n'+goal+'\n\n'+snapshot_note+
        'Используйте отдельную демо-копию и соответствующую ей выгрузку исходников. '
        'Подготовка окружения — в [README](README.md). Сеанс закрывается последней ячейкой; при прерывании выполните `runtime.close()`.'),
        md('## Подготовка' + setup_note), py(setup)]

def overview_reload_cells():
    return [
        md('## Hot reload: ФОТ со страховыми взносами\n\nВ выгрузке SOURCE_ROOT '
           'откройте CommonModules/ПлановыеНачисленияСотрудников/Ext/Module.bsl. '
           'Замените метод ТекущиеДанныеОплатыТрудаСотрудников кодом ниже '
           'и сохраните файл. ФОТСоСтраховыми содержит ФОТ с условными '
           'страховыми взносами 30%:\n\n'
           '```bsl\n' + OVERVIEW_RELOADED_METHOD + '\n```\n\n'
           'Правка меняет файл выгрузки на диске, но не конфигурацию ИБ. '
           'Hot reload применяет сохранённый Module.bsl к этому runtime-сеансу.'),
        py('runtime.load_worker_module(MODULE_PATH);'),
        bsl('ПлановыйФотСоСтраховыми = ПлановыеНачисленияСотрудников.ТекущиеДанныеОплатыТрудаСотрудников(\n'
            '    Неопределено, СписокСотрудников);\n'
            'ПланСоСтраховыми = ПлановыйФотСоСтраховыми.Скопировать(, '
            '"Сотрудник,ФОТ,ФОТСоСтраховыми");'),
        py('reloaded = ПланСоСтраховыми.to_df(refs="presentation")\n'
           'display(reloaded)\n'
           'print("ФОТ:", reloaded["ФОТ"].sum(), "₽")\n'
           'print("ФОТ со страховыми:", reloaded["ФОТСоСтраховыми"].sum(), "₽")'),
        py('''fig, ax = plt.subplots(figsize=(7, 4))
ax.bar(['ФОТ', 'ФОТ со страховыми'], [
    float(reloaded['ФОТ'].sum()),
    float(reloaded['ФОТСоСтраховыми'].sum()),
])
ax.set_ylabel('Сумма по выбранным сотрудникам, ₽')
ax.set_title('Результат перегруженного метода')
fig.tight_layout()
display(fig)
plt.close(fig)'''),
    ]

def capture_control():
    return [
        md('## Две точки внутри типового метода\n\nA — перед выгрузкой итогового запроса; '
           'B — перед возвратом таблицы. Следующая Python-ячейка находит эти инструкции '
           'в вашей выгрузке `КадровыйУчет` и устанавливает точки через путь к модулю и номер строки. '
           'Выгрузка должна соответствовать запущенной конфигурации.'),
        py(CAPTURE_ARM),
        md('## Запускаем вызов\n\nВызов находится в отдельной BSL-ячейке. '
           'Она остановится в точке A; последующие ячейки работают с тем же вызовом.'),
        bsl(CAPTURE_CALL),
        py('''import pandas as pd

status_a = runtime.status()
assert status_a.state.value == 'captured'
stack = runtime.runtime_api.capture_stack(cursor=0, limit=20)
display(pd.DataFrame(stack['frames'])[['level', 'module_type', 'line']])
print('Кадров в стеке:', stack['total'])'''),
        md('## Читаем временную таблицу\n\n`КонтекстОтладки` доступен, пока исходный вызов остановлен. '
           'Переносим в Python только три нужных столбца.'),
        bsl(CAPTURE_READ),
        py('''temporary = СнимокВТ.to_df(refs='uuid')
display(temporary)
print('Строк:', len(temporary), 'ФОТ:', temporary['ФОТ'].sum(), '₽')'''),
        py('''resume_b = runtime.resume_capture()
assert resume_b.state.value == 'captured'
assert resume_b.operation_id == status_a.operation_id
print('Останов B:', resume_b.stop_sequence)'''),
        bsl(CAPTURE_OUTPUT),
        py('''captured_output = СнимокВыхода.to_df(refs='uuid')
keys = ['Сотрудник', 'Организация']
sort_rows = lambda frame: frame.sort_values(keys).reset_index(drop=True)
pd.testing.assert_frame_equal(sort_rows(temporary), sort_rows(captured_output))
display(captured_output)'''),
        py('''completed = runtime.resume_capture()
assert completed.succeeded and completed.state.value == 'completed'
assert completed.operation_id == resume_b.operation_id
runtime.clear_capture_points()
print('Обычный результат:', completed.result, '₽')'''),
    ]

def overview_capture_cells():
    return [
        md('## Внутри типового метода\n\nТочка на строке возврата позволяет '
           'посмотреть локальную таблицу до завершения вызова. Путь может быть '
           'абсолютным или относительным к SOURCE_ROOT. Перед запуском укажите '
           'в CAPTURE_LINE строку Возврат ЗначенияДанныхОплатыТруда; '
           'из своей выгрузки.'),
        py(OVERVIEW_CAPTURE_ARM),
        bsl(OVERVIEW_CAPTURE_CALL),
        bsl('СнимокВызова = КонтекстОтладки.ЗначенияДанныхОплатыТруда.Скопировать(, "Сотрудник,ФОТ");'),
        py(OVERVIEW_CAPTURE_READ),
        py(OVERVIEW_CAPTURE_FINISH),
        bsl('ПланПовторКратко = ПланПовтор.Скопировать(, "Сотрудник,ФОТ");'),
        py(OVERVIEW_CAPTURE_RESULT),
    ]

def cleanup(): return [md('## Завершение'),py("runtime.close()\nprint('Сеанс закрыт')")]

def build():
    overview = opening('Плановый ФОТ в Jupyter: от метода ЗУП к hot reload',
        'Выберем всех работающих сотрудников, получим плановый ФОТ типовым методом, '
        'посмотрим результат в Python и добавим страховые взносы через hot reload.',
        setup=OVERVIEW_SETUP,
        snapshot_note='Пример проверен с выгрузкой ЗУП КОРП 3.1.38.92 и платформой 8.5.1.1529; дата демоснимка — 01.08.2021. ',
        setup_note='\n\nЭтот вариант использует API wheels 0.1.17. '
                   'Перед запуском измените `PLATFORM_BIN`, `CONNECTION_STRING` '
                   'и `SOURCE_ROOT` в следующей ячейке. Имя пользователя уже задано; пароль пустой.')
    overview += [
        md('## Выбираем сотрудников\n\nТиповой метод КадровыйУчет.СотрудникиОрганизации '
           'возвращает работающих по трудовым договорам на 01.08.2021. '
           'Для метода оплаты труда оставляем уникальные пары Сотрудник и Период.'),
        bsl(OVERVIEW_EMPLOYEES),
        md('## Текущие данные оплаты труда\n\nТиповой метод возвращает ФОТ, '
           'период и тарифные показатели. Для диаграммы берём только Сотрудник и ФОТ. '
           'Отсутствующие в регистре сотрудники могут не попасть в результат.'),
        bsl(OVERVIEW_PLAN),
        md('## Таблица и диаграмма\n\nИмена BSL-переменных доступны в Python '
           'без префикса. Представление ссылки подходит для подписи столбцов; '
           'это снимок, который не меняется после hot reload.'),
        py(OVERVIEW_MATERIALIZE),
        py(OVERVIEW_PLAN_CHART),
    ]
    overview += overview_capture_cells() + overview_reload_cells()
    overview += [md('## Дальше\n\nВ [03-capture.ipynb](03-capture.ipynb) '
                   'показаны две точки останова, стек вызовов и изменение локального запроса.'),
                 md('## Завершение'), py('runtime.close()')]

    capture = opening('Capture: временная таблица внутри типового метода ЗУП',
        'Остановим один вызов дважды, посмотрим стек и временную таблицу, '
        'затем изменим запрос только внутри выбранного вызова.',
        setup=OVERVIEW_SETUP,
        snapshot_note='Пример рассчитан на ЗУП КОРП 3.1.38.92 и платформу 8.5.1.1529; дата демоснимка — 01.08.2021. ',
        setup_note='\n\nИспользуйте API wheels 0.1.17. Перед запуском измените '
                   '`PLATFORM_BIN`, `CONNECTION_STRING` и `SOURCE_ROOT` в следующей ячейке. '
                   'Имя пользователя уже задано; пароль пустой.')
    capture += [
        md('## Выбираем сотрудников\n\nБерём тех же работающих на дату сотрудников, '
           'что и в обзоре. Для `КадровыеДанныеСотрудников` нужен список ссылок.'),
        bsl(OVERVIEW_EMPLOYEES),
        bsl('СотрудникиДемо = СписокСотрудников.ВыгрузитьКолонку("Сотрудник");'),
    ]
    capture += capture_control()
    capture += [
        md('## Меняем результат одного вызова\n\nПовторно ставим те же точки. '
           'В останове A меняем текст локального `Запрос`: новый запрос читает прежнюю '
           'временную таблицу и умножает ФОТ на 1,1. Исходная конфигурация не меняется.'),
        py('''runtime.add_capture_point(MODULE_PATH, CAPTURE_LINE_A)
runtime.add_capture_point(MODULE_PATH, CAPTURE_LINE_B)'''),
        bsl(CAPTURE_CALL),
        py("experiment_a = runtime.status()\nassert experiment_a.state.value == 'captured'"),
        bsl('КонтекстОтладки.Запрос.Текст = "ВЫБРАТЬ Сотрудник, Организация, ФОТ * 1.1 КАК ФОТ ИЗ ВТКадровыеДанныеСотрудников";'),
        py('''experiment_b = runtime.resume_capture()
assert experiment_b.state.value == 'captured'
assert experiment_b.operation_id == experiment_a.operation_id'''),
        bsl(CAPTURE_OUTPUT),
        py('''from decimal import Decimal

changed_output = СнимокВыхода.to_df(refs='uuid')
expected = sort_rows(temporary).copy()
expected['ФОТ'] = expected['ФОТ'].map(lambda value: Decimal(str(value)) * Decimal('1.1'))
actual = sort_rows(changed_output)
pd.testing.assert_frame_equal(expected[keys], actual[keys])
assert [Decimal(str(value)) for value in expected['ФОТ']] == [
    Decimal(str(value)) for value in actual['ФОТ']]
display(changed_output)
print('ФОТ после изменения запроса:', changed_output['ФОТ'].sum(), '₽')'''),
        py('''experiment_done = runtime.resume_capture()
assert experiment_done.succeeded and experiment_done.state.value == 'completed'
assert experiment_done.operation_id == experiment_b.operation_id
runtime.clear_capture_points()'''),
        md('## Новый вызов снова использует типовой запрос\n\nИзменение локального объекта '
           'действовало только в остановленном вызове. Повторим операцию без точек.'),
        bsl(CAPTURE_CALL),
        bsl('ПланПовторКратко = ПланПовтор.Скопировать(, "Сотрудник,Организация,ФОТ");'),
        py('''restored = ПланПовторКратко.to_df(refs='uuid')
pd.testing.assert_frame_equal(sort_rows(temporary), sort_rows(restored))
display(restored)
print('ФОТ после обычного вызова:', restored['ФОТ'].sum(), '₽')'''),
        md('## Граница опыта\n\nМы изменили свойство локального объекта `Запрос`, '
           'а не код уже начатого вызова. Для присваивания нового значения скалярной '
           'локальной переменной нужен другой механизм.'),
    ] + cleanup()
    for name, cells in [('01-overview', overview), ('03-capture', capture)]:
        for index, cell in enumerate(cells):
            cell.id = f'{name}-{index:02d}'
        notebook = nbf.v4.new_notebook(
            cells=cells,
            metadata={
                'kernelspec': {'display_name': '1C BSL Demo', 'language': 'python', 'name': 'onec-demo'},
                'language_info': {'name': 'python'},
            },
        )
        nbf.validate(notebook)
        DEMO_DEST.mkdir(parents=True, exist_ok=True)
        nbf.write(notebook, DEMO_DEST / (name + '.ipynb'))
        print(name, len(cells), 'cells')

if __name__ == '__main__': build()
