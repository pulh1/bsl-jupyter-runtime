from __future__ import annotations

import subprocess
import json
import os
import sys
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path
from re import split

WORKSPACE = Path(__file__).parents[2]
PRODUCT_VERSION = tomllib.loads((WORKSPACE / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']


def _normalized_distribution(filename: str) -> str:
    return filename.split("-", 1)[0].replace("_", "-")


def build_workspace_wheels(tmp_path: Path) -> dict[str, Path]:
    output = tmp_path / "wheels"
    completed = subprocess.run(
        ["uv", "build", "--all-packages", "--wheel", "--out-dir", str(output)],
        cwd=WORKSPACE,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return {_normalized_distribution(path.name): path for path in output.glob("*.whl")}


def build_jupyter_release_wheel(tmp_path: Path) -> Path:
    output = tmp_path / "jupyter-release"
    completed = subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "onec-interactive-jupyter",
            "--wheel",
            "--out-dir",
            str(output),
        ],
        cwd=WORKSPACE,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheels = tuple(output.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _metadata(wheel: Path):
    with zipfile.ZipFile(wheel) as archive:
        (member,) = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        return BytesParser().parsebytes(archive.read(member))


def requires_dist(wheel: Path) -> set[str]:
    return set(_metadata(wheel).get_all("Requires-Dist", []))


def _requirement_distribution(requirement: str) -> str:
    return split(r"[<>=!~\[\s]", requirement.split(";", 1)[0].strip(), maxsplit=1)[
        0
    ].casefold().replace("_", "-")


def test_product_dependency_edges_and_versions(tmp_path: Path) -> None:
    wheels = build_workspace_wheels(tmp_path)

    assert set(wheels) == {
        "onec-interactive-runtime-core",
        "onec-interactive-jupyter",
        "onec-interactive-mcp",
    }
    assert {_metadata(wheel)["Version"] for wheel in wheels.values()} == {PRODUCT_VERSION}
    for wheel in wheels.values():
        expected_license = 'GPL-3.0-only'
        if wheel in (
            wheels['onec-interactive-runtime-core'],
            wheels['onec-interactive-jupyter'],
        ):
            expected_license += ' AND MIT'
        assert _metadata(wheel)['License-Expression'] == expected_license
        with zipfile.ZipFile(wheel) as archive:
            license_files = [name for name in archive.namelist() if name.endswith('.dist-info/licenses/LICENSE')]
            assert len(license_files) == 1
            assert archive.read(license_files[0]) == (WORKSPACE / 'LICENSE').read_bytes()
            copyright_files = [name for name in archive.namelist() if name.endswith('.dist-info/licenses/COPYRIGHT')]
            assert len(copyright_files) == 1
            assert archive.read(copyright_files[0]) == (WORKSPACE / 'COPYRIGHT').read_bytes()
    assert {
        requirement for requirement in requires_dist(wheels["onec-interactive-jupyter"])
        if "extra ==" not in requirement
    } == {
        "ipykernel<7,>=6.29",
        "ipython<10,>=8.22",
        "jupyter-client<9,>=8.6",
        f"onec-interactive-runtime-core=={PRODUCT_VERSION}",
        "psutil<8,>=6",
    }
    assert {
        _requirement_distribution(requirement)
        for requirement in requires_dist(wheels["onec-interactive-jupyter"])
        if "extra ==" in requirement
    } == {"jupyterlab-lsp", "jupyter-lsp"}
    assert "paramiko<5,>=4" in requires_dist(wheels["onec-interactive-runtime-core"])
    assert f"onec-interactive-runtime-core=={PRODUCT_VERSION}" in requires_dist(
        wheels["onec-interactive-mcp"]
    )


def test_core_wheel_preserves_v8unpack_mit_notice(tmp_path: Path) -> None:
    wheel = build_workspace_wheels(tmp_path)["onec-interactive-runtime-core"]
    with zipfile.ZipFile(wheel) as archive:
        notice_name = next(
            name for name in archive.namelist()
            if name.endswith('.dist-info/licenses/THIRD_PARTY_NOTICES.txt')
        )
        notice = archive.read(notice_name).decode('utf-8')
    assert 'saby-integration/v8unpack' in notice
    assert 'Copyright (c) 2015 infactum' in notice
    assert 'Permission is hereby granted, free of charge' in notice
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in notice
    assert _metadata(wheel)['License-Expression'] == (
        'GPL-3.0-only AND MIT'
    )


def test_source_distributions_ship_corresponding_sources_and_notices(tmp_path: Path) -> None:
    output = tmp_path / 'sdists'
    completed = subprocess.run(
        ['uv', 'build', '--all-packages', '--sdist', '--out-dir', str(output)],
        cwd=WORKSPACE,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    archives = {path.name.split('-', 1)[0]: path for path in output.glob('*.tar.gz')}
    assert set(archives) == {
        'onec_interactive_runtime_core', 'onec_interactive_jupyter', 'onec_interactive_mcp'
    }
    for archive_path in archives.values():
        with tarfile.open(archive_path) as archive:
            members = {member.name for member in archive.getmembers()}
            root = archive_path.name.removesuffix('.tar.gz') + '/'
            assert not any('/node_modules/' in name or '/.venv/' in name for name in members)
            assert archive.extractfile(root + 'LICENSE').read() == (WORKSPACE / 'LICENSE').read_bytes()
            assert archive.extractfile(root + 'COPYRIGHT').read() == (WORKSPACE / 'COPYRIGHT').read_bytes()
            if archive_path == archives['onec_interactive_runtime_core']:
                assert root + 'THIRD_PARTY_NOTICES.txt' in members
                assert root + 'src/onec_runtime/epf_container.py' in members
                assert root + 'onec/OnecInteractiveRuntime/Configuration.xml' in members
                assert root + 'onec/OnecInteractiveRuntime/Ext/ManagedApplicationModule.bsl' in members
                assert root + 'tools/build_runtime_extension_bundle.py' in members
            elif archive_path == archives['onec_interactive_jupyter']:
                assert root + 'frontend/src/index.ts' in members
                assert root + 'src/onec_runtime_jupyter/labextension/static/third-party-licenses.json' in members


def test_product_wheels_exclude_repository_support(tmp_path: Path) -> None:
    wheels = build_workspace_wheels(tmp_path)
    forbidden = (
        "/demo/",
        "/integration/",
        "/notebooks/",
        "/tests/",
        "_spike.py",
        "_live_evidence.py",
        "_private_replay.py",
        "/benchmark.py",
        "zup_cross_module_sources.py",
        "zup_cross_module_linker.py",
        "zup_cross_module_evidence.py",
        "zup_cross_module_reload_spike.py",
        "test_zup_cross_module_reload_1c.py",
        "test_worker_universe_1c.py",
        "кадровыйучет",
        "кадровыйучетрасширенный",
    )

    for wheel in wheels.values():
        with zipfile.ZipFile(wheel) as archive:
            members = tuple(f"/{name}" for name in archive.namelist())
        assert not any(marker in member for member in members for marker in forbidden)

    with zipfile.ZipFile(wheels["onec-interactive-runtime-core"]) as archive:
        members = set(archive.namelist())
        assert {
            "onec_runtime/bsl/generated_semantic_parser.py",
            "onec_runtime/bsl/module_catalog.py",
            "onec_runtime/bsl/module_universe.py",
            "onec_runtime/worker_universe.py",
        } <= members


def test_product_wheels_have_no_com_runtime_dependency(tmp_path: Path) -> None:
    """Break caught: a test-harness COM dependency must never ship at runtime."""
    wheels = build_workspace_wheels(tmp_path)
    forbidden_distributions = {"pywin32", "pypiwin32"}
    forbidden_payload_markers = (
        b"comconnector",
        b"win32com",
        b"pythoncom",
        b"pywin32",
        b"v83.comconnector",
        b"comcntr",
    )

    for wheel in wheels.values():
        dependency_names = {
            _requirement_distribution(requirement)
            for requirement in requires_dist(wheel)
        }
        assert dependency_names.isdisjoint(forbidden_distributions)
        with zipfile.ZipFile(wheel) as archive:
            product_payload = b"\n".join(
                archive.read(name)
                for name in archive.namelist()
            ).lower()
        assert not any(
            marker in product_payload for marker in forbidden_payload_markers
        )


def test_requirement_distribution_normalizes_version_and_extra_syntax() -> None:
    assert _requirement_distribution("pywin32>=306; sys_platform == 'win32'") == (
        "pywin32"
    )
    assert _requirement_distribution("PyWin32[interop] ~= 306") == "pywin32"


def test_product_sources_have_no_com_connector_implementation() -> None:
    forbidden = (b"win32com", b"pythoncom", b"v83.comconnector", b"comcntr")
    product_sources = (
        path
        for root in (WORKSPACE / "src", WORKSPACE / "packages", WORKSPACE / "onec")
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".py", ".bsl", ".json"}
    )

    for path in product_sources:
        payload = path.read_bytes().lower()
        assert not any(marker in payload for marker in forbidden), path


def test_only_core_wheel_contains_runtime_and_extension_bundle(
    tmp_path: Path,
) -> None:
    wheels = build_workspace_wheels(tmp_path)
    extension = {
        "onec_runtime/resources/extension/OnecInteractiveRuntime.cfe",
        "onec_runtime/resources/extension/extension-manifest.json",
    }
    core_markers = {
        "onec_runtime/__init__.py",
        "onec_runtime/session.py",
        "onec_runtime/bsl/generated_semantic_parser.py",
    }

    with zipfile.ZipFile(wheels["onec-interactive-runtime-core"]) as archive:
        core_members = set(archive.namelist())
    assert extension | core_markers <= core_members

    with zipfile.ZipFile(wheels["onec-interactive-jupyter"]) as archive:
        jupyter_members = set(archive.namelist())
    assert "onec_runtime_jupyter/__init__.py" in jupyter_members
    assert not any(name.startswith("onec_runtime/") for name in jupyter_members)
    assert not any(name.startswith("onec_runtime_mcp/") for name in jupyter_members)

    with zipfile.ZipFile(wheels["onec-interactive-mcp"]) as archive:
        mcp_members = set(archive.namelist())
    assert not any("resources/extension" in name for name in mcp_members)
    assert not any(name.startswith("onec_runtime/") for name in mcp_members)


def test_jupyter_release_build_produces_one_core_dependent_wheel(
    tmp_path: Path,
) -> None:
    wheel = build_jupyter_release_wheel(tmp_path)

    assert wheel.name == f"onec_interactive_jupyter-{PRODUCT_VERSION}-py3-none-any.whl"
    assert f"onec-interactive-runtime-core=={PRODUCT_VERSION}" in requires_dist(wheel)
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
    assert "onec_runtime_jupyter/__init__.py" in members
    assert not any(name.startswith("onec_runtime/") for name in members)
    assert not any(name.startswith("onec_runtime_mcp/") for name in members)


def test_jupyter_wheel_ships_discoverable_highlighting_and_licenses(tmp_path: Path) -> None:
    wheel = build_jupyter_release_wheel(tmp_path)
    assert _metadata(wheel)['License-Expression'] == (
        'GPL-3.0-only AND MIT'
    )
    with zipfile.ZipFile(wheel) as archive:
        prefix = (
            f"onec_interactive_jupyter-{PRODUCT_VERSION}.data/data/share/jupyter/labextensions/"
            "@onec-interactive/jupyter-bsl/"
        )
        manifest = json.loads(archive.read(prefix + "package.json"))
        assert manifest['version'] == PRODUCT_VERSION
        assert manifest['license'] == 'GPL-3.0-only AND MIT'
        assert archive.read(prefix + 'LICENSE') == (WORKSPACE / 'LICENSE').read_bytes()
        assert archive.read(prefix + 'COPYRIGHT') == (WORKSPACE / 'COPYRIGHT').read_bytes()
        assert prefix + manifest["jupyterlab"]["_build"]["load"] in archive.namelist()
        install = json.loads(archive.read(prefix + "install.json"))
        assert install["packageName"] == "onec-interactive-jupyter"
        licenses = json.loads(archive.read(prefix + "static/third-party-licenses.json"))
        grammar = next(
            item for item in licenses["packages"]
            if item["name"] == "@1c-syntax/codemirror-lang-bsl"
        )
        assert grammar["licenseId"] == "MIT"
        assert "1C-Syntax contributors" in grammar["extractedText"]
        assert "Permission is hereby granted" in grammar["extractedText"]


def _install_product(tmp_path: Path, distribution: str) -> tuple[Path, dict[str, Path]]:
    wheels = build_workspace_wheels(tmp_path)
    environment = tmp_path / distribution
    created = subprocess.run(
        ["uv", "venv", "--seed", str(environment)],
        cwd=WORKSPACE,
        text=True,
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    python = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--find-links",
            str(next(iter(wheels.values())).parent),
            str(wheels[distribution]),
        ],
        cwd=WORKSPACE,
        text=True,
        capture_output=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    return python, wheels


def _run_import(python: Path, source: str) -> str:
    completed = subprocess.run(
        [str(python), "-c", source],
        cwd=python.parent,
        env={key: value for key, value in os.environ.items()
             if key != "PYTHONPATH" and not key.startswith("ONEC_PARSERGEN")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_jupyter_product_installs_matching_core_distribution(
    tmp_path: Path,
) -> None:
    python, _ = _install_product(tmp_path, "onec-interactive-jupyter")

    output = _run_import(
        python,
        """
import importlib.metadata as metadata
from pathlib import Path
from tempfile import TemporaryDirectory
import onec_runtime
import onec_runtime_jupyter
from onec_runtime.extension_bundle import packaged_extension_bundle
from onec_runtime.session import RuntimeSessionConfig
from onec_runtime_jupyter.lsp_kernel import ProjectBridge
from onec_runtime_jupyter.lsp_project import ProjectConfig, encode_envelope, decode_envelope
assert 'site-packages' in onec_runtime.__file__
assert 'site-packages' in onec_runtime_jupyter.__file__
for dependency in ('jupyter-lsp', 'jupyterlab-lsp', 'parsergen'):
    try:
        metadata.version(dependency)
    except metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError('Optional/build dependency installed in base product')
assert decode_envelope(encode_envelope(1, ProjectConfig('a' * 32, None), None))[1].source_root is None
assert not any(name.startswith('jupyter_lsp') for name in __import__('sys').modules)

def installed(name):
    try:
        metadata.version(name)
    except metadata.PackageNotFoundError:
        return False
    return True

print(metadata.version("onec-interactive-jupyter"))
print(installed("onec-interactive-runtime-core"))
print(installed("onec-interactive-mcp"))
with TemporaryDirectory() as root:
    bundle = packaged_extension_bundle(Path(root))
    print(bundle.manifest.extension_name)
    print(bundle.cfe_path.is_file())
""",
    )

    assert output.splitlines() == [
        PRODUCT_VERSION,
        "True",
        "False",
        "OnecInteractiveRuntime",
        "True",
    ]


def test_mcp_product_installs_core_without_ipython(tmp_path: Path) -> None:
    python, _ = _install_product(tmp_path, "onec-interactive-mcp")

    output = _run_import(
        python,
        "import importlib.util, importlib.metadata as m; import onec_runtime; "
        "import onec_runtime_mcp; "
        "from pathlib import Path; from tempfile import TemporaryDirectory; "
        "from onec_runtime.extension_bundle import packaged_extension_bundle; "
        "print(m.version('onec-interactive-runtime-core')); "
        "print(importlib.util.find_spec('IPython') is not None); "
        "root=TemporaryDirectory(); bundle=packaged_extension_bundle(Path(root.name)); "
        "print(bundle.manifest.extension_name); print(bundle.cfe_path.is_file())",
    )

    assert output.splitlines() == [PRODUCT_VERSION, "False", "OnecInteractiveRuntime", "True"]
