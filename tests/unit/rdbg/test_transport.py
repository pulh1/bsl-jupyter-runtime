import httpx
import pytest

from onec_runtime.errors import (
    ProtocolError,
    RdbgDebugUiNotRegistered,
    RdbgTransportError,
    RdbgTransportTimeout,
)
from onec_runtime.rdbg.transport import RdbgTransport


def test_http_connect_failure_is_typed_as_transport_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("planned disconnect", request=request)

    client = httpx.Client(transport=httpx.MockTransport(fail))
    transport = RdbgTransport("127.0.0.1", 12345, client=client)

    with pytest.raises(RdbgTransportError, match="planned disconnect"):
        transport.request("pingDebugUIParams")


def test_ping_read_timeout_is_treated_as_an_empty_long_poll() -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("planned empty long poll", request=request)

    client = httpx.Client(transport=httpx.MockTransport(time_out))
    transport = RdbgTransport("127.0.0.1", 12345, client=client)

    assert transport.request("pingDebugUIParams", timeout_s=0.1) == b""


def test_evaluation_ping_read_timeout_is_reported_not_silently_ignored() -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("planned lost long poll", request=request)

    client = httpx.Client(transport=httpx.MockTransport(time_out))
    transport = RdbgTransport("127.0.0.1", 12345, client=client)

    with pytest.raises(RdbgTransportTimeout, match="planned lost long poll"):
        transport.request(
            "pingDebugUIParams", timeout_s=15.0, read_timeout_as_empty=False,
        )


@pytest.mark.parametrize(
    "timeout_type",
    (
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
    ),
)
def test_non_ping_http_timeout_is_typed_as_transport_deadline(timeout_type) -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise timeout_type("planned request timeout", request=request)

    client = httpx.Client(transport=httpx.MockTransport(time_out))
    transport = RdbgTransport("127.0.0.1", 12345, client=client)

    with pytest.raises(RdbgTransportTimeout, match="planned request timeout"):
        transport.request("getDbgAllTargetStates", timeout_s=0.1)


@pytest.mark.parametrize(
    "timeout_type",
    (httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout),
)
def test_ping_only_treats_read_timeout_as_empty_long_poll(timeout_type) -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise timeout_type("planned ping deadline", request=request)

    client = httpx.Client(transport=httpx.MockTransport(time_out))
    transport = RdbgTransport("127.0.0.1", 12345, client=client)

    with pytest.raises(RdbgTransportTimeout, match="planned ping deadline"):
        transport.request("pingDebugUIParams", timeout_s=0.1)


def test_http_500_remains_protocol_error() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(500, content=b"planned server failure")
        )
    )
    transport = RdbgTransport("127.0.0.1", 12345, client=client)

    with pytest.raises(ProtocolError, match="HTTP 500") as caught:
        transport.request("pingDebugUIParams")

    assert not isinstance(caught.value, RdbgTransportError)


@pytest.mark.parametrize("command", ["pingDebugUIParams", "detachDebugUI"])
def test_missing_debug_ui_400_has_a_precise_error_type(command: str) -> None:
    response = (
        '<exception><descr>UI+ - часть отладки не зарегистрирована'
        '</descr></exception>'
    ).encode("utf-8")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, content=response)
        )
    )
    transport = RdbgTransport("127.0.0.1", 12345, client=client)

    with pytest.raises(RdbgDebugUiNotRegistered, match="not registered"):
        transport.request(command)


def test_other_http_400_does_not_look_like_missing_debug_ui() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, content=b"unrelated bad request")
        )
    )
    transport = RdbgTransport("127.0.0.1", 12345, client=client)

    with pytest.raises(ProtocolError) as caught:
        transport.request("pingDebugUIParams")
    assert not isinstance(caught.value, RdbgDebugUiNotRegistered)
