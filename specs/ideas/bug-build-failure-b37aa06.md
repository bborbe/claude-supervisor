---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `b37aa0628c25b4ea0af9fbd545eea9b7c25a15e2`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold / changelog-fold

Episode SHA: `b37aa0628c25b4ea0af9fbd545eea9b7c25a15e2`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/38175225416) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold guard fails deterministically at HEAD with 'FAIL: 1 folded bullet(s) across 407 released section(s) at v0.129.3' and 'the releaser is stalled at v0.129.3 — unreleased work with no topmost `## Unreleased` section', i.e. a repo-content defect in CHANGELOG.md (a bullet folded under a released heading with the Unreleased section missing), not a dependency, go.mod/go.sum, or vulnerability problem.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold guard fails deterministically at HEAD with 'FAIL: 1 folded bullet(s) across 407 released section(s) at v0.129.3' and 'the releaser is stalled at v0.129.3 — unreleased work with no topmost `## Unreleased` section', i.e. a repo-content defect in CHANGELOG.md (a bullet folded under a released heading with the Unreleased section missing), not a dependency, go.mod/go.sum, or vulnerability problem.`
