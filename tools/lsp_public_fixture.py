"""Small public-session fixture for the optional Jupyter LSP browser checks.

The browser checks concern source-root visibility and notebook bindings, so
their fake execution owner confirms Worker generations without a 1C process.
"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from onec_runtime.bsl import (
    SourceUnitKind, SourceUnitRef, WorkerModuleUnit, mapped_visible_source,
    source_sha256,
)
from onec_runtime.config import RuntimeConfig
from onec_runtime.runtime_models import (
    OperationState, RuntimeNamespaceSnapshot, RuntimeReply, RuntimeReplyKind,
    RuntimeStatus,
)
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from onec_runtime.worker_universe import WorkerGenerationHandle


class LspFailureTarget:
    """Choose a confirmed rejection or an unknown reply in a test fixture."""

    def __init__(self) -> None:
        self.failure: str | None = None


class _Closeable:
    def close(self) -> None:
        pass


class _IdleRdbg:
    pass


class _LspExecutionFacade:
    def __init__(self, target: LspFailureTarget) -> None:
        self._target = target
        self._generation = 0
        self._handle: WorkerGenerationHandle | None = None
        self._units: dict[WorkerGenerationHandle, tuple[WorkerModuleUnit, ...]] = {}

    @contextmanager
    def execution_caller_handoff(self, _release):
        yield

    def execute_bsl(self, _source: str, **_kwargs) -> RuntimeReply:
        return RuntimeReply(RuntimeReplyKind.MAIN_COMPLETED, 1, OperationState.COMPLETED)

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(OperationState.IDLE, 1, 0, self._handle)

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot:
        return RuntimeNamespaceSnapshot(1, 1, ())

    def load_worker_modules(self, units, **_kwargs) -> WorkerGenerationHandle:
        if self._target.failure == "unknown":
            raise TimeoutError("test fixture lost the publication reply")
        if self._target.failure:
            raise RuntimeError("test fixture rejected Worker publication")
        self._generation += 1
        digest = sha256(repr(tuple((u.logical_name, u.revision) for u in units)).encode()).hexdigest()
        handle = WorkerGenerationHandle(1, 1, self._generation, digest)
        self._handle = handle
        self._units[handle] = units
        return handle

    def confirmed_worker_module_units(self, handle) -> tuple[WorkerModuleUnit, ...]:
        return self._units[handle]

    def release_worker_generation(self, _handle) -> None:
        pass

    def try_heartbeat_ticket(self):
        return None

    def close(self) -> None:
        pass


def worker_module_source_unit(
    logical_name: str, revision: int, source: str,
) -> WorkerModuleUnit:
    reference = SourceUnitRef(
        SourceUnitKind.MODULE, logical_name, revision, source_sha256(source),
    )
    return WorkerModuleUnit(
        logical_name, "module", revision, mapped_visible_source(source, reference),
    )


def make_lsp_session(
    work_root: Path, source_root: Path | None, target: LspFailureTarget,
) -> RuntimeSession:
    """Create a real public RuntimeSession with an inert test execution owner."""
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    workspace = work_root / f"runtime-{uuid4().hex}"
    platform_bin = workspace / "bin"
    platform_bin.mkdir(parents=True)
    for executable in ("1cv8.exe", "1cv8c.exe", "dbgs.exe"):
        (platform_bin / executable).touch()
    infobase = workspace / "base"
    infobase.mkdir()
    (infobase / "1Cv8.1CD").write_bytes(b"test-infobase")
    runtime = RuntimeConfig(
        workspace, platform_bin, connection_string=f'File="{infobase}";',
    )
    config = RuntimeSessionConfig(
        runtime, workspace / "evidence", source_root=source_root,
    )
    return RuntimeSession(
        config, _Closeable(), _Closeable(), _IdleRdbg(),
        _LspExecutionFacade(target), object(),
    )


def reload_code(method, revision, parameters="Первый", failure=None):
    """Notebook source that tests public Worker generation publication."""
    source = f"Функция {method}({parameters}) Экспорт\nВозврат 1;\nКонецФункции\n"
    call = (
        '_lsp_runtime.load_worker_modules('
        f'(_worker_module_source_unit("JupyterBslFixtureCalleeServer", {revision}, {source!r}),))'
    )
    if failure:
        return (
            f"_lsp_previous = _lsp_runtime.status().worker_generation\n"
            f"_lsp_target.failure = {failure!r}\n"
            f"try:\n    {call}\n"
            "except Exception:\n    pass\n"
            'else:\n    raise AssertionError("Expected confirmation failure")\n'
            + (
                "assert _lsp_runtime.status().worker_generation is _lsp_previous\n"
                if failure != "unknown" else ""
            )
            + "_lsp_target.failure = None"
        )
    return (
        f"_lsp_handle = {call}\n"
        "assert _lsp_runtime.status().worker_generation is _lsp_handle\n"
        "_lsp_runtime.release_worker_generation(_lsp_handle)"
    )
