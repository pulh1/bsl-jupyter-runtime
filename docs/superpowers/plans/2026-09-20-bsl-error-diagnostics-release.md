# BSL Error Diagnostics v0.1.21 Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the source-mapped BSL error diagnostics on `master` as `v0.1.21`, including verified runtime-core and Jupyter packages on PyPI.

**Architecture:** Prepare one metadata-only release commit in an isolated worktree, prove the resulting source and binary distributions locally, then publish the exact commit through the existing GitHub Release and PyPI Trusted Publishing workflow. The MCP workspace package remains version-aligned but is not uploaded, matching the established release boundary.

**Tech Stack:** Git, Python 3.12+, uv, pytest, npm, GitHub CLI, GitHub Actions, PyPI Trusted Publishing.

**Spec:** `docs/superpowers/specs/2026-09-20-bsl-error-diagnostics-release-design.md`

## Global Constraints

- Release version is exactly `0.1.21`; tag is exactly `v0.1.21`.
- Release base is `master` commit `86b7972`.
- Publish only `onec-interactive-runtime-core` and `onec-interactive-jupyter` to PyPI.
- Keep `onec-interactive-mcp` version-aligned in the workspace but unpublished.
- Keep the VS Code extension at `0.1.4` and the product 1C extension artifact/protocol unchanged.
- Do not add the deferred MCP expert diagnostic contract.
- Do not commit credentials, live evidence, local paths, generated environments, or built artifacts.
- Preserve the user's existing checkout and uncommitted notebook edits.

## Review Focus

- A stale `0.1.20` package version or exact dependency would produce inconsistent wheels; the version-consistency and packaging checks must catch it.
- A README-wide replacement could incorrectly change the VSIX release/version; inspect the final README diff explicitly.
- A tag not reachable from `master` would fail the workflow ancestor gate; verify `origin/master` equals the tagged commit before release publication.
- A partial PyPI publication must be retried from the same immutable tag and artifacts; never rebuild under `0.1.21` after one package is uploaded.
- A local install can accidentally resolve packages from the public index; local artifact installation must use `--no-index`, and the final public verification must use a fresh environment and the PyPI index.

---

### Task 1: Prepare the metadata-only release commit

**Files:**
- Create: `docs/superpowers/specs/2026-09-20-bsl-error-diagnostics-release-design.md`
- Create: `docs/superpowers/plans/2026-09-20-bsl-error-diagnostics-release.md`
- Modify: `pyproject.toml`
- Modify: `src/onec_runtime/__init__.py`
- Modify: `packages/jupyter/pyproject.toml`
- Modify: `packages/jupyter/src/onec_runtime_jupyter/__init__.py`
- Modify: `packages/jupyter/frontend/package.json`
- Modify: `packages/jupyter/frontend/package-lock.json`
- Modify: `packages/mcp/pyproject.toml`
- Modify: `packages/mcp/src/onec_runtime_mcp/__init__.py`
- Modify: `uv.lock`
- Modify: `README.md`
- Test: `tests/packaging/test_product_wheels.py`

**Interfaces:**
- Consumes: the version fields and exact dependency pins currently set to `0.1.20`.
- Produces: a single internally consistent `0.1.21` source tree suitable for the existing release workflow.

- [ ] **Step 1: Confirm the release preconditions**

Run a read-only script that asserts every product version and exact dependency
pin is `0.1.20`, `v0.1.21` is absent locally and remotely, `master` and
`origin/master` both equal `86b7972`, and PyPI reports `0.1.20` as the latest
core and Jupyter release.

Expected: all assertions pass and no release state is changed.

- [ ] **Step 2: Update all release metadata to `0.1.21`**

Change only the files listed above. In README, update the source release link,
the stated Python package version, and the installation pin to `0.1.21`; leave
the VSIX reference at `v0.1.18` / `0.1.4`.

- [ ] **Step 3: Regenerate lock metadata**

Run: `uv lock --offline`

Expected: only the three workspace package versions and their exact core pins
move to `0.1.21`; third-party dependency resolutions do not change.

- [ ] **Step 4: Verify metadata consistency and the README diff**

Run a Python script that loads the three `pyproject.toml` files, the two Python
`__version__` declarations, the frontend `package.json`/`package-lock.json`,
and `uv.lock`, and asserts version `0.1.21` everywhere. Assert Jupyter and MCP
both require `onec-interactive-runtime-core==0.1.21`. Inspect
`git diff -- README.md` and confirm the VSIX reference is unchanged.

Expected: every assertion passes; no stale product `0.1.20` remains outside
historical release documents/specifications.

- [ ] **Step 5: Run the focused metadata/packaging gate**

Run: `uv run --group dev python -m pytest tests/packaging/test_product_wheels.py -q`

Expected: all 12 packaging tests pass.

- [ ] **Step 6: Commit the release candidate**

Run `git diff --check`, inspect the complete changed-file list, then commit
with message `chore: prepare v0.1.21 release`.

Expected: one release commit on top of `86b7972`, with no generated artifacts
or local paths tracked.

### Task 2: Qualify the exact release candidate

**Files:**
- Read: `.github/workflows/releases.yaml`
- Read: `tests/packaging/test_product_wheels.py`
- Verify: built wheel and sdist artifacts outside the tracked tree

**Interfaces:**
- Consumes: the committed, version-consistent `0.1.21` source tree from Task 1.
- Produces: test output and local package artifacts proving the exact commit is publishable.

- [ ] **Step 1: Run the offline unit gate**

Run: `uv run --group dev python -m pytest tests/unit -q`

Expected: the full offline unit suite passes; conditional skips are reported
separately and are not described as live qualification.

- [ ] **Step 2: Verify the Jupyter frontend**

Run:

```powershell
npm --prefix packages/jupyter/frontend ci
npm --prefix packages/jupyter/frontend test
npm --prefix packages/jupyter/frontend run build
```

Expected: dependency installation, frontend tests, TypeScript compilation,
and JupyterLab extension build all succeed.

- [ ] **Step 3: Verify the bundled 1C extension is unchanged and current**

Run: `uv run --group dev python -m pytest tests/unit/test_universal_extension_source.py::test_checked_in_bundle_matches_canonical_source_and_manifest -q`

Expected: one test passes.

- [ ] **Step 4: Build the exact publishable artifacts**

Create a fresh temporary output directory outside the tracked tree, then run:

```powershell
uv build --package onec-interactive-runtime-core --no-sources --out-dir $releaseArtifacts
uv build --package onec-interactive-jupyter --no-sources --out-dir $releaseArtifacts
```

Expected: exactly one wheel and one sdist for each published package, all with
version `0.1.21`; no MCP distribution is present.

- [ ] **Step 5: Inspect and install the local artifacts without an index**

Check wheel METADATA, filenames, bundled extension files, and Jupyter frontend
assets. Create a fresh temporary virtual environment and install
`onec-interactive-jupyter==0.1.21` with `--no-index --find-links
$releaseArtifacts`. Import both packages and assert their distribution and
module versions are exactly `0.1.21`.

Expected: the clean installation succeeds using only the four local artifacts.

- [ ] **Step 6: Verify the branch is publication-ready**

Run `git diff --check`, `git status --short --branch`, and compare `HEAD` with
`86b7972`. Confirm there is exactly one release commit and the worktree is
clean. Prepare release notes that report the fresh test counts and clearly
label the previously completed disposable-infobase checks as earlier live
qualification of the runtime commit.

Expected: clean branch, exact release notes, and no untracked artifacts.

### Task 3: Publish and verify `v0.1.21`

**Files:**
- Publish: Git commit, annotated Git tag, GitHub Release, PyPI distributions
- Verify: GitHub Actions run and PyPI JSON/file metadata

**Interfaces:**
- Consumes: the exact verified Task 2 commit and release notes.
- Produces: public GitHub release `v0.1.21` and PyPI core/Jupyter `0.1.21` distributions.

- [ ] **Step 1: Recheck remote state immediately before publication**

Fetch `origin`, assert `origin/master` is still `86b7972`, confirm tag/release
`v0.1.21` does not exist, and confirm PyPI still has no `0.1.21` files.

Expected: no concurrent release or master update has appeared.

- [ ] **Step 2: Push the verified commit and tag**

Push `HEAD` to `origin/master`, verify the remote SHA, create annotated tag
`v0.1.21` with message `Release v0.1.21`, and push that tag.

Expected: `origin/master`, local `HEAD`, and `v0.1.21^{commit}` are identical.

- [ ] **Step 3: Publish the GitHub Release**

Create a non-draft, non-prerelease GitHub Release named
`v0.1.21 - source-mapped BSL diagnostics` from the verified existing tag and
the prepared release notes.

Expected: the release is public and triggers `.github/workflows/releases.yaml`.

- [ ] **Step 4: Monitor Trusted Publishing to completion**

Locate the release-triggered workflow run and watch it until the build,
`publish-core`, and `publish-jupyter` jobs all complete successfully.

Expected: the workflow conclusion is `success`. If publication is partial,
retry the same workflow/tag without changing source or artifacts.

- [ ] **Step 5: Verify PyPI and a clean public installation**

Query the official PyPI JSON endpoints and assert both packages report
`0.1.21`, with one wheel and one sdist each. In a fresh environment, install
`onec-interactive-jupyter==0.1.21` from PyPI and assert the installed Jupyter
and core distribution/module versions are `0.1.21`.

Expected: both public packages install and import successfully from PyPI.

- [ ] **Step 6: Record final release state**

Confirm the GitHub Release URL, workflow URL, PyPI project URLs, remote commit,
and tag. Remove only temporary local environments/artifacts and the isolated
release worktree after the release is fully verified.

Expected: the public release is reproducible from one tagged release commit,
and the user's original checkout remains unchanged.
