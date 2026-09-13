from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
import json
from math import isfinite
from threading import RLock
from typing import TYPE_CHECKING, TypeAlias
from uuid import UUID, uuid4

from onec_runtime.errors import ProtocolError, ReleasedProxy, StaleProxy

if TYPE_CHECKING:
    from onec_runtime_mcp.agent.capture_contracts import CaptureFence


JSONScalar: TypeAlias = None | bool | int | float | str
FrozenJSON: TypeAlias = JSONScalar | tuple[object, ...] | Mapping[str, object]

MAX_QUALIFIED_NAME = 512
MAX_TYPE_NAME = 256
MAX_PREVIEW_ITEMS = 100
MAX_PREVIEW_BYTES = 16 * 1024


class ProxyRealm(StrEnum):
    ONEC = "onec"
    PYTHON = "python"


class ProxyLifetime(StrEnum):
    CONTEXT = "context"
    FRAME = "frame"
    SNAPSHOT = "snapshot"
    WORKSPACE = "workspace"


class ProxyConsistency(StrEnum):
    EXACT = "exact"
    LATEST = "latest"


class SizeAccuracy(StrEnum):
    EXACT = "exact"
    ESTIMATE = "estimate"
    LOWER_BOUND = "lower_bound"
    UNKNOWN = "unknown"


class MeasurementCost(StrEnum):
    CHEAP = "cheap"
    SCAN_REQUIRED = "scan_required"
    EVALUATION_REQUIRED = "evaluation_required"


class _FrozenJSONMap(Mapping[str, FrozenJSON]):
    """Small immutable mapping whose deepcopy is a public JSON object."""

    __slots__ = ("_items",)

    def __init__(self, items: tuple[tuple[str, FrozenJSON], ...]) -> None:
        self._items = items

    def __getitem__(self, key: str) -> FrozenJSON:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __deepcopy__(self, memo: dict[int, object]) -> dict[str, object]:
        del memo
        return {key: _thaw_json(value) for key, value in self._items}

    def __repr__(self) -> str:
        return repr({key: _thaw_json(value) for key, value in self._items})


def _freeze_json(value: object) -> FrozenJSON:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not isfinite(value):
            raise TypeError("preview value must be finite and JSON-safe")
        return value
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, Mapping):
        items: list[tuple[str, FrozenJSON]] = []
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("preview mapping keys must be strings")
            items.append((key, _freeze_json(item)))
        return _FrozenJSONMap(tuple(items))
    raise TypeError("preview value must be JSON-safe")


def _thaw_json(value: FrozenJSON) -> object:
    if isinstance(value, _FrozenJSONMap):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _require_text(value: str, *, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{name} must be bounded to {maximum} characters")


def _require_positive_int(value: int, *, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_optional_non_negative(value: int | None, *, name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{name} must be a non-negative integer")


def _require_sha256(value: str, *, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class ProxyFence:
    runtime_id: str | None = None
    runtime_generation: int | None = None
    context_generation: int | None = None
    python_generation: int | None = None
    capture_fence: CaptureFence | None = None

    def __post_init__(self) -> None:
        if self.runtime_id is not None:
            _require_text(self.runtime_id, name="runtime_id", maximum=256)
        for name in ("runtime_generation", "context_generation", "python_generation"):
            value = getattr(self, name)
            if value is not None:
                _require_positive_int(value, name=name)
        if self.capture_fence is not None:
            from onec_runtime_mcp.agent.capture_contracts import CaptureFence

            if not isinstance(self.capture_fence, CaptureFence):
                raise TypeError("capture_fence must be a CaptureFence")
            if self.runtime_id is None or self.runtime_generation is None:
                raise ValueError(
                    "capture-scoped proxy fence requires runtime identity and generation"
                )

    @classmethod
    def from_wire(cls, value: object) -> "ProxyFence":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("proxy fence must be a mapping")
        allowed = {
            "runtime_id",
            "runtime_generation",
            "context_generation",
            "python_generation",
            "capture_fence",
        }
        if set(value) - allowed:
            raise ValueError("proxy fence has unsupported fields")
        capture_fence = value.get("capture_fence")
        if capture_fence is not None:
            from onec_runtime_mcp.agent.capture_contracts import CaptureFence

            capture_fence = CaptureFence.from_wire(capture_fence)
        return cls(
            runtime_id=value.get("runtime_id"),  # type: ignore[arg-type]
            runtime_generation=value.get("runtime_generation"),  # type: ignore[arg-type]
            context_generation=value.get("context_generation"),  # type: ignore[arg-type]
            python_generation=value.get("python_generation"),  # type: ignore[arg-type]
            capture_fence=capture_fence,
        )


@dataclass(frozen=True, slots=True)
class ProxyProvenance:
    cell_id: str
    revision: int
    source_sha256: str
    operation_id: str
    parent_proxy_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.cell_id, name="cell_id", maximum=256)
        _require_positive_int(self.revision, name="revision")
        _require_sha256(self.source_sha256, name="source_sha256")
        _require_text(self.operation_id, name="operation_id", maximum=256)
        if isinstance(self.parent_proxy_ids, str) or not isinstance(
            self.parent_proxy_ids, (tuple, list)
        ):
            raise TypeError("parent_proxy_ids must be a sequence")
        parents = tuple(self.parent_proxy_ids)
        for proxy_id in parents:
            try:
                UUID(proxy_id)
            except (TypeError, ValueError, AttributeError) as error:
                raise ValueError("parent_proxy_ids must contain UUIDs") from error
        object.__setattr__(self, "parent_proxy_ids", parents)


@dataclass(frozen=True, slots=True)
class ValueBudget:
    max_depth: int
    max_items: int
    max_rows: int
    max_bytes: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        for name in ("max_depth", "max_items", "max_rows", "max_bytes"):
            _require_positive_int(getattr(self, name), name=name)
        if (
            type(self.timeout_seconds) not in {int, float}
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class ValueSize:
    items: int | None = None
    rows: int | None = None
    bytes: int | None = None
    accuracy: SizeAccuracy = SizeAccuracy.UNKNOWN
    cost: MeasurementCost = MeasurementCost.CHEAP

    def __post_init__(self) -> None:
        for name in ("items", "rows", "bytes"):
            _require_optional_non_negative(getattr(self, name), name=name)
        if not isinstance(self.accuracy, SizeAccuracy):
            raise TypeError("accuracy must be a SizeAccuracy")
        if not isinstance(self.cost, MeasurementCost):
            raise TypeError("cost must be a MeasurementCost")

    @classmethod
    def unknown(cls, cost: MeasurementCost | str) -> "ValueSize":
        return cls(accuracy=SizeAccuracy.UNKNOWN, cost=MeasurementCost(cost))


@dataclass(frozen=True, slots=True)
class ValuePreview:
    type_name: str
    scalar: JSONScalar = None
    sample: tuple[FrozenJSON, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        _require_text(self.type_name, name="type_name", maximum=MAX_TYPE_NAME)
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a bool")
        if isinstance(self.sample, str) or not isinstance(self.sample, (tuple, list)):
            raise TypeError("sample must be a sequence")
        if len(self.sample) > MAX_PREVIEW_ITEMS:
            raise ValueError("sample must be bounded")
        scalar = _freeze_json(self.scalar)
        if isinstance(scalar, (tuple, _FrozenJSONMap)):
            raise TypeError("scalar preview must be a JSON scalar")
        sample = tuple(_freeze_json(item) for item in self.sample)
        encoded = json.dumps(
            {"scalar": _thaw_json(scalar), "sample": _thaw_json(sample)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_PREVIEW_BYTES:
            raise ValueError("preview fields must be bounded")
        object.__setattr__(self, "scalar", scalar)
        object.__setattr__(self, "sample", sample)


@dataclass(frozen=True, slots=True)
class ProxyDescriptor:
    proxy_id: str
    realm: ProxyRealm
    lifetime: ProxyLifetime
    qualified_name: str
    type_name: str
    version: int
    consistency: ProxyConsistency
    fence: ProxyFence
    provenance: ProxyProvenance
    capabilities: tuple[str, ...] = ()
    known_size: ValueSize | None = None
    bounded_preview: ValuePreview | None = None

    def __post_init__(self) -> None:
        try:
            UUID(self.proxy_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("proxy_id must be a UUID") from error
        if not isinstance(self.realm, ProxyRealm):
            raise TypeError("realm must be a ProxyRealm")
        if not isinstance(self.lifetime, ProxyLifetime):
            raise TypeError("lifetime must be a ProxyLifetime")
        _require_text(
            self.qualified_name,
            name="qualified_name",
            maximum=MAX_QUALIFIED_NAME,
        )
        _require_text(self.type_name, name="type_name", maximum=MAX_TYPE_NAME)
        _require_positive_int(self.version, name="version")
        if not isinstance(self.consistency, ProxyConsistency):
            raise TypeError("consistency must be a ProxyConsistency")
        if not isinstance(self.fence, ProxyFence):
            raise TypeError("fence must be a ProxyFence")
        if not isinstance(self.provenance, ProxyProvenance):
            raise TypeError("provenance must be a ProxyProvenance")
        if isinstance(self.capabilities, str) or not isinstance(
            self.capabilities, (tuple, list)
        ):
            raise TypeError("capabilities must be a sequence")
        capabilities = tuple(self.capabilities)
        if any(not isinstance(item, str) or not item.strip() for item in capabilities):
            raise ValueError("capabilities must contain non-empty strings")
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("capabilities must be unique")
        object.__setattr__(self, "capabilities", capabilities)
        if self.known_size is not None and not isinstance(self.known_size, ValueSize):
            raise TypeError("known_size must be a ValueSize")
        if self.bounded_preview is not None and not isinstance(
            self.bounded_preview, ValuePreview
        ):
            raise TypeError("bounded_preview must be a ValuePreview")


@dataclass(slots=True)
class _ProxyRecord:
    descriptor: ProxyDescriptor
    resolver_handle: object | None
    exact_anchor_id: str | None = None
    stale: bool = False
    released: bool = False


_MISSING_BINDING = object()


@dataclass(slots=True)
class _CapturePublicationCheckpoint:
    owner_token: object
    fence: "CaptureFence"
    created_proxy_ids: list[str]
    bindings_before: dict[tuple[ProxyRealm, str], str | object]
    binding_versions_before: dict[tuple[ProxyRealm, str], int | object]
    closed: bool = False


class ProxyRegistry:
    """Thread-safe proxy metadata registry that never publishes backend handles."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, _ProxyRecord] = {}
        self._bindings: dict[tuple[ProxyRealm, str], str] = {}
        self._binding_versions: dict[tuple[ProxyRealm, str], int] = {}
        self._capture_publication_owner = object()
        self._active_capture_publication: _CapturePublicationCheckpoint | None = None

    def _capture_state_snapshot(self, capture_fence: "CaptureFence") -> object:
        """Snapshot only records owned by one paused capture fence.

        Continuation rollback must never rewind unrelated context or Python
        publications that happened after admission. Bindings and their
        versions are not changed by capture invalidation, so only the mutable
        frame records need restoration. Per-inspect admission uses an O(batch)
        mutation checkpoint instead of this continuation-only snapshot.
        """
        from onec_runtime_mcp.agent.capture_contracts import CaptureFence

        if not isinstance(capture_fence, CaptureFence):
            raise TypeError("capture_fence must be a CaptureFence")
        with self._lock:
            return {
                proxy_id: replace(record)
                for proxy_id, record in self._records.items()
                if record.descriptor.lifetime is ProxyLifetime.FRAME
                and record.descriptor.fence.capture_fence == capture_fence
            }

    def _begin_capture_publication(
        self, capture_fence: "CaptureFence"
    ) -> _CapturePublicationCheckpoint:
        """Open an O(batch) rollback journal for one inspect publication."""
        from onec_runtime_mcp.agent.capture_contracts import CaptureFence

        if not isinstance(capture_fence, CaptureFence):
            raise TypeError("capture_fence must be a CaptureFence")
        with self._lock:
            if self._active_capture_publication is not None:
                raise ProtocolError(
                    "capture publication transaction is already active"
                )
            checkpoint = _CapturePublicationCheckpoint(
                self._capture_publication_owner,
                capture_fence,
                [],
                {},
                {},
            )
            self._active_capture_publication = checkpoint
            return checkpoint

    def _commit_capture_publication(
        self, checkpoint: _CapturePublicationCheckpoint
    ) -> None:
        with self._lock:
            self._require_capture_publication(checkpoint)
            checkpoint.closed = True
            self._active_capture_publication = None

    def _rollback_capture_publication(
        self, checkpoint: _CapturePublicationCheckpoint
    ) -> None:
        with self._lock:
            self._require_capture_publication(checkpoint)
            try:
                for proxy_id in checkpoint.created_proxy_ids:
                    self._records.pop(proxy_id, None)
                for key, previous in checkpoint.bindings_before.items():
                    if previous is _MISSING_BINDING:
                        self._bindings.pop(key, None)
                    else:
                        assert isinstance(previous, str)
                        self._bindings[key] = previous
                for key, previous in checkpoint.binding_versions_before.items():
                    if previous is _MISSING_BINDING:
                        self._binding_versions.pop(key, None)
                    else:
                        assert isinstance(previous, int)
                        self._binding_versions[key] = previous
            finally:
                checkpoint.closed = True
                self._active_capture_publication = None

    def _require_capture_publication(
        self, checkpoint: _CapturePublicationCheckpoint
    ) -> None:
        if (
            not isinstance(checkpoint, _CapturePublicationCheckpoint)
            or checkpoint.owner_token is not self._capture_publication_owner
            or checkpoint.closed
            or self._active_capture_publication is not checkpoint
        ):
            raise ProtocolError("capture publication checkpoint is invalid")

    def _restore_capture_state(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict) or any(
            not isinstance(proxy_id, str) or not isinstance(record, _ProxyRecord)
            for proxy_id, record in snapshot.items()
        ):
            raise TypeError("capture proxy snapshot is invalid")
        with self._lock:
            for proxy_id, saved in snapshot.items():
                current = self._records.get(proxy_id)
                if current is None:
                    self._records[proxy_id] = replace(saved)
                    continue
                # Capture invalidation changes only staleness and the resolver
                # handle. Preserve a concurrent release if a non-service
                # caller bypassed the capture admission boundary.
                current.stale = saved.stale
                if not current.released:
                    current.resolver_handle = saved.resolver_handle

    def register_context(
        self,
        *,
        qualified_name: str,
        type_name: str,
        runtime_id: str,
        runtime_generation: int,
        context_generation: int,
        provenance: ProxyProvenance,
        consistency: ProxyConsistency = ProxyConsistency.LATEST,
        resolver_handle: object | None = None,
        capabilities: tuple[str, ...] = (),
        known_size: ValueSize | None = None,
        bounded_preview: ValuePreview | None = None,
    ) -> ProxyDescriptor:
        return self._register(
            realm=ProxyRealm.ONEC,
            lifetime=ProxyLifetime.CONTEXT,
            qualified_name=qualified_name,
            type_name=type_name,
            consistency=consistency,
            fence=ProxyFence(
                runtime_id=runtime_id,
                runtime_generation=runtime_generation,
                context_generation=context_generation,
            ),
            provenance=provenance,
            resolver_handle=resolver_handle,
            capabilities=capabilities,
            known_size=known_size,
            bounded_preview=bounded_preview,
        )

    def register_context_batch(
        self,
        entries: tuple[Mapping[str, object], ...],
    ) -> tuple[ProxyDescriptor, ...]:
        """Publish a set of context bindings atomically under one registry lock."""
        if not isinstance(entries, tuple) or any(
            not isinstance(entry, Mapping) for entry in entries
        ):
            raise TypeError("context batch must be a tuple of mappings")
        with self._lock:
            bindings_before = dict(self._bindings)
            versions_before = dict(self._binding_versions)
            records_before = set(self._records)
            try:
                return tuple(
                    self.register_context(**dict(entry))  # type: ignore[arg-type]
                    for entry in entries
                )
            except BaseException:
                self._bindings = bindings_before
                self._binding_versions = versions_before
                for proxy_id in tuple(set(self._records) - records_before):
                    del self._records[proxy_id]
                raise

    def register_python(
        self,
        *,
        qualified_name: str,
        type_name: str,
        python_generation: int,
        provenance: ProxyProvenance,
        consistency: ProxyConsistency = ProxyConsistency.LATEST,
        resolver_handle: object | None = None,
        capabilities: tuple[str, ...] = (),
        known_size: ValueSize | None = None,
        bounded_preview: ValuePreview | None = None,
    ) -> ProxyDescriptor:
        return self._register(
            realm=ProxyRealm.PYTHON,
            lifetime=ProxyLifetime.WORKSPACE,
            qualified_name=qualified_name,
            type_name=type_name,
            consistency=consistency,
            fence=ProxyFence(python_generation=python_generation),
            provenance=provenance,
            resolver_handle=resolver_handle,
            capabilities=capabilities,
            known_size=known_size,
            bounded_preview=bounded_preview,
        )

    def register_frame(
        self,
        *,
        qualified_name: str,
        type_name: str,
        runtime_id: str,
        runtime_generation: int,
        context_generation: int,
        capture_fence: CaptureFence,
        provenance: ProxyProvenance,
        consistency: ProxyConsistency = ProxyConsistency.EXACT,
        resolver_handle: object | None = None,
        capabilities: tuple[str, ...] = (),
        known_size: ValueSize | None = None,
        bounded_preview: ValuePreview | None = None,
        _publication: _CapturePublicationCheckpoint | None = None,
    ) -> ProxyDescriptor:
        """Register a proxy whose lifetime ends when its capture fence advances."""
        return self._register(
            realm=ProxyRealm.ONEC,
            lifetime=ProxyLifetime.FRAME,
            qualified_name=qualified_name,
            type_name=type_name,
            consistency=consistency,
            fence=ProxyFence(
                runtime_id=runtime_id,
                runtime_generation=runtime_generation,
                context_generation=context_generation,
                capture_fence=capture_fence,
            ),
            provenance=provenance,
            resolver_handle=resolver_handle,
            capabilities=capabilities,
            known_size=known_size,
            bounded_preview=bounded_preview,
            capture_publication=_publication,
        )

    def resolve(self, proxy_id: str) -> ProxyDescriptor:
        with self._lock:
            record = self._record(proxy_id)
            if record.released:
                raise ReleasedProxy("value proxy was released")
            if record.stale:
                raise StaleProxy("value proxy generation is stale")
            descriptor = record.descriptor
            current_id = self._bindings[self._binding_key(descriptor)]
            if record.exact_anchor_id is not None:
                anchor = self._records.get(record.exact_anchor_id)
                if (
                    current_id != record.exact_anchor_id
                    or anchor is None
                    or anchor.stale
                    or anchor.released
                    or anchor.descriptor.fence != descriptor.fence
                ):
                    raise StaleProxy("exact value proxy version was superseded")
                return descriptor
            if current_id == proxy_id:
                return descriptor
            if descriptor.consistency is ProxyConsistency.EXACT:
                raise StaleProxy("exact value proxy version was superseded")
            current = self._records[current_id]
            if current.stale or current.released or current.descriptor.fence != descriptor.fence:
                raise StaleProxy("latest value proxy cannot cross a generation fence")
            return current.descriptor

    def resolver_handle(self, proxy_id: str) -> object | None:
        with self._lock:
            descriptor = self.resolve(proxy_id)
            return self._records[descriptor.proxy_id].resolver_handle

    def metadata(self, proxy_id: str) -> ProxyDescriptor:
        """Return public metadata without resolving lifecycle or backend state."""
        with self._lock:
            return self._record(proxy_id).descriptor

    def is_exact_snapshot(self, proxy_id: str) -> bool:
        with self._lock:
            return self._record(proxy_id).exact_anchor_id is not None

    def snapshot_exact(self, proxy_id: str) -> ProxyDescriptor:
        """Create an exact alias without changing the live LATEST binding."""
        with self._lock:
            descriptor = self.resolve(proxy_id)
            record = self._records[descriptor.proxy_id]
            if descriptor.consistency is ProxyConsistency.EXACT:
                return descriptor
            exact = replace(
                descriptor,
                proxy_id=str(uuid4()),
                consistency=ProxyConsistency.EXACT,
            )
            self._records[exact.proxy_id] = _ProxyRecord(
                descriptor=exact,
                resolver_handle=record.resolver_handle,
                exact_anchor_id=descriptor.proxy_id,
            )
            return exact

    def current(self, realm: ProxyRealm | None = None) -> tuple[ProxyDescriptor, ...]:
        if realm is not None and not isinstance(realm, ProxyRealm):
            raise TypeError("realm must be a ProxyRealm")
        with self._lock:
            result: list[ProxyDescriptor] = []
            for (binding_realm, _name), proxy_id in self._bindings.items():
                record = self._records[proxy_id]
                if (
                    (realm is None or binding_realm is realm)
                    and not record.stale
                    and not record.released
                ):
                    result.append(record.descriptor)
            return tuple(sorted(result, key=lambda item: item.qualified_name.casefold()))

    def resolve_name(self, qualified_name: str) -> ProxyDescriptor:
        _require_text(qualified_name, name="qualified_name", maximum=MAX_QUALIFIED_NAME)
        prefix = qualified_name.split(".", 1)[0].casefold()
        realm = {"bsl": ProxyRealm.ONEC, "python": ProxyRealm.PYTHON}.get(prefix)
        if realm is None:
            raise ProtocolError("qualified variable name requires bsl. or python. prefix")
        with self._lock:
            proxy_id = self._bindings.get(
                (realm, self._qualified_name_key(realm, qualified_name))
            )
            if proxy_id is None:
                raise ProtocolError("variable binding is unknown")
            return self.resolve(proxy_id)

    def history(self, qualified_name: str) -> tuple[ProxyDescriptor, ...]:
        _require_text(qualified_name, name="qualified_name", maximum=MAX_QUALIFIED_NAME)
        prefix = qualified_name.split(".", 1)[0].casefold()
        realm = {"bsl": ProxyRealm.ONEC, "python": ProxyRealm.PYTHON}.get(prefix)
        if realm is None:
            raise ProtocolError("qualified variable name requires bsl. or python. prefix")
        key = (realm, self._qualified_name_key(realm, qualified_name))
        with self._lock:
            result = tuple(
                sorted(
                    (
                        record.descriptor
                        for record in self._records.values()
                        if self._binding_key(record.descriptor) == key
                        and record.exact_anchor_id is None
                    ),
                    key=lambda item: item.version,
                )
            )
            if not result:
                raise ProtocolError("variable binding is unknown")
            return result

    def release(self, proxy_id: str) -> bool:
        with self._lock:
            record = self._record(proxy_id)
            if record.released:
                return False
            record.released = True
            record.resolver_handle = None
            return True

    def release_target(
        self, proxy_id: str
    ) -> tuple[ProxyDescriptor, object | None, bool]:
        """Resolve the exact record affected by releasing an exact/latest proxy."""
        with self._lock:
            record = self._record(proxy_id)
            if record.released:
                return record.descriptor, None, False
            if record.stale:
                raise StaleProxy("value proxy generation is stale")
            descriptor = record.descriptor
            current_id = self._bindings[self._binding_key(descriptor)]
            if record.exact_anchor_id is not None:
                anchor = self._records.get(record.exact_anchor_id)
                if (
                    current_id != record.exact_anchor_id
                    or anchor is None
                    or anchor.stale
                    or anchor.released
                ):
                    raise StaleProxy("exact value proxy version was superseded")
                return descriptor, record.resolver_handle, True
            target = record
            if current_id != proxy_id:
                if descriptor.consistency is ProxyConsistency.EXACT:
                    raise StaleProxy("exact value proxy version was superseded")
                current = self._records[current_id]
                if current.stale or current.descriptor.fence != descriptor.fence:
                    raise StaleProxy("latest value proxy cannot cross a generation fence")
                if current.released:
                    return current.descriptor, None, False
                target = current
            return target.descriptor, target.resolver_handle, True

    def release_resolved(self, proxy_id: str) -> bool:
        descriptor, _handle, should_release = self.release_target(proxy_id)
        if not should_release:
            return False
        return self.release(descriptor.proxy_id)

    def invalidate_runtime_generation(self, runtime_id: str, generation: int) -> int:
        _require_text(runtime_id, name="runtime_id", maximum=256)
        _require_positive_int(generation, name="generation")
        with self._lock:
            matches = 0
            for record in self._records.values():
                fence = record.descriptor.fence
                if (
                    record.descriptor.realm is ProxyRealm.ONEC
                    and fence.runtime_id == runtime_id
                    and fence.runtime_generation == generation
                    and not record.stale
                ):
                    record.stale = True
                    record.resolver_handle = None
                    matches += 1
            return matches

    def invalidate_python_generation(self, generation: int) -> int:
        _require_positive_int(generation, name="generation")
        with self._lock:
            matches = 0
            for record in self._records.values():
                if (
                    record.descriptor.realm is ProxyRealm.PYTHON
                    and record.descriptor.fence.python_generation == generation
                    and not record.stale
                ):
                    record.stale = True
                    record.resolver_handle = None
                    matches += 1
            return matches

    def invalidate_capture_fence(self, capture_fence: "CaptureFence") -> int:
        """Expire every frame value from one exact paused capture."""
        from onec_runtime_mcp.agent.capture_contracts import CaptureFence

        if not isinstance(capture_fence, CaptureFence):
            raise TypeError("capture_fence must be a CaptureFence")
        with self._lock:
            matches = 0
            for record in self._records.values():
                if (
                    record.descriptor.lifetime is ProxyLifetime.FRAME
                    and record.descriptor.fence.capture_fence == capture_fence
                    and not record.stale
                ):
                    record.stale = True
                    record.resolver_handle = None
                    matches += 1
            return matches

    def _register(
        self,
        *,
        realm: ProxyRealm,
        lifetime: ProxyLifetime,
        qualified_name: str,
        type_name: str,
        consistency: ProxyConsistency,
        fence: ProxyFence,
        provenance: ProxyProvenance,
        resolver_handle: object | None,
        capabilities: tuple[str, ...],
        known_size: ValueSize | None,
        bounded_preview: ValuePreview | None,
        capture_publication: _CapturePublicationCheckpoint | None = None,
    ) -> ProxyDescriptor:
        if not isinstance(consistency, ProxyConsistency):
            raise TypeError("consistency must be a ProxyConsistency")
        key = (realm, self._qualified_name_key(realm, qualified_name))
        with self._lock:
            if capture_publication is not None:
                self._require_capture_publication(capture_publication)
                if (
                    lifetime is not ProxyLifetime.FRAME
                    or fence.capture_fence != capture_publication.fence
                ):
                    raise ProtocolError(
                        "capture publication checkpoint does not match frame fence"
                    )
                capture_publication.bindings_before.setdefault(
                    key, self._bindings.get(key, _MISSING_BINDING)
                )
                capture_publication.binding_versions_before.setdefault(
                    key, self._binding_versions.get(key, _MISSING_BINDING)
                )
            version = self._binding_versions.get(key, 0) + 1
            descriptor = ProxyDescriptor(
                proxy_id=str(uuid4()),
                realm=realm,
                lifetime=lifetime,
                qualified_name=qualified_name,
                type_name=type_name,
                version=version,
                consistency=consistency,
                fence=fence,
                provenance=provenance,
                capabilities=capabilities,
                known_size=known_size,
                bounded_preview=bounded_preview,
            )
            self._records[descriptor.proxy_id] = _ProxyRecord(
                descriptor=descriptor,
                resolver_handle=resolver_handle,
            )
            if capture_publication is not None:
                capture_publication.created_proxy_ids.append(descriptor.proxy_id)
            self._bindings[key] = descriptor.proxy_id
            self._binding_versions[key] = version
            return descriptor

    def _record(self, proxy_id: str) -> _ProxyRecord:
        try:
            UUID(proxy_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise ProtocolError("proxy_id must be a UUID") from error
        try:
            return self._records[proxy_id]
        except KeyError as error:
            raise ProtocolError("value proxy is unknown") from error

    @staticmethod
    def _binding_key(descriptor: ProxyDescriptor) -> tuple[ProxyRealm, str]:
        return (
            descriptor.realm,
            ProxyRegistry._qualified_name_key(
                descriptor.realm, descriptor.qualified_name
            ),
        )

    @staticmethod
    def _qualified_name_key(realm: ProxyRealm, qualified_name: str) -> str:
        return qualified_name if realm is ProxyRealm.PYTHON else qualified_name.casefold()


__all__ = [
    "MeasurementCost",
    "ProxyConsistency",
    "ProxyDescriptor",
    "ProxyFence",
    "ProxyLifetime",
    "ProxyProvenance",
    "ProxyRealm",
    "ProxyRegistry",
    "ReleasedProxy",
    "SizeAccuracy",
    "StaleProxy",
    "ValueBudget",
    "ValuePreview",
    "ValueSize",
]
