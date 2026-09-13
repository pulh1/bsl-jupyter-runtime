from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
import re
from types import MappingProxyType

from onec_runtime_mcp.agent.proxies import ProxyDescriptor


_IMPORT_NAME = re.compile(r"[A-Za-z_]\w*")


def _positive_int(value: int, *, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class PythonWorkspaceLimits:
    timeout_seconds: float
    max_code_bytes: int
    max_request_bytes: int
    max_response_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_variables: int
    allowed_imports: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.timeout_seconds) not in {int, float}
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 300
        ):
            raise ValueError("timeout_seconds must be finite and between 0 and 300")
        for name in (
            "max_code_bytes",
            "max_request_bytes",
            "max_response_bytes",
            "max_stdout_bytes",
            "max_stderr_bytes",
            "max_variables",
        ):
            _positive_int(getattr(self, name), name=name)
        imports = tuple(self.allowed_imports)
        if (
            len(imports) > 100
            or len(set(imports)) != len(imports)
            or any(not isinstance(item, str) or not _IMPORT_NAME.fullmatch(item) for item in imports)
        ):
            raise ValueError("allowed_imports must be unique top-level module names")
        object.__setattr__(self, "allowed_imports", imports)


@dataclass(frozen=True, slots=True)
class PythonWorkspaceStatus:
    generation: int
    pid: int
    state: str
    variable_count: int
    hard_memory_limit: bool = False

    def __post_init__(self) -> None:
        _positive_int(self.generation, name="generation")
        _positive_int(self.pid, name="pid")
        if self.state not in {"ready", "busy", "lost", "closed"}:
            raise ValueError("invalid Python workspace state")
        if type(self.variable_count) is not int or self.variable_count < 0:
            raise ValueError("variable_count must be non-negative")
        if type(self.hard_memory_limit) is not bool:
            raise TypeError("hard_memory_limit must be a bool")


@dataclass(frozen=True, slots=True)
class PythonRunResult:
    succeeded: bool
    outputs: Mapping[str, ProxyDescriptor]
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error_category: str = ""
    diagnostic_id: str = ""

    def __post_init__(self) -> None:
        if type(self.succeeded) is not bool:
            raise TypeError("succeeded must be a bool")
        if not isinstance(self.outputs, Mapping):
            raise TypeError("outputs must be a mapping")
        outputs = dict(self.outputs)
        if any(
            not isinstance(name, str) or not isinstance(value, ProxyDescriptor)
            for name, value in outputs.items()
        ):
            raise TypeError("outputs must contain proxy descriptors")
        object.__setattr__(self, "outputs", MappingProxyType(outputs))
        for name in ("stdout", "stderr", "error_category", "diagnostic_id"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        if self.succeeded and (self.error_category or self.diagnostic_id):
            raise ValueError("successful run cannot contain failure metadata")
        if not self.succeeded and (not self.error_category or not self.diagnostic_id):
            raise ValueError("failed run requires normalized failure metadata")


@dataclass(frozen=True, slots=True)
class PythonInspection:
    proxy_id: str
    type_name: str
    preview: None | bool | int | float | str
    shape: tuple[int, ...] | None = None
    dtype: str | None = None
    memory_bytes: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.proxy_id, str) or not self.proxy_id:
            raise ValueError("proxy_id must be non-empty")
        if not isinstance(self.type_name, str) or not self.type_name:
            raise ValueError("type_name must be non-empty")
        if type(self.preview) is float and not isfinite(self.preview):
            raise ValueError("preview must be finite")
        if self.shape is not None:
            shape = tuple(self.shape)
            if any(type(item) is not int or item < 0 for item in shape):
                raise ValueError("shape must be non-negative integers")
            object.__setattr__(self, "shape", shape)
        if self.dtype is not None and not isinstance(self.dtype, str):
            raise TypeError("dtype must be a string")
        if self.memory_bytes is not None and (
            type(self.memory_bytes) is not int or self.memory_bytes < 0
        ):
            raise ValueError("memory_bytes must be non-negative")


@dataclass(frozen=True, slots=True)
class PythonImportDescriptor:
    name: str
    version: str

    def __post_init__(self) -> None:
        if not _IMPORT_NAME.fullmatch(self.name):
            raise ValueError("import name is invalid")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("import version must be non-empty")
