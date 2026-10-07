---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `f7dd68310a9ff1a74df4bfc6dbb9ce2b60a1e2f8`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `f7dd68310a9ff1a74df4bfc6dbb9ce2b60a1e2f8`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/36823253405) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold check fails because a folded bullet (merge 2e0a4ed) claims to be in released section v0.87.1 but is not actually in that tag — the check-changelog-fold.sh script detects this mismatch as a CHANGELOG.md structure bug, not a dependency or Go build issue`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold check fails because a folded bullet (merge 2e0a4ed) claims to be in released section v0.87.1 but is not actually in that tag — the check-changelog-fold.sh script detects this mismatch as a CHANGELOG.md structure bug, not a dependency or Go build issue`
