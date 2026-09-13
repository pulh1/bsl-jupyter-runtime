from __future__ import annotations

import subprocess
import sys


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
