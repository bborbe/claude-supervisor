#!/usr/bin/env python3
"""Resolve whether a task has a wide-reach decision pending, and whether it is accepted.

One resolver, called by both entry points:

  * the **post path** — `attention-ask.py` imports `resolve_task()` and refuses a
    deciding card when the resolved task has a pending decision (SC1);
  * the **close path** — `vault-cli` shells out to `resolve` and refuses to close
    a task whose required record is not accepted (SC2).

The join is the task's `task_identifier` UUID, not its title: a title scan misses
when the record spells it differently and matches the wrong task when two vaults
hold the same name. A title link is accepted only when that title is unique across
every registered vault.

⚠️ **Failure is refusal, never allow.** A missing, unreadable or failing resolver
must not reopen the fail-open hole this module exists to close — so every error
path returns `status: "unresolvable"` and both callers treat that as refuse.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_NONE = "none"
STATUS_UNRESOLVABLE = "unresolvable"

_DEFAULT_ROOT = os.path.expanduser("~/Documents/Obsidian")

# Task directory names are per-vault configured, not fixed: private-personal and
# the seibert vaults use 24/25, while private-starcitizen and private-family use
# 23/24. Both pairs are scanned; a vault with neither is an incomplete scan.
TASK_DIR_PAIRS = (("25 Tasks",), ("24 Tasks",), ("23 Tasks",))


def split_frontmatter(text):
    """Return (frontmatter_dict, body). A parse miss yields ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    front = {}
    for line in parts[1].splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        front[key.strip()] = value.strip().strip("'\"")
    return front, parts[2]


def body_hash(text):
    """sha256 of the body after the closing `---`, trailing whitespace stripped.

    Frontmatter edits do not change it; a body edit does. That is the point: the
    ack binds to the decision text the operator actually read.
    """
    _, body = split_frontmatter(text)
    return hashlib.sha256(body.rstrip().encode("utf-8")).hexdigest()


def _iter_vault_dirs(root):
    try:
        vaults = sorted(os.listdir(root))
    except OSError as err:
        raise RuntimeError(f"cannot list {root}: {err}") from err
    for vault in vaults:
        yield os.path.join(root, vault)


def iter_task_files(root):
    seen_any_dir = False
    for vault_dir in _iter_vault_dirs(root):
        for names in TASK_DIR_PAIRS:
            for name in names:
                directory = os.path.join(vault_dir, name)
                if not os.path.isdir(directory):
                    continue
                seen_any_dir = True
                for entry in sorted(os.listdir(directory)):
                    if entry.endswith(".md"):
                        yield os.path.join(directory, entry)
    if not seen_any_dir:
        raise RuntimeError(f"incomplete scan: no task directory under {root}")


def iter_decision_files(root):
    for vault_dir in _iter_vault_dirs(root):
        directory = os.path.join(vault_dir, "40 Decisions")
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if entry.endswith(".md"):
                yield os.path.join(directory, entry)


def find_task_by_session(session_id, root=None):
    """Return (path, frontmatter, body) for the in-progress task of this session."""
    root = root or _DEFAULT_ROOT
    for path in iter_task_files(root):
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        front, body = split_frontmatter(text)
        if front.get("claude_session_id") != session_id:
            continue
        if str(front.get("status", "")).strip().lower() not in ("in_progress", "todo"):
            continue
        return path, front, body
    return None


def _title_occurrences(task_name, root):
    """How many tasks across every registered vault carry this exact title."""
    count = 0
    for path in iter_task_files(root):
        if os.path.splitext(os.path.basename(path))[0] == task_name:
            count += 1
    return count


def _record_matches(front, task_name, task_id, title_unique):
    related = str(front.get("related_task", "")).strip()
    if not related:
        return False
    if task_id and task_id in related:
        return True
    if title_unique and related == f"[[{task_name}]]":
        return True
    return False


def resolve(task_path, root=None):
    """Resolve the decision state for one task file.

    Returns a dict: {status, task, record, reason}. `status` is one of
    pending / accepted / none / unresolvable. Callers refuse on `pending` and
    `unresolvable`; `accepted` and `none` allow.
    """
    root = root or _DEFAULT_ROOT
    try:
        with open(task_path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as err:
        return {"status": STATUS_UNRESOLVABLE, "task": task_path, "record": None,
                "reason": f"cannot read task: {err}"}

    front, _ = split_frontmatter(text)
    task_name = os.path.splitext(os.path.basename(task_path))[0]
    task_id = str(front.get("task_identifier", "")).strip()
    required = str(front.get("required_decision", "")).strip()

    try:
        title_unique = _title_occurrences(task_name, root) == 1
        candidates = []
        for record_path in iter_decision_files(root):
            try:
                with open(record_path, encoding="utf-8") as handle:
                    record_text = handle.read()
            except OSError:
                continue
            record_front, _ = split_frontmatter(record_text)
            if not _record_matches(record_front, task_name, task_id, title_unique):
                continue
            candidates.append((record_path, record_text, record_front))
    except RuntimeError as err:
        return {"status": STATUS_UNRESOLVABLE, "task": task_path, "record": None,
                "reason": str(err)}

    if required:
        named = [c for c in candidates
                 if os.path.splitext(os.path.basename(c[0]))[0] == required]
        if not named:
            return {"status": STATUS_UNRESOLVABLE, "task": task_path, "record": required,
                    "reason": f"required_decision {required!r} names no existing record"}

    if not candidates:
        return {"status": STATUS_NONE, "task": task_path, "record": None, "reason": ""}

    # A record counts as accepted only when acked_revision matches the current
    # body hash. `reviewed: true` alone is not evidence: a hand edit writes it too.
    for record_path, record_text, record_front in candidates:
        acked = str(record_front.get("acked_revision", "")).strip()
        if acked and acked == body_hash(record_text):
            return {"status": STATUS_ACCEPTED, "task": task_path,
                    "record": record_path, "reason": ""}

    return {"status": STATUS_PENDING, "task": task_path,
            "record": candidates[0][0],
            "reason": "record present but no matching acked_revision"}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != "resolve":
        print("usage: task-decision-resolver.py resolve --task <path>", file=sys.stderr)
        return 64
    task_path, root = "", None
    it = iter(argv[1:])
    for arg in it:
        if arg == "--task":
            task_path = next(it, "")
        elif arg == "--root":
            root = next(it, "")
    if not task_path:
        print("REFUSED: --task is required", file=sys.stderr)
        return 2
    result = resolve(task_path, root)
    print(json.dumps(result))
    return 0 if result["status"] in (STATUS_ACCEPTED, STATUS_NONE) else 2


if __name__ == "__main__":
    sys.exit(main())
