from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from tools.prepare_release_assets import prepare_release_assets


VERSION = "0.1.21"
VSCODE_VERSION = "0.1.5"
RESOURCE_ROOT = "onec_runtime/resources/extension"


def _write_release_inputs(
    root: Path,
    *,
    embedded_cfe: bytes | None = None,
) -> tuple[Path, Path]:
    dist = root / "dist"
    extension_root = root / "extension"
    dist.mkdir()
    extension_root.mkdir()

    cfe = b"verified-cfe"
    manifest = b'{"artifact_version":"test"}\n'
    (extension_root / "OnecInteractiveRuntime.cfe").write_bytes(cfe)
    (extension_root / "extension-manifest.json").write_bytes(manifest)

    core_wheel = dist / f"onec_interactive_runtime_core-{VERSION}-py3-none-any.whl"
    with ZipFile(core_wheel, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{RESOURCE_ROOT}/OnecInteractiveRuntime.cfe",
            cfe if embedded_cfe is None else embedded_cfe,
        )
        archive.writestr(
            f"{RESOURCE_ROOT}/extension-manifest.json",
            manifest,
        )

    for filename, payload in (
        (f"onec_interactive_runtime_core-{VERSION}.tar.gz", b"core-sdist"),
        (f"onec_interactive_jupyter-{VERSION}-py3-none-any.whl", b"jupyter-wheel"),
        (f"onec_interactive_jupyter-{VERSION}.tar.gz", b"jupyter-sdist"),
        (f"bsl-notebook-{VSCODE_VERSION}.vsix", b"vscode-extension"),
    ):
        (dist / filename).write_bytes(payload)
    (dist / ".gitignore").write_text("*\n", encoding="utf-8")
    return dist, extension_root


def test_prepare_release_assets_builds_exact_deterministic_bundle(tmp_path: Path) -> None:
    dist, extension_root = _write_release_inputs(tmp_path)

    assets = prepare_release_assets(dist, extension_root)

    expected_names = {
        f"onec_interactive_runtime_core-{VERSION}-py3-none-any.whl",
        f"onec_interactive_runtime_core-{VERSION}.tar.gz",
        f"onec_interactive_jupyter-{VERSION}-py3-none-any.whl",
        f"onec_interactive_jupyter-{VERSION}.tar.gz",
        "OnecInteractiveRuntime.cfe",
        "extension-manifest.json",
        f"bsl-notebook-{VSCODE_VERSION}.vsix",
        "SHA256SUMS.txt",
    }
    assert {path.name for path in assets} == expected_names
    assert (dist / "OnecInteractiveRuntime.cfe").read_bytes() == b"verified-cfe"
    assert (dist / "extension-manifest.json").read_bytes() == (
        extension_root / "extension-manifest.json"
    ).read_bytes()

    checksum_lines = (dist / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines()
    covered_names = [line.split("  ", 1)[1] for line in checksum_lines]
    assert covered_names == sorted(expected_names - {"SHA256SUMS.txt"})
    for line in checksum_lines:
        digest, filename = line.split("  ", 1)
        assert digest == hashlib.sha256((dist / filename).read_bytes()).hexdigest()


def test_prepare_release_assets_rejects_stale_embedded_extension(tmp_path: Path) -> None:
    dist, extension_root = _write_release_inputs(
        tmp_path,
        embedded_cfe=b"stale-cfe",
    )

    with pytest.raises(ValueError, match="differs from canonical resource"):
        prepare_release_assets(dist, extension_root)

    assert not (dist / "SHA256SUMS.txt").exists()


def test_prepare_release_assets_rejects_unexpected_or_duplicate_inputs(
    tmp_path: Path,
) -> None:
    dist, extension_root = _write_release_inputs(tmp_path)
    (dist / "debug.log").write_text("must not ship", encoding="utf-8")
    (dist / "bsl-notebook-0.1.6.vsix").write_bytes(b"duplicate-vsix")

    with pytest.raises(ValueError, match="unexpected release inputs"):
        prepare_release_assets(dist, extension_root)

    assert not (dist / "SHA256SUMS.txt").exists()


def test_release_workflow_builds_and_uploads_complete_github_bundle() -> None:
    repository = Path(__file__).resolve().parents[2]
    workflow = (repository / ".github/workflows/releases.yaml").read_text(
        encoding="utf-8"
    )
    package = json.loads(
        (repository / "packages/vscode/package.json").read_text(encoding="utf-8")
    )
    package_lock = json.loads(
        (repository / "packages/vscode/package-lock.json").read_text(
            encoding="utf-8"
        )
    )

    assert package["version"] == "0.1.5"
    assert package_lock["version"] == "0.1.5"
    assert package_lock["packages"][""]["version"] == "0.1.5"
    assert "packages/vscode/package-lock.json" in workflow
    assert "npm ci --prefix packages/vscode" in workflow
    assert "npm --prefix packages/vscode run test:unit" in workflow
    assert "npm --prefix packages/vscode run compile" in workflow
    assert "bsl-notebook-${VSCODE_VERSION}.vsix" in workflow
    assert "python tools/prepare_release_assets.py" in workflow
    assert "name: release-assets" in workflow
    assert "upload-release-assets:" in workflow
    assert "needs: [build, publish-core, publish-jupyter]" in workflow
    assert "contents: write" in workflow
    assert "GH_REPO: ${{ github.repository }}" in workflow
    assert 'gh release upload "$RELEASE_TAG" release-assets/* --clobber' in workflow
