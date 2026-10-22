---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `a5c5cbf0b2157237a18c9ba4d53ddc8c0a0cec51`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): Changelog Fold Guard

Episode SHA: `a5c5cbf0b2157237a18c9ba4d53ddc8c0a0cec51`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/39824869583) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The only failing step, "Run the changelog fold guard" in the repo-owned "Changelog Fold Guard" workflow, is a repository-content validation script rather than dependency resolution, go.sum drift, or a vulnerable module, so the root cause is repo-side code/config that dark-factory must fix.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The only failing step, "Run the changelog fold guard" in the repo-owned "Changelog Fold Guard" workflow, is a repository-content validation script rather than dependency resolution, go.sum drift, or a vulnerable module, so the root cause is repo-side code/config that dark-factory must fix.`
