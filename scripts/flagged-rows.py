#!/usr/bin/env python3
"""`flagged-rows.py` — one JSON row per ELIGIBLE flagged page, across every vault.

Eligibility is the consumer's own gate, never "every page carrying a flag":
`flag` (per kind, below) AND `status: in_progress` AND (`defer_date` absent or due).
Measured 2026-10-31 in `private-personal`: **736 flagged pages, 39 eligible** — a
resolver that emitted the finished ones would spawn work on closed rows.

⚠️ **The flag predicate differs by kind, and the difference is live.**
A **task** binds to `commands/open.md` § Step 0.5's strict `t.get('flag') is True`,
which is what the consumer acts on. A **goal** or **topic** binds to
`scripts/topic-spawn.py`'s `FLAG_TRUTHY` (`true` / `yes` / `on`, quoted or not) —
**eight of `private-personal`'s nine flagged goals spell it `flag: "true"`**, and a
strict reader drops every one.

⚠️ **Vault discovery reads the vault-cli config, never a sibling guess.** Four vaults
(`assistant-data`, `private-boss`, `private-family`, `private-starcitizen`) keep their
goals in `23 Goals/`; the others use `24 Goals/`. A hardcoded sibling emits zero goal
rows for four vaults — the silent tasks-only fallback this script exists to remove.
A fallback probe is REPORTED in `fallbacks`, never taken silently.

Every value is copied from its own authority rather than derived at spawn time; the
field → authority map lives in the task file
`One Resolver Emits Every Flagged Row's Spawn Facts as JSON`.

Usage:
  flagged-rows.py [--json] [--vault <name>] [--role-map <path>]
                  [--template-root <dir>] [--no-liveness]

`--no-liveness` skips the heartbeat probe, the pane lookup and every owner probe,
reporting `liveness: none` and `owner_live: null`. Rows then fall through to
`create` / `direct` because nothing can be proven live — useful offline, and it is
what the fixture tests use so they never touch the live fleet. It is NOT a
substitute for a live run: `open-manager` can never fire under it, since that
branch needs a subject proven to have no live manager.

Exit codes: 0 ok, 2 usage or config error.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROLE_MAP = os.path.expanduser("~/.cache/wezterm-role-map.json")
TEMPLATE_ROOT = os.path.expanduser("~/Documents/workspaces/nuke/task/recurring-schedules/prod")

#: `scripts/topic-spawn.py`'s own predicate — goals and topics accept any spelling,
#: because a YAML boolean has three.
FLAG_TRUTHY = ("true", "yes", "on")

#: `commands/open.md` § 3.0's role → window table. The role map's fourth key `Hold`
#: is not a role target and its `chip` is null, so the map is never the enum source.
ROLE_WINDOW = {"agent": "Agents", "manager": "Managers", "human": "Direct"}
ROLE_CHIP = {"agent": "pink", "manager": "orange", "human": "cyan"}
MISSING_MAP_CHIP = "pink"

#: `kind` is the sort key: topics first, so the consumer opens managers before tasks.
KIND_ORDER = {"topic": 0, "goal": 1, "task": 2}
KIND_DIRS = (
    ("task", "tasks_dir", ("25 Tasks", "24 Tasks", "tasks")),
    ("goal", "goals_dir", ("24 Goals", "23 Goals", "Goals")),
    ("topic", "topics_dir", ("23 Topics", "Topics")),
)

#: `session-liveness.py --check` exit code → verdict. Five answers; an unanswerable
#: probe is UNKNOWN, never folded into ABSENT.
LIVENESS_EXIT = {0: "LIVE", 1: "ABSENT", 2: "UNKNOWN", 3: "AMBIGUOUS", 4: "MALFORMED"}

FLEET = {"kind": "fleet", "name": "Fleet Manager"}

#: `server/spawn-mode.mjs`'s `SPAWN_MODES` — the only two values a spawn accepts.
#: A page carrying anything else (`non_interactive` is live on eight pages today) is
#: normalised to the fleet default and REPORTED, never copied through.
SPAWN_MODES = ("interactive", "headless")
DEFAULT_MODE = "interactive"
FLEET_CONFIG = os.path.expanduser("~/.config/claude-supervisor/config.json")

#: The three roles `commands/open.md` § 3.0 maps to a window. An on-disk value
#: outside this set has no window, so it is normalised to `agent` and reported.
ROLES = tuple(ROLE_WINDOW)

#: `launcher_source` — the union of the worker arm (`docs/fleet-surface.md` § Spawn a
#: worker item 8) and the manager arm (§ Which launcher a manager opens on), which is
#: exactly what `manager-launcher.py` prints. Bound here so the schema and the code
#: cannot drift apart.
LAUNCHER_SOURCES = ("task", "worker-default", "vault", "override",
                    "subject", "manager-default", "claude-sibling")


def _load_open_queue():
    """Import `open-queue.py` so its predicates are reused, not re-implemented.

    The hyphen forbids a normal import; loading by path runs nothing, because its
    `main()` is guarded.
    """
    path = os.path.join(HERE, "open-queue.py")
    spec = importlib.util.spec_from_file_location("open_queue", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


open_queue = _load_open_queue()


# ---------------------------------------------------------------- eligibility


def flag_admits(block, kind):
    """Does this page's `flag:` admit it, under its KIND's predicate?

    A task is strict (`flag: true`, an unquoted YAML boolean), matching
    `commands/open.md` § Step 0.5 — the predicate the consumer applies, so a row this
    admits is a row the consumer would open. A goal or topic is truthy, matching
    `scripts/topic-spawn.py`'s `FLAG_TRUTHY`.

    ⚠️ The strict branch cannot use `field()`, which strips quotes: `flag: "true"` is a
    STRING in YAML and is not `True`.
    """
    raw = re.search(r"^flag:[ \t]*(.*)$", block, re.MULTILINE)
    if not raw:
        return False
    value = raw.group(1).strip()
    if kind == "task":
        # The YAML scalar is lowercase `true`; `open.md` § Step 0.5's `t.get('flag')
        # is True` only reads True because it PARSES the file. A quoted scalar is a
        # string in YAML, never a boolean, so it is rejected here as it is there.
        if value.startswith(('"', "'")):
            return False
        return value == "true"
    return value.strip("\"'").lower() in FLAG_TRUTHY


def eligible(block, kind, today):
    """The consumer's gate — a reason string when ineligible, None when eligible."""
    if block is None:
        return "unreadable page"
    if not flag_admits(block, kind):
        return "not flagged"
    status = (open_queue.field(block, "status") or "").strip()
    if status != "in_progress":
        return "status is %s" % (status or "<none>")
    defer = (open_queue.field(block, "defer_date") or "").strip()[:10]
    if defer and defer > today:
        return "deferred"
    return None


# ---------------------------------------------------------------- discovery


def vault_configs():
    """Every configured vault that declares a `tasks_dir`, name → config dict."""
    out = subprocess.run(["vault-cli", "config", "list", "--output", "json"],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit("flagged-rows: `vault-cli config list` failed: %s"
                         % (out.stderr.strip() or out.returncode))
    payload = json.loads(out.stdout)
    vaults = payload.get("vaults", payload) if isinstance(payload, dict) else payload
    items = (vaults.items() if isinstance(vaults, dict)
             else [(v.get("name"), v) for v in vaults])
    return {name: cfg for name, cfg in items if (cfg.get("tasks_dir") or "").strip()}


def kind_dir(vault_path, cfg, key, fallbacks):
    """(absolute dir, probe name or None) for one kind.

    The config key wins. Only when it is absent does the probe run, and the probe is
    REPORTED — a silently wrong directory is the defect this replaces.
    """
    declared = (cfg.get(key) or "").strip()
    if declared:
        return os.path.join(vault_path, declared), None
    for candidate in fallbacks:
        path = os.path.join(vault_path, candidate)
        if os.path.isdir(path):
            return path, candidate
    return None, None


def read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def read_block(path):
    return open_queue.frontmatter(read_text(path)) or None


def page_type(block, kind):
    declared = (open_queue.field(block, "page_type") or "").strip()
    return declared if declared in KIND_ORDER else kind


def topic_index(topics_dir):
    """goal name → the topic whose `## Goals` lists it.

    ⚠️ The relation is declared on the TOPIC page, never the goal page. The goal's own
    `topics:` field is not a substitute: measured 2026-10-31, 64 of 82 declarations
    appear in that topic's `## Goals` and 18 do not, and only the `## Goals` index
    predicts a live topic manager.
    """
    index = {}
    if not topics_dir or not os.path.isdir(topics_dir):
        return index
    for filename in sorted(os.listdir(topics_dir)):
        if not filename.endswith(".md"):
            continue
        text = read_text(os.path.join(topics_dir, filename))
        match = re.search(r"^## Goals[ \t]*\n(.*?)(?=^#{1,2} |\Z)",
                          text, re.MULTILINE | re.DOTALL)
        if not match:
            continue
        for goal in re.findall(r"\[\[([^\]|#]+)", match.group(1)):
            index.setdefault(goal.strip(), filename[:-3])
    return index


# ---------------------------------------------------------------- authorities


def resolve_role(block, text, template_root):
    """(role, source). `template` wins whenever a template exists — including over a
    DISAGREEING on-disk `role:`, which is the Start Day case where a writer flipped it
    to `agent` while the template still said `human`.

    ⚠️ `text` is the WHOLE page, not the frontmatter block: the `# Source` footer it
    keys on lives in the body, so a search over `block` alone can never match.
    """
    template = template_role(text, template_root)
    if template:
        return template, "template"
    on_disk = (open_queue.field(block, "role") or "").strip()
    if on_disk:
        return on_disk, "on-disk"
    return "agent", "body"


def template_role(text, template_root):
    """The `role:` in the `# Source` schedule YAML's own `frontmatter:` block.

    Read from source control (`bborbe/nuke`), never the deployed CR, which lags.
    `<slug>` is the footer's filename — `start-day` for the instance titled
    `Start Day - 2026-10-31` — never the instance title, which carries the period.
    """
    match = re.search(
        r"`bborbe/nuke/task/recurring-schedules/prod/([A-Za-z0-9._-]+)\.yaml`", text)
    if not match or not template_root:
        return None
    text = read_text(os.path.join(template_root, match.group(1) + ".yaml"))
    if not text:
        return None
    # ⚠️ `frontmatter:` is INDENTED — it sits under `spec.template` in the real
    # schedule YAML (`start-day.yaml` line 58 is `    frontmatter:`), so a
    # column-0 anchor never matches and `role_source: template` silently never
    # fires. Measured 2026-10-31: the real template resolves `role: human` only
    # with the indented form. The block ends at the first line indented no deeper
    # than the key itself.
    key = re.search(r"^([ \t]*)frontmatter:[ \t]*$", text, re.MULTILINE)
    if not key:
        return None
    indent = len(key.group(1))
    lines = []
    for line in text[key.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        lines.append(line)
    role = re.search(r"^[ \t]+role:[ \t]*[\"']?([A-Za-z]+)", "\n".join(lines), re.MULTILINE)
    return role.group(1) if role else None


#: `session-liveness.py --list` renders `<sid>  pid <pid>  <name>  [<transport>]`,
#: optionally followed by `  DEGRADED (...)`. The name is the only field that can
#: contain spaces, so it is captured between `pid <n>` and the bracketed transport —
#: never `parts[2]`, which is the literal word `pid`.
_LIVE_LINE = re.compile(r"^(\S+)\s+pid\s+\d+\s+(.*?)\s+\[[^\]]*\]")


def live_sessions():
    """`{sid: name}` for every registry-live session.

    The line is `<sid>  pid <pid>  <name>  [<transport>]`, optionally followed by
    `  DEGRADED (...)`. The name is the only field that can contain spaces, so it is
    captured between `pid <n>` and the bracketed transport — never `parts[2]`, which
    is the literal word `pid`.
    """
    out = subprocess.run([sys.executable, os.path.join(HERE, "session-liveness.py"),
                          "--list"], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        return {}
    sessions = {}
    for line in out.stdout.splitlines():
        match = _LIVE_LINE.match(line)
        if match:
            sessions[match.group(1)] = match.group(2).strip()
    return sessions


def live_session_names(sessions):
    """Subjects with a live session, manager names included both raw and stripped.

    A manager is named `<Subject> Manager` (`fleet-board.py`'s `_MANAGER_SUFFIX`), so
    the subject is what `resolve_owner` matches on — carrying only the raw name would
    never match a goal, and `owner_live` would read `None` on every row.
    """
    names = set()
    for name in sessions.values():
        if not name:
            continue
        names.add(name)
        stripped = re.sub(r"\s+Manager$", "", name)
        if stripped != name:
            names.add(stripped)
    return names


def liveness_of(sid):
    """`session-liveness.py --check` — the heartbeat endpoint, never the registry."""
    if not sid:
        return "none"
    out = subprocess.run([sys.executable, os.path.join(HERE, "session-liveness.py"),
                          "--check", sid], capture_output=True, text=True, check=False)
    return LIVENESS_EXIT.get(out.returncode, "UNKNOWN")


def row_liveness(ctx, sid):
    """`liveness_of` with a per-run cache — several rows share a session id."""
    key = ("row", sid)
    if key not in ctx["liveness_cache"]:
        ctx["liveness_cache"][key] = liveness_of(sid)
    return ctx["liveness_cache"][key]


def pane_of(sid):
    """`who-needs-me.py`'s `pane_for()` — neither the heartbeat store nor the registry
    carries a pane field.

    ⚠️ `pane_for` PRINTS the pane and returns an exit code, so the pane is its stdout,
    never its return value. Reading the return value yields `0`, which is not a pane
    id and folds every live session into `skip`.
    """
    if not sid:
        return None
    script = ("import importlib.util, sys;"
              "s = importlib.util.spec_from_file_location('wnm', sys.argv[1]);"
              "m = importlib.util.module_from_spec(s); s.loader.exec_module(m);"
              "sys.exit(m.pane_for(sys.argv[2]))")
    out = subprocess.run([sys.executable, "-c", script,
                          os.path.join(HERE, "who-needs-me.py"), sid],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        return None
    pane = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    return pane or None


def fleet_mode():
    """The fleet's `spawn.mode`, the default a page with no `mode:` opens on.

    `server/spawn-mode.mjs` line 133 answers `interactive` when the config is silent.
    """
    try:
        with open(FLEET_CONFIG, encoding="utf-8") as handle:
            value = ((json.load(handle) or {}).get("spawn") or {}).get("mode")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_MODE
    return value if value in SPAWN_MODES else DEFAULT_MODE


def resolve_mode(block, default):
    """(mode, raw-unrecognised-value-or-None).

    `commands/open.md` line 259: a row with no `mode:` opens per fleet config, so an
    ABSENT value is not an anomaly. A value outside `SPAWN_MODES` is.
    """
    raw = (open_queue.field(block, "mode") or "").strip()
    if not raw or raw in SPAWN_MODES:
        return (raw or default), None
    return default, raw


def normalise_role(role):
    """(role, raw-or-None) — `ROLE_WINDOW` is the only role → window table there is."""
    return (role, None) if role in ROLES else ("agent", role)


def _beside(value, cfg, source):
    """Resolve a page-supplied launcher: a bare name sits beside the vault's launcher."""
    if "/" in value:
        return os.path.expanduser(value), source
    base = ((cfg.get("worker_launcher") or cfg.get("manager_launcher")
             or cfg.get("claude_script") or "").strip())
    if not base:
        return value, source
    return os.path.join(os.path.dirname(os.path.expanduser(base)), value), source


def _sibling(path, strip, add):
    """`manager-launcher.py`'s `sibling()` — the compatibility rung."""
    head, base = os.path.split(path)
    base = re.sub(strip + r"$", "", base)
    return path if base.endswith(add) else os.path.join(head, base + add)


def resolve_launcher(kind, role, block, cfg):
    """(launcher, source) — the rule `/supervisor:open` would apply, never a guess.

    Worker arm (`commands/open.md` § Spawn a worker): the task's `launcher:` →
    `worker-default` → `vault`. Manager arm (`manager-launcher.py`): the page's
    `manager_launcher:` → `manager-default` → the `-claude` sibling → `vault`.

    ⚠️ The two arms report DIFFERENT sources on purpose — "the config decided this" and
    "a naming convention decided this" must not look alike in a spawn response.
    """
    if role == "manager" or kind in ("goal", "topic"):
        override = (open_queue.field(block, "manager_launcher") or "").strip()
        if override:
            return _beside(override, cfg, "subject")
        configured = (cfg.get("manager_launcher") or "").strip()
        if configured:
            return os.path.expanduser(configured), "manager-default"
        base = (cfg.get("claude_script") or "").strip()
        if base:
            sib = _sibling(os.path.expanduser(base), "-deepseek", "-claude")
            return (sib, "claude-sibling") if os.path.isfile(sib) else (base, "vault")
        return None, "unresolved"
    own = (open_queue.field(block, "launcher") or "").strip()
    if own:
        return _beside(own, cfg, "task")
    configured = (cfg.get("worker_launcher") or "").strip()
    if configured:
        return os.path.expanduser(configured), "worker-default"
    base = (cfg.get("claude_script") or "").strip()
    return (os.path.expanduser(base), "vault") if base else (None, "unresolved")


def blocked_by_of(block):
    """The row's unresolved blockers, in either frontmatter shape, as a list."""
    match = re.search(r"^blocked_by:[ \t]*(.*)$", block, re.MULTILINE)
    if not match:
        return []
    inline = match.group(1).strip()
    if inline:
        return [] if inline in ("[]", "[ ]") else [inline]
    out = []
    for line in block[match.end():].splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            item = line.strip()
            if item.startswith("-"):
                out.append(item[1:].strip().strip("'\""))
            continue
        break
    return out


def goals_of(block):
    match = re.search(r"^goals:[ \t]*(.*?)(?=^\w|\Z)", block, re.MULTILINE | re.DOTALL)
    return [g.strip() for g in re.findall(r"\[\[([^\]|#]+)\]\]", match.group(1))] if match else []


def subject_page(ctx, vault, kind, name):
    """The page for one subject, or None — `kind` is `goal` or `topic`."""
    return ctx["subjects"].get((vault, kind, name))


def subject_live(ctx, vault, kind, name):
    """Is a manager for this subject LIVE? Registry by name — `manager_on_chain`'s own
    signal, which is what SELECTS an owner. Never the subject page's
    `manager_session_id`; that is a lead for the `--check` probe below, and the two
    are different questions (`docs/fleet-surface.md` § Which manager owns a subject)."""
    if ctx["no_liveness"]:
        return None
    return name in ctx["managers"]


def subject_liveness(ctx, vault, kind, name):
    """`true` / `false` / `null` for one subject's recorded manager session.

    `session-liveness.py --check` on the owner page's `manager_session_id` — the
    section's own "treat it as a lead only, never as a verdict" table: `true` or
    `false` when it answers, `null` when it answers UNKNOWN / AMBIGUOUS / MALFORMED.
    A subject with NO recorded manager session is `false` — absence is a fact, an
    unanswerable probe is not, and collapsing the second into `false` is the fold
    `branch: unknown` exists to reject. Cached: the same subject owns many rows.
    """
    key = (vault, kind, name)
    if key in ctx["liveness_cache"]:
        return ctx["liveness_cache"][key]
    if ctx["no_liveness"]:
        return None
    path = subject_page(ctx, vault, kind, name)
    block = read_block(path) if path else None
    sid = None
    if block is not None:
        sid = ((open_queue.field(block, "manager_session_id") or "").strip()
               or (open_queue.field(block, "claude_session_id") or "").strip()
               or None)
    if not sid:
        verdict = False
    else:
        live = liveness_of(sid)
        verdict = True if live == "LIVE" else (False if live == "ABSENT" else None)
    ctx["liveness_cache"][key] = verdict
    return verdict


#: The fleet sentinel has no page to read a `manager_session_id` off, so its session
#: id comes from the registry and is still checked through `--check`. The cache key
#: is a plain subject triple so fixtures can seed it like any other.
FLEET_KEY = ("fleet", "fleet", "Fleet Manager")


def fleet_liveness(ctx):
    if FLEET_KEY in ctx["liveness_cache"]:
        return ctx["liveness_cache"][FLEET_KEY]
    if ctx["no_liveness"]:
        return None
    verdict = liveness_of(ctx["fleet_sid"])
    live = True if verdict == "LIVE" else (False if verdict == "ABSENT" else None)
    ctx["liveness_cache"][FLEET_KEY] = live
    return live


def resolve_owner(kind, name, block, ctx, vault):
    """`docs/fleet-surface.md` § Which manager owns a subject — the rungs, in order.

    `owner` is never null: `{kind, name}` or the fleet sentinel. Selection is by
    REGISTRY liveness, never by `--check`, so `owner_live` can still answer `false`
    for an owner whose recorded session id has rotated — the two probes are
    deliberately independent.

    ⚠️ A goal's topic is NOT a rung for a worker — tier 4 makes the topic manager
    the owner of an unmanaged GOAL so it can start that goal's manager; it never
    parents the worker to a topic manager. The task's OWN topic is a rung (a task
    named directly in a topic's `## Goals` with no `goals:` of its own), which is
    what stops a goal-less task falling to the root.
    """
    if kind == "topic":
        return ({"kind": "topic", "name": name}
                if subject_live(ctx, vault, "topic", name) is not False else FLEET)
    if kind == "goal":
        if subject_live(ctx, vault, "goal", name) is not False:
            return {"kind": "goal", "name": name}
        topic = ctx["topic_map"].get(name)
        if topic and subject_live(ctx, vault, "topic", topic) is not False:
            return {"kind": "topic", "name": topic}
        return FLEET
    managed_by = (open_queue.field(block, "managed_by") or "").strip().strip("[]'\"")
    for subject in ([managed_by] if managed_by else []) + goals_of(block):
        if subject_live(ctx, vault, "goal", subject) is not False:
            return {"kind": "goal", "name": subject}
    topic = ctx["topic_map"].get(name)
    if topic and subject_live(ctx, vault, "topic", topic) is not False:
        return {"kind": "topic", "name": topic}
    return FLEET


def owner_liveness(owner, kind, name, ctx, vault):
    """(`owner_live`, `chain_live`) for the resolved owner.

    `owner_live` is SC9's field: `--check` on the OWNER's recorded manager session —
    `true` / `false` / `null`. The fleet sentinel has no page, so it reads the live
    session literally named `Fleet Manager`.

    `chain_live` is what `open-manager` keys on: does ANY live manager cover the
    row's subject, the goal's own manager or its declaring topic's? A subject with
    no live manager anywhere is `false`, which is the only value that opens one.
    """
    if owner is FLEET:
        # The sentinel has no page, so its session id comes from the registry and is
        # still checked through `--check` — the same probe every other owner gets,
        # which is what lets a degraded fleet manager read `false` rather than `true`.
        fleet = fleet_liveness(ctx)
        if kind == "topic":
            chain = subject_live(ctx, vault, "topic", name)
        elif kind == "goal":
            chain = subject_live(ctx, vault, "goal", name)
            if chain is False:
                topic = ctx["topic_map"].get(name)
                chain = subject_live(ctx, vault, "topic", topic) if topic else False
        else:
            chain = None
        return fleet, chain
    return subject_liveness(ctx, vault, owner["kind"], owner["name"]), True


def resolve_branch(kind, role, sid, live, pane, chain_live, block):
    """The ordered predicate list — first match wins, so the set is a partition.

    `unknown` outranks everything: an unanswerable probe must never be folded into
    "not live", which is the double-writer `session-liveness.py` forbids.

    `open-manager` keys on `chain_live is False` — the row's subject has NO live
    manager, proven rather than assumed. `None` (an unanswerable probe) does not
    fire it, for the same reason `unknown` outranks `create`.
    """
    refused = []
    if live in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
        return "unknown", refused
    if role == "human":
        return "direct", refused
    if kind == "task" and (open_queue.field(block, "role") or "").strip() == "manager":
        return "skip", ["manager-row"]
    if kind in ("goal", "topic") and chain_live is False:
        return "open-manager", refused
    if live == "LIVE" and pane:
        return "jump", refused
    if live == "ABSENT" and sid:
        return "resume", refused
    if live == "LIVE":
        return "skip", refused
    return "create", refused


# ---------------------------------------------------------------- rows


def row_for(vault, cfg, kind, path, ctx):
    text = read_text(path)
    block = open_queue.frontmatter(text) or None
    reason = eligible(block, kind, ctx["today"])
    if reason is not None:
        return None, reason

    name = os.path.basename(path)[:-3]
    page_kind = page_type(block, kind)
    role, role_source = resolve_role(block, text, ctx["template_root"])
    role, role_raw = normalise_role(role)
    mode, mode_raw = resolve_mode(block, ctx["mode_default"])
    for field_name, raw, used in (("role", role_raw, role), ("mode", mode_raw, mode)):
        if raw is not None:
            ctx["normalised"].append({"vault": vault, "name": name,
                                      "field": field_name, "raw": raw, "used": used})
    launcher, launcher_source = resolve_launcher(page_kind, role, block, cfg)
    if not launcher:
        return None, "no launcher resolved"
    sid = ((open_queue.field(block, "claude_session_id") or "").strip()
           or (open_queue.field(block, "manager_session_id") or "").strip()
           or None)
    live = "none" if ctx["no_liveness"] else row_liveness(ctx, sid)
    # A pane is only meaningful for a LIVE session, and `pane_of` re-imports a
    # 2000-line module per call — probing ABSENT rows too tripled the run time for
    # an answer that is `null` by construction.
    pane = pane_of(sid) if live == "LIVE" else None
    window = ROLE_WINDOW.get(role, "Agents")
    entry = (ctx["role_map"] or {}).get(window) or {}
    owner = resolve_owner(page_kind, name, block, ctx, vault)
    owner_live, chain_live = owner_liveness(owner, page_kind, name, ctx, vault)
    branch, refused = resolve_branch(page_kind, role, sid, live, pane, chain_live, block)
    return {
        "kind": page_kind,
        "vault": vault,
        "owner": owner,
        "owner_live": owner_live,
        "name": name,
        "role": role,
        "role_source": role_source,
        "mode": mode,
        "launcher": launcher,
        "launcher_source": launcher_source,
        "window": window,
        "window_id": entry.get("window_id"),
        "chip": entry.get("chip") or MISSING_MAP_CHIP,
        "session_id": sid,
        "liveness": live,
        "branch": branch,
        "pane": pane,
        "blocked_by": blocked_by_of(block),
        "refused": refused,
    }, None


def role_map(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def collect(args):
    """(rows, skipped-reason counts, fallbacks taken, normalised values)."""
    sessions = {} if args.no_liveness else live_sessions()
    ctx = {
        "today": date.today().isoformat(),
        "role_map": role_map(args.role_map),
        "template_root": args.template_root,
        "no_liveness": args.no_liveness,
        "managers": set() if args.no_liveness else live_session_names(sessions),
        "fleet_sid": next((sid for sid, name in sessions.items()
                           if name == "Fleet Manager"), None),
        "mode_default": fleet_mode(),
        "normalised": [],
        #: (vault, kind, name) → page, for the goal/topic subjects an owner resolves
        #: through. Filled as the directories are walked, before any row is built.
        "subjects": {},
        "liveness_cache": {},
    }
    rows, skipped, fallbacks = [], {}, []
    for vault, cfg in sorted(vault_configs().items()):
        if args.vault and vault != args.vault:
            continue
        vault_path = cfg.get("path") or ""
        topics_dir, _ = kind_dir(vault_path, cfg, "topics_dir", ("23 Topics", "Topics"))
        ctx["topic_map"] = topic_index(topics_dir)
        scanned = []
        # Pass 1 — index every goal and topic page FIRST. `KIND_DIRS` walks tasks
        # before goals, and a task's owner resolves through a goal or topic page, so
        # building rows in walk order would look up a subject the index has not
        # reached yet and fall to the fleet sentinel on every task.
        for kind, key, probe_names in KIND_DIRS:
            directory, probe = kind_dir(vault_path, cfg, key, probe_names)
            if probe:
                fallbacks.append({"vault": vault, "kind": kind, "dir": probe})
            if not directory or not os.path.isdir(directory):
                continue
            scanned.append((kind, directory))
            if kind not in ("goal", "topic"):
                continue
            for filename in sorted(os.listdir(directory)):
                if filename.endswith(".md"):
                    ctx["subjects"][(vault, kind, filename[:-3])] = os.path.join(
                        directory, filename)
        # Pass 2 — build the rows.
        for kind, directory in scanned:
            for filename in sorted(os.listdir(directory)):
                if not filename.endswith(".md"):
                    continue
                row, reason = row_for(vault, cfg, kind,
                                      os.path.join(directory, filename), ctx)
                if row is None:
                    skipped[reason] = skipped.get(reason, 0) + 1
                    continue
                rows.append(row)
    rows.sort(key=lambda r: (KIND_ORDER.get(r["kind"], 9), r["vault"], r["name"]))
    return rows, skipped, fallbacks, ctx["normalised"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit rows as JSON")
    parser.add_argument("--vault", help="narrow the scan to one vault")
    parser.add_argument("--role-map", default=ROLE_MAP)
    parser.add_argument("--template-root", default=TEMPLATE_ROOT,
                        help="the bborbe/nuke recurring-schedules/prod directory")
    parser.add_argument("--no-liveness", action="store_true",
                        help="skip the heartbeat probe and pane lookup (offline/fixtures)")
    args = parser.parse_args(argv)

    rows, skipped, fallbacks, normalised = collect(args)
    if args.json:
        json.dump({"rows": rows, "skipped": skipped, "fallbacks": fallbacks,
                   "normalised": normalised}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    for row in rows:
        print("%-6s %-22s %-12s %s" % (row["kind"], row["vault"], row["branch"], row["name"]))
    for item in fallbacks:
        print("fallback: %s/%s -> %s" % (item["vault"], item["kind"], item["dir"]))
    for item in normalised:
        print("normalised: %s/%s %s %r -> %s"
              % (item["vault"], item["name"], item["field"], item["raw"], item["used"]))
    print("%d rows, %d skipped" % (len(rows), sum(skipped.values())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
