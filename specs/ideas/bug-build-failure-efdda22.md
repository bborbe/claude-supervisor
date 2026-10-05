---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `efdda2272aea86f7f04a249d04fca70f0236bd3c`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold

Episode SHA: `efdda2272aea86f7f04a249d04fca70f0236bd3c`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/37278059331) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold step fails because CHANGELOG.md is missing a `## Unreleased` section above the top released heading. The folded-bullet check and the releaser both require this section to be present. This is a code/test bug in the repo's changelog management — the CHANGELOG.md file is malformed at HEAD, missing the required section header.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold step fails because CHANGELOG.md is missing a `## Unreleased` section above the top released heading. The folded-bullet check and the releaser both require this section to be present. This is a code/test bug in the repo's changelog management — the CHANGELOG.md file is malformed at HEAD, missing the required section header.`
