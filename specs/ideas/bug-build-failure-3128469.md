---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `31284694f7339059e1e2e2b5f12dd00c87469654`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold/changelog-fold

Episode SHA: `31284694f7339059e1e2e2b5f12dd00c87469654`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/39289149188) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold guard failed because 5 source files (server/heartbeat.mjs, server/heartbeat.test.mjs, server/cluster-heartbeat.mjs, server/cluster-heartbeat.test.mjs, server/supervisor.mjs) were modified without a corresponding ## Unreleased entry in CHANGELOG.md — a documentation-process bug where unreleased code changes lack a changelog anchor`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold guard failed because 5 source files (server/heartbeat.mjs, server/heartbeat.test.mjs, server/cluster-heartbeat.mjs, server/cluster-heartbeat.test.mjs, server/supervisor.mjs) were modified without a corresponding ## Unreleased entry in CHANGELOG.md — a documentation-process bug where unreleased code changes lack a changelog anchor`
