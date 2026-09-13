default: precommit

check-versions:
	@python3 scripts/check-versions.py

check-changelog:
	@python3 scripts/check-changelog.py

check:
	@node --check server/supervisor.mjs && echo "  server/supervisor.mjs parses"
	@python3 -c "import json;[json.load(open(f)) for f in ['.claude-plugin/plugin.json','.claude-plugin/marketplace.json','server/package.json','server/policy.json']]" && echo "  manifests parse"
	@test -f .mcp.json && test -f README.md && echo "  plugin files present"

test: check
	@node --test server/*.test.mjs

precommit: check-versions check-changelog check

.PHONY: default check-versions check-changelog check test precommit
