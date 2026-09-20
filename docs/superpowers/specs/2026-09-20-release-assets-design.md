# Complete GitHub release assets design

## Goal

Make GitHub product releases self-contained while preserving PyPI Trusted
Publishing: every release must expose the two published Python wheels and
sdists, the standalone 1C extension and manifest, the independently versioned
VS Code extension, and one checksum file.

## Current release repair

Repair `v0.1.21` without changing its tag or its immutable PyPI files:

- recover the exact core and Jupyter distributions produced by successful
  workflow run `35510216839`;
- verify their wheel hashes against PyPI;
- extract `OnecInteractiveRuntime.cfe` and `extension-manifest.json` from the
  verified core wheel and compare them with canonical packaged resources;
- release the current VS Code source as `bsl-notebook-0.1.5.vsix` from a
  follow-up packaging/automation commit tagged `vscode-v0.1.5`;
- generate `SHA256SUMS.txt` for all uploaded assets;
- upload the complete bundle to the existing GitHub Release and document the
  VSIX follow-up tag in its notes.

The VSIX asset is independently versioned and its provenance must be explicit;
the Python release tag remains immutable.

## Future release workflow

The existing release workflow continues to build and publish only
`onec-interactive-runtime-core` and `onec-interactive-jupyter` to PyPI. Its
build job additionally:

1. installs, tests, compiles, and packages `packages/vscode`;
2. assembles and validates the complete GitHub bundle with a repository tool;
3. uploads that bundle as a GitHub Actions artifact.

After both PyPI jobs succeed, a separate job with `contents: write` downloads
the bundle and attaches every file to the existing GitHub Release using
`gh release upload --clobber`. No PyPI credentials are reused for GitHub
assets.

## Asset contract

The bundle contains exactly:

- `onec_interactive_runtime_core-<python-version>-py3-none-any.whl`
- `onec_interactive_runtime_core-<python-version>.tar.gz`
- `onec_interactive_jupyter-<python-version>-py3-none-any.whl`
- `onec_interactive_jupyter-<python-version>.tar.gz`
- `OnecInteractiveRuntime.cfe`
- `extension-manifest.json`
- `bsl-notebook-<vscode-version>.vsix`
- `SHA256SUMS.txt`

The checksum file covers the preceding seven files in deterministic filename
order. The assembly tool rejects missing, duplicate, unexpected, or stale
artifacts.

## Versioning and documentation

- Python packages remain `0.1.21` for the repaired release.
- VS Code becomes `0.1.5` in `package.json`, `package-lock.json`, and its
  installation documentation.
- The root README downloads `bsl-notebook-0.1.5.vsix` from `v0.1.21`.
- Runtime and 1C extension behavior/version are unchanged.

## Verification

- RED/GREEN tests cover successful bundle assembly, stale embedded extension
  rejection, unexpected files, deterministic checksums, and workflow wiring.
- VS Code `npm ci`, unit tests, compile, and package commands pass.
- The produced VSIX contains manifest version `0.1.5` and compiled extension
  code.
- The full packaging test file and focused extension consistency test pass.
- Before upload, every repaired release asset is compared with its authoritative
  source and the final GitHub asset list/checksums are independently verified.
