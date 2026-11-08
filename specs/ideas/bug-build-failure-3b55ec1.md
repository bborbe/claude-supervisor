---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `3b55ec1e50b81b54ba556ab63a6509f699405e9b`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): Changelog Fold Guard

Episode SHA: `3b55ec1e50b81b54ba556ab63a6509f699405e9b`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/42475129143) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The failing step 'Run the changelog fold guard' is a repo-owned changelog check, not dependency resolution, go.sum drift, or vulnerability scanning, so the root cause lies in the repository's own code/content and warrants a bug spec.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The failing step 'Run the changelog fold guard' is a repo-owned changelog check, not dependency resolution, go.sum drift, or vulnerability scanning, so the root cause lies in the repository's own code/content and warrants a bug spec.`
