"""Adapt RuntimeSession value arguments to the fenced value router."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

import pandas as pd

from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.execution.local_wait import validate_local_wait_timeout
from onec_runtime.table_materialization import ReferenceMode, ReferencePolicy
from onec_runtime.value_materialization import MaterializationOptions

_T = TypeVar("_T")


class _DynamicValueRouter(Protocol):
    def materialize(
        self,
        handle: str,
        options: MaterializationOptions | None = None,
        *,
        table_policy: ReferencePolicy | None = None,
        timeout_s: float | None = None,
    ) -> object: ...

    def to_df(
        self,
        handle: str,
        policy: ReferencePolicy | None = None,
        *,
        max_rows: int,
        max_bytes: int,
    ) -> pd.DataFrame: ...


class SessionValueMaterializationAdapter:
    """Keep supported Session arguments on the single-owner value route.

    The route transfers one bounded payload. ``chunk_size`` is validated as
    an advisory hint; it does not split that payload or control RDBG reads.
    ``profiler`` times the whole routed operation rather than its internal
    transfer and decode stages. ``timeout_s`` bounds the initiating caller's
    ticket wait only; it does not end remote execution.
    """

    _TABLE_MAX_ROWS = 100_000
    _TABLE_MAX_BYTES = 64 * 1024 * 1024

    def __init__(self, router: _DynamicValueRouter) -> None:
        self._router = router

    def to_df(
        self,
        handle: str,
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int | None = None,
        profiler: PhaseRecorder | None = None,
    ) -> pd.DataFrame:
        """Copy a table with fixed public row and byte ceilings.

        Runtime extension 0.1.11 / protocol 5 rejects a query result that
        exceeds the row ceiling instead of returning a truncated prefix.
        Older extension versions are not compatible with this guarantee.
        """

        _validate_transfer_options(chunk_size=chunk_size, profiler=profiler)
        policy = _reference_policy(refs, ref_columns, uuid_suffix)
        return _profile(
            profiler, "table.routed_transfer",
            lambda: self._router.to_df(
                handle, policy,
                max_rows=self._TABLE_MAX_ROWS,
                max_bytes=self._TABLE_MAX_BYTES,
            ),
        )

    def materialize(
        self,
        handle: str,
        *,
        refs: str | ReferenceMode = ReferenceMode.PRESENTATION,
        ref_columns: dict[str, str | ReferenceMode] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int | None = None,
        max_depth: int = 32,
        max_items: int = 100_000,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_s: float | None = None,
        profiler: PhaseRecorder | None = None,
    ) -> object:
        """Materialize a direct value or table with supported public options."""

        _validate_transfer_options(
            chunk_size=chunk_size, profiler=profiler, timeout_s=timeout_s,
        )
        policy = _reference_policy(refs, ref_columns, uuid_suffix)
        options = MaterializationOptions(
            refs=ReferenceMode(policy.refs).value,
            max_depth=max_depth,
            max_items=max_items,
            max_bytes=max_bytes,
        )
        return _profile(
            profiler, "materialization.routed_transfer",
            lambda: self._router.materialize(
                handle, options, table_policy=policy, timeout_s=timeout_s,
            ),
        )

    def materialize_value(self, handle: str, **options: object) -> object:
        """Keep the frontend proxy alias for dynamic materialization."""

        return self.materialize(handle, **options)

    def materialize_table(self, handle: str, **options: object) -> pd.DataFrame:
        """Keep RuntimeSession's current to_df delegation name."""

        return self.to_df(handle, **options)


def _validate_transfer_options(
    *, chunk_size: int | None, profiler: PhaseRecorder | None,
    timeout_s: float | None = None,
) -> None:
    if chunk_size is not None and (type(chunk_size) is not int or chunk_size <= 0):
        raise ValueError("chunk_size must be a positive integer")
    if profiler is not None and not isinstance(profiler, PhaseRecorder):
        raise TypeError("profiler must be a PhaseRecorder")
    validate_local_wait_timeout(timeout_s)


def _profile(
    profiler: PhaseRecorder | None, phase: str, operation: Callable[[], _T],
) -> _T:
    if profiler is None:
        return operation()
    return profiler.measure(phase, operation)


def _reference_policy(
    refs: str | ReferenceMode,
    ref_columns: dict[str, str | ReferenceMode] | None,
    uuid_suffix: str,
) -> ReferencePolicy:
    try:
        mode = ReferenceMode(refs).value
    except (TypeError, ValueError) as error:
        raise ValueError("unknown reference mode") from error
    if ref_columns is not None and not isinstance(ref_columns, dict):
        raise TypeError("ref_columns must be a dictionary")
    overrides: dict[str, str] | None = None
    if ref_columns is not None:
        overrides = {}
        for column, selected in ref_columns.items():
            if not isinstance(column, str) or not column:
                raise ValueError("reference column name must be non-empty")
            try:
                overrides[column] = ReferenceMode(selected).value
            except (TypeError, ValueError) as error:
                raise ValueError("unknown reference mode for column") from error
    if not isinstance(uuid_suffix, str) or not uuid_suffix:
        raise ValueError("uuid_suffix must be a non-empty string")
    return ReferencePolicy(mode, overrides, uuid_suffix)
