#!/usr/bin/env python3
"""`/supervisor:fleet-reset` — clear the fleet's remembered state. Destructive, confirm-gated.

The fleet remembers itself in five stores, and auto-resume reads the task files on top of
them. A reset must clear the five AND (behind a separate opt-in) strip the task-file links,
or the next keep-alive leg resumes the sessions the reset was meant to retire:

  sessions         <root>/sessions/*.json                the live registry, one file per pid
  worker-manager   <root>/state/worker-manager/*.json    manager-loop anchors
  open-items       <root>/state/open-items/*.json        per-session asks ledgers
  holds            <root>/state/session-holds.json       the operator's do-not-work marks
  snapshot         <root>/state/fleet-snapshot.json      the sweep baseline

Four properties are load-bearing, and each has a test in `tests/test_fleet_reset.py`:

  * NOTHING happens without `--confirm fleet-reset`. Without the token the verb refuses
    with a named reason and touches nothing.
  * `--dry-run` is INERT: it prints the full plan — per-store counts, the pids it would
    signal, the task files it would touch — and changes no file, not even an mtime.
  * The task-link strip is a SEPARATE flag, `--strip-task-links`, never implied by the
    state clear. A state-only run leaves every task file byte-identical.
  * The strip is LIVENESS-GATED: it unlinks a task only when EVERY session id it carries —
    `claude_session_id` and each `metrics_sessions` entry — answers `ABSENT` from
    `session-liveness.py --check` AND the identity registry holds no running pid for it.
    `LIVE`, `UNKNOWN`, `AMBIGUOUS`, `MALFORMED` and a registry-backed process all KEEP the
    link — fail-closed, because an unlink is what makes a live session invisible to
    keep-alive, nudge, close and resume (measured 2026-11-10: one status-only strip
    unlinked 30 live sessions across two vaults). ⚠️ Both halves are load-bearing:
    `--check` answered `ABSENT` for session `4295ddac` while its pid 26515 was running, and
    the strip removes the whole `metrics_sessions` block, which can name a *different, live*
    session. The verdicts are snapshotted BEFORE the stop, so the dry-run and the real run
    decide from one fleet state and the prediction is the count the real run strips.

Sessions are stopped BEFORE their registry entries are removed, so no live process is
orphaned into a registry-less state. Each pid is SIGTERMed, given `--grace` seconds, then
SIGKILLed; a pid that survives both KEEPS its registry entry and is reported, because a
cleared entry over a live process is exactly the orphan this ordering exists to prevent.
This session's own pid and its ancestors are never signalled, and their registry entries
are never removed — a reset that kills the session running it cannot report what it did.

⚠️ This is NOT `/supervisor:reset`. That verb re-discovers state and NEVER deletes; its
contract is the vault's `Manager Session` runbook § Reset. This verb deletes. The two share
no code and must not be folded together.

Store roots are overridable so a drill or a test never reads or writes the live tree:
`--root` (default `~/.claude`), `SUPERVISOR_FLEET_RESET_ROOT`, and for the process-shape
guard `SUPERVISOR_FLEET_RESET_PROC_PATTERN` (default `claude`). Task directories come from
`--task-dir` (repeatable) or, when absent, from `vault-cli config list`.
"""
import argparse
import fcntl
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

CONFIRM_TOKEN = "fleet-reset"

#: Statuses after which a task is not resumed. `hold` is terminal for this purpose: the
#: operator parked the session, so the keep-alive leg will not resume it either.
TERMINAL_STATUSES = {"completed", "aborted", "hold"}

LINK_KEYS = ("claude_session_id", "metrics_sessions")

#: `session-liveness.py --check`'s documented exit codes. The instrument's contract, not a
#: re-derivation: it prints LIVE on stdout and every other verdict on stderr, so the exit code
#: is the only channel that carries all five.
SESSION_VERDICTS = {0: "LIVE", 1: "ABSENT", 2: "UNKNOWN", 3: "AMBIGUOUS", 4: "MALFORMED"}

#: One probe per id per run; the child is bounded so a wedged endpoint cannot hang a reset.
PROBE_TIMEOUT = 20.0

_HERE = os.path.dirname(os.path.abspath(__file__))
_VERDICT_MEMO = {}
_IDENTITY_MEMO = {}

#: The identity registry: one `<pid>.json` per session, carrying `sessionId` and `pid`.
SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "sessions")


class Stores(object):
    """The five stores, under one root so a drill can point the whole verb at a fixture."""

    def __init__(self, root):
        self.root = os.path.expanduser(root)
        self.sessions = os.path.join(self.root, "sessions")
        self.state = os.path.join(self.root, "state")
        self.worker_manager = os.path.join(self.state, "worker-manager")
        self.open_items = os.path.join(self.state, "open-items")
        self.holds = os.path.join(self.state, "session-holds.json")
        self.snapshot = os.path.join(self.state, "fleet-snapshot.json")
        self.lock = os.path.join(self.state, "fleet-reset.lock")


class Locked(object):
    """The whole run is one read-modify-write; the sidecar flock makes it one critical section.

    Same discipline `session-holds.py` uses: a sidecar lock, released on exit, and every
    individual change landed by `os.unlink` or a tmp+`os.replace`. The lock is skipped in
    dry-run, because creating it would itself be a write.
    """

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.handle = open(self.path, "a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        return False


# ---------------------------------------------------------------- readers

def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def count_jsons(directory):
    return len(glob.glob(os.path.join(directory, "*.json")))


def count_holds(path):
    """Hold ENTRIES, not the file. An absent or unreadable store counts as zero."""
    holds = read_json(path).get("holds")
    return len(holds) if isinstance(holds, dict) else 0


def file_count(path):
    return 1 if os.path.exists(path) else 0


def registry_entries(directory):
    """`[(path, record)]` for every parsable registry file. Unreadable files are skipped."""
    entries = []
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict):
            entries.append((path, rec))
    return entries


# ---------------------------------------------------------------- process control

def own_pids():
    """This process and every ancestor, so the reset never signals the session running it.

    `os.getppid()` seeds the parent without a subprocess, so a failed `ps` walk still
    protects the immediate parent — the smallest protected set is this process and its
    parent, never this process alone.
    """
    pids = {os.getpid()}
    ppid = os.getppid()
    if ppid > 1:
        pids.add(ppid)
    pid = ppid
    while True:
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            break
        if out.returncode != 0:
            break
        try:
            ppid = int((out.stdout or "0").strip() or 0)
        except ValueError:
            break
        if ppid <= 1 or ppid in pids:
            break
        pids.add(ppid)
        pid = ppid
    return pids


def pid_alive(pid):
    """Whether `pid` is a RUNNING process.

    ⚠️ `os.kill(pid, 0)` alone answers True for a zombie: the pid still exists until its
    parent reaps it, so a SIGTERMed session whose parent has not yet called wait() would
    read as alive, burn the whole grace window, and then be reported `SURVIVED` — a false
    survivor on every kill. The process STATE is the discriminator, so `Z` answers False.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return not (out.stdout or "").strip().startswith("Z")


def proc_comm(pid):
    try:
        out = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "").strip()


def stop_sessions(stores, dry_run, grace):
    """Stop every live session, then remove its registry file.

    Returns `(lines, survivors, kept)`. `kept` is the number of entries this run leaves
    behind — the session running it, its ancestors, and any pid it could not stop — so a
    dry-run can print the count it WOULD leave rather than the count still on disk.

    Ordering is the contract: a registry entry is removed only after its pid is proven gone.
    A pid that survives SIGTERM and SIGKILL keeps its entry and is reported as a survivor,
    so the caller can exit non-zero rather than claim a clean reset over a live process.
    """
    lines = []
    survivors = []
    kept = 0
    protected = own_pids()
    self_sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    pattern = os.environ.get("SUPERVISOR_FLEET_RESET_PROC_PATTERN", "claude")
    comm_re = re.compile(pattern, re.I)
    entries = registry_entries(stores.sessions)
    lines.append(f"   registry {len(entries)} entry(ies) under {stores.sessions}")

    for path, rec in entries:
        sid = str(rec.get("sessionId") or rec.get("session_id") or "").strip()
        name = str(rec.get("name") or "").strip()
        pid = rec.get("pid")
        label = f"{sid[:8] or '?'} {name or ''}".strip()
        if sid and self_sid and sid == self_sid:
            kept += 1
            lines.append(f"   keep    {label} — this session")
            continue
        if not isinstance(pid, int) or isinstance(pid, bool):
            # No pid cannot name a live process, so there is nothing to orphan: the entry is
            # stale metadata and is cleared with the rest of the registry.
            lines.append(f"   stale   {label} — no integer pid")
            if not dry_run:
                _unlink(path)
            continue
        if pid in protected:
            kept += 1
            lines.append(f"   keep    {label} — pid {pid} is this process or an ancestor")
            continue
        if not pid_alive(pid):
            lines.append(f"   stale   {label} — pid {pid} already gone")
            if not dry_run:
                _unlink(path)
            continue
        comm = proc_comm(pid)
        if not comm_re.search(comm):
            survivors.append((path, pid, label))
            kept += 1
            lines.append(
                f"   refuse  {label} — pid {pid} is {comm or 'unreadable'}, not /{pattern}/; "
                "its entry is kept"
            )
            continue
        if dry_run:
            lines.append(f"   would stop {label} — pid {pid} ({comm})")
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            lines.append(f"   stale   {label} — pid {pid} vanished before the signal")
            _unlink(path)
            continue
        except PermissionError:
            survivors.append((path, pid, label))
            kept += 1
            lines.append(f"   refuse  {label} — pid {pid} cannot be signalled (permission)")
            continue
        if _wait_gone(pid, grace):
            _unlink(path)
            lines.append(f"   stopped {label} — pid {pid} (SIGTERM)")
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            _unlink(path)
            lines.append(f"   stopped {label} — pid {pid} (exited during grace)")
            continue
        except PermissionError:
            survivors.append((path, pid, label))
            kept += 1
            lines.append(f"   refuse  {label} — pid {pid} cannot be killed (permission)")
            continue
        if _wait_gone(pid, grace):
            _unlink(path)
            lines.append(f"   stopped {label} — pid {pid} (SIGKILL after grace)")
        else:
            survivors.append((path, pid, label))
            kept += 1
            lines.append(f"   SURVIVED {label} — pid {pid} is still alive; its entry is kept")
    return lines, survivors, kept


def _wait_gone(pid, timeout):
    deadline = time.time() + max(timeout, 0)
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- store clear

def clear_json_dir(directory, dry_run):
    """Empty a directory of `*.json` files. Returns `(before, after)`.

    The dry-run's `after` is the count it WOULD leave — 0 — not the count still on disk.
    Printing `168 → 168` under `--dry-run` would describe a no-op, which is the opposite
    of the plan the operator is approving.
    """
    before = count_jsons(directory)
    if not dry_run and os.path.isdir(directory):
        for path in glob.glob(os.path.join(directory, "*.json")):
            _unlink(path)
    after = 0 if dry_run else count_jsons(directory)
    return before, after


def clear_file(path, before, dry_run):
    """Remove one store file. Returns `(before, after)` — 0 in a dry-run, as above."""
    if not dry_run:
        _unlink(path)
    return before, (0 if dry_run else file_count(path))


# ---------------------------------------------------------------- task-link strip

def task_dirs(explicit):
    """`--task-dir` values, else every configured vault's task directory."""
    if explicit:
        return [os.path.expanduser(p) for p in explicit]
    override = os.environ.get("SUPERVISOR_VAULT_CONFIG")
    try:
        if override:
            with open(os.path.expanduser(override), encoding="utf-8") as fh:
                vaults = json.load(fh)
        else:
            out = subprocess.run(
                ["vault-cli", "config", "list", "--output", "json"],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode != 0:
                return []
            vaults = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    dirs = []
    for vault in vaults if isinstance(vaults, list) else []:
        if not isinstance(vault, dict):
            continue
        root = str(vault.get("path") or "").strip()
        tasks = str(vault.get("tasks_dir") or "").strip()
        if root and tasks:
            dirs.append(os.path.expanduser(os.path.join(root, tasks)))
    return dirs


def split_frontmatter(text):
    """`(frontmatter, rest)` or `(None, text)` when the page carries no frontmatter.

    `frontmatter` keeps the leading newline after the opening `---`; reassembling with
    `"---" + frontmatter + rest` reproduces the file byte-for-byte apart from the edit.
    """
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    return text[3:end], text[end:]


def frontmatter_status(frontmatter):
    match = re.search(r"^status:\s*(\S+)", frontmatter, re.M)
    return match.group(1).strip("'\"") if match else ""


def has_links(frontmatter):
    return any(re.search(rf"^{key}\s*:", frontmatter, re.M) for key in LINK_KEYS)


def strip_key(frontmatter, key):
    """Remove a top-level `key:` and its indented block, leaving every other line intact."""
    lines = frontmatter.split("\n")
    out = []
    i = 0
    while i < len(lines):
        if re.match(rf"^{re.escape(key)}\s*:", lines[i]):
            i += 1
            while i < len(lines) and lines[i][:1] in (" ", "\t"):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def write_atomic(path, text):
    tmp = path + ".fleet-reset.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def scan_tasks(dirs):
    """`[(path, status, frontmatter, rest)]` for every task file, in a stable order."""
    rows = []
    for directory in dirs:
        for path in sorted(glob.glob(os.path.join(directory, "*.md"))):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            frontmatter, rest = split_frontmatter(text)
            if frontmatter is None:
                continue
            rows.append((path, frontmatter_status(frontmatter), frontmatter, rest))
    return rows


def session_id(frontmatter):
    """The task's `claude_session_id`, or `""` when it carries none."""
    match = re.search(r"^claude_session_id:\s*(\S+)", frontmatter, re.M)
    return match.group(1).strip("'\"") if match else ""


def _verdict_from(returncode, stderr):
    """Map the instrument's exit code to a verdict, corroborated against its stderr word.

    ⚠️ `1` is both the instrument's ABSENT code and Python's uncaught-exception code. A
    load-time failure — a `SyntaxError` or an `ImportError` in the script or a sibling it
    imports — exits 1 without printing a verdict, and ABSENT is the one verdict that
    authorises the destructive unlink. An exit 1 that does not carry `ABSENT` on stderr is
    therefore UNKNOWN, never ABSENT.
    """
    verdict = SESSION_VERDICTS.get(returncode, "UNKNOWN")
    if verdict == "ABSENT" and "ABSENT" not in (stderr or ""):
        return "UNKNOWN"
    return verdict


def session_verdict(sid):
    """`session-liveness.py --check`'s verdict for one id, memoized per run.

    Read from the exit code, not the text: the instrument prints `LIVE` on stdout and every
    other verdict on stderr, so a stdout-only reader can only ever answer `LIVE` or `UNKNOWN`.
    An unreadable probe is `UNKNOWN` — never `ABSENT`, which is a verdict about a session
    rather than about the probe.
    """
    if sid in _VERDICT_MEMO:
        return _VERDICT_MEMO[sid]
    try:
        done = subprocess.run(
            [sys.executable, os.path.join(_HERE, "session-liveness.py"), "--check", sid],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        _VERDICT_MEMO[sid] = "UNKNOWN"
        return "UNKNOWN"
    verdict = _verdict_from(done.returncode, done.stderr)
    _VERDICT_MEMO[sid] = verdict
    return verdict


def _pid_alive(pid):
    """Delegates to the stop leg's zombie-aware `pid_alive`, behind a type guard.

    A registry pid that is not an int is not a liveness answer: `ps -p 1234.0` exits
    non-zero, which would read as "dead" and open the strip. It keeps the link instead.
    """
    if not isinstance(pid, int) or isinstance(pid, bool):
        return True
    return pid_alive(pid)


def _identity_rows(directory):
    """`{session id: pid}` from `<directory>/*.json`, or `None` when it cannot be read.

    ⚠️ `glob` on a missing directory returns `[]` rather than raising, so an absent registry
    would otherwise be indistinguishable from "no session is live" — the one reading that
    permits the destructive strip. `session-identity.read_registry` separates the two the
    same way, for the same reason.
    """
    key = os.path.abspath(directory)
    if key in _IDENTITY_MEMO:
        return _IDENTITY_MEMO[key]
    rows = None
    if os.path.isdir(directory):
        rows = {}
        try:
            for path in glob.glob(os.path.join(directory, "*.json")):
                try:
                    with open(path, encoding="utf-8") as fh:
                        rec = json.load(fh)
                except (OSError, ValueError):
                    continue
                sid = str(rec.get("sessionId") or "").strip()
                pid = rec.get("pid")
                if sid and pid:
                    rows[sid] = pid
        except OSError:
            rows = None
    _IDENTITY_MEMO[key] = rows
    return rows


def identity_alive(sid, sessions_dir=None):
    """Does the identity registry hold a RUNNING pid for this session id?

    ⚠️ **`ABSENT` alone is not evidence of death.** Measured 2026-11-10: `--check 4295ddac`
    answered `ABSENT` for a session whose registry entry carried pid 26515 and whose process
    was running — the endpoint's coverage gap, not a verdict. A strip that trusted `ABSENT`
    alone would have re-run the very incident this guard exists to prevent, so a positive
    liveness verdict is corroborated against the one store that knows the pid.

    The directory is the verb's OWN registry (`<root>/sessions`), never a hardcoded
    `~/.claude/sessions`: `--root` exists so a drill or a test never reads the live tree,
    and corroborating against a different store than the one just stopped would be a
    silent mismatch. Fail-closed in both directions — an unreadable registry answers
    "alive", because keeping a link is always the safe move.
    """
    rows = _identity_rows(sessions_dir or SESSIONS_DIR)
    if rows is None:
        return True
    pid = rows.get(sid)
    return pid is not None and _pid_alive(pid)


def session_ids(frontmatter):
    """Every session id the row carries: `claude_session_id` plus each `metrics_sessions` id.

    ⚠️ The `metrics_sessions` match is UNANCHORED on purpose — its entries are indented
    (`    - session_id: …`), so a `^session_id:` match collects only the frontmatter id and
    reproduces the very bug this exists to fix (measured 2026-10-01: 64 of 124 tasks
    carrying a `metrics_sessions` block carry no `claude_session_id` at all).
    """
    ids = []
    primary = session_id(frontmatter)
    if primary:
        ids.append(primary)
    for sid in re.findall(r"^\s*-\s*session_id:\s*(\S+)", frontmatter, re.M):
        sid = sid.strip("'\"")
        if sid and sid not in ids:
            ids.append(sid)
    return ids


class LinkState(object):
    """Liveness as measured at ONE instant.

    ⚠️ The strip runs after `stop_sessions()` has killed the fleet, and `--dry-run` stops
    nothing — so probing inside `strip_task_links()` would have the two measure different
    states (a pruned registry and a heartbeat that still reads `live: true` for up to
    `HEARTBEAT_TTL_MS`). The prediction would stop being the count the real run strips.
    One snapshot, taken before any destructive step, is what makes them agree.
    """

    def __init__(self, sessions_dir, verdict=None, alive=None):
        self._verdict = verdict or session_verdict
        self._alive = alive or (lambda sid: identity_alive(sid, sessions_dir))
        self._memo = {}

    def dead(self, sid):
        if sid not in self._memo:
            self._memo[sid] = self._verdict(sid) == "ABSENT" and not self._alive(sid)
        return self._memo[sid]


def strippable(row, state):
    """May this task lose its links? Only when EVERY id it carries is dead.

    ⚠️ Gating on `claude_session_id` alone would be wrong twice over. The strip removes the
    whole `metrics_sessions` block, and that block can name a *different, live* session —
    `server/task-binding.mjs` appends a second worker to an owned task and leaves
    `claude_session_id` intact, and the repo's rule is "alive if ANY id in the set is alive"
    (`manager-predispatch.session_id_set`). And a task carrying only a `metrics_sessions`
    block would never be stripped at all. `LIVE`, `UNKNOWN`, `AMBIGUOUS`, `MALFORMED`, an id
    the registry still backs with a process, and a link carrying no id all keep the link.
    """
    ids = session_ids(row[2])
    if not ids:
        return False
    return all(state.dead(sid) for sid in ids)


def strip_task_links(dirs, dry_run, state):
    """Strip the links from every non-terminal task whose sessions are ALL dead.

    Returns `(candidates, stripped, kept_live)`.

    A terminal task (`completed` / `aborted` / `hold`) is left byte-identical: its session
    link is history, not a resume target, and rewriting it would be an edit the reset has
    no reason to make. A non-terminal task whose sessions are not all dead is kept for the
    same kind of reason — it is still resumable, and unlinking it is what made 30 live
    sessions invisible to keep-alive on 2026-11-10.

    `state` is a `LinkState` snapshot taken BEFORE the stop, so the dry-run and the real run
    decide from the same measurements rather than from two different fleet states.
    """
    rows = scan_tasks(dirs)
    linked = [r for r in rows if has_links(r[2]) and r[1] not in TERMINAL_STATUSES]
    candidates = [r for r in linked if strippable(r, state)]
    kept_live = len(linked) - len(candidates)
    stripped = 0
    if dry_run:
        return len(candidates), 0, kept_live
    for path, _status, frontmatter, rest in candidates:
        new = frontmatter
        for key in LINK_KEYS:
            new = strip_key(new, key)
        write_atomic(path, "---" + new + rest)
        stripped += 1
    return len(candidates), stripped, kept_live


# ---------------------------------------------------------------- report

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Clear the fleet's remembered state. Destructive; requires --confirm.",
    )
    ap.add_argument("--root", default=os.environ.get("SUPERVISOR_FLEET_RESET_ROOT") or "~/.claude",
                    help="store root (default: ~/.claude)")
    ap.add_argument("--confirm", default="",
                    help=f"the literal token {CONFIRM_TOKEN!r}; without it nothing is changed")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the full plan and change nothing")
    ap.add_argument("--strip-task-links", action="store_true",
                    help="ALSO strip claude_session_id / metrics_sessions from non-terminal task "
                         "files — never implied by the state clear")
    ap.add_argument("--task-dir", action="append", default=[],
                    help="a task directory to scan for the strip (repeatable; default: every "
                         "configured vault's tasks dir)")
    ap.add_argument("--grace", type=float, default=10.0,
                    help="seconds to wait after SIGTERM before SIGKILL (default: 10)")
    args = ap.parse_args(argv)

    stores = Stores(args.root)
    if not args.dry_run and args.confirm != CONFIRM_TOKEN:
        print(
            f"❌ fleet-reset: refusing to act without --confirm {CONFIRM_TOKEN}. "
            "Nothing was changed. Run --dry-run first to see the plan."
        )
        return 2

    dirs = task_dirs(args.task_dir) if args.strip_task_links else []
    if args.strip_task_links and not dirs:
        print("❌ fleet-reset: --strip-task-links found no task directory; pass --task-dir.")
        return 2

    # Taken BEFORE anything destructive: the strip runs after the sessions are stopped, so a
    # probe at strip time would read a post-kill fleet in a real run and a live fleet in a
    # dry run — and the prediction would stop being the count the real run strips.
    link_state = LinkState(stores.sessions) if args.strip_task_links else None

    print(
        f"🧨 FLEET RESET · root {stores.root}"
        f"{' · dry-run' if args.dry_run else ''}"
        f"{' · strip-task-links' if args.strip_task_links else ' · state-only'}"
    )

    def run():
        # Counted BEFORE the stop: `stop_sessions` removes each entry as it stops its pid, so
        # a count taken afterwards would report 0 → 0 on a successful run.
        sessions_before = len(registry_entries(stores.sessions))
        print("1. sessions — stop before the registry is cleared")
        sess_lines, survivors, kept = stop_sessions(stores, args.dry_run, args.grace)
        print("\n".join(sess_lines))
        sessions_after = kept if args.dry_run else len(registry_entries(stores.sessions))
        print(f"   sessions         {sessions_before} → {sessions_after}")

        print("2. five stores")
        wm_before, wm_after = clear_json_dir(stores.worker_manager, args.dry_run)
        oi_before, oi_after = clear_json_dir(stores.open_items, args.dry_run)
        h_before, h_after = clear_file(stores.holds, count_holds(stores.holds), args.dry_run)
        s_before, s_after = clear_file(stores.snapshot, file_count(stores.snapshot), args.dry_run)
        print(f"   worker-manager   {wm_before} → {wm_after}")
        print(f"   open-items       {oi_before} → {oi_after}")
        print(f"   session-holds    {h_before} → {h_after}")
        print(f"   fleet-snapshot   {s_before} → {s_after}")

        if args.strip_task_links:
            print("3. task-link strip (separate opt-in)")
            print(f"   task dirs        {len(dirs)}")
            candidates, stripped, kept_live = strip_task_links(dirs, args.dry_run, link_state)
            print(
                f"   task files       {candidates} candidate(s)"
                f"{'' if args.dry_run else f' → {stripped} stripped'}"
            )
            if kept_live:
                print(
                    f"   kept             {kept_live} task(s) whose session is not proven dead"
                    " — link preserved"
                )
        else:
            print("3. task-link strip — NOT requested (state-only run; task files untouched)")
        return survivors

    if args.dry_run:
        survivors = run()
    else:
        with Locked(stores.lock):
            survivors = run()

    if survivors:
        print(
            f"⚠️ {len(survivors)} session(s) survived the stop and kept their registry entry: "
            + ", ".join(f"{label} (pid {pid})" for _path, pid, label in survivors)
        )
        return 3
    print(
        "✅ fleet-reset complete"
        + (" (dry-run — nothing changed)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
