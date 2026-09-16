from __future__ import annotations

from dataclasses import dataclass, is_dataclass
from enum import Enum
from html import escape
from inspect import Parameter, signature
import json
import keyword
import re
from shlex import split
from threading import Lock
from typing import Any, Protocol, cast
from uuid import uuid4
import weakref

from IPython.core.error import UsageError
from IPython.core.interactiveshell import InteractiveShell
from IPython.core.magic import Magics, cell_magic, line_magic, magics_class
from IPython.display import display

from onec_runtime.runtime_contracts import (
    OperationExecutionProvenance,
    sanitize_normalized_diagnostic,
)
from onec_runtime.bsl import SourceUnitKind, SourceUnitRef, source_sha256
from onec_runtime.capture_evaluation import CaptureEvaluationKind
from onec_runtime.errors import CaptureEvaluationPendingError, ProtocolError
from onec_runtime.runtime_api import (
    MAX_PROJECTION_POSITION,
    RuntimeNamespaceSnapshot,
    RuntimeReply,
    RuntimeReplyKind,
    RuntimeStatus,
)
from onec_runtime.privacy import (
    bounded_platform_diagnostic,
    diagnostic_to_expert_wire,
    diagnostic_to_public_wire,
    public_artifact_value,
)


RUNTIME_NAMESPACE_NAME = "_onec_runtime"
BSL_NAMESPACE_NAME = "bsl"
MACHINE_MIME_TYPE = "application/vnd.onec.runtime+json"
_DISPLAY_CONFIG_NAME = "_onec_runtime_display"
_NAMESPACE_BRIDGE_NAME = "_onec_runtime_bsl_bridge"
_SOURCE_SESSION_NAME = "_onec_runtime_source_session"
_DIAGNOSTIC_EXCERPT_LIMIT = 512
_PRESENTATION_REASON_LIMIT = 512
_PENDING_EVALUATION_ID_LIMIT = 256
_PENDING_WAIT_GUIDANCE = "runtime.current_capture().wait(timeout_s=10)"
_PLATFORM_LOCATION_PREFIX = re.compile(
    r"^\{[^{}\r\n]{1,512}\([0-9]{1,10}(?:\s*,\s*[0-9]{1,10})?\)\}:\s*"
)


class BslCellError(RuntimeError):
    """A failed BSL reply surfaced as a Jupyter execution error."""

    def _render_traceback_(self) -> list[str]:
        # Verbose IPython tracebacks otherwise repr local RuntimeReply objects,
        # including raw platform diagnostics. Source context is displayed safely
        # in the separate rich diagnostic, not in Python adapter stack frames.
        return [f"{type(self).__name__}: {self}"]


class NotebookRuntime(Protocol):
    def execute_bsl(
        self,
        source: str,
        *,
        source_unit: SourceUnitRef,
    ) -> RuntimeReply: ...

    def resume_capture(
        self,
        *,
        dirty_roots: tuple[str, ...] = (),
    ) -> RuntimeReply: ...

    def status(self) -> RuntimeStatus: ...

    def namespace_snapshot(self) -> RuntimeNamespaceSnapshot: ...


@dataclass(frozen=True, slots=True)
class NotebookDisplayConfig:
    mode: str = "presentation"

    def __post_init__(self) -> None:
        if self.mode not in {"presentation", "diagnostic"}:
            raise ValueError("notebook display mode must be presentation or diagnostic")

    @classmethod
    def presentation(cls) -> "NotebookDisplayConfig":
        return cls("presentation")

    @classmethod
    def diagnostic(cls) -> "NotebookDisplayConfig":
        return cls("diagnostic")


@dataclass(frozen=True, slots=True)
class NotebookDisplay:
    text: str
    payload: dict[str, object]
    diagnostic: bool = False
    html: str | None = None

    def __repr__(self) -> str:
        return self.text

    def _repr_mimebundle_(
        self,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
    ) -> dict[str, object]:
        del include, exclude
        bundle: dict[str, object] = {
            "text/plain": self.text,
            MACHINE_MIME_TYPE: self.payload,
        }
        if self.html is not None:
            bundle["text/html"] = self.html
        if self.diagnostic:
            bundle["application/json"] = self.payload
        return bundle


class _NotebookSourceSession:
    """Adapter-private, atomic source identity state for one shell object."""

    __slots__ = ("_execution_counter", "_lock", "_session_id", "_issued_units", "_runtime_ref")

    def __init__(self) -> None:
        # This adapter-owned UUID is intentionally non-secret and unrelated to
        # Jupyter connection files, authentication tokens, or process IDs.
        session_id = uuid4().hex
        if re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
            raise ProtocolError("Jupyter source session identity is invalid")
        self._session_id = session_id
        self._execution_counter = 0
        self._lock = Lock()
        self._issued_units: set[SourceUnitRef] = set()
        self._runtime_ref: weakref.ReferenceType[object] | None = None

    def attach(self, runtime: object) -> None:
        with self._lock:
            if self._runtime_ref is None or self._runtime_ref() is not runtime:
                self._issued_units.clear()
                self._runtime_ref = weakref.ref(runtime)

    def detach(self) -> None:
        with self._lock:
            self._issued_units.clear()
            self._runtime_ref = None

    def recognizes(self, unit: SourceUnitRef | None, runtime: object) -> bool:
        with self._lock:
            return (
                self._runtime_ref is not None
                and self._runtime_ref() is runtime
                and unit in self._issued_units
            )

    def next_unit(self, source: str) -> SourceUnitRef:
        digest = source_sha256(source)
        with self._lock:
            self._execution_counter += 1
            counter = self._execution_counter
            unit = SourceUnitRef(
                SourceUnitKind.NOTEBOOK_CELL,
                f"jupyter:{self._session_id}:execution:{counter}",
                counter,
                digest,
            )
            # Retain only the identity/hash already sent to this attachment.
            # Source text remains owned by the runtime's active definitions.
            if self._runtime_ref is not None and self._runtime_ref() is not None:
                self._issued_units.add(unit)
            return unit


_SOURCE_SESSIONS: weakref.WeakKeyDictionary[object, _NotebookSourceSession] = (
    weakref.WeakKeyDictionary()
)
_SOURCE_SESSIONS_LOCK = Lock()


def _source_session_for_shell(
    shell: object,
    *,
    create: bool,
) -> _NotebookSourceSession | None:
    try:
        with _SOURCE_SESSIONS_LOCK:
            session = _SOURCE_SESSIONS.get(shell)
            if session is None and create:
                session = _NotebookSourceSession()
                _SOURCE_SESSIONS[shell] = session
            return session
    except TypeError as error:
        raise ProtocolError(
            "IPython shell cannot own a private Jupyter source session"
        ) from error


class OnecValueProxy:
    """A lazy symbolic reference to one persistent BSL context value."""

    __slots__ = (
        "_runtime_ref",
        "_runtime_generation",
        "_context_generation",
        "_path",
        "_selection",
        "name",
    )

    def __init__(
        self,
        runtime: object,
        name: str,
        *,
        runtime_generation: int,
        context_generation: int,
        path: tuple[str, ...] = (),
        selection: dict[str, int] | None = None,
    ) -> None:
        self._runtime_ref = weakref.ref(runtime)
        self.name = name
        self._runtime_generation = runtime_generation
        self._context_generation = context_generation
        self._path = tuple(path)
        self._selection = None if selection is None else dict(selection)

    def __repr__(self) -> str:
        return (
            f"<OnecValueProxy BSL {self._display_name()} "
            f"generation={self._runtime_generation}/{self._context_generation}>"
        )

    def to_df(
        self,
        *,
        refs: str = "presentation",
        ref_columns: dict[str, str] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int | None = None,
    ) -> Any:
        runtime = self._validated_runtime()
        if self._selection is not None:
            project = getattr(runtime, "project_to_df", None)
            if not callable(project):
                raise ProtocolError("1C runtime does not support bounded projection")
            return project(
                self._context_handle(),
                dict(self._selection),
                refs=refs,
                ref_columns=ref_columns,
                uuid_suffix=uuid_suffix,
                chunk_size=chunk_size,
            )
        materialize = getattr(runtime, "to_df", None)
        if not callable(materialize):
            materialize = getattr(runtime, "materialize_table", None)
        if not callable(materialize):
            raise ProtocolError("1C runtime does not support table materialization")
        return materialize(
            self._context_handle(),
            refs=refs,
            ref_columns=ref_columns,
            uuid_suffix=uuid_suffix,
            chunk_size=chunk_size,
        )

    def materialize(
        self,
        *,
        refs: str = "presentation",
        ref_columns: dict[str, str] | None = None,
        uuid_suffix: str = "__uuid",
        chunk_size: int | None = None,
        max_depth: int = 32,
        max_items: int = 100_000,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> Any:
        runtime = self._validated_runtime()
        if self._selection is not None:
            project = getattr(runtime, "project_value", None)
            if not callable(project):
                raise ProtocolError("1C runtime does not support bounded projection")
            return project(
                self._context_handle(),
                dict(self._selection),
                refs=refs,
                ref_columns=ref_columns,
                uuid_suffix=uuid_suffix,
                chunk_size=chunk_size,
                max_depth=max_depth,
                max_items=max_items,
                max_bytes=max_bytes,
            )
        materialize = getattr(runtime, "materialize_value", None)
        if not callable(materialize):
            raise ProtocolError("1C runtime does not support value materialization")
        return materialize(
            self._context_handle(),
            refs=refs,
            ref_columns=ref_columns,
            uuid_suffix=uuid_suffix,
            chunk_size=chunk_size,
            max_depth=max_depth,
            max_items=max_items,
            max_bytes=max_bytes,
        )

    def head(self, limit: int) -> OnecValueProxy:
        if type(limit) is not int or limit <= 0:
            raise ProtocolError("bounded slice limit must be a positive integer")
        return self[:limit]

    def __getitem__(self, selection: object) -> OnecValueProxy:
        if (
            not isinstance(selection, slice)
            or selection.step is not None
            or selection.stop is None
        ):
            raise ProtocolError("bounded slice requires finite start:stop without step")
        start = 0 if selection.start is None else selection.start
        stop = selection.stop
        if (
            type(start) is not int
            or type(stop) is not int
            or start < 0
            or stop <= start
            or stop > MAX_PROJECTION_POSITION
        ):
            raise ProtocolError("bounded slice requires finite non-negative start:stop")
        if self._selection is not None:
            raise ProtocolError("bounded slice is already selected")
        runtime = self._validated_runtime()
        return OnecValueProxy(
            runtime,
            self.name,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
            path=self._path,
            selection={"offset": start, "limit": stop - start},
        )

    def tabular_section(self, name: str) -> OnecValueProxy:
        if self._selection is not None:
            raise ProtocolError("bounded projection cannot extend a nested path")
        if not isinstance(name, str) or not re.fullmatch(
            r"[^\W\d]\w*", name, re.UNICODE
        ):
            raise ProtocolError("tabular section name must be one BSL identifier")
        runtime = self._validated_runtime()
        return OnecValueProxy(
            runtime,
            self.name,
            runtime_generation=self._runtime_generation,
            context_generation=self._context_generation,
            path=self._path + (name,),
        )

    def _validated_runtime(self) -> object:
        runtime = self._runtime_ref()
        if runtime is None:
            raise ProtocolError("1C runtime for this proxy is no longer available")
        snapshot_method = getattr(runtime, "namespace_snapshot", None)
        if not callable(snapshot_method):
            raise ProtocolError("1C runtime does not expose a namespace snapshot")
        snapshot = snapshot_method()
        if not isinstance(snapshot, RuntimeNamespaceSnapshot):
            raise ProtocolError("1C runtime namespace snapshot is invalid")
        if (
            snapshot.runtime_generation != self._runtime_generation
            or snapshot.context_generation != self._context_generation
        ):
            raise ProtocolError("1C value proxy is stale after runtime generation change")
        if self.name.casefold() not in {name.casefold() for name in snapshot.names}:
            raise ProtocolError(f"BSL name {self.name!r} is no longer persistent")
        guard = getattr(runtime, "validate_value_reference", None)
        if not callable(guard):
            raise ProtocolError("1C runtime does not expose local reference validation")
        guard(self._context_handle())
        return runtime

    def _context_handle(self) -> str:
        suffix = "".join(f".{item}" for item in self._path)
        return f"Контекст.{self.name}{suffix}"

    def _display_name(self) -> str:
        return ".".join((self.name, *self._path))


class _BslNamespaceBridge:
    __slots__ = ("_runtime_ref", "_proxies", "_snapshot")

    def __init__(self, runtime: object) -> None:
        self._runtime_ref = weakref.ref(runtime)
        self._proxies: dict[str, OnecValueProxy] = {}
        self._snapshot: RuntimeNamespaceSnapshot | None = None

    def sync(self, user_ns: dict[str, object]) -> None:
        runtime = self._runtime_ref()
        if runtime is None:
            raise ProtocolError("1C runtime is no longer available")
        method = getattr(runtime, "namespace_snapshot", None)
        if not callable(method):
            raise ProtocolError("1C runtime does not expose persistent BSL names")
        snapshot = method()
        if not isinstance(snapshot, RuntimeNamespaceSnapshot):
            raise ProtocolError("1C runtime namespace snapshot is invalid")
        guard = getattr(runtime, "validate_value_reference", None)
        if not callable(guard):
            raise ProtocolError("1C runtime does not expose local reference validation")
        handles = tuple(f"Контекст.{name}" for name in snapshot.names)
        for handle in handles:
            guard(handle)

        proposed = dict(self._proxies)
        for name in snapshot.names:
            normalized = name.casefold()
            proxy = proposed.get(normalized)
            if proxy is None or (
                proxy._runtime_generation != snapshot.runtime_generation
                or proxy._context_generation != snapshot.context_generation
            ):
                proxy = OnecValueProxy(
                    runtime,
                    name,
                    runtime_generation=snapshot.runtime_generation,
                    context_generation=snapshot.context_generation,
                )
                proposed[normalized] = proxy

        self._snapshot = snapshot
        self._proxies = proposed
        for name in snapshot.names:
            proxy = proposed[name.casefold()]
            existing = user_ns.get(name, _MISSING)
            if existing is _MISSING or (
                isinstance(existing, OnecValueProxy)
                and existing.name.casefold() == name.casefold()
            ):
                user_ns[name] = proxy

    def _proxy(self, name: str) -> OnecValueProxy:
        if self._snapshot is None:
            raise ProtocolError("BSL namespace has not been synchronized")
        normalized = name.casefold()
        proxy = self._proxies.get(normalized)
        if proxy is None or (
            proxy._runtime_generation != self._snapshot.runtime_generation
            or proxy._context_generation != self._snapshot.context_generation
        ):
            runtime = self._runtime_ref()
            if runtime is None:
                raise ProtocolError("1C runtime is no longer available")
            proxy = OnecValueProxy(
                runtime,
                name,
                runtime_generation=self._snapshot.runtime_generation,
                context_generation=self._snapshot.context_generation,
            )
            self._proxies[normalized] = proxy
        return proxy

    def get(self, name: str) -> OnecValueProxy:
        if self._snapshot is None:
            raise AttributeError(name)
        actual = next(
            (item for item in self._snapshot.names if item.casefold() == name.casefold()),
            None,
        )
        if actual is None:
            raise AttributeError(name)
        return self._proxy(actual)

    def names(self) -> tuple[str, ...]:
        return () if self._snapshot is None else self._snapshot.names

    def detach(self, user_ns: dict[str, object]) -> None:
        for proxy in self._proxies.values():
            if user_ns.get(proxy.name) is proxy:
                user_ns.pop(proxy.name, None)


class BslNamespace:
    __slots__ = ("_bridge",)

    def __init__(self, bridge: _BslNamespaceBridge) -> None:
        self._bridge = bridge

    def __getattr__(self, name: str) -> OnecValueProxy:
        return self._bridge.get(name)

    def __getitem__(self, name: str) -> OnecValueProxy:
        return self._bridge.get(name)

    def __dir__(self) -> list[str]:
        return sorted(
            name
            for name in self._bridge.names()
            if name.isidentifier() and not keyword.iskeyword(name)
        )

    def __repr__(self) -> str:
        return f"<BslNamespace names={len(self._bridge.names())}>"


_MISSING = object()


def install_runtime(
    shell: object,
    runtime: NotebookRuntime,
    *,
    display: NotebookDisplayConfig | None = None,
) -> None:
    user_ns = getattr(shell, "user_ns", None)
    if not isinstance(user_ns, dict):
        raise TypeError("IPython shell does not expose a user namespace")
    existing_bsl = user_ns.get(BSL_NAMESPACE_NAME, _MISSING)
    if existing_bsl is not _MISSING and not isinstance(existing_bsl, BslNamespace):
        raise ProtocolError("reserved Python name 'bsl' is already in use")
    previous_bridge = user_ns.get(_NAMESPACE_BRIDGE_NAME)
    bridge = _BslNamespaceBridge(runtime)
    bridge.sync(user_ns)
    if isinstance(previous_bridge, _BslNamespaceBridge):
        previous_bridge.detach(user_ns)
    source_session = _source_session_for_shell(shell, create=True)
    assert source_session is not None
    source_session.attach(runtime)
    # Remove state left by older adapters or attempted namespace substitution.
    user_ns.pop(_SOURCE_SESSION_NAME, None)
    user_ns[RUNTIME_NAMESPACE_NAME] = runtime
    user_ns[_DISPLAY_CONFIG_NAME] = display or NotebookDisplayConfig.presentation()
    user_ns[_NAMESPACE_BRIDGE_NAME] = bridge
    user_ns[BSL_NAMESPACE_NAME] = BslNamespace(bridge)
    try:
        from .lsp_kernel import install_project_bridge
        install_project_bridge(shell, runtime)
    except Exception:
        # Language support is optional and cannot undo a started runtime.
        import logging
        logging.getLogger(__name__).warning('BSL project bridge unavailable')


def synchronize_bsl_namespace(shell: object) -> None:
    user_ns = getattr(shell, "user_ns", None)
    if not isinstance(user_ns, dict):
        raise TypeError("IPython shell does not expose a user namespace")
    bridge = user_ns.get(_NAMESPACE_BRIDGE_NAME)
    if not isinstance(bridge, _BslNamespaceBridge):
        raise ProtocolError("1C BSL namespace bridge is not installed")
    bridge.sync(user_ns)


@magics_class
class OnecRuntimeMagics(Magics):
    @cell_magic
    def bsl(self, line: str, cell: str) -> NotebookDisplay | None:
        if line.strip():
            raise UsageError("%%bsl does not accept line arguments")
        runtime = self._runtime()
        source_unit = self._source_session().next_unit(cell)
        provenance: list[OperationExecutionProvenance] = []

        def capture(value: OperationExecutionProvenance) -> None:
            if isinstance(value, OperationExecutionProvenance):
                provenance.append(value)

        execute = runtime.execute_bsl
        try:
            if _accepts_keyword(execute, "on_execution_provenance"):
                reply = cast(Any, execute)(
                    cell,
                    source_unit=source_unit,
                    on_execution_provenance=capture,
                )
            else:
                reply = execute(cell, source_unit=source_unit)
        except CaptureEvaluationPendingError as error:
            return _display_pending_evaluation(error)
        return self._finish_reply(
            reply,
            visible_source=cell,
            source_unit=source_unit,
            execution_provenance=(
                provenance[0] if len(provenance) == 1 else None
            ),
        )

    @line_magic
    def bsl_resume(self, line: str) -> NotebookDisplay | None:
        roots = tuple(split(line)) if line.strip() else ()
        return self._finish_reply(
            self._runtime().resume_capture(dirty_roots=roots)
        )

    @line_magic
    def bsl_status(self, line: str) -> NotebookDisplay:
        if line.strip():
            raise UsageError("%bsl_status does not accept arguments")
        return _display_status(self._runtime().status())

    def _runtime(self) -> NotebookRuntime:
        runtime = self.shell.user_ns.get(RUNTIME_NAMESPACE_NAME)
        if runtime is None:
            raise UsageError(
                "1C runtime is not installed; call install_runtime(shell, runtime)"
            )
        required = (
            "execute_bsl",
            "resume_capture",
            "status",
            "namespace_snapshot",
        )
        if any(not callable(getattr(runtime, name, None)) for name in required):
            raise UsageError("Installed 1C runtime does not implement Runtime API")
        return cast(NotebookRuntime, runtime)

    def _display_config(self) -> NotebookDisplayConfig:
        config = self.shell.user_ns.get(_DISPLAY_CONFIG_NAME)
        if not isinstance(config, NotebookDisplayConfig):
            raise UsageError("1C runtime display configuration is missing")
        return config

    def _source_session(self) -> _NotebookSourceSession:
        session = _source_session_for_shell(self.shell, create=False)
        if session is None:
            raise UsageError("1C runtime source session is missing")
        return session

    def _finish_reply(
        self,
        reply: RuntimeReply,
        *,
        visible_source: str | None = None,
        source_unit: SourceUnitRef | None = None,
        execution_provenance: OperationExecutionProvenance | None = None,
    ) -> NotebookDisplay | None:
        if reply.succeeded:
            synchronize_bsl_namespace(self.shell)
        for message in reply.messages:
            print(message)
        if (
            self._display_config().mode == "presentation"
            and reply.succeeded
            and (
                reply.kind is RuntimeReplyKind.WORKER_LOADED
                or (
                    reply.kind is RuntimeReplyKind.MAIN_COMPLETED
                    and reply.result is None
                )
            )
        ):
            return None
        diagnostic_unit = _diagnostic_source_unit(reply.diagnostic)
        known_source_unit = (
            diagnostic_unit
            if self._source_session().recognizes(diagnostic_unit, self._runtime())
            else None
        )
        displayed = _display_reply(
            reply,
            self._display_config(),
            visible_source=visible_source,
            source_unit=source_unit,
            execution_provenance=execution_provenance,
            known_source_unit=known_source_unit,
        )
        if not reply.succeeded:
            # Publish the rich diagnostic before raising: failed cells have no
            # execute_result. Never use raw platform error text in an exception.
            display(displayed)
            diagnostic = (
                {}
                if reply.diagnostic is None
                else diagnostic_to_public_wire(reply.diagnostic)
            )
            summary = (
                displayed.text
                if self._display_config().mode == "presentation"
                else diagnostic.get("runtime_summary", "BSL execution failed")
            )
            raise BslCellError(summary) from None
        return displayed


def _display_pending_evaluation(
    error: CaptureEvaluationPendingError,
) -> NotebookDisplay:
    """Render the safe ticket receipt without inspecting live runtime state."""

    evaluation_id = _safe_pending_evaluation_id(
        getattr(error, "evaluation_id", None)
    )
    kind = getattr(error, "evaluation_kind", None)
    evaluation_kind = (
        kind.value if isinstance(kind, CaptureEvaluationKind) else "unknown"
    )
    text = "\n".join(
        (
            f"evaluation_id={evaluation_id}",
            f"evaluation_kind={evaluation_kind}",
            _PENDING_WAIT_GUIDANCE,
        )
    )
    return NotebookDisplay(
        text,
        {
            "evaluation_id": evaluation_id,
            "evaluation_kind": evaluation_kind,
            "guidance": _PENDING_WAIT_GUIDANCE,
        },
        html="<pre>" + escape(text) + "</pre>",
    )


def _safe_pending_evaluation_id(value: object) -> str:
    if type(value) is not str:
        return "<unknown>"
    cleaned = "".join(
        character if character.isprintable() else " "
        for character in value[:_PENDING_EVALUATION_ID_LIMIT]
    )
    return " ".join(cleaned.split()) or "<unknown>"


def load_ipython_extension(ipython: InteractiveShell) -> None:
    ipython.register_magics(OnecRuntimeMagics(ipython))
    from .capture_display import install_capture_formatters
    install_capture_formatters(ipython)
    from .completion import install_completion_matcher
    install_completion_matcher(ipython)
    from .lsp_kernel import install_project_bridge
    runtime = getattr(ipython, 'user_ns', {}).get(RUNTIME_NAMESPACE_NAME)
    install_project_bridge(ipython, runtime)


def unload_ipython_extension(ipython: InteractiveShell) -> None:
    from .capture_display import remove_capture_formatters
    remove_capture_formatters(ipython)
    from .completion import remove_completion_matcher
    remove_completion_matcher(ipython)
    source_session = _source_session_for_shell(ipython, create=False)
    if source_session is not None:
        source_session.detach()
    from .lsp_kernel import detach_project_bridge
    detach_project_bridge(ipython)


def _display_reply(
    reply: RuntimeReply,
    config: NotebookDisplayConfig | None = None,
    *,
    visible_source: str | None = None,
    source_unit: SourceUnitRef | None = None,
    execution_provenance: OperationExecutionProvenance | None = None,
    known_source_unit: SourceUnitRef | None = None,
) -> NotebookDisplay:
    selected = config or NotebookDisplayConfig.presentation()
    has_normalized_diagnostic = reply.diagnostic is not None
    public_diagnostic = (
        {}
        if reply.diagnostic is None
        else diagnostic_to_public_wire(reply.diagnostic)
    )
    runtime_summary = public_diagnostic.pop("runtime_summary", None)
    if public_diagnostic and type(runtime_summary) is not str:
        public_diagnostic = {}
    current_source_unit = _jupyter_source_unit_wire(
        source_unit,
        visible_source,
    )
    diagnostic_unit = _diagnostic_source_unit(reply.diagnostic)
    jupyter_source_unit = None
    if public_diagnostic and diagnostic_unit is not None:
        if diagnostic_unit == source_unit:
            if current_source_unit is None:
                public_diagnostic = {}
            else:
                jupyter_source_unit = current_source_unit
        elif diagnostic_unit == known_source_unit:
            jupyter_source_unit = _source_unit_wire(diagnostic_unit)
        else:
            public_diagnostic = {}
    excerpt = (
        _diagnostic_excerpt(
            public_diagnostic,
            visible_source,
            source_unit,
        )
        if selected.mode == "diagnostic" and diagnostic_unit == source_unit
        else None
    )
    payload: dict[str, object] = {
        "kind": reply.kind.value,
        "operation_id": reply.operation_id,
        "state": reply.state.value,
        "result": _json_value(reply.result),
        "succeeded": reply.succeeded,
        "stop_sequence": reply.stop_sequence,
        "messages": list(reply.messages),
    }
    if has_normalized_diagnostic:
        payload["diagnostic"] = public_diagnostic
        if public_diagnostic and jupyter_source_unit is not None:
            payload["source_unit"] = jupyter_source_unit
        if selected.mode == "diagnostic":
            expert_diagnostic = (
                {}
                if reply.diagnostic is None
                else diagnostic_to_expert_wire(reply.diagnostic)
            )
            if (
                not public_diagnostic
                or expert_diagnostic.get("diagnostic_id")
                != public_diagnostic.get("diagnostic_id")
            ):
                expert_diagnostic = {}
            else:
                expert_diagnostic["excerpt"] = excerpt
                _bind_worker_provenance(
                    expert_diagnostic,
                    source_unit,
                    execution_provenance,
                )
            payload["diagnostic_details"] = expert_diagnostic
    else:
        payload["error"] = reply.error if reply.succeeded else "BSL execution failed"
        payload["location"] = _json_value(reply.location)
    text = (
        f"BSL {reply.kind.value.upper()} operation={reply.operation_id} "
        f"state={reply.state.value}"
    )
    if reply.stop_sequence is not None:
        text += f" stop={reply.stop_sequence}"
    if reply.result is not None:
        text += f" result={reply.result!r}"
    if public_diagnostic:
        text += (
            f" summary={runtime_summary}"
            f" stage={public_diagnostic['stage']}"
            f" confidence={public_diagnostic['mapping_confidence']}"
        )
        unit = payload.get("source_unit")
        if isinstance(unit, dict):
            text += (
                f" cell={unit['unit_id']} revision={unit['revision']}"
                f" source_sha256={unit['source_sha256']}"
            )
        location = public_diagnostic.get("visible_location")
        if isinstance(location, dict):
            text += f" line={location['line']} column={location['column']}"
        synthetic_region = public_diagnostic.get("synthetic_region")
        if synthetic_region is not None:
            text += f" synthetic_region={synthetic_region}"
        text += f" diagnostic_id={public_diagnostic['diagnostic_id']}"
    elif has_normalized_diagnostic:
        text += " diagnostic=unavailable"
    elif payload.get("error"):
        text += f" error={payload['error']}"
    if selected.mode == "presentation" and not reply.succeeded:
        stage = public_diagnostic.get("stage")
        stage_label = {
            "parsing": "разбора",
            "lowering": "подготовки",
            "compilation": "компиляции",
            "execution": "исполнения",
        }.get(stage, "исполнения")
        safe_diagnostic = sanitize_normalized_diagnostic(reply.diagnostic)
        unlocated_origin_verified = (
            source_unit is None
            or (
                current_source_unit is not None
                and isinstance(execution_provenance, OperationExecutionProvenance)
                and isinstance(source_unit, SourceUnitRef)
                and safe_diagnostic is not None
                and execution_provenance.visible_source_sha256
                == source_unit.source_sha256
                and execution_provenance.executed_source_sha256
                == safe_diagnostic.execution_artifact_sha256
                and execution_provenance.source_map_sha256
                == safe_diagnostic.source_map_sha256
            )
        )
        reason = None
        if public_diagnostic and (
            diagnostic_unit is not None or unlocated_origin_verified
        ):
            reason = (
                "перед следующим оператором пропущена точка с запятой «;»"
                if safe_diagnostic is not None
                and safe_diagnostic.code == "missing_statement_separator"
                else _safe_platform_reason(safe_diagnostic)
            )
        text = f"Ошибка {stage_label} BSL"
        if reason is not None:
            text += f": {reason}"
        location = public_diagnostic.get("visible_location")
        if isinstance(location, dict) and "source_unit" in payload:
            text += f" (строка {location['line']}, колонка {location['column']})"
    if selected.mode == "diagnostic":
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return NotebookDisplay(text, payload, diagnostic=selected.mode == "diagnostic")


def _safe_platform_reason(diagnostic: object) -> str | None:
    """Show a bounded 1C cause while withholding redacted private evidence."""
    safe = sanitize_normalized_diagnostic(diagnostic)
    if safe is None or safe.platform_diagnostic is None:
        return None
    message = safe.platform_diagnostic.casefold()
    bounded, _, redacted = bounded_platform_diagnostic(
        safe.platform_diagnostic,
        truncated=safe.platform_diagnostic_truncated,
        redacted=safe.platform_diagnostic_redacted,
    )
    lines = () if bounded is None or redacted else tuple(
        line.strip() for line in bounded.splitlines() if line.strip()
    )
    for index in range(len(lines) - 1, -1, -1):
        marker = re.match(r"по причине:\s*(.*)", lines[index], re.IGNORECASE)
        if marker is None:
            continue
        cause = marker.group(1) or (
            lines[index + 1] if index + 1 < len(lines) else ""
        )
        if readable := _presentation_platform_line(cause):
            return readable
    if "ошибка при вызове метода контекста (выполнить)" in message:
        return "Ошибка при выполнении запроса 1С"
    if "тип не определен" in message or "неизвестный тип" in message:
        return "Тип 1С не определён"
    if "синтаксическая ошибка" in message:
        return "Синтаксическая ошибка"
    if "поле объекта не обнаружено" in message:
        return "Поле объекта не обнаружено"
    if "метод объекта не обнаружен" in message:
        return "Метод объекта не обнаружен"
    if "деление на 0" in message or "деление на ноль" in message:
        return "Деление на ноль"
    return _presentation_platform_line(lines[0]) if lines else None


def _presentation_platform_line(value: str) -> str | None:
    line = _PLATFORM_LOCATION_PREFIX.sub("", value).strip()
    line = re.sub(r"\s+", " ", line)
    if not line:
        return None
    if len(line) > _PRESENTATION_REASON_LIMIT:
        return line[: _PRESENTATION_REASON_LIMIT - 1].rstrip() + "…"
    return line


def _diagnostic_source_unit(
    diagnostic: object,
) -> SourceUnitRef | None:
    safe = sanitize_normalized_diagnostic(diagnostic)
    if safe is None:
        return None
    diagnostic_unit = safe.source_unit
    if diagnostic_unit is None and safe.visible_location is not None:
        diagnostic_unit = safe.visible_location.source_unit
    return diagnostic_unit


def _jupyter_source_unit_wire(
    source_unit: SourceUnitRef | None,
    visible_source: str | None,
) -> dict[str, object] | None:
    if (
        not isinstance(source_unit, SourceUnitRef)
        or type(visible_source) is not str
        or source_unit.source_sha256 != source_sha256(visible_source)
    ):
        return None
    return _source_unit_wire(source_unit)


def _source_unit_wire(source_unit: SourceUnitRef) -> dict[str, object]:
    return {
        "kind": source_unit.kind.value,
        "unit_id": source_unit.unit_id,
        "revision": source_unit.revision,
        "source_sha256": source_unit.source_sha256,
    }


def _diagnostic_excerpt(
    diagnostic: dict[str, object],
    visible_source: str | None,
    source_unit: SourceUnitRef | None,
) -> str | None:
    if (
        not diagnostic
        or type(visible_source) is not str
        or not isinstance(source_unit, SourceUnitRef)
        or source_unit.source_sha256 != source_sha256(visible_source)
    ):
        return None
    location = diagnostic.get("visible_location")
    related = diagnostic.get("related_visible_span")
    span = location.get("span") if isinstance(location, dict) else related
    if not isinstance(span, dict):
        return None
    start = span.get("start")
    end = span.get("end")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start <= end <= len(visible_source)
        or end - start > _DIAGNOSTIC_EXCERPT_LIMIT
    ):
        return None
    return visible_source[start:end]


def _bind_worker_provenance(
    diagnostic: dict[str, object],
    source_unit: SourceUnitRef | None,
    provenance: OperationExecutionProvenance | None,
) -> None:
    if (
        source_unit is None
        or not isinstance(provenance, OperationExecutionProvenance)
        or provenance.visible_source_sha256 != source_unit.source_sha256
        or provenance.executed_source_sha256
        != diagnostic.get("execution_artifact_sha256")
        or provenance.source_map_sha256 != diagnostic.get("source_map_sha256")
    ):
        return
    diagnostic["worker_generation"] = provenance.worker_generation
    diagnostic["worker_manifest_sha256"] = provenance.worker_manifest_sha256


def _accepts_keyword(function: object, keyword_name: str) -> bool:
    try:
        parameters = signature(function).parameters.values()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == keyword_name
        or parameter.kind is Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _display_status(status: RuntimeStatus) -> NotebookDisplay:
    payload = {
        "state": status.state.value,
        "runtime_generation": status.runtime_generation,
        "operation_id": status.operation_id,
        "worker_generation": _json_value(status.worker_generation),
    }
    return NotebookDisplay(
        "1C runtime "
        f"state={status.state.value} generation={status.runtime_generation} "
        f"operation={status.operation_id}",
        payload,
    )

def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(public_artifact_value(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value
