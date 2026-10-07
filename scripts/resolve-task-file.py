#!/usr/bin/env python3
"""Resolve a session's task file by name, across every configured vault.

One home for the fleet sweep's task-file resolution. `agents/fleet-sweep-reader.md`
step 4 calls this; the rule is not restated there beyond what a caller needs.

**Why this is a script and not a snippet in the agent file.** The rule regressed once
already — a first cut treated any two hits as ambiguous, which broke the task-over-goal
precedence for a name that is a task in one folder and a goal in the *same* vault
(`BRO-22032 MDM Merge of Parties based on names` is exactly that shape). A rule carried
as prose in a markdown file has nothing to run against it, so the five cases that caught
the regression lived only in a PR body. Extracted 2026-10-07 so
`scripts/tests/test_resolve_task_file.py` pins them.

**Resolution is tier by tier, never globally:**

  * exactly one `tasks_dir` hit across all vaults  -> that path
  * else, with NO `tasks_dir` hit at all, exactly one `goals_dir` hit -> that path
  * else -> no resolution

An ambiguous `tasks_dir` tier does **not** fall through to `goals_dir`: the condition is
`not tasks` (zero hits), not "tasks had no unique hit", so two colliding tasks mean no
resolution rather than a silent drop to the goal tier. A name that is a task in one vault
and a goal in another resolves to the *task* — the precedence is a property of the name,
not of the vault.

**Never guesses.** Zero hits, or more than one inside the tier reached, prints nothing on
stdout; the colliding paths go to stderr as `AMBIGUOUS <path>` so the caller can name them
in NOTES. That is `docs/subject-resolution.md` § The page test — exact basename, the folder
is the discriminator, both match or neither -> STOP, and every candidate path printed.

Exits 0 even when it resolves nothing. A resolution failure degrades to "unowned", and
step 4 is explicitly not skippable, so this must never raise through the round.
"""
import json
import os
import subprocess
import sys


def vault_dirs():
    """Every configured vault as `{path, tasks_dir, goals_dir}`.

    Guarded the way `fleet-sessions.py`'s `vault_dirs_from_cli()` is: a non-zero exit, a
    timeout, a missing binary or an unreadable config all degrade to no vaults rather than
    raising. ⚠️ The loop over the payload is INSIDE the guard for the same reason, and that
    placement is the defect this function was extracted to fix — a payload that parses to
    something other than a list of dicts (a bare object, or a dict keyed by vault name)
    otherwise reaches `.get()` on a string and raises `AttributeError` straight through a
    pass the reader declares "not skippable, and not partially skippable".
    """
    try:
        res = subprocess.run(
            ["vault-cli", "--output", "json", "config", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode != 0:
            return []
        out = []
        for v in json.loads(res.stdout):
            if not isinstance(v, dict):
                continue
            path = os.path.expanduser(v.get("path") or "")
            if path:
                out.append({
                    "path": path,
                    "tasks_dir": v.get("tasks_dir") or "",
                    "goals_dir": v.get("goals_dir") or "",
                })
        return out
    except Exception:
        return []


def hits(vaults, key, stem):
    """Every `<stem>.md` under `<vault>/<key>`, across all vaults.

    Exact basename, case-insensitive — never a substring glob, and the folder is the
    discriminator rather than a frontmatter field (`docs/subject-resolution.md`).
    """
    found = []
    for v in vaults:
        d = v[key]
        if not d:
            continue                       # assistant-* vaults carry no dirs; never probe the root
        full = os.path.join(v["path"], d)
        try:
            entries = os.listdir(full)
        except OSError:
            continue
        found += [os.path.join(full, e) for e in entries if e.casefold() == stem]
    return found


def resolve(name, vaults):
    """The rule. Returns `(path, ambiguous)`; `path` is `""` when nothing resolves."""
    stem = name.casefold() + ".md"
    tasks = hits(vaults, "tasks_dir", stem)
    goals = hits(vaults, "goals_dir", stem)
    if len(tasks) == 1:
        return tasks[0], []                # a task beats a same-named goal, in any vault
    if not tasks and len(goals) == 1:
        return goals[0], []
    return "", tasks + goals               # zero, or ambiguous — never guess


def main():
    if len(sys.argv) != 2:
        print("usage: resolve-task-file.py <name>", file=sys.stderr)
        return 2
    path, ambiguous = resolve(sys.argv[1], vault_dirs())
    for p in ambiguous:
        print(f"AMBIGUOUS {p}", file=sys.stderr)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
