from __future__ import annotations

from base64 import b64encode
from dataclasses import fields, replace
from hashlib import sha256

import pytest

from onec_runtime.table_materialization import (
    ReferencePolicy,
    TableCleanupError,
    TableMaterializationError,
    TableMaterializer,
    TableTransferChunk,
    TableTransferManifest,
)
from test_table_materialization import payload


class Backend:
    def __init__(self, content: bytes, *, chunk_size: int = 80) -> None:
        self.content = content
        self.chunks = [
            content[offset : offset + chunk_size]
            for offset in range(0, len(content), chunk_size)
        ]
        self.calls: list[tuple[object, ...]] = []
        schema_line = content.splitlines()[0]
        self.value = TableTransferManifest(
            token="transfer-7",
            runtime_generation=3,
            context_generation=5,
            byte_count=len(content),
            schema_byte_count=len(schema_line),
            chunk_count=len(self.chunks),
            payload_sha256=sha256(content).hexdigest(),
            schema_sha256=sha256(schema_line).hexdigest(),
        )
        self.close_error: BaseException | None = None
    def start(self, handle: str, chunk_size: int) -> str:
        self.calls.append(("start", handle, chunk_size))
        return self.value.token

    def manifest(self, token: str) -> TableTransferManifest:
        self.calls.append(("manifest", token))
        return self.value

    def chunk(self, token: str, sequence: int) -> TableTransferChunk:
        self.calls.append(("chunk", token, sequence))
        return TableTransferChunk(sequence, b64encode(self.chunks[sequence]).decode("ascii"))

    def close(self, token: str) -> None:
        self.calls.append(("close", token))
        if self.close_error is not None:
            raise self.close_error


def test_manifest_declares_exact_schema_byte_count() -> None:
    assert "schema_byte_count" in {
        field.name for field in fields(TableTransferManifest)
    }


def test_downloads_ordered_chunks_validates_integrity_and_closes() -> None:
    backend = Backend(payload())
    materializer = TableMaterializer(
        backend,
        runtime_generation=3,
        context_generation=5,
        chunk_size=80,
    )

    frame = materializer.to_df("table-1", ReferencePolicy(refs="both"))

    assert frame.loc[0, "Сотрудник"] == "Иванов И.И."
    assert backend.calls == [
        ("start", "table-1", 80),
        ("manifest", "transfer-7"),
        *[("chunk", "transfer-7", index) for index in range(len(backend.chunks))],
        ("close", "transfer-7"),
    ]


def test_schema_hash_covers_json_record_without_jsonl_delimiter() -> None:
    content = payload()
    backend = Backend(content)
    schema_record = content.splitlines()[0]
    backend.value = replace(
        backend.value,
        schema_sha256=sha256(schema_record).hexdigest(),
    )

    frame = TableMaterializer(
        backend,
        runtime_generation=3,
        context_generation=5,
    ).to_df("table-1", ReferencePolicy(refs="both"))

    assert frame.loc[0, "Сотрудник"] == "Иванов И.И."


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"byte_count": 1}, "byte count"),
        ({"payload_sha256": "0" * 64}, "payload SHA-256"),
        ({"schema_sha256": "f" * 64}, "schema SHA-256"),
        ({"runtime_generation": 4}, "runtime generation"),
        ({"context_generation": 6}, "context generation"),
    ],
)
def test_rejects_manifest_mismatch_and_still_closes(
    change: dict[str, object], message: str
) -> None:
    backend = Backend(payload())
    backend.value = replace(backend.value, **change)
    materializer = TableMaterializer(backend, runtime_generation=3, context_generation=5)

    with pytest.raises(TableMaterializationError, match=message):
        materializer.to_df("table-1", ReferencePolicy())

    assert backend.calls[-1] == ("close", "transfer-7")


def test_schema_mismatch_reports_only_expected_and_observed_hashes() -> None:
    backend = Backend(payload())
    observed = backend.value.schema_sha256
    expected = "f" * 64
    backend.value = replace(backend.value, schema_sha256=expected)

    with pytest.raises(TableMaterializationError) as raised:
        TableMaterializer(backend, runtime_generation=3, context_generation=5).to_df(
            "table-1", ReferencePolicy()
        )

    assert str(raised.value) == (
        "table schema SHA-256 does not match manifest: "
        f"expected={expected}, observed={observed}"
    )


def test_rejects_chunk_sequence_and_invalid_base64() -> None:
    class BrokenChunkBackend(Backend):
        def chunk(self, token: str, sequence: int) -> TableTransferChunk:
            if sequence == 1:
                return TableTransferChunk(0, "not base64!")
            return super().chunk(token, sequence)

    backend = BrokenChunkBackend(payload())

    with pytest.raises(TableMaterializationError, match="chunk sequence"):
        TableMaterializer(backend, runtime_generation=3, context_generation=5).to_df(
            "table-1", ReferencePolicy()
        )

    assert backend.calls[-1] == ("close", "transfer-7")


def test_preserves_primary_and_cleanup_failures() -> None:
    backend = Backend(payload())
    backend.value = replace(backend.value, payload_sha256="0" * 64)
    backend.close_error = RuntimeError("cleanup failed")

    with pytest.raises(TableCleanupError) as caught:
        TableMaterializer(backend, runtime_generation=3, context_generation=5).to_df(
            "table-1", ReferencePolicy()
        )

    assert isinstance(caught.value.primary, TableMaterializationError)
    assert str(caught.value.cleanup) == "cleanup failed"


def test_cleanup_failure_after_success_is_not_swallowed() -> None:
    backend = Backend(payload())
    backend.close_error = RuntimeError("cleanup failed")

    with pytest.raises(TableCleanupError) as caught:
        TableMaterializer(backend, runtime_generation=3, context_generation=5).to_df(
            "table-1", ReferencePolicy()
        )

    assert caught.value.primary is None
    assert str(caught.value.cleanup) == "cleanup failed"
