#!/usr/bin/env python3
"""`/supervisor:reset` — re-discover a manager's state from disk. Never deletes.

Four steps, in order, each printing what it read and what it changed:

  1. subject   re-resolved from scratch: the explicit/conversation argument, this
               session's own state file, this session's name. `last-<vault>.json` —
               the cross-session fallback of last resort — is NEVER read for the
               subject; its content is printed only so the skip is visible.
  2. digest    `~/.claude/state/sweep-gate-loop/<vault>/<topic>.json`: the digest is REWRITTEN to
               a `reset-<ts>` sentinel that can never equal a real digest, so the next
               tick is a full sweep. Rewritten rather than deleted, because the same
               file carries `busy_since` — the cross-tick stuck clock — and deleting
               the file would silently reset every stuck verdict with it.
  3. ledger    every OPEN entry in `~/.claude/state/open-items/<session>.json` is
               re-read against disk. Exactly two outcomes change an entry: closed with
               the cited path whose task reads `status: completed`, or flagged
               (`reset_flag`) when its premise no longer resolves. An entry that
               cannot be classified stays open. No entry is ever removed: the id set
               is compared before the write, and a mismatch aborts without writing.
  4. tracked   re-read from the topic page's `## Goals` list plus every task whose
               `goals:` frontmatter names a member — never from a cached file.

The ledger is read and written through `open-items.py`'s own load/save, so the
write discipline (atomic tmp+rename, filename is the session authority) is shared,
not copied. `--dry-run` computes and prints everything and writes none of reset's
changes (open-items' own load() may still persist its schema heal, as `list` does).
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

STATE = os.path.expanduser("~/.claude/state")
HERE = os.path.dirname(os.path.abspath(__file__))


def load_open_items():
    spec = importlib.util.spec_from_file_location("open_items", os.path.join(HERE, "open-items.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def slug(topic):
    # Same as sweep-gate.py's slug(), so both agree on the filename.
    return re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")


def read_json(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def vault_config(name):
    """The vault-cli config entry for `name` (case-insensitive), or None."""
    try:
        proc = subprocess.run(
            ["vault-cli", "--output", "json", "config", "list"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        vaults = json.loads(proc.stdout) if proc.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for v in vaults:
        if (v.get("name") or "").lower() == name.lower():
            return v
    return None


def page_for(subject, vcfg):
    """(branch, path) of the goal/topic page `subject` names, or (None, None)."""
    root = os.path.expanduser(vcfg["path"])
    for branch, key in (("topic", "topics_dir"), ("goal", "goals_dir")):
        sub = vcfg.get(key)
        if sub:
            path = os.path.join(root, sub, subject + ".md")
            if os.path.exists(path):
                return branch, path
    return None, None


# ---------------------------------------------------------------- 1. subject

def resolve_subject(args, sid, vcfg):
    """Returns (subject, branch, page, source, lines)."""
    lines = []
    candidates = []
    if args.subject:
        candidates.append((args.source, args.subject))
    st = read_json(os.path.join(STATE, "worker-manager", f"{sid}.json")) if sid else {}
    if st.get("subject") and (st.get("vault") or "").lower() == vcfg["name"].lower():
        candidates.append(("session", st["subject"]))
    pid = os.environ.get("CLAUDE_PID", "")
    name = read_json(os.path.expanduser(f"~/.claude/sessions/{pid}.json")).get("name", "") if pid else ""
    name = re.sub(r"^[^\w\[]+", "", name).strip()
    if name:
        candidates.append(("name", name))

    chosen = None
    for source, subject in candidates:
        branch, page = page_for(subject, vcfg)
        if chosen is None and page:
            chosen = (subject, branch, page, source)
            lines.append(f"  used     {source}: {subject}")
        elif chosen is None:
            lines.append(f"  miss     {source}: {subject} (no goal/topic page)")
        else:
            lines.append(f"  unused   {source}: {subject}")

    last = [
        p for p in glob.glob(os.path.join(STATE, "worker-manager", "last-*.json"))
        if os.path.basename(p)[5:-5].lower() == vcfg["name"].lower()
    ]
    held = read_json(last[0]).get("subject", "") if last else ""
    label = os.path.basename(last[0]) if last else f"last-{vcfg['name']}.json"
    lines.append(f"  skipped  {label} (holds: {held or 'nothing'}) — reset never reads it")
    if chosen is None:
        return None, None, None, None, lines
    return (*chosen, lines)


# ---------------------------------------------------------------- 2. digest

def reset_digest(subject, vault, dry):
    path = os.path.join(STATE, "sweep-gate-loop", vault.lower(), f"{slug(subject)}.json")
    if not os.path.exists(path):
        return [f"  before   {path} absent", "  after    absent — next tick is a first-run full sweep"]
    data = read_json(path)
    before = data.get("digest", "(no digest key)")
    after = f"reset-{now_iso()}"
    if not dry:
        data["digest"] = after
        data["reset_at"] = now_iso()
        write_json(path, data)
    return [
        f"  before   {path} digest {before}",
        f"  after    digest {after}{' (dry-run, not written)' if dry else ''} — next tick renders a full sweep",
        f"  kept     busy_since ({len(data.get('busy_since') or {})} entries)",
    ]


# ---------------------------------------------------------------- 3. ledger

def task_status(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    m = re.search(r"^status:\s*(\S+)", text.split("\n---", 1)[0], re.M)
    return m.group(1).strip("'\"") if m else ""


def revalidate(oi, sid, dry):
    data = oi.load(sid)
    items = data["items"]
    ids_before = sorted(i["id"] for i in items)
    open_before = sum(1 for i in items if i.get("state") == "open")
    dirs = oi.configured_task_dirs()
    lines, changed = [], False
    for item in items:
        if item.get("state") != "open":
            continue
        head = f"  {item['id']} {item['kind']}"
        if item["kind"] == "asked-of-you":
            lines.append(f"{head}: open — resolves only on the operator's answer")
            continue
        state, path = oi.target_state(item, dirs)
        if state == "none":
            lines.append(f"{head}: open — names no task, no disk evidence to test")
            continue
        if state != "ok":
            reason = "task file not found" if state == "unresolvable" else "task dirs not searchable"
            item["reset_flag"] = f"{reason}: {item.get('task')}"
            item["reset_flagged_at"] = oi.now()
            changed = True
            lines.append(f"{head}: FLAGGED — {item['reset_flag']}")
            continue
        status = task_status(path)
        if status == "completed":
            item["state"] = "closed"
            item["closed_at"] = oi.now()
            item["closed_evidence"] = f"reset: {path} reads status: completed"
            changed = True
            lines.append(f"{head}: CLOSED — {path} reads status: completed")
        elif status == "aborted":
            item["reset_flag"] = f"task aborted: {path}"
            item["reset_flagged_at"] = oi.now()
            changed = True
            lines.append(f"{head}: FLAGGED — {path} reads status: aborted")
        else:
            lines.append(f"{head}: open — {os.path.basename(path)} reads status: {status or '?'}")
    ids_after = sorted(i["id"] for i in items)
    if ids_after != ids_before:
        sys.exit("error: ledger id set changed during re-validation — nothing written")
    open_after = sum(1 for i in items if i.get("state") == "open")
    if changed and not dry:
        oi.save(data)
    summary = (
        f"  ledger   {oi.path_for(sid)}: {len(ids_before)} entries before, {len(ids_after)} after "
        f"(id set identical) · open {open_before} → {open_after}"
        f"{' (dry-run, not written)' if dry and changed else ''}"
    )
    return [summary] + lines


# ---------------------------------------------------------------- 4. tracked

_LIST_ITEM = re.compile(r"^\s*-\s*\[\[(.+?)\]\]")


def members_of(topic_page):
    with open(topic_page, encoding="utf-8") as fh:
        text = fh.read()
    # Section ends at the next `##` or `#`, never at a `###`: topic pages carry
    # `###` prose notes and phase sub-headings inside `## Goals`, and terminating
    # on `###` truncated the member list (measured 2026-10-06). Matches
    # `fleet-board.py`, which has always stopped at `## `. ⚠️ The same lookahead
    # is duplicated in `manager-predispatch.py` (same repo) and in the VAULT's
    # `<vault>/.claude/scripts/sweep-gate.py` — that third copy is NOT in this
    # repository (`bborbe/obsidian-personal`), so a grep here finds only two.
    m = re.search(r"^## Goals\s*\n(.*?)(?=\n## |\n# |\Z)", text, re.S | re.M)
    if not m:
        return []
    return [h.group(1).strip() for h in map(_LIST_ITEM.match, m.group(1).splitlines()) if h]


def clean(value):
    """Strip `- `, quotes, brackets until the name stops changing — covers
    `- '[[X]]'`, `goals: X`, `- - X` and `['[[X]]']`."""
    prev = None
    while prev != value:
        prev = value
        value = value.strip().lstrip("-").strip().strip("'\"").strip("[]").strip()
    return value.split("|")[0]


def goals_of(text):
    fm = text.split("\n---", 1)[0] if text.startswith("---") else ""
    m = re.search(r"^goals:(.*)$", fm, re.M)
    if not m:
        return set()
    rest = m.group(1).strip()
    if rest and rest != "[]":
        return {clean(v) for v in rest.strip("[]").split(",") if clean(v)}
    block = re.search(r"^goals:\s*\n((?:[ \t]*-.*\n?)*)", fm, re.M)
    return {clean(v) for v in (block.group(1).splitlines() if block else []) if clean(v)}


def tracked_set(subject, branch, page, vcfg):
    members = members_of(page) if branch == "topic" else [subject]
    tasks_dir = os.path.join(os.path.expanduser(vcfg["path"]), vcfg["tasks_dir"])
    direct, via_goals = [], []
    for path in sorted(glob.glob(os.path.join(tasks_dir, "*.md"))):
        name = os.path.basename(path)[:-3]
        if name in members:
            direct.append(name)
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()  # whole file: long metrics_sessions can push goals: far down
        except OSError:
            continue
        if goals_of(text) & set(members):
            via_goals.append(name)
    src1 = f"{page} ## Goals ({len(members)} members)" if branch == "topic" else f"goal {subject}"
    lines = [
        f"  source   {src1}",
        f"  source   goals: frontmatter of {tasks_dir} ({len(via_goals)} tasks)",
        f"  tracked  {len(direct) + len(via_goals)} tasks ({len(direct)} declared directly)",
    ]
    lines += [f"    - {n}" for n in sorted(direct + via_goals)]
    return lines


def main():
    ap = argparse.ArgumentParser(description="Re-discover a manager's state from disk; never deletes.")
    ap.add_argument("--subject", help="goal or topic name (explicit or from the conversation)")
    ap.add_argument("--source", choices=("explicit", "conversation"), default="explicit")
    ap.add_argument("--vault", help="vault name (default: this session's recorded vault)")
    ap.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sid = args.session
    if not sid:
        sys.exit("error: no session id — pass --session")

    vname = args.vault or read_json(os.path.join(STATE, "worker-manager", f"{sid}.json")).get("vault", "")
    vcfg = vault_config(vname) if vname else None
    if not vcfg:
        sys.exit(f"error: vault {vname or '(none)'} not in vault-cli config — pass --vault")

    print(f"🔄 RESET · session {sid[:8]} · vault {vcfg['name']}{' · dry-run' if args.dry_run else ''}")
    subject, branch, page, source, lines = resolve_subject(args, sid, vcfg)
    print("1. subject")
    print("\n".join(lines))
    if not subject:
        print("❌ No subject resolves. Pass one: /supervisor:reset \"<goal or topic>\"")
        return 3
    print(f"  Subject: {subject} (from {source}) · {branch} · {page}")
    if not args.dry_run:
        write_json(
            os.path.join(STATE, "worker-manager", f"{sid}.json"),
            {"subject": subject, "vault": vcfg["name"], "branch": branch, "resolved_at": now_iso()},
        )
    print("2. sweep-gate digest")
    print("\n".join(reset_digest(subject, vcfg["name"], args.dry_run)))
    print("3. ledger re-validation")
    print("\n".join(revalidate(load_open_items(), sid, args.dry_run)))
    print("4. tracked set")
    print("\n".join(tracked_set(subject, branch, page, vcfg)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
