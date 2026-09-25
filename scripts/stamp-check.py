#!/usr/bin/env python3
"""Apply the session-stamp rule to a vault's task files — fleet-verify check 3.

The rule (docs/fleet-surface.md § Session stamps): a session may carry many
`claude_session_id` stamps, but **at most one on an open task**, and task creation
never stamps. A session answers "what am I working on" by resolving its stamp, so
two open tasks behind one stamp is an ambiguous answer. Many finished tasks behind
one stamp is history — one session worked several tasks in turn — and it records
who did the work, so it is permitted and never rewritten.

  violation  -- one stamp on 2+ open tasks (exit 1)
  suspect    -- one stamp on exactly one open task plus finished ones, where that
                open task's `metrics_sessions` names its workers and the stamp is
                not among them: the creating session's id left on a task someone
                else worked (reported, does not fail the run)
  permitted  -- every other stamp spanning 2+ files

Measured 2026-09-25 over `25 Tasks/`: 33 stamps spanned 2+ files. Counting "2+
files" called all 33 defects; under the rule, 30 were history and 3 were creator
stamps on open tasks. A fourth "stamp" was `goals:` — three files with an EMPTY
`claude_session_id:`, read by a newline-crossing parse. An empty value is no stamp.

The field is read from the frontmatter block only, so a stamp quoted in a task's
body is never mistaken for a declaration.

  stamp-check.py <tasks_dir>          text report
  stamp-check.py <tasks_dir> --json   the document

Exit 0 clean, 1 on any violation, 2 when the directory is missing or holds no
task file — an unreadable vault must never render as one with no violations.
"""
import argparse, glob, json, os, re, sys

TERMINAL = {"completed", "aborted"}
_STAMP = re.compile(r"^claude_session_id:[ \t]*['\"]?([^'\"\n]*?)['\"]?[ \t]*$", re.M)
_STATUS = re.compile(r"^status:[ \t]*['\"]?([A-Za-z_]+)", re.M)
_WORKER = re.compile(r"^[ \t]*-?[ \t]*session_id:[ \t]*['\"]?([0-9a-f-]{8,})", re.M)


def frontmatter(text):
    """The block between the first two `---` lines; empty when there is none."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return ""
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[1:i])
    return ""


def stamp(fm):
    """The stamp, or `None` for an absent or empty field.

    `[ \\t]*`, never `\\s*`: `\\s` crosses the newline after an empty
    `claude_session_id:` and returns the next line as the value.
    """
    m = _STAMP.search(fm)
    return (m.group(1).strip() or None) if m else None


def status(fm):
    m = _STATUS.search(fm)
    return m.group(1) if m else ""


def worked_by(fm):
    """Session ids listed under `metrics_sessions` — the sessions that worked it."""
    block = re.search(r"^metrics_sessions:[ \t]*\n((?:[ \t].*\n?)*)", fm + "\n", re.M)
    return set(_WORKER.findall(block.group(1))) if block else set()


def classify(tasks_dir):
    files = {}
    for p in sorted(glob.glob(os.path.join(tasks_dir, "*.md"))):
        try:
            with open(p, encoding="utf-8") as fh:
                fm = frontmatter(fh.read())
        except (OSError, UnicodeDecodeError):
            continue
        files[os.path.basename(p)[:-3]] = fm

    by_stamp, unstamped = {}, 0
    for name, fm in files.items():
        s = stamp(fm)
        if s is None:
            unstamped += 1
            continue
        by_stamp.setdefault(s, []).append(name)

    violations, suspects, permitted = [], [], []
    for s, names in sorted(by_stamp.items()):
        if len(names) < 2:
            continue
        # No `status:` is not closed: count it open so a malformed file raises a
        # violation rather than hiding one.
        opened = [n for n in names if status(files[n]) not in TERMINAL]
        entry = {"stamp": s, "files": sorted(names), "open": sorted(opened)}
        if len(opened) >= 2:
            violations.append(entry)
            continue
        if len(opened) == 1:
            workers = worked_by(files[opened[0]])
            if workers and s not in workers:
                entry["worked_by"] = sorted(workers)
                suspects.append(entry)
                continue
        permitted.append(entry)

    return {"files": len(files), "stamps": len(by_stamp), "unstamped": unstamped,
            "violations": violations, "suspects": suspects, "permitted": permitted}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tasks_dir")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.tasks_dir):
        print(f"stamp-check: {a.tasks_dir} is not a directory — refusing to report it clean", file=sys.stderr)
        return 2
    r = classify(a.tasks_dir)
    if r["files"] == 0:
        print(f"stamp-check: no task files under {a.tasks_dir} — refusing to report it clean", file=sys.stderr)
        return 2

    if a.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"stamp-check — {r['files']} files · {r['stamps']} stamps · {r['unstamped']} unstamped · "
              f"{len(r['violations'])} violating · {len(r['suspects'])} suspect · "
              f"{len(r['permitted'])} permitted multi-file")
        for v in r["violations"]:
            print(f"  ❌ {v['stamp']} — {len(v['open'])} open tasks: {'; '.join(v['open'])}")
        for v in r["suspects"]:
            print(f"  ⚠️  {v['stamp']} — on open task {v['open'][0]!r}, "
                  f"which was worked by {', '.join(w[:8] for w in v['worked_by'])}")
    return 1 if r["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
