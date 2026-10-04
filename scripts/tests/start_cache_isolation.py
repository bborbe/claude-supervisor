"""One isolated start-time cache for the whole test run — imported for its side effect.

`session-liveness.py` caches `pid -> start-epoch` in a file, and it is keyed by PID, so a suite
that reads the machine's real store answers for whatever process last held that number: a result
that depends on the host rather than on the code. That is the same rule the heartbeat store
already carries ("an isolated heartbeat store, and it is not optional"), and it is why every
suite that loads the module imports this one first.

⚠️ **ONE assignment, in ONE module, deliberately.** Five suites load `session-liveness.py`, and
the documented run command (`python3 -m unittest discover -s scripts/tests`) imports every test
module into a SINGLE process. Five import-time assignments of the same environment key therefore
do not isolate five suites — only the last one survives, and because `_start_cache_path()` is
resolved per call rather than at import, every suite then reads and writes whichever store won.
The isolation guarantee held anyway (assignment rather than `setdefault`, so the real store was
never reached), but four of the five `TemporaryDirectory` objects were used by nobody. One home
for the assignment is what makes the comment true rather than merely reassuring.

⚠️ **ASSIGNED, never `setdefault`.** An exported `SUPERVISOR_START_CACHE` is precisely the
environment where isolation matters, and `setdefault` would silently defer to it.

⚠️ `TemporaryDirectory` registers its own cleanup, so the store does not outlive the run.
"""
import os
import tempfile

_DIR = tempfile.TemporaryDirectory(prefix="claude-supervisor-test-starts-")
os.environ["SUPERVISOR_START_CACHE"] = os.path.join(_DIR.name, "starts.json")
