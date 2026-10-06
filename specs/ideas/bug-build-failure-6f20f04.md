---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `6f20f043d9c2976d1036ebaeb70a75caa46764d7`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `6f20f043d9c2976d1036ebaeb70a75caa46764d7`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/36930909454) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The `changelog-fold` step runs `.changelog-fold-guard/scripts/check-changelog-fold.sh` which detected 1 folded bullet referencing merge 2b777a5 that claims to be under v0.90.1 but the merge is NOT in that tag — a CHANGELOG validation script bug in the repo.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The `changelog-fold` step runs `.changelog-fold-guard/scripts/check-changelog-fold.sh` which detected 1 folded bullet referencing merge 2b777a5 that claims to be under v0.90.1 but the merge is NOT in that tag — a CHANGELOG validation script bug in the repo.`
