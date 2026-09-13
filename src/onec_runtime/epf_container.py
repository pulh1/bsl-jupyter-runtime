from __future__ import annotations

"""Minimal deterministic codec for the 32-bit 1C container used by Worker EPF.

The layout and field interpretation are derived from the MIT-licensed
``saby-integration/v8unpack`` container reader/writer (copyright 2015
Infactum).  This module intentionally implements only the bounded format used
by the one-object-module runtime Worker; it is not a general 1C file library.
"""

from collections.abc import Mapping
from io import BytesIO
from struct import Struct
import zlib


_END_MARKER = 0x7FFFFFFF
_DEFAULT_BLOCK_SIZE = 0x200
_CONTAINER_HEADER = Struct("<4i")
_INDEX_ENTRY = Struct("<3i")
_FILE_ATTRIBUTES = Struct("<QQi")
_BLOCK_HEADER_SIZE = 31
_INDEX_OFFSET = _CONTAINER_HEADER.size


class EpfContainerError(ValueError):
    """The byte sequence is outside the supported bounded container format."""


def raw_deflate(payload: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(payload) + compressor.flush()


def raw_inflate(payload: bytes) -> bytes:
    try:
        decompressor = zlib.decompressobj(wbits=-15)
        result = decompressor.decompress(payload) + decompressor.flush()
    except zlib.error as error:
        raise EpfContainerError("Invalid raw-deflate stream") from error
    if not decompressor.eof:
        raise EpfContainerError("Invalid raw-deflate stream is truncated")
    if decompressor.unused_data or decompressor.unconsumed_tail:
        raise EpfContainerError("Invalid raw-deflate stream contains trailing data")
    return result


def build_container(entries: Mapping[str, bytes]) -> bytes:
    ordered = tuple(sorted(entries.items()))
    if not ordered:
        raise EpfContainerError("Container must contain at least one entry")
    if len({name for name, _payload in ordered}) != len(ordered):
        raise EpfContainerError("Container entry names must be unique")

    stream = BytesIO()
    stream.write(
        _CONTAINER_HEADER.pack(
            _END_MARKER,
            _DEFAULT_BLOCK_SIZE,
            len(ordered),
            0,
        )
    )
    stream.write(b"\x00" * (_BLOCK_HEADER_SIZE + _DEFAULT_BLOCK_SIZE))

    table: list[tuple[int, int]] = []
    for name, payload in ordered:
        if not name or "\x00" in name:
            raise EpfContainerError("Container entry name is invalid")
        if not isinstance(payload, bytes):
            raise TypeError("Container entries must contain bytes")
        attributes = (
            _FILE_ATTRIBUTES.pack(0, 0, 0)
            + name.encode("utf-16-le")
            + b"\x00" * 4
        )
        attribute_offset = _write_document(stream, attributes)
        data_offset = _write_document(
            stream,
            payload,
            minimum_block_size=_DEFAULT_BLOCK_SIZE,
        )
        table.append((attribute_offset, data_offset))

    toc = b"".join(
        _INDEX_ENTRY.pack(attribute_offset, data_offset, _END_MARKER)
        for attribute_offset, data_offset in table
    )
    _write_document(
        stream,
        toc,
        minimum_block_size=_DEFAULT_BLOCK_SIZE,
        offset=_INDEX_OFFSET,
    )
    return stream.getvalue()


def read_container(payload: bytes) -> dict[str, bytes]:
    if len(payload) < _INDEX_OFFSET + _BLOCK_HEADER_SIZE:
        raise EpfContainerError("Container is truncated")
    try:
        end_marker, block_size, count, reserved = _CONTAINER_HEADER.unpack_from(
            payload, 0
        )
    except Exception as error:
        raise EpfContainerError("Container header is invalid") from error
    if (
        end_marker != _END_MARKER
        or block_size != _DEFAULT_BLOCK_SIZE
        or count <= 0
        or reserved != 0
    ):
        raise EpfContainerError("Container header is unsupported")

    toc = _read_document(payload, _INDEX_OFFSET)
    if not toc or len(toc) % _INDEX_ENTRY.size:
        raise EpfContainerError("Container table of contents is invalid")

    result: dict[str, bytes] = {}
    # Platform-built nested module containers may retain a header count that is
    # one lower than the actual TOC entry count.  v8unpack likewise treats the
    # bounded TOC document as authoritative.
    toc_count = len(toc) // _INDEX_ENTRY.size
    if toc_count not in {count, count + 1}:
        raise EpfContainerError("Container entry count does not match its bounded TOC")
    for index in range(toc_count):
        position = index * _INDEX_ENTRY.size
        attribute_offset, data_offset, marker = _INDEX_ENTRY.unpack_from(toc, position)
        if marker != _END_MARKER:
            raise EpfContainerError("Container table marker is invalid")
        attributes = _read_document(payload, attribute_offset)
        if len(attributes) < _FILE_ATTRIBUTES.size + 4:
            raise EpfContainerError("Container entry attributes are truncated")
        name_bytes = attributes[_FILE_ATTRIBUTES.size :]
        try:
            name = name_bytes.decode("utf-16-le").split("\x00", 1)[0]
        except UnicodeDecodeError as error:
            raise EpfContainerError("Container entry name is invalid") from error
        if not name or name in result:
            raise EpfContainerError("Container entry name is empty or duplicated")
        result[name] = _read_document(payload, data_offset)
    return result


def _write_document(
    stream: BytesIO,
    data: bytes,
    *,
    minimum_block_size: int = 0,
    offset: int | None = None,
) -> int:
    if offset is None:
        stream.seek(0, 2)
        offset = stream.tell()
    else:
        stream.seek(offset)
    current_block_size = max(minimum_block_size, len(data))
    if current_block_size > _END_MARKER:
        raise EpfContainerError("Container document is too large")
    header = (
        f"\r\n{len(data):08x} {current_block_size:08x} "
        f"{_END_MARKER:08x} \r\n"
    ).encode("ascii")
    if len(header) != _BLOCK_HEADER_SIZE:
        raise AssertionError("Unexpected 1C block header size")
    stream.write(header)
    stream.write(data)
    stream.write(b"\x00" * (current_block_size - len(data)))
    return offset


def _read_document(payload: bytes, offset: int) -> bytes:
    if offset < _INDEX_OFFSET or offset + _BLOCK_HEADER_SIZE > len(payload):
        raise EpfContainerError("Container document offset is outside the file")
    header = payload[offset : offset + _BLOCK_HEADER_SIZE]
    if (
        header[:2] != b"\r\n"
        or header[10:11] != b" "
        or header[19:20] != b" "
        or header[28:29] != b" "
        or header[29:] != b"\r\n"
    ):
        raise EpfContainerError("Container document header is invalid")
    try:
        document_size = int(header[2:10], 16)
        current_block_size = int(header[11:19], 16)
        next_offset = int(header[20:28], 16)
    except ValueError as error:
        raise EpfContainerError("Container document size is invalid") from error
    if current_block_size < document_size or next_offset != _END_MARKER:
        raise EpfContainerError("Chained container documents are unsupported")
    start = offset + _BLOCK_HEADER_SIZE
    end = start + current_block_size
    if end > len(payload):
        raise EpfContainerError("Container document is truncated")
    return payload[start : start + document_size]
