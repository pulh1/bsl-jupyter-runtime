from __future__ import annotations

from pathlib import Path
from struct import pack_into

import pytest

from onec_runtime.epf_container import (
    EpfContainerError,
    build_container,
    raw_deflate,
    raw_inflate,
    read_container,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "worker-v2-platform.epf"
METADATA_STREAM = "2a00a4fa-8ea9-4dc4-9de1-472044c40101"
MODULE_STREAM = "2a00a4fa-8ea9-4dc4-9de1-472044c40102.0"
EXPECTED_STREAMS = (
    METADATA_STREAM,
    MODULE_STREAM,
    "copyinfo",
    "root",
    "version",
    "versions",
)


def test_reads_real_platform_worker_and_nested_module_container() -> None:
    streams = read_container(FIXTURE.read_bytes())

    assert tuple(streams) == EXPECTED_STREAMS
    nested = read_container(raw_inflate(streams[MODULE_STREAM]))
    assert tuple(nested) == ("info", "text")
    assert nested["info"].startswith(b"\xef\xbb\xbf{3,1,0")
    assert "Функция Версия() Экспорт" in nested["text"].decode("utf-8-sig")


def test_container_round_trip_preserves_entries_and_is_deterministic() -> None:
    entries = {
        "root": b"root-data",
        "module": build_container({"info": b"info", "text": b"text"}),
    }

    first = build_container(entries)
    second = build_container(entries)

    assert first == second
    assert read_container(first) == dict(sorted(entries.items()))


def test_raw_deflate_round_trip_is_deterministic() -> None:
    payload = (b"worker-source\r\n" * 1_000) + bytes(range(256))

    first = raw_deflate(payload)

    assert first == raw_deflate(payload)
    assert raw_inflate(first) == payload


def test_rejects_truncated_raw_deflate_stream() -> None:
    compressed = raw_deflate(b"worker-source" * 100)

    with pytest.raises(EpfContainerError, match="raw-deflate"):
        raw_inflate(compressed[:-1])


def test_rejects_header_count_unrelated_to_bounded_toc() -> None:
    payload = bytearray(build_container({"root": b"root", "text": b"text"}))
    pack_into("<i", payload, 8, 100)

    with pytest.raises(EpfContainerError, match="entry count"):
        read_container(bytes(payload))


@pytest.mark.parametrize(
    "payload",
    (
        b"",
        b"not-a-container",
        b"\xff\xff\xff\x7f" + b"\x00" * 20,
        FIXTURE.read_bytes()[:-50],
    ),
)
def test_rejects_truncated_or_invalid_containers(payload: bytes) -> None:
    with pytest.raises(EpfContainerError):
        read_container(payload)
