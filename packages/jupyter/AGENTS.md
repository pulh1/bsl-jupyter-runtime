# Jupyter adapter

This package owns `%%bsl`, `%bsl_resume`, `%bsl_status`, Python value proxies, kernel completion, kernel lifecycle, and the JupyterLab frontend. Delegate MAIN/CAPTURE routing, Worker publication, RDBG access, and value transfer to `onec_runtime`. A BSL cell that starts a capture must leave the operation available to later Python and BSL cells.

Use `e1cRuntimeКонтекст.<name>` for persistent notebook values and `КонтекстОтладки` for the stopped CAPTURE frame. Synchronize Python `bsl` proxies from the runtime namespace snapshot on installation and after successful replies; validate their generations and symbolic handles before reading values. Kernel completion for BSL context fields uses the runtime completion API, while the JupyterLab frontend handles editor features.

When changing notebook behavior, check `tests/unit/test_jupyter_adapter.py`, the relevant `test_jupyter_*` cases, and the generated fixture notebook contract. Frontend changes need their own build or tests. Reader examples live in `notebooks/demo`; test acceptance notebooks live in `tests/fixtures/notebooks`.
