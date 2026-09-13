from __future__ import annotations

import json
from uuid import uuid4

import pytest

from onec_runtime_mcp.agent.contracts import (
    AgentOperationState,
    BackendExecution,
    StateChanged,
    to_wire,
)
import onec_runtime_mcp.agent.contracts as contracts
from onec_runtime_mcp.agent.facade_contracts import (
    AgentOperationKind,
    OperationViewFacts,
)
from onec_runtime_mcp.agent.operation_view import OperationViewProjector
from onec_runtime_mcp.agent.operations import OperationRegistry
from onec_runtime_mcp.agent.proxies import (
    ProxyConsistency,
    ProxyDescriptor,
    ProxyFence,
    ProxyLifetime,
    ProxyProvenance,
    ProxyRealm,
)
from onec_runtime.bsl import (
    DiagnosticStage,
    VisibleSourceContext,
    parse_platform_diagnostic,
    remap_platform_diagnostic,
)
from onec_runtime.bsl.source_maps import (
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)


def _command() -> dict[str, object]:
    return {
        "operation_kind": "code_run",
        "runtime_id": "runtime-1",
        "runtime_generation": 3,
        "code_id": "cell-main",
        "revision": 7,
        "source_sha256": "a" * 64,
        "inputs_sha256": "b" * 64,
    }


def _proxy(operation_id: str) -> ProxyDescriptor:
    return ProxyDescriptor(
        proxy_id=str(uuid4()),
        realm=ProxyRealm.ONEC,
        lifetime=ProxyLifetime.CONTEXT,
        qualified_name="bsl.Ответ",
        type_name="Число",
        version=1,
        consistency=ProxyConsistency.EXACT,
        fence=ProxyFence(
            runtime_id="runtime-1",
            runtime_generation=3,
            context_generation=4,
        ),
        provenance=ProxyProvenance(
            cell_id="cell-main",
            revision=7,
            source_sha256="a" * 64,
            operation_id=operation_id,
        ),
        capabilities=("inspect",),
    )


def _exact_diagnostic(source: str):  # type: ignore[no-untyped-def]
    unit = SourceUnitRef(
        SourceUnitKind.NOTEBOOK_CELL,
        "cell-main",
        7,
        source_sha256(source),
    )
    mapped = mapped_visible_source(source, unit)
    return remap_platform_diagnostic(
        parse_platform_diagnostic(
            "{<Неизвестный модуль>(1, 1)}: rdbg_pid=9182 token=private"
        ),
        mapped,
        stage=DiagnosticStage.EXECUTION,
        visible_source_context=VisibleSourceContext({unit: source}),
    )


def test_operation_view_pages_messages_without_starting_another_operation(tmp_path) -> None:
    registry = OperationRegistry(tmp_path)
    submitted = registry.submit(
        _command(),
        lambda: BackendExecution.completed(
            messages=("one", "two"),
            result_present=False,
        ),
    )
    registry.wait(submitted.operation_id, timeout_s=2)
    count_before = len(registry.list())

    view = OperationViewProjector(registry, lambda _proxy_id: None).project(
        submitted.operation_id,
        message_limit=1,
    )

    assert view.operation.kind is AgentOperationKind.CODE_RUN
    assert view.state is AgentOperationState.COMPLETED
    assert view.messages == ("one",)
    assert view.next_message_cursor > 0
    assert view.truncation.messages is True
    assert view.changed_variables == ()
    assert dict(view.outputs) == {}
    assert view.capture is None
    assert view.failure is None
    assert len(registry.list()) == count_before
    assert "raw_value" not in json.dumps(to_wire(view), ensure_ascii=False)
    registry.shutdown()


def test_operation_view_facts_and_kind_survive_registry_recovery(tmp_path) -> None:
    registry = OperationRegistry(tmp_path)
    submitted = registry.submit(
        _command(),
        lambda: BackendExecution.completed(messages=(), result_present=False),
    )
    registry.wait(submitted.operation_id, timeout_s=2)
    proxy = _proxy(submitted.operation_id)
    registry.set_view_facts(
        submitted.operation_id,
        OperationViewFacts(
            changed_variables=(proxy,),
            outputs={"answer": proxy},
        ),
    )
    registry.shutdown()

    recovered = OperationRegistry(tmp_path)
    view = OperationViewProjector(
        recovered,
        lambda _proxy_id: (_ for _ in ()).throw(AssertionError("must not resolve")),
    ).project(submitted.operation_id)

    assert view.operation.kind is AgentOperationKind.CODE_RUN
    assert view.changed_variables == (proxy,)
    assert dict(view.outputs) == {"answer": proxy}
    recovered.shutdown()


def test_operation_view_reports_output_truncation_with_stable_alias_order(tmp_path) -> None:
    registry = OperationRegistry(tmp_path)
    submitted = registry.submit(
        _command(),
        lambda: BackendExecution.completed(messages=(), result_present=False),
    )
    registry.wait(submitted.operation_id, timeout_s=2)
    first = _proxy(submitted.operation_id)
    second = _proxy(submitted.operation_id)
    registry.set_view_facts(
        submitted.operation_id,
        OperationViewFacts(
            outputs={"first": first, "second": second},
        ),
    )
    proxies = {first.proxy_id: first, second.proxy_id: second}

    view = OperationViewProjector(registry, proxies.get).project(
        submitted.operation_id,
        output_limit=1,
    )

    assert tuple(view.outputs) == ("first",)
    assert view.truncation.outputs is True
    registry.shutdown()


def test_operation_view_persists_visible_diagnostic_without_expert_details(
    tmp_path,
) -> None:
    """Break caught: terminal diagnostics disappear on journal restart or leak raw text."""
    source = "СекретныйРасчет = 1;"
    diagnostic = _exact_diagnostic(source)
    registry = OperationRegistry(tmp_path)
    release = __import__("threading").Event()
    holder: list[str] = []
    provenance = contracts.OperationExecutionProvenance(
        visible_source_sha256=source_sha256(source),
        executed_source_sha256=diagnostic.execution_artifact_sha256,
        source_map_sha256=diagnostic.source_map_sha256,
        mode="main",
    )

    def execute() -> BackendExecution:
        assert release.wait(1)
        registry.set_execution_provenance(holder[0], provenance)
        return BackendExecution(
            AgentOperationState.FAILED,
            ("BSL execution failed",),
            False,
            "ready",
            failure_stage="execution",
            diagnostic=diagnostic,
            state_changed=StateChanged.NO,
        )

    submitted = registry.submit(
        {**_command(), "source_sha256": source_sha256(source)},
        execute,
    )
    holder.append(submitted.operation_id)
    release.set()
    assert (
        registry.wait(submitted.operation_id, timeout_s=2).state
        is AgentOperationState.FAILED
    )
    public_journal = (
        tmp_path / ".runtime" / "agent-service" / "operations.jsonl"
    ).read_text(encoding="utf-8")
    assert source not in public_journal
    assert "rdbg_pid" not in public_journal
    assert "token=private" not in public_journal
    assert "platform_diagnostic" not in public_journal
    registry.shutdown()

    recovered = OperationRegistry(tmp_path)
    view = OperationViewProjector(
        recovered,
        lambda _proxy: (_ for _ in ()).throw(AssertionError("must not resolve")),
    ).project(submitted.operation_id)

    assert view.failure["diagnostic"]["mapping_confidence"] == "exact"
    assert view.failure["diagnostic"]["stage"] == "execution"
    assert view.failure["state_changed"] == "no"
    assert view.execution_provenance == provenance
    assert set(view.failure["diagnostic"]) == {
        "diagnostic_id",
        "stage",
        "mapping_confidence",
        "visible_location",
        "related_visible_span",
        "excerpt",
        "synthetic_region",
    }
    assert "platform_diagnostic" not in json.dumps(to_wire(view), ensure_ascii=False)
    recovered.shutdown()


def test_missing_private_diagnostic_keeps_compact_public_view_without_raw_fallback(
    tmp_path,
) -> None:
    """Break caught: recovery resolves source/raw error when private detail is missing."""
    source = "Результат = 1;"
    diagnostic = _exact_diagnostic(source)
    registry = OperationRegistry(tmp_path)
    submitted = registry.submit(
        _command(),
        lambda: BackendExecution(
            AgentOperationState.FAILED,
            (),
            False,
            "ready",
            failure_stage="execution",
            diagnostic=diagnostic,
            state_changed=StateChanged.NO,
        ),
    )
    assert registry.wait(submitted.operation_id, timeout_s=2).state is AgentOperationState.FAILED
    registry.shutdown()
    private_path = (
        tmp_path / ".runtime" / "agent-service" / "diagnostics.private.jsonl"
    )
    assert private_path.exists()
    private_path.unlink()

    recovered = OperationRegistry(tmp_path)
    view = OperationViewProjector(
        recovered,
        lambda _proxy: (_ for _ in ()).throw(AssertionError("must not resolve")),
    ).project(submitted.operation_id)

    assert view.failure["diagnostic"]["diagnostic_id"] == diagnostic.diagnostic_id
    assert view.failure["diagnostic"]["excerpt"] is None
    assert "rdbg_pid" not in json.dumps(to_wire(view), ensure_ascii=False)
    recovered.shutdown()


def test_compact_operation_view_never_loads_private_diagnostics_or_proxies(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an ordinary Agent view opens the expert-only journal."""
    source = "Результат = 1;"
    diagnostic = _exact_diagnostic(source)
    registry = OperationRegistry(tmp_path)
    registry.record_diagnostic(diagnostic, excerpt=source)
    submitted = registry.submit(
        {**_command(), "source_sha256": source_sha256(source)},
        lambda: BackendExecution(
            AgentOperationState.FAILED,
            (),
            False,
            "ready",
            failure_stage="execution",
            diagnostic=diagnostic,
            state_changed=StateChanged.NO,
        ),
    )
    assert (
        registry.wait(submitted.operation_id, timeout_s=2).state
        is AgentOperationState.FAILED
    )
    registry.shutdown()

    recovered = OperationRegistry(tmp_path)
    monkeypatch.setattr(
        recovered,
        "_ensure_private_diagnostics_loaded_locked",
        lambda: (_ for _ in ()).throw(
            AssertionError("compact view must not load private storage")
        ),
    )
    view = OperationViewProjector(
        recovered,
        lambda _proxy: (_ for _ in ()).throw(
            AssertionError("compact view must not resolve proxies")
        ),
    ).project(submitted.operation_id)

    assert view.failure["diagnostic"]["diagnostic_id"] == diagnostic.diagnostic_id
    assert view.failure["diagnostic"]["excerpt"] is None
    recovered.shutdown()


def test_invalid_utf8_private_diagnostic_keeps_compact_public_view(
    tmp_path,
) -> None:
    """Break caught: invalid private UTF-8 aborts otherwise safe public projection."""
    source = "Результат = 1;"
    diagnostic = _exact_diagnostic(source)
    registry = OperationRegistry(tmp_path)
    registry.record_diagnostic(diagnostic, excerpt=source)
    submitted = registry.submit(
        {**_command(), "source_sha256": source_sha256(source)},
        lambda: BackendExecution(
            AgentOperationState.FAILED,
            (),
            False,
            "ready",
            failure_stage="execution",
            diagnostic=diagnostic,
            state_changed=StateChanged.NO,
        ),
    )
    assert registry.wait(submitted.operation_id, timeout_s=2).state is AgentOperationState.FAILED
    registry.shutdown()
    private_path = (
        tmp_path / ".runtime" / "agent-service" / "diagnostics.private.jsonl"
    )
    private_path.write_bytes(b"\xff\xfeinvalid-private-record")

    recovered = OperationRegistry(tmp_path)
    view = OperationViewProjector(
        recovered,
        lambda _proxy: (_ for _ in ()).throw(AssertionError("must not resolve")),
    ).project(submitted.operation_id)

    assert view.failure["diagnostic"]["diagnostic_id"] == diagnostic.diagnostic_id
    assert view.failure["diagnostic"]["excerpt"] is None
    encoded = json.dumps(to_wire(view), ensure_ascii=False)
    assert source not in encoded
    assert "rdbg_pid" not in encoded
    recovered.shutdown()


def test_conflicting_private_diagnostic_recovery_omits_excerpt_fail_closed(
    tmp_path,
) -> None:
    """Break caught: a later duplicate can revive conflicted private evidence."""
    source = "Результат = 1;"
    diagnostic = _exact_diagnostic(source)
    registry = OperationRegistry(tmp_path)
    registry.record_diagnostic(diagnostic, excerpt=source)
    submitted = registry.submit(
        _command(),
        lambda: BackendExecution(
            AgentOperationState.FAILED,
            (),
            False,
            "ready",
            failure_stage="execution",
            diagnostic=diagnostic,
            state_changed=StateChanged.NO,
        ),
    )
    assert registry.wait(submitted.operation_id, timeout_s=2).state is AgentOperationState.FAILED
    registry.shutdown()

    private_path = (
        tmp_path / ".runtime" / "agent-service" / "diagnostics.private.jsonl"
    )
    original_line = private_path.read_text(encoding="utf-8").strip()
    conflicting = json.loads(original_line)
    conflicting["excerpt"] = "ПоддельныйФрагмент = 2;"
    private_path.write_text(
        "\n".join(
            (
                original_line,
                json.dumps(conflicting, ensure_ascii=False, separators=(",", ":")),
                original_line,
                "{corrupt",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    recovered = OperationRegistry(tmp_path)
    view = OperationViewProjector(recovered, lambda _proxy: None).project(
        submitted.operation_id
    )

    assert view.failure["diagnostic"]["diagnostic_id"] == diagnostic.diagnostic_id
    assert view.failure["diagnostic"]["excerpt"] is None
    with pytest.raises(ValueError, match="conflicted"):
        recovered.record_diagnostic(diagnostic, excerpt=source)
    assert recovered.diagnostic_excerpt(diagnostic.diagnostic_id) is None
    recovered.shutdown()
