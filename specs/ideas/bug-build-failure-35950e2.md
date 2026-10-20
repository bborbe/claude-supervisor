---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `35950e2b03026502bf857c3b135abc479be89be0`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `35950e2b03026502bf857c3b135abc479be89be0`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/39420423937) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold step failed because CHANGELOG.md is missing a '## Unreleased' section above v0.188.0; files scripts/manager-liveness.py and scripts/tests/test_manager_liveness.py have unreleased changes (merge c346f07a) that need a '## Unreleased' section in the changelog.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold step failed because CHANGELOG.md is missing a '## Unreleased' section above v0.188.0; files scripts/manager-liveness.py and scripts/tests/test_manager_liveness.py have unreleased changes (merge c346f07a) that need a '## Unreleased' section in the changelog.`
