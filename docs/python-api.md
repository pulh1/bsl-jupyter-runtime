# Python API сеанса 1С

Это руководство описывает публичные объекты текущей версии runtime, которые доступны из Python-ячейки notebook. Запуск с `RuntimeConfig`, `RuntimeSessionConfig` и `InteractiveRuntimeSession.start()` показан в [быстром старте](../README.md#быстрый-старт-в-vs-code). Все вызовы к 1С относятся к запущенному сеансу; обычные Python-вычисления не требуют обращения к debugger.

`RuntimeConfig` импортируется из `onec_runtime.config`, `RuntimeSessionConfig`, `ExtensionMode` и `RuntimeSession` — из `onec_runtime.session`, а `InteractiveRuntimeSession` — из `onec_runtime_jupyter`. В `RuntimeConfig` обязательный `platform_bin` указывает каталог исполняемых файлов платформы; `connection_string` выбирает файловую или серверную ИБ. `RuntimeSessionConfig(runtime=..., source_root=..., extension_mode=...)` связывает сеанс с выгрузкой исходников и режимом установки расширения (`AUTO` или `MANUAL`). `source_root` нужен для загрузки модулей и точек по пути к файлу. Дополнительные параметры, включая `workspace`, `chunk_size` и `evidence_root`, используются при настройке окружения и переноса данных.

Типы ответов `RuntimeReply`, `RuntimeReplyKind`, `RuntimeStatus` и `RuntimeNamespaceSnapshot` находятся в `onec_runtime.runtime_api`; `CaptureView`, `DebugFrame` и `StackPage` — в `onec_runtime.capture_inspection`; `CaptureStatus`, `CapturePhase`, `CaptureEvaluationOutcome`, `CaptureEvaluationState`, `CaptureEvaluationKind`, `CaptureEvaluationTiming` и `CaptureFailureDiagnostic` — в `onec_runtime.capture_evaluation`. `RuntimeDebugStop` находится в `onec_runtime.worker_breakpoints`, а его `StopReason` — в `onec_runtime.stop_routing`. Обычно эти объекты не нужно создавать вручную: их возвращают методы сеанса и текущего CAPTURE view.

`RuntimeSession` — пользовательская граница core runtime. Объект `runtime.runtime_api` сейчас имеет реализацию `PrototypeRuntimeApi` и собирается при запуске сеанса. Прямой вызов его методов в приложении может обойти блокировку и владение CAPTURE на уровне `RuntimeSession`; для обычного Python-кода используйте методы сеанса. `RdbgArbiter`, его `ExecutionTicket` и `StopRequestOutcome` относятся к внутреннему слою исполнения, а не к API notebook.

## Сеанс и выполнение

```python
from onec_runtime_jupyter import InteractiveRuntimeSession

# config: RuntimeSessionConfig из быстрого старта
runtime = InteractiveRuntimeSession.start(config)

reply = runtime.execute_bsl('Сообщить("Готово");')
print(reply.kind.value, reply.succeeded, reply.messages)
print(runtime.status().state.value)

runtime.close()
```

`InteractiveRuntimeSession.start(config, *, shell=None, display=None)` владеет `RuntimeSession`, устанавливает `%%bsl` magic в текущий IPython shell и делегирует методы core-сеансу. Его можно использовать как контекстный менеджер (`with InteractiveRuntimeSession.start(config) as runtime:`). Для приложения без notebook есть `RuntimeSession.start(config, *, progress=None)` из `onec_runtime.session`; он возвращает тот же core API, но не устанавливает Jupyter magic и Python-прокси. `close()` освобождает сеанс; если завершение ресурсов не удалось, вызов можно повторить.

| Метод или объект | Назначение |
| --- | --- |
| `runtime.execute_bsl(source: str, *, source_unit=None, on_execution_provenance=None) -> RuntimeReply` | Выполнить исходный BSL как одну ячейку в текущем маршруте MAIN или CAPTURE. Необязательные `source_unit` и callback нужны интеграциям, которые сопоставляют диагностику с ячейкой. В notebook обычно используется `%%bsl`. |
| `runtime.status() -> RuntimeStatus` | Получить состояние runtime без запуска новой BSL-команды. |
| `runtime.namespace_snapshot() -> RuntimeNamespaceSnapshot` | Получить имена постоянных BSL-значений и поколения runtime/контекста. |
| `runtime.current_capture() -> CaptureView` | Получить вид текущей остановки CAPTURE; при её отсутствии вызывается `NoActiveCaptureError`. |
| `runtime.resume_capture(*, dirty_roots=(), continuation_attempt_id=None, timeout_s=None) -> RuntimeReply` | Продолжить выполнение остановленной MAIN-команды через текущий CAPTURE. `dirty_roots` добавляет имена корневых переменных для записи назад; обычный путь отслеживает их автоматически. `timeout_s` ограничивает ожидание вызывающего, а не BSL-код. В notebook доступно также `%bsl_resume`. |
| `runtime.resume_debug_stop(*, timeout_s=None) -> RuntimeReply` | Продолжить остановку `debug_stopped` на пользовательской точке. Это отдельный путь от `resume_capture()`. |
| `runtime.add_capture_point(path, line) -> ModuleLocation` / `runtime.clear_capture_points()` | Добавить точку по файлу внутри `source_root` и номеру строки от 1 / снять точки сеанса. |
| `runtime.refresh_capture_sources()` | Повторно прочитать настроенный каталог исходников после внешних изменений. Требует настроенного каталога и отсутствия активной CAPTURE; ранее разрешённые точки нужно установить снова. |
| `runtime.close()` | Завершить принадлежащий этому объекту сеанс. |

`RuntimeReply` содержит `kind` (`RuntimeReplyKind`), `state` (`OperationState`), `operation_id`, `succeeded`, `result`, `messages`, `error` и, когда применимо, `location`, `debug_stop`, `diagnostic`, `stop_sequence`, `capture_ticket` и `changed_roots`. Успех BSL-ячейки и завершение всей MAIN-команды различаются: `kind == RuntimeReplyKind.CAPTURED` означает, что MAIN остановлена и может продолжиться через `resume_capture()`. Проверяйте `kind` и `succeeded`, а не только наличие `result`.

`RuntimeStatus` содержит `state`, `runtime_generation`, `operation_id`, `worker_generation` и `capture_setup`. Последнее поле равно `None`, когда незавершённого открытия CAPTURE нет; иначе это снимок `CaptureSetupSnapshot` с `setup_stage`, `context_state`, `frame_identity` и безопасным `error_code`. `state == capture_setup_failed` после подтверждённой ошибки чтения locals, переноса, поиска kernel frame или начала контекста сохраняет остановленную MAIN-команду. До успешного открытия контекста новую CAPTURE-ячейку и `resume_capture()` выполнить нельзя; `CaptureView` в этой стадии ещё недоступен. Совпадение target и адреса строки само по себе не доказывает, что при повторной проверке это та же физическая остановка, поэтому публичного повтора setup пока нет.

Во время ожидания остановки MAIN вызов `status()` из другого Python-потока возвращает `state == main_pending` и идентификатор текущей команды. Такое наблюдение не прерывает выполнение 1С и не занимает RDBG.

`RuntimeNamespaceSnapshot` содержит `names`, `runtime_generation`, `context_generation`. Поколения используются для проверки актуальности прокси. Состав состояний `OperationState` включает `idle`, `main_pending`, `captured`, `capture_setup_failed`, `evaluating_capture`, `debug_stopped`, `resuming`, `recovering`, `completed`, `failed` и другие диагностические состояния; не считайте любое состояние, отличное от `captured`, потерей target. `main_pending` означает, что MAIN-команда ещё ждёт исхода или следующей остановки; `evaluating_capture` и `resuming` означают занятую CAPTURE-операцию. `status()` не сообщает, завершилось ли удалённое выполнение после прерывания одного лишь Python-ожидания.

Если `reply.kind == RuntimeReplyKind.DEBUG_STOPPED`, поле `reply.debug_stop` содержит `RuntimeDebugStop`: `reason`, `origin`, `operation_id`, `location`, `frames` и `breakpoint_ids`. `reason` — значение `StopReason` (`user_breakpoint`, `pause`, `exception` и другие причины); `origin` указывает на MAIN или CAPTURE evaluation. Продолжайте такую остановку через `runtime.resume_debug_stop()`. Для `RuntimeReplyKind.CAPTURED` используйте `runtime.current_capture()` и `runtime.resume_capture()`.

## `%%bsl` и значения в Python

После успешной `%%bsl`-ячейки постоянные имена BSL становятся доступны в Python как ленивые `OnecValueProxy`. Их можно брать через автоматически установленный объект `bsl` или по имени в notebook. `bsl["Имя"]` удобен, если такое имя уже занято в Python.

```python
%%bsl
Количество = 3;
```

В следующей **Python-ячейке**:

```python
print(bsl["Количество"].materialize())
```

Прокси обозначает значение в сеансе 1С, а не готовую Python-копию. `repr(proxy)` не читает содержимое. Чтение происходит при `materialize()` или `to_df()`. После замены/закрытия сеанса или смены поколения контекста старый прокси становится неактуальным; возьмите новый из `bsl`. `runtime.namespace_snapshot().names` показывает текущие опубликованные имена.

| Метод `OnecValueProxy` | Результат и параметры |
| --- | --- |
| `materialize(*, refs="presentation", ref_columns=None, uuid_suffix="__uuid", chunk_size=None, max_depth=32, max_items=100_000, max_bytes=64*1024*1024)` | Перенос поддерживаемого значения в Python с ограничениями глубины, числа элементов и объёма. |
| `to_df(*, refs="presentation", ref_columns=None, uuid_suffix="__uuid", chunk_size=None)` | Перенос таблицы значений в `pandas.DataFrame`. |
| `head(limit)` | Новый прокси на первые `limit` элементов таблицы/коллекции. |
| `proxy[start:stop]` | Новый прокси на ограниченный срез без шага. |
| `tabular_section(name)` | Новый прокси на табличную часть объекта. |

Для ссылочных значений `refs` принимает `"presentation"` (представление), `"uuid"` (идентификатор) или `"both"` (оба). `ref_columns` задаёт режим для отдельных колонок по имени; в режиме `"both"` дополнительная колонка получает суффикс `uuid_suffix`. Фактический Python-тип результата `materialize()` зависит от формы BSL-значения; `to_df()` предназначен для таблиц.

```python
df = bsl["Таблица"].head(10).to_df(refs="uuid")
value = bsl["Объект"].tabular_section("Строки").materialize(max_items=100)
```

Материализация и чтение CAPTURE-переменных могут обращаться к остановленному 1С target. Пока другой CAPTURE-запрос выполняется, такой вызов может получить `CaptureBusyError`; после истечения ожидания CAPTURE-выражения может прийти `CaptureEvaluationPendingError`. Эти ошибки сами по себе не доказывают потерю остановленного кадра.

При использовании `RuntimeSession` без Jupyter те же операции доступны по ограниченному handle постоянного контекста: `runtime.materialize("Контекст.Количество")` и `runtime.to_df("Контекст.Таблица", refs="uuid")`. Эти методы принимают те же параметры политики ссылок; `materialize()` дополнительно принимает `max_depth`, `max_items`, `max_bytes` и `timeout_s`.

## Остановка CAPTURE

`add_capture_point(path: str, line: int)` устанавливает точку по пути к модулю внутри `source_root` и номеру строки; возвращает `ModuleLocation`. `clear_capture_points()` удаляет заданные точки. После попадания в точку `runtime.current_capture()` возвращает `CaptureView` именно этой остановки. Сохранённый view нельзя использовать для новых чтений после продолжения или перехода к другой остановке.

```python
runtime.add_capture_point(r"CommonModules\ExampleServer\Ext\Module.bsl", 120)
# Следующая MAIN BSL-ячейка вызывает код, проходящий через эту строку.
capture = runtime.current_capture()
print(capture.status().phase.value)

page = capture.stack[:10]
print(page.total, page.next_cursor)
for frame in page.frames:
    print(frame)
```

`CaptureView` содержит `operation_id`, `capture_generation`, `stop_sequence` и следующие средства:

| API | Что возвращает |
| --- | --- |
| `capture.status() -> CaptureStatus` | Снимок фазы (`phase`) и доступных действий `can_inspect`, `can_resume_capture`, `can_wait`. Также содержит `pending_evaluation_id`, `last_evaluation_id`, `last_user_evaluation_id`, `evaluation_kind`, `evaluation_timing`, `failure`, если применимо. Для старого view вернёт фазу `stale`. |
| `capture.wait(timeout_s=None, evaluation_id=None) -> CaptureEvaluationOutcome` | Ждёт исход конкретной или последней оценки. При истечении локального ожидания возвращает outcome с `state == pending`; не запускает и не отменяет оценку. Для старого view вызывает `StaleCaptureError`. |
| `capture.stack[index]`, `capture.stack[start:stop]` | Один `DebugFrame` или страница `StackPage`. `capture.stack.native` открывает физические кадры, включая служебные. Срез должен иметь конечные границы и не больше 100 кадров. |
| `capture.context.variables[name]`, `capture.context.variables[start:stop]` | Значение по точному имени или ограниченная страница переменных staged CAPTURE-контекста. |

`StackPage` содержит `frames`, `total`, `next_cursor`; `with_methods()` добавляет разрешённые по исходникам имена и сигнатуры методов. `DebugFrame` содержит `source`, `line`, `native_level`, `source_status`, `method_status`; `frame.variables`, `frame.parameters`, `frame.locals` открывают переменные этого кадра. Страницы переменных содержат `items`, `total`, `next_cursor`. `ValueNode` показывает `name`, `type_name`, `preview`, `size`, `shape`, `expandable` и разрешает ограниченное чтение через `children`, `fields`, `items`, `columns`, `rows`. Выбор страницы требует конечного среза; бесконечная итерация намеренно недоступна. Некоторые значения или сведения об исходниках могут быть недоступны; это отражается диагностикой чтения, а не обязательной потерей CAPTURE.

```python
frame = capture.stack[0]
for node in frame.locals[:10].items:
    print(node.name, node.preview)

for node in capture.context.variables[:10].items:
    print(node.name, node.preview)

runtime.resume_capture()
runtime.clear_capture_points()
```

Если `%%bsl` в CAPTURE вернул идентификатор ожидающего выражения, исход можно проверить в следующей Python-ячейке:

```python
capture = runtime.current_capture()
status = capture.status()
if status.can_wait:
    outcome = capture.wait(timeout_s=10, evaluation_id=status.pending_evaluation_id)
    print(outcome.state.value, outcome.messages)
```

`CaptureEvaluationOutcome` содержит `evaluation_id`, `evaluation_kind`, `state` (`pending`, `completed`, `failed`, `unknown`), `result`, `messages`, `error`, `diagnostic` и `timing`. `wait()` наблюдает уже отправленную операцию. Он не является повторным `evalExpr`.

`CaptureEvaluationKind` различает пользовательский BSL (`user_bsl`), чтение (`inspection`) и вспомогательную материализацию (`materialization_helper`). `CaptureFailureDiagnostic` содержит безопасные `code`, `message` и `recommended_action`. `CaptureEvaluationTiming` содержит время создания, отметки этапов в миллисекундах, `remote_step_count` и `poll_count`; отсутствующая отметка имеет значение `None`. Поля `failure` и `diagnostic` описывают состояние операции, но сами по себе не доказывают потерю остановленного target.

### Исправление в том же CAPTURE

Ошибочная BSL-ячейка, чтение stack/variables и неудачная материализация относятся к отдельным операциям CAPTURE. При подтверждённой ошибке они не снимают остановку сами по себе: сначала проверьте `capture.status()`, исправьте причину и повторите только нужную операцию. Следующий пример запускается в уже остановленном CAPTURE; `ТаблицаСтрок` — прокси `bsl` из ранее успешной BSL-ячейки с более чем одной строкой.

```python
from onec_runtime.errors import CaptureValueCheckError, MaterializationLimitError

capture = runtime.current_capture()

# Ошибка пользовательской BSL остаётся ошибкой этой ячейки, а не resume.
failed = runtime.execute_bsl('ВызватьИсключение "проверка CAPTURE";')
assert not failed.succeeded
assert capture.status().can_inspect

# При остановке в пользовательском модуле его кадр доступен для проверки.
frames = [frame for frame in capture.stack.native[:20].frames
          if not frame.runtime_kernel]
local_names = ([node.name for node in frames[0].locals[:20].items]
               if frames else [])
context_names = [node.name for node in capture.context.variables[:20].items]
corrected = runtime.execute_bsl("РезультатИнструкции = 42;")
assert corrected.succeeded

proxy = bsl["ТаблицаСтрок"]       # ленивое значение, данных в Python ещё нет
try:
    proxy.materialize(max_items=1)  # намеренно мало для таблицы с несколькими строками
except (MaterializationLimitError, CaptureValueCheckError):
    # Неудача materialize() не доказывает потерю кадра.
    assert capture.status().can_inspect

rows = proxy.to_df(refs="presentation")  # повтор с подходящим способом переноса
reply = runtime.resume_capture()
```

В реальной программе выбирайте лимит `max_items` по данным, а не полагайтесь на этот намеренно маленький лимит. Если после любой операции `status().phase` равна `evaluating` или `outcome_unknown`, не отправляйте повтор: используйте `capture.wait()` с `pending_evaluation_id`. При `recovery_required` текущий API блокирует новые чтения до выяснения исхода и ремонта ресурса; сама эта фаза не доказывает потерю кадра. При `stale` старый `CaptureView` уже не относится к текущему stop. После `resume_capture()` прокси, привязанные к прежнему контексту, нужно получить заново после следующей успешной публикации BSL-значений.

## Прерывание ячейки и Stop

`KeyboardInterrupt` или кнопка остановки notebook прерывают ожидание Python-ячейки. После отправки команды в 1С это само по себе **не подтверждает** прекращение BSL-кода. Сначала проверьте `runtime.status()` (в notebook — `%bsl_status`). Если сохранилась текущая CAPTURE-остановка, `runtime.current_capture().status()` показывает её фазу; `capture.wait(timeout_s=..., evaluation_id=...)` позволяет наблюдать уже принятую оценку без повторной отправки. Истечение `timeout_s` у `capture.wait()` возвращает `pending`, а у `resume_capture()` прекращает ожидание вызывающего; эти интервалы не служат сроком выполнения BSL.

Для принятого CAPTURE `evalExpr` прерванный ожидающий вызов отвязывается от операции. Для принятого `resume_capture()` завершение также может прийти после ухода ожидающего Python-вызова. Новую CAPTURE-операцию отправляйте лишь после проверки текущего состояния: `evaluating_capture`, `resuming` и `recovering` не являются подтверждением нового свободного stop. При `main_pending` MAIN-команда ещё может исполняться или ждать debugger stop; `status()` показывает опубликованное состояние, не останавливая её.

У `RuntimeSession` и `InteractiveRuntimeSession` пока нет публичного `request_stop()` или `abort_current()`. Внутренний `RdbgArbiter.request_stop(ticket)` возвращает `StopRequestOutcome`: `CANCELLED_BEFORE_EFFECT` означает локальную отмену до удалённого действия, `REQUESTED` означает принятую заявку и запрет новых действий этой операции, `ALREADY_SETTLED` означает, что ticket уже завершён. `REQUESTED` и поле `TicketStatus.stop_requested` **не являются** доказательством остановки или завершения target. Эти объекты предназначены для владельца исполнения; notebook-код не должен создавать tickets или вызывать arbiter напрямую.

## Ошибки и практические границы

- `BslCellError` — ошибка `%%bsl` в Jupyter; подробный безопасный диагноз выводится под ячейкой. Прямой `execute_bsl()` возвращает `RuntimeReply`, если запрос завершился штатным ответом runtime.
- `NoActiveCaptureError` — активной CAPTURE-остановки нет; `StaleCaptureError` — сохранённый `CaptureView` относится к старому останову.
- `CaptureBusyError` — текущая CAPTURE-операция заняла маршрут; наблюдайте её через `capture.status()`/`wait()`.
- `CaptureEvaluationPendingError` — подтверждённая CAPTURE-оценка пережила ожидание вызывающей ячейки; у ошибки есть `evaluation_id` и `evaluation_kind`. Результат проверяется через `CaptureView.wait()`.
- `ProtocolError` и более специальные ошибки чтения/материализации могут означать отказ отдельной операции. Проверяйте `runtime.status()` и `capture.status()` перед решением о перезапуске.
