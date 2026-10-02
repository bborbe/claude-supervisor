---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `b92c3a8ef7fe89ef56d1ec5ed12b0ef6a55824fd`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `b92c3a8ef7fe89ef56d1ec5ed12b0ef6a55824fd`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/37043409446) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold check fails because two folded bullets in CHANGELOG.md claim to be in released sections (v0.93.5, v0.90.1) but their merges are NOT in those tags — a test/assertion bug in the changelog validation script`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold check fails because two folded bullets in CHANGELOG.md claim to be in released sections (v0.93.5, v0.90.1) but their merges are NOT in those tags — a test/assertion bug in the changelog validation script`
