---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `62eceee41e996ff734a6a6c350b63a7850750fd0`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `62eceee41e996ff734a6a6c350b63a7850750fd0`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/39325982559) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold validation script detected that a CHANGELOG bullet for merge 2d66d57a is folded under the released section ## v0.184.5, but that merge is NOT contained in v0.184.5 — the entry must be moved to ## Unreleased. This is a CHANGELOG management bug, not a Go dependency or build issue.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold validation script detected that a CHANGELOG bullet for merge 2d66d57a is folded under the released section ## v0.184.5, but that merge is NOT contained in v0.184.5 — the entry must be moved to ## Unreleased. This is a CHANGELOG management bug, not a Go dependency or build issue.`
