# BSL Jupyter Runtime

Этот проект позволяет выполнять код 1С:Предприятия (BSL) в Jupyter-ноутбуках прямо в VS Code и работать с результатами в Python. BSL-ячейки используют один сеанс 1С и сохраняют его состояние: можно вызвать метод конфигурации, получить таблицу значений как `pandas.DataFrame`, остановить выполнение внутри типового метода и посмотреть его данные. Для эксперимента с изменением метода можно загрузить его изменённую локальную копию без обновления конфигурации ИБ.

Например, [обзорный notebook ЗУП](notebooks/demo/ZUP/01-overview.ipynb) получает плановый ФОТ сотрудников типовым методом, показывает таблицу и диаграмму в Python, останавливается перед возвратом из метода и затем пересчитывает ФОТ с условными страховыми взносами через hot reload. Все шаги выполняются в одном интерактивном сеансе. [Второй notebook ЗУП](notebooks/demo/ZUP/03-capture.ipynb) подробнее показывает две точки останова, стек вызовов и временную таблицу. [Демо УТ](notebooks/demo/UT/05-ut-sales.ipynb) исследует выручку и меняет расчёт процента прироста через hot reload.

## Быстрый старт в VS Code

Нужны Windows, Python 3.12+, VS Code 1.136+, установленная платформа 1С, **отдельная копия ИБ** и локальная копия выгрузки исходников той же конфигурации. По умолчанию runtime устанавливает в ИБ служебное расширение. Демо проверены на платформе **8.5.1.1529**: ЗУП — с **ЗУП КОРП 3.1.38.92** и снимком данных от **01.08.2021**, УТ — с **УТ 11.6.1.61** (дата данных определяется из ИБ). Платформа, демобазы и выгрузки в пакет не входят.

Скачайте **Source code (zip)** из [релиза v0.1.21](https://github.com/pulh1/bsl-jupyter-runtime/releases/tag/v0.1.21) и распакуйте архив: в нём находятся `notebooks/demo`. Из [релиза v0.1.18](https://github.com/pulh1/bsl-jupyter-runtime/releases/tag/v0.1.18) скачайте `bsl-notebook-0.1.4.vsix`. VSIX отвечает за подсказки BSL в редакторе; Python-пакеты версии 0.1.21 устанавливаются из PyPI.

В PowerShell из папки распакованного проекта создайте окружение, установите пакет и зарегистрируйте kernel:

```powershell
py -3.12 -m venv .venv
$python = ".\.venv\Scripts\python.exe"
& $python -m pip install "onec-interactive-jupyter==0.1.21" "ipykernel>=6.29,<7" "matplotlib>=3.11,<4"
& $python -m ipykernel install --user --name onec-bsl --display-name "1C BSL"
```

`onec-interactive-jupyter` установит совместимый `onec-interactive-runtime-core` и его служебное расширение 1С. `matplotlib` нужен для диаграмм в демо.

Установите расширения VS Code из Marketplace:

- [Python](https://marketplace.visualstudio.com/items?itemName=ms-python.python) (`ms-python.python`);
- [Jupyter](https://marketplace.visualstudio.com/items?itemName=ms-toolsai.jupyter) (`ms-toolsai.jupyter`);
- [Pylance](https://marketplace.visualstudio.com/items?itemName=ms-python.vscode-pylance) (`ms-python.vscode-pylance`; обычно устанавливается вместе с Python);
- [Language 1C (BSL)](https://marketplace.visualstudio.com/items?itemName=1c-syntax.language-1c-bsl) (`1c-syntax.language-1c-bsl`).

В VS Code выполните **Extensions: Install from VSIX** и укажите скачанный `bsl-notebook-0.1.4.vsix`. Откройте папку с выгрузкой исходников 1С через **File → Open Folder**, затем добавьте распакованный проект через **File → Add Folder to Workspace**. Выгрузка должна быть первой папкой рабочего пространства. В notebook выберите kernel **1C BSL** (при необходимости через **Select Another Kernel → Jupyter Kernels**) и выполните команду **1C BSL: Выбрать исходники проекта**, указав выгрузку.

Для первой пробы создайте пустой Python-notebook. В первой ячейке запустите сеанс, подставив пути к своей платформе, **копии** ИБ и **копии** исходников:

```python
from onec_runtime.config import RuntimeConfig
from onec_runtime.session import ExtensionMode, RuntimeSessionConfig
from onec_runtime_jupyter import InteractiveRuntimeSession

runtime = InteractiveRuntimeSession.start(
    RuntimeSessionConfig(
        runtime=RuntimeConfig(
            platform_bin=r'C:\Program Files\1cv8\8.5.1.1529\bin',
            connection_string=r'File="C:\demo\ZUP-copy";',
            # При необходимости добавьте username="...", password="...".
        ),
        source_root=r'C:\demo\ZUP-source-copy',
        extension_mode=ExtensionMode.AUTO,
    )
)
```

Во второй ячейке выполните BSL:

```python
%%bsl
Сообщить("Привет, мир!");
```

После опыта закройте сеанс в Python-ячейке: `runtime.close()`.

Методы `runtime`, объекты результатов, CAPTURE-остановки и ленивые Python-прокси описаны в [руководстве по Python API](docs/python-api.md).

Если хотите управлять расширением вручную, сначала найдите CFE из установленного пакета:

```powershell
& $python -c "from importlib.resources import files; print(files('onec_runtime').joinpath('resources/extension/OnecInteractiveRuntime.cfe'))"
```

Загрузите этот CFE через Конфигуратор в копию ИБ, отключите у расширения **«Безопасный режим»** и примените изменения к ИБ. Затем в стартовой Python-ячейке используйте `extension_mode=ExtensionMode.MANUAL` вместо `ExtensionMode.AUTO`. Runtime проверит совместимость установленного расширения при запуске.

Для готового сценария откройте [обзор ЗУП](notebooks/demo/ZUP/01-overview.ipynb), [capture ЗУП](notebooks/demo/ZUP/03-capture.ipynb) или [продажи УТ](notebooks/demo/UT/05-ut-sales.ipynb). В их стартовой ячейке замените `PLATFORM_BIN`, `CONNECTION_STRING` и `SOURCE_ROOT` на свои значения и запускайте ячейки по порядку; второй сеанс из примера выше создавать не нужно. Выбор исходников в редакторе не заменяет `SOURCE_ROOT` в notebook. Номера строк точек останова заданы для указанных версий конфигураций. Для hot reload меняйте только локальную копию исходников. Подготовка баз и сценарии подробнее описаны в [руководстве по демо](notebooks/demo/README.md).

## Компоненты

| Компонент | Назначение |
|---|---|
| [`src/onec_runtime`](src/onec_runtime) | Runtime core: сеанс 1С, выполнение BSL, состояние, остановы, hot reload и BSL-парсер. |
| [`onec`](onec) | Расширение 1С и Worker для выполнения кода в ИБ. |
| [`packages/jupyter`](packages/jupyter) | Интеграция со стандартным Python kernel и `%%bsl`-ячейки. |
| [`packages/vscode`](packages/vscode) | Предварительная версия расширения VS Code со статической поддержкой BSL в локальных notebooks. |

Имена распространяемых Python-пакетов — `onec-interactive-runtime-core` и `onec-interactive-jupyter`; они отличаются от имени репозитория. В VS Code ячейки выполняет локальный Python kernel из созданного окружения; VSIX отвечает за статическую поддержку BSL в редакторе.

## Поддержка BSL в редакторах

VSIX добавляет подсветку, автодополнение, сигнатуры, hover и переход к определениям для `%%bsl` в локальных notebooks. Это **предварительная версия**; настройка и ограничения перечислены в [инструкции VS Code](packages/vscode/README.md). Для статических подсказок BSL в JupyterLab есть отдельное [руководство по BSL Language Server](docs/jupyter-bsl-lsp.md).

## Расширение 1С

Core wheel содержит `OnecInteractiveRuntime.cfe` и при обычном запуске автоматически устанавливает его в целевую ИБ. Вариант с ручной установкой и `ExtensionMode.MANUAL` показан в «Быстром старте». Версию и SHA-256 CFE можно сверить по [manifest](src/onec_runtime/resources/extension/extension-manifest.json), который также входит в пакет.

## Разработка

Для работы с исходниками нужны Python 3.12+ и `uv`; живые сценарии требуют установленной 1С. Unit-тесты не требуют демобазы:

```powershell
uv sync --group dev
uv run python -m pytest tests/unit -q
```

Проверки генератора парсера используют включённый в репозиторий [тестовый исходник](tests/fixtures/parsergen/README.md). Архитектура описана [отдельно](docs/architecture/2026-08-12-canonical-runtime-architecture.md).

Собственный код распространяется по `GPL-3.0-only`; лицензии и уведомления сторонних компонентов находятся в [LICENSE](LICENSE) и [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
