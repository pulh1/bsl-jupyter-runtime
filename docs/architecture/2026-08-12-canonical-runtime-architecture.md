# 1C Interactive Runtime — каноническая архитектура

Дата: 2026-08-12.

Статус: текущая authoritative revision для дальнейших spikes. Документ фиксирует
решения, но не превращает недоказанные возможности в факты. Точная степень
доказанности хранится в `RUNTIME-CONTEXT.md` и research-артефактах.

## 1. Product thesis

1C Interactive Runtime — stateful interactive development и computational
debugging environment поверх настоящего server-side runtime 1С.

Его ценность не сводится по отдельности к произвольному BSL, RDBG, Jupyter или
MCP. Целевой workflow объединяет:

```text
real 1C runtime
  + persistent exploratory state
  + interactive BSL execution
  + capture настоящего application frame
  + изменение state и продолжение того же computation
  + hot-reloadable worker modules
  + Python/pandas materialization
  + human и agent frontends
```

Runtime предназначен прежде всего для дорогих исследований: сложных расчётов
ЗУП/НДФЛ, длинных query pipelines, временных таблиц, трудно воспроизводимых
ошибок и сравнения нескольких гипотез без повторной подготовки всего state.

Runtime не заменяет EDT-MCP, YAXUnit и обычный debugger. Предпочтительный цикл:

```text
EDT-MCP: найти production code и зависимости
  → Interactive Runtime: исследовать, менять, сравнивать
  → EDT-MCP: перенести стабилизированное решение
  → clean replay + YAXUnit + conventional integration/debug verification
```

## 2. Scope первой версии

### 2.1. Два темпа разработки

Проект сознательно ведётся двумя связанными дорожками:

```text
Prototype track
  → быстро получить работающий Jupyter flow
  → MAIN + CAPTURE + persistent context
  → минимальный parser/lowering
  → доказать полезный end-to-end workflow

Architecture track
  → не потерять ownership/lifecycle contracts
  → определить recovery, generations, isolation и MCP boundary
  → не дать prototype shortcuts стать неявным product design
```

Зафиксированы два deployment-профиля с разными гарантиями:

```text
PrototypeProfile
  → in-process Controller facade внутри ipykernel
  → один frontend и один writer
  → тот же Runtime API и те же MAIN/CAPTURE semantics
  → нет гарантии пережить смерть ipykernel

ProductProfile
  → отдельный supervised Runtime Controller
  → reconnect, lease/watchdog и multi-frontend arbitration
  → потеря frontend не уничтожает runtime session
```

Ближайший prototype не обязан сразу иметь отдельный daemon, полноценный
supervisor, shared-cluster isolation или production parser. Promotion gate в
ProductProfile — доказанные external ownership, reconnect, operation
arbitration, lease/watchdog и fault recovery без изменения публичного Runtime
API, semantics `MAIN`/`CAPTURE` и notebook namespace.

Минимальный Jupyter prototype считается целостным, когда он умеет:

- выполнить обычную BSL cell через MAIN channel;
- остановиться на зарегистрированной capture breakpoint;
- вернуть управление notebook, сохранив paused computation;
- выполнить debug cells в captured frame;
- применить root write-back и продолжить computation;
- получить final MAIN completion;
- сохранить notebook context между ячейками;
- пережить ожидаемую BSL-ошибку без ручного перезапуска.

Первый ближайший deliverable — собрать эти уже частично доказанные примитивы в
один notebook E2E: `MAIN → CAPTURE → debug cells → write-back → resume → final
completion`, включая persistent context и ожидаемую ошибку.

### 2.2. Функциональный scope

В scope:

- server BSL;
- dedicated файловая или изолированная server development session;
- запросы и временные таблицы;
- `ТаблицаЗначений`, `Массив`, `Структура`, `Соответствие` и ссылки 1С;
- persistent notebook context;
- внешние обработки/worker generations;
- debugger-only capture;
- Python, pandas, Jupyter, MCP и CLI через общий Runtime API.

Вне scope v1:

- формы и client BSL semantics;
- обычные пользовательские production-сеансы;
- shared cluster без доказанной isolation;
- иллюзия локальных Python-объектов для remote values 1С;
- собственная BSL VM;
- автоматический dependency DAG временных таблиц;
- custom multi-language Jupyter kernel.

Среда v1 считается trusted dev/test. `execute`, `eval`, frame mutation и reload
являются debugger-grade remote code execution.

## 3. Главная граница владения

В ProductProfile Runtime Controller — самостоятельный долгоживущий сервис и
единственный логический владелец живой runtime session. В PrototypeProfile тот
же ownership contract реализует in-process facade, но process-failure гарантий
нет.

```text
       Jupyter          MCP            CLI/tests
          \              |               /
           \             | RPC          /
            +------------v-------------+
            |      Runtime Controller   |
            | Runtime API               |
            | RuntimeState              |
            | ExecutionRouter           |
            | RDBG client/event pump    |
            | 1C/worker lifecycle       |
            | lease/watchdog/recovery   |
            +-------------+-------------+
                          |
                     RDBG + data plane
                          |
                    dedicated 1C session
```

Следствия:

1. Jupyter, MCP и CLI — thin frontends. Они не реализуют собственную debugger
   state machine и не вызывают `modifyValue`, `Continue` или raw `evalExpr`
   напрямую.
2. MCP adapter не владеет persistent state или lifetime 1С. Все tools вызывают
   public Runtime API.
3. Transport frontend↔controller (`stdio`, local socket, pipe, HTTP) не задаёт
   lifetime runtime session.
4. В ProductProfile падение/перезапуск ipykernel, MCP adapter, CLI или UI debug
   adapter не должно уничтожать Controller, 1C KernelLoop/RuntimeState либо
   оставлять их без владельца.
5. Controller должен уметь принять новый frontend connection и вернуть
   фактические `SessionState`, `StopReason`, active capture и generation.

Для PrototypeProfile Controller facade живёт в ipykernel. Его смерть завершает
runtime generation; это явно более слабая гарантия, а не product topology.

## 4. Failure boundaries

Нельзя смешивать разные виды потери процесса.

### 4.1. Потеря frontend

Controller и 1С продолжают существовать. Active request завершается ошибкой
transport, но владение session остаётся у Controller. Для paused capture
запускается lease policy; frontend может переподключиться до истечения lease.

### 4.2. Потеря debug adapter frontend

Отдельный DAP/MCP/Jupyter adapter является клиентом Controller и не владеет
RDBG DebugUI registration. Его смерть эквивалентна потере frontend, а не потере
kernel.

### 4.3. Потеря RDBG transport или `dbgs`, Controller жив

Controller обнаруживает отказ по transport error/heartbeat и публикует `LOST`
с `LossCause=RDBG`. Состояние target и paused frame считается неизвестным.
Reattach допустим только после отдельного platform proof с совпадающими target,
runtime generation и stop sequence; иначе внешний supervisor завершает
dedicated target и Controller создаёт новую runtime generation. Старые
Python/worker/capture references инвалидируются.

### 4.4. Потеря процесса Controller

Погибший Controller сам не может опубликовать `LOST`, выполнить watchdog или
сохранить in-memory journal. Detector и recovery authority принадлежат внешнему
supervisor и, при необходимости, target-side guard. В PrototypeProfile это
просто потеря всей runtime generation. Для ProductProfile recovery/reconnect
остаётся OPEN до crash/restart spike; по умолчанию orphaned dedicated target
завершается, а не продолжается вслепую.

### 4.5. Потеря 1С target/session

Controller публикует `LOST`, закрывает capture, увеличивает
`runtime_generation`, помечает remote proxies stale и выполняет clean restart.
Бесшовное восстановление in-memory state не обещается.

### 4.6. Почему нужен lease/watchdog

Paused application request может удерживать транзакцию, locks, временные ресурсы
и память. Cleanup не может зависеть от `finally` в Jupyter kernel.

```text
CAPTURED
  → owner heartbeat/lease
  → reconnect within lease
  → иначе controller: safe resume или terminate generation
```

До recovery spike действует консервативная policy:

| Stop/write state | Lease expiry action |
|---|---|
| Известный capture, write journal пуст, object mutations отсутствуют | `Continue` допустим только как явно настроенная policy |
| Запись staged, но flush не начинался | Отменить staged changes; затем policy `Continue` или termination |
| Partial write-back, immediate object mutation, unknown stop или потеря frame identity | Terminate runtime generation; автоматический `Continue` запрещён |

Контроллерный watchdog недостаточен при смерти самого Controller: ProductProfile
обязан иметь внешний supervisor/target-side guard.

## 5. Canonical RuntimeState

```text
RuntimeState
  ├─ NotebookContext
  ├─ CaptureState
  ├─ WorkerRegistry
  ├─ RuntimeGeneration
  ├─ ModuleGenerations
  ├─ LifecycleMetadata
  ├─ ActiveOperation
  ├─ LeaseOwner/LeaseExpiry
  └─ WriteJournal
```

`KernelLoop` не владеет `NotebookContext`. MAIN channel, worker и capture
processing используют один logical RuntimeState независимо от его физического
представления внутри 1С.

`ПараметрыСеанса` не являются canonical storage. В PrototypeProfile
authoritative `NotebookContext` физически живёт в persistent runtime-обработке
dedicated 1C session; Python хранит только handles/snapshots. В ProductProfile
Controller владеет durable metadata/checkpoints, но живые 1C objects всё равно
принадлежат target generation. Restart Controller не обещает восстановить
неcheckpointed heap; restart target всегда инвалидирует его.

Команды и handles несут релевантные epochs:

```text
controller_incarnation/control_epoch
runtime_generation
context_generation
module_generation
capture_generation/stop_seq
```

`reset_context` увеличивает `context_generation`; restart target —
`runtime_generation`. Поздний response или handle с несовпадающим epoch
отклоняется.

## 6. Два execution channels

### MAIN

```text
STOPPED + MAIN_SERVICE
  → modifyValue(Command envelope)
  → Continue
  → обычное выполнение BSL через Выполнить()/worker
  → CAPTURE breakpoint или MAIN_SERVICE completion
```

MAIN использует нормальный `Continue` transition. Запуск MAIN через active
`evalExpr` отклонён: breakpoint внутри debugger evaluation не имеет доказанного
stop/modify/resume/completion contract.

### CAPTURE

```text
STOPPED + CAPTURE(id)
  → selected frame
  → evalLocalVariables / explicit variables
  → live КонтекстОтладки
  → debug cells через evalExpr без Continue
  → write-back roots
  → Continue
```

`ExecutionRouter` выбирает channel по текущему state/reason. Нельзя скрывать оба
протокола внутри одного условно разросшегося `execute()`.

Research command loop с восемью и более reads сохраняется как verification
harness. Product MAIN protocol стремится к четырём операциям:

```text
modify Command → Continue → wait stop → read Completion
```

Оптимизация выполняется только после waterfall; отдельный IPC command channel —
Plan B при доказанном RDBG bottleneck.

## 7. State machine и операции

Transport/lifecycle state и причина остановки ортогональны.

```text
SessionState:
  DETACHED | ATTACHING | STOPPED | RUNNING | FAILED | LOST | TERMINATING

StopReason:
  MAIN_SERVICE | CAPTURE(id) | USER_BREAKPOINT | EXCEPTION | PAUSE | UNKNOWN
```

Derived UI/runtime modes:

```text
STOPPED + MAIN_SERVICE  → MAIN_READY
STOPPED + CAPTURE(id)   → CAPTURED(id)
STOPPED + USER_BREAKPOINT → DEBUG_STOPPED
RUNNING                 → RUNNING
LOST                    → RUNTIME_LOST(LossCause)
```

Каждый event проверяется по target id, module identity, line, runtime generation
и ожидаемому transition. Неожиданная остановка не считается completion.

Третья ортогональная ось описывает активную операцию:

```text
OperationState:
  IDLE | MAIN_PENDING | EVALUATING_CAPTURE | FLUSHING |
  PARTIAL_WRITEBACK_FAILURE | RESUMING | COMPLETED | FAILED
```

На runtime разрешена одна mutating operation. Каждая команда несёт
`operation_id`, `runtime_generation`, при capture — `capture_id/stop_seq`, а
также principal/owner и lease expiry. Retry идемпотентен по `operation_id`;
поздние ответы и cancellation проверяют epochs. Исходная MAIN operation
сохраняется через промежуточный CAPTURE и коррелируется с final completion.
При meaningful capture-stop frontend получает `CapturedStop` и operation
handle, а не ложный final result.

## 8. Notebook и capture namespace

```text
ОбычноеИмя           → NotebookContext.ОбычноеИмя
КонтекстОтладки.X    → root текущего captured frame
```

Captured locals не shadow’ят notebook variables. Скрытый `Контекст` передаётся
BSL-ячейкам автоматически. Frontends не знают физическое размещение state.

Python и 1С имеют разные heaps. Python получает symbol proxies, а операции
`inspect`, `eval`, `call`, `to_df`/`to_pandas` являются явными remote actions.
После смены generation старый proxy возвращает `StaleOneCReference`.

## 9. Capture semantics

Capture point — обычная RDBG breakpoint без обязательной source
instrumentation. Extension-based CaptureLoop остаётся fallback/research path.

DECIDED contract изменения state:

- mutable object mutation через live reference происходит немедленно;
- присваивание scalar/object root staged до write-back;
- debug cell не является транзакцией;
- исключение не откатывает уже выполненные object mutations;
- write-back нескольких roots последовательный и неатомарный;
- при failure execution остаётся paused;
- Controller хранит per-root journal `pending/succeeded/failed/compensated`.

Текущий PROVEN scope уже: один mutable `Массив` в `ServerEmulation` на платформе
8.3.27.2170/frame 0; одна строковая root variable/frame 0; два roots с одним
намеренно отсутствующим root. Journal и компенсация пока реализованы
spike-driver, а не crash-safe Controller. Другие mutable types, параметры,
non-zero frames, retry после partial failure и потеря Controller/frontend — OPEN.

Aliasing делает чисто синтаксический `dirty_roots` ненадёжным. До semantic AST
безопасный MVP использует setter/journal либо консервативный flush всех writable
roots. Bare alias всего `КонтекстОтладки` может быть запрещён lowering'ом.

Jupyter request заканчивается на meaningful stop. Call stack 1С может оставаться
paused, но ipykernel свободен; следующая cell является новым RPC request.

## 10. Worker и hot reload

Persistent state отделён от hot-reloadable code.

```text
runtime_generation = N
WorkerName.module_generation = M
```

Reload увеличивает module generation, но не обязан менять runtime generation.
Ссылки на старые worker objects становятся stale/unsupported; одновременное
неявное использование нескольких версий запрещено.

`control_epoch`, `runtime_generation`, `context_generation`,
`module_generation` и `capture_generation/stop_seq` имеют независимые причины
изменения и совместно проверяются Runtime API. Это защищает от late response
старого Controller и команды к уже закрытому capture.

Worker prototyping покрывает общие модули и обработки, но не обещает полного
равенства production context объектных модулей, форм или privileged/common
module properties.

## 11. Jupyter, MCP и CLI

### Jupyter

Human-facing frontend: стандартный Python/ipykernel + extension/magics.
Python runtime и 1С runtime persistent, но разделены. Notebook хранит journal
эксперимента, BSL/Python cells, результаты, snapshots и доказательства.

### MCP

Machine-facing adapter того же Runtime API. Минимальные capability boundaries:

```text
READ_RUNTIME
EXECUTE_BSL
MUTATE_CAPTURE
CONTROL_EXECUTION
RELOAD_MODULE
MATERIALIZE_DATA
```

MCP tool никогда не обращается к raw `RdbgClient`. Agent permissions, audit и
security policy проверяются на Runtime API boundary. Внешняя публикация MCP для
v1 запрещена без отдельной auth/isolation model.

Transport authentication выполняется на adapter edge; domain authorization и
audit — в Controller. Capability связывается с principal и runtime session;
read-only principal не может `resume`. Long-running вызовы возвращают operation
handle/status/events и не удерживают MCP request. Jupyter, MCP и CLI подчиняются
одной single-writer/arbitration policy.

### CLI/tests

Используют тот же API для provisioning, deterministic reproduction, stress,
soak и artifact verification. Они не являются второй реализацией runtime.

## 12. Parser/lowering

BSL исполняет платформа, но notebook source требует безопасного lowering.

`ParserFrontend` — сменная граница. Parsergen остаётся выбранным strategic
candidate, ANTLR — fallback/differential oracle. Grammar/corpus work не блокирует
RDBG Controller.

Генерация Python-target является отдельным модулем parsergen, а не частью
`onec_runtime`, `ParserFrontend` или notebook lowering. Этот модуль принимает
канонические Parser IR/decision DAG и генерирует самодостаточный Python parser
runtime вместе с моделью semantic AST. Runtime подключает только опубликованный
generated artifact через стабильный adapter и не импортирует внутренности
генератора.

Python-target должен генерировать из grammar semantic AST model:

- разные Python-классы для alternatives;
- поля и source spans;
- semantic actions/bindings;
- transformations ordinary names → `Контекст.*`;
- controlled capture-root setters;
- перехват `Сообщить(...)` в per-cell output buffer.

Name binding и lowering выполняются одним семантическим проходом. Обязательный
контракт:

```text
свободное имя notebook     → persistent Контекст
КонтекстОтладки.X          → root текущего captured frame
экспортный вызов worker    → метод активного hot-reloadable worker instance
локал/параметр подпрограммы → локал/параметр BSL без переписывания
```

Каталог экспортов worker является входом binder'а: пользовательские процедуры и
функции нельзя принимать за поля `Контекст`, встроенные функции платформы или
методы значений. Вне CAPTURE `КонтекстОтладки` недоступен. На первом этапе bare
alias всего `КонтекстОтладки` запрещён; mutable frame objects меняются по live
reference, а присваивания roots проходят через controlled write-back. Перехват
`Сообщить` выполняется после binding и потому не реализуется текстовой подменой.

Preprocessing, lowering, printer и source-map contract принадлежат
`ParserFrontend`. В PrototypeProfile допустимо удалять строки, начинающиеся с
`#` и `&`; это spike-only preprocessing без production semantics и без обещания
точных source mappings.

## 13. Data plane

RDBG — control/debug plane и путь небольших значений. `ТаблицаЗначений` и
результаты запросов materialize в Python как snapshots. Для больших таблиц
выбирается отдельный bulk path после benchmark: local file, pipe, local HTTP или
columnar/binary format.

Materialization contract обязан определить fidelity для `Decimal`, дат,
`Неопределено`/`NULL`, ссылок, колонок смешанных типов и размеров данных.

## 14. Evidence и ближайшие gates

Уже доказанные capabilities перечислены в `RUNTIME-CONTEXT.md`; область proof
не расширяется автоматически на cluster, другой platform version или non-zero
frame.

Ближайший порядок:

1. цельный Jupyter E2E `MAIN → CAPTURE → debug → write-back → resume → final`;
2. reentrancy, nested stop behavior и operation correlation;
3. recovery после debug-cell/transport failures;
4. server hot reload и generation semantics;
5. per-cell `Сообщить` output;
6. server `to_df` proxy/materialization;
7. отдельный модуль генерации semantic Python-target, затем product lowering
   поверх его generated AST;
8. dedicated-target/request affinity для PrototypeProfile;
9. process/session isolation, lease/watchdog и fault injection для ProductProfile;
10. waterfall-оптимизация T0–T10: измерить каждый переход и изменить только
    подтверждённые узкие места;
11. prototype-хранилище использует `ReturnValuesReuse=DuringSession`, но перед
    каждым CAPTURE Controller обязательно перепривязывает его к живому
    `Контекст` server-kernel frame. `DuringRequest` отвергнут длительным
    прогоном: после удаления reusable value MAIN frame сохранял состояние, а
    независимый RDBG request получил пустую структуру. Короткий forced-eviction
    `MAIN → CAPTURE → resume → MAIN` E2E с profile
    `session-frame-rebind-v1` пройден;
12. отдельный server stress уже оптимизированного протокола на базе ЗУП:
    не менее 10 000 изменений и массовые
    `MAIN/CAPTURE/eval/resume`, user stops,
    planned errors, transport/Controller faults и несколько sessions с проверкой
    потерь, дублей, порядка, latency и RSS;
13. финальный часовой server-topology soak на базе ЗУП с heartbeat примерно
    раз в две минуты.

MCP не входит в текущую очередь реализации или spikes. Описанная выше MCP
boundary сохраняется только как архитектурный future scope и потребует нового
явного решения пользователя.

После функциональных gates сначала измеряется и оптимизируется обычный protocol.
Server stress и soak квалифицируют уже оптимизированный вариант и доказывают
разные свойства: stress повышает плотность событий и отказов, soak проверяет
длительную стабильность, heartbeat и отсутствие накопительной деградации. Уже
пройденные file/ManagedClient stress на 10 000 команд и часовой soak остаются
предварительным evidence, но не заменяют финальные прогоны на базе ЗУП.
Финальный server soak не подменяет функциональные gates, оптимизацию или stress
и поэтому выполняется последним.

Shared-cluster multi-user isolation остаётся future scope и не блокирует
PrototypeProfile. Текущие live server claims относятся к файловой базе в режиме
`ServerEmulation`, а не к production server cluster.

## 15. Superseded assumptions

- `Kernel.epf owns Context` → `RuntimeState owns Context`.
- Jupyter/MCP напрямую владеют RDBG → thin frontends через Controller.
- Один execution path → явные MAIN и CAPTURE channels.
- Instrumented CaptureLoop как core → ordinary debugger breakpoint.
- Capture как transaction → non-atomic mutation + journal.
- Один enum `MAIN/CAPTURED/ERROR` → `SessionState` + `StopReason`.
- Parsergen блокирует Runtime → сменный `ParserFrontend`.
- Research 8+ call protocol = product loop → отдельные verification/product
  protocols.
- Runtime заменяет EDT-MCP → Runtime дополняет discovery, EDT-MCP выполняет
  productionization.

## 16. Product kill-gate

После технических gates проводится A/B на одной дорогой investigation task:

```text
A: Agent + EDT-MCP + YAXUnit + conventional debugger
B: Agent + EDT-MCP + Interactive Runtime
```

Измеряются время до локализации и validated fix, число production edits,
restart/replay, повторных expensive computations и manual recovery. Если Runtime
не даёт заметного выигрыша на своём целевом классе задач, расширение compiler,
notebook и proxy surface пересматривается.

## 17. Исторические аналитические входы

Три независимых brainstorm-аудита сохранены как historical pre-canonical
inputs. Они анализировали ограниченный набор верхнеуровневых документов до
текущих live spikes и потому не задают актуальный evidence status:

- `docs/research/brainstorm/2026-08-12-architecture-evolution-audit.md`;
- `docs/research/brainstorm/2026-08-12-context-contradictions-audit.md`;
- `docs/research/brainstorm/2026-08-12-evidence-hypotheses-audit.md`.

Текущая степень доказанности определяется только `RUNTIME-CONTEXT.md` и
связанными research-артефактами. Полезные противоречия из аудитов переносятся в
этот документ явно; сами аудиты не являются нормативными решениями.
