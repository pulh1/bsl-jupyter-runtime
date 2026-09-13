# Jupyter adapter

This package owns `%%bsl`, `%bsl_resume`, Python value proxies, kernel lifecycle, and the JupyterLab frontend. Keep execution and capture semantics delegated to `onec_runtime`. A BSL cell that starts a capture must leave the operation available to later Python and BSL cells.

When changing notebook behavior, check `tests/unit/test_jupyter_adapter.py`, the related Jupyter unit tests, and the generated fixture notebook contract. Frontend changes need their own build or tests. Reader examples live in `notebooks/demo`; test acceptance notebooks live in `tests/fixtures/notebooks`.
