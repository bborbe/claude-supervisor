default: precommit

check-versions:
	@python3 scripts/check-versions.py

check-changelog:
	@python3 scripts/check-changelog.py

check-spawn-mode:
	@python3 scripts/check-spawn-mode.py

check:
	@for f in server/*.mjs; do case "$$f" in *.test.mjs) continue ;; esac; node --check "$$f" || exit 1; done; echo "  server modules parse"
	@python3 -c "import json;[json.load(open(f)) for f in ['.claude-plugin/plugin.json','.claude-plugin/marketplace.json','server/package.json','server/policy.json']]" && echo "  manifests parse"
	@test -f .mcp.json && test -f README.md && echo "  plugin files present"

test: check
	@rc=0; \
	node --test server/*.test.mjs || rc=1; \
	python3 -m unittest discover -s scripts/tests || rc=1; \
	exit $$rc

# `test`, not `check`: check only parses, test runs the suites. CI runs this target,
# so depending on `check` alone is what let a red suite report green.
precommit: check-versions check-changelog check-spawn-mode test

.PHONY: default check-versions check-changelog check-spawn-mode check test precommit
