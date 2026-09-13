"""Optional jupyter-lsp server discovery. Paths belong to the Jupyter server."""

import os
import shutil
import sys


def language_server_spec(manager):
    executable = os.environ.get("ONEC_BSL_LANGUAGE_SERVER", "bsl-language-server")
    resolved = shutil.which(executable)
    if not resolved:
        return {}
    return {"onec-bsl": {
        "version": 2,
        "argv": [sys.executable, "-m", "onec_runtime_jupyter.lsp_proxy",
                 "--", resolved],
        "languages": ["bsl"],
        "mime_types": ["text/x-bsl"],
        "display_name": "BSL (runtime context)",
        "requires_documents_on_disk": False,
    }}
