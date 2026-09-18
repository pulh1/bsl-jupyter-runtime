"""Hash-only source identity for an admitted notebook statement."""

from __future__ import annotations

from onec_runtime.bsl.source_maps import MappedSource
from onec_runtime.errors import ProtocolError
from onec_runtime.execution.capture.policy import CapturePreparedPayload
from onec_runtime.execution.contracts import CommonCell, PreparedCell
from onec_runtime.execution.main.policy import MainPreparedPayload
from onec_runtime.execution.preparation import RoutePreparedStatement
from onec_runtime.runtime_contracts import OperationExecutionProvenance


class PreparedExecutionProvenanceReader:
    """Read exact prepared statement hashes before target admission.

    MAIN and CAPTURE policies own their payloads; this publication adapter
    interprets them outside the generic pipeline. A Worker-only cell has only
    a candidate projection at this stage, not the compiled artifact. It must
    fail before admission rather than claim a source hash for code that may
    change during Worker activation.
    """

    def __call__(self, prepared: PreparedCell) -> OperationExecutionProvenance:
        if not isinstance(prepared, PreparedCell):
            raise TypeError("provenance requires a prepared cell")
        payload = prepared.payload
        if isinstance(payload, MainPreparedPayload):
            mode = "main"
        elif isinstance(payload, CapturePreparedPayload):
            mode = "capture"
        else:
            raise ProtocolError("execution provenance has an unknown route payload")

        common: CommonCell = payload.common
        if (
            not isinstance(common, CommonCell)
            or common.source_hash != common.source_unit.source_sha256
        ):
            raise ProtocolError("visible source identity changed during preparation")
        statement = payload.deferred_statement or payload.statement
        if statement is None:
            if payload.worker_intent is not None:
                raise ProtocolError("Worker artifact is not built before admission")
            raise ProtocolError("execution provenance has no prepared statement")
        if not isinstance(statement, RoutePreparedStatement):
            raise TypeError("prepared statement provenance is invalid")
        executed = statement.lowering.mapped_source
        if not isinstance(executed, MappedSource):
            raise TypeError("prepared execution source map is invalid")
        artifact = executed.artifact
        return OperationExecutionProvenance(
            visible_source_sha256=common.source_hash,
            executed_source_sha256=artifact.source_sha256,
            source_map_sha256=executed.source_map_sha256,
            mode=mode,
            worker_generation=artifact.worker_generation,
            worker_manifest_sha256=artifact.worker_manifest_sha256,
        )
