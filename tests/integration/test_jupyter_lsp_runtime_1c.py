"""Explicit opt-in exact-target source-LSP/runtime budget; no fixture DB install."""
import asyncio
import os
from pathlib import Path

import pytest


@pytest.mark.live_1c
@pytest.mark.timeout(21600)
def test_exact_live_source_lsp_runtime_budget(tmp_path, monkeypatch):
    if os.environ.get('ONEC_RUN_JUPYTER_LSP_INTEGRATION') != '1':
        pytest.skip('exact source-LSP live benchmark is opt-in only')
    required = ('ONEC_BSL_LANGUAGE_SERVER', 'ONEC_JUPYTER_LSP_INITIAL_INDEX_PROOF', 'ONEC_JUPYTER_LSP_PROTOCOL_PROOF')
    if any(not os.environ.get(name) for name in required):
        pytest.fail('explicit native executable and final all-layout proof artifacts are required')
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    from tools.check_jupyter_lsp_zup import run_live
    result = asyncio.run(run_live(Path(os.environ[required[0]]), tmp_path,
                                 Path(os.environ[required[1]]), Path(os.environ[required[2]])))
    assert result['status'] == result['budget_status'] == 'PASS'
    assert result['database_file_identity_stable'] and result['source_tree_unchanged']
    assert result['remaining_owned_target_processes'] == 0
