from __future__ import annotations

from dataclasses import dataclass
from time import monotonic_ns
from typing import Callable

import httpx

from onec_runtime.errors import (
    ProtocolError,
    RdbgDebugUiNotRegistered,
    RdbgTransportError,
    RdbgTransportTimeout,
)


@dataclass(frozen=True)
class TranscriptEntry:
    monotonic_ns: int
    command: str
    request: bytes
    status_code: int | None
    response: bytes
    duration_ms: float
    error: str = ""


class RdbgTransport:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        transcript: Callable[[TranscriptEntry], None] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = f"http://{host}:{port}/e1crdbg/rdbg"
        self._transcript = transcript
        self._client = client or httpx.Client(
            headers={
                "User-Agent": "1CV8",
                "Accept": "application/xml",
                "Content-Type": "application/xml; charset=utf-8",
            },
            trust_env=False,
        )

    def request(
        self,
        command: str,
        payload: bytes = b"",
        *,
        timeout_s: float = 60.0,
        dbgui: str | None = None,
    ) -> bytes:
        start_ns = monotonic_ns()
        status: int | None = None
        response_body = b""
        error_text = ""
        try:
            params = {"cmd": command}
            if dbgui is not None:
                params["dbgui"] = dbgui
            response = self._client.post(
                self._base_url,
                params=params,
                content=payload,
                timeout=timeout_s,
            )
            status = response.status_code
            response_body = response.content
            if not 200 <= response.status_code < 300:
                bounded = response_body[:1000].decode("utf-8", errors="replace")
                if (
                    response.status_code == 400
                    and "UI+ -" in bounded
                    and "часть отладки не зарегистрирована" in bounded.casefold()
                ):
                    raise RdbgDebugUiNotRegistered(
                        f"RDBG debug UI is not registered during {command}"
                    )
                raise ProtocolError(
                    f"RDBG {command} returned HTTP {response.status_code}: {bounded}"
                )
            return response_body
        except httpx.ReadTimeout as error:
            error_text = str(error)
            if command == "pingDebugUIParams":
                return b""
            raise RdbgTransportTimeout(
                f"RDBG {command} transport failure: {error}"
            ) from error
        except httpx.HTTPError as error:
            error_text = str(error)
            raise RdbgTransportError(
                f"RDBG {command} transport failure: {error}"
            ) from error
        except ProtocolError as error:
            error_text = str(error)
            raise
        finally:
            if self._transcript is not None:
                self._transcript(
                    TranscriptEntry(
                        monotonic_ns=start_ns,
                        command=command,
                        request=payload,
                        status_code=status,
                        response=response_body,
                        duration_ms=(monotonic_ns() - start_ns) / 1_000_000,
                        error=error_text,
                    )
                )

    def test_server(self, *, timeout_s: float = 5.0) -> float:
        start_ns = monotonic_ns()
        try:
            response = self._client.post(
                self._base_url.replace("/rdbg", "/rdbgTest"),
                params={"cmd": "test"},
                content=b"",
                timeout=timeout_s,
            )
            if response.status_code >= 500:
                raise ProtocolError(
                    f"RDBG test endpoint returned HTTP {response.status_code}"
                )
        except httpx.HTTPError as error:
            raise RdbgTransportError(f"RDBG test endpoint failure: {error}") from error
        return (monotonic_ns() - start_ns) / 1_000_000

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RdbgTransport":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
