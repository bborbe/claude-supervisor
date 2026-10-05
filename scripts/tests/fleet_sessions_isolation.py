"""One isolated transcript + vault root for the whole test run — imported for its side effect.

`fleet-sessions.py` reads two ambient roots: `~/.claude/projects`, the transcripts the roster is
built from, and `~/Documents/Obsidian`, the vaults its work map walks. A suite that reads the
machine's real ones answers for the host rather than for the code — and on a clean CI runner
neither root exists, so `main()` takes its `no ~/.claude/projects` early return and every render
assertion then fails against *that string* instead of against the render. Measured 2026-09-30 on
PR #23's CI run: three of the four failures were exactly this.

⚠️ **ONE assignment, in ONE module, deliberately.** Same reason `start_cache_isolation.py` gives:
the documented run command (`python3 -m unittest discover -s scripts/tests`) imports every test
module into a SINGLE process, so several import-time assignments of the same key would leave only
the last one in force. Both roots are resolved per call in the subject, so this one assignment is
what every suite reads.

⚠️ **ASSIGNED, never `setdefault`.** An exported `SUPERVISOR_PROJECTS_DIR` or `OBSIDIAN_DIR` is
precisely the environment where isolation matters, and `setdefault` would silently defer to it.

⚠️ **The transcript fixture is load-bearing, not decoration.** `RetiredFlags` asserts the roster
is non-empty — its own docstring says an empty set "compares equal vacuously" — so pointing the
root at a bare tempdir would satisfy that assertion while testing nothing. The vault root, by
contrast, is deliberately left EMPTY: an empty vault tree is what a clean runner has, and the
work map degrading to `—` is the behaviour under test, not a gap in the fixture.

⚠️ `TemporaryDirectory` registers its own cleanup, so neither root outlives the run.
"""
import json
import os
import tempfile
from pathlib import Path

# Any UUID: the roster filters on `UUID_RE.fullmatch`, so a non-UUID name is skipped
# silently and the fixture would vanish without failing anything.
SID = "11111111-2222-3333-4444-555555555555"

_DIR = tempfile.TemporaryDirectory(prefix="claude-supervisor-test-fleet-")
_ROOT = Path(_DIR.name)

# `<projects>/<escaped-cwd>/<session-id>.jsonl` is the shape the reader globs for.
_PROJECTS = _ROOT / "projects"
(_PROJECTS / "-tmp-fixture-vault").mkdir(parents=True)
(_PROJECTS / "-tmp-fixture-vault" / f"{SID}.jsonl").write_text(
    json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00.000Z"}) + "\n",
    encoding="utf-8",
)

_VAULTS = _ROOT / "vaults"
_VAULTS.mkdir()

os.environ["SUPERVISOR_PROJECTS_DIR"] = str(_PROJECTS)
os.environ["OBSIDIAN_DIR"] = str(_VAULTS)
