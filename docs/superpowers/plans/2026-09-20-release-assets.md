# Complete GitHub Release Assets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair `v0.1.21` with a complete downloadable bundle and automate the same bundle for future GitHub releases.

**Architecture:** A small Python assembly tool owns the exact asset contract, embedded-extension extraction, canonical comparison, and deterministic checksums. GitHub Actions builds all independently versioned products, publishes Python distributions to PyPI as before, and uploads the validated bundle to the GitHub Release only after both PyPI jobs succeed.

**Tech Stack:** Python 3.12+, pytest, uv, npm, TypeScript, VSCE, GitHub Actions, GitHub CLI, PyPI JSON API.

**Spec:** `docs/superpowers/specs/2026-09-20-release-assets-design.md`

## Global Constraints

- Do not move or rewrite tag `v0.1.21` or replace any PyPI file.
- Python package versions remain `0.1.21`; VS Code becomes `0.1.5`.
- The standalone CFE and manifest must be byte-identical to the verified core wheel and canonical repository resources.
- Upload only bounded release artifacts; never upload environments, credentials, local paths, or raw live evidence.
- Preserve the user's original checkout and notebook changes.
- No runtime, protocol, or 1C extension behavior changes.

## Review Focus

- A GitHub bundle assembled from a different build could disagree with PyPI; compare wheel SHA-256 with official PyPI metadata before repairing `v0.1.21`.
- Reusing VSIX version `0.1.4` would create two different artifacts with one version; require `0.1.5` in source and inside the VSIX.
- A stale CFE extracted from an arbitrary wheel could bypass source validation; compare wheel bytes with canonical packaged resources.
- Broad `dist/*` upload can leak unexpected files; the assembler and workflow must enforce the exact eight-file contract.
- A workflow upload before partial PyPI failure would expose an incomplete release; the GitHub upload job must depend on both successful publish jobs.

---

### Task 1: Define and implement the release bundle contract

**Files:**
- Create: `tools/prepare_release_assets.py`
- Create: `tests/packaging/test_release_assets.py`
- Create: `docs/superpowers/specs/2026-09-20-release-assets-design.md`
- Create: `docs/superpowers/plans/2026-09-20-release-assets.md`

**Interfaces:**
- Produces: `prepare_release_assets(dist: Path, extension_root: Path) -> tuple[Path, ...]` and CLI options `--dist`, `--extension-root`.
- Produces: exactly eight validated release files including `SHA256SUMS.txt`.

- [ ] Write tests for a valid synthetic bundle, stale embedded CFE, duplicate/unexpected inputs, and deterministic checksum ordering.
- [ ] Run the focused tests and confirm RED because the tool does not exist.
- [ ] Implement the minimum assembler and CLI.
- [ ] Run the focused tests and confirm GREEN.

### Task 2: Release VS Code 0.1.5 and wire the workflow

**Files:**
- Modify: `packages/vscode/package.json`
- Modify: `packages/vscode/package-lock.json`
- Modify: `packages/vscode/README.md`
- Modify: `README.md`
- Modify: `.github/workflows/releases.yaml`
- Modify: `tests/packaging/test_release_assets.py`

**Interfaces:**
- Consumes: the Task 1 assembly CLI.
- Produces: VSIX `0.1.5` and an Actions `release-assets` bundle uploaded to the corresponding GitHub Release after both PyPI jobs.

- [ ] Add a failing workflow-contract test for VS Code build/package, complete bundle assembly, exact artifact upload, `contents: write`, and dependencies on both PyPI jobs.
- [ ] Run the focused test and confirm RED against the existing workflow.
- [ ] Bump VS Code and documentation to `0.1.5`.
- [ ] Update the workflow with VS Code setup/tests/package, asset assembly/upload, and the final GitHub release upload job.
- [ ] Run the focused tests and confirm GREEN.
- [ ] Run VS Code install, unit, compile, and package gates; inspect the VSIX manifest and compiled payload.
- [ ] Run packaging and extension bundle gates, review the exact diff, and commit once.

### Task 3: Publish the VS Code tag and repair v0.1.21

**Files:**
- Publish: the verified automation commit to `master`
- Publish: annotated tag `vscode-v0.1.5`
- Update: GitHub Release `v0.1.21` assets and notes

**Interfaces:**
- Consumes: the committed assembler, verified VSIX, workflow artifacts from run `35510216839`, and official PyPI hashes.
- Produces: the complete eight-file public release bundle.

- [ ] Recheck remote master/tag/release state and exact PyPI hashes.
- [ ] Push the single reviewed commit to `master`, tag it `vscode-v0.1.5`, and verify remote SHAs.
- [ ] Download the exact successful workflow distributions, validate their PyPI hashes, add the verified VSIX, and run the committed assembler.
- [ ] Upload the eight files to `v0.1.21` and amend notes with explicit VSIX tag provenance.
- [ ] Query GitHub assets, download them independently, verify `SHA256SUMS.txt`, CFE manifest hash, wheel PyPI hashes, and VSIX version/payload.
- [ ] Remove only the temporary release worktree and report the published URLs and any non-blocking workflow warnings.
