default: precommit

check-versions:
	@python3 scripts/check-versions.py

check-changelog:
	@python3 scripts/check-changelog.py

# CHANGELOG.md grows a section per release and is never pruned, so it eventually crosses
# the release watcher's 1 MiB read cap — and the watcher then drops the whole repo from
# every poll cycle, so no release can be cut at all (measured 2026-10-13: 1 056 367 bytes,
# `repo dropped from cycle … too large` on every cycle). This fails while there is still
# runway, naming the remedy, so the cap can never be crossed silently again.
check-changelog-size:
	@python3 scripts/changelog-archive.py --check

# Rotate the oldest released sections into CHANGELOG-archive.md. Idempotent — a second run
# reports `nothing to archive`. Both byte limits live in scripts/changelog-archive.py.
changelog-archive:
	@python3 scripts/changelog-archive.py

check-spawn-mode:
	@python3 scripts/check-spawn-mode.py

check-worker-target:
	@python3 scripts/check-worker-target.py

check-recording-step:
	@python3 scripts/check-recording-step.py

check-bucket-clause:
	@python3 scripts/check-bucket-clause.py

check-content-key-formula:
	@python3 scripts/check-content-key-formula.py

check-subject-write-scope:
	@python3 scripts/check-subject-write-scope.py

check-provenance-sentence:
	@python3 scripts/check-provenance-sentence.py

check-necessity-templates:
	@python3 scripts/check-necessity-templates.py

check-inflight-limb:
	@python3 scripts/check-inflight-limb.py

check-goal-row-clause:
	@python3 scripts/check-goal-row-clause.py

check-budget:
	@python3 scripts/check-budget.py

check-gate-class-order:
	@python3 scripts/check-gate-class-order.py

check-step-when:
	@python3 scripts/check-step-when.py

check-handback-keys:
	@python3 scripts/check-handback-keys.py

check-round-overlap:
	@python3 scripts/check-round-overlap.py

check-drive-ledger-writes:
	@python3 scripts/check-drive-ledger-writes.py

check:
	@for f in server/*.mjs scripts/*.mjs; do case "$$f" in *.test.mjs) continue ;; esac; node --check "$$f" || exit 1; done; echo "  server + scripts modules parse"
	@python3 -c "import json;[json.load(open(f)) for f in ['.claude-plugin/plugin.json','.claude-plugin/marketplace.json','server/package.json','server/policy.json']]" && echo "  manifests parse"
	@test -f .mcp.json && test -f README.md && echo "  plugin files present"

test: check
	@rc=0; \
	node --test server/*.test.mjs || rc=1; \
	python3 -m unittest discover -s scripts/tests || rc=1; \
	exit $$rc

# `test`, not `check`: `check` only parses, `test` runs the suites — and `test: check`, so
# the parse checks stay implied. CI runs this target, so depending on `check` alone is what
# let a red suite report green: `node --test` and `unittest discover` had never run in CI.
#
# ⚠️ Requires `npm install --no-audit --no-fund` **inside `server/`** before the first run; CI
# does exactly that at `.github/workflows/ci.yml:24-25` — the `run:` line and its
# `working-directory: server` sibling. A fresh worktree carries no `server/node_modules`, so
# `node --test` dies with `ERR_MODULE_NOT_FOUND: Cannot find package
# '@anthropic-ai/claude-agent-sdk'` — while every `check-*` guard above still passes, which
# reads as a broken test rather than a missing install.
#
# ⚠️ **The directory is the part that bites, and the error does not name it.** There is no root
# `package.json` in this repo (only `server/package.json`, lockfile `server/bun.lock`), so a
# root `npm install` *succeeds* while resolving nothing, and the identical error comes back —
# a reader following the wrong directory gets no feedback that they did. `server/package.json`'s
# own `start` script is `bun install --no-summary && node supervisor.mjs`, so `bun install`
# inside `server/` works too.
precommit: check-versions check-changelog check-changelog-size check-spawn-mode check-worker-target check-recording-step check-bucket-clause check-content-key-formula check-subject-write-scope check-provenance-sentence check-necessity-templates check-inflight-limb check-goal-row-clause check-budget check-gate-class-order check-step-when check-handback-keys check-round-overlap check-drive-ledger-writes test

.PHONY: default check-versions check-changelog check-changelog-size changelog-archive check-spawn-mode check-worker-target check-recording-step check-bucket-clause check-content-key-formula check-subject-write-scope check-provenance-sentence check-necessity-templates check-inflight-limb check-goal-row-clause check-budget check-gate-class-order check-step-when check-handback-keys check-round-overlap check test precommit
