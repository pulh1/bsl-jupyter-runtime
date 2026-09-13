"""Worker publication regressions retained after removing LSP source snapshots."""

import pytest

from onec_runtime.errors import (
    BslExecutionError,
    StaleWorkerGeneration,
    WorkerPromotionOutcomeUnknown,
)
from test_runtime_api import (
    _PinnedOperationController,
    _SemanticSnapshotFailureTarget,
    _common_module_catalog,
    _semantic_snapshot_runtime,
    _worker_module_unit,
)


@pytest.mark.parametrize('failure', ['staging', 'create', 'wire'])
def test_known_publication_failure_preserves_confirmed_active_generation(tmp_path, failure):
    catalog = _common_module_catalog('МодульА')
    target = _SemanticSnapshotFailureTarget()
    api = _semantic_snapshot_runtime(tmp_path, catalog, target=target)
    first = api.load_worker_modules(
        (_worker_module_unit('МодульА', 1, catalog),), common_modules=catalog,
    )
    target.failure = failure

    with pytest.raises(BslExecutionError):
        api.load_worker_modules(
            (_worker_module_unit('МодульА', 2, catalog),), common_modules=catalog,
        )

    assert api.worker_generation_handle is first
    assert api._worker_universe.active_handle is first
    assert api._worker_active_modules['модульа'].unit.revision == 1


def test_unknown_publication_outcome_keeps_runtime_quarantined(tmp_path):
    catalog = _common_module_catalog('МодульА')
    target = _SemanticSnapshotFailureTarget()
    api = _semantic_snapshot_runtime(tmp_path, catalog, target=target)
    first = api.load_worker_modules(
        (_worker_module_unit('МодульА', 1, catalog),), common_modules=catalog,
    )
    target.failure = 'unknown'

    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.load_worker_modules(
            (_worker_module_unit('МодульА', 2, catalog),), common_modules=catalog,
        )

    assert api.worker_generation_handle is first
    assert api._worker_active_modules == {}
    with pytest.raises(WorkerPromotionOutcomeUnknown):
        api.status()


def test_operation_pin_retains_old_generation_until_resume(tmp_path):
    catalog = _common_module_catalog('МодульА')
    api = _semantic_snapshot_runtime(tmp_path, catalog)
    api._controller = _PinnedOperationController()
    first = api.load_worker_modules(
        (_worker_module_unit('МодульА', 1, catalog),), common_modules=catalog,
    )
    api.execute_bsl('Результат = Capture();')
    assert api.operation_worker_generation is first
    second = api.load_worker_modules(
        (_worker_module_unit('МодульА', 2, catalog),), common_modules=catalog,
    )
    assert api._api_owned_worker_generation_handle is second
    with pytest.raises(StaleWorkerGeneration):
        api.release_worker_generation(first)
    retained = api._worker_universe._confirmed_live_inventory()
    assert retained is not None
    assert retained.manifest_sha256s == frozenset((
        first.manifest_sha256, second.manifest_sha256,
    ))

    api.resume_capture()

    assert api.operation_worker_generation is None
    released = api._worker_universe._confirmed_live_inventory()
    assert released is not None
    assert released.manifest_sha256s == frozenset((second.manifest_sha256,))
