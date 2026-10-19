---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `dc8b29eb100718c650c48db988b159b8a3c41b5f`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `dc8b29eb100718c650c48db988b159b8a3c41b5f`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/39314851146) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold guard correctly detected that merge 36bba1b8 shipped in v0.184.0 but its bullet was placed in the v0.183.0 section — a misplacement of a CHANGELOG entry, not a dependency or Go toolchain issue.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold guard correctly detected that merge 36bba1b8 shipped in v0.184.0 but its bullet was placed in the v0.183.0 section — a misplacement of a CHANGELOG entry, not a dependency or Go toolchain issue.`
