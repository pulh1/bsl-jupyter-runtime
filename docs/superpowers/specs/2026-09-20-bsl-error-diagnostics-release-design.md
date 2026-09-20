# BSL error diagnostics v0.1.21 release design

## Goal

Release the source-mapped BSL error diagnostics already present on `master` as
version `0.1.21`, publish the runtime core and Jupyter distributions to PyPI,
and leave the user's existing checkout and notebook edits untouched.

## Scope

- Base the release on `master` commit `86b7972`.
- Use one release commit containing version metadata, lock-file updates,
  release-facing README updates, and the release planning documents.
- Keep `onec-interactive-runtime-core`, `onec-interactive-jupyter`, and the
  unpublished workspace package `onec-interactive-mcp` on the same version.
- Publish only `onec-interactive-runtime-core==0.1.21` and
  `onec-interactive-jupyter==0.1.21` through the existing GitHub Actions
  Trusted Publishing workflow.
- Keep the VS Code extension at `0.1.4`; this release contains no VS Code
  product changes.
- Do not change runtime behavior, the 1C extension artifact/protocol, or the
  deferred MCP expert diagnostic contract.

## Release metadata

Update the product version from `0.1.20` to `0.1.21` in:

- `pyproject.toml`
- `src/onec_runtime/__init__.py`
- `packages/jupyter/pyproject.toml`
- `packages/jupyter/src/onec_runtime_jupyter/__init__.py`
- `packages/jupyter/frontend/package.json`
- `packages/jupyter/frontend/package-lock.json`
- `packages/mcp/pyproject.toml`
- `packages/mcp/src/onec_runtime_mcp/__init__.py`
- `uv.lock`

Update the root README installation command and GitHub release link to
`0.1.21`. Preserve the separate VSIX link to `v0.1.18` and version `0.1.4`.

## Verification

The release candidate must pass:

1. the repository's product packaging test file;
2. the offline unit suite;
3. Jupyter frontend tests and production build;
4. the checked-in 1C extension source/bundle consistency test;
5. local core and Jupyter wheel/sdist builds;
6. installation of the locally built Jupyter distribution and its exact core
   dependency in a clean environment;
7. a final branch review and a clean Git diff/status check.

The runtime commit being released has already passed the full unit suite and
live disposable-infobase qualification for the ZUP capture and overview demos,
the UT sales demo, and diagnostic probes. Because the release commit changes
only metadata and documentation, live 1C qualification is not repeated; the
release notes must distinguish that earlier live evidence from the fresh
release-candidate checks.

## Publication

Push the verified release commit to `master`, create and push annotated tag
`v0.1.21`, and publish a GitHub Release from that exact tag. Monitor the
existing `Publish Python packages to PyPI` workflow until both publication jobs
succeed. Then verify the `0.1.21` files through the PyPI JSON API and install
`onec-interactive-jupyter==0.1.21` from PyPI in a clean environment.

PyPI uploads are immutable. If core publishes but Jupyter fails, retry the
same workflow/tag; do not create a different build or reuse the version for
changed artifacts.
