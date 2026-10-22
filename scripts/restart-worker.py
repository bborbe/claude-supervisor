#!/usr/bin/env python3
"""`restart-worker.py` — restart ONE stale idle session, worker or manager, registry-guarded.

A manager's standing mandate (`65 Runbooks/Manager Session.md` § Gate triage class A)
covers killing and restarting a live session whose loaded code is stale. This script is
the narrow, allowlistable way to exercise it: one session id in, one pid killed, the
same session resumed — never a broad `kill` rule.

⚠️ The filename says `worker`, and the target set no longer stops there — the same
`worker-` naming that made `/supervisor:worker-restart` an exception to the tier prefix
rule (`docs/session-tiers.md`). A manager is restarted by this script too, in its own
colour, with a re-arm note; the name is kept because it is fixed by the shipped
`commands/worker-restart.md`, `docs/restart-worker.md` and their allowlist rules.

Seven refusals, each exit 1 with a stable reason token as the first line of output:

  unknown-session-id  no registry entry carries that session id. The message says
                      whether the id was absent or the registry itself was unreadable
                      — the two are the same class (the id cannot be resolved) but
                      never the same sentence.
  ambiguous-pid       more than one registry pid claims that session id, or the entry
                      carries no integer pid. The pid must be unique or nothing is
                      killed.
  busy-target         the target is mid-turn (`status` is not `idle`). A mid-turn kill
                      loses the in-flight work.
  headless-target     the target is not `interactive`. A headless worker has no reliable
                      resume guard, so its liveness stays undetermined.
  held-session        the operator holds this session (`/supervisor:hold`). Only they
                      release it, and restarting is exactly the act the hold prevents.
  role-undetermined   no role signal could be read. Fail closed; never assume worker.
  stale-load-path     nothing would load differently: no load-path copy, no MCP config
                      file and no launcher script is newer than the session's own
                      start, so restarting it buys nothing.

Exit codes: 0 success, 1 refusal, 2 usage error.

⚠️ **A manager IS restarted — there is no `manager-target` refusal.** The role predicate
still runs, but it now selects the resume colour and the re-arm note rather than a
refusal: a manager comes back in the manager colour and is told to re-arm its loop, a
worker in its own chip and told what changed underneath it. The caller decides whether
to type that note at all — a gate the target holds is never answered by a restart
(`commands/restart.md` Step 4).

The restart also reports which tree the session's supervisor MCP server LAUNCHES from
and warns when that tree's version differs from the installed plugin's — a restart onto
a stale workspace checkout reloads the old server code and reads as a no-op.

The role predicate is NOT reimplemented here. It calls `fleet-board.py`'s own
`vault_index()`, `loop_slugs()`, `manager_subject()` and `colour_census()` through the
same importlib sibling-import the repo already uses, so this script and the fleet board
can never disagree about who is a manager. All four of `build_grouping()` Rule 1's
signals are honoured — the root name, `manager_subject()`'s two sources, and the orange
colour — because a predicate covering only the two page-based signals would classify the
root manager as a worker and restart it.

The resume recipe is a copy of `commands/open.md` Step 3.1's. A markdown command is not
executable, so the two are kept in lockstep by hand; that is the second place this
mechanism lives, and the reason `commands/worker-restart.md` is scoped to call THIS
script rather than reimplement kill+resume.
"""
import argparse
import datetime
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The registry. `SUPERVISOR_SESSIONS_DIR` first so a drill can point at a fixture tree;
# never a hardcoded home path in committed content.
SESSIONS_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSIONS_DIR") or "~/.claude/sessions"
)

# The plugin's load path — the copy that is actually executed. Deliberately NOT the
# marketplace clone, which updates independently of what a running session loaded.
# `SUPERVISOR_LOAD_PATH` first, for the same reason as the registry above: a drill or a
# test must be able to point the precondition-4 check at a tree it controls.
LOAD_PATH_ROOT = os.path.expanduser(
    os.environ.get("SUPERVISOR_LOAD_PATH")
    or "~/.claude/plugins/cache/claude-supervisor/supervisor"
)

PLUGIN = "supervisor@claude-supervisor"


def load_sibling(filename):
    """Import a sibling script by path — the repo's own convention for sharing code."""
    name = os.path.basename(filename).replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def registry_entries(directory=None):
    """Every registry record as `(path, rec)`. `None` when the directory is unreadable.

    `None` is a distinct answer from `[]`: an unreadable registry cannot prove the id
    is absent, and reporting an empty list would turn a failed probe into a confident
    "no such session".
    """
    d = directory or SESSIONS_DIR
    # Read through the plugin's single reader rather than globbing the directory here. This used
    # to open it directly; a second raw read of one directory is precisely the class of defect
    # `session-identity.py`'s own header records.
    #
    # ⚠️ **The RAW record, not the reader's normalised one.** This path's consumers read
    # `sessionId`, `startedAt`/`procStart`, `kind` and `name`, and the normalised shape carries
    # none of those — the `kind` check is what refuses a headless worker. Taking the normalised
    # record here would drop that check silently, in the one path whose entire job is not to
    # resume onto a live session. The reader therefore carries the record it read, verbatim.
    # ⚠️ The **list** accessor, not `read_registry()`. The dict is keyed by session id, so two
    # files claiming one id collapse into a single entry — and the collision is what this path
    # refuses on. Resuming onto the wrong claimant of two is the double-writer the guard exists
    # to prevent, so taking the dict here would silently delete the guard.
    return load_sibling("session-identity.py").read_registry_entries(d)


def refuse(reason, detail):
    """Print the reason token, then the sentence. Returns exit code 1."""
    print(reason)
    print(f"❌ {reason}: {detail}")
    return 1


def find_target(entries, sid):
    """`(path, rec)`, or `('ambiguous', matches)`, or `(None, None)` when absent."""
    matches = [(p, r) for p, r in entries if r.get("sessionId") == sid]
    if not matches:
        return None, None
    if len(matches) > 1:
        return "ambiguous", matches
    return matches[0][0], matches[0][1]


def role_of(sid, name, fb, index, slugs, colours):
    """`'manager'`, `'worker'`, or `None` when no signal could be read.

    Mirrors `build_grouping()` Rule 1 exactly. `None` is the fail-closed answer and is
    deliberately distinct from `'worker'`: an unreadable index proves nothing.
    """
    if not name:
        return None
    # Signal 1 — the root. There is no `Fleet Manager` page, so no other signal catches
    # it, and missing it would restart the root manager.
    if name == fb.ROOT_NAME:
        return "manager"
    # Signals 2 + 3 — manager_subject()'s two sources. An unreadable index is not the
    # same answer as "not a manager".
    if index is None:
        return None
    if fb.manager_subject(name, index, slugs):
        return "manager"
    # Signal 4 — colour. An empty census is "not recorded", never "no managers"; it
    # costs the colour half of the rule and leaves the page half standing.
    if (colours.get(sid) or "").strip().lower() == fb.MANAGER_COLOUR:
        return "manager"
    return "worker"


def _version_key(name):
    """Sort key for a version dir name, numeric so `0.52.0` sorts above `0.51.2`.

    A non-numeric segment sorts below any numeric one, so a stray directory cannot
    win the comparison by accident. Segments of the same kind always compare as the
    same type, so no comparison can raise.
    """
    return [(1, int(p)) if p.isdigit() else (0, p) for p in name.split(".")]


def newest_load_path():
    """`(path, mtime)` for the newest load-path version dir, or `(None, None)`.

    Selected by VERSION, never by directory mtime. Two versions installed in one
    operation get near-identical mtimes and the tie falls either way — measured
    2026-09-24: `0.51.2` and `0.52.0` differed by 3ms with the **older** one later,
    so an mtime-max picked `0.51.2` and precondition 4 then compared the session
    against the wrong copy, refusing a restart that should have been allowed. That
    is the check failing in the direction that hides a real fix.

    The version is the authority for *which copy is newest*; that directory's own
    mtime is still what answers *was it installed after this session started*.
    """
    candidates = []
    for path in glob.glob(os.path.join(LOAD_PATH_ROOT, "*")):
        if not os.path.isdir(path):
            continue
        try:
            candidates.append(
                (_version_key(os.path.basename(path)), path, os.path.getmtime(path))
            )
        except OSError:
            continue
    if not candidates:
        return None, None
    _, best, mtime = max(candidates, key=lambda candidate: candidate[0])
    return best, mtime


# The supervisor MCP server's entry point, as the launch config names it. Matching on the
# basename keeps this independent of which checkout a config points at.
MCP_ENTRY = "mcp-start.py"


def plugin_version(root):
    """The `version` in `<root>/.claude-plugin/plugin.json`, or `None` when unreadable.

    A plain file read — deliberately not `git describe` or any `git -C`: the question is
    what the tree on disk says it is, and a checkout mid-pull has no useful git answer.
    """
    try:
        with open(
            os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8"
        ) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return str(version).strip() or None if version else None


def mcp_source(argv):
    """`(source_root, source_version, installed_version)` for the supervisor MCP server.

    A restart refreshes a session's MCP server only if the tree that server LAUNCHES from
    is current. `~/.claude/mcp-<vault>.json` may point the `supervisor` server at a
    workspace checkout (`~/Documents/workspaces/claude-supervisor/scripts/mcp-start.py`)
    rather than the plugin cache, and a checkout lags the release until someone pulls it —
    so a restart onto a stale checkout brings back the same server code and reads as a
    no-op. Measured 2026-10-17: `spawn_agent` still lacked `mounts`/`secrets` after a
    fresh process, because the launched checkout was behind.

    The source comes from the session's own `--mcp-config` argv; both versions are read
    live — the checkout's `.claude-plugin/plugin.json` against the newest installed copy
    under `LOAD_PATH_ROOT` — never hardcoded, so a printed version is evidence rather
    than a claim. `source_root` is `None` when no config, no supervisor entry or no
    readable entry point is found: an unknown source must never be reported as a
    matching one, which is also why the warning is keyed on the two versions being
    known AND equal.
    """
    load_path, _ = newest_load_path()
    installed = os.path.basename(load_path.rstrip(os.sep)) if load_path else None
    for path in mcp_config_paths(argv):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            continue
        for config in servers.values():
            if not isinstance(config, dict):
                continue
            tokens = [str(config.get("command") or "")]
            tokens += [str(arg) for arg in (config.get("args") or [])]
            for token in tokens:
                if not token.endswith(MCP_ENTRY):
                    continue
                entry = os.path.abspath(os.path.expanduser(token))
                root = os.path.dirname(os.path.dirname(entry))
                return root, plugin_version(root), installed
    return None, None, installed


def describe_source(source_root, source_version, installed_version):
    """`[(line, is_warning)]` for the MCP-source report — one line, plus a warning when stale.

    The warning is emitted only when BOTH versions are known and differ. A source whose
    version cannot be read is reported as unreadable, never as current: the failure this
    report exists to catch is a restart that reloads stale server code while reading as a
    success, so an unreadable version must not be silent.
    """
    if source_root is None:
        return [("   MCP source: unknown (no supervisor entry in the session's --mcp-config)", False)]
    shown = source_version or "unreadable"
    line = f"   MCP source: {source_root} ({shown}) · installed {installed_version or 'unknown'}"
    if source_version and installed_version and source_version != installed_version:
        return [
            (line, False),
            (
                f"   ⚠️ MCP source version {source_version} ≠ installed {installed_version} — "
                f"a restart reloads {source_root}; pull it or the server code stays stale",
                True,
            ),
        ]
    return [(line, False)]


def worker_argv(pid):
    """The live worker's argv as one string, or `None` when it cannot be read.

    The registry carries no argv, so the `--mcp-config` path the session was launched
    with is only recoverable from the running process itself — read here, before the
    kill, because afterwards there is nothing left to read. `SUPERVISOR_WORKER_ARGV`
    overrides the read, for the same reason `SUPERVISOR_LOAD_PATH` does: a drill or a
    test must not depend on a real process.
    """
    override = os.environ.get("SUPERVISOR_WORKER_ARGV")
    if override is not None:
        return override
    try:
        out = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def mcp_config_paths(argv):
    """Every `--mcp-config` value in `argv`, in both the `=` and the spaced form.

    `ps` prints argv unquoted, so a path containing a space splits apart here. That
    fails closed: the fragment stats as missing and contributes nothing, leaving the
    other inputs to decide.
    """
    if not argv:
        return []
    tokens = argv.split()
    paths = []
    for i, tok in enumerate(tokens):
        if tok.startswith("--mcp-config="):
            paths.append(tok.split("=", 1)[1])
        elif tok == "--mcp-config" and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
            paths.append(tokens[i + 1])
    return [os.path.expanduser(p) for p in paths if p]


# A launcher value comes from task/goal frontmatter, which supervised workers can write,
# and it is exec'd inside `bash -lc` — so anything but a plain path is refused, never
# quoted. Same shape `server/spawn-cwd.mjs` `SAFE_LAUNCHER` enforces on the spawn path.
SAFE_LAUNCHER = re.compile(r"^[A-Za-z0-9._/-]+$")


def vault_configs():
    """`[{name, path, claude_script}]` from vault-cli config, or `None` when unreadable.

    `SUPERVISOR_VAULT_CONFIG` names a JSON file holding the same list, for the same
    reason `SUPERVISOR_LOAD_PATH` exists: a drill or a test must not read the real config.
    """
    override = os.environ.get("SUPERVISOR_VAULT_CONFIG")
    try:
        if override:
            with open(os.path.expanduser(override), encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            out = subprocess.run(
                ["vault-cli", "config", "list", "--output", "json"],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode != 0:
                return None
            data = json.loads(out.stdout)
    except Exception:
        return None
    return data if isinstance(data, list) else None


def _launcher_field(path):
    """The `launcher:` frontmatter value of one page, or `''`."""
    # Read whole: a long-lived task's `metrics_sessions` can push the closing `---` past
    # any fixed head, and a truncated read would drop `launcher:` without a word.
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read()
    except OSError:
        return ""
    if not head.startswith("---"):
        return ""
    end = head.find("\n---", 3)
    fm = head[3:end] if end != -1 else ""
    match = re.search(r"^launcher:[ \t]*['\"]?([^'\"\n]*?)['\"]?[ \t]*$", fm, re.M)
    return match.group(1).strip() if match else ""


def resolve_launcher(sid, cwd, fb, subject=None):
    """`(launcher_path, source)` the session must resume through, or `(None, reason)`.

    The resume must run the worker's OWN launcher, not the caller's. A launcher script
    exports what the session needs before it execs `claude` — the router base URL, the
    model, the `--mcp-config` file — so resuming through bare `claude` brings the session
    back on a reduced MCP surface and the wrong backend. Measured 2026-10-10: a manager
    with no `CLAUDE_SCRIPT` restarted a worker through bare `claude`, and the resumed
    process came back without `--mcp-config` and without its `a2a` server.

    Precedence mirrors the spawn path (`server/spawn-cwd.mjs` `resolveTaskLauncher`): the
    task bound to this session (`claude_session_id`) names a `launcher:`, else the vault's
    configured WORKER default (`worker_launcher`, reported as `worker-default`), else the
    vault's `claude_script`. The vault is the one whose path holds the session's cwd. A bare
    name resolves beside the vault launcher. Anything that cannot be resolved to an existing
    file answers `None` — the caller refuses before the kill.
    `SUPERVISOR_LAUNCHER` overrides the whole resolution, for drills and tests.

    ⚠️ A GOAL's `launcher:` is NOT consulted, and dropping it is the fix this carries. A goal
    states how its MANAGER runs; inheriting it here resumed every worker under such a goal on
    its manager's Opus launcher (measured 2026-10-22: 12 private-personal goals). Goal
    frontmatter is now inert for workers on both the spawn and the resume path.

    A MANAGER (`subject` = `(kind, title)` from `manager_subject()`) takes the manager rule
    instead, mirroring `resolveManagerLauncher` in `server/spawn-cwd.mjs`: the subject page's
    `manager_launcher:`, else the vault's configured `manager_launcher` (`manager-default`),
    else the `-claude` sibling for a vault-cli too old to emit that key, else `claude_script`.
    """
    override = os.environ.get("SUPERVISOR_LAUNCHER")
    if override:
        return override, "override"
    configs = vault_configs()
    if configs is None:
        return None, "vault-cli config could not be read"
    real_cwd = os.path.realpath(cwd) if cwd else ""
    best = None
    for vault in configs:
        # Guard each element, inside the loop: a payload that parses to a list holding a
        # non-dict must refuse cleanly, never raise — `resolve-task-file.py` learned this.
        if not isinstance(vault, dict):
            continue
        # Guard the RAW path: `realpath("")` is the process cwd, never `""`, so an entry
        # with no path would otherwise claim this script's own cwd as a vault root.
        raw = str(vault.get("path") or "").strip()
        if not raw:
            continue
        root = os.path.realpath(os.path.expanduser(raw))
        if root and (real_cwd == root or real_cwd.startswith(root + os.sep)):
            if best is None or len(root) > len(best[0]):
                best = (root, vault)
    if best is None:
        return None, f"cwd {cwd or '(none)'} is under no configured vault"
    root, vault = best
    vault_launcher = str(vault.get("claude_script") or "").strip()
    if not vault_launcher:
        return None, f"vault {vault.get('name')!r} has no claude_script"

    if subject is not None:
        kind, title = subject
        subdirs = fb.TOPIC_SUBDIRS if kind == "topic" else fb.GOAL_SUBDIRS
        page = next((p for p in fb._pages(root, subdirs)
                     if os.path.basename(p)[:-3].lower() == str(title).strip().lower()), None)
        manager_default = str(vault.get("manager_launcher") or "").strip() or vault_launcher
        return load_sibling("manager-launcher.py").resolve(manager_default, page)

    # The task bound to this session, matched on the row's WHOLE id set —
    # `claude_session_id` plus every `metrics_sessions[].session_id`, read through
    # `manager-predispatch.py`'s `session_id_set`, never re-derived. Many tasks carry the
    # id only in `metrics_sessions`, and a single-field match would drop their `launcher:`.
    session_id_set = load_sibling("manager-predispatch.py").session_id_set
    claimants = []
    for path in fb._pages(root, fb.TASK_SUBDIRS):
        text = fb._read(path)
        if sid not in text:
            continue
        fm = fb.frontmatter(text)
        if sid in session_id_set(fm):
            claimants.append((path, fm))
    if len(claimants) > 1:
        names = ", ".join(os.path.basename(p)[:-3] for p, _ in claimants)
        return None, f"{len(claimants)} task pages claim session {sid} ({names})"

    # Only the task's OWN `launcher:` is read. Its goals are deliberately not consulted: a
    # goal's `launcher:` describes its manager, and inheriting it resumed every worker under
    # such a goal on the manager's Opus launcher (measured 2026-10-22, 12 private-personal
    # goals). The worker default comes from the vault config instead.
    own = ""
    if claimants:
        own = _launcher_field(claimants[0][0])

    worker_default = str(vault.get("worker_launcher") or "").strip()
    for value in [own, worker_default]:
        if value and (not SAFE_LAUNCHER.match(value) or ".." in value.split("/")):
            return None, f"launcher {value!r} is not a plain script name or path"
    if own:
        name, source = own, "task"
    elif worker_default:
        name, source = worker_default, "worker-default"
    else:
        name, source = vault_launcher, "vault"
    if "/" not in name:
        name = os.path.join(os.path.dirname(vault_launcher), name)
    name = os.path.expanduser(name)
    if not os.path.isfile(name):
        return None, f"launcher {name} ({source}) does not exist"
    return name, source


def changed_inputs(load_path, load_mtime, argv, launcher, started):
    """`[(label, path)]` for every input newer than `started`.

    Three inputs decide whether a restart would load anything differently: the plugin
    load path, the MCP config file(s) the worker was launched with, and the launcher
    script. An input that cannot be read contributes nothing — it can never turn a
    refusal into an accept, so an unreadable argv falls back to the load-path check.
    """
    changed = []
    if load_path is not None and load_mtime > started:
        changed.append(("plugin version", os.path.basename(load_path)))
    for path in mcp_config_paths(argv):
        try:
            if os.path.getmtime(path) > started:
                changed.append(("MCP config", path))
        except OSError:
            continue
    if launcher:
        try:
            if os.path.getmtime(launcher) > started:
                changed.append(("launcher", launcher))
        except OSError:
            pass
    return changed


def describe_changed(changed):
    """`plugin version (0.1.0); MCP config (/p)` — one line naming every changed input."""
    return "; ".join(f"{label} ({what})" for label, what in changed)


def parse_started(rec):
    """The session's start time as an epoch float, or `None` when unparseable.

    `startedAt` is **milliseconds** since the epoch in the live registry — measured
    2026-09-24: `1790269714645` — while an ISO string is seconds. A bare `float(raw)`
    therefore reads a millisecond value as a date some 57,000 years out, and every
    comparison against a filesystem mtime then fails: precondition 4 refuses *every*
    restart unconditionally, which is the check failing in the direction that hides
    real fixes. The unit is detected by magnitude — anything past `1e11` is
    milliseconds, a figure seconds-since-epoch does not reach this century.
    """
    raw = rec.get("startedAt") or rec.get("procStart")
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            value = float(raw)
            return value / 1000.0 if value > 1e11 else value
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def resume_command(sid, title, chip, cwd, launcher):
    """The resume recipe, copied from `commands/open.md` Step 3.1.

    A markdown command is not executable, so this is a deliberate copy — see the module
    docstring. The `unset` list matters: a resumed session that inherits the parent's
    messaging socket believes it is the parent.

    `--cwd` is in the recipe as of 2026-09-24 and must not be dropped from either copy.
    `wezterm cli spawn` otherwise inherits wezterm's own working directory — for a
    server started from a home directory, `$HOME` — and Claude Code then stops on
    *"Accessing workspace /Users/<user> — do you trust this folder?"* before it ever
    registers a pid. Measured 2026-09-24: a restart of a session whose real cwd was
    `~/Documents/Obsidian/my-vault` resumed into `$HOME`, stalled on that dialog and
    produced no registry entry, so a working kill+resume looked like a no-op. The
    cwd comes from the registry record, read before the kill.

    ⚠️ **The colour chip is NOT a byte-for-byte copy, and the difference is load-bearing.**
    `commands/open.md` Step 3.1 writes `"/color '"$CHIP"'"` — those single quotes are *shell*
    syntax that breaks out of the outer `bash -lc '…'` single-quoted string so `$CHIP` expands.
    That string is built in Python here, where there is no outer quoting to break out of, so a
    copied `\\'` becomes a **literal** quote in the value and Claude Code rejects it:
    `Invalid color "'pink'"`. Measured 2026-09-25 on the first live run of
    `/supervisor:worker-restart`: both restarts came up with the rejected chip and kept the
    default colour instead of pink. It matters because the colour is the fleet's only role cue
    (`open.md`: *"The colour is the only fleet-wide cue for which role a session plays"*) — a
    restarted worker that should be pink is exactly the mis-coloured session that nearly got a
    manager relaunched as a worker on 2026-09-18. The quotes are dropped here, deliberately.
    """
    script = launcher
    inner = (
        "unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN "
        "CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; "
        f'exec "{script}" --resume {sid} -n "{title}" "/color {chip}"'
    )
    argv = ["wezterm", "cli", "spawn"]
    if cwd:
        argv += ["--cwd", cwd]
    return argv + ["--", "bash", "-lc", inner]


HOLDS_PATH = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSION_HOLDS", "~/.claude/state/session-holds.json")
)


def read_holds():
    """The operator's session holds, keyed on session id. `{}` on any read failure.

    Inlined rather than shelling out to session-holds.py: the plugin's scripts do not
    import each other, and a subprocess dependency on a plugin path is a fail-open.
    Reads are lock-free because the writer lands every change through `os.replace`.

    A missing or corrupt store reads as *nothing held*. That direction is the safe one
    here: inventing a hold would refuse a restart the operator asked for, while reading
    a real store as empty merely fails to honour a hold -- and this module's whole design
    is that every refusal is named rather than silent.
    """
    try:
        with open(HOLDS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    holds = data.get("holds") if isinstance(data, dict) else None
    return holds if isinstance(holds, dict) else {}


def held_reason(session_id):
    """The hold's reason for this session, or None when it is not held."""
    if not session_id:
        return None
    entry = read_holds().get(session_id)
    if not isinstance(entry, dict):
        return None
    return entry.get("reason") or "held"


def main():
    ap = argparse.ArgumentParser(
        description="Restart one stale idle session — worker or manager — registry-guarded.",
    )
    ap.add_argument("session_id", help="the session id to restart")
    ap.add_argument("--dry-run", action="store_true", help="check everything, kill nothing")
    ap.add_argument("--sessions-dir", help=f"registry dir (default: {SESSIONS_DIR})")
    args = ap.parse_args()

    sid = args.session_id.strip()
    if not sid:
        ap.error("session id must not be empty")  # exits 2

    fb = load_sibling("fleet-board.py")

    entries = registry_entries(args.sessions_dir)
    if entries is None:
        return refuse(
            "unknown-session-id",
            f"the registry at {args.sessions_dir or SESSIONS_DIR} could not be read, "
            f"so {sid} cannot be resolved",
        )

    path, rec = find_target(entries, sid)
    if path == "ambiguous":
        pids = [os.path.basename(p) for p, _ in rec]
        return refuse(
            "ambiguous-pid",
            f"{len(rec)} registry entries claim session {sid} ({', '.join(pids)}); "
            "the pid must be unique or nothing is killed",
        )
    if path is None:
        return refuse("unknown-session-id", f"no registry entry carries session id {sid}")

    # ⚠️ A held session is never restarted. Checked BEFORE the status and kind gates
    # because a hold is the stronger statement: `busy` says the moment is wrong, a hold says the session
    # is not yours to touch at all, and it outranks every other consideration here.
    # Restarting is exactly the act the operator held the session to prevent.
    held = held_reason(sid)
    if held is not None:
        return refuse(
            "held-session",
            f"session {sid} carries an operator hold ({held}); only the operator "
            "releases it -- see the /supervisor:hold skill",
        )

    pid = rec.get("pid")
    if not isinstance(pid, int):
        return refuse("ambiguous-pid", f"registry entry {path} carries no integer pid")

    status = (rec.get("status") or "").strip().lower()
    if status != "idle":
        return refuse(
            "busy-target",
            f"session {sid} is {status or 'in an unknown state'}, not idle; "
            "a mid-turn kill loses the in-flight work",
        )

    kind = (rec.get("kind") or "").strip().lower()
    if kind != "interactive":
        return refuse(
            "headless-target",
            f"session {sid} is {kind or 'of unknown kind'}; a headless worker has no "
            "reliable resume guard, so liveness stays undetermined",
        )

    # Role — fail closed. An unreadable index or census is `role-undetermined`, never
    # `worker`.
    try:
        index = fb.vault_index()
    except Exception:
        index = None
    slugs = fb.loop_slugs()
    colours = fb.colour_census()
    name = (rec.get("name") or "").strip()

    role = role_of(sid, name, fb, index, slugs, colours)
    if role is None:
        return refuse(
            "role-undetermined",
            f"no role signal could be read for session {sid} (name {name!r}); "
            "refusing rather than assuming worker",
        )
    # A manager is restarted like a worker: the role is not a gate, it selects the resume
    # colour and the re-arm note below. It used to refuse here (`manager-target`);
    # `role-undetermined` is now the only role-based stop, because a role that cannot be
    # read still cannot be defaulted.

    # Precondition 4 — something must actually load differently. Three inputs are
    # compared against the session's own start: the newest load-path copy, the MCP
    # config file(s) in the worker's live argv, and the launcher script. Only when ALL
    # are no newer would the restart reload identical code and env. The argv is read
    # here, before the kill, because afterwards there is no process left to read.
    load_path, load_mtime = newest_load_path()
    started = parse_started(rec)
    if started is None:
        return refuse(
            "stale-load-path",
            f"session {sid} carries no parseable start time, so it cannot be shown that "
            "anything would load differently",
        )
    argv = worker_argv(pid)
    subject = fb.manager_subject(name, index, slugs) if role == "manager" and index is not None else None
    launcher, launcher_source = resolve_launcher(sid, (rec.get("cwd") or "").strip(), fb, subject)
    changed = changed_inputs(load_path, load_mtime, argv, launcher, started)
    if not changed:
        checked = [
            f"load path {os.path.basename(load_path)}" if load_path
            else f"no load path under {LOAD_PATH_ROOT}",
            "MCP config (unreadable argv)" if argv is None
            else ", ".join(mcp_config_paths(argv)) or "no --mcp-config",
            launcher or f"launcher unresolved: {launcher_source}",
        ]
        return refuse(
            "stale-load-path",
            f"none of plugin version, MCP config or launcher is newer than session "
            f"{sid}'s start ({'; '.join(checked)}); restarting would reload identical "
            "code and config",
        )

    print(
        f"🔄 restart-worker · session {sid[:8]} · pid {pid} · {name!r}"
        f"{' · dry-run' if args.dry_run else ''}"
    )
    print(
        f"   role: {role} · status: {status} · load path: "
        f"{os.path.basename(load_path) if load_path else 'none'}"
    )
    print(f"   changed: {describe_changed(changed)}")
    for line, _ in describe_source(*mcp_source(argv)):
        print(line)

    # The cwd the session must resume INTO. Read here, before the kill: a session
    # resumed without its own cwd lands in wezterm's default working directory and
    # stalls on Claude Code's workspace-trust dialog, registering no pid at all — so
    # this is checked BEFORE signalling, never after. An unresumable session must not
    # be killed, which is the whole reason the guard sits above `os.kill`.
    cwd = (rec.get("cwd") or "").strip()

    # The launcher the session resumes THROUGH — resolved above, checked here, before the
    # kill: a session resumed through the wrong launcher comes back on a reduced MCP
    # surface, so an unresolvable launcher is refused exactly like a missing cwd.
    if launcher is None:
        print(
            f"❌ error: no launcher could be resolved for {sid} ({launcher_source}); "
            "resuming through bare `claude` drops its MCP config and env — refusing before the kill"
        )
        return 1
    print(f"   launcher: {launcher} ({launcher_source})")

    if args.dry_run:
        print("   ✅ would kill the pid above and resume the same session id")
        if cwd:
            print(f"   ↪ would resume into {cwd}")
        return 0

    if not cwd:
        print(
            f"❌ error: registry entry for {sid} carries no cwd, and resuming without "
            "one strands the session on a workspace-trust dialog — refusing before the kill"
        )
        return 1

    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        print(f"   ℹ️ pid {pid} was already gone")
    except PermissionError:
        # An operational failure, NOT one of the seven target refusals: the pid
        # resolved and the session id is known, the caller simply may not signal it.
        # Reported under an `error:` prefix so it can never be read as a refusal
        # token — `unknown-session-id` here would send the operator hunting for a
        # typo in an id that was in fact found.
        print(
            f"❌ error: pid {pid} resolved but cannot be signalled (permission denied) "
            "— this is an operational failure, not a target refusal"
        )
        return 1

    title = name or sid[:8]
    # The fallback chip is role-aware. A manager's own colour is the fleet's only role
    # cue, so a restarted manager coming back pink is exactly the mis-colouring that
    # nearly got a manager relaunched as a worker on 2026-09-18.
    fallback_chip = fb.MANAGER_COLOUR if role == "manager" else "pink"
    chip = (colours.get(sid) or fallback_chip).strip().lower() or fallback_chip
    # Both resume-failure paths below return 1 via `refuse()`. There is deliberately
    # no fall-through to the success `return 0` at the end of main(): a killed pid
    # whose session was NOT resumed must never read as a successful restart, because
    # the caller branches on this exit code to decide whether the worker is back.
    try:
        spawned = subprocess.run(
            resume_command(sid, title, chip, cwd, launcher),
            capture_output=True, text=True, timeout=30,
        )
    except Exception as error:
        return refuse("unknown-session-id", f"resume failed: {error}")
    if spawned.returncode != 0:
        return refuse(
            "unknown-session-id",
            f"resume failed (exit {spawned.returncode}): {(spawned.stderr or '').strip()}",
        )

    pane = (spawned.stdout or "").strip().splitlines()
    print(
        f"   ✅ killed pid {pid}; resumed session {sid} in a new tab "
        f"(pane {pane[-1] if pane else '?'})"
    )
    if role == "manager":
        print(
            "   ↪ manager: tell the resumed session it was restarted and must RE-ARM its "
            "loop (`/supervisor:goal-loop <subject>` or `/supervisor:topic-loop <subject>`, "
            "or `/supervisor:fleet-loop`) — a "
            f"killed session's scheduled jobs die with it. Changed: {describe_changed(changed)}"
        )
    else:
        print(
            f"   ↪ tell the resumed session it was restarted, and what changed: {describe_changed(changed)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
