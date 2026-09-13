from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import nbformat
import pytest

from onec_runtime_mcp.agent.code_store import (
    CodeConflict,
    CodeDeleteRequest,
    CodeNotFound,
    CodePathError,
    CodePlatformUnavailable,
    CodePutRequest,
    NotebookCodeStore,
)
from onec_runtime_mcp.agent.contracts import CodeLanguage, CodeMode


def seeded_store(
    tmp_path: Path,
    *,
    source: str,
    legacy: bool = False,
    extra_cell_ids: tuple[str, ...] = (),
) -> tuple[NotebookCodeStore, Path]:
    project = tmp_path / "project"
    notebooks = project / "notebooks"
    notebooks.mkdir(parents=True)
    notebook = notebooks / "demo.ipynb"
    main = nbformat.v4.new_code_cell(source=source, id="cell-main")
    main.outputs = [nbformat.v4.new_output("stream", name="stdout", text="kept\n")]
    if not legacy:
        main.metadata["onec_runtime"] = {
            "revision": 1,
            "language": "bsl",
            "mode": "main",
            "source_sha256": sha256(source),
        }
    cells = [main, nbformat.v4.new_markdown_cell("# untouched", id="notes")]
    for cell_id in extra_cell_ids:
        cells.append(nbformat.v4.new_code_cell(source=f"{cell_id} = 1", id=cell_id))
    nbformat.write(nbformat.v4.new_notebook(cells=cells), notebook)
    store = NotebookCodeStore(project, project / ".runtime" / "agent-service")
    store.list("notebooks/demo.ipynb")
    return store, notebook


def sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def edit_notebook_source(notebook: Path, cell_id: str, source: str) -> None:
    document = nbformat.read(notebook, as_version=4)
    next(cell for cell in document.cells if cell.id == cell_id).source = source
    nbformat.write(document, notebook)


def test_put_updates_only_one_cell_and_records_immutable_revision(tmp_path: Path) -> None:
    store, notebook = seeded_store(tmp_path, source="Ответ = 1;")
    before = store.get("cell-main")
    saved = store.put(
        cell_id="cell-main",
        source="Ответ = 2;",
        language=CodeLanguage.BSL,
        mode=CodeMode.MAIN,
        expected_revision=before.revision,
        expected_document_sha256=before.document_sha256,
    )

    document = nbformat.read(notebook, as_version=4)
    assert saved.revision == before.revision + 1
    assert store.get("cell-main", revision=before.revision).source == "Ответ = 1;"
    assert store.get("cell-main").source == "Ответ = 2;"
    assert document.cells[0].outputs[0].text == "kept\n"
    assert document.cells[1].source == "# untouched"


def test_external_editor_change_conflicts_without_overwrite(tmp_path: Path) -> None:
    store, notebook = seeded_store(tmp_path, source="Ответ = 1;")
    fence = store.get("cell-main")
    edit_notebook_source(notebook, "cell-main", "Ответ = 99;")

    with pytest.raises(CodeConflict):
        store.put(
            cell_id="cell-main",
            source="Ответ = 2;",
            language=CodeLanguage.BSL,
            mode=CodeMode.MAIN,
            expected_revision=fence.revision,
            expected_document_sha256=fence.document_sha256,
        )

    assert "Ответ = 99;" in notebook.read_text(encoding="utf-8")


def test_list_rejects_paths_outside_project_and_symlinked_notebooks(tmp_path: Path) -> None:
    store, _ = seeded_store(tmp_path, source="Ответ = 1;")
    outside = tmp_path / "outside.ipynb"
    nbformat.write(nbformat.v4.new_notebook(), outside)

    with pytest.raises(CodePathError):
        store.list("../outside.ipynb")

    link = tmp_path / "project" / "notebooks" / "linked.ipynb"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    with pytest.raises(CodePathError, match="symlink"):
        store.list(link)


def test_list_rejects_duplicate_and_get_rejects_missing_cell_ids(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    notebook = project / "duplicate.ipynb"
    duplicate = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_code_cell("a = 1", id="same"),
            nbformat.v4.new_code_cell("a = 2", id="different"),
        ]
    )
    raw_duplicate = json.loads(nbformat.writes(duplicate))
    raw_duplicate["cells"][1]["id"] = "same"
    notebook.write_text(json.dumps(raw_duplicate), encoding="utf-8")
    store = NotebookCodeStore(project, project / ".runtime" / "agent-service")

    with pytest.raises(ValueError, match="duplicate cell id"):
        store.list("duplicate.ipynb")

    valid, _ = seeded_store(tmp_path / "valid", source="Ответ = 1;")
    with pytest.raises(CodeNotFound):
        valid.get("missing")


def test_list_derives_legacy_initial_revision_without_writing_notebook_bytes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    notebooks = project / "notebooks"
    notebooks.mkdir(parents=True)
    notebook = notebooks / "legacy.ipynb"
    cell = nbformat.v4.new_code_cell(source="Ответ = 1;", id="cell-main")
    cell.outputs = [nbformat.v4.new_output("stream", name="stdout", text="kept\n")]
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), notebook)
    original = notebook.read_bytes()
    store = NotebookCodeStore(project, project / ".runtime" / "agent-service")

    listed = store.list("notebooks/legacy.ipynb")
    saved = store.get("cell-main")
    document = nbformat.read(notebook, as_version=4)
    cell = document.cells[0]
    assert saved.revision == 1
    assert listed[0].revision == 1
    assert notebook.read_bytes() == original
    assert cell.source == "Ответ = 1;"
    assert cell.outputs[0].text == "kept\n"
    assert "onec_runtime" not in cell.metadata


def test_failed_atomic_replace_preserves_the_original_notebook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, notebook = seeded_store(tmp_path, source="Ответ = 1;")
    before = store.get("cell-main")
    original = notebook.read_bytes()

    def fail_notebook_replace(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_replace_document", fail_notebook_replace)
    with pytest.raises(OSError, match="disk full"):
        store.put(
            cell_id="cell-main",
            source="Ответ = 2;",
            language=CodeLanguage.BSL,
            mode=CodeMode.MAIN,
            expected_revision=before.revision,
            expected_document_sha256=before.document_sha256,
        )
    assert notebook.read_bytes() == original
    assert [item.revision for item in store.history("cell-main")] == [1]


def test_diff_is_bounded(tmp_path: Path) -> None:
    store, _ = seeded_store(tmp_path, source="\n".join(f"old-{i}" for i in range(250)))
    before = store.get("cell-main")
    store.put(
        cell_id="cell-main",
        source="\n".join(f"new-{i}" for i in range(250)),
        language=CodeLanguage.BSL,
        mode=CodeMode.MAIN,
        expected_revision=before.revision,
        expected_document_sha256=before.document_sha256,
    )

    diff = store.diff("cell-main", 1, 2)
    assert "... diff truncated ..." in diff
    assert len(diff.splitlines()) <= 201


def test_diff_has_a_total_character_bound(tmp_path: Path) -> None:
    store, _ = seeded_store(tmp_path, source="old-" + "x" * 40_000)
    before = store.get("cell-main")
    store.put(
        cell_id="cell-main",
        source="new-" + "y" * 40_000,
        language=CodeLanguage.BSL,
        mode=CodeMode.MAIN,
        expected_revision=before.revision,
        expected_document_sha256=before.document_sha256,
    )

    diff = store.diff("cell-main", 1, 2)
    assert len(diff) <= 20_000
    assert "... diff truncated ..." in diff


def test_promote_preserves_the_exact_source(tmp_path: Path) -> None:
    store, _ = seeded_store(tmp_path, source="Ответ = 1;")
    before = store.get("cell-main")
    source = "  Ответ = 2;\n\n"

    promoted = store.promote(
        CodePutRequest(
            cell_id="cell-main",
            source=source,
            language=CodeLanguage.BSL,
            mode=CodeMode.MAIN,
            expected_revision=before.revision,
            expected_document_sha256=before.document_sha256,
        )
    )
    assert promoted.source == source
    assert store.get("cell-main").source == source


def test_delete_removes_active_cell_but_retains_immutable_history(tmp_path: Path) -> None:
    store, _ = seeded_store(tmp_path, source="Ответ = 1;")
    before = store.get("cell-main")
    store.delete(
        CodeDeleteRequest(
            cell_id="cell-main",
            expected_revision=before.revision,
            expected_document_sha256=before.document_sha256,
        )
    )

    with pytest.raises(CodeNotFound):
        store.get("cell-main")
    assert [revision.revision for revision in store.history("cell-main")] == [1]


def test_cell_ids_are_case_sensitive(tmp_path: Path) -> None:
    store, _ = seeded_store(tmp_path, source="Ответ = 1;", extra_cell_ids=("Cell-Main",))
    upper = store.get("Cell-Main")
    lower = store.get("cell-main")

    store.put(
        cell_id="Cell-Main",
        source="Cell_Main = 2",
        language=CodeLanguage.PYTHON,
        mode=CodeMode.MAIN,
        expected_revision=upper.revision,
        expected_document_sha256=upper.document_sha256,
    )
    assert store.get("Cell-Main").source == "Cell_Main = 2"
    assert store.get("cell-main").source == lower.source


def test_history_is_scoped_to_the_selected_notebook_identity(tmp_path: Path) -> None:
    store, _ = seeded_store(tmp_path, source="Ответ = 1;")
    first = store.get("cell-main")
    store.put(
        cell_id="cell-main",
        source="Ответ = 2;",
        language=CodeLanguage.BSL,
        mode=CodeMode.MAIN,
        expected_revision=first.revision,
        expected_document_sha256=first.document_sha256,
    )

    other = tmp_path / "project" / "notebooks" / "other.ipynb"
    other_cell = nbformat.v4.new_code_cell(source="ДругойОтвет = 1;", id="cell-main")
    other_cell.metadata["onec_runtime"] = {
        "revision": 1,
        "language": "bsl",
        "mode": "main",
        "source_sha256": sha256("ДругойОтвет = 1;"),
    }
    nbformat.write(nbformat.v4.new_notebook(cells=[other_cell]), other)

    store.list("notebooks/other.ipynb")
    assert store.get("cell-main", revision=1).source == "ДругойОтвет = 1;"
    assert [item.source for item in store.history("cell-main")] == ["ДругойОтвет = 1;"]


def test_history_persistence_failure_does_not_change_notebook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, notebook = seeded_store(tmp_path, source="Ответ = 1;")
    before = store.get("cell-main")
    original = notebook.read_bytes()

    def fail_prepare_history(*args: object) -> None:
        raise OSError("state disk full")

    monkeypatch.setattr(store, "_prepare_history", fail_prepare_history, raising=False)
    with pytest.raises(OSError, match="state disk full"):
        store.put(
            cell_id="cell-main",
            source="Ответ = 2;",
            language=CodeLanguage.BSL,
            mode=CodeMode.MAIN,
            expected_revision=before.revision,
            expected_document_sha256=before.document_sha256,
        )
    assert notebook.read_bytes() == original


def test_external_edit_after_staging_conflicts_without_replacing_editor_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, notebook = seeded_store(tmp_path, source="Ответ = 1;")
    before = store.get("cell-main")

    def edit_at_commit_boundary(path: Path) -> None:
        edit_notebook_source(path, "cell-main", "Ответ = редактор;")

    monkeypatch.setattr(store, "_before_document_commit", edit_at_commit_boundary, raising=False)
    with pytest.raises(CodeConflict):
        store.put(
            cell_id="cell-main",
            source="Ответ = 2;",
            language=CodeLanguage.BSL,
            mode=CodeMode.MAIN,
            expected_revision=before.revision,
            expected_document_sha256=before.document_sha256,
        )
    assert "Ответ = редактор;" in notebook.read_text(encoding="utf-8")
    assert [item.revision for item in store.history("cell-main")] == [1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing-mode guard")
def test_commit_guard_denies_editor_write_after_final_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, notebook = seeded_store(tmp_path, source="Ответ = 1;")
    before = store.get("cell-main")
    editor_was_denied = False

    def editor_write_after_verification(path: Path) -> None:
        nonlocal editor_was_denied
        with pytest.raises(PermissionError):
            edit_notebook_source(path, "cell-main", "Ответ = редактор;")
        editor_was_denied = True

    monkeypatch.setattr(
        store,
        "_after_document_verification",
        editor_write_after_verification,
        raising=False,
    )
    saved = store.put(
        cell_id="cell-main",
        source="Ответ = 2;",
        language=CodeLanguage.BSL,
        mode=CodeMode.MAIN,
        expected_revision=before.revision,
        expected_document_sha256=before.document_sha256,
    )

    assert editor_was_denied
    assert saved.source == "Ответ = 2;"
    assert store.get("cell-main").source == "Ответ = 2;"


def test_existing_immutable_revision_with_different_source_fails_closed(tmp_path: Path) -> None:
    store, notebook = seeded_store(tmp_path, source="Ответ = 1;")
    edit_notebook_source(notebook, "cell-main", "Ответ = редактор;")

    with pytest.raises(CodeConflict, match="immutable revision"):
        store.list("notebooks/demo.ipynb")

    assert store.get("cell-main", revision=1).source == "Ответ = 1;"


def test_unavailable_mutation_guard_fails_before_notebook_or_history_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, notebook = seeded_store(tmp_path, source="Ответ = 1;")
    before = store.get("cell-main")
    original_notebook = notebook.read_bytes()
    state_root = notebook.parents[1] / ".runtime" / "agent-service"
    original_history = {
        path.relative_to(state_root): path.read_bytes()
        for path in state_root.rglob("*.json")
    }

    monkeypatch.setattr(store, "_host_supports_guarded_mutations", lambda: False)
    assert not store.guarded_mutations_available
    with pytest.raises(CodePlatformUnavailable, match="guarded mutations"):
        store.put(
            cell_id="cell-main",
            source="Ответ = 2;",
            language=CodeLanguage.BSL,
            mode=CodeMode.MAIN,
            expected_revision=before.revision,
            expected_document_sha256=before.document_sha256,
        )

    assert notebook.read_bytes() == original_notebook
    assert {
        path.relative_to(state_root): path.read_bytes()
        for path in state_root.rglob("*.json")
    } == original_history
