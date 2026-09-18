from __future__ import annotations

from onec_runtime.execution.namespace import RuntimeNamespaceOwner
from onec_runtime.execution.worker_activation import WorkerActivationSnapshot


def test_namespace_owner_merges_confirmed_names_and_invalidates_snapshot() -> None:
    worker = [WorkerActivationSnapshot(0, (), None, None)]
    owner = RuntimeNamespaceOwner(12, 3, worker_snapshot=lambda: worker[0])

    initial = owner.snapshot()
    assert initial.namespace_names == ()
    assert owner.namespace_snapshot().names == ()

    owner.publish_additions(("Документ", "Сумма"))
    confirmed = owner.snapshot()
    assert confirmed.owner is owner
    assert confirmed.version > initial.version
    assert confirmed.namespace_names == ("Документ", "Сумма")
    assert owner.namespace_snapshot().names == ("Документ", "Сумма")

    owner.publish_additions(("документ", "Итого"))
    assert owner.snapshot().namespace_names == ("Документ", "Сумма", "Итого")
    assert owner.namespace_snapshot().runtime_generation == 12
    assert owner.namespace_snapshot().context_generation == 3


def test_speculative_main_names_are_route_local_and_never_public() -> None:
    owner = RuntimeNamespaceOwner(
        1, 1, worker_snapshot=lambda: WorkerActivationSnapshot(0, (), None, None)
    )
    before = owner.snapshot()
    speculative = owner.snapshot(speculative_names=("НоваяПеременная",))

    assert speculative.namespace_names == ("НоваяПеременная",)
    assert speculative.version > before.version
    assert owner.namespace_snapshot().names == ()
    assert owner.snapshot().namespace_names == ()


def test_worker_revision_and_methods_are_one_snapshot_version() -> None:
    worker = [WorkerActivationSnapshot(0, (), None, None)]
    owner = RuntimeNamespaceOwner(1, 1, worker_snapshot=lambda: worker[0])
    before = owner.snapshot()
    worker[0] = WorkerActivationSnapshot(1, (), None, None)

    after = owner.snapshot()

    assert after.version > before.version
    assert after.worker_exports == worker[0].worker_exports
    assert after.previous_methods is worker[0].active_methods
    assert owner.snapshot().version == after.version
