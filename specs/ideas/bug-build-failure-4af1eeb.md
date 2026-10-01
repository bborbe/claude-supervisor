---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `4af1eebc77425f3a80229d8ff937538587f993e1`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `4af1eebc77425f3a80229d8ff937538587f993e1`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/36917869499) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The `changelog-fold` step failed because CHANGELOG.md is missing a `## Unreleased` section above v0.90.1 while carrying unreleased folded bullets (merge 2b777a5 is claimed in v0.90.1 but is NOT in that tag), preventing the releaser from cutting the next release`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The `changelog-fold` step failed because CHANGELOG.md is missing a `## Unreleased` section above v0.90.1 while carrying unreleased folded bullets (merge 2b777a5 is claimed in v0.90.1 but is NOT in that tag), preventing the releaser from cutting the next release`
