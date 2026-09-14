# BSL Jupyter Runtime

Этот проект позволяет выполнять код 1С:Предприятия (BSL) в Jupyter и работать с результатами в Python. BSL-ячейки используют один сеанс 1С и сохраняют его состояние: можно вызвать метод конфигурации, получить таблицу значений как `pandas.DataFrame`, остановить выполнение внутри типового метода и посмотреть его данные. Для эксперимента с изменением метода можно загрузить его изменённую локальную копию без обновления конфигурации ИБ.

Например, [обзорный notebook](notebooks/demo/01-overview.ipynb) получает плановый ФОТ сотрудников типовым методом ЗУП КОРП, показывает таблицу и диаграмму в Python, останавливается перед возвратом из метода и затем пересчитывает ФОТ с условными страховыми взносами через hot reload. Все шаги выполняются в одном интерактивном сеансе. [Второй notebook](notebooks/demo/03-capture.ipynb) подробнее показывает две точки останова, стек вызовов и временную таблицу.

## Быстрый старт

Команды ниже рассчитаны на Windows и Python 3.12+; для установки обычных зависимостей нужен доступ к PyPI. Для выполнения BSL нужны установленная платформа 1С и доступ к **отдельной копии ИБ**: runtime устанавливает в неё служебное расширение. Демо-ноутбуки проверены с **ЗУП КОРП 3.1.38.92** на платформе **8.5.1.1529**. Для них также нужна выгрузка исходников этой же конфигурации. Платформа, демобаза и выгрузка в релиз не входят.

Скачайте из [релиза `v0.1.18`](https://github.com/pulh1/bsl-jupyter-runtime/releases/tag/v0.1.18) архив **Source code (zip)** и два wheel-файла: `onec_interactive_runtime_core-0.1.18-py3-none-any.whl` и `onec_interactive_jupyter-0.1.18-py3-none-any.whl`. Распакуйте архив, положите оба wheel в корень распакованного проекта и откройте там PowerShell. Архив содержит `notebooks/demo`; версии core и Jupyter должны совпадать.

```powershell
py -3.12 -m venv .venv
$python = ".\.venv\Scripts\python.exe"
& $python -m pip install `
    .\onec_interactive_runtime_core-0.1.18-py3-none-any.whl `
    .\onec_interactive_jupyter-0.1.18-py3-none-any.whl `
    "jupyterlab>=4.1,<5" "matplotlib>=3.11,<4"
& $python -m ipykernel install --user --name onec-bsl --display-name "1C BSL"
& $python -m jupyterlab notebooks/demo
```

В JupyterLab откройте `01-overview.ipynb` и выберите kernel **1C BSL**. В стартовой Python-ячейке укажите путь к `bin` установленной платформы (`PLATFORM_BIN`), строку подключения к ИБ (`CONNECTION_STRING`) и путь к **локальной копии** выгрузки (`SOURCE_ROOT`). При необходимости измените имя пользователя и `EXTENSION_MODE`. Затем запускайте ячейки по порядку: первые BSL-ячейки получат данные, а Python-ячейки покажут их через `ПланФОТ.to_df(refs='presentation')`.

Для полного прохождения обзора проверьте `CAPTURE_LINE` по своей выгрузке перед установкой точки останова. В разделе hot reload notebook предлагает изменить метод в локальной копии модуля из `SOURCE_ROOT`; не правьте исходную выгрузку. Последняя ячейка закрывает сеанс через `runtime.close()`. Подготовка демобазы и оба сценария подробнее описаны в [руководстве по демо](notebooks/demo/README.md).

## Компоненты

| Компонент | Назначение |
|---|---|
| [`src/onec_runtime`](src/onec_runtime) | Runtime core: сеанс 1С, выполнение BSL, состояние, остановы, hot reload и BSL-парсер. |
| [`onec`](onec) | Расширение 1С и Worker для выполнения кода в ИБ. |
| [`packages/jupyter`](packages/jupyter) | Интеграция со стандартным Python kernel, `%%bsl`-ячейки и frontend JupyterLab. |
| [`packages/vscode`](packages/vscode) | Предварительная версия расширения VS Code со статической поддержкой BSL в локальных notebooks. |

Имена распространяемых Python-пакетов — `onec-interactive-runtime-core` и `onec-interactive-jupyter`; они отличаются от имени репозитория. Готовый wheel Jupyter уже содержит frontend и не требует Node.js. Если JupyterLab и kernel работают в разных окружениях, установите одинаковые версии обоих wheels в каждое окружение.

## Поддержка BSL в редакторах

Для статического дополнения BSL в JupyterLab можно отдельно подключить BSL Language Server; установка и настройка описаны в [руководстве LSP](docs/jupyter-bsl-lsp.md). Выполнение BSL не зависит от этого дополнения.

Расширение VS Code из релиза (`bsl-notebook-0.1.4.vsix`) добавляет подсветку, автодополнение, сигнатуры, hover и переход к определениям для `%%bsl` в локальных notebooks. Это **предварительная версия**; выполнение ячеек остаётся за Jupyter и Python-пакетами. Установите VSIX через **Extensions: Install from VSIX** в VS Code 1.136+; также нужны расширения Microsoft Jupyter, Python/Pylance и `1c-syntax.language-1c-bsl`. Настройка и ограничения перечислены в [инструкции VS Code](packages/vscode/README.md).

## Расширение 1С

Core wheel содержит `OnecInteractiveRuntime.cfe` и при обычном запуске автоматически устанавливает его в целевую ИБ. Если расширение нужно установить вручную, возьмите CFE из того же релиза, загрузите его через Конфигуратор, примените изменения к ИБ и задайте `extension_mode=ExtensionMode.MANUAL` в `RuntimeSessionConfig`. Версию и SHA-256 CFE можно сверить по [manifest](src/onec_runtime/resources/extension/extension-manifest.json) из релиза.

## Разработка

Для работы с исходниками нужны Python 3.12+ и `uv`; живые сценарии требуют установленной 1С. Unit-тесты не требуют демобазы:

```powershell
uv sync --group dev
uv run python -m pytest tests/unit -q
```

Проверки генератора парсера используют включённый в репозиторий [тестовый исходник](tests/fixtures/parsergen/README.md). Архитектура описана [отдельно](docs/architecture/2026-08-12-canonical-runtime-architecture.md), а сведения о первоначальном переносе сохранены в [MIGRATION.md](MIGRATION.md).

Собственный код распространяется по `GPL-3.0-only`; лицензии и уведомления сторонних компонентов находятся в [LICENSE](LICENSE) и [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
