"""Assemble and validate the downloadable GitHub release bundle."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from zipfile import ZipFile


_RESOURCE_ROOT = "onec_runtime/resources/extension"
_EXTENSION_FILES = ("OnecInteractiveRuntime.cfe", "extension-manifest.json")
_INPUT_PATTERNS = (
    "onec_interactive_runtime_core-*.whl",
    "onec_interactive_runtime_core-*.tar.gz",
    "onec_interactive_jupyter-*.whl",
    "onec_interactive_jupyter-*.tar.gz",
    "bsl-notebook-*.vsix",
)


def _single_match(dist: Path, pattern: str) -> Path:
    matches = sorted(dist.glob(pattern))
    if len(matches) != 1:
        raise ValueError(
            "unexpected release inputs: "
            f"expected exactly one {pattern!r}, found {[path.name for path in matches]}"
        )
    return matches[0]


def prepare_release_assets(
    dist: Path,
    extension_root: Path,
) -> tuple[Path, ...]:
    """Create the exact release bundle in *dist* and return its files."""

    dist = dist.resolve(strict=True)
    extension_root = extension_root.resolve(strict=True)
    inputs = tuple(_single_match(dist, pattern) for pattern in _INPUT_PATTERNS)
    expected_inputs = {path.name for path in inputs}
    actual_inputs = {
        path.name
        for path in dist.iterdir()
        if path.name != ".gitignore"
    }
    if actual_inputs != expected_inputs:
        raise ValueError(
            "unexpected release inputs: "
            f"expected {sorted(expected_inputs)}, found {sorted(actual_inputs)}"
        )

    core_wheel = inputs[0]
    extracted: dict[str, bytes] = {}
    with ZipFile(core_wheel) as archive:
        for filename in _EXTENSION_FILES:
            resource = f"{_RESOURCE_ROOT}/{filename}"
            embedded = archive.read(resource)
            canonical = (extension_root / filename).read_bytes()
            if embedded != canonical:
                raise ValueError(
                    f"embedded {filename} differs from canonical resource"
                )
            extracted[filename] = embedded

    for filename, payload in extracted.items():
        (dist / filename).write_bytes(payload)

    covered = sorted(
        (*inputs, *(dist / name for name in _EXTENSION_FILES)),
        key=lambda path: path.name,
    )
    checksum_path = dist / "SHA256SUMS.txt"
    checksum_path.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in covered
        ),
        encoding="ascii",
        newline="\n",
    )
    return tuple(sorted((*covered, checksum_path), key=lambda path: path.name))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Assemble the validated GitHub release asset bundle."
    )
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--extension-root", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    for path in prepare_release_assets(args.dist, args.extension_root):
        print(path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
