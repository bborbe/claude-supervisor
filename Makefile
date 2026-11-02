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

check-spawn-target:
	@python3 scripts/check-spawn-target.py

check-open-manager-resolution:
	@python3 scripts/check-open-manager-resolution.py

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

check-checkbox-regex-drift:
	@python3 scripts/check-checkbox-regex-drift.py

check-handback-keys:
	@python3 scripts/check-handback-keys.py

check-round-overlap:
	@python3 scripts/check-round-overlap.py

check-drive-ledger-writes:
	@python3 scripts/check-drive-ledger-writes.py

check-drive-audit-budget:
	@python3 scripts/check-drive-audit-budget.py

check-drive-unverifiable-paste:
	@python3 scripts/check-drive-unverifiable-paste.py

check: check-manager-allowlist check-open-manager-resolution
	@for f in server/*.mjs scripts/*.mjs; do case "$$f" in *.test.mjs) continue ;; esac; node --check "$$f" || exit 1; done; echo "  server + scripts modules parse"
	@python3 -c "import json;[json.load(open(f)) for f in ['.claude-plugin/plugin.json','.claude-plugin/marketplace.json','server/package.json','server/policy.json','server/manager-allowlist.json']]" && echo "  manifests parse"
	@test -f .mcp.json && test -f README.md && echo "  plugin files present"

# Two artifacts, one policy: `server/manager-allowlist.json` is what the server matches against,
# `docs/permission-allowlist.md` is what a human reads. Without this check they drift silently and
# in the worst direction — the doc is the half a reader trusts, the JSON is the half that decides.
check-manager-allowlist:
	@node scripts/check-manager-allowlist.mjs

# ⚠️ The guard the suite's own failure cannot give you, and the reason it is a target rather
# than another paragraph of the note below. With `server/node_modules` absent, `node --test`
# emits one `ERR_MODULE_NOT_FOUND` per module it cannot load — three here (`undici`, for
# `cluster-spawn.test.mjs`; the Agent SDK, for `mode.test.mjs` and, through the DYNAMIC
# `await import('./mode.mjs')` at `shipping-settings.test.mjs:45`, for that file too) — and
# then aborts before running a single test. Three module errors read as three broken tests,
# which is how they were read on 2026-10-14: a worker concluded the baseline was red and filed
# a task against a repo that was green. The note below is a comment; this is what `make` prints.
#
# ⚠️ It installs nothing, deliberately. A check that reaches the network fails for reasons that
# are not about the tree, and an `npm install` here would also drop an untracked
# `server/package-lock.json` into every worktree that ran it.
#
# ⚠️ It hangs off `test`, not off `precommit`, because `test` is the single point every path
# that runs the suites goes through — `make test` and `make precommit` alike. Wiring it into
# both would be two homes for one rule.
check-server-deps:
	@test -d server/node_modules || { echo "server/node_modules is missing — the suites cannot load. Run: cd server && npm install --no-audit --no-fund"; exit 1; }
	@echo "  server deps present"

test: check check-server-deps
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
precommit: check-versions check-changelog check-changelog-size check-spawn-mode check-spawn-target check-open-manager-resolution check-worker-target check-recording-step check-bucket-clause check-content-key-formula check-subject-write-scope check-provenance-sentence check-necessity-templates check-inflight-limb check-goal-row-clause check-budget check-gate-class-order check-step-when check-checkbox-regex-drift check-handback-keys check-round-overlap check-drive-ledger-writes check-drive-audit-budget check-drive-unverifiable-paste test

.PHONY: default check-versions check-changelog check-changelog-size changelog-archive check-spawn-mode check-spawn-target check-open-manager-resolution check-worker-target check-recording-step check-bucket-clause check-content-key-formula check-subject-write-scope check-provenance-sentence check-necessity-templates check-inflight-limb check-goal-row-clause check-budget check-gate-class-order check-step-when check-checkbox-regex-drift check-handback-keys check-round-overlap check-drive-audit-budget check-drive-unverifiable-paste check-manager-allowlist check check-server-deps test precommit
