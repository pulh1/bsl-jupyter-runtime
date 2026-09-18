"""RuntimeSession's prepared MAIN calls use the public single-owner facade."""

from contextlib import contextmanager
from threading import RLock

from onec_runtime.execution.public_facade import PublicExecutionFacade
from onec_runtime.session import RuntimeSession


def test_session_main_preparation_and_execution_use_public_facade() -> None:
    calls: list[tuple[object, ...]] = []
    facade = object.__new__(PublicExecutionFacade)
    prepared = object()
    provenance = object()
    attempt = object()
    source_unit = object()

    facade.prepare_bsl = lambda source, *, source_unit: (
        calls.append(("prepare", source, source_unit)) or prepared
    )
    facade.prepared_bsl_execution_provenance = lambda candidate: (
        calls.append(("provenance", candidate)) or provenance
    )

    @contextmanager
    def bind_wait(release):
        calls.append(("bind_wait", release))
        yield

    facade.execution_caller_handoff = bind_wait
    facade.attempt_prepared_main_for_capture = lambda candidate: (
        calls.append(("attempt", candidate)) or attempt
    )
    facade.discard_prepared_bsl = lambda candidate: calls.append(
        ("discard", candidate)
    )

    session = object.__new__(RuntimeSession)
    session.runtime_api = facade
    session._operation_lock = RLock()
    session._closed = False

    assert session.prepare_main_for_capture("Результат = 1;", source_unit=source_unit) is prepared
    assert session.prepared_main_execution_provenance(prepared) is provenance
    assert session.activate_prepared_main_for_capture(prepared) is prepared
    assert session.execute_prepared_main_for_capture(prepared) is attempt
    session.discard_prepared_main_for_capture(prepared)

    assert calls[0] == ("prepare", "Результат = 1;", source_unit)
    assert calls[1] == ("provenance", prepared)
    assert calls[2][0] == "bind_wait"
    assert calls[3] == ("attempt", prepared)
    assert calls[4] == ("discard", prepared)
