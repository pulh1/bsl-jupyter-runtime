"""Saved stack snapshots and local syntax enrichment.

LocalStackAdapter is an internal integration seam, not a CaptureView. Its backend
must validate the exact stop fence and inspection state under the owning session
and runtime locks for every fresh inventory. Worker source resolution must use
the physical frame's owning artifact/generation, never the current name inventory.
Enrichment needs neither the backend nor a live capture and does not change state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from hashlib import sha256
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Protocol, cast, overload
from uuid import UUID, uuid4

from onec_runtime.bsl.full_ast_worker_projection import (
    full_ast_parser_identity, parse_full_ast_module,
)
from onec_runtime.bsl.module_syntax import (
    MethodSyntaxInfo, ModuleIdentity, ModuleSyntaxIndex, ModuleSyntaxRegistry,
)
from onec_runtime.bsl.worker_projection_model import ParsedModuleModel
from onec_runtime.capture_source import (
    CaptureModuleSource, CaptureSourceCatalog, CaptureSourceChangedError,
    SourceVersionRef,
)
from onec_runtime.configuration_source import SourceRootBinding
from onec_runtime.errors import ProtocolError
from onec_runtime.rdbg.models import StackFrame


class StackInventoryBackend(Protocol):
    def read_stack(self, fence: object) -> tuple[StackFrame, ...]:
        """Validate the exact fence atomically with a fresh native inventory.

        The owner enforces command timeout, target/operation/stop identity and
        can_inspect state. Do not satisfy this with the legacy cached stack API.
        """
        ...


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedFrameSource:
    """Internal source association established by strict physical mapping."""

    source: str
    line: int
    identity: ModuleIdentity
    version: SourceVersionRef

    def __post_init__(self) -> None:
        if (not isinstance(self.source, str) or len(self.source) > 512
                or not all(part.isidentifier() for part in self.source.split("."))):
            raise ValueError("frame source must be a bounded logical module name")
        if type(self.line) is not int or self.line < 1:
            raise ValueError("source line must be positive")


class ConfigurationFrameResolver:
    """Batch source mapping with opaque, binding-specific syntax namespaces.

    Retain this resolver with its registry for the session lifetime. Worker
    owners can compose it with their strict generation-aware source resolver.
    """

    def __init__(self, catalog: CaptureSourceCatalog) -> None:
        self._catalog = catalog
        self._namespaces: dict[SourceRootBinding, str] = {}
        self._lock = Lock()

    def __call__(self, frames: tuple[StackFrame, ...]) -> tuple[ResolvedFrameSource | None, ...]:
        sources = self._catalog.resolve_modules(tuple(frame.location for frame in frames))
        result = []
        for source in sources:
            if not isinstance(source, CaptureModuleSource):
                result.append(None)
                continue
            with self._lock:
                namespace = self._namespaces.get(source.binding)
                if namespace is None:
                    namespace = uuid4().hex
                    self._namespaces[source.binding] = namespace
            identity = ModuleIdentity(
                namespace, "configuration", source.module_kind,
                str(source.object_id), str(source.property_id), source.binding.extension_name,
            )
            result.append(ResolvedFrameSource(
                source.canonical_name, source.line, identity, source.source_version,
            ))
        return tuple(result)


@dataclass(frozen=True, slots=True, repr=False)
class PhysicalFrameIdentity:
    """Diagnostic coordinates only; URLs and target/session identities are absent."""

    object_id: UUID
    property_id: UUID
    module_type: str
    extension_name: str


@dataclass(frozen=True, slots=True, repr=False)
class DebugFrame:
    native_level: int
    source: str
    line: int | None
    visible_index: int | None = None
    source_status: str = "unavailable"
    detail: str = "line"
    method: MethodSyntaxInfo | None = None
    method_status: str = "not_requested"
    method_reason: str | None = None
    runtime_kernel: bool = False
    source_sha256: str | None = field(default=None, repr=False)
    physical: PhysicalFrameIdentity | None = field(default=None, repr=False)
    _resolved: ResolvedFrameSource | None = field(default=None, repr=False)
    _enricher: _MethodEnricher | None = field(default=None, repr=False, compare=False)

    def with_method(self, work_budget_s: float | None = None) -> DebugFrame:
        if self._enricher is None:
            return self
        return cast(DebugFrame, self._enricher.enrich((self,), work_budget_s)[0])

    def __str__(self) -> str:
        label = (f"#{self.visible_index}" if self.visible_index is not None
                 else f"native #{self.native_level}")
        if self.runtime_kernel:
            return f"{label} служебный кадр"
        text = f"{label} {self.source}:{self.line}"
        if self.method is not None:
            signature = f"{self.method.name}({', '.join(self.method.parameters)})"
            text += " — " + (signature if len(signature) <= 512 else signature[:511] + "…")
        elif self.source_status == "unavailable":
            text += " — исходный файл не найден"
        elif self.method_status not in ("not_requested", "resolved"):
            text += f" — {self.method_status}"
        return text

    __repr__ = __str__


@dataclass(frozen=True, slots=True)
class RuntimeFrameMarker:
    count: int

    def __str__(self) -> str:
        return f"… скрыто {self.count} служебных кадров"


@dataclass(frozen=True, slots=True, repr=False)
class StackPage:
    """Saved request result; an empty request has no continuation cursor."""

    frames: tuple[DebugFrame | RuntimeFrameMarker, ...]
    total: int
    next_cursor: int | None
    detail: str = "line"
    native: bool = False
    _enricher: _MethodEnricher | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.frames) is not tuple or any(
            not isinstance(frame, (DebugFrame, RuntimeFrameMarker)) for frame in self.frames
        ):
            raise TypeError("stack frames must be a tuple of immutable frames or markers")

    def with_methods(self, work_budget_s: float | None = None) -> StackPage:
        if self._enricher is None:
            return self
        return replace(self, frames=self._enricher.enrich(self.frames, work_budget_s), detail="method")

    def __str__(self) -> str:
        return "Стек вызовов\n" + "\n".join(str(frame) for frame in self.frames)

    __repr__ = __str__


@dataclass(frozen=True, slots=True, repr=False)
class StackDescriptor:
    _owner: LocalStackAdapter
    _native: bool = False

    @property
    def native(self) -> StackDescriptor:
        return replace(self, _native=True)

    @overload
    def __getitem__(self, key: int) -> DebugFrame: ...

    @overload
    def __getitem__(self, key: slice) -> StackPage: ...

    def __getitem__(self, key: int | slice) -> StackPage | DebugFrame:
        if type(key) is int:
            start, stop = key, key + 1
        elif type(key) is slice and (
            key.step is None or type(key.step) is int and key.step == 1
        ):
            start, stop = 0 if key.start is None else key.start, key.stop
        else:
            raise TypeError("stack requires a nonnegative index or bounded unit-step slice")
        if (type(start) is not int or type(stop) is not int
                or start < 0 or stop < start or stop - start > 100):
            raise ValueError("stack pages require nonnegative bounds and at most 100 frames")
        page = self._owner._read(start, stop, native=self._native)
        if type(key) is int:
            for frame in page.frames:
                if isinstance(frame, DebugFrame):
                    return frame
            raise IndexError("stack frame is outside the inventory")
        return page

    def __iter__(self):
        raise TypeError("stack iteration requires a bounded page")


class LocalStackAdapter:
    """Internal adapter awaiting CaptureView's lifecycle/variable integration."""

    def __init__(
        self, backend: StackInventoryBackend, fence: object, *,
        resolve_sources: Callable[[tuple[StackFrame, ...]], tuple[ResolvedFrameSource | None, ...]],
        is_runtime_frame: Callable[[StackFrame], bool],
        registry: ModuleSyntaxRegistry, command_timeout_s: float,
        max_source_bytes: int = 2_000_000,
        parse_module: Callable[[str], ParsedModuleModel] = parse_full_ast_module,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not isfinite(command_timeout_s) or command_timeout_s <= 0:
            raise ValueError("command timeout must be finite and positive")
        if type(max_source_bytes) is not int or max_source_bytes < 1:
            raise ValueError("source size limit must be positive")
        self._backend, self._fence = backend, fence
        self._resolve_sources, self._is_runtime = resolve_sources, is_runtime_frame
        self._enricher = _MethodEnricher(registry, command_timeout_s, max_source_bytes, parse_module, clock)

    @property
    def stack(self) -> StackDescriptor:
        return StackDescriptor(self)

    def _read(self, start: int, stop: int, *, native: bool) -> StackPage:
        frames = self._backend.read_stack(self._fence)
        runtime = tuple(self._is_runtime(frame) for frame in frames)
        total = len(frames) if native else sum(not hidden for hidden in runtime)
        selected: list[StackFrame] = []
        entries: list[StackFrame | RuntimeFrameMarker] = []
        cursor = 0
        hidden_count = 0
        for frame, hidden in zip(frames, runtime, strict=True):
            if hidden and not native:
                hidden_count += 1
                continue
            if hidden_count:
                if start <= cursor < stop:
                    entries.append(RuntimeFrameMarker(hidden_count))
                hidden_count = 0
            if start <= cursor < stop:
                entries.append(frame)
                selected.append(frame)
            cursor += 1
        if (hidden_count and start <= cursor <= stop and stop > start
                and (selected or total == 0 and start == 0)):
            entries.append(RuntimeFrameMarker(hidden_count))
        sources = (None,) * len(selected) if native else self._resolve_sources(tuple(selected))
        mapped = dict(zip((frame.level for frame in selected), sources, strict=True))
        result = []
        visible_index = start
        for entry in entries:
            if isinstance(entry, RuntimeFrameMarker):
                result.append(entry)
                continue
            hidden = self._is_runtime(entry)
            source = mapped[entry.level]
            result.append(DebugFrame(
                native_level=entry.level,
                source=source.source if source else "Модуль конфигурации",
                line=None if hidden else source.line if source else entry.location.line,
                visible_index=None if native else visible_index,
                source_status=source.version.source_status if source else "unavailable",
                runtime_kernel=hidden,
                physical=None if hidden else PhysicalFrameIdentity(
                    entry.location.object_id, entry.location.property_id,
                    entry.location.module_type[:256], entry.location.extension_name[:256],
                ),
                _resolved=source, _enricher=self._enricher,
            ))
            visible_index += 1
        return StackPage(tuple(result), total, stop if start < stop < total else None,
                         native=native, _enricher=self._enricher)


class _MethodEnricher:
    """Soft deadline: synchronous reads/parses finish, then their budget is checked.

    No parser preemption is claimed. Completed facts may enter the registry even
    if their frame receives timeout; another local request can reuse those facts.
    """

    def __init__(
        self, registry: ModuleSyntaxRegistry, command_timeout_s: float,
        max_source_bytes: int, parse_module: Callable[[str], ParsedModuleModel],
        clock: Callable[[], float],
    ) -> None:
        self._registry = registry
        self._timeout = command_timeout_s
        self._max_bytes = max_source_bytes
        self._parse = parse_module
        self._clock = clock

    def enrich(
        self, frames: tuple[DebugFrame | RuntimeFrameMarker, ...],
        work_budget_s: float | None,
    ) -> tuple[DebugFrame | RuntimeFrameMarker, ...]:
        budget = self._timeout if work_budget_s is None else work_budget_s
        if not isfinite(budget) or budget < 0:
            raise ValueError("work budget must be finite and nonnegative")
        deadline = self._clock() + min(budget, self._timeout)
        cache = {}
        result = []
        for frame in frames:
            if isinstance(frame, RuntimeFrameMarker) or frame.method_status == "resolved":
                result.append(frame)
                continue
            source = frame._resolved
            if source is None:
                result.append(replace(frame, detail="method", method_status="unavailable"))
                continue
            key = (source.identity, source.version)
            if key not in cache:
                cache[key] = self._index(source, deadline)
            index, status, reason = cache[key]
            method = (index.method_at_line(frame.line)
                      if index is not None and status == "resolved" and frame.line is not None else None)
            if status == "resolved" and method is None:
                status, reason = "unavailable", "method_unavailable"
            result.append(replace(
                frame, detail="method", method=method, method_status=status, method_reason=reason,
                source_sha256=index.source_sha256 if index else frame.source_sha256,
            ))
        return tuple(result)

    def _index(
        self, source: ResolvedFrameSource, deadline: float,
    ) -> tuple[ModuleSyntaxIndex | None, str, str | None]:
        if self._clock() >= deadline:
            return None, "timeout", None
        version = source.version
        parser_identity = full_ast_parser_identity()
        digest = version.source_sha256
        index = self._registry.get(source.identity, digest, parser_identity) if digest else None
        if index is not None:
            return index, "resolved", None
        if version.size is not None and version.size > self._max_bytes:
            return None, "unavailable", "source_too_large"
        if self._clock() >= deadline:
            return None, "timeout", None
        try:
            text = version.read_text()
        except CaptureSourceChangedError:
            if self._clock() >= deadline:
                return None, "timeout", None
            return None, "source_changed", None
        except (OSError, ValueError, ProtocolError):
            if self._clock() >= deadline:
                return None, "timeout", None
            return None, "unavailable", "source_unavailable"
        if self._clock() >= deadline:
            return None, "timeout", None
        content = text.encode("utf-8")
        if len(content) > self._max_bytes:
            return None, "unavailable", "source_too_large"
        digest = sha256(content).hexdigest()
        index = self._registry.get(source.identity, digest, parser_identity)
        if index is not None:
            return index, "resolved", None
        if self._clock() >= deadline:
            return None, "timeout", None
        try:
            index = self._parse(text).syntax_index
        except ValueError:
            if self._clock() >= deadline:
                return None, "timeout", None
            return None, "unavailable", "parse_unavailable"
        expired = self._clock() >= deadline
        if index is None or index.source_sha256 != digest or index.parser_identity != parser_identity:
            return None, "unavailable", "syntax_version_unavailable"
        self._registry.publish(source.identity, index)
        return index, "timeout" if expired else "resolved", None
