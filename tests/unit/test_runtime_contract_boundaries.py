from __future__ import annotations

from hashlib import sha256
import subprocess
import sys

from onec_runtime.bsl import DiagnosticStage, MappingConfidence, NormalizedDiagnostic
from onec_runtime.bsl.diagnostics import _PrivatePlatformEvidence
from onec_runtime.runtime_contracts import sanitize_normalized_diagnostic


def test_sanitizer_uses_utf8_byte_bound_for_private_platform_evidence() -> None:
    accepted_text = "😀" * (64 * 1024 // len("😀".encode("utf-8")))
    rejected_text = accepted_text + "😀"

    def diagnostic(text: str) -> NormalizedDiagnostic:
        return NormalizedDiagnostic(
            "a" * 64,
            "untrusted summary",
            DiagnosticStage.EXECUTION,
            MappingConfidence.UNKNOWN,
            _platform_evidence=_PrivatePlatformEvidence(text),
            platform_diagnostic_sha256=sha256(text.encode("utf-8")).hexdigest(),
        )

    assert sanitize_normalized_diagnostic(diagnostic(accepted_text)) is not None
    assert sanitize_normalized_diagnostic(diagnostic(rejected_text)) is None


def test_core_runtime_imports_without_agent_package() -> None:
    script = """
import builtins

real_import = builtins.__import__

def guarded(name, *args, **kwargs):
    if name == "onec_runtime_mcp.agent" or name.startswith("onec_runtime_mcp.agent."):
        raise AssertionError(name)
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded

import onec_runtime.runtime_api
import onec_runtime.privacy
import onec_runtime.session
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_runtime_session_imports_without_ipython() -> None:
    script = """
import builtins

real_import = builtins.__import__

def guarded(name, *args, **kwargs):
    if name == "IPython" or name.startswith("IPython."):
        raise AssertionError(name)
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded

import onec_runtime.session
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
