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
from typing import TYPE_CHECKING, Protocol, cast, overload
from uuid import UUID, uuid4

from onec_runtime.bsl.full_ast_worker_projection import (
    full_ast_parser_identity, parse_full_ast_module,
)
from onec_runtime.bsl.module_syntax import (
    MethodSyntaxInfo, ModuleIdentity, ModuleSyntaxIndex, ModuleSyntaxRegistry,
)
from onec_runtime.bsl.worker_projection_model import ParsedModuleModel
from onec_runtime.capture_evaluation import (
    CaptureEvaluationOutcome, CapturePhase, CaptureStatus,
)
from onec_runtime.capture_source import (
    CaptureModuleSource, CaptureSourceCatalog, CaptureSourceChangedError,
    SourceVersionRef,
)
from onec_runtime.configuration_source import SourceRootBinding
from onec_runtime.errors import (
    CaptureSourceUnavailableError, ProtocolError, StaleCaptureError,
)
from onec_runtime.rdbg.models import StackFrame

if TYPE_CHECKING:
    from onec_runtime.capture_values import CaptureContextView, VariableDescriptor


@dataclass(frozen=True, slots=True, repr=False)
class CaptureView:
    """A handle fenced to one CAPTURE debugger stop.

    Obtain it with ``runtime.current_capture()``. ``operation_id``,
    ``capture_generation`` and ``stop_sequence`` identify the local stop.
    The handle may outlive that stop, but its status then reports ``stale``;
    waiting or reading its frame-backed data is no longer valid.
    """

    operation_id: int
    capture_generation: int
    stop_sequence: int
    __is_current: Callable[[], bool] = field(repr=False, compare=False)
    __read_status: Callable[[], CaptureStatus] = field(repr=False, compare=False)
    __wait_for_outcome: Callable[
        [float | None, str | None], CaptureEvaluationOutcome
    ] = field(repr=False, compare=False)
    __stack: StackDescriptor | None = field(default=None, repr=False, compare=False)
    __context: CaptureContextView | None = field(
        default=None, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        for name in ("operation_id", "capture_generation", "stop_sequence"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not all(
            callable(callback)
            for callback in (
                self.__is_current,
                self.__read_status,
                self.__wait_for_outcome,
            )
        ):
            raise TypeError("capture view readers must be callable")
        if self.__stack is not None and not isinstance(self.__stack, StackDescriptor):
            raise TypeError("capture stack descriptor is invalid")
        if self.__context is not None:
            from onec_runtime.capture_values import CaptureContextView

            if not isinstance(self.__context, CaptureContextView):
                raise TypeError("capture context descriptor is invalid")

    def __repr__(self) -> str:
        return (
            "CaptureView("
            f"operation_id={self.operation_id}, "
            f"capture_generation={self.capture_generation}, "
            f"stop_sequence={self.stop_sequence})"
        )

    def status(self) -> CaptureStatus:
        """Return this stop's phase and available inspection/wait actions.

        An old view returns a ``CaptureStatus`` with phase ``stale`` without
        consulting a later debugger frame.
        """
        if not self.__is_current():
            return CaptureStatus(
                self.operation_id,
                self.capture_generation,
                self.stop_sequence,
                CapturePhase.STALE,
            )
        return self.__read_status()

    def wait(
        self,
        timeout_s: float | None = None,
        evaluation_id: str | None = None,
    ) -> CaptureEvaluationOutcome:
        """Observe a CAPTURE evaluation already owned by this stop.

        ``evaluation_id`` selects an exact evaluation; when omitted, the
        current or last retained evaluation is selected. ``timeout_s`` is a
        local wait limit. Expiry returns an outcome with state ``pending``;
        it does not cancel or redispatch the remote expression. Raises
        ``StaleCaptureError`` if this view no longer names the current stop.
        """
        if not self.__is_current():
            raise StaleCaptureError()
        return self.__wait_for_outcome(timeout_s, evaluation_id)

    @property
    def stack(self) -> StackDescriptor:
        """Return a descriptor for bounded stack pages at this stop.

        Each indexed read validates the stop fence. ``stack.native`` includes
        physical frames that the default visible stack may hide.
        """
        if self.__stack is None:
            raise CaptureSourceUnavailableError("capture stack inspection is not attached")
        return self.__stack

    @property
    def context(self) -> CaptureContextView:
        """Return live, fenced variables of the staged CAPTURE context.

        Use ``context.variables[name]`` or a bounded slice. Each value read
        checks this stop's identity; a later stop cannot revive this view.
        """
        if self.__context is None:
            raise CaptureSourceUnavailableError(
                "capture context value inspection is not attached"
            )
        return self.__context



class StackInventoryBackend(Protocol):
    def read_stack(self, fence: object) -> tuple[StackFrame, ...]:
        """Validate the exact fence and return its recorded native stack.

        The owner enforces command timeout, target/operation/stop identity and
        can_inspect state. A later stop must invalidate the previous fence.
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
    """One saved frame with optional source and live value descriptors.

    ``source_status`` and ``method_status`` indicate whether source mapping
    and method enrichment succeeded. ``variables``, ``parameters`` and
    ``locals`` return descriptors whose indexed reads check the stop fence.
    """

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
    _value_scope: object | None = field(default=None, repr=False, compare=False)

    def _values(self) -> CaptureContextView:
        from onec_runtime.capture_values import CaptureContextView

        if not isinstance(self._value_scope, CaptureContextView):
            raise CaptureSourceUnavailableError("frame value inspection is not attached")
        return self._value_scope

    @property
    def variables(self) -> VariableDescriptor:
        return self._values().variables

    @property
    def parameters(self) -> VariableDescriptor:
        return self._values().parameters

    @property
    def locals(self) -> VariableDescriptor:
        return self._values().locals

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
    """A bounded stack result with ``frames``, ``total`` and ``next_cursor``.

    ``with_methods()`` adds source-derived method names where available.
    The returned frames are saved metadata; their value descriptors still
    require the original stop to be current.
    """

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
    """Read a CAPTURE stack by index or bounded slice.

    ``stack[index]`` returns one visible frame; ``stack[start:stop]`` returns
    a :class:`StackPage` of at most 100 frames. Use ``stack.native`` to read
    physical frames, including runtime frames.
    """

    _owner: LocalStackAdapter
    _native: bool = False

    @property
    def native(self) -> StackDescriptor:
        """Return a descriptor that indexes physical debugger frames."""
        return replace(self, _native=True)

    @overload
    def __getitem__(self, key: int) -> DebugFrame: ...

    @overload
    def __getitem__(self, key: slice) -> StackPage: ...

    def __getitem__(self, key: int | slice) -> StackPage | DebugFrame:
        """Fetch a frame or finite unit-step page under the current stop fence."""
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
        bind_frame: Callable[[DebugFrame], DebugFrame] | None = None,
    ) -> None:
        if not isfinite(command_timeout_s) or command_timeout_s <= 0:
            raise ValueError("command timeout must be finite and positive")
        if type(max_source_bytes) is not int or max_source_bytes < 1:
            raise ValueError("source size limit must be positive")
        self._backend, self._fence = backend, fence
        self._resolve_sources, self._is_runtime = resolve_sources, is_runtime_frame
        self._bind_frame = bind_frame
        self._enricher = _MethodEnricher(registry, command_timeout_s, max_source_bytes, parse_module, clock)

    @property
    def stack(self) -> StackDescriptor:
        return StackDescriptor(self)

    def native_frame_with_method(
        self,
        native_level: int,
        work_budget_s: float | None = None,
    ) -> DebugFrame:
        """Enrich one physical frame only for a requested value role.

        ``stack.native`` remains a fast line-only inventory.  Parameter and
        local classification is the explicit slow path that needs the method
        declaration, so it resolves and parses just the requested frame.
        """
        if type(native_level) is not int or native_level < 0:
            raise ValueError("native frame level must be nonnegative")
        frames = self._backend.read_stack(self._fence)
        frame = next((item for item in frames if item.level == native_level), None)
        if frame is None:
            raise IndexError("stack frame is outside the inventory")
        runtime = self._is_runtime(frame)
        source = None
        if not runtime:
            sources = self._resolve_sources((frame,))
            if type(sources) is not tuple or len(sources) != 1:
                raise ProtocolError("stack source mapping is invalid")
            source = sources[0]
        mapped = self._mapped_frame(
            frame,
            source,
            visible_index=None,
            runtime=runtime,
        )
        enriched = self._enricher.enrich((mapped,), work_budget_s)[0]
        if not isinstance(enriched, DebugFrame):
            raise ProtocolError("stack method enrichment is invalid")
        return enriched

    def _read(self, start: int, stop: int, *, native: bool) -> StackPage:
        frames = self._backend.read_stack(self._fence)
        runtime = tuple(self._is_runtime(frame) for frame in frames)
        selected: list[StackFrame] = []
        entries: list[StackFrame | RuntimeFrameMarker] = []
        if native:
            total = 0 if not frames else max(frame.level for frame in frames) + 1
            selected.extend(
                frame for frame in frames if start <= frame.level < stop
            )
            entries.extend(selected)
        else:
            total = sum(not hidden for hidden in runtime)
            cursor = 0
            hidden_count = 0
            for frame, hidden in zip(frames, runtime, strict=True):
                if hidden:
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
            mapped_frame = self._mapped_frame(
                entry,
                source,
                visible_index=None if native else visible_index,
                runtime=hidden,
            )
            result.append(
                self._bind_frame(mapped_frame)
                if self._bind_frame is not None and not hidden
                else mapped_frame
            )
            visible_index += 1
        return StackPage(tuple(result), total, stop if start < stop < total else None,
                         native=native, _enricher=self._enricher)

    def _mapped_frame(
        self,
        frame: StackFrame,
        source: ResolvedFrameSource | None,
        *,
        visible_index: int | None,
        runtime: bool,
    ) -> DebugFrame:
        return DebugFrame(
            native_level=frame.level,
            source=source.source if source else "Модуль конфигурации",
            line=None if runtime else source.line if source else frame.location.line,
            visible_index=visible_index,
            source_status=source.version.source_status if source else "unavailable",
            runtime_kernel=runtime,
            physical=None if runtime else PhysicalFrameIdentity(
                frame.location.object_id, frame.location.property_id,
                frame.location.module_type[:256], frame.location.extension_name[:256],
            ),
            _resolved=source,
            _enricher=self._enricher,
        )


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


__all__ = [
    "CaptureView",
    "StackInventoryBackend",
    "ResolvedFrameSource",
    "ConfigurationFrameResolver",
    "PhysicalFrameIdentity",
    "DebugFrame",
    "RuntimeFrameMarker",
    "StackPage",
    "StackDescriptor",
    "LocalStackAdapter",
]
