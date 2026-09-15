import json

import pytest

import onec_runtime.privacy as privacy

from onec_runtime.fault_injection import (
    CloseTransportAt,
    FaultPoint,
    InjectedTransportFailure,
)


def test_close_transport_fault_fires_once_at_exact_boundary() -> None:
    closes: list[str] = []
    fault = CloseTransportAt(
        FaultPoint.AFTER_FIRST_ROOT_WRITE,
        lambda: closes.append("closed"),
    )

    fault(FaultPoint.AFTER_CAPTURE_CHECKPOINT)
    with pytest.raises(InjectedTransportFailure, match="after_first_root_write"):
        fault(FaultPoint.AFTER_FIRST_ROOT_WRITE)
    fault(FaultPoint.AFTER_FIRST_ROOT_WRITE)

    assert closes == ["closed"]


def test_capture_privacy_distinguishes_opaque_hash_from_raw_pid_leakage() -> None:
    """Break caught: decimal PID scanning rejects an opaque SHA-256 collision."""
    from integration.evidence.capture_live_evidence import (
        CaptureEvidenceError,
        assert_mcp_response_private_safe,
    )

    identity_sha256 = "b4af827076f077786b77be9101b81da254e79e38bd3ed29f5b253975fcf27a42"

    assert_mcp_response_private_safe(
        {"identity_sha256": identity_sha256},
        private_markers=(),
        owned_pids=(9101,),
    )
    for leaked_pid in (9101, "raw process PID 9101"):
        with pytest.raises(CaptureEvidenceError, match="private"):
            assert_mcp_response_private_safe(
                {"process": leaked_pid},
                private_markers=(),
                owned_pids=(9101,),
            )


@pytest.mark.parametrize(
    ("private_text", "secrets"),
    (
        (
            '{"rdbg_session":"json-session","pid":9182}',
            ("json-session", "9182"),
        ),
        (
            "process_id=44321 credentials: prose-credential",
            ("44321", "prose-credential"),
        ),
        (
            "rdbgSessionId=camel-session authorizationHeader: camel-auth",
            ("camel-session", "camel-auth"),
        ),
        (
            "rdbgTarget=suffix-target rdbgProcess suffix-process",
            ("suffix-target", "suffix-process"),
        ),
        (
            "dbPassword=pass-secret token=token-secret "
            "Authorization: Bearer bearer-secret",
            ("pass-secret", "token-secret", "bearer-secret"),
        ),
    ),
    ids=(
        "json-rdbg-pid",
        "prose-process-credentials",
        "camel-rdbg-authorization",
        "suffixless-rdbg",
        "password-token-bearer",
    ),
)
def test_acceptance_private_identity_mutations_remain_verbatim_or_rejected_without_reflection(
    private_text: str,
    secrets: tuple[str, ...],
) -> None:
    """Break caught: a private mutation crosses the public evidence wire."""
    from integration.evidence.capture_live_evidence import (
        CaptureEvidenceError,
        assert_mcp_response_private_safe,
    )

    bounded, truncated, redacted = privacy.bounded_platform_diagnostic(
        private_text,
        truncated=False,
        redacted=False,
    )

    assert bounded is not None
    assert len(bounded.encode("utf-8")) <= 64 * 1024
    assert truncated is False
    assert redacted is False
    encoded = json.dumps(
        {"platform_diagnostic": bounded},
        ensure_ascii=False,
    )
    assert all(secret in encoded for secret in secrets)

    with pytest.raises(CaptureEvidenceError) as rejected:
        assert_mcp_response_private_safe(
            {"failure": private_text},
            private_markers=secrets,
            owned_pids=(9182, 44321),
        )

    rejection = str(rejected.value)
    assert private_text not in rejection
    assert all(secret not in rejection for secret in secrets)
