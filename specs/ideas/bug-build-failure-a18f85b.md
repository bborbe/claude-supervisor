---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `a18f85b230ce8f74487c5353954d199610fc5560`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `a18f85b230ce8f74487c5353954d199610fc5560`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/39807200194) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold guard failed deterministically at HEAD: `check-changelog-fold.sh` reported `FAIL: 2 folded bullet(s) across 264 released section(s) at v0.202.0`, naming the two bullets (merges 4f532473 and 9a2c1b44, `git tag --contains` finds no tag) as sitting under `## v0.202.0` when their destination is `## Unreleased` — a repo-content/CHANGELOG.md placement defect, not a dependency, go.sum, or vulnerability issue.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold guard failed deterministically at HEAD: `check-changelog-fold.sh` reported `FAIL: 2 folded bullet(s) across 264 released section(s) at v0.202.0`, naming the two bullets (merges 4f532473 and 9a2c1b44, `git tag --contains` finds no tag) as sitting under `## v0.202.0` when their destination is `## Unreleased` — a repo-content/CHANGELOG.md placement defect, not a dependency, go.sum, or vulnerability issue.`
