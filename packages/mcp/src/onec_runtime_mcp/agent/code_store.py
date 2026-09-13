"""Revisioned, conflict-fenced notebook source storage for the agent service."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from difflib import unified_diff
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import RLock

import nbformat

from onec_runtime_mcp.agent.contracts import (
    CodeDescriptor,
    CodeLanguage,
    CodeMode,
    CodeRevision,
)


MAX_DIFF_LINES = 200
MAX_DIFF_CHARACTERS = 16_384


class CodeStoreError(RuntimeError):
    """Base error for saved notebook code operations."""


class CodePathError(CodeStoreError):
    """A notebook path is outside the configured project or unsafe."""


class CodeNotFound(CodeStoreError):
    """The requested notebook cell or immutable revision does not exist."""


class CodeConflict(CodeStoreError):
    """A caller's revision or document fence no longer matches the notebook."""


class CodePlatformUnavailable(CodeStoreError):
    """The host cannot provide the mandatory mutation commit guard."""


@dataclass(frozen=True, slots=True)
class CodePutRequest:
    cell_id: str
    source: str
    language: CodeLanguage
    mode: CodeMode
    expected_revision: int
    expected_document_sha256: str
    outputs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodeDeleteRequest:
    cell_id: str
    expected_revision: int
    expected_document_sha256: str


@dataclass(slots=True)
class _StagedDocument:
    path: Path
    temporary: Path | None
    document_sha256: str


class NotebookCodeStore:
    """Persist notebook cells with immutable source revisions and optimistic fences.

    ``list`` selects the notebook for the remaining cell-scoped methods.  This
    mirrors the service's container-first API without making every operation
    repeat a notebook path.
    """

    def __init__(self, project_root: Path, state_root: Path) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._state_root = Path(state_root)
        if not self._state_root.is_absolute():
            self._state_root = self._project_root / self._state_root
        self._state_root = self._state_root.resolve(strict=False)
        self._notebook: Path | None = None
        self._notebook_identity: str | None = None
        self._lock = RLock()

    @property
    def guarded_mutations_available(self) -> bool:
        """Whether this host can enforce the repository mutation fence."""
        return self._host_supports_guarded_mutations()

    def list(self, notebook: str | Path) -> tuple[CodeDescriptor, ...]:
        """Select a project notebook and return its saved code units."""
        with self._lock:
            path = self._resolve_notebook(notebook)
            document, _, document_sha = self._read_document(path)
            self._notebook = path
            self._notebook_identity = self._notebook_sha256(path)
            revisions = self._current_revisions(document, document_sha)
            if self.guarded_mutations_available:
                for revision in revisions:
                    self._prepare_history(revision)
            return tuple(
                CodeDescriptor(
                    cell_id=revision.cell_id,
                    revision=revision.revision,
                    language=revision.language,
                    mode=revision.mode,
                    source_sha256=revision.source_sha256,
                    outputs=revision.outputs,
                )
                for revision in revisions
            )

    def get(self, cell_id: str, revision: int | None = None) -> CodeRevision:
        """Read the active cell or one immutable saved revision."""
        with self._lock:
            self._validate_cell_id(cell_id)
            if revision is not None:
                return self._find_history(cell_id, revision)
            document, _, document_sha = self._active_document()
            cell = self._find_cell(document, cell_id)
            return self._revision_from_cell(cell, document_sha)

    def put(
        self,
        *,
        cell_id: str,
        source: str,
        language: CodeLanguage,
        mode: CodeMode,
        expected_revision: int,
        expected_document_sha256: str,
        outputs: tuple[str, ...] = (),
    ) -> CodeRevision:
        """Save a new immutable revision after both caller fences match."""
        return self._commit_revision(
            CodePutRequest(
                cell_id=cell_id,
                source=source,
                language=language,
                mode=mode,
                expected_revision=expected_revision,
                expected_document_sha256=expected_document_sha256,
                outputs=outputs,
            )
        )

    def diff(self, cell_id: str, left: int, right: int) -> str:
        """Return a bounded unified diff between two immutable revisions."""
        with self._lock:
            left_revision = self.get(cell_id, left)
            right_revision = self.get(cell_id, right)
            return self._bounded_diff(cell_id, left, right, left_revision.source, right_revision.source)

    def history(self, cell_id: str) -> tuple[CodeRevision, ...]:
        """Return every immutable source revision for an exact cell id."""
        with self._lock:
            self._validate_cell_id(cell_id)
            revisions = self._visible_history_for_cell(cell_id)
            if not revisions:
                raise CodeNotFound(f"cell not found: {cell_id}")
            return tuple(sorted(revisions, key=lambda item: item.revision))

    def promote(self, request: CodePutRequest) -> CodeRevision:
        """Persist an inline source verbatim using the ordinary conflict fence."""
        if not isinstance(request, CodePutRequest):
            raise TypeError("request must be a CodePutRequest")
        return self._commit_revision(request)

    def delete(self, request: CodeDeleteRequest) -> None:
        """Remove an active cell while leaving its immutable history untouched."""
        if not isinstance(request, CodeDeleteRequest):
            raise TypeError("request must be a CodeDeleteRequest")
        with self._lock:
            self._require_guarded_mutations()
            self._validate_request_fence(request)
            document, path, document_sha = self._active_document()
            self._check_document_fence(document_sha, request.expected_document_sha256)
            cell = self._find_cell(document, request.cell_id)
            current = self._revision_from_cell(cell, document_sha)
            self._check_revision_fence(current.revision, request.expected_revision)
            document.cells.remove(cell)
            staged = self._stage_document(path, document)
            try:
                self._commit_staged_document(staged, request.expected_document_sha256)
            finally:
                self._discard_staged_document(staged)

    def _commit_revision(self, request: CodePutRequest) -> CodeRevision:
        with self._lock:
            self._require_guarded_mutations()
            self._validate_request_fence(request)
            if not isinstance(request.source, str):
                raise TypeError("source must be a string")
            if not isinstance(request.language, CodeLanguage):
                raise TypeError("language must be a CodeLanguage")
            if not isinstance(request.mode, CodeMode):
                raise TypeError("mode must be a CodeMode")
            document, path, document_sha = self._active_document()
            self._check_document_fence(document_sha, request.expected_document_sha256)
            cell = self._find_cell(document, request.cell_id)
            current = self._revision_from_cell(cell, document_sha)
            self._check_revision_fence(current.revision, request.expected_revision)

            cell.source = request.source
            cell.metadata["onec_runtime"] = {
                "revision": current.revision + 1,
                "language": request.language.value,
                "mode": request.mode.value,
                "source_sha256": self._source_sha256(request.source),
                "outputs": list(request.outputs),
            }
            staged = self._stage_document(path, document)
            saved = self._revision_from_cell(cell, staged.document_sha256)
            try:
                self._prepare_history(saved)
                self._commit_staged_document(staged, request.expected_document_sha256)
                return saved
            finally:
                self._discard_staged_document(staged)

    def _active_document(self) -> tuple[nbformat.NotebookNode, Path, str]:
        if self._notebook is None:
            raise CodeStoreError("select a notebook with list() first")
        document, _, document_sha = self._read_document(self._notebook)
        return document, self._notebook, document_sha

    @staticmethod
    def _host_supports_guarded_mutations() -> bool:
        return os.name == "nt"

    def _require_guarded_mutations(self) -> None:
        if not self.guarded_mutations_available:
            raise CodePlatformUnavailable("guarded mutations are unavailable on this host")

    def _resolve_notebook(self, notebook: str | Path) -> Path:
        candidate = Path(notebook)
        if not candidate.is_absolute():
            candidate = self._project_root / candidate
        candidate = candidate.absolute()
        if candidate.suffix != ".ipynb":
            raise CodePathError("notebook must be an .ipynb file")
        try:
            candidate.relative_to(self._project_root)
        except ValueError as error:
            raise CodePathError("notebook must be below project root") from error
        if not candidate.exists():
            raise CodePathError("notebook does not exist")
        for part in (candidate, *candidate.parents):
            if part == self._project_root.parent:
                break
            if part.is_symlink():
                raise CodePathError("symlinked notebook paths are not allowed")
            if part == self._project_root:
                break
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(self._project_root)
        except ValueError as error:
            raise CodePathError("notebook must resolve below project root") from error
        return resolved

    @staticmethod
    def _read_document(path: Path) -> tuple[nbformat.NotebookNode, bytes, str]:
        raw = path.read_bytes()
        try:
            raw_document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("notebook must contain valid UTF-8 JSON") from error
        cells = raw_document.get("cells") if isinstance(raw_document, dict) else None
        if not isinstance(cells, list):
            raise ValueError("notebook cells must be a list")
        ids: set[str] = set()
        for raw_cell in cells:
            cell_id = raw_cell.get("id") if isinstance(raw_cell, dict) else None
            if not isinstance(cell_id, str) or not cell_id:
                raise ValueError("cell id must be a non-empty string")
            if cell_id in ids:
                raise ValueError(f"duplicate cell id: {cell_id}")
            ids.add(cell_id)
        document = nbformat.reads(raw.decode("utf-8"), as_version=4)
        return document, raw, hashlib.sha256(raw).hexdigest()

    def _stage_document(self, path: Path, document: nbformat.NotebookNode) -> _StagedDocument:
        payload = nbformat.writes(document, version=4).encode("utf-8")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            return _StagedDocument(
                path=path,
                temporary=temporary,
                document_sha256=hashlib.sha256(payload).hexdigest(),
            )
        except BaseException:
            if temporary is not None and temporary.exists():
                temporary.unlink()
            raise

    def _commit_staged_document(
        self, staged: _StagedDocument, expected_document_sha256: str
    ) -> None:
        self._before_document_commit(staged.path)
        with self._commit_guard(staged.path):
            current_sha = hashlib.sha256(staged.path.read_bytes()).hexdigest()
            self._check_document_fence(current_sha, expected_document_sha256)
            self._after_document_verification(staged.path)
            assert staged.temporary is not None
            self._replace_document(staged)
            staged.temporary = None

    @staticmethod
    def _discard_staged_document(staged: _StagedDocument) -> None:
        if staged.temporary is not None and staged.temporary.exists():
            staged.temporary.unlink()

    @staticmethod
    def _before_document_commit(path: Path) -> None:
        """Host-specific locking hooks may strengthen this conditional update."""

    @staticmethod
    def _after_document_verification(path: Path) -> None:
        """Test seam immediately before the guarded replacement."""

    @staticmethod
    def _replace_document(staged: _StagedDocument) -> None:
        assert staged.temporary is not None
        if os.name != "nt":
            raise CodePlatformUnavailable("guarded mutations are unavailable on this host")
        replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
        replace_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
        )
        replace_file.restype = wintypes.BOOL
        if not replace_file(str(staged.path), str(staged.temporary), None, 0, None, None):
            raise OSError(
                ctypes.get_last_error(),
                "atomic notebook replacement failed",
                str(staged.path),
            )

    @contextmanager
    def _commit_guard(self, path: Path):
        """Hold the final verification and replacement in one platform guard.

        Windows opens the destination with write sharing denied while allowing
        delete sharing, so another writer cannot open it while ``ReplaceFileW``
        atomically replaces it. This guard is mandatory for mutations; hosts
        without the Windows primitive fail closed with
        ``CodePlatformUnavailable``.
        """
        if os.name != "nt":
            raise CodePlatformUnavailable("guarded mutations are unavailable on this host")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_file(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001 | 0x00000004,  # FILE_SHARE_READ | FILE_SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise CodeConflict("notebook commit guard unavailable")
        try:
            yield
        finally:
            close_handle(handle)

    def _current_revisions(
        self, document: nbformat.NotebookNode, document_sha: str
    ) -> tuple[CodeRevision, ...]:
        return tuple(self._revision_from_cell(cell, document_sha) for cell in document.cells)

    def _revision_from_cell(self, cell: nbformat.NotebookNode, document_sha: str) -> CodeRevision:
        metadata = cell.metadata.get("onec_runtime")
        source = self._cell_source(cell)
        if metadata is None:
            revision = 1
            language = self._default_language(cell)
            mode = CodeMode.MAIN
            outputs: tuple[str, ...] = ()
        elif not isinstance(metadata, dict):
            raise ValueError("cell metadata.onec_runtime must be an object")
        else:
            try:
                revision = metadata["revision"]
                language = CodeLanguage(metadata["language"])
                mode = CodeMode(metadata["mode"])
                raw_outputs = metadata.get("outputs", [])
            except (KeyError, ValueError) as error:
                raise ValueError("invalid metadata.onec_runtime") from error
            if (
                isinstance(raw_outputs, str)
                or not isinstance(raw_outputs, (tuple, list))
                or any(not isinstance(item, str) or not item for item in raw_outputs)
            ):
                raise ValueError("invalid metadata.onec_runtime.outputs")
            outputs = tuple(raw_outputs)
        if type(revision) is not int or revision <= 0:
            raise ValueError("metadata.onec_runtime.revision must be positive")
        return CodeRevision(
            cell_id=cell.id,
            revision=revision,
            source=source,
            source_sha256=self._source_sha256(source),
            document_sha256=document_sha,
            language=language,
            mode=mode,
            outputs=outputs,
        )

    @staticmethod
    def _cell_source(cell: nbformat.NotebookNode) -> str:
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        if not isinstance(source, str):
            raise ValueError("cell source must be a string")
        return source

    @staticmethod
    def _default_language(cell: nbformat.NotebookNode) -> CodeLanguage:
        return CodeLanguage.MARKDOWN if cell.cell_type == "markdown" else CodeLanguage.BSL

    def _find_cell(self, document: nbformat.NotebookNode, cell_id: str) -> nbformat.NotebookNode:
        self._validate_cell_id(cell_id)
        matches = [cell for cell in document.cells if cell.get("id") == cell_id]
        if not matches:
            raise CodeNotFound(f"cell not found: {cell_id}")
        if len(matches) > 1:
            raise ValueError(f"duplicate cell id: {cell_id}")
        return matches[0]

    @staticmethod
    def _source_sha256(source: str) -> str:
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _bounded_diff(
        self, cell_id: str, left: int, right: int, left_source: str, right_source: str
    ) -> str:
        lines: list[str] = []
        characters = 0
        truncated = False
        for line in unified_diff(
            left_source.splitlines(),
            right_source.splitlines(),
            fromfile=f"{cell_id}@{left}",
            tofile=f"{cell_id}@{right}",
            lineterm="",
        ):
            separator = 1 if lines else 0
            remaining = MAX_DIFF_CHARACTERS - characters - separator
            if len(lines) >= MAX_DIFF_LINES or remaining < len(line):
                truncated = True
                break
            lines.append(line)
            characters += separator + len(line)
        if truncated:
            marker = "... diff truncated ..."
            while lines and characters + 1 + len(marker) > MAX_DIFF_CHARACTERS:
                removed = lines.pop()
                characters -= len(removed) + (1 if lines else 0)
            if not lines and len(marker) > MAX_DIFF_CHARACTERS:
                return marker[:MAX_DIFF_CHARACTERS]
            lines.append(marker)
        return "\n".join(lines)

    def _history_root(self) -> Path:
        if self._notebook_identity is None:
            raise CodeStoreError("select a notebook with list() first")
        return self._state_root / "code" / self._notebook_identity

    def _notebook_sha256(self, notebook: Path) -> str:
        relative = notebook.relative_to(self._project_root).as_posix()
        return hashlib.sha256(relative.encode("utf-8")).hexdigest()

    def _prepare_history(self, revision: CodeRevision) -> None:
        existing = [
            item
            for item in self._history_for_cell(revision.cell_id)
            if item.revision == revision.revision
        ]
        if existing:
            if len(existing) == 1 and existing[0] == revision:
                return
            raise CodeConflict("immutable revision differs from existing history")
        path = self._history_path(revision)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cell_id": revision.cell_id,
            "revision": revision.revision,
            "source": revision.source,
            "source_sha256": revision.source_sha256,
            "document_sha256": revision.document_sha256,
            "language": revision.language.value,
            "mode": revision.mode.value,
            "outputs": list(revision.outputs),
        }
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _history_path(self, revision: CodeRevision) -> Path:
        root = self._history_root()
        directory = root / revision.cell_id
        if directory.exists() and any(
            self._history_cell_id(item) not in {None, revision.cell_id}
            for item in directory.glob("*.json")
        ):
            directory = root / f"{revision.cell_id}--{self._source_sha256(revision.cell_id)[:12]}"
        return directory / f"{revision.revision}.json"

    @staticmethod
    def _history_cell_id(path: Path) -> str | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload.get("cell_id") if isinstance(payload.get("cell_id"), str) else None

    def _history_for_cell(self, cell_id: str) -> list[CodeRevision]:
        root = self._history_root()
        if not root.exists():
            return []
        revisions: list[CodeRevision] = []
        for path in root.glob("*/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload["cell_id"] != cell_id:
                    continue
                revisions.append(
                    CodeRevision(
                        cell_id=payload["cell_id"],
                        revision=payload["revision"],
                        source=payload["source"],
                        source_sha256=payload["source_sha256"],
                        document_sha256=payload["document_sha256"],
                        language=CodeLanguage(payload["language"]),
                        mode=CodeMode(payload["mode"]),
                        outputs=tuple(payload.get("outputs", ())),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return revisions

    def _visible_history_for_cell(self, cell_id: str) -> list[CodeRevision]:
        revisions = self._history_for_cell(cell_id)
        document, _, document_sha = self._active_document()
        try:
            current = self._revision_from_cell(self._find_cell(document, cell_id), document_sha)
        except CodeNotFound:
            return revisions
        return [item for item in revisions if item.revision <= current.revision]

    def _find_history(self, cell_id: str, revision: int) -> CodeRevision:
        if type(revision) is not int or revision <= 0:
            raise ValueError("revision must be positive")
        matches = [
            item for item in self._visible_history_for_cell(cell_id) if item.revision == revision
        ]
        if not matches:
            raise CodeNotFound(f"revision not found: {cell_id}@{revision}")
        if len(matches) > 1:
            raise CodeStoreError(f"ambiguous immutable revision: {cell_id}@{revision}")
        return matches[0]

    @staticmethod
    def _validate_cell_id(cell_id: object) -> None:
        if not isinstance(cell_id, str) or not cell_id or cell_id in {".", ".."}:
            raise ValueError("cell id must be a non-empty string")
        if "/" in cell_id or "\\" in cell_id:
            raise ValueError("cell id must not contain path separators")

    @staticmethod
    def _validate_request_fence(request: CodePutRequest | CodeDeleteRequest) -> None:
        if type(request.expected_revision) is not int or request.expected_revision <= 0:
            raise ValueError("expected_revision must be positive")
        if not isinstance(request.expected_document_sha256, str) or not request.expected_document_sha256:
            raise ValueError("expected_document_sha256 must be a non-empty string")
        NotebookCodeStore._validate_cell_id(request.cell_id)

    @staticmethod
    def _check_document_fence(actual: str, expected: str) -> None:
        if actual != expected:
            raise CodeConflict("notebook document changed since it was read")

    @staticmethod
    def _check_revision_fence(actual: int, expected: int) -> None:
        if actual != expected:
            raise CodeConflict("cell revision changed since it was read")
