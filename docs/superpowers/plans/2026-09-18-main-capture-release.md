# MAIN/CAPTURE runtime release plan

**Goal:** Squash `feature/main-capture-runtime` into `master` as one commit and publish version `0.1.20` of the runtime core and Jupyter packages to PyPI.

**Architecture:** Preserve the branch's public `RuntimeSession` cutover and single RDBG owner. Use an isolated `master` worktree so the existing article branch and its uncommitted notebook edits remain untouched. The GitHub release workflow builds and publishes the two packages from the release tag.

**Tech stack:** Git, Python/uv/pytest, npm, GitHub Actions, PyPI Trusted Publishing.

## Checklist

1. Review the branch diff, repository instructions, version metadata, release workflow, and any unresolved correctness finding.
2. Squash the feature branch onto current `master`; update release facing documentation and fix any confirmed release blocker within the same commit.
3. Run focused checks, the offline unit suite, package/build checks, and relevant frontend checks. State live qualification evidence separately.
4. Commit the squash as one commit, push `master`, and confirm required checks.
5. Create and publish `v0.1.20`, monitor both PyPI publication jobs, and verify the uploaded files on PyPI.
6. Remove the isolated release worktree after publication, preserving the user's original checkout.
