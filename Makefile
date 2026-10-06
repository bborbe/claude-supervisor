default: precommit

check-versions:
	@python3 scripts/check-versions.py

check-changelog:
	@python3 scripts/check-changelog.py

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
precommit: check-versions check-changelog check-spawn-mode check-worker-target check-recording-step check-bucket-clause check-content-key-formula check-subject-write-scope test

.PHONY: default check-versions check-changelog check-spawn-mode check-worker-target check-recording-step check-bucket-clause check-content-key-formula check-subject-write-scope check test precommit
