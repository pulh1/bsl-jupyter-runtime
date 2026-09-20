"""Jupyter adapter for the 1C interactive runtime."""

from onec_runtime_jupyter.extension import (
    MACHINE_MIME_TYPE,
    BslCellError,
    NotebookDisplay,
    NotebookDisplayConfig,
    OnecRuntimeMagics,
    install_runtime,
    load_ipython_extension,
    unload_ipython_extension,
)
from onec_runtime_jupyter.session import InteractiveRuntimeSession

__version__ = "0.1.21"


def _jupyter_labextension_paths():
    return [{"src": "labextension", "dest": "@onec-interactive/jupyter-bsl"}]

__all__ = [
    "InteractiveRuntimeSession",
    "MACHINE_MIME_TYPE",
    "BslCellError",
    "NotebookDisplay",
    "NotebookDisplayConfig",
    "OnecRuntimeMagics",
    "install_runtime",
    "load_ipython_extension",
    "unload_ipython_extension",
]
