---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `ed74884e6c9d72ea89fa4d011e0659e2e5f7f856`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `ed74884e6c9d72ea89fa4d011e0659e2e5f7f856`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/39417466545) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold guard detected 3 folded bullets placed under ## v0.187.1 whose merge SHA 3e98a4be is NOT in that tag, indicating CHANGELOG.md has unreleased work misfiled under a released version heading rather than under ## Unreleased.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold guard detected 3 folded bullets placed under ## v0.187.1 whose merge SHA 3e98a4be is NOT in that tag, indicating CHANGELOG.md has unreleased work misfiled under a released version heading rather than under ## Unreleased.`
