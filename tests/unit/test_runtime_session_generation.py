from __future__ import annotations

import onec_runtime.session as session_module


def test_bootstrap_controller_assigns_monotonically_advancing_runtime_generations(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    calls: list[dict[str, object]] = []

    def controller(*_args: object, **kwargs: object) -> object:
        calls.append(dict(kwargs))
        return object()

    monkeypatch.setattr(session_module, "PrototypeRuntimeController", controller)

    first = session_module._bootstrap_runtime_controller(
        object(), object(), object()  # type: ignore[arg-type]
    )
    second = session_module._bootstrap_runtime_controller(
        object(), object(), object()  # type: ignore[arg-type]
    )

    assert first is not second
    assert [call["command_timeout_s"] for call in calls] == [90.0, 90.0]
    generations = [call["runtime_generation"] for call in calls]
    assert all(type(generation) is int for generation in generations)
    assert generations[1] > generations[0]
