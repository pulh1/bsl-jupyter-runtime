from __future__ import annotations

from inspect import signature

from onec_runtime.runtime_api import PrototypeRuntimeApi
from onec_runtime.session import RuntimeSession
from onec_runtime.worker_breakpoints import WorkerBreakpointReloadPolicy


def test_public_surface_is_logical_and_has_no_legacy_loader() -> None:
    for runtime_type in (PrototypeRuntimeApi, RuntimeSession):
        assert not hasattr(runtime_type, "load_worker")
        assert not hasattr(runtime_type, "worker_version")
        assert not hasattr(runtime_type, "worker_calculate")
        load_signature = signature(runtime_type.load_worker_modules)
        assert (
            load_signature.parameters["breakpoint_policy"].default
            is WorkerBreakpointReloadPolicy.STRICT
        )
        assert "force" not in load_signature.parameters
        for name in (
            "add_worker_breakpoint",
            "remove_worker_breakpoint",
            "set_worker_breakpoint_enabled",
            "worker_breakpoint_status",
            "list_worker_breakpoints",
            "last_worker_breakpoint_reload_report",
            "resume_debug_stop",
        ):
            assert callable(getattr(runtime_type, name))


def test_logical_breakpoint_signatures_match_on_api_and_session() -> None:
    for name in (
        "remove_worker_breakpoint",
        "set_worker_breakpoint_enabled",
        "worker_breakpoint_status",
        "list_worker_breakpoints",
        "resume_debug_stop",
    ):
        assert signature(getattr(PrototypeRuntimeApi, name)) == signature(
            getattr(RuntimeSession, name)
        )


def test_session_breakpoint_installation_requires_only_path_and_line() -> None:
    assert tuple(signature(RuntimeSession.add_worker_breakpoint).parameters) == (
        "self", "path", "line",
    )
