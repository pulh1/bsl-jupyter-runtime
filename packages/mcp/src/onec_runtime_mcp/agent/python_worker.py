from __future__ import annotations

import builtins
from contextlib import redirect_stderr, redirect_stdout
import importlib.metadata
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import sys
import traceback
from types import MappingProxyType
from uuid import uuid4


class _ImportDenied(ImportError):
    pass


class _BoundedText:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self._data = bytearray()
        self.truncated = False

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            value = str(value)
        encoded = value.encode("utf-8", errors="replace")
        remaining = self.maximum - len(self._data)
        if remaining > 0:
            self._data.extend(encoded[:remaining])
        if len(encoded) > remaining:
            self.truncated = True
        return len(value)

    def flush(self) -> None:
        return None

    def text(self) -> str:
        return self._data.decode("utf-8", errors="ignore")


class _Worker:
    def __init__(self) -> None:
        self.objects: dict[str, object] = {}
        self.bindings: dict[str, str] = {}
        self.allowed_imports: frozenset[str] = frozenset()
        self.max_stdout = 4096
        self.max_stderr = 4096
        self.max_variables = 100
        self.private_log: Path | None = None
        self.transfer_root: Path | None = None

    def handle(self, request: dict[str, object]) -> dict[str, object]:
        method = request.get("method")
        if method == "initialize":
            return self._initialize(request)
        if method == "status":
            return {"ok": True, "state": "ready", "variable_count": len(self.bindings)}
        if method == "run":
            return self._run(request)
        if method == "inspect":
            return self._inspect(request)
        if method == "ingest":
            return self._ingest(request)
        if method == "release_object":
            return self._release_object(request)
        if method == "delete_bindings":
            return self._delete_bindings(request)
        if method == "imports":
            return self._imports()
        if method == "reset":
            self.objects.clear()
            self.bindings.clear()
            return {"ok": True}
        if method == "close":
            return {"ok": True, "closing": True}
        return self._failure("invalid_request")

    def _initialize(self, request: dict[str, object]) -> dict[str, object]:
        imports = request.get("allowed_imports")
        if not isinstance(imports, list) or any(not isinstance(item, str) for item in imports):
            return self._failure("invalid_request")
        self.allowed_imports = frozenset(imports)
        self.max_stdout = self._positive(request, "max_stdout_bytes")
        self.max_stderr = self._positive(request, "max_stderr_bytes")
        self.max_variables = self._positive(request, "max_variables")
        log_path = request.get("private_log")
        if not isinstance(log_path, str) or not log_path:
            return self._failure("invalid_request")
        self.private_log = Path(log_path)
        transfer_root = request.get("transfer_root")
        if not isinstance(transfer_root, str) or not transfer_root:
            return self._failure("invalid_request")
        self.transfer_root = Path(transfer_root).resolve()
        return {"ok": True, "pid": os.getpid(), "state": "ready"}

    def _run(self, request: dict[str, object]) -> dict[str, object]:
        code = request.get("code")
        inputs = request.get("inputs")
        outputs = request.get("outputs")
        if (
            not isinstance(code, str)
            or not isinstance(inputs, dict)
            or not isinstance(outputs, list)
            or any(not isinstance(item, str) for item in outputs)
        ):
            return self._failure("invalid_request")
        locals_: dict[str, object] = {}
        try:
            for name, source in inputs.items():
                if not isinstance(name, str) or not isinstance(source, dict):
                    raise ValueError("invalid input")
                if set(source) == {"literal"}:
                    locals_[name] = source["literal"]
                elif set(source) == {"object_handle"}:
                    handle = source["object_handle"]
                    if not isinstance(handle, str) or handle not in self.objects:
                        raise ValueError("unknown input object")
                    locals_[name] = self.objects[handle]
                else:
                    raise ValueError("invalid input source")
        except (TypeError, ValueError):
            return self._failure("invalid_request")
        stdout = _BoundedText(self.max_stdout)
        stderr = _BoundedText(self.max_stderr)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exec(code, self._globals(), locals_)
            if len(self.bindings | dict.fromkeys(outputs)) > self.max_variables:
                raise ValueError("variable limit")
            missing = [name for name in outputs if name not in locals_]
            if missing:
                raise ValueError("declared output missing")
            result: dict[str, object] = {}
            for name in outputs:
                handle = str(uuid4())
                value = locals_[name]
                previous = self.bindings.get(name)
                if previous is not None:
                    self.objects.pop(previous, None)
                self.objects[handle] = value
                self.bindings[name] = handle
                result[name] = {"object_handle": handle, **self._metadata(value)}
            return {
                "ok": True,
                "outputs": result,
                "stdout": stdout.text(),
                "stderr": stderr.text(),
                "stdout_truncated": stdout.truncated,
                "stderr_truncated": stderr.truncated,
            }
        except BaseException as error:
            self._log_failure(error)
            category = "denied" if isinstance(error, _ImportDenied) else "execution_error"
            return {
                **self._failure(category),
                "stdout": stdout.text(),
                "stderr": stderr.text(),
                "stdout_truncated": stdout.truncated,
                "stderr_truncated": stderr.truncated,
            }

    def _inspect(self, request: dict[str, object]) -> dict[str, object]:
        handle = request.get("object_handle")
        if not isinstance(handle, str) or handle not in self.objects:
            return self._failure("stale")
        return {"ok": True, **self._metadata(self.objects[handle])}

    def _ingest(self, request: dict[str, object]) -> dict[str, object]:
        path_value = request.get("path")
        kind = request.get("kind")
        byte_count = request.get("byte_count")
        payload_sha256 = request.get("payload_sha256")
        max_bytes = request.get("max_bytes")
        options = request.get("options")
        if (
            self.transfer_root is None
            or not isinstance(path_value, str)
            or kind not in {"value", "compact_table"}
            or type(byte_count) is not int
            or byte_count <= 0
            or type(max_bytes) is not int
            or max_bytes <= 0
            or byte_count > max_bytes
            or not isinstance(payload_sha256, str)
            or not isinstance(options, dict)
        ):
            return self._failure("invalid_request")
        path = Path(path_value)
        owned_path: Path | None = None
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self.transfer_root):
                return self._failure("denied")
            owned_path = resolved
            payload = resolved.read_bytes()
            if len(payload) != byte_count or sha256(payload).hexdigest() != payload_sha256:
                return self._failure("integrity")
            if kind == "value":
                from onec_runtime.value_materialization import (
                    MaterializationOptions,
                    decode_value_payload,
                )

                decode_options = MaterializationOptions(
                    refs=options.get("refs", "presentation"),
                    max_depth=options.get("max_depth"),
                    max_items=options.get("max_items"),
                    max_bytes=max_bytes,
                )
                value = decode_value_payload(payload, decode_options)
            else:
                from onec_runtime.compact_table import decode_compact_table_payload
                from onec_runtime.table_materialization import ReferencePolicy

                max_rows = options.get("max_rows")
                if type(max_rows) is not int or max_rows <= 0:
                    return self._failure("invalid_request")
                record_count = sum(1 for line in payload.splitlines() if line.strip())
                if record_count < 1 or record_count - 1 > max_rows:
                    return self._failure("limit")
                value = decode_compact_table_payload(
                    payload,
                    ReferencePolicy(
                        refs=options.get("refs", "presentation"),
                        ref_columns=options.get("ref_columns"),
                        uuid_suffix=options.get("uuid_suffix", "__uuid"),
                    ),
                )
                columns = options.get("columns")
                if columns is not None:
                    if not isinstance(columns, list) or any(
                        not isinstance(item, str) for item in columns
                    ):
                        return self._failure("invalid_request")
                    if any(item not in value.columns for item in columns):
                        return self._failure("invalid_request")
                    value = value.loc[:, columns]
            if len(self.objects) >= self.max_variables:
                return self._failure("limit")
            handle = str(uuid4())
            self.objects[handle] = value
            return {
                "ok": True,
                "object_handle": handle,
                **self._metadata(value),
            }
        except BaseException as error:
            self._log_failure(error)
            return self._failure("decode_error")
        finally:
            if owned_path is not None:
                try:
                    owned_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _release_object(self, request: dict[str, object]) -> dict[str, object]:
        handle = request.get("object_handle")
        if not isinstance(handle, str):
            return self._failure("invalid_request")
        return {"ok": True, "released": self.objects.pop(handle, None) is not None}

    def _delete_bindings(self, request: dict[str, object]) -> dict[str, object]:
        names = request.get("names")
        if (
            not isinstance(names, list)
            or not names
            or any(not isinstance(name, str) or name not in self.bindings for name in names)
            or len(set(names)) != len(names)
        ):
            return self._failure("invalid_request")
        for name in names:
            self.objects.pop(self.bindings.pop(name), None)
        return {"ok": True, "deleted": names}

    def _imports(self) -> dict[str, object]:
        values: list[dict[str, str]] = []
        standard = getattr(sys, "stdlib_module_names", frozenset())
        for name in sorted(self.allowed_imports):
            version = "stdlib" if name in standard else "available"
            if version == "available":
                try:
                    version = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    version = "unavailable"
            values.append({"name": name, "version": version})
        return {"ok": True, "imports": values}

    def _globals(self) -> dict[str, object]:
        safe = dict(vars(builtins))
        original_import = builtins.__import__
        allowed = self.allowed_imports | {"sys", "builtins", "__future__"}

        def guarded_import(
            name: str,
            globals_: object = None,
            locals_: object = None,
            fromlist: object = (),
            level: int = 0,
        ) -> object:
            root = name.split(".", 1)[0]
            if root not in allowed:
                raise _ImportDenied("import is not allowlisted")
            return original_import(name, globals_, locals_, fromlist, level)

        safe["__import__"] = guarded_import
        return {"__builtins__": MappingProxyType(safe), "__name__": "__onec_workspace__"}

    @staticmethod
    def _metadata(value: object) -> dict[str, object]:
        type_ = type(value)
        type_name = f"{type_.__module__}.{type_.__qualname__}"[:256]
        is_pandas = type_.__module__ == "pandas" or type_.__module__.startswith(
            "pandas."
        )
        if is_pandas and type_.__name__ in {
            "DataFrame",
            "Series",
        }:
            type_name = f"pandas.{type_.__name__}"
        elif type_.__module__.startswith("numpy") and type_.__name__ == "ndarray":
            type_name = "numpy.ndarray"
        preview: None | bool | int | float | str = None
        if value is None or type(value) in {bool, int}:
            preview = value
        elif type(value) is float and isfinite(value):
            preview = value
        elif isinstance(value, str):
            preview = value[:4096]
        shape: list[int] | None = None
        dtype: str | None = None
        memory_bytes: int | None = None
        if is_pandas:
            raw_shape = getattr(value, "shape", None)
            if isinstance(raw_shape, tuple) and all(type(item) is int for item in raw_shape):
                shape = list(raw_shape)
            raw_dtype = getattr(value, "dtype", None)
            if raw_dtype is not None:
                dtype = str(raw_dtype)[:256]
            try:
                usage = value.memory_usage(deep=True)  # type: ignore[attr-defined]
                memory_bytes = int(usage.sum() if hasattr(usage, "sum") else usage)
            except BaseException:
                memory_bytes = None
        return {
            "type_name": type_name,
            "preview": preview,
            "shape": shape,
            "dtype": dtype,
            "memory_bytes": memory_bytes,
        }

    def _log_failure(self, error: BaseException) -> None:
        if self.private_log is None:
            return
        try:
            self.private_log.parent.mkdir(parents=True, exist_ok=True)
            with self.private_log.open("a", encoding="utf-8", newline="\n") as handle:
                traceback.print_exception(error, file=handle)
        except OSError:
            return

    @staticmethod
    def _positive(request: dict[str, object], name: str) -> int:
        value = request.get(name)
        if type(value) is not int or value <= 0:
            raise ValueError(name)
        return value

    @staticmethod
    def _failure(category: str) -> dict[str, object]:
        return {"ok": False, "error_category": category, "diagnostic_id": f"diag_{uuid4().hex}"}


def main() -> int:
    maximum = int(os.environ.get("ONEC_PYTHON_MAX_REQUEST_BYTES", str(256 * 1024)))
    maximum_response = int(os.environ.get("ONEC_PYTHON_MAX_RESPONSE_BYTES", str(256 * 1024)))
    worker = _Worker()
    while True:
        request: object = {}
        line = sys.stdin.buffer.readline(maximum + 2)
        if not line:
            return 0
        if len(line) > maximum or not line.endswith(b"\n"):
            response = worker._failure("limit")
        else:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise TypeError
                response = worker.handle(request)
            except BaseException:
                response = worker._failure("invalid_request")
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > maximum_response:
            encoded = json.dumps(worker._failure("limit"), separators=(",", ":")).encode()
        sys.stdout.buffer.write(encoded + b"\n")
        sys.stdout.buffer.flush()
        if isinstance(request, dict) and request.get("method") == "close":
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
