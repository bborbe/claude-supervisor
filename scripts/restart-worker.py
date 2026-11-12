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
                      loses the in-flight work. `--allow-busy-lapse` lifts this ONE refusal
                      and nothing else — see the flag's own note.
  headless-target     the target is not `interactive`. A headless worker has no reliable
                      resume guard, so its liveness stays undetermined.
  held-session        the operator holds this session (`/supervisor:hold`). Only they
                      release it, and restarting is exactly the act the hold prevents.
  role-undetermined   no role signal could be read. Fail closed; never assume worker.
  stale-load-path     nothing would load differently: no load-path copy, no MCP config
                      file and no launcher script is newer than the session's own
                      start, so restarting it buys nothing.

Exit codes: 0 success, 1 refusal, 2 usage error, 3 environment fault.

An environment fault is NOT a refusal. `environment-fault` (exit 3) means this script's
own environment is broken — today, `vault-cli` cannot be found, so no launcher can be
resolved for ANY session. A refusal is a decision about one target; an environment fault
is a defect in the caller, and it must never print as a refusal. Measured 2026-11-01: a
launchd agent whose `PATH` lacked `vault-cli` printed a launcher refusal on ~1000 ticks,
so an agent that had never worked read as a guard correctly declining.

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

⚠️ **The launcher is not part of that copy.** `resolve_launcher` is the single home of the
rule, and `commands/open.md` § 3.1 calls `scripts/resolve-launcher.py` — a thin CLI over it
that also prints the chip — rather than restating the precedence in prose. Prose there let
whichever session ran the resume pick the launcher: measured 2026-11-07, two `role: agent`
rows came back as `cc-private-claude --resume … /color cyan` because their resumer ran on
Opus.
"""
import argparse
import datetime
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# The exit-record writer, which sits beside this script. Passed to the spawned pane as an
# absolute path through `env`, because the pane cannot resolve `$CLAUDE_PLUGIN_ROOT` for a
# script this file already knows the location of.
RECORD_SCRIPT = os.path.join(HERE, "session-exit-record.py")

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


# Where `vault-cli` is installed when the caller's `PATH` does not carry it. launchd gives a
# job no login shell, so a bare-name lookup is only as good as the plist's `PATH`; resolve
# the lookup here instead of depending on it (`go install` → GOPATH/bin, Homebrew).
VAULT_CLI_LOCATIONS = (
    "~/Documents/workspaces/go/bin/vault-cli",
    "~/go/bin/vault-cli",
    "/opt/homebrew/bin/vault-cli",
    "/usr/local/bin/vault-cli",
)

ENV_FAULT = "environment-fault"


def find_vault_cli():
    """Absolute path of an executable `vault-cli`, or `None` when none exists.

    `SUPERVISOR_VAULT_CLI` overrides the search (tests, drills). Otherwise `PATH`, then
    `$GOPATH/bin`, then `VAULT_CLI_LOCATIONS`.
    """
    override = os.environ.get("SUPERVISOR_VAULT_CLI")
    if override is not None:
        path = os.path.expanduser(override)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    found = shutil.which("vault-cli")
    if found:
        return os.path.abspath(found)
    candidates = []
    gopath = os.environ.get("GOPATH")
    if gopath:
        candidates.append(os.path.join(gopath, "bin", "vault-cli"))
    candidates.extend(VAULT_CLI_LOCATIONS)
    for cand in candidates:
        path = os.path.expanduser(cand)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


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
            vault_cli = find_vault_cli()
            if vault_cli is None:
                return None
            out = subprocess.run(
                [vault_cli, "config", "list", "--output", "json"],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode != 0:
                return None
            data = json.loads(out.stdout)
    except Exception:
        return None
    return data if isinstance(data, list) else None


def _page_field(path, key):
    """One frontmatter scalar from a vault page, or `''` when absent or unreadable."""
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
    match = re.search(rf"^{re.escape(key)}:[ \t]*['\"]?([^'\"\n]*?)['\"]?[ \t]*$", fm, re.M)
    return match.group(1).strip() if match else ""


def _launcher_field(path):
    """The `launcher:` frontmatter value of one page, or `''`."""
    return _page_field(path, "launcher")


def _role_field(path):
    """The `role:` frontmatter value of one page, or `''` when it carries none.

    `''` is not `agent`: the matrix's Rule 1 default belongs to the caller, and this
    helper reporting `agent` for a page that says nothing would make "absent" and
    "declared agent" indistinguishable one layer up.
    """
    return _page_field(path, "role")


def vault_for_cwd(cwd, configs):
    """`(root, vault_entry)` for the configured vault whose path holds `cwd`, else None.

    Longest root wins: a vault nested inside another must not be shadowed by its parent.
    """
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
    return best


def session_claimant(sid, root, fb):
    """`(claimant, error)` — the task page claiming `sid`, or `(None, None)` for none.

    `claimant` is `(path, frontmatter)`. Matched on the row's WHOLE id set —
    `claude_session_id` plus every `metrics_sessions[].session_id`, read through
    `manager-predispatch.py`'s `session_id_set`, never re-derived. Many tasks carry the
    id only in `metrics_sessions`, and a single-field match would drop their `launcher:`.

    Two claimants is an error, not a first-match: picking one would resume the session
    under a launcher chosen by page ordering.
    """
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
    return (claimants[0] if claimants else None), None


def session_subject(sid, root, fb):
    """`(subject, error)` — the goal or topic page claiming `sid`, or `(None, None)`.

    Scanned from the PAGES, never from the live session registry, and that is the whole
    point: the registry is deleted when a session exits, and a manager only reaches the
    resume path once it has exited — so a registry-based lookup answers "not a manager"
    exactly where the resume happens. Caught in review on the first cut of `resume_role`,
    which read the registry and therefore left the bug it claimed to fix in place.
    `commands/open.md` reads the same page field to decide the resume, so the two agree.

    ⚠️ **The id is matched on the page's WHOLE id set**, exactly as `session_claimant`
    does — `claude_session_id` plus every `metrics_sessions[].session_id`. Matching only
    the scalar field drops a page whose manager id was recorded the other way, and the
    asymmetry was caught in review on this function while its sibling already had it right.

    Two pages claiming one id is an error, not a first-match, for the same reason: picking
    one would resume the session under a subject chosen by page ordering.
    """
    session_id_set = load_sibling("manager-predispatch.py").session_id_set
    claims = []
    for kind, subdirs in (("topic", fb.TOPIC_SUBDIRS), ("goal", fb.GOAL_SUBDIRS)):
        for path in fb._pages(root, subdirs):
            text = fb._read(path)
            if sid not in text:
                continue
            if sid in session_id_set(fb.frontmatter(text)):
                claims.append((kind, os.path.basename(path)[:-3]))
    if len(claims) > 1:
        names = ", ".join(f"{k} {t}" for k, t in claims)
        return None, f"{len(claims)} pages claim session {sid} ({names})"
    return (claims[0] if claims else None), None


def resume_role(sid, cwd, fb):
    """`(role, subject, error)` a resume must launch and colour by.

    `role` is `manager`, `human` or `agent`. A MANAGER is checked first: a manager session
    usually carries no task page, so the claimant lookup alone would read it as a worker
    and resume it on the worker default — the same defect the prose rule had, one layer
    down. `subject` is passed straight to `resolve_launcher`.

    ⚠️ **The manager arm is page-keyed, so a manager with no goal or topic page reads as
    `agent` here** — the root manager is the live case. Its LAUNCHER still matches (the
    worker chain resolves the same script), but its chip comes out `pink` rather than
    `orange`. The page is the only source that survives an exited session, which is the
    case this function exists for; the registry could cover the page-less root but only
    while it is live, when no resume is happening.

    An unreadable vault config is not a refusal here: there is no kill to authorise and
    the row still has to open, so it answers `agent` and the chip follows.
    """
    configs = vault_configs()
    best = vault_for_cwd(cwd, configs) if configs is not None else None
    if best is None:
        return "agent", None, None
    root = best[0]
    subject, error = session_subject(sid, root, fb)
    if error:
        return "agent", None, error
    if subject is not None:
        return "manager", subject, None
    claimant, claimant_error = session_claimant(sid, root, fb)
    if claimant_error:
        return "agent", None, claimant_error
    return (_role_field(claimant[0]) if claimant else "") or "agent", None, None


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

    A `role: human` task takes the Claude launcher (`_human_launcher`), mirroring
    `applyHumanLauncherRule` in `server/spawn-cwd.mjs` — the operator's own session is not a
    delegate. Its own `launcher:` still wins.
    """
    override = os.environ.get("SUPERVISOR_LAUNCHER")
    if override:
        return override, "override"
    # A missing `vault-cli` is the caller's environment, not this session's state: report
    # it under its own token so `main` exits 3, never as a launcher refusal.
    if not os.environ.get("SUPERVISOR_VAULT_CONFIG") and find_vault_cli() is None:
        return None, (
            f"{ENV_FAULT}: vault-cli not found on PATH ({os.environ.get('PATH', '')}) "
            f"nor at {', '.join(VAULT_CLI_LOCATIONS)}"
        )
    configs = vault_configs()
    if configs is None:
        return None, "vault-cli config could not be read"
    best = vault_for_cwd(cwd, configs)
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
        # A bare configured name resolves beside the vault's own launcher, exactly as a bare
        # `launcher:` does below — `manager-launcher.py` receives one path and has no directory
        # to resolve against, so the join happens here or the file check fails on a valid value.
        # Provenance travels with it: `manager-default` when the value came from the config key,
        # `vault` when it is `claude_script` and the sibling rule is the one deciding.
        manager_default = str(vault.get("manager_launcher") or "").strip()
        if not manager_default:
            return load_sibling("manager-launcher.py").resolve(vault_launcher, page, "vault")
        if "/" not in manager_default:
            manager_default = os.path.join(os.path.dirname(vault_launcher), manager_default)
        return load_sibling("manager-launcher.py").resolve(manager_default, page, "manager-default")

    claimant, claimant_error = session_claimant(sid, root, fb)
    if claimant_error:
        return None, claimant_error

    # Only the task's OWN `launcher:` is read. Its goals are deliberately not consulted: a
    # goal's `launcher:` describes its manager, and inheriting it resumed every worker under
    # such a goal on the manager's Opus launcher (measured 2026-10-22, 12 private-personal
    # goals). The worker default comes from the vault config instead.
    own = _launcher_field(claimant[0]) if claimant else ""

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

    # A `role: human` row takes the vault's CLAUDE launcher, never the worker default — the
    # operator's own session is not a delegate (operator ruling 2026-11-06, matrix Rule 4).
    # Mirrors `applyHumanLauncherRule` in `server/spawn-cwd.mjs`, including its ordering: the
    # `-claude` sibling sits AHEAD of a configured `manager_launcher`, because private-personal
    # and seibert-brogrammers both point that key at a `-deepseek` script and taking it first
    # would leave a human row on the worker model while the rule reported success. A task's
    # own `launcher:` still wins — it is the operator's explicit choice for that row.
    if claimant and source != "task" and _role_field(claimant[0]) == "human":
        name, source = _human_launcher(vault_launcher, vault)
        if name is None:
            return None, source

    if not os.path.isfile(name):
        return None, f"launcher {name} ({source}) does not exist"
    return name, source


def _human_launcher(vault_launcher, vault):
    """`(name, source)` for a `role: human` row: the `-claude` sibling, else the manager chain.

    Reuses `manager-launcher.py`'s `sibling()` and `resolve()` rather than restating the
    naming convention, so the compatibility rung has one home.

    ⚠️ **The last rung is the vault's `claude_script`, never the worker default.** That is
    what matrix Rule 4 states and what `applyHumanLauncherRule` does (it falls through to
    `resolveManagerLauncher`, whose final arm is `vaultLauncher`). Returning the worker
    resolution here would put a `role: human` row on the `-deepseek` worker launcher in a
    vault that configures `worker_launcher` but has neither a `-claude` sibling nor a
    `manager_launcher` — the very outcome this rule exists to prevent. Caught in review.

    `(None, reason)` when the configured `manager_launcher` is unsafe or missing: the
    reason is `manager-launcher.py`'s own sentence and the caller refuses with it, rather
    than turning it into a second, misleading "does not exist".
    """
    ml = load_sibling("manager-launcher.py")
    sibling = os.path.expanduser(ml.sibling(vault_launcher))
    if sibling == vault_launcher:
        return vault_launcher, "vault"
    if os.path.isfile(sibling):
        return sibling, "claude-sibling"
    manager_default = str(vault.get("manager_launcher") or "").strip()
    if not manager_default:
        return vault_launcher, "vault"
    if "/" not in manager_default:
        manager_default = os.path.join(os.path.dirname(vault_launcher), manager_default)
    return ml.resolve(manager_default, None, "manager-default")


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
    """`plugin version (0.1.0); MCP config (/p)` — one line naming every changed input.

    Empty means a `--reason lapse` restart: the loop stopped, no input changed. Said out loud
    rather than left blank, so the caller cannot read an empty string as a missing report.
    """
    if not changed:
        return "nothing — the loop stopped, no input changed (`--reason lapse`)"
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


def resume_command(sid, title, chip, cwd, launcher, fresh_prompt=None,
                   wezterm="wezterm"):
    """The resume recipe, copied from `commands/open.md` Step 3.1.

    ``fresh_prompt`` set → a NEW session is started through the same launcher, cwd and exit
    trap, with that prompt as its first turn, instead of ``--resume <sid>``. Used by
    `fleet-keeper.py`'s answer-gated `restart fresh` (operator ruling 2026-11-10): a lapsed
    manager whose context is exhausted comes back exhausted on ``--resume``. The colour chip is
    not applied on this path — the launcher takes one initial prompt, and it carries the loop.

    ``wezterm`` must be the RESOLVED binary, never a bare name. Under launchd the keeper's
    `PATH` is `/usr/bin:/bin:/usr/sbin:/sbin`, which does not contain wezterm, so a bare
    `"wezterm"` raises FileNotFoundError there. Measured 2026-11-11: the keeper's
    `restart fresh` killed the Fleet Manager and then could not spawn the replacement,
    leaving the fleet dead for 14 minutes until the separate `--absent` resume path (which
    already used `find_wezterm()`) recovered it.

    A markdown command is not executable, so this is a deliberate copy — see the module
    docstring. The `unset` list matters: a resumed session that inherits the parent's
    messaging socket believes it is the parent. ⚠️ The `env` prefix carries one positive
    assignment beside the writer path — `CLAUDE_CODE_ENTRYPOINT=cli` — because the pane
    inherits the SPAWNER's environment and a headless spawner would otherwise register a
    real tab as `sdk-cli` (measured 2026-11-12; see the comment above `inner`).

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
    # ⚠️ NO `exec`, and the trap is why. `exec` replaces the pane's shell with the launcher,
    # so no process is left to `waitpid` and the session's exit status stops existing — a
    # SIGKILLed worker then leaves no record anywhere, which is the whole gap
    # `session-exit-record.py` closes. Measured 2026-10-27 on this shape: as a child with
    # `trap _sx EXIT`, a `kill -9` records `rc=137`; behind `exec`, the trap cannot fire.
    # The writer path travels as an `env` assignment because the pane cannot resolve the
    # plugin root for a script this file already knows the location of.
    #
    # ⚠️ `CLAUDE_CODE_ENTRYPOINT=cli` is not decoration. `wezterm cli spawn` forwards the
    # CALLER's environment into the new pane (measured 2026-11-12: a spawn from a `cli`
    # session reads `cli` back inside the pane, and the mux server carries no such
    # variable), so a manager resumed by a headless leg comes up registered `sdk-cli`
    # while it has a real pane. The census then charts it `invalid / headless` — a manager
    # cannot be headless — and `closer-gate-lint.py` reads the same variable as its
    # paneless test, so its `Shift+Tab` and `/reload-plugins` closers are refused as
    # unactionable. Measured 2026-11-12 on five live goal managers at once.
    #
    # Set explicitly rather than added to the `unset` list: an absent value leaves Claude
    # Code to derive the entrypoint itself, and this pane is a tab by construction.
    #
    # The observer is started BEFORE the trap and BEFORE the launcher, and it detaches into
    # its own session, so it outlives a kill of this whole process group. It watches `$$`
    # — this shell — because the shell outlives the launcher and dies exactly when the group
    # is killed, which is the case the trap cannot record. Its own `append` call writes the
    # spawn marker first so a record that begins at `kind=spawn` proves the writer was wired.
    trap = (
        '_sx() { rc=$?; "$SESSION_EXIT_RECORD" append --session "'
        + sid
        + '" --pid $$ --rc "$rc" || true; }; "$SESSION_EXIT_RECORD" append --kind spawn '
        '--session "'
        + sid
        + '" --pid $$ || true; "$SESSION_EXIT_RECORD" observe --pid $$ --session "'
        + sid
        + '" || true; trap _sx EXIT; '
    )
    inner = (
        "unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN "
        "CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; "
        + trap
        + (f'"{script}" -n "{title}" "{fresh_prompt}"' if fresh_prompt
           else f'"{script}" --resume {sid} -n "{title}" "/color {chip}"')
    )
    argv = [wezterm, "cli", "spawn"]
    if cwd:
        argv += ["--cwd", cwd]
    return argv + ["--", "env", f"SESSION_EXIT_RECORD={RECORD_SCRIPT}",
                   "CLAUDE_CODE_ENTRYPOINT=cli", "bash", "-lc", inner]


#: The only prompts `--fresh-prompt` accepts. The value lands inside `bash -lc '…'`, so it is
#: an allowlist, never free text.
FRESH_PROMPTS = ("/supervisor:fleet-loop",)


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


# --------------------------------------------------------------------------- #
# the Hold window -- the second source of heldness
# --------------------------------------------------------------------------- #
#
# A pane parked in the WezTerm role map's Hold window is held with no store entry
# at all: the operator's statement is the pane's POSITION, not a `hold` call. It is
# resolved at read time from the map's `Hold.window_id` plus `wezterm cli list`, and
# never persisted -- a stored entry would outlive the pane leaving the window.
# Inlined, like `read_holds` above: the plugin's scripts do not import each other.

# The WezTerm role map. Same variable the server reads (`server/config.mjs`) and
# `session-holds.py` reads.
ROLE_MAP = os.environ.get("SUPERVISOR_ROLE_MAP") or os.path.expanduser(
    "~/.cache/wezterm-role-map.json"
)


def panes():
    """Every WezTerm pane, or `None` when the query FAILED.

    ⚠️ `None`, never `[]`: a failed `wezterm cli list` cannot prove a pane is gone
    any more than it can prove one is live. `[]` is reserved for a transport that
    answered with zero panes, and for a machine with no `wezterm` BINARY -- an
    absent transport has no panes, and the Hold window is a WezTerm concept. Only
    `FileNotFoundError` reads that way; any other `OSError` is a genuine failure and
    stays `None`. See `docs/pane-reads.md`.
    """
    try:
        result = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return []
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        listing = json.loads(result.stdout)
    except ValueError:
        return None
    return listing if isinstance(listing, list) else None


# The activity glyph WezTerm puts in front of a pane title (`✳` active, `◑`/`◐`
# otherwise). Decoration on the title, never part of the name.
_GLYPH = re.compile(r"^[^\w\s]{1,2}\s+")


def registry_names():
    """name -> session id, for the pane join. Later entries win on a clash."""
    names = {}
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        sid = data.get("sessionId") or data.get("session_id")
        name = data.get("name")
        if sid and name:
            names[name] = sid
    return names


def session_pane(session_id, listing):
    """The pane currently showing this session, or None when the listing has none.

    The registry carries no pane id, so a session id joins to its pane through the
    registry `name`, which is what the tab TITLE carries.
    """
    names = registry_names()
    for pane in listing:
        title = pane.get("title") or ""
        name = title if title in names else _GLYPH.sub("", title)
        if names.get(name) == session_id:
            return pane.get("pane_id")
    return None


def pane_window(pane_id, listing):
    """The WezTerm window id owning this pane, or None when the listing has none.

    Read straight off the pane record, with no name join, so it answers for a
    Hold-window pane whose title the registry does not know.
    """
    for pane in listing:
        if str(pane.get("pane_id")) == str(pane_id):
            window = pane.get("window_id")
            if isinstance(window, bool) or not isinstance(window, int):
                return None
            return window
    return None


def hold_window_id():
    """The role map's Hold window id, or None when it cannot be read.

    Read directly from the map's `Hold.window_id`, never through the server's role
    map: its `ROLES` deliberately excludes Hold, because the Hold window is a
    parking lot rather than a role and is never spawned into.
    """
    try:
        with open(ROLE_MAP, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    entry = data.get("Hold") if isinstance(data, dict) else None
    window = entry.get("window_id") if isinstance(entry, dict) else None
    if isinstance(window, bool) or not isinstance(window, int):
        return None
    return window


def in_hold_window(target):
    """Whether session `target` sits in the role map's Hold window.

    Three states, per `docs/pane-reads.md`: `True` in the window, `False` not in
    it, `None` when the pane listing could NOT be read, so the question is
    unanswered. A missing role map, an unreadable one, or one carrying no `Hold`
    key is NOT that third state -- it is the documented fallback to explicit holds
    only, and it answers `False`.
    """
    window = hold_window_id()
    if window is None:
        return False
    listing = panes()
    if listing is None:
        return None
    pane = target if str(target).isdigit() else session_pane(target, listing)
    if pane is None:
        return False
    return pane_window(pane, listing) == window


def hold_refusal(session_id):
    """The refusal a hold warrants for `session_id`, or `(None, None)` when free.

    Returns `(token, detail)`. A hold -- store entry OR a pane in the WezTerm Hold
    window -- returns `held-session`. ⚠️ A FAILED pane read returns a distinct
    `hold-unreadable` refusal rather than `(None, None)`, and that is the decision
    for THIS caller: a restart KILLS a process, so a broken transport must never be
    read as "not held" -- that is the direction that restarts a session the operator
    parked. `docs/pane-reads.md` prescribes *refuse non-zero, naming the transport*
    for a caller that already exits non-zero, which this one does on every refusal.
    """
    reason = held_reason(session_id)
    if reason is not None:
        return "held-session", (
            f"session {session_id} carries an operator hold ({reason}); only the "
            "operator releases it -- see the /supervisor:hold skill"
        )
    verdict = in_hold_window(session_id)
    if verdict is None:
        return "hold-unreadable", (
            f"the WezTerm pane list could not be read, so whether session "
            f"{session_id} sits in the operator's Hold window is unknown; refusing "
            "rather than restarting a session the operator may have parked"
        )
    if verdict:
        return "held-session", (
            f"session {session_id} sits in the operator's WezTerm Hold window; only "
            "the operator releases it -- see the /supervisor:hold skill"
        )
    return None, None
#: The manager registration `fleet-register.py` writes — `{subject, branch, vault}` keyed on
#: the session id. It is the one record that outlives the process: the registry entry
#: (`~/.claude/sessions/<pid>.json`) is deleted on death, this is not. Same override chain
#: `fleet-keeper.py` reads it through.
WORKER_MANAGER_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_WORKER_MANAGER_DIR")
    or os.environ.get("WORKER_MANAGER_STATE_DIR")
    or "~/.claude/state/worker-manager"
)


#: Where `wezterm` lives when no `PATH` reaches it. ⚠️ The fleet keeper is a launchd job whose
#: PATH is `/usr/bin:/bin:/usr/sbin:/sbin` by design (user-writable dirs would shadow system
#: tools), so a bare `wezterm` is FileNotFoundError there — the failure that left the
#: 2026-11-01 keeper unable to resume a manager it had killed. Resolved by absolute path, the
#: same way `find_vault_cli` resolves `vault-cli`, rather than by widening that PATH.
WEZTERM_LOCATIONS = (
    "/Applications/WezTerm.app/Contents/MacOS/wezterm",
    "~/Applications/WezTerm.app/Contents/MacOS/wezterm",
    "/opt/homebrew/bin/wezterm",
    "/usr/local/bin/wezterm",
)

#: The exit-record store. Since v0.243.0 every heartbeat carries the session id, so a pane
#: already running — or just spawned for — a session is visible here before the session
#: registers itself in `~/.claude/sessions/`.
SESSION_EXIT_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSION_EXIT_DIR") or "~/.claude/state/session-exit"
)

#: How recent a pane line naming the session must be to count as "that session is running
#: or starting somewhere". Comfortably above the observer's 30 s heartbeat interval.
IN_FLIGHT_WINDOW = 600


def find_wezterm():
    """Absolute path of an executable `wezterm`, or `None`. `SUPERVISOR_WEZTERM` overrides."""
    override = os.environ.get("SUPERVISOR_WEZTERM")
    if override is not None:
        path = os.path.expanduser(override)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    found = shutil.which("wezterm")
    if found:
        return os.path.abspath(found)
    for cand in WEZTERM_LOCATIONS:
        path = os.path.expanduser(cand)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def unparseable_registry_entries(directory):
    """Names of `*.json` registry files that cannot be parsed, or `None` when unreadable.

    On the normal path a skipped file fails safe. On `--absent`, absence IS the licence, so a
    half-written file — possibly the very session, mid-rewrite — must never count as absence.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    bad = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError, RecursionError):
            bad.append(name)
            continue
        if not isinstance(rec, dict):
            bad.append(name)
    return bad


def recent_pane_for(sid, now=None, window=IN_FLIGHT_WINDOW):
    """`(pane file, line)` of a recent pane record naming `sid` with no end after it, or `None`.

    A resume through any OTHER path (`/supervisor:open`, `/supervisor:restart`, by hand) writes
    a `spawn` line before `claude` boots and registers itself, so for that window the registry
    check alone would pass and two copies would run. This closes it.
    """
    now = time.time() if now is None else now
    try:
        names = [n for n in os.listdir(SESSION_EXIT_DIR) if n.startswith("pane-") and n.endswith(".log")]
    except OSError:
        return None
    for name in names:
        last = None
        try:
            with open(os.path.join(SESSION_EXIT_DIR, name), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if f"\tsession={sid}\t" in line:
                        last = line
        except OSError:
            continue
        if last is None:
            continue
        kind = next((f[5:] for f in last.split("\t") if f.startswith("kind=")), "")
        if kind not in ("spawn", "heartbeat"):
            continue
        try:
            stamp = datetime.datetime.strptime(last.split("\t", 1)[0], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        age = now - stamp.replace(tzinfo=datetime.timezone.utc).timestamp()
        if age <= window:
            return name, last.rstrip("\n")
    return None


def read_registration(sid):
    """The manager registration for `sid`, or `None` when absent or unreadable."""
    if not sid or any(ch in sid for ch in ("/", "\\", "\x00")) or sid in (".", ".."):
        return None
    try:
        with open(os.path.join(WORKER_MANAGER_DIR, f"{sid}.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def task_terminal_refusal(sid, cwd):
    """`(token, detail)` when this session's anchored task is already finished, else `None`.

    Every resume leg starts from "the process is gone", never from "the work is done", so a
    reaped worker whose task reads `completed` is handed a spawn slot again and the close is
    undone — measured 2026-11-11: session 2e540d65 was closed at 07:18:34Z and its id was live
    again at 07:33:55Z. The status is read from disk **here, at resume time**, not from a
    census or digest taken earlier in the round: a task that flips between the candidate
    enumeration and this call must be refused, and a snapshot read would start it.

    The terminal set and the task lookup are `close-worker.py`'s, not a second copy — the
    close side and the resume side have to agree on what "finished" means, or the two drift
    and a task the close refuses to reap is a task the resume happily restarts.

    `(None, None)` is returned in three cases, and only the middle one is an oversight risk:
      * no task anchor — a manager, or a worker that never entered its task. Refusing here
        would stop every manager resume.
      * handed off — the session no longer owns the task; terminality is the successor's.
      * `task-not-terminal` — the ordinary case, the resume may proceed.
    """
    cw = load_sibling("close-worker.py")
    task = cw.find_task(sid, cwd)
    if task is None:
        return None, None
    path, fm, handed_off = task
    if handed_off:
        return None, None
    if cw.task_verdict(task, cw.defer_date.as_now()) is not None:
        return None, None
    status = (fm.get("status") or "").strip()
    defer = fm.get("defer_date")
    return (
        "task-terminal",
        f"session {sid} · {os.path.basename(path)} is status {status or '(none)'!r} with "
        f"defer_date {defer or '(unset)'!r}; its work is finished, so resuming would "
        "restart a session for a closed task and undo the close",
    )


def resume_absent(sid, sessions_dir, dry_run):
    """Resume a session whose process is GONE. Signals nothing; exits 0 only on a resume.

    The normal path cannot do this: it refuses `unknown-session-id` when no registry entry
    carries the id, and a dead session's entry is deleted with it — so a Fleet Manager that
    died could never be brought back (measured 2026-11-06: three deaths in one day). This
    entry inverts the precondition and holds no kill path at all.

    Refusals, each first-line token + exit 1, every one before the spawn:
      `not-absent`         a registry entry carries the id, or the registry is unreadable —
                           the process may be alive, and resuming it would run two copies
      `held-session`       an operator hold; outranks everything, exactly as on the normal path
      `unknown-session-id` no manager registration, so no vault to resume into
    """
    # FIRST, before anything reads a record's fields: a file that is half-written, or valid
    # JSON that is not an object, must refuse with its token rather than crash `find_target`.
    bad = unparseable_registry_entries(sessions_dir or SESSIONS_DIR)
    if bad is None or bad:
        return refuse("not-absent", f"registry entries could not be parsed ({', '.join(bad or ['the directory'])}); "
                                    f"a half-written record may be {sid} itself, so its absence is unproven")
    entries = registry_entries(sessions_dir)
    if entries is None:
        return refuse("not-absent", f"the registry at {sessions_dir or SESSIONS_DIR} could not "
                                    f"be read, so it cannot be proved that {sid} is gone")
    path, _rec = find_target(entries, sid)
    if path is not None:
        return refuse("not-absent", f"a registry entry still carries session {sid}; the process "
                                    "may be alive — use the normal restart, never --absent")
    in_flight = recent_pane_for(sid)
    if in_flight is not None:
        return refuse("not-absent", f"{in_flight[0]} names session {sid} within the last "
                                    f"{IN_FLIGHT_WINDOW}s with no end — it is running or being "
                                    "resumed elsewhere; a second copy would split the conversation")

    held = held_reason(sid)
    if held is not None:
        return refuse("held-session", f"session {sid} carries an operator hold ({held}); only the "
                                      "operator releases it -- see the /supervisor:hold skill")

    reg = read_registration(sid)
    if reg is None:
        return refuse("unknown-session-id", f"no manager registration for {sid} under "
                                            f"{WORKER_MANAGER_DIR}, so there is no vault to resume into")

    vault_name = str(reg.get("vault") or "").strip()
    configs = vault_configs()
    vault = next((v for v in (configs or []) if isinstance(v, dict) and v.get("name") == vault_name), None)
    cwd = os.path.expanduser(str(vault.get("path") or "").strip()) if vault else ""
    if not cwd or not os.path.isdir(cwd):
        print(f"❌ error: the registration names vault {vault_name!r}, which resolves to no "
              f"existing directory — resuming without a cwd strands the session on a "
              f"workspace-trust dialog")
        return 1

    # A resume must not start a process for a task that is already finished. This is the only
    # resume this script can perform for a headless worker, and the incident that motivated the
    # guard was exactly that shape: a reaped headless worker whose task read `completed` came
    # back as a live process fifteen minutes later.
    token, detail = task_terminal_refusal(sid, cwd)
    if token is not None:
        return refuse(token, detail)

    fb = load_sibling("fleet-board.py")
    subject = str(reg.get("subject") or "").strip() or sid[:8]
    branch = str(reg.get("branch") or "").strip() or "fleet"
    # An explicit manager subject: `fleet-board.py`'s `manager_subject` knows only topic and
    # goal, so a fleet manager resolved through it would fall to the WORKER launcher rule.
    launcher, launcher_source = resolve_launcher(sid, cwd, fb, subject=(branch, subject))
    if launcher is None:
        if launcher_source.startswith(ENV_FAULT):
            print(ENV_FAULT)
            print(f"🛑 environment fault (not a refusal): {launcher_source[len(ENV_FAULT) + 2:]}")
            return 3
        print(f"❌ error: no launcher could be resolved for {sid} ({launcher_source})")
        return 1

    wezterm = find_wezterm()
    if wezterm is None:
        print("❌ error: no wezterm found on PATH or at a known install location "
              f"({', '.join(WEZTERM_LOCATIONS)}) — nothing was spawned")
        return 1

    chip = fb.MANAGER_COLOUR
    header = (f"🔄 restart-worker --absent · session {sid} · {subject!r}"
              f"{' · dry-run' if dry_run else ''}")
    if dry_run:
        print(header)
        print(f"   launcher: {launcher} ({launcher_source})")
        print(f"   wezterm: {wezterm}")
        print(f"   ✅ would resume session {sid} — nothing to kill, the process is gone")
        print(f"   ↪ would resume into {cwd}")
        return 0

    # One resume per session per window, across every --absent caller: an O_EXCL lock that
    # outlives this process for `IN_FLIGHT_WINDOW`, so a second call during the new pane's
    # boot (before it registers or heartbeats) is refused instead of starting a second copy.
    # Taken BEFORE any output, so a refusal's token stays the first line a caller reads.
    lock = os.path.join(SESSION_EXIT_DIR, f"absent-resume-{sid}.lock")
    try:
        os.makedirs(SESSION_EXIT_DIR, exist_ok=True)
        if os.path.exists(lock) and time.time() - os.path.getmtime(lock) > IN_FLIGHT_WINDOW:
            os.remove(lock)
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return refuse("not-absent", f"another --absent resume of {sid} started within the last "
                                    f"{IN_FLIGHT_WINDOW}s ({lock})")
    except OSError as exc:
        print(f"❌ error: cannot take the resume lock {lock}: {exc} — nothing was spawned")
        return 1
    print(header)
    print(f"   launcher: {launcher} ({launcher_source})")
    print(f"   wezterm: {wezterm}")

    argv = resume_command(sid, subject, chip, cwd, launcher)
    argv[0] = wezterm
    try:
        spawned = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        # The spawn may well have happened — a second, hand-started copy is the hazard.
        print(f"❌ error: resume outcome UNKNOWN — wezterm did not answer within 30 s; check for "
              f"a pane running {sid} before starting it again")
        return 1
    except Exception as error:
        print(f"❌ error: resume failed before spawning: {error}")
        return 1
    if spawned.returncode != 0:
        print(f"❌ error: resume failed (exit {spawned.returncode}): {(spawned.stderr or '').strip()}")
        return 1
    pane = (spawned.stdout or "").strip().splitlines()
    print(f"   ✅ resumed session {sid} in a new tab (pane {pane[-1] if pane else '?'}); "
          "nothing was signalled")
    print("   ↪ manager: the resumed session must RE-ARM its loop (`/supervisor:fleet-loop`) — "
          "a dead session's scheduled jobs died with it")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Restart one stale idle session — worker or manager — registry-guarded.",
    )
    ap.add_argument("session_id", help="the session id to restart")
    ap.add_argument("--dry-run", action="store_true", help="check everything, kill nothing")
    ap.add_argument("--reason", choices=("code", "lapse"), default="code",
                    help="why this restart is justified. `code` (default) requires something "
                         "newer than the session's start to load, which is precondition 4. "
                         "`lapse` is for a session whose LOOP stopped — a lapsed fleet round "
                         "writes no file, so no input changed — and skips precondition 4 and "
                         "nothing else: a held, busy or headless target is still refused")
    ap.add_argument("--sessions-dir", help=f"registry dir (default: {SESSIONS_DIR})")
    ap.add_argument("--absent", action="store_true",
                    help="resume a session whose process is GONE: refuses unless no registry "
                         "entry carries the id, signals nothing, and takes cwd and launcher from "
                         "the manager registration")
    ap.add_argument("--fresh-prompt", choices=FRESH_PROMPTS, default=None,
                    help="kill the session and start a NEW one (fresh context) through the same "
                         "launcher and cwd, with this prompt as its first turn, instead of "
                         "resuming the same id. Allowlisted: the value is typed into a shell")
    ap.add_argument("--allow-busy-lapse", action="store_true",
                    help="lift ONLY the busy-target refusal, and only for the lapse "
                         "fresh-restart path: requires --reason lapse AND --fresh-prompt, so "
                         "it can never widen a code-staleness restart or a plain resume. "
                         "Operator ruling 2026-11-12: on the lapse card the operator's answer "
                         "IS the judgement that separates a lapse from a slow round, so a "
                         "blanket refusal would override a decision the human has made")
    ap.add_argument("--task-guard", action="store_true",
                    help="read-only: run ONLY the terminal-task guard and exit. Exit 1 with the "
                         "`task-terminal` token when the session's anchored task is finished, 0 "
                         "when a resume may proceed. Signals nothing and spawns nothing, so a "
                         "resume leg that does its own spawn (`commands/open.md` § 3) can route "
                         "the check through this one implementation instead of copying it")
    ap.add_argument("--cwd", help="with --task-guard: the directory the task lookup starts "
                                  "from, normally the session's vault")
    args = ap.parse_args()
    if args.fresh_prompt and args.absent:
        ap.error("--fresh-prompt and --absent are exclusive")
    if args.allow_busy_lapse and not (args.reason == "lapse" and args.fresh_prompt):
        # The flag's whole safety is its narrowness: tied to the lapse fresh-restart path it
        # can only lift a refusal the operator has already judged. Anywhere else it would let
        # a mid-turn kill through on a machine token alone.
        ap.error("--allow-busy-lapse requires --reason lapse AND --fresh-prompt")

    sid = args.session_id.strip()
    if not sid:
        ap.error("session id must not be empty")  # exits 2

    if args.task_guard:
        # The one implementation of the terminal-task guard, exposed so a resume leg that
        # spawns on its own (`commands/open.md` § 3's wezterm `--resume`) can route the check
        # here instead of copying it. Read-only: it signals nothing and spawns nothing.
        token, detail = task_terminal_refusal(sid, args.cwd or os.getcwd())
        if token is not None:
            return refuse(token, detail)
        return 0

    if args.absent:
        return resume_absent(sid, args.sessions_dir, args.dry_run)

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
    # Restarting is exactly the act the operator held the session to prevent. A hold is
    # either a store entry or a pane parked in the WezTerm Hold window; an UNREADABLE
    # pane list refuses too (`hold-unreadable`) rather than reading as "not held".
    token, detail = hold_refusal(sid)
    if token is not None:
        return refuse(token, detail)

    pid = rec.get("pid")
    if not isinstance(pid, int):
        return refuse("ambiguous-pid", f"registry entry {path} carries no integer pid")

    status = (rec.get("status") or "").strip().lower()
    if status != "idle" and not args.allow_busy_lapse:
        return refuse(
            "busy-target",
            f"session {sid} is {status or 'in an unknown state'}, not idle; "
            "a mid-turn kill loses the in-flight work",
        )
    if status != "idle":
        # Reached only under --allow-busy-lapse, which the argument check above confines to
        # `--reason lapse --fresh-prompt`. Printed so the kill is never silent about why it
        # was permitted: a mid-turn manager is being stopped deliberately.
        print(f"   ℹ️ busy-target lifted by --allow-busy-lapse (status {status!r}); the operator "
              f"answered `restart fresh` on this lapse card")

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
    argv = worker_argv(pid)
    subject = fb.manager_subject(name, index, slugs) if role == "manager" and index is not None else None
    launcher, launcher_source = resolve_launcher(sid, (rec.get("cwd") or "").strip(), fb, subject)
    if launcher is None and launcher_source.startswith(ENV_FAULT):
        # Checked before every refusal below: a broken environment must never be masked
        # by, or printed as, a decision about the target.
        print(ENV_FAULT)
        print(f"🛑 environment fault (not a refusal): {launcher_source[len(ENV_FAULT) + 2:]}")
        return 3

    # ⚠️ Precondition 4 applies to a CODE-staleness restart only. `--reason lapse` restarts a
    # session whose LOOP stopped, not one whose code changed: a lapsed fleet round writes no
    # file, so requiring a newer input would refuse every legitimate lapse restart and the
    # keeper that calls this could never act. Everything above and below still applies — a
    # held, busy, headless or unresumable target is refused exactly as before.
    changed = []
    if args.reason == "code":
        if started is None:
            return refuse(
                "stale-load-path",
                f"session {sid} carries no parseable start time, so it cannot be shown that "
                "anything would load differently",
            )
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

    # The cwd the session must resume INTO. Read here, before the kill: a session
    # resumed without its own cwd lands in wezterm's default working directory and
    # stalls on Claude Code's workspace-trust dialog, registering no pid at all — so
    # this is checked BEFORE signalling, never after. An unresumable session must not
    # be killed, which is the whole reason the guard sits above `os.kill`.
    cwd = (rec.get("cwd") or "").strip()

    # The terminal-task guard runs before ANY output: every other refusal in this script
    # prints its token as the first line a caller reads, and an unresumable session must not
    # be signalled. It sits above the header for that reason, not by accident.
    token, detail = task_terminal_refusal(sid, cwd)
    if token is not None:
        return refuse(token, detail)

    print(
        f"🔄 restart-worker · session {sid} · pid {pid} · {name!r}"
        f"{' · dry-run' if args.dry_run else ''}"
    )
    print(
        f"   role: {role} · status: {status} · load path: "
        f"{os.path.basename(load_path) if load_path else 'none'}"
    )
    print(f"   changed: {describe_changed(changed)}")
    for line, _ in describe_source(*mcp_source(argv)):
        print(line)

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

    # ⚠️ Resolved BEFORE the kill, for the same reason the launcher and cwd are: a spawn that
    # cannot happen must not leave a killed pid behind it. The keeper runs under launchd, whose
    # PATH holds no wezterm, so a bare `"wezterm"` fails there while the `--absent` path (which
    # already calls `find_wezterm()`) succeeds — the asymmetry that produced the 2026-11-11
    # destructive-without-restorative failure.
    wezterm = find_wezterm()
    if wezterm is None:
        print("❌ error: no wezterm found on PATH or at a known install location "
              f"({', '.join(WEZTERM_LOCATIONS)}) — refusing before the kill, nothing signalled")
        return 1

    if args.dry_run:
        print("   ✅ would kill the pid above and "
              + (f"start a fresh session with {args.fresh_prompt}" if args.fresh_prompt
                 else "resume the same session id"))
        if cwd:
            print(f"   ↪ would resume into {cwd}")
        return 0

    if not cwd:
        print(
            f"❌ error: registry entry for {sid} carries no cwd, and resuming without "
            "one strands the session on a workspace-trust dialog — refusing before the kill"
        )
        return 1

    # A machine token, printed BEFORE the signal: a caller that must know whether a failure
    # came before or after the kill reads this line rather than matching the prose of
    # whichever refusal follows. `scripts/fleet-keeper.py` arms its cap on it.
    print(f"   killing-pid {pid}")
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
            resume_command(sid, title, chip, cwd, launcher, args.fresh_prompt, wezterm),
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
    if args.fresh_prompt:
        print(
            f"   ✅ killed pid {pid}; started a FRESH session (not {sid}) in a new tab "
            f"(pane {pane[-1] if pane else '?'}) with first turn {args.fresh_prompt}"
        )
        return 0
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
