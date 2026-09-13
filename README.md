# bsl-jupyter-runtime

Интерактивный runtime для кода 1С:Предприятия: выполнение BSL с сохранением состояния сеанса, остановы внутри типовых методов, просмотр данных из Python и hot reload. В репозитории находятся runtime core, адаптер Jupyter, MCP-сервер и расширение VS Code.

| Компонент | Каталог |
|---|---|
| Runtime core и BSL-парсер | [`src/onec_runtime`](src/onec_runtime) |
| 1С-расширение и Worker | [`onec`](onec) |
| Jupyter и BSL-ячейки | [`packages/jupyter`](packages/jupyter) |
| MCP-сервер | [`packages/mcp`](packages/mcp) |
| VS Code | [`packages/vscode`](packages/vscode) |
| Демо для ЗУП КОРП 3.1.38.92 | [`notebooks/demo`](notebooks/demo/README.md) |
| Тестовые notebooks | [`tests/fixtures/notebooks`](tests/fixtures/notebooks) |

Имя репозитория отличается от текущих имён Python-пакетов `onec-interactive-runtime-core`, `onec-interactive-jupyter` и `onec-interactive-mcp`: их переименование не входит в этот перенос.

## Установка релиза

Скачайте файлы [релиза `v0.1.17`](https://github.com/pulh1/bsl-jupyter-runtime/releases/tag/v0.1.17) в одну папку. Для Python нужны версия 3.12+ и доступ к PyPI для обычных зависимостей. На Windows создайте окружение и установите нужные компоненты из скачанных файлов:

```powershell
py -3.12 -m venv .venv
$python = ".\.venv\Scripts\python.exe"
& $python -m pip install .\onec_interactive_runtime_core-0.1.17-py3-none-any.whl
& $python -m pip install .\onec_interactive_jupyter-0.1.17-py3-none-any.whl "jupyterlab>=4.1,<5"
& $python -m pip install .\onec_interactive_mcp-0.1.17-py3-none-any.whl
```

Core нужен обоим адаптерам. Для одного только core оставьте первую команду установки; для Jupyter или MCP добавьте соответствующую команду. Установите одинаковые версии core и Jupyter в окружениях сервера Jupyter и kernel, если они разделены. Готовый wheel Jupyter уже содержит frontend и не требует Node.js. Для диаграмм в демо установите `matplotlib>=3.11,<4`.

Для Jupyter зарегистрируйте kernel и запустите JupyterLab:

```powershell
& $python -m ipykernel install --user --name onec-bsl --display-name "1C BSL"
& $python -m jupyter lab
```

Начните с [демо-ноутбуков](notebooks/demo/README.md): в стартовой ячейке укажите установленную платформу 1С, строку подключения к отдельной ИБ и выгрузку исходников той же конфигурации. Дополнительное статическое дополнение BSL для JupyterLab устанавливается как `onec-interactive-jupyter[lsp]`; его настройка и внешний BSL Language Server описаны в [руководстве LSP](docs/jupyter-bsl-lsp.md).

MCP работает через отдельный foreground-сервис. После установки MCP wheel запустите `onec-runtime-service --workspace C:\path\to\workspace`, затем подключите MCP-клиент к `onec-runtime-mcp --workspace C:\path\to\workspace` по stdio. Используйте один и тот же каталог workspace для обоих процессов; сервис по умолчанию работает в режиме `observe`.

Для VS Code установите `bsl-notebook-0.1.3.vsix` через **Extensions: Install from VSIX** или `code --install-extension .\bsl-notebook-0.1.3.vsix`. Требуются VS Code 1.136+, расширения Microsoft Jupyter, Python/Pylance и `1c-syntax.language-1c-bsl`; подробности — в [инструкции расширения](packages/vscode/README.md). VSIX предоставляет статические функции для `%%bsl` в локальных notebooks; выполнение ячеек обеспечивают Jupyter и Python-пакеты выше.

Core wheel уже содержит `OnecInteractiveRuntime.cfe` и при обычном запуске устанавливает расширение в целевую ИБ автоматически. Для ручной установки возьмите одноимённый CFE из релиза, загрузите его в расширения конфигурации через Конфигуратор и примените изменения к ИБ. Затем задайте `extension_mode=ExtensionMode.MANUAL` в `RuntimeSessionConfig`; [manifest](src/onec_runtime/resources/extension/extension-manifest.json) из релиза позволяет сверить версию и SHA-256 CFE. Платформа 1С и сама ИБ в релиз не входят.

Для локальной разработки нужны Python 3.12+, `uv` и Windows с установленной 1С для живых сценариев. Unit-тесты и сборка Python-кода не требуют демобазы. Исходник `parsergen` для проверки BSL-парсера включён в [`tests/fixtures/parsergen`](tests/fixtures/parsergen/README.md), поэтому отдельный checkout не нужен. Из корня репозитория:

```powershell
uv sync --group dev
uv run python -m pytest tests/unit/test_demo_notebooks.py tests/unit/test_jupyter_bsl_fixture_notebook.py -q
uv run python tools/build_jupyter_bsl_fixture_notebook.py --check
```

Для расширения VS Code используйте `npm --prefix packages/vscode ci` и `npm --prefix packages/vscode run test:unit`. Порядок запуска демо и параметры демобазы описаны в [README notebooks](notebooks/demo/README.md). Живые тесты 1С запускаются отдельно и требуют локальной платформы, временной ИБ и явно включённых переменных окружения.

Каноническая [архитектура](docs/architecture/2026-08-12-canonical-runtime-architecture.md) и [интеграция BSL Language Server](docs/jupyter-bsl-lsp.md) описаны отдельно. Исходное состояние переноса: commit `da7a6195c6d321890cb87b14f3b0156d62c905e2`; состав переноса записан в [MIGRATION.md](MIGRATION.md).

Собственный код распространяется по `GPL-3.0-only`; лицензии и уведомления сторонних компонентов находятся в [LICENSE](LICENSE) и [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
