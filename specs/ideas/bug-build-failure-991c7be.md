---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `991c7bef79be7527acb7ce9612976f358a086f9f`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): Changelog Fold Guard

Episode SHA: `991c7bef79be7527acb7ce9612976f358a086f9f`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/38451626844) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The failing step 'Run the changelog fold guard' is a repo-local guard/tooling check rather than dependency resolution, go.sum drift, or a vulnerable module, so the root cause lies in repo code/tooling that dark-factory should fix.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The failing step 'Run the changelog fold guard' is a repo-local guard/tooling check rather than dependency resolution, go.sum drift, or a vulnerable module, so the root cause lies in repo code/tooling that dark-factory should fix.`
