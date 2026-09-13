from __future__ import annotations

from pathlib import Path

import pytest

from onec_runtime_mcp.agent.proxies import ProxyRealm, ReleasedProxy, StaleProxy
from onec_runtime_mcp.agent.python_protocol import PythonWorkspaceLimits
from onec_runtime_mcp.agent.python_workspace import PythonWorkspace
from onec_runtime_mcp.agent.python_worker import _Worker
from onec_runtime.errors import ProtocolError


def test_pandas_three_root_module_metadata_keeps_shape_and_memory() -> None:
    class DataFrame:
        __module__ = "pandas"
        shape = (2, 3)
        dtype = None

        def memory_usage(self, *, deep: bool) -> int:
            assert deep is True
            return 48

    metadata = _Worker._metadata(DataFrame())

    assert metadata == {
        "type_name": "pandas.DataFrame",
        "preview": None,
        "shape": [2, 3],
        "dtype": None,
        "memory_bytes": 48,
    }


def limits(**changes: object) -> PythonWorkspaceLimits:
    values: dict[str, object] = {
        "timeout_seconds": 5.0,
        "max_code_bytes": 64 * 1024,
        "max_request_bytes": 256 * 1024,
        "max_response_bytes": 256 * 1024,
        "max_stdout_bytes": 4096,
        "max_stderr_bytes": 4096,
        "max_variables": 100,
        "allowed_imports": ("pandas", "numpy", "math", "time"),
    }
    values.update(changes)
    return PythonWorkspaceLimits(**values)  # type: ignore[arg-type]


def test_python_outputs_survive_frontend_and_onec_restart(tmp_path: Path) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    try:
        result = workspace.run(
            "answer = source + 1",
            inputs={"source": 41},
            outputs=("answer",),
        )
        proxy = result.outputs["answer"]

        # Frontend replacement and a 1C restart do not own this process/lifetime.
        assert workspace.inspect(proxy.proxy_id).preview == 42
        assert workspace.variables() == (proxy,)
        assert proxy.realm is ProxyRealm.PYTHON
    finally:
        workspace.close()


def test_intermediate_locals_and_repr_never_cross_protocol(tmp_path: Path) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    try:
        result = workspace.run(
            "class SecretRepr:\n"
            "    def __repr__(self): return 'Пароль=super-secret'\n"
            "secret_local = SecretRepr()\n"
            "published = 1",
            inputs={},
            outputs=("published",),
        )

        assert set(result.outputs) == {"published"}
        assert [item.qualified_name for item in workspace.variables()] == [
            "python.published"
        ]
        transcript = workspace.public_transcript_bytes()
        assert b"SecretRepr" not in transcript
        assert "super-secret" not in transcript.decode("utf-8")
    finally:
        workspace.close()


def test_reset_invalidates_old_generation_and_clears_bindings(tmp_path: Path) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    try:
        proxy = workspace.run("x = 1", inputs={}, outputs=("x",)).outputs["x"]

        status = workspace.reset()

        assert status.generation == proxy.fence.python_generation + 1
        assert workspace.variables() == ()
        with pytest.raises(StaleProxy):
            workspace.inspect(proxy.proxy_id)
    finally:
        workspace.close()


@pytest.mark.parametrize("name", ["", "a-b", "1value", "a.b", "class", "_private"])
def test_output_names_are_validated_before_worker_access(
    tmp_path: Path, name: str
) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    try:
        before = workspace.public_transcript_bytes()
        with pytest.raises(ValueError, match="output"):
            workspace.run("pass", inputs={}, outputs=(name,))
        assert workspace.public_transcript_bytes() == before
    finally:
        workspace.close()


def test_imports_are_allowlisted_and_reported_without_importing_values(
    tmp_path: Path,
) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    try:
        imports = workspace.imports()
        names = {item.name for item in imports}
        assert {"pandas", "numpy", "math", "time"} <= names

        denied = workspace.run(
            "import subprocess\nanswer = 1",
            inputs={},
            outputs=("answer",),
        )
        assert denied.succeeded is False
        assert denied.outputs == {}
        assert denied.error_category == "denied"
    finally:
        workspace.close()


def test_stdout_and_stderr_are_bounded(tmp_path: Path) -> None:
    workspace = PythonWorkspace.start(
        tmp_path,
        tmp_path / ".runtime",
        limits(max_stdout_bytes=64, max_stderr_bytes=64),
    )
    try:
        result = workspace.run(
            "import sys\nprint('x' * 1000)\nprint('y' * 1000, file=sys.stderr)\nout = 1",
            inputs={},
            outputs=("out",),
        )

        assert len(result.stdout.encode("utf-8")) <= 64
        assert len(result.stderr.encode("utf-8")) <= 64
        assert result.stdout_truncated is True
        assert result.stderr_truncated is True
    finally:
        workspace.close()


def test_non_finite_scalar_has_no_wire_preview(tmp_path: Path) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    try:
        proxy = workspace.run(
            "value = float('nan')", inputs={}, outputs=("value",)
        ).outputs["value"]

        assert proxy.bounded_preview is None
        assert workspace.inspect(proxy.proxy_id).preview is None
    finally:
        workspace.close()


def test_project_state_root_must_be_confined(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError, match="state_root"):
        PythonWorkspace.start(project, tmp_path / "outside", limits())


def test_malformed_worker_response_fails_closed(tmp_path: Path) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    try:
        workspace._request = lambda *_args, **_kwargs: {"unexpected": True}  # type: ignore[method-assign]
        with pytest.raises(ProtocolError, match="worker response"):
            workspace.status()
    finally:
        workspace.close()


def test_ingest_cannot_delete_file_outside_owned_transfer_root(tmp_path: Path) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    outside = tmp_path / "must-survive.payload"
    outside.write_bytes(b"not-owned")
    try:
        response = workspace._request(
            "ingest",
            {
                "path": str(outside),
                "kind": "value",
                "byte_count": outside.stat().st_size,
                "payload_sha256": "0" * 64,
                "max_bytes": 1024,
                "options": {},
            },
        )

        assert response["ok"] is False
        assert response["error_category"] == "denied"
        assert outside.read_bytes() == b"not-owned"
    finally:
        workspace.close()


def test_releasing_old_latest_proxy_reissues_named_binding_without_deleting_object(
    tmp_path: Path,
) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    try:
        old = workspace.run("x = 1", inputs={}, outputs=("x",)).outputs["x"]
        current = workspace.run("x = 2", inputs={}, outputs=("x",)).outputs["x"]

        assert workspace.release(old.proxy_id) is True
        assert workspace.release(old.proxy_id) is False
        replacement = workspace.registry.resolve_name("python.x")
        assert replacement.version == current.version + 1
        assert workspace.inspect(replacement.proxy_id).preview == 2
        with pytest.raises(ReleasedProxy):
            workspace.inspect(current.proxy_id)
    finally:
        workspace.close()


def test_python_binding_names_preserve_python_case_sensitivity(tmp_path: Path) -> None:
    workspace = PythonWorkspace.start(tmp_path, tmp_path / ".runtime", limits())
    try:
        result = workspace.run(
            "value = 1\nValue = 2",
            inputs={},
            outputs=("value", "Value"),
        )

        assert set(result.outputs) == {"value", "Value"}
        assert {item.qualified_name for item in workspace.variables()} == {
            "python.value",
            "python.Value",
        }
        assert workspace.inspect(result.outputs["value"].proxy_id).preview == 1
        assert workspace.inspect(result.outputs["Value"].proxy_id).preview == 2
    finally:
        workspace.close()


def test_python_workspace_source_contains_no_pickle_transport() -> None:
    root = (
        Path(__file__).parents[2]
        / "packages"
        / "mcp"
        / "src"
        / "onec_runtime_mcp"
        / "agent"
    )
    source = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("python_protocol.py", "python_worker.py", "python_workspace.py")
    )
    forbidden = "pick" + "le"
    assert forbidden not in source.casefold()
