"""One pinned worker mode for the whole test run — imported for its side effect.

`permission-answer-poll.py` reads `SUPERVISOR_WORKER_MODE` at the top of `main()`, which is the
composition root and the right place for it. The suite that drives `main()` therefore inherits
whatever mode the *host* is running under: inside a headless supervised worker the ambient value
is `headless`, so the three cases that expect the polling path take the early exit instead and
fail. Measured 2026-11-08: `test_permission_answer_poll` went 3 failures + 3 errors inside a
supervised session and 0 with this one variable unset, and a one-variable bisect — unset only it
→ green, keep only it → red — named it. `test_plugin_version_census` went red beside it as a
cascade, because the failing poll cases leave `self.m.time.sleep` patched on the real `time`
module and the census reload leg sleeps.

⚠️ **ONE assignment, in ONE module, deliberately.** Same reason `start_cache_isolation.py`
gives: the documented run command (`python3 -m unittest discover -s scripts/tests`) imports every
test module into a SINGLE process, so several import-time assignments of the same key would
leave only the last one in force.

⚠️ **ASSIGNED, never `setdefault`.** An exported `SUPERVISOR_WORKER_MODE` is precisely the
environment where isolation matters, and `setdefault` would silently defer to it.

⚠️ **`interactive` is the documented default, not an arbitrary choice.** `SPAWN_MODES` in
`server/spawn-mode.mjs` is `['interactive', 'headless']` and its fallback returns
`{ mode: 'interactive' }`, so this is the mode a suite sees on a clean runner — which is also why
CI never saw the defect. The two cases that need the other value inject it themselves with
`mock.patch.dict`.
"""
import os

os.environ["SUPERVISOR_WORKER_MODE"] = "interactive"
