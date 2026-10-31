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

`--no-liveness` skips the heartbeat probe and the pane lookup, reporting
`liveness: none` / `branch: create` — useful offline, and it is what the fixture
tests use so they never touch the live fleet.

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


def resolve_role(block, template_root):
    """(role, source). `template` wins whenever a template exists — including over a
    DISAGREEING on-disk `role:`, which is the Start Day case where a writer flipped it
    to `agent` while the template still said `human`."""
    template = template_role(block, template_root)
    if template:
        return template, "template"
    on_disk = (open_queue.field(block, "role") or "").strip()
    if on_disk:
        return on_disk, "on-disk"
    return "agent", "body"


def template_role(block, template_root):
    """The `role:` in the `# Source` schedule YAML's own `frontmatter:` block.

    Read from source control (`bborbe/nuke`), never the deployed CR, which lags.
    `<slug>` is the footer's filename — `start-day` for the instance titled
    `Start Day - 2026-10-31` — never the instance title, which carries the period.
    """
    match = re.search(
        r"`bborbe/nuke/task/recurring-schedules/prod/([A-Za-z0-9._-]+)\.yaml`", block)
    if not match or not template_root:
        return None
    text = read_text(os.path.join(template_root, match.group(1) + ".yaml"))
    if not text:
        return None
    block_match = re.search(r"^frontmatter:[ \t]*\n((?:[ \t]+.*\n)+)", text, re.MULTILINE)
    if not block_match:
        return None
    role = re.search(r"^[ \t]+role:[ \t]*[\"']?([A-Za-z]+)", block_match.group(1), re.MULTILINE)
    return role.group(1) if role else None


def live_session_names():
    """Names of every live session — a manager for subject X is a live session named X."""
    out = subprocess.run([sys.executable, os.path.join(HERE, "session-liveness.py"),
                          "--list"], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        return set()
    names = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            names.add(parts[2])
    return names


def liveness_of(sid):
    """`session-liveness.py --check` — the heartbeat endpoint, never the registry."""
    if not sid:
        return "none"
    out = subprocess.run([sys.executable, os.path.join(HERE, "session-liveness.py"),
                          "--check", sid], capture_output=True, text=True, check=False)
    return LIVENESS_EXIT.get(out.returncode, "UNKNOWN")


def pane_of(sid):
    """`who-needs-me.py`'s `pane_for()` — neither the heartbeat store nor the registry
    carries a pane field."""
    if not sid:
        return None
    script = ("import importlib.util, json, sys;"
              "s = importlib.util.spec_from_file_location('wnm', sys.argv[1]);"
              "m = importlib.util.module_from_spec(s); s.loader.exec_module(m);"
              "print(json.dumps(m.pane_for(sys.argv[2])))")
    out = subprocess.run([sys.executable, "-c", script,
                          os.path.join(HERE, "who-needs-me.py"), sid],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout.strip())
    except ValueError:
        return None


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


def resolve_owner(kind, name, block, topic_map, managers, own_live):
    """`docs/fleet-surface.md` § Which manager owns a subject, via `fleet-board.py`.

    Returns (owner, owner_live). `owner` is never null: `{kind, name}` or the fleet
    sentinel. ⚠️ A goal's topic is NOT a rung for a worker — tier 4 makes the topic
    manager the owner of an unmanaged GOAL so it can start that goal's manager; it
    never parents the worker to a topic manager.
    """
    if kind == "topic":
        return ({"kind": "topic", "name": name}, own_live) if name in managers else (FLEET, None)
    if kind == "goal":
        topic = topic_map.get(name)
        if not topic:
            return FLEET, None
        return {"kind": "topic", "name": topic}, (True if topic in managers else None)
    managed_by = (open_queue.field(block, "managed_by") or "").strip().strip("[]'\"")
    if managed_by:
        return ({"kind": "goal", "name": managed_by},
                True if managed_by in managers else None)
    for goal in goals_of(block):
        if goal in managers:
            return {"kind": "goal", "name": goal}, True
    return FLEET, None


def resolve_branch(kind, role, sid, live, pane, owner, owner_live, block):
    """The ordered predicate list — first match wins, so the set is a partition.

    `unknown` outranks everything: an unanswerable probe must never be folded into
    "not live", which is the double-writer `session-liveness.py` forbids.
    """
    refused = []
    if live in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
        return "unknown", refused
    if role == "human":
        return "direct", refused
    if kind == "task" and (open_queue.field(block, "role") or "").strip() == "manager":
        return "skip", ["manager-row"]
    if kind in ("goal", "topic") and owner is not FLEET and not owner_live:
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
    block = read_block(path)
    reason = eligible(block, kind, ctx["today"])
    if reason is not None:
        return None, reason

    name = os.path.basename(path)[:-3]
    role, role_source = resolve_role(block, ctx["template_root"])
    sid = ((open_queue.field(block, "claude_session_id") or "").strip()
           or (open_queue.field(block, "manager_session_id") or "").strip()
           or None)
    live = "none" if ctx["no_liveness"] else liveness_of(sid)
    pane = None if ctx["no_liveness"] else pane_of(sid)
    page_kind = page_type(block, kind)
    window = ROLE_WINDOW.get(role, "Agents")
    entry = (ctx["role_map"] or {}).get(window) or {}
    owner, owner_live = resolve_owner(page_kind, name, block, ctx["topic_map"],
                                      ctx["managers"], live == "LIVE")
    branch, refused = resolve_branch(page_kind, role, sid, live, pane, owner,
                                     owner_live, block)
    launcher = (open_queue.field(block, "launcher") or "").strip()
    return {
        "kind": page_kind,
        "vault": vault,
        "owner": owner,
        "owner_live": owner_live,
        "name": name,
        "role": role,
        "role_source": role_source,
        "mode": (open_queue.field(block, "mode") or "").strip() or "headless",
        "launcher": launcher or None,
        "launcher_source": "task" if launcher else "worker-default",
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
    """(rows, skipped-reason counts, fallbacks taken)."""
    ctx = {
        "today": date.today().isoformat(),
        "role_map": role_map(args.role_map),
        "template_root": args.template_root,
        "no_liveness": args.no_liveness,
        "managers": set() if args.no_liveness else live_session_names(),
    }
    rows, skipped, fallbacks = [], {}, []
    for vault, cfg in sorted(vault_configs().items()):
        if args.vault and vault != args.vault:
            continue
        vault_path = cfg.get("path") or ""
        topics_dir, _ = kind_dir(vault_path, cfg, "topics_dir", ("23 Topics", "Topics"))
        ctx["topic_map"] = topic_index(topics_dir)
        for kind, key, probe_names in KIND_DIRS:
            directory, probe = kind_dir(vault_path, cfg, key, probe_names)
            if probe:
                fallbacks.append({"vault": vault, "kind": kind, "dir": probe})
            if not directory or not os.path.isdir(directory):
                continue
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
    return rows, skipped, fallbacks


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

    rows, skipped, fallbacks = collect(args)
    if args.json:
        json.dump({"rows": rows, "skipped": skipped, "fallbacks": fallbacks},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    for row in rows:
        print("%-6s %-22s %-12s %s" % (row["kind"], row["vault"], row["branch"], row["name"]))
    for item in fallbacks:
        print("fallback: %s/%s -> %s" % (item["vault"], item["kind"], item["dir"]))
    print("%d rows, %d skipped" % (len(rows), sum(skipped.values())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
