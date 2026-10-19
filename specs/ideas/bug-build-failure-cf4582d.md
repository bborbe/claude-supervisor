---
status: idea
kind: bug
---

# Build Failure: bborbe/claude-supervisor

Filed automatically by the build-fix agent for the CI episode `cf4582d949adb906794285837868498b8650730b`.

## Summary

The default-branch build for `bborbe/claude-supervisor` is failing; the build-fix diagnosis classified this as a code/test bug (verdict `file_spec`).

## Reproduction

Failing workflow(s): changelog-fold / changelog-fold

Episode SHA: `cf4582d949adb906794285837868498b8650730b`

Log evidence:

```text
| Workflow | Job | Failed Step | Run |
|---|---|---|---|
| Changelog Fold Guard | changelog-fold / changelog-fold | Run the changelog fold guard | [Run](https://github.com/bborbe/claude-supervisor/actions/runs/39325206804) |
```

## Expected vs Actual

**Expected:** green CI on the default branch.
**Actual:** `The changelog-fold workflow failed because CHANGELOG.md has no topmost `## Unreleased` section while 2 files (scripts/manager-attention-watch.py and scripts/tests/test_manager_attention_watch.py) differ from v0.184.5 — the fix requires editing CHANGELOG.md to add the missing section and move the unreleased bullet into it.`

## Why this is a bug

The default-branch build is the repository's quality gate; a red build blocks merges. Diagnosis: `The changelog-fold workflow failed because CHANGELOG.md has no topmost `## Unreleased` section while 2 files (scripts/manager-attention-watch.py and scripts/tests/test_manager_attention_watch.py) differ from v0.184.5 — the fix requires editing CHANGELOG.md to add the missing section and move the unreleased bullet into it.`
