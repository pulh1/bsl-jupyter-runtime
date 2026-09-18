"""Confirmed notebook names and immutable route preparation views.

The namespace owner publishes names only after a confirmed cell outcome.
CAPTURE lowering may additionally use the suspended MAIN command's speculative
catalog; passing that catalog to :meth:`snapshot` never publishes it.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock

from onec_runtime.execution.snapshot_binding import RoutePreparationSnapshot
from onec_runtime.execution.worker_activation import WorkerActivationSnapshot


class RuntimeNamespaceOwner:
    """Version namespace and one atomic Worker snapshot for cell admission."""

    def __init__(
        self,
        runtime_generation: int,
        context_generation: int,
        *,
        worker_snapshot: Callable[[], WorkerActivationSnapshot],
        initial_names: tuple[str, ...] = (),
    ) -> None:
        if type(runtime_generation) is not int or runtime_generation <= 0:
            raise ValueError("runtime generation must be positive")
        if type(context_generation) is not int or context_generation <= 0:
            raise ValueError("context generation must be positive")
        if not callable(worker_snapshot):
            raise TypeError("Worker snapshot reader is required")
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._worker_snapshot = worker_snapshot
        self._lock = RLock()
        self._names: tuple[str, ...] = ()
        self._namespace_revision = 0
        self._snapshot_version = 0
        self._snapshot_key: tuple[int, int, tuple[str, ...]] | None = None
        self.publish_additions(initial_names)

    def publish_additions(self, names: tuple[str, ...]) -> None:
        """Merge a confirmed full catalog, retaining the first spelling."""

        _validate_names(names)
        with self._lock:
            merged = _merge_names(self._names, names)
            if merged != self._names:
                self._names = merged
                self._namespace_revision += 1

    def snapshot(
        self, *, speculative_names: tuple[str, ...] = (),
    ) -> RoutePreparationSnapshot:
        """Read one Worker generation and a route-local namespace view.

        A change to confirmed names, the Worker generation, or speculative
        names advances the monotonic version used by controller admission.
        """

        _validate_names(speculative_names)
        with self._lock:
            worker = self._worker_snapshot()
            if not isinstance(worker, WorkerActivationSnapshot):
                raise TypeError("Worker snapshot reader returned an invalid value")
            names = _merge_names(self._names, speculative_names)
            key = (self._namespace_revision, worker.revision, names)
            if key != self._snapshot_key:
                self._snapshot_version += 1
                self._snapshot_key = key
            return RoutePreparationSnapshot(
                self,
                self._snapshot_version,
                names,
                worker.worker_exports,
                worker.active_methods,
                worker.active_handle,
            )

    def namespace_snapshot(self) -> object:
        """Return only confirmed public names and this runtime's identity."""

        from onec_runtime.runtime_models import RuntimeNamespaceSnapshot

        with self._lock:
            return RuntimeNamespaceSnapshot(
                self._runtime_generation,
                self._context_generation,
                self._names,
            )


def _validate_names(names: tuple[str, ...]) -> None:
    if type(names) is not tuple or any(
        not isinstance(name, str) or not name for name in names
    ):
        raise ValueError("namespace catalog must be an immutable tuple of names")


def _merge_names(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    merged: dict[str, str] = {}
    for name in (*first, *second):
        merged.setdefault(name.casefold(), name)
    return tuple(merged.values())
