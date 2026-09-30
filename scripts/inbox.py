#!/usr/bin/env python3
"""inbox — the operator's approval queue, as one read-only view.

`phase: todo` IS the operator's approval boundary: agents file there and stop,
and the operator's `todo -> planning` flip is the approval. This script renders
that boundary as a readable list, grouped so a triage pass can be done in one
sitting rather than by walking the vault.

READ-ONLY, and it must stay that way. This script writes no task file, no cache
and no state file. The three verbs that move a row (approve / reject / later)
belong to `commands/inbox.md`, and they run through `vault-cli` — the only
writer of an approval. A render that mutated anything would make the view
indistinguishable from the act, which is the one property the whole surface
depends on.

WHAT COUNTS AS LIVE. A row is in the inbox when `phase` is `todo` AND its
`status` is neither terminal (`completed`, `aborted`) nor a deliberate park
(`backlog`, `hold`). `backlog` is excluded for a specific reason rather than for
tidiness: `later` SENDS a row there, so a row already at `backlog` is one the
operator has already answered — listing it again would re-ask a settled
question on every render.

⚠️ Excluding `backlog` is not the same as excluding a stale row; the two are
independent and this script does not judge staleness. It reports what sits at
the boundary. The audit that RANKS rows is `agents/manager-drive.md` clause
(1)'s, not this one's.

WHAT IS EXCLUDED AS RECURRING. `recurring:` frontmatter, or a cadence-marked
title carrying an ISO-week token (`2026W35`, optionally with a weekday suffix).
⚠️ A plain ISO DATE in a title is NOT a cadence marker and must not be treated
as one — `Fix Failed CI Build claude-supervisor 2026-09-30` is a one-off whose
title happens to be dated, and a date-shaped test would silently drop every such
row from the inbox. The week token is the discriminator; keep it that way.

TITLES ARE NOT NAMESPACED. One vault's `a recurring task - 2026-09-21` has
coexisted with another vault's task of the same name, as two different tasks.
Every row therefore prints its vault even when it is grouped under a topic:
grouping is a reading aid, the vault is the identity.

A VAULT THAT CANNOT ANSWER IS SKIPPED AND NAMED, NEVER FATAL. One configured
vault returns literal `null` rather than a task list, and empty stdin does the
same; either would otherwise abort the scan mid-loop and silently truncate every
vault after it in iteration order. The failure mode to avoid is a SHORT list
that reads as a complete one.

RESOLVING A ROW TO ITS VAULT. `--resolve <title>` prints the vault owning a live
row, because every verb must pass `--vault` (ten vaults declare a `tasks_dir`,
and the configured default is one of them). It REFUSES on ambiguity rather than
picking: titles are not namespaced, so the same title can be live in two vaults
at once and a verb that guessed would move the wrong row in the wrong vault.
Resolution reads the same live set the view renders, so a row already approved,
rejected or deferred resolves to nothing — which is the honest answer, and it is
what stops a second verb from re-deciding a settled row.

Usage:
    inbox.py [--vault <name>] [--json]
    inbox.py --resolve <title>

Exit codes: 0 ok, 2 vault config unreadable, 3 no such live row, 4 ambiguous.
"""
import argparse
import json
import os
import re
import subprocess
import sys

# A cadence-marked title carries an ISO-week token; a dated one-off does not.
# See the module docstring for why the date form is deliberately absent.
CADENCE_RE = re.compile(r"\d{4}W\d{2}")

# Terminal and parked statuses. `backlog` is here because `later` writes it.
NOT_LIVE = {"completed", "aborted", "backlog", "hold"}

FM_RE = re.compile(r"\A---\n(.*?)\n---", re.S)

# `Topic: [[Manager Layer]]` — the topic-direct convention. A row may also carry
# a `topics:` frontmatter list; both are read, frontmatter first.
TOPIC_LINE_RE = re.compile(r"^Topic:\s*\[\[([^\]|]+)", re.M)

WHY_WIDTH = 96


def load_vaults():
    """[(name, path, tasks_dir, has_topics_dir)] from vault-cli's own config.

    vault-cli is the single source of truth for where a vault keeps its tasks,
    so a folder rename (`24 Tasks` -> `25 Tasks`) needs no edit here. Raises
    rather than returning [] so the caller can say "nothing readable" instead of
    rendering an empty inbox that reads like a clean one.
    """
    res = subprocess.run(
        ["vault-cli", "--output", "json", "config", "list"],
        capture_output=True, text=True, timeout=20,
    )
    if res.returncode != 0:
        raise RuntimeError(f"vault-cli config list failed: {res.stderr.strip()}")
    out = []
    for v in json.loads(res.stdout) or []:
        tasks_dir = v.get("tasks_dir")
        path = os.path.expanduser(v.get("path") or "")
        if tasks_dir and path:
            out.append((v.get("name") or os.path.basename(path), path,
                        tasks_dir, bool(v.get("topics_dir"))))
    return sorted(out)


def parse(text):
    """(frontmatter dict, body). Frontmatter values are kept as raw strings."""
    m = FM_RE.match(text)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if km:
            fm[km.group(1)] = km.group(2).strip().strip("'\"")
    return fm, text[m.end():]


def is_recurring(fm, title):
    return "recurring" in fm or bool(CADENCE_RE.search(title))


def why_it_matters(body):
    """The `# Impact` first paragraph, else the first paragraph. One line."""
    m = re.search(r"^# Impact\s*\n(.*?)(?=\n# |\Z)", body, re.S | re.M)
    para = m.group(1) if m else body
    for chunk in re.split(r"\n\s*\n", para):
        line = " ".join(chunk.split())
        if line:
            return line[:WHY_WIDTH] + ("…" if len(line) > WHY_WIDTH else "")
    return ""


def topic_of(fm, body, vault_has_topics):
    """The row's group. A topic when the vault has one, else None (-> vault)."""
    if not vault_has_topics:
        return None
    if fm.get("topics"):
        return fm["topics"].strip("[]").split(",")[0].strip().strip("'\"")
    m = TOPIC_LINE_RE.search(body)
    return m.group(1).strip() if m else None


def scan(vaults, only=None):
    """([(group, row)], [skipped]) for every live row."""
    rows, skipped = [], []
    for name, path, tasks_dir, has_topics in vaults:
        if only and name != only:
            continue
        d = os.path.join(path, tasks_dir)
        if not os.path.isdir(d):
            skipped.append(f"{name} (no {tasks_dir}/)")
            continue
        try:
            entries = sorted(os.listdir(d))
        except OSError as e:
            skipped.append(f"{name} ({e.strerror})")
            continue
        for entry in entries:
            if not entry.endswith(".md"):
                continue
            try:
                with open(os.path.join(d, entry), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError) as e:
                skipped.append(f"{name}/{entry} ({e.__class__.__name__})")
                continue
            fm, body = parse(text)
            if fm.get("phase") != "todo" or fm.get("status") in NOT_LIVE:
                continue
            title = entry[:-3]
            if is_recurring(fm, title):
                continue
            group = topic_of(fm, body, has_topics) or name
            rows.append((group, {"title": title, "vault": name,
                                 "why": why_it_matters(body)}))
    return rows, skipped


def render(rows, skipped, out=sys.stdout):
    if not rows:
        print("✅ Inbox empty — no live row sits at `phase: todo`.", file=out)
    groups = {}
    for group, row in rows:
        groups.setdefault(group, []).append(row)
    print(f"⌛ {len(rows)} unapproved · {len(groups)} group(s) · "
          f"read-only, nothing written", file=out)
    for group in sorted(groups, key=lambda g: (-len(groups[g]), g)):
        members = sorted(groups[group], key=lambda r: r["title"])
        print(f"\n▸ {group} ({len(members)})", file=out)
        for r in members:
            print(f"  • {r['title']}", file=out)
            print(f"    [{r['vault']}] {r['why']}", file=out)
    if skipped:
        print(f"\n⚠️ skipped {len(skipped)}: " + "; ".join(skipped), file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", help="narrow the scan to one vault")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--resolve", metavar="TITLE",
                    help="print the vault owning this live row (refuses if ambiguous)")
    args = ap.parse_args(argv)

    try:
        vaults = load_vaults()
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    if not vaults:
        print("❌ no vault declares a tasks_dir", file=sys.stderr)
        return 2

    rows, skipped = scan(vaults, args.vault)
    if args.resolve:
        hits = sorted({r["vault"] for _, r in rows if r["title"] == args.resolve})
        if not hits:
            print(f"❌ no live row named {args.resolve!r} — it is approved, "
                  f"rejected, deferred, or does not exist", file=sys.stderr)
            return 3
        if len(hits) > 1:
            print(f"❌ {args.resolve!r} is live in more than one vault: "
                  + ", ".join(hits) + " — pass --vault to say which", file=sys.stderr)
            return 4
        print(hits[0])
        return 0
    if args.json:
        json.dump({"count": len(rows), "skipped": skipped,
                   "rows": [{"group": g, **r} for g, r in rows]},
                  sys.stdout, indent=2)
        print()
    else:
        render(rows, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
