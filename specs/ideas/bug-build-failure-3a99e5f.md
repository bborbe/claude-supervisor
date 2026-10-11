---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `3a99e5fdf5a82eea8203d941e5416047254cccc6`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `3a99e5fdf5a82eea8203d941e5416047254cccc6`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/38178118618) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold CI step fails deterministically at v0.129.5 because CHANGELOG.md has no topmost `## Unreleased` section and 1 bullet whose merge landed after the release cut is folded under a released heading — a repo-file defect (CHANGELOG structure) to be fixed by editing the changelog, not a dependency/go.sum/vuln issue and not a transient failure.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold CI step fails deterministically at v0.129.5 because CHANGELOG.md has no topmost `## Unreleased` section and 1 bullet whose merge landed after the release cut is folded under a released heading — a repo-file defect (CHANGELOG structure) to be fixed by editing the changelog, not a dependency/go.sum/vuln issue and not a transient failure.`
